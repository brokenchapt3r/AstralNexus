"""Auditable, domain-neutral tools used by AstralNexus workers.

The LLM may select these tools, but every call is dispatched here, bounded by
timeouts, and returned as structured evidence.  Optional providers are
reported unavailable instead of being represented as fake capabilities.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from AstralNexus.tools.registry import registry, tool_error, tool_result
from AstralNexus.tools.approval import is_tool_approved
from AstralNexus.tools.context import get_tool_workspace


def _workspace() -> Path:
    bound = get_tool_workspace()
    if bound is not None:
        return bound
    return Path(os.environ.get("ASTRALNEXUS_WORKSPACE", os.getcwd())).resolve()


def _inside_workspace(raw: str) -> Path:
    # Some OpenAI-compatible gateways/model outputs preserve JSON escaping
    # in Windows absolute paths (``C:\\\\work\\\\file``).  Normalize only
    # drive-qualified paths; do not alter UNC paths or ordinary argv values.
    if os.name == "nt" and len(raw) >= 3 and raw[1] == ":":
        raw = raw.replace("\\\\", "\\")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _workspace() / path
    resolved = path.resolve()
    if resolved != _workspace() and _workspace() not in resolved.parents:
        raise ValueError("path must stay inside ASTRALNEXUS_WORKSPACE")
    return resolved


_PROTECTED_PATH_PARTS = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".docker",
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials", "credential", "secrets", "secret", "tokens",
})
_PROTECTED_FILE_NAMES = frozenset({
    "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
    "credentials.json", "secrets.json", "service-account.json",
})


def _assert_safe_workspace_path(path: Path, *, operation: str) -> None:
    """Reject common credential and source-control paths inside the workspace."""
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if parts.intersection(_PROTECTED_PATH_PARTS) or name in _PROTECTED_FILE_NAMES:
        raise PermissionError(
            f"protected_path_denied: {operation} is not allowed for sensitive path '{path.name}'"
        )


def _limit(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AstralNexus/1.0", "Accept": "application/json", **(headers or {})},
    )
    timeout = _limit(os.environ.get("ASTRALNEXUS_TOOL_TIMEOUT"), 20, 120)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_get(args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("https://", "http://")):
        return tool_error("url must use http or https")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AstralNexus/1.0"})
        with urllib.request.urlopen(request, timeout=_limit(args.get("timeout"), 20, 120)) as response:
            body = response.read(_limit(args.get("max_bytes"), 200_000, 2_000_000))
            return tool_result({"status": "success", "url": url, "status_code": response.status,
                                "content_type": response.headers.get("Content-Type", ""),
                                "body": body.decode("utf-8", errors="replace")})
    except Exception as exc:
        return tool_error(str(exc), status="error", url=url)


def _openalex_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    params = {"search": query, "per-page": str(_limit(args.get("limit"), 10, 50))}
    year_from = args.get("year_from")
    year_to = args.get("year_to")
    if year_from or year_to:
        lo, hi = year_from or "1900", year_to or "2100"
        params["filter"] = f"publication_year:{lo}-{hi}"
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if key:
        params["api_key"] = key
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
        results = []
        for item in data.get("results", []):
            results.append({"id": item.get("id"), "title": item.get("title"),
                            "year": item.get("publication_year"),
                            "doi": item.get("doi"), "type": item.get("type"),
                            "open_access": item.get("open_access"),
                            "cited_by_count": item.get("cited_by_count")})
        return tool_result({"status": "success", "source": "OpenAlex", "query": query,
                            "count": data.get("meta", {}).get("count"), "results": results,
                            "evidence_refs": [item.get("id") for item in results if item.get("id")]})
    except Exception as exc:
        return tool_error(str(exc), status="error", source="OpenAlex", query=query)


def _crossref_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    params = urllib.parse.urlencode({"query": query, "rows": _limit(args.get("limit"), 10, 50)})
    try:
        data = _get_json("https://api.crossref.org/works?" + params)
        results = []
        for item in data.get("message", {}).get("items", []):
            results.append({"title": (item.get("title") or [""])[0],
                            "doi": item.get("DOI"), "type": item.get("type"),
                            "published": item.get("published-print") or item.get("published-online"),
                            "url": item.get("URL")})
        return tool_result({"status": "success", "source": "Crossref", "query": query, "results": results,
                            "evidence_refs": [f"https://doi.org/{x['doi']}" for x in results if x.get("doi")]})
    except Exception as exc:
        return tool_error(str(exc), status="error", source="Crossref", query=query)


def _arxiv_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    params = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0,
                                     "max_results": _limit(args.get("limit"), 10, 50)})
    url = "https://export.arxiv.org/api/query?" + params
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AstralNexus/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            root = ET.fromstring(response.read())
        ns = {"a": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("a:entry", ns):
            results.append({"id": (entry.findtext("a:id", default="", namespaces=ns)),
                            "title": " ".join((entry.findtext("a:title", default="", namespaces=ns)).split()),
                            "published": entry.findtext("a:published", default="", namespaces=ns),
                            "summary": " ".join((entry.findtext("a:summary", default="", namespaces=ns)).split()),
                            "authors": [x.findtext("a:name", default="", namespaces=ns) for x in entry.findall("a:author", ns)]})
        return tool_result({"status": "success", "source": "arXiv", "query": query, "results": results,
                            "evidence_refs": [x["id"] for x in results if x.get("id")]})
    except Exception as exc:
        return tool_error(str(exc), status="error", source="arXiv", query=query)


def _github_repo(args: dict[str, Any]) -> str:
    repo = str(args.get("repo", "")).strip().strip("/")
    if repo.count("/") != 1:
        return tool_error("repo must be owner/name")
    path = str(args.get("path", "")).strip().lstrip("/")
    url = "https://api.github.com/repos/" + repo + "/contents/" + urllib.parse.quote(path, safe="/")
    headers = {"Accept": "application/vnd.github.object+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        data = _get_json(url, headers=headers)
        if isinstance(data, dict) and data.get("content") and data.get("encoding") == "base64":
            import base64
            data["decoded_content"] = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            data.pop("content", None)
        return tool_result({"status": "success", "source": "GitHub", "repo": repo, "path": path, "data": data,
                            "evidence_refs": [data.get("html_url")] if isinstance(data, dict) and data.get("html_url") else []})
    except Exception as exc:
        return tool_error(str(exc), status="error", source="GitHub", repo=repo, path=path)


def _file_read(args: dict[str, Any]) -> str:
    try:
        path = _inside_workspace(str(args.get("path", "")))
        _assert_safe_workspace_path(path, operation="read")
        text = path.read_text(encoding=str(args.get("encoding", "utf-8")))
        limit = _limit(args.get("max_chars"), 100_000, 1_000_000)
        return tool_result({"status": "success", "path": str(path), "content": text[:limit],
                            "truncated": len(text) > limit, "bytes": path.stat().st_size})
    except Exception as exc:
        return tool_error(str(exc), status="error")


def _looks_like_long_lived_process(command: list[str]) -> bool:
    """Identify commands that are expected to stay alive as local services."""
    text = " ".join(str(item).lower() for item in command)
    return any(
        marker in text
        for marker in (
            "uvicorn ", "flask run", "npm start", "npm run dev",
            "npm run start", "node server", "python -m http.server",
            # Python workers often embed the server entry point instead of
            # invoking a named CLI.  Treat these as long-lived services too.
            "app.run(", "uvicorn.run(", "run_simple(", "serve_forever(",
            "http.server", "socketserver.",
            # Windows ``start /b`` is itself an explicit background-process
            # request. Running it through subprocess.run keeps redirected
            # handles open and makes the tool appear to hang.
            "start /b ",
        )
    )


def _process_registry_path(root: Path) -> Path:
    return root / ".astralnexus_process_logs" / "process_registry.json"


def _stop_services_referencing(path: Path) -> None:
    """Stop only run-owned background services that reference ``path``.

    A Worker may repair a service source file while an older process is still
    listening.  Without a lifecycle boundary, tests silently hit stale code.
    The registry is scoped to the run workspace and records only processes
    started by this tool, so unrelated user services are never touched.
    """
    current = path.resolve()
    for parent in [current.parent, *current.parents]:
        registry_path = _process_registry_path(parent)
        if not registry_path.is_file():
            continue
        try:
            entries = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            entries = []
        changed = False
        kept = []
        for entry in entries if isinstance(entries, list) else []:
            command = [str(item) for item in (entry.get("command") or [])]
            pid = entry.get("pid")
            referenced = any(Path(item).name.casefold() == current.name.casefold() for item in command)
            if not referenced:
                kept.append(entry)
                continue
            try:
                if pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            changed = True
        if changed:
            try:
                registry_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        return


def _run_process(args: dict[str, Any], command: list[str], tool_name: str) -> str:
    try:
        cwd = _inside_workspace(str(args.get("cwd", ".")))
        # Package installation is an external side effect with a different
        # failure mode from ordinary validation.  Do not let a provider
        # repeatedly hang on pip/npm network access during a real task.  The
        # capability is explicit; when it is not granted, return a fast,
        # actionable result so the Worker can use an already-installed or
        # standard-library implementation and record the decision.
        command_text = " ".join(str(item).lower() for item in command)
        install_markers = (
            "pip install", "pip3 install", "python -m pip install",
            "npm install", "npm i ", "pnpm install", "yarn install",
        )
        package_install_allowed = os.environ.get(
            "ASTRALNEXUS_ALLOW_PACKAGE_INSTALL", "false"
        ).lower() in {"1", "true", "yes"}
        if any(marker in command_text for marker in install_markers) and not package_install_allowed:
            return tool_error(
                "package_install_not_authorized: package installation is disabled for this run; "
                "use an installed dependency or Python/Node standard library, or explicitly "
                "enable ASTRALNEXUS_ALLOW_PACKAGE_INSTALL=true",
                status="approval_required",
                tool=tool_name,
                command=command,
                cwd=str(cwd),
                next_action="use_existing_dependency_or_request_explicit_package_install",
            )
        # A missing timeout must not consume the whole node deadline.  Models
        # may request a longer build/test explicitly, but the safe default is
        # short enough for the orchestrator to observe a failed tool and
        # choose a different action within the same node.
        # A model may request a very large timeout for a foreground server or
        # an accidentally hanging build.  That would consume the whole node
        # deadline and prevent Governor recovery.  Keep an explicit, bounded
        # per-tool ceiling; long-lived services must be started by a detached
        # project script and checked through a separate health request.
        requested_timeout = _limit(args.get("timeout"), 45, 900)
        process_ceiling = _limit(
            os.environ.get("ASTRALNEXUS_PROCESS_TIMEOUT_MAX"), 120, 900
        )
        process_timeout = min(requested_timeout, process_ceiling)
        process_env = os.environ.copy()
        if tool_name in {"python_exec", "pytest_run"}:
            # Windows commonly inherits a GBK console.  Real validation
            # scripts may legitimately print Unicode status markers; force
            # UTF-8 so a successful check is not turned into a false failure.
            process_env.setdefault("PYTHONIOENCODING", "utf-8")
            process_env.setdefault("PYTHONUTF8", "1")
        if tool_name == "pytest_run" or any(
            "pytest" in str(part).casefold() for part in command
        ):
            # The host may have unrelated pytest entry points (for example a
            # missing Brownie plugin).  Project-owned tests must be isolated
            # from those plugins so their real result reaches the Worker.
            process_env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        if bool(args.get("background")) or _looks_like_long_lived_process(command):
            # A server is a background artifact, not a foreground shell step.
            # Detach it so the Worker can continue to health checks and record
            # a PID/log evidence reference instead of receiving a false timeout.
            log_dir = cwd / ".astralnexus_process_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{tool_name}_{int(time.time() * 1000)}.log"
            log_handle = log_path.open("a", encoding="utf-8", errors="replace")
            creation_flags = 0
            if os.name == "nt":
                creation_flags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=process_env,
                creationflags=creation_flags,
                start_new_session=os.name != "nt",
            )
            log_handle.close()
            registry_path = _process_registry_path(cwd)
            try:
                registry_path.parent.mkdir(parents=True, exist_ok=True)
                entries = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else []
                if not isinstance(entries, list):
                    entries = []
                entries.append({
                    "pid": process.pid,
                    "command": command,
                    "cwd": str(cwd),
                    "log_path": str(log_path),
                    "started_at": time.time(),
                })
                registry_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                pass
            return tool_result({
                "status": "started",
                "tool": tool_name,
                "pid": process.pid,
                "command": command,
                "cwd": str(cwd),
                "log_path": str(log_path),
                "background": True,
            })
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=process_timeout,
            check=False,
            env=process_env,
        )
        return tool_result({"status": "success" if completed.returncode == 0 else "failed",
                            "tool": tool_name, "returncode": completed.returncode,
                            "stdout": completed.stdout[-200_000:], "stderr": completed.stderr[-100_000:],
                            "cwd": str(cwd), "timeout_seconds": process_timeout})
    except FileNotFoundError as exc:
        # Windows installations frequently expose tools through a shell
        # shim (pytest, curl, npm, or a project script) rather than a direct
        # executable.  Retry the exact argv through cmd.exe so a missing
        # shim is not misreported as a product failure.
        if tool_name == "terminal_exec" and os.name == "nt":
            try:
                shell_command = subprocess.list2cmdline(command)
                if command and str(command[0]).casefold() in {"pip", "pip3"}:
                    # Python environments on Windows often expose pip only
                    # as a module, not as a PATH executable.
                    shell_command = subprocess.list2cmdline(
                        [sys.executable, "-m", "pip", *command[1:]]
                    )
                completed = subprocess.run(
                    ["cmd.exe", "/d", "/s", "/c", shell_command],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=process_timeout,
                    check=False,
                    env=process_env,
                )
                return tool_result({
                    "status": "success" if completed.returncode == 0 else "failed",
                    "tool": tool_name,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-200_000:],
                    "stderr": completed.stderr[-100_000:],
                    "cwd": str(cwd),
                    "shell_fallback": True,
                    "timeout_seconds": process_timeout,
                })
            except Exception as fallback_exc:
                return tool_error(str(fallback_exc), status="error", tool=tool_name)
        return tool_error(str(exc), status="error", tool=tool_name)
    except Exception as exc:
        return tool_error(str(exc), status="error", tool=tool_name)


def _python_exec(args: dict[str, Any]) -> str:
    code = str(args.get("code", ""))
    if not code.strip():
        return tool_error("code is required")
    # Windows limits the complete command line to roughly 32 KiB.  Real Agent
    # validation scripts routinely exceed that boundary, so passing source via
    # ``python -c`` turns valid code into WinError 206.  Execute a temporary
    # workspace script instead; this also keeps shell quoting out of the path.
    try:
        cwd = _inside_workspace(str(args.get("cwd", ".")))
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix=".astralnexus_python_",
            dir=cwd,
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(code)
            script_path = Path(handle.name)
        try:
            return _run_process(
                args,
                [sys.executable, str(script_path)],
                "python_exec",
            )
        finally:
            script_path.unlink(missing_ok=True)
    except Exception as exc:
        return tool_error(str(exc), status="error", tool="python_exec")


def _pytest_run(args: dict[str, Any]) -> str:
    target = str(args.get("target", "")).strip()
    if not target:
        return tool_error("target is required")
    path = _inside_workspace(target)
    return _run_process(args, [sys.executable, "-m", "pytest", str(path), "-q"], "pytest_run")


def _search_files(args: dict[str, Any]) -> str:
    """Search workspace text files with bounded output."""
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    root = _inside_workspace(str(args.get("path", ".")))
    max_results = _limit(args.get("max_results"), 30, 200)
    max_files = _limit(args.get("max_files"), 5000, 20_000)
    max_file_bytes = _limit(args.get("max_file_bytes"), 2_000_000, 10_000_000)
    timeout = max(0.5, min(float(args.get("timeout", 8)), 30.0))
    deadline = time.monotonic() + timeout
    skipped_dirs = {
        ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build",
    }
    matches = []
    scanned = 0
    truncated_reason = ""
    try:
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = [name for name in dirs if name not in skipped_dirs]
            for filename in files:
                if time.monotonic() >= deadline:
                    truncated_reason = "timeout"
                    break
                if scanned >= max_files:
                    truncated_reason = "max_files"
                    break
                path = Path(current) / filename
                scanned += 1
                try:
                    if path.stat().st_size > max_file_bytes:
                        continue
                except OSError:
                    continue
                try:
                    _assert_safe_workspace_path(path, operation="search")
                except PermissionError:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for line_no, line in enumerate(text.splitlines(), 1):
                    if query.lower() in line.lower():
                        matches.append({"path": str(path), "line": line_no, "text": line[:500]})
                        if len(matches) >= max_results:
                            truncated_reason = "max_results"
                            break
                if truncated_reason:
                    break
            if truncated_reason:
                break
        return tool_result({"status": "success", "query": query, "matches": matches,
                            "scanned_files": scanned,
                            "truncated": bool(truncated_reason),
                            "truncated_reason": truncated_reason})
    except Exception as exc:
        return tool_error(str(exc), status="error")


class _SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self._current = None
        self._capture = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", ""):
            self._current = {"url": attrs.get("href", ""), "title": ""}
            self._capture = True

    def handle_data(self, data):
        if self._capture and self._current is not None:
            self._current["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._capture and self._current is not None:
            self._current["title"] = " ".join(self._current["title"].split())
            self.items.append(self._current)
            self._current = None
            self._capture = False


def _web_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    limit = _limit(args.get("limit"), 8, 20)
    try:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(url, headers={"User-Agent": "AstralNexus/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read(500_000).decode("utf-8", errors="replace")
        parser = _SearchResultParser()
        parser.feed(html)
        results = parser.items[:limit]
        source = "DuckDuckGo HTML"
        if not results:
            # DuckDuckGo may return a 202 challenge page with no result
            # anchors. Bing's public RSS endpoint is structured and avoids
            # coupling the runtime to another unstable HTML layout.
            rss_url = "https://www.bing.com/search?" + urllib.parse.urlencode(
                {"q": query, "format": "rss"}
            )
            rss_request = urllib.request.Request(
                rss_url,
                headers={"User-Agent": "AstralNexus/1.0"},
            )
            with urllib.request.urlopen(rss_request, timeout=20) as response:
                rss = response.read(500_000)
            root = ET.fromstring(rss)
            results = [
                {
                    "title": " ".join((item.findtext("title") or "").split()),
                    "url": (item.findtext("link") or "").strip(),
                    "snippet": " ".join((item.findtext("description") or "").split())[:1000],
                }
                for item in root.findall(".//item")[:limit]
                if (item.findtext("link") or "").strip()
            ]
            source = "Bing RSS"
        return tool_result({"status": "success", "source": source, "query": query,
                            "results": results, "evidence_refs": [x["url"] for x in results if x.get("url")]})
    except Exception as exc:
        return tool_error(str(exc), status="error", source="DuckDuckGo", query=query)


def _file_write(args: dict[str, Any]) -> str:
    if not is_tool_approved("file_write"):
        return tool_error("approval_required: set ASTRALNEXUS_ALLOW_FILE_WRITE=true for file writes", status="approval_required")
    try:
        path = _inside_workspace(str(args.get("path", "")))
        _assert_safe_workspace_path(path, operation="write")
        _stop_services_referencing(path)
        content = str(args.get("content", ""))
        if len(content) > 1_000_000:
            return tool_error("content exceeds 1,000,000 characters")
        extensionless_files = {
            "LICENSE", "LICENSE.txt", "Makefile", "Dockerfile",
            ".gitignore", ".dockerignore", ".gitkeep", ".keep", "Procfile",
        }
        looks_like_directory = (
            path.exists() and path.is_dir()
        ) or (
            not path.exists()
            and not path.suffix
            and path.name not in extensionless_files
        )
        if looks_like_directory:
            return tool_error(
                f"target_is_directory: {path}; file_write requires a concrete file path. "
                "Write files inside this directory instead, using a filename with an extension."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".astralnexus.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
        return tool_result({
            "status": "success",
            "path": str(path),
            "bytes": path.stat().st_size,
            "artifact_created": True,
        })
    except Exception as exc:
        return tool_error(str(exc), status="error")


def _normalize_terminal_command(raw: Any) -> list[str]:
    """Normalize OpenAI-compatible tool arguments into one argv list.

    Providers may return the declared array directly, serialize that array as
    a JSON string, or split the JSON representation across array elements.
    Plain strings remain shell commands for AstralNexus-compatible interaction.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if (
            isinstance(parsed, list)
            and parsed
            and all(isinstance(item, str) for item in parsed)
        ):
            return parsed
        return ["cmd.exe", "/d", "/s", "/c", text] if os.name == "nt" else ["/bin/sh", "-lc", text]

    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        return []

    # Some compatible gateways split '["python","--version"]' into
    # ['["python"', '"--version"]']. Reassemble only JSON-looking input so
    # legitimate argv lists are never passed through a shell.
    joined = ",".join(item.strip() for item in raw)
    if joined.lstrip().startswith("[") and joined.rstrip().endswith("]"):
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError:
            parsed = None
        if (
            isinstance(parsed, list)
            and parsed
            and all(isinstance(item, str) for item in parsed)
        ):
            return parsed
    return raw


def _terminal_exec(args: dict[str, Any]) -> str:
    if not is_tool_approved("terminal_exec"):
        return tool_error("approval_required: set ASTRALNEXUS_ALLOW_TERMINAL=true for terminal execution", status="approval_required")
    command = _normalize_terminal_command(args.get("command"))
    if not command:
        return tool_error("command must be a non-empty argv list")
    return _run_process(args, command, "terminal_exec")


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _browser_fetch(args: dict[str, Any]) -> str:
    if not _playwright_available():
        return tool_error("Playwright is not installed; install playwright and browser binaries before enabling browser_fetch", status="unavailable")
    try:
        from playwright.sync_api import sync_playwright
        url = str(args.get("url", "")).strip()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            result = {"status": "success", "url": page.url, "title": page.title(), "text": page.locator("body").inner_text()[:200_000]}
            browser.close()
            return tool_result(result)
    except Exception as exc:
        return tool_error(str(exc), status="error")


registry.register(name="http_get", toolset="network", schema={"name": "http_get", "description": "Fetch a bounded HTTP(S) response for evidence collection.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "max_bytes": {"type": "integer"}}, "required": ["url"]}}, handler=_http_get, max_result_size_chars=300_000)
registry.register(name="openalex_search", toolset="research", schema={"name": "openalex_search", "description": "Search scholarly works in OpenAlex and return identifiers and metadata.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "year_from": {"type": "integer"}, "year_to": {"type": "integer"}}, "required": ["query"]}}, handler=_openalex_search)
registry.register(name="crossref_search", toolset="research", schema={"name": "crossref_search", "description": "Search Crossref works and return DOI-backed metadata.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}, handler=_crossref_search)
registry.register(name="arxiv_search", toolset="research", schema={"name": "arxiv_search", "description": "Search arXiv and return paper identifiers and abstracts.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}, handler=_arxiv_search)
registry.register(name="github_repo_contents", toolset="software", schema={"name": "github_repo_contents", "description": "Read a public GitHub repository file or directory with source references.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "path": {"type": "string"}}, "required": ["repo"]}}, handler=_github_repo)
registry.register(name="file_read", toolset="file", schema={"name": "file_read", "description": "Read a text file inside the AstralNexus workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["path"]}}, handler=_file_read)
registry.register(name="python_exec", toolset="execution", schema={"name": "python_exec", "description": "Run a bounded Python experiment in the AstralNexus workspace.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["code"]}}, handler=_python_exec)
registry.register(name="pytest_run", toolset="execution", schema={"name": "pytest_run", "description": "Run pytest against a workspace path and return stdout/stderr evidence.", "parameters": {"type": "object", "properties": {"target": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["target"]}}, handler=_pytest_run)
registry.register(name="search_files", toolset="file", schema={"name": "search_files", "description": "Search text files inside the local AstralNexus workspace with bounded results. This tool does not search the public web.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "max_results": {"type": "integer"}, "max_files": {"type": "integer"}, "max_file_bytes": {"type": "integer"}, "timeout": {"type": "number"}}, "required": ["query"]}}, handler=_search_files)
registry.register(name="web_search", toolset="network", schema={"name": "web_search", "description": "Search the public web and return source URLs for evidence.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}, handler=_web_search)
registry.register(name="file_write", toolset="file", schema={"name": "file_write", "description": "Write a UTF-8 text file inside the workspace after explicit approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}, handler=_file_write, requires_approval=True)
registry.register(name="terminal_exec", toolset="execution", schema={"name": "terminal_exec", "description": "Run an argv command in the workspace after explicit approval. Set background=true for a server or other long-lived process; the tool returns its PID and log path immediately.", "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}, "background": {"type": "boolean", "description": "Detach a long-lived process and return PID/log evidence without waiting for exit."}}, "required": ["command"]}}, handler=_terminal_exec, requires_approval=True)
registry.register(name="browser_fetch", toolset="browser", schema={"name": "browser_fetch", "description": "Fetch rendered page text with Playwright when installed.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}, handler=_browser_fetch, check_fn=_playwright_available)
