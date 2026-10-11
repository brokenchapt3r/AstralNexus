"""Auditable, domain-neutral tools used by AstralNexus workers.

The LLM may select these tools, but every call is dispatched here, bounded by
timeouts, and returned as structured evidence.  Optional providers are
reported unavailable instead of being represented as fake capabilities.
"""
from __future__ import annotations

import json
import contextvars
import hashlib
import ast
import os
import re
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from AstralNexus.tools.registry import registry, tool_error, tool_result
from AstralNexus.tools.approval import is_tool_approved
from AstralNexus.tools.context import get_tool_workspace, resolve_tool_path


def _workspace() -> Path:
    bound = get_tool_workspace()
    if bound is not None:
        return bound
    return resolve_tool_path(Path(os.environ.get("ASTRALNEXUS_WORKSPACE", os.getcwd())))


def _inside_workspace(raw: str) -> Path:
    from AstralNexus.runtime.presentation import mapped_user_path
    mapped = mapped_user_path(raw)
    if mapped is not None:
        return mapped
    # Some OpenAI-compatible gateways/model outputs preserve JSON escaping
    # in Windows absolute paths (``C:\\\\work\\\\file``).  Normalize only
    # drive-qualified paths; do not alter UNC paths or ordinary argv values.
    if os.name == "nt" and len(raw) >= 3 and raw[1] == ":":
        raw = raw.replace("\\\\", "\\")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _workspace() / path
    resolved = resolve_tool_path(path)
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


def _text_window(text: str, args: dict[str, Any], limit: int) -> dict[str, Any]:
    """Address a bounded window of ``text`` by char offset or by 1-based line range.

    ``file_read`` used to return only ``text[:max_chars]``.  A worker that needed a
    region past that head had no way to ask for it, so large-file inspection degraded
    into an unbounded ``python_exec`` slicing loop that consumed the whole node budget
    (observed: one 40 KB module re-read 77 times inside a single execution).  The
    window keeps the same default behaviour (head of file) but always reports where
    the window sits and how to continue, so paging terminates.
    """
    total_chars = len(text)
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    offset_arg = args.get("offset")
    line_mode = start_line is not None or end_line is not None
    if line_mode and offset_arg is not None:
        raise ValueError("Use either start_line/end_line or offset, not both.")

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    if line_mode:
        try:
            first = max(1, int(start_line if start_line is not None else 1))
        except (TypeError, ValueError):
            raise ValueError("start_line must be an integer >= 1") from None
        if end_line is None:
            last = total_lines
        else:
            try:
                last = int(end_line)
            except (TypeError, ValueError):
                raise ValueError("end_line must be an integer") from None
        last = min(max(last, first - 1), total_lines)
        char_start = sum(len(item) for item in lines[: min(first, total_lines + 1) - 1])
        chunk = "".join(lines[first - 1 : last]) if first <= total_lines else ""
        window_mode = "lines"
    else:
        try:
            char_start = max(0, int(offset_arg or 0))
        except (TypeError, ValueError):
            raise ValueError("offset must be a non-negative integer") from None
        char_start = min(char_start, total_chars)
        chunk = text[char_start:]
        first = text.count("\n", 0, char_start) + 1
        last = total_lines
        window_mode = "offset" if offset_arg else "head"

    clipped = len(chunk) > limit
    if clipped:
        chunk = chunk[:limit]
        last = first + chunk.count("\n") - (1 if chunk.endswith("\n") else 0)
        last = max(first, min(last, total_lines))

    char_end = char_start + len(chunk)
    eof = char_end >= total_chars
    result: dict[str, Any] = {
        "content": chunk,
        "window_mode": window_mode,
        "offset": char_start,
        "next_offset": None if eof else char_end,
        "returned_chars": len(chunk),
        "total_chars": total_chars,
        "start_line": first if chunk else min(first, total_lines + 1),
        "end_line": last if chunk else first - 1,
        "total_lines": total_lines,
        "eof": eof,
        "truncated": not eof,
    }
    if not eof:
        result["continuation"] = (
            f"window ends at char {char_end} of {total_chars}; continue with "
            f"offset={char_end}, or address a region directly with "
            f"start_line/end_line (search_files returns the line numbers)."
        )
    return result


def _file_read(args: dict[str, Any]) -> str:
    try:
        from AstralNexus.tools.context import read_tool_path
        path = read_tool_path(str(args.get("path", "")))
        _assert_safe_workspace_path(path, operation="read")
        extraction = "text"
        if path.suffix.lower() == ".docx":
            # Extract body text (including table paragraphs), not rendered
            # layout, drawings, headers, or OCR. Never extract archive paths.
            with zipfile.ZipFile(path) as archive:
                member = archive.getinfo("word/document.xml")
                if member.file_size > 20_000_000:
                    raise ValueError("DOCX main document exceeds 20 MB extraction limit")
                document = ET.fromstring(archive.read(member))
            paragraphs = []
            for paragraph in document.iter():
                if paragraph.tag.rsplit("}", 1)[-1] != "p":
                    continue
                parts = []
                for element in paragraph.iter():
                    tag = element.tag.rsplit("}", 1)[-1]
                    if tag == "t":
                        parts.append(element.text or "")
                    elif tag == "tab":
                        parts.append("\t")
                    elif tag in {"br", "cr"}:
                        parts.append("\n")
                paragraphs.append("".join(parts))
            text = "\n".join(paragraphs)
            extraction = "docx_main_body_text_only"
        else:
            raw_content = path.read_bytes()
            text = raw_content.decode(str(args.get("encoding", "utf-8")))
        limit = _limit(args.get("max_chars"), 100_000, 1_000_000)
        window = _text_window(text, args, limit)
        payload = {"status": "success", "path": str(path), "content": window["content"],
                   "truncated": window["truncated"], "bytes": path.stat().st_size,
                   "extraction": extraction,
                   "sha256": hashlib.sha256(raw_content).hexdigest() if extraction == "text" else None}
        payload.update({k: v for k, v in window.items() if k != "content"})
        return tool_result(payload)
    except UnicodeDecodeError as exc:
        return tool_error(
            f"Text decoding failed: {exc}. Retry file_read with the source encoding "
            "(for example encoding='gb18030' for a Windows Chinese log). "
            "For a SQLite/binary file use python_exec with its format reader, not text decoding.",
            status="error",
        )
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


# doc 18 修复G：节点会话身份标签。worker 在编排器线程池内同线程调用
# 工具，ContextVar 即会话作用域；后台服务注册时打标，会话边界按标清理。
_CURRENT_NODE_CONTEXT: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "astralnexus_tool_node", default=""
)


def set_tool_node_context(node_id: str) -> None:
    """标记当前线程后续的后台服务启动归属节点（会话级清理依据）。"""
    _CURRENT_NODE_CONTEXT.set(str(node_id or ""))


def _bind_runtime_executable(command: list[str]) -> list[str]:
    """Bind Python entry points to the interpreter running AstralNexus.

    Windows machines often expose several unrelated ``python``, ``pytest``
    and ``pip`` shims on PATH.  A Worker must not validate with a different
    environment from ``python_exec`` and the runtime itself.
    """
    if not command:
        return command
    executable = Path(str(command[0])).name.casefold()
    rest = [str(item) for item in command[1:]]
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        return [sys.executable, *rest]
    if executable in {"py", "py.exe"}:
        if rest and (rest[0] == "-3" or rest[0].startswith("-3.")):
            rest = rest[1:]
        return [sys.executable, *rest]
    if executable in {"pytest", "pytest.exe"}:
        return [sys.executable, "-m", "pytest", *rest]
    if executable in {"pip", "pip.exe", "pip3", "pip3.exe"}:
        return [sys.executable, "-m", "pip", *rest]
    return command


_OUTPUT_DECODINGS = ("utf-8", "utf-8-sig", "gb18030")


def _decode_output_bytes(data: bytes) -> tuple[str, str]:
    """Decode child-process output under the project encoding convention.

    Children handed a raw file/pipe handle write console-codepage bytes
    directly (Windows Chinese consoles commonly emit cp936/GBK).  Try UTF-8
    first, then gb18030 (a strict superset of GBK/cp936), and only then fall
    back to lossy UTF-8 so bounded diagnostics are never dropped.  Returns
    ``(text, encoding_label)``.
    """
    for encoding in _OUTPUT_DECODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _process_spawn_command(command: list[str]) -> list[str] | str:
    """Preserve an explicit cmd command string at the Windows spawn boundary.

    Popen serializes lists with MS C argv quoting (backslash-quote), which
    cmd.exe does not understand. Only a single explicit /c or /k payload
    containing quotes needs raw shell syntax. Ordinary argv stays shell-free.
    /s strips the one wrapper pair we add, leaving the payload unchanged.
    """
    if sys.platform != "win32" or not command:
        return command
    if Path(command[0]).name.casefold() not in {"cmd", "cmd.exe"}:
        return command
    for index, argument in enumerate(command[1:], 1):
        if argument.casefold() in {"/c", "/k"}:
            if index != len(command) - 2 or '"' not in command[-1]:
                return command
            prefix = command[:index]
            if not any(flag.casefold() == "/s" for flag in prefix[1:]):
                prefix = [*prefix, "/s"]
            return subprocess.list2cmdline([*prefix, argument]) + ' "' + command[-1] + '"'
    return command


def _run_foreground_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    """Run one bounded command and always reap its descendant process tree.

    On Windows, ``subprocess.run(timeout=...)`` can kill only the immediate
    launcher while a pip/npm child keeps the captured pipe open.  The caller
    then appears to hang after the timeout.  Own the process explicitly and
    terminate the complete tree before collecting bounded diagnostics.
    """
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    # File-backed capture avoids a Windows pipe deadlock: package managers
    # may leave grandchildren holding inherited pipe handles after timeout.
    # Regular files can be read immediately even if such a child is slow to
    # exit, so control always returns to Governor recovery.
    # Binary mode is deliberate: the child writes raw bytes straight to the
    # OS handle, bypassing any parent-side text encoding layer.  A text-mode
    # temp file previously turned GBK console output into silent mojibake on
    # read-back; capture bytes faithfully and decode once, parent-side.
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        process = subprocess.Popen(
            _process_spawn_command(command),
            cwd=cwd,
            # Tools must not inherit the web worker control pipe. On Windows
            # a concurrent stdin read can block even Python startup.
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=env,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            from AstralNexus.runtime.process_control import stop_process_tree
            stop_process_tree(process, grace_seconds=0)
        stdout_file.flush()
        stderr_file.flush()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout_text, output_encoding = _decode_output_bytes(stdout_file.read())
        stderr_text, _ = _decode_output_bytes(stderr_file.read())
        return {
            "returncode": int(process.returncode or (-1 if timed_out else 0)),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output_encoding": output_encoding,
            "timed_out": timed_out,
        }


def _kill_process_tree(pid: int) -> None:
    """Forcibly stop one process tree (taskkill /T /F equivalent).

    环境实证（台账 §23.6）：`subprocess.run(capture_output=True)` 触发
    CreatePipe 拒绝（WinError 5），而部分派生进程形态下 taskkill 返回
    Access denied；psutil 的 TerminateProcess 直调对两者都可靠。异常
    全部就地吞掉：调用方语义是尽力清理，不因清理失败中断主流程。
    """
    try:
        import psutil  # type: ignore[import-untyped]

        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        for child in reversed(root.children(recursive=True)):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            root.kill()
        except psutil.NoSuchProcess:
            pass
    except ImportError:
        pass


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
                    _kill_process_tree(int(pid))
            except (OSError, ValueError):
                pass
            changed = True
        if changed:
            try:
                registry_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        return


def _kill_registry_entries(root: Path, predicate) -> int:
    """Stop registered run-owned background services matching ``predicate``.

    doc 18 修复G共享内核：只处理 run 工作区注册表（与
    ``_stop_services_referencing`` 同一边界——只杀本工具启动的进程，
    绝不触碰无关用户服务）。匹配条目经 ``_kill_process_tree``（Windows，
    taskkill /T /F 的 psutil 等价形态）或 killpg（POSIX，注册进程均为
    新会话组长）停止，并从注册表移除。返回停止的条目数。
    """
    stopped = 0
    current = Path(root).resolve()
    for parent in [current, *current.parents]:
        registry_path = _process_registry_path(parent)
        if not registry_path.is_file():
            continue
        try:
            entries = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            entries = []
        kept = []
        changed = False
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not predicate(entry):
                kept.append(entry)
                continue
            pid = entry.get("pid")
            try:
                if pid:
                    if sys.platform == "win32":
                        _kill_process_tree(int(pid))
                    else:
                        try:
                            os.killpg(int(pid), 9)
                        except (OSError, ProcessLookupError):
                            pass
                    stopped += 1
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            changed = True
        if changed:
            try:
                registry_path.write_text(
                    json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        return stopped
    return stopped


def stop_services_for_node(node_id: str, root) -> int:
    """doc 18 修复G：停止指定节点会话启动的全部后台服务。

    会话生命周期边界：节点会话结束（任何结局）后，它启动的服务不再
    是资产而是污染源——attempt3/4/5 实证：泄漏服务占住端口，后续代
    worker 启动失败 → 重复同一动作 → runtime_no_progress 空转，恢复
    链代际全灭，甚至跨 attempt 泄漏。
    """
    target = str(node_id or "")
    if not target:
        return 0
    return _kill_registry_entries(
        Path(root), lambda entry: str(entry.get("node_id") or "") == target
    )


def stop_all_registered_services(root) -> int:
    """doc 18 修复G：run 级硬边界——停止注册表中全部后台服务。

    调用点：run 收尾（所有终态路径，含 blocked/host_exception）与恢复
    接管启动前（清理崩溃代际的遗留进程）。
    """
    return _kill_registry_entries(Path(root), lambda entry: True)


def _run_process(args: dict[str, Any], command: list[str], tool_name: str) -> str:
    try:
        cwd = _inside_workspace(str(args.get("cwd", ".")))
        # cmd parses forward slashes in a batch-file command as switches.
        # Normalize only an existing, standalone script token; never rewrite
        # a compound shell expression or silently enable shell execution.
        if (sys.platform == "win32" and len(command) >= 3
                and Path(command[0]).name.casefold() in {"cmd", "cmd.exe"}
                and command[1].casefold() in {"/c", "/k"}):
            script = command[2]
            if script.lower().endswith((".bat", ".cmd")) and not any(
                marker in script for marker in ('"', '&', '|', '<', '>', '\n', '\r')
            ):
                script_path = (cwd / script).resolve()
                if script_path.is_file():
                    command = [*command[:2], str(script_path), *command[3:]]
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
        # Round 12 (live r11 resume #17, 18:50:25 UTC): a REPAIR worker
        # hunting a stale service executed `taskkill /F /IM python.exe` and
        # mass-killed every python process on the host — the orchestrator
        # itself, the run monitor, and the live evidence service on port
        # 5000.  The run died mid-iteration with zero convergence.  Killing
        # by image/pattern name can never be a legitimate worker operation
        # here: the framework, its tools, and every service any worker
        # started share the same image names.  PID-targeted termination
        # stays allowed; the error text teaches the safe sequence so the
        # worker self-corrects within the same round.
        first_part = str(command[0]).strip().casefold() if command else ""
        # A PID is not proof of service ownership. In particular, tasklist
        # includes the interpreter and its launcher that are running us.
        # Reject self/parent termination before spawning any command.
        protected_pids = {os.getpid(), os.getppid()}
        targeted_pids: set[int] = set()
        if "taskkill" in command_text:
            targeted_pids.update(int(value) for value in re.findall(r"/pid\s+[\"']?(\d+)", command_text))
        if "stop-process" in command_text:
            targeted_pids.update(int(value) for value in re.findall(r"-id\s+[\"']?(\d+)", command_text))
        if targeted_pids & protected_pids:
            return tool_error(
                "runtime_process_kill_blocked: the requested PID belongs to the "
                "current Agent runtime or its parent launcher, not a proven service. "
                "Use the PID from the service's background-start receipt or its "
                "listening socket. An empty port needs a service start, not taskkill.",
                status="error", failure_class="guard_blocked", tool=tool_name,
                command=command, cwd=str(cwd),
                next_action="inspect_service_start_receipt_and_log",
            )
        mass_kill = False
        if "taskkill" in first_part:
            mass_kill = any(
                str(part).strip().casefold().startswith("/im")
                for part in command[1:]
            )
        elif first_part.endswith(("pkill", "killall")):
            mass_kill = True
        elif any(
            marker in command_text
            for marker in (
                "stop-process -name",
                "wmic process where name",
                "wmic process where (name",
            )
        ):
            mass_kill = True
        if not mass_kill and "taskkill" in command_text and "/im" in command_text:
            # Wrapper forms (cmd /c …, powershell -Command …) still carry
            # both tokens in the joined argv text.
            mass_kill = True
        if mass_kill:
            return tool_error(
                "mass_process_kill_blocked: killing processes by image or "
                "pattern name is permanently denied — it kills the "
                "orchestrator, monitors, sibling workers and their services "
                "(live incident: 'taskkill /F /IM python.exe' killed the "
                "whole run). To restart ONE service: locate its PID via "
                "the listening socket for the service port (or reuse "
                "the PID returned by its background start), then run "
                "'taskkill /F /PID <pid>'. If the port is already free, "
                "just start the service again with background=true.",
                status="approval_required",
                failure_class="guard_blocked",
                tool=tool_name,
                command=command,
                cwd=str(cwd),
                next_action="use_pid_targeted_taskkill_or_restart_service",
            )
        command = _bind_runtime_executable(command)
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
        # Windows commonly inherits a GBK console.  Any Python child may
        # legitimately print Unicode status markers — not only direct
        # python_exec/pytest_run, but also flask/uvicorn started through a
        # .bat by terminal_exec (r11 N-004: the background service log came
        # back cp936 and file_read's strict UTF-8 default could not decode
        # it).  Force UTF-8 for every spawn so redirected logs and captured
        # output stay UTF-8-clean; non-Python children ignore these vars and
        # their raw bytes are handled by the decode convention instead.
        # These are framework-owned runtime guarantees, not defaults.  An
        # inherited PYTHONIOENCODING=gbk overrides Python's UTF-8 mode and can
        # make python_exec fail while printing ordinary Unicode diagnostics.
        process_env["PYTHONIOENCODING"] = "utf-8"
        process_env["PYTHONUTF8"] = "1"
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
            # Convention: process logs are byte-faithful records.  The child
            # writes raw console-codepage bytes straight to the OS handle —
            # a text-mode wrapper here never applied and only pretended the
            # file was UTF-8.  Python children are UTF-8-clean via the env
            # vars above; other children's bytes stay as produced, and
            # file_read decodes them (explicit encoding parameter, with an
            # actionable hint when strict UTF-8 fails).
            log_handle = log_path.open("ab")
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            process = subprocess.Popen(
                _process_spawn_command(command),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=process_env,
                creationflags=creation_flags,
                start_new_session=os.name != "nt",
            )
            log_handle.close()
            # Execution cwd may be a nested backend directory; lifecycle
            # ownership belongs to the run workspace, not that directory.
            registry_path = _process_registry_path(_workspace())
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
                    # doc 18 修复G：会话归属标签（节点级清理依据）
                    "node_id": _CURRENT_NODE_CONTEXT.get(),
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
        completed = _run_foreground_command(
            command,
            cwd=cwd,
            timeout=process_timeout,
            env=process_env,
        )
        output_refs: dict[str, Any] = {}
        for channel in ("stdout", "stderr"):
            captured_output = completed[channel]
            if len(captured_output) > 12000:
                log_dir = cwd / ".astralnexus_process_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                output_path = log_dir / f"{tool_name}_{time.time_ns()}.{channel}.log"
                output_path.write_bytes(captured_output.encode("utf-8"))
                output_refs[f"{channel}_log_path"] = str(output_path)
                output_refs[f"{channel}_truncated"] = True
                output_refs[f"{channel}_chars"] = len(captured_output)
        if output_refs:
            output_refs["output_evidence_hint"] = (
                "Full decoded command output is already saved at the log paths. "
                "Reference these files as execution evidence; do not transcribe the "
                "log through file_write. Inspect/filter the saved log with python_exec "
                "or file_read when a specific assertion is needed."
            )
        return tool_result({
            "status": (
                "timeout" if completed["timed_out"]
                else "success" if completed["returncode"] == 0
                else "failed"
            ),
            "tool": tool_name,
            "returncode": completed["returncode"],
            "stdout": completed["stdout"][-12000:],
            "stderr": completed["stderr"][-12000:],
            **output_refs,
            "output_encoding": completed["output_encoding"],
            "cwd": str(cwd),
            "timeout_seconds": process_timeout,
            "timed_out": completed["timed_out"],
            "command_hint": (
                "argv is executed without a shell: standalone |, >, && tokens are "
                "literal program arguments. For a pipeline, explicitly call a shell "
                "with one command-string argument, or capture and filter output in python_exec."
                if completed["returncode"] != 0 and any(
                    part in {"|", "||", "&&", ">", ">>", "<"} for part in command
                ) else ""
            ),
        })
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
                completed = _run_foreground_command(
                    ["cmd.exe", "/d", "/s", "/c", shell_command],
                    cwd=cwd,
                    timeout=process_timeout,
                    env=process_env,
                )
                return tool_result({
                    "status": (
                        "timeout" if completed["timed_out"]
                        else "success" if completed["returncode"] == 0
                        else "failed"
                    ),
                    "tool": tool_name,
                    "returncode": completed["returncode"],
                    "stdout": completed["stdout"][-200_000:],
                    "stderr": completed["stderr"][-100_000:],
                    "output_encoding": completed["output_encoding"],
                    "cwd": str(cwd),
                    "shell_fallback": True,
                    "timeout_seconds": process_timeout,
                    "timed_out": completed["timed_out"],
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
    from AstralNexus.tools.context import read_tool_path
    root = read_tool_path(str(args.get("path", ".")))
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
        if not root.exists():
            return tool_error(f"search path does not exist: {root}", status="error")
        # os.walk(file) silently produces no entries, which falsely told a
        # real development run that an existing Flask module had no routes.
        entries = ([(str(root.parent), [], [root.name])] if root.is_file()
                   else os.walk(root, topdown=True, followlinks=False))
        for current, dirs, files in entries:
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


_FILE_WRITE_LOCK = threading.Lock()


def _file_write(args: dict[str, Any]) -> str:
    if not is_tool_approved("file_write"):
        return tool_error("approval_required: set ASTRALNEXUS_ALLOW_FILE_WRITE=true for file writes", status="approval_required")
    try:
        path = _inside_workspace(str(args.get("path", "")))
        _assert_safe_workspace_path(path, operation="write")
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
        mode = args.get("mode", "overwrite")
        if mode not in {"overwrite", "append", "replace"}:
            return tool_error("mode must be overwrite, append or replace")
        with _FILE_WRITE_LOCK:
            data = content.encode("utf-8")
            if mode == "replace":
                old_text = args.get("old_text")
                expected_hash = args.get("expected_sha256")
                if not isinstance(old_text, str) or not old_text or not isinstance(expected_hash, str):
                    return tool_error("replace requires nonempty old_text and expected_sha256 from file_read; no content written")
                original = path.read_bytes()
                if hashlib.sha256(original).hexdigest() != expected_hash:
                    return tool_error("replace version conflict: re-read current file; no content written")
                text = original.decode("utf-8")
                if text.count(old_text) != 1:
                    return tool_error("replace requires exactly one old_text match; include more context; no content written")
                data = text.replace(old_text, content, 1).encode("utf-8")
            if mode == "append":
                expected = args.get("expected_bytes")
                if type(expected) is not int or expected < 0:
                    return tool_error("append requires expected_bytes from the preceding successful file_write result")
                if not path.is_file() or path.stat().st_size != expected:
                    return tool_error("append size conflict: read the current file before retrying; no content appended")
                data = path.read_bytes() + data
            _stop_services_referencing(path)
            temp = path.with_name(path.name + ".astralnexus.tmp")
            temp.write_bytes(data)
            temp.replace(path)
        return tool_result({
            "status": "success",
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(data).hexdigest(),
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
        # Some model responses retain JSON-style escaping around the Python
        # snippet. cmd.exe does not treat backslash as a quote escape. Only
        # normalize a complete, syntactically valid Python invocation; never
        # rewrite general shell commands or their internal escaping.
        python_snippet = re.fullmatch(r'(?s)(python(?:\d+(?:\.\d+)*)?(?:\.exe)?)\s+-c\s+\\"(.*)\\"', text)
        if os.name == "nt" and python_snippet:
            executable, code = python_snippet.groups()
            try:
                ast.parse(code)
            except SyntaxError:
                pass
            else:
                return [executable, "-c", code]
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
    except PermissionError as exc:
# 【R25 复核】R15 沙箱边界结论是工具沙箱内现象，非机器边界（台账 §34）：Playwright driver 启动
        # 需要匿名管道（WinError 5 拒绝），chromium 多进程形态的 crashpad 子
        # 进程与 mojo 命名管道同样被拒；single-process 形态可起 CDP HTTP 端
        # 点，但打开/导航页面 target 必触发 mojo platform_channel FATAL。
# 沙箱内此时确无可用提供方；沙箱外匿名管道与真实浏览器渲染已证可用。
        # unavailable，而不是把环境拒绝伪装成页面抓取失败。
        return tool_error(
            "browser provider cannot start in this context: process creation was refused (%s); "
            "HTTP fetching is not equivalent to a browser test" % (exc.strerror or exc),
            status="unavailable")
    except Exception as exc:
        return tool_error(str(exc), status="error")


registry.register(name="http_get", toolset="network", schema={"name": "http_get", "description": "Fetch a bounded HTTP(S) response for evidence collection.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "max_bytes": {"type": "integer"}}, "required": ["url"]}}, handler=_http_get, max_result_size_chars=300_000)
registry.register(name="openalex_search", toolset="research", schema={"name": "openalex_search", "description": "Search scholarly works in OpenAlex and return identifiers and metadata.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "year_from": {"type": "integer"}, "year_to": {"type": "integer"}}, "required": ["query"]}}, handler=_openalex_search)
registry.register(name="crossref_search", toolset="research", schema={"name": "crossref_search", "description": "Search Crossref works and return DOI-backed metadata.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}, handler=_crossref_search)
registry.register(name="arxiv_search", toolset="research", schema={"name": "arxiv_search", "description": "Search arXiv and return paper identifiers and abstracts.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}, handler=_arxiv_search)
registry.register(name="github_repo_contents", toolset="software", schema={"name": "github_repo_contents", "description": "Read a public GitHub repository file or directory with source references.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "path": {"type": "string"}}, "required": ["repo"]}}, handler=_github_repo)
registry.register(name="file_read", toolset="file", schema={"name": "file_read", "description": "Read a bounded window of a text file inside the AstralNexus workspace. Defaults to the head of the file. For a large file do not re-read it through python_exec slicing: address the region directly with start_line/end_line (search_files returns line numbers), or page forward with the offset returned as next_offset. The result reports offset/next_offset/start_line/end_line/total_lines/eof so paging terminates.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}, "offset": {"type": "integer", "minimum": 0, "description": "0-based character offset to start the window at; pass the previous result's next_offset to continue. Mutually exclusive with start_line/end_line."}, "start_line": {"type": "integer", "minimum": 1, "description": "1-based first line of the window. Mutually exclusive with offset."}, "end_line": {"type": "integer", "minimum": 1, "description": "1-based last line of the window, inclusive; defaults to end of file."}, "encoding": {"type": "string", "description": "Text source encoding; default utf-8. Use the actual source encoding for legacy logs."}}, "required": ["path"]}}, handler=_file_read)
registry.register(name="python_exec", toolset="execution", schema={"name": "python_exec", "description": "Run a bounded Python experiment in the AstralNexus workspace.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["code"]}}, handler=_python_exec)
registry.register(name="pytest_run", toolset="execution", schema={"name": "pytest_run", "description": "Run pytest against a workspace path and return stdout/stderr evidence.", "parameters": {"type": "object", "properties": {"target": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["target"]}}, handler=_pytest_run)
registry.register(name="search_files", toolset="file", schema={"name": "search_files", "description": "Search text files inside the local AstralNexus workspace with bounded results. This tool does not search the public web.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "max_results": {"type": "integer"}, "max_files": {"type": "integer"}, "max_file_bytes": {"type": "integer"}, "timeout": {"type": "number"}}, "required": ["query"]}}, handler=_search_files)
registry.register(name="web_search", toolset="network", schema={"name": "web_search", "description": "Search the public web and return source URLs for evidence.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}, handler=_web_search)
registry.register(name="file_write", toolset="file", schema={"name": "file_write", "description": "Write a UTF-8 file. For a small edit use mode=replace, old_text matching exactly once, content as replacement, and expected_sha256 from file_read; do not resend the whole file. For new large files overwrite a small first chunk then append sequentially with expected_bytes from the prior write. Never append chunks in parallel.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string", "enum": ["overwrite", "append", "replace"]}, "old_text": {"type": "string"}, "expected_sha256": {"type": "string"}, "expected_bytes": {"type": "integer", "minimum": 0}}, "required": ["path", "content"]}}, handler=_file_write, requires_approval=True)
registry.register(name="terminal_exec", toolset="execution", schema={"name": "terminal_exec", "description": "Run an argv command in the workspace after explicit approval. Set background=true for a server or other long-lived process; the tool returns its PID and log path immediately.", "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}, "background": {"type": "boolean", "description": "Detach a long-lived process and return PID/log evidence without waiting for exit."}}, "required": ["command"]}}, handler=_terminal_exec, requires_approval=True)
registry.register(name="browser_fetch", toolset="browser", schema={"name": "browser_fetch", "description": "Fetch rendered page text with Playwright when installed.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}, handler=_browser_fetch, check_fn=_playwright_available)
