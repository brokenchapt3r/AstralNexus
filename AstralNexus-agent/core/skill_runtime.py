"""Runtime Skill catalog and progressive-disclosure loader.

This is AstralNexus' native equivalent of AstralNexus' skill discovery layer:
frontmatter is indexed at startup, only compact metadata enters every prompt,
and full SKILL.md content is loaded only when a worker explicitly activates it.
"""
from __future__ import annotations

import os
import json
import re
import sys
import hashlib
from pathlib import Path
from typing import Any
import yaml


EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".staging", "references", "templates", "assets", "scripts"}


def skills_root(home: Path | None = None) -> Path:
    configured = os.environ.get("ASTRALNEXUS_SKILLS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if home is not None:
        return (Path(home).expanduser() / "skills").resolve()
    return (Path(os.environ.get("ASTRALNEXUS_HOME", Path.home() / ".astralnexus")) / "skills").resolve()


def _frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    match = re.search(r"\n---\s*\n", content[3:])
    if not match:
        return {}, content
    raw = content[3:match.start() + 3]
    body = content[match.end() + 3:]
    try:
        parsed = yaml.safe_load(raw)
        result: dict[str, Any] = json.loads(json.dumps(parsed, default=str)) if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        result = {}
    return result, body


def _platform_ok(frontmatter: dict[str, Any]) -> bool:
    platforms = frontmatter.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    if not platforms:
        return True
    aliases = {"windows": "win32", "macos": "darwin"}
    current = aliases.get(sys.platform, sys.platform)
    return any(aliases.get(str(item).lower(), str(item).lower()) in current for item in platforms)


def _manifest_records(home: Path | None) -> list[dict[str, Any]]:
    path = skills_root(home) / "installed_skills.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _skill_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for directory, children, names in os.walk(root):
            children[:] = [name for name in children if name not in EXCLUDED]
            if 'SKILL.md' in names:
                files.append(Path(directory) / 'SKILL.md')
                if '.astralnexus_origin.json' in names:
                    # Nested SKILL.md files remain readable supporting assets.
                    # The importer separately indexes their namespaced copies.
                    children[:] = []
    return sorted(files, key=lambda path: str(path).lower())


def _snapshot_path(home: Path | None) -> Path:
    return skills_root(home) / ".astralnexus_skill_manifest.json"


def _snapshot_fingerprint(files: list[Path]) -> list[dict[str, Any]]:
    result = []
    for path in files:
        try:
            stat = path.stat()
            result.append({"path": str(path), "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
        except OSError:
            continue
    return result


def _read_snapshot(path: Path, fingerprint: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == 2 and data.get("fingerprint") == fingerprint and isinstance(data.get("entries"), list):
            return data["entries"]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def _write_snapshot(path: Path, fingerprint: list[dict[str, Any]], entries: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 2, "fingerprint": fingerprint, "entries": entries},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # A read-only home must not prevent the runtime from discovering skills.
        pass


def _conditions_match(item: dict[str, Any], available_tools: set[str] | None = None,
                      toolsets: set[str] | None = None) -> bool:
    front = item.get("frontmatter", {})
    required_tools = front.get("requires_tools") or front.get("requires-tools") or []
    required_sets = front.get("requires_toolsets") or front.get("requires-toolsets") or []
    if isinstance(required_tools, str):
        required_tools = [required_tools]
    if isinstance(required_sets, str):
        required_sets = [required_sets]
    if available_tools is not None and required_tools and not set(map(str, required_tools)).issubset(available_tools):
        return False
    if toolsets is not None and required_sets and not set(map(str, required_sets)).issubset(toolsets):
        return False
    return True


def discover_skills(home: Path | None = None, *, query: str | None = None,
                    enabled_only: bool = True, available_tools: set[str] | None = None,
                    toolsets: set[str] | None = None) -> list[dict[str, Any]]:
    roots = [skills_root(home), Path(__file__).resolve().parents[1] / "skills"]
    roots = list(dict.fromkeys(root for root in roots if root.exists()))
    records = _manifest_records(home)
    blocked = {str(item.get("skill_name", "")).lower() for item in records
               if isinstance(item, dict) and (item.get("status") != "installed" or
                                              (enabled_only and item.get("enabled") is False))}
    entries: list[dict[str, Any]] = []
    files = _skill_files(roots)
    fingerprint = _snapshot_fingerprint(files + [p.parent / '.astralnexus_origin.json' for p in files
                                                 if (p.parent / '.astralnexus_origin.json').is_file()])
    cached = _read_snapshot(_snapshot_path(home), fingerprint)
    if cached is not None:
        entries = cached
    else:
        for skill_file in files:
            try:
                root = next((candidate for candidate in roots if candidate in skill_file.parents),
                            skill_file.parent.parent)
                content = skill_file.read_text(encoding="utf-8", errors="replace")
                front, body = _frontmatter(content)
                name = str(front.get("name") or skill_file.parent.name)
                origin = {}
                origin_path = skill_file.parent / '.astralnexus_origin.json'
                if origin_path.is_file():
                    data = json.loads(origin_path.read_text(encoding='utf-8'))
                    origin = {key: data[key] for key in ('name', 'namespace', 'repo', 'commit', 'source_path', 'status') if key in data}
                    name = str(origin['name'])
                description = str(front.get("description") or " ".join(body.strip().split())[:240])
                entries.append({
                    "name": name,
                    "description": description,
                    "path": str(skill_file),
                    "skill_dir": str(skill_file.parent),
                    "category": skill_file.parent.parent.name if skill_file.parent.parent != root else "general",
                    "platforms": front.get("platforms", []),
                    "metadata": front.get("metadata", {}),
                    "frontmatter": front,
                    "origin": origin,
                })
            except (OSError, ValueError, KeyError):
                continue
        _write_snapshot(_snapshot_path(home), fingerprint, entries)
    unique: dict[str, dict[str, Any]] = {}
    for item in entries:
        if item["name"].lower() in blocked or not _platform_ok(item.get("frontmatter", item)):
            continue
        if not _conditions_match(item, available_tools, toolsets):
            continue
        if query:
            terms = re.findall(r'[\w-]+', query.casefold())
            haystack = (item['name'] + ' ' + item['description']).casefold()
            if not terms or not any(term in haystack for term in terms):
                continue
        unique.setdefault(item["name"].lower(), item)
    terms = re.findall(r'[\w-]+', (query or '').casefold())
    return sorted(unique.values(), key=lambda item: (
        -sum(3 * (term in item['name'].casefold()) + (term in item['description'].casefold()) for term in terms),
        item['category'], item['name']))


def skill_manifest(home: Path | None = None, limit: int = 200, **filters: Any) -> list[dict[str, Any]]:
    return [{k: item[k] for k in ("name", "description", "category", "path", "platforms")}
            for item in discover_skills(home, **filters)[:limit]]


def load_skill(name: str, home: Path | None = None) -> dict[str, Any] | None:
    needle = name.strip().lower()
    catalog = discover_skills(home)
    exact = [item for item in catalog if needle == item['name'].lower()]
    candidates = exact or [item for item in catalog if needle in {Path(item['skill_dir']).name.lower(), item['path'].lower()}]
    if len(candidates) > 1:
        raise ValueError('Ambiguous skill name; use the namespaced catalog name')
    for item in candidates:
        if needle not in {item["name"].lower(), Path(item["skill_dir"]).name.lower(), item["path"].lower()}:
            continue
        try:
            raw = Path(item["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        front, body = _frontmatter(raw)
        bridge = ''
        if item.get('origin'):
            bridge = (
                'AstralNexus compatibility: this is an upstream skill, not a replacement orchestrator. '
                'Use skill_view(name, relative_path) for its supporting files. Read/Write/Edit map to '
                'file_read/file_write; Bash/PowerShell map to terminal_exec argv; Skill maps to skill_view. '
                'For delegation, todo, approval, worktree or provider-specific actions absent from the '
                'current tool catalog, return a Governor next_action instead of inventing a tool. '
                'Resolve scripts relative to skill_dir; inspect dependencies before execution. '
                'A skill being installed does not mean its cloud credentials or external runtimes are configured.\n\n'
            )
        return {**item, "frontmatter": front, "compatibility": bridge.strip(), "content": bridge + body.strip()}
    return None


def view_skill_file(name: str, relative_path: str = "SKILL.md", home: Path | None = None,
                    max_bytes: int = 500_000) -> dict[str, Any] | None:
    """Read one file from a discovered skill, with traversal containment checks."""
    item = load_skill(name, home)
    if not item:
        return None
    root = Path(item["skill_dir"]).resolve()
    relative = Path(relative_path or "SKILL.md")
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("skill file path must be relative to the skill directory")
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("skill file escapes the skill directory")
    if not target.is_file():
        raise FileNotFoundError(relative.as_posix())
    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"skill file exceeds {max_bytes} byte limit")
    return {"name": item["name"], "path": str(target), "relative_path": relative.as_posix(),
            "skill_dir": str(root), "origin": item.get("origin"),
            "compatibility": item.get("compatibility", ""),
            "size": size, "content": target.read_text(encoding="utf-8", errors="replace")}


def runtime_skill_context(metadata: dict[str, Any], goal: str, role: str, home: Path | None = None) -> str:
    """Return compact index plus explicitly activated full Skill content."""
    catalog = discover_skills(home, enabled_only=True)
    relevant = discover_skills(home, query=goal + ' ' + role, enabled_only=True)
    entries = (relevant or catalog)[:24]
    lines = [f"Skill catalog ({len(catalog)} available; {len(entries)} task-ranked suggestions; metadata only):",
             "Use skills_list(query, offset, limit) to search/page the complete catalog, then skill_view to load a skill."]
    for item in entries:
        lines.append(f"- {item['name']} [{item['category']}]: {item['description'][:180]}")
    requested = metadata.get("activated_skills") or metadata.get("skills") or []
    if isinstance(requested, str):
        requested = [requested]
    selected: list[dict[str, Any]] = []
    for name in requested:
        loaded = load_skill(str(name), home)
        if loaded:
            selected.append(loaded)
    if selected:
        lines.append("\nActivated Skills (use only for this node):")
        for item in selected:
            lines.append(f"\n### {item['name']}\n{item['content']}")
    elif "skill" in str(goal).lower():
        lines.append("\nThis node may use find_skills to discover a reusable Skill; do not install one without Governor approval.")
    return "\n".join(lines)
