"""Runtime-owned presentation facts; models cannot grant delivery success."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from typing import Any, Iterator
from AstralNexus.core.delivery_snapshot import snapshot, FRAMEWORK_FILES


class Presentation:
    def __init__(self, run_dir: Path, outputs: Path, workspace: Path | None = None):
        if outputs.is_symlink() or not outputs.resolve().is_relative_to(run_dir.resolve()):
            raise ValueError("outputs must be a physical directory inside the run")
        self.run_dir, self.outputs = run_dir.resolve(), outputs.resolve()
        self.workspace = (workspace or run_dir).resolve()
        self.paths: list[str] = []
        self._lock = RLock()

    def present(self, paths: list[str]) -> dict[str, Any]:
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
            raise ValueError("present_files requires a nonempty string list")
        normalized = []
        for raw in paths:
            text = raw.replace("\\", "/").strip()
            for prefix in ("/mnt/user-data/outputs/", "outputs/"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
            if text.rstrip("/") in {"outputs", "/mnt/user-data/outputs"}:
                text = "."
            candidate = Path(text)
            candidate = candidate if candidate.is_absolute() else self.outputs / candidate
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.outputs):
                raise ValueError("present_files path must remain inside outputs")
            if not candidate.is_file() and not candidate.is_dir():
                raise FileNotFoundError(raw)
            relative = candidate.resolve().relative_to(self.outputs).as_posix()
            normalized.append("outputs" if relative == "." else "outputs/" + relative)
        with self._lock:
            self.paths = list(dict.fromkeys([*self.paths, *normalized]))
        return {"status": "success", "paths": normalized, "presented": len(normalized)}


def output_inventory(root: Path) -> dict[str, tuple[str, int, int]]:
    if not root.exists():
        return {}
    return {"outputs/" + name: (sha, size, (root / name).stat().st_mtime_ns) for name, sha, size in snapshot(root)}


def delivery_receipt(before: dict[str, tuple[str, int, int]], after: dict[str, tuple[str, int, int]], presented: list[str]) -> dict[str, Any]:
    produced = sorted(path for path, version in after.items() if before.get(path) != version)
    presented = list(dict.fromkeys(path.rstrip("/") for path in presented))
    matched = [path for path in produced if any(path == item or path.startswith(item + "/") for item in presented)]
    return {"presented": len(presented), "paths": presented, "by_tool": {"present_files": presented},
            "produced_paths": produced, "presented_paths": presented, "matched_paths": matched,
            "stage": "not_required" if not produced else "presented" if matched else "mismatched" if presented else "not_started",
            "satisfied": bool(matched) if produced else True, "output_files": after,
            "verification": {"source": "outputs_changed", "requirement": "present_files_matches_produced_output"}}


_active: ContextVar[Presentation | None] = ContextVar("astral_presentation", default=None)


def current_presentation() -> Presentation | None:
    return _active.get()


@contextmanager
def presentation_scope(value: Presentation | None) -> Iterator[None]:
    token = _active.set(value)
    try:
        yield
    finally:
        _active.reset(token)


def mapped_user_path(raw: str) -> Path | None:
    """One translation for explicit workspace/outputs namespaces."""
    context = current_presentation()
    if context is None:
        return None
    text = str(raw).replace("\\", "/")
    for prefix, root in (("/mnt/user-data/outputs", context.outputs), ("outputs", context.outputs),
                         ("/mnt/user-data/workspace", context.workspace)):
        if text == prefix or text.startswith(prefix + "/"):
            target = (root / text[len(prefix):].lstrip("/")).resolve()
            if not target.is_relative_to(root):
                raise ValueError("virtual path escapes its namespace")
            return target
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() and candidate.resolve().is_relative_to(context.outputs):
        return candidate.resolve()
    return None


def mapped_input_path(raw: str) -> Path | None:
    """Read existing session inputs, preferring files in the isolated run."""
    mapped = mapped_user_path(raw)
    if mapped is not None:
        return mapped
    context = current_presentation()
    if context is None:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        return resolved if resolved.is_relative_to(context.workspace) else None
    local = (context.run_dir / candidate).resolve()
    if not local.is_relative_to(context.run_dir):
        raise ValueError("input path escapes the run namespace")
    if local.exists():
        return local
    supplied = (context.workspace / candidate).resolve()
    if not supplied.is_relative_to(context.workspace):
        raise ValueError("input path escapes the workspace namespace")
    return supplied if supplied.exists() else None
