"""Find, audit, and install skills for AstralNexus.

The search surface is public and read-only. Installation is deliberately
explicit and auditable: a Governor-approved request may invoke the official
``npx skills add`` CLI, then the resulting skill is validated before it can be
considered enabled.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from AstralNexus.core.skill_runtime import discover_skills, skill_manifest, view_skill_file

from AstralNexus.tools.registry import registry, tool_error, tool_result


_SEARCH_URLS = (
    "https://skills.sh/api/v1/skills/search",
    "https://findskills.org/api/v1/search",
)
_DANGEROUS_PATTERNS = (
    r"curl\s+[^\n|]*\|\s*(?:sh|bash)",
    r"wget\s+[^\n|]*\|\s*(?:sh|bash)",
    r"(?:Invoke-WebRequest|iwr)[^\n|]*\|\s*(?:iex|Invoke-Expression)",
    r"\brm\s+-rf\b",
    r"\b(?:os\.system|subprocess\.(?:run|Popen|call))\b",
    r"(?:api[_-]?key|secret|token)[^\n]{0,30}(?:upload|post|send|exfil)",
)


def _home(args: dict[str, Any]) -> Path:
    raw = str(args.get("home") or os.environ.get("ASTRALNEXUS_HOME") or (Path.home() / ".astralnexus"))
    return Path(raw).expanduser().resolve()


def _skills_dir(args: dict[str, Any]) -> Path:
    root = _home(args) / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest_path(args: dict[str, Any]) -> Path:
    return _skills_dir(args) / "installed_skills.json"


def _load_manifest(args: dict[str, Any]) -> list[dict[str, Any]]:
    path = _manifest_path(args)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_manifest(args: dict[str, Any], records: list[dict[str, Any]]) -> None:
    path = _manifest_path(args)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_get(url: str, timeout: int = 20) -> Any:
    headers = {"User-Agent": "AstralNexus/1.0", "Accept": "application/json"}
    token = os.environ.get("SKILLS_SH_TOKEN") or os.environ.get("VERCEL_OIDC_TOKEN")
    if token and "skills.sh" in url:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(2_000_000).decode("utf-8", errors="replace")
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if "json" not in content_type and not raw.lstrip().startswith(("{", "[")):
            raise ValueError(f"skill index returned non-JSON content ({content_type or 'unknown'}): {raw[:160]}")
        return json.loads(raw)


def _npx_command() -> str | None:
    configured = os.environ.get("ASTRALNEXUS_NPX", "").strip()
    if configured:
        return configured
    # Prefer the system Node installation over a second bundled Node runtime.
    candidates = [r"C:\Program Files\nodejs\npx.cmd", shutil.which("npx.cmd"), shutil.which("npx")]
    return next((item for item in candidates if item and Path(item).exists()), None)


def _skills_cli_package() -> str:
    """Select a skills CLI release compatible with the installed Node runtime."""
    configured = os.environ.get("ASTRALNEXUS_SKILLS_CLI_VERSION", "").strip()
    if configured:
        return "skills" if configured in {"latest", "current"} else f"skills@{configured.lstrip('@')}"
    node = shutil.which("node.exe") or shutil.which("node")
    if node:
        try:
            version = subprocess.run([node, "--version"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=10, check=False)
            major = int(re.search(r"v?(\d+)", version.stdout or "").group(1))
            if major < 22:
                return "skills@1.4.0"
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    return "skills"


def _staging_root() -> Path:
    configured = os.environ.get("ASTRALNEXUS_SKILL_STAGING", "").strip()
    candidates = ([Path(configured)] if configured else []) + [
        Path(tempfile.gettempdir()) / "astralnexus-skill-staging",
        Path.cwd() / ".astralnexus-skill-staging",
    ]
    errors = []
    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write-probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink(missing_ok=True)
            return root
        except OSError as exc:
            errors.append(f"{root}: {exc}")
    raise PermissionError("No writable AstralNexus skill staging directory: " + "; ".join(errors))


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "skills", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize(item: Any, query: str) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {"name": str(item)}
    package = str(_first(item, "package", "install", "id", "slug", "name") or "").strip()
    source = str(_first(item, "source", "repo", "repository", "github", "url") or "").strip()
    if not package and source:
        package = source.rstrip("/").split("/")[-1]
    if package.startswith("https://github.com/"):
        package = package.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    source_url = str(_first(item, "url", "html_url", "source_url") or "").strip()
    if not source_url and item.get("id"):
        source_url = "https://skills.sh/" + str(item["id"]).strip("/")
    return {
        "name": str(_first(item, "name", "title", "slug") or package or "unknown"),
        "package": package,
        "description": str(_first(item, "description", "summary") or ""),
        "source": source,
        "source_url": source_url or source,
        "stars": _first(item, "stars", "github_stars"),
        "installs": _first(item, "installs", "install_count", "downloads"),
        "score": _first(item, "score", "relevance"),
        "ecosystem": "skills.sh / FindSkills.org",
        "query": query,
        "install_command": f"npx skills add {package} -g -y" if package else "",
        "audit_status": "not_audited",
        "raw": item,
    }


def _find_skills(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_error("query is required")
    limit = max(1, min(int(args.get("limit", 10)), 50))
    errors: list[str] = []
    for base_url in _SEARCH_URLS:
        try:
            url = base_url + "?" + urllib.parse.urlencode({"q": query, "limit": limit})
            data = _json_get(url, timeout=min(int(args.get("timeout", 20)), 60))
            candidates = [_normalize(item, query) for item in _items(data)[:limit]]
            if not candidates:
                errors.append(f"{base_url}: empty result")
                continue
            source = "skills.sh" if "skills.sh" in base_url else "FindSkills.org"
            return tool_result({
                "status": "success", "source": source, "query": query,
                "count": len(candidates), "candidates": candidates,
                "install_policy": "Search is public; installation requires explicit Governor approval and audit.",
                "evidence_refs": [x["source_url"] for x in candidates if x.get("source_url")],
            })
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    try:
        # FindSkills.org is an index, not a guaranteed transport. Use the
        # official CLI as a real fallback, never as a fabricated result.
        npx = _npx_command()
        if npx:
            cli_env = os.environ.copy()
            staging = _staging_root()
            cli_cache = staging / "npm-cache"
            cli_cache.mkdir(parents=True, exist_ok=True)
            cli_env["npm_config_cache"] = str(cli_cache)
            cli_env["NPM_CONFIG_CACHE"] = str(cli_cache)
            cli_env["npm_config_prefix"] = str(staging / "npm-prefix")
            cli_env["NPM_CONFIG_PREFIX"] = str(staging / "npm-prefix")
            cli_package = _skills_cli_package()
            for cli_name, cli_args, source in (
                ("findskills", ["findskills"], "FindSkills CLI"),
                ("skills", [cli_package, "find"], "skills.sh CLI"),
            ):
                cli = subprocess.run([npx, "-y", *cli_args, query],
                                     env=cli_env, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=90, check=False)
                if cli.returncode == 0 and cli.stdout.strip():
                    packages = re.findall(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.-]+)?\b", cli.stdout)
                    candidates = [{"name": package.split("/")[-1], "package": package,
                                   "description": "Returned by an official Skill ecosystem CLI; inspect source before install.",
                                   "source": source, "source_url": "https://skills.sh/",
                                   "install_command": f"npx skills add {package} -g -y",
                                   "audit_status": "not_audited", "query": query}
                                  for package in list(dict.fromkeys(packages))[:limit]]
                    if candidates:
                        return tool_result({"status": "success", "source": source, "query": query,
                                            "count": len(candidates), "candidates": candidates,
                                            "raw_output": cli.stdout[-50_000:], "transport_warning": errors})
                errors.append(f"{cli_name}: {cli.stderr[-500:]}")
    except Exception as exc:
        errors.append(f"skills CLI: {exc}")
    return tool_error("all skill search transports failed", status="error", source="skills.sh/FindSkills.org",
                          query=query, transport_errors=errors,
                          install_hint="npx skills find " + query,
                          cli_error="official skills CLI was unavailable or failed")


def _skill_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def _audit_skill(root: Path) -> dict[str, Any]:
    skill_files = [p for p in _skill_files(root) if p.name.lower() == "skill.md"]
    if not skill_files:
        return {"audit_status": "rejected", "reasons": ["SKILL.md is missing"], "files": 0}
    files = _skill_files(root)
    if len(files) > 500 or sum(p.stat().st_size for p in files) > 10_000_000:
        return {"audit_status": "review_required", "reasons": ["skill exceeds file or size limit"], "files": len(files)}
    text_parts: list[str] = []
    for path in files:
        if path.suffix.lower() in {".md", ".txt", ".py", ".js", ".ts", ".sh", ".ps1", ".json", ".yaml", ".yml"}:
            try:
                text_parts.append(path.read_text(encoding="utf-8", errors="ignore")[:200_000])
            except OSError:
                pass
    joined = "\n".join(text_parts)
    hits = [pattern for pattern in _DANGEROUS_PATTERNS if re.search(pattern, joined, re.IGNORECASE)]
    front = skill_files[0].read_text(encoding="utf-8", errors="ignore")[:20_000]
    required = ["name", "description"]
    missing = [field for field in required if not re.search(rf"(?im)^\s*{field}\s*:", front)]
    reasons = (["dangerous execution or exfiltration pattern detected"] if hits else []) + (["SKILL.md frontmatter is incomplete"] if missing else [])
    return {"audit_status": "review_required" if reasons else "passed", "reasons": reasons,
            "dangerous_patterns": hits, "files": len(files), "skill_file": str(skill_files[0])}


def _skill_audit(args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return tool_error("name is required")
    root = _skills_dir(args) / Path(name).name
    if not root.exists():
        # Installed packages may be grouped by category. Resolve by the
        # canonical SKILL.md index before rejecting the request.
        for skill_file in _skills_dir(args).rglob("SKILL.md"):
            if skill_file.parent.name.lower() == Path(name).name.lower():
                root = skill_file.parent
                break
    if not root.exists() or not root.is_dir():
        return tool_error("skill is not installed", name=name)
    return tool_result({"status": "success", "name": name, "path": str(root), "audit": _audit_skill(root)})


def _skill_list(args: dict[str, Any]) -> str:
    records = _load_manifest(args)
    installed = []
    for path in sorted(_skills_dir(args).iterdir()):
        if path.is_dir() and not path.name.startswith("."):
            installed.append({"name": path.name, "audit": _audit_skill(path)})
    return tool_result({"status": "success", "skills": installed, "manifest": records})


def _skills_list(args: dict[str, Any]) -> str:
    """Return the compact runtime catalog, equivalent to AstralNexus skills_list."""
    home = Path(args["home"]).expanduser().resolve() if args.get("home") else None
    available = args.get("available_tools")
    toolsets = args.get("toolsets")
    available_set = set(map(str, available)) if isinstance(available, list) else None
    toolset_set = set(map(str, toolsets)) if isinstance(toolsets, list) else None
    entries = skill_manifest(home, limit=max(1, min(int(args.get("limit", 200)), 500)),
                             query=str(args.get("query") or "").strip() or None,
                             enabled_only=bool(args.get("enabled_only", True)),
                             available_tools=available_set, toolsets=toolset_set)
    return tool_result({"status": "success", "count": len(entries), "skills": entries,
                        "disclosure": "metadata_only", "next": "Use skill_view to read one file."})


def _skill_view(args: dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return tool_error("name is required")
    home = Path(args["home"]).expanduser().resolve() if args.get("home") else None
    try:
        result = view_skill_file(name, str(args.get("relative_path") or "SKILL.md"), home,
                                 max_bytes=min(int(args.get("max_bytes", 500_000)), 500_000))
        if result is None:
            return tool_error("skill is not available", name=name)
        return tool_result({"status": "success", **result,
                            "disclosure": "single_file"})
    except (OSError, ValueError) as exc:
        return tool_error(str(exc), status="error", name=name)


def _set_skill_enabled(args: dict[str, Any], enabled: bool) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return tool_error("name is required")
    if not bool(args.get("approved_by_governor", False)):
        return tool_result({"status": "approval_required", "name": name,
                            "action": "enable" if enabled else "disable"})
    records = _load_manifest(args)
    matches = [item for item in records if str(item.get("skill_name", "")).lower() == name.lower()]
    if not matches:
        return tool_error("only installed skills can be enabled or disabled", name=name)
    for item in matches:
        item["enabled"] = enabled
        item["status"] = "installed" if enabled else "disabled"
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_manifest(args, records)
    return tool_result({"status": "success", "name": name, "enabled": enabled})


def _skill_install(args: dict[str, Any]) -> str:
    package = str(args.get("package", "")).strip()
    if not package or any(char in package for char in ";&|<>`\n\r"):
        return tool_error("package must be a safe package identifier")
    approved = bool(args.get("approved_by_governor", False))
    if not approved:
        return tool_result({"status": "approval_required", "package": package,
                            "install_command": f"npx skills add {package} -g -y",
                            "message": "Governor approval is required before installation."})
    npx = _npx_command() or "npx"
    home = _home(args)
    staging = _staging_root()
    # The official CLI installs to <cwd>/.agents/skills by default.  Keep that
    # side effect in a per-request staging directory, then copy only an audited
    # SKILL.md package into AstralNexus' own catalog.
    request_dir = staging / re.sub(r"[^A-Za-z0-9._-]", "_", package)[:80]
    if request_dir.exists():
        shutil.rmtree(request_dir, ignore_errors=True)
    request_dir.mkdir(parents=True, exist_ok=True)
    npm_cache = staging / "npm-cache"
    npm_cache.mkdir(parents=True, exist_ok=True)
    npm_prefix = staging / "npm-prefix"
    npm_prefix.mkdir(parents=True, exist_ok=True)
    npm_env = os.environ.copy()
    npm_env["npm_config_cache"] = str(npm_cache)
    npm_env["NPM_CONFIG_CACHE"] = str(npm_cache)
    npm_env["npm_config_prefix"] = str(npm_prefix)
    npm_env["NPM_CONFIG_PREFIX"] = str(npm_prefix)
    try:
        cli_package = _skills_cli_package()
        result = subprocess.run([npx, "-y", cli_package, "add", package, "--copy", "-y"],
                                cwd=request_dir, env=npm_env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300, check=False)
    except Exception as exc:
        result = subprocess.CompletedProcess([], 1, "", str(exc))
    staged_root = request_dir / ".agents" / "skills"
    candidates = [p for p in staged_root.rglob("*") if p.is_dir() and (p / "SKILL.md").exists()] if staged_root.exists() else []
    install_backend = "npx"
    # npm is an optional transport. If Windows/npm is locked, clone the
    # declared GitHub source as data, then run exactly the same audit before
    # copying it into the active catalog. Git never executes skill scripts.
    if not candidates and result.returncode != 0:
        source, _, requested_skill = package.partition("@")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
            source = "https://github.com/" + source + ".git"
        if source.startswith("https://github.com/"):
            git = shutil.which("git")
            clone_dir = request_dir / "source"
            if git:
                clone = subprocess.run([git, "clone", "--depth", "1", source, str(clone_dir)],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=300, check=False)
                if clone.returncode == 0 and clone_dir.exists():
                    staged_root = clone_dir
                    candidates = [p.parent for p in staged_root.rglob("SKILL.md")]
                    if requested_skill:
                        selected = [p for p in candidates if p.name.lower() == requested_skill.lower()]
                        candidates = selected or candidates
                    result = clone
                    install_backend = "git"
    if not candidates:
        audit = {"audit_status": "rejected", "reasons": ["official CLI produced no SKILL.md package"], "files": 0}
        status = "rejected"
    else:
        candidate = max(candidates, key=lambda p: p.stat().st_mtime)
        audit = _audit_skill(candidate)
        status = "installed" if result.returncode == 0 and audit["audit_status"] == "passed" else "review_required"
        if status == "installed":
            target = _skills_dir(args) / candidate.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(candidate, target)
            audit = _audit_skill(target)
            if audit["audit_status"] != "passed":
                status = "review_required"
    record = {"package": package, "skill_name": (candidate.name if candidates else ""),
              "command": f"npx {cli_package} add {package} --copy -y", "backend": install_backend, "status": status,
              "audit": audit, "installed_at": datetime.now(timezone.utc).isoformat(),
              "stdout": (result.stdout or "")[-20_000:], "stderr": (result.stderr or "")[-20_000:]}
    records = [x for x in _load_manifest(args) if x.get("package") != package]
    records.append(record)
    _save_manifest(args, records)
    return tool_result({"status": status, "package": package, "audit": audit, "record": record,
                        "staging_dir": str(request_dir)})


def _skill_update(args: dict[str, Any]) -> str:
    """Check or update through the official CLI without bypassing auditing."""
    approved = bool(args.get("approved_by_governor", False))
    if not approved:
        return tool_result({"status": "approval_required", "command": "npx skills check / npx skills update"})
    npx = shutil.which("npx.cmd") or shutil.which("npx") or "npx"
    try:
        result = subprocess.run([npx, "-y", _skills_cli_package(), "check"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180, check=False)
        return tool_result({"status": "success" if result.returncode == 0 else "failed",
                            "returncode": result.returncode, "stdout": (result.stdout or "")[-30_000:],
                            "stderr": (result.stderr or "")[-20_000:], "action": "check"})
    except Exception as exc:
        return tool_error(str(exc), status="error", action="check")


registry.register(name="find_skills", toolset="skills", schema={"name": "find_skills", "description": "Search the open Skill ecosystem for reusable agent skills and return candidates with sources and install commands.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "timeout": {"type": "integer"}}, "required": ["query"]}}, handler=_find_skills)
registry.register(name="skill_install", toolset="skills", schema={"name": "skill_install", "description": "Install a Governor-approved skill through the official skills CLI, then audit SKILL.md and source files.", "parameters": {"type": "object", "properties": {"package": {"type": "string"}, "home": {"type": "string"}, "approved_by_governor": {"type": "boolean"}}, "required": ["package"]}}, handler=_skill_install)
registry.register(name="skill_list", toolset="skills", schema={"name": "skill_list", "description": "List installed AstralNexus skills and their audit status.", "parameters": {"type": "object", "properties": {"home": {"type": "string"}}}}, handler=_skill_list)
registry.register(name="skills_list", toolset="skills", schema={"name": "skills_list", "description": "List the compact runtime Skill catalog without loading full instructions.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "home": {"type": "string"}, "enabled_only": {"type": "boolean"}, "available_tools": {"type": "array", "items": {"type": "string"}}, "toolsets": {"type": "array", "items": {"type": "string"}}}}}, handler=_skills_list)
registry.register(name="skill_view", toolset="skills", schema={"name": "skill_view", "description": "Read one SKILL.md or safe supporting file after selecting a Skill from the catalog.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "relative_path": {"type": "string"}, "home": {"type": "string"}, "max_bytes": {"type": "integer"}}, "required": ["name"]}}, handler=_skill_view)
registry.register(name="skill_audit", toolset="skills", schema={"name": "skill_audit", "description": "Audit an installed skill before enabling it in the execution graph.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "home": {"type": "string"}}, "required": ["name"]}}, handler=_skill_audit)
registry.register(name="skill_update", toolset="skills", schema={"name": "skill_update", "description": "Check the open Skill installation for available updates through the official CLI.", "parameters": {"type": "object", "properties": {"approved_by_governor": {"type": "boolean"}}}}, handler=_skill_update)
registry.register(name="skill_enable", toolset="skills", schema={"name": "skill_enable", "description": "Enable an audited installed Skill in the runtime catalog after Governor approval.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "home": {"type": "string"}, "approved_by_governor": {"type": "boolean"}}, "required": ["name"]}}, handler=lambda args: _set_skill_enabled(args, True))
registry.register(name="skill_disable", toolset="skills", schema={"name": "skill_disable", "description": "Disable an installed Skill without deleting it.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "home": {"type": "string"}, "approved_by_governor": {"type": "boolean"}}, "required": ["name"]}}, handler=lambda args: _set_skill_enabled(args, False))
