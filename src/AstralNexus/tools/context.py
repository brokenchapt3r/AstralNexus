"""Per-call execution context for AstralNexus tools.

Tool handlers are shared by many workers, so the active workspace must not be
stored in a process-global mutable variable.  A context variable gives each
worker/thread its own safe workspace while preserving the existing environment
variable fallback for direct callers and compatibility tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
from typing import Iterator, Optional


_TOOL_WORKSPACE: ContextVar[Optional[Path]] = ContextVar(
    "astralnexus_tool_workspace", default=None
)
_TOOL_OWNED_PATHS: ContextVar[tuple[str, ...]] = ContextVar(
    "astralnexus_tool_owned_paths", default=()
)


def resolve_tool_path(path: Path) -> Path:
    """Resolve links first, then unify Win32 extended and ordinary namespaces.

    Windows non-strict resolve during concurrent parent creation can retain
    the device prefix on only one side of a containment comparison. The prefix
    does not change file identity. Do not normalize arbitrary device paths.
    """
    resolved = path.expanduser().resolve(strict=False)
    text = str(resolved)
    if os.name == 'nt':
        # Extended paths can name files that ordinary Win32 parsing would
        # reinterpret (trailing dots/spaces or DOS devices). Keep those exact.
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        if any(part.endswith(('.', ' ')) or part.split('.')[0].upper() in reserved
               for part in resolved.parts[1:]):
            return resolved
        if text[:8].casefold() == '\\\\?\\unc\\':
            return Path('\\\\' + text[8:])
        if text.startswith('\\\\?\\') and len(text) > 6 and text[5:7] == ':\\':
            return Path(text[4:])
    return resolved


def get_tool_workspace() -> Optional[Path]:
    """Return the workspace bound to the current tool invocation."""
    return _TOOL_WORKSPACE.get()


def get_tool_owned_paths() -> tuple[str, ...]:
    """Return the current Worker output ownership contract."""
    return _TOOL_OWNED_PATHS.get()


@contextmanager
def tool_workspace(root: str | Path | None) -> Iterator[None]:
    """Bind a normalized workspace for the duration of one tool call."""
    if root is None or not str(root).strip():
        yield
        return
    value = resolve_tool_path(Path(root))
    token = _TOOL_WORKSPACE.set(value)
    try:
        yield
    finally:
        _TOOL_WORKSPACE.reset(token)


@contextmanager
def tool_owned_paths(paths: list[str] | tuple[str, ...] | None) -> Iterator[None]:
    """Bind the Worker-owned write namespace for one LLM/tool session."""
    values = tuple(str(item).replace("\\", "/").strip() for item in (paths or ()) if str(item).strip())
    token = _TOOL_OWNED_PATHS.set(values)
    try:
        yield
    finally:
        _TOOL_OWNED_PATHS.reset(token)


def read_tool_path(raw: str) -> Path:
    """Use the same session-input/run namespace for every read-only tool."""
    from AstralNexus.runtime.presentation import mapped_input_path
    mapped = mapped_input_path(raw)
    if mapped is not None:
        return mapped
    from AstralNexus.runtime.run_context import get_env
    root = resolve_tool_path(get_tool_workspace() or Path(get_env("ASTRALNEXUS_WORKSPACE", "") or Path.cwd()))
    candidate = resolve_tool_path(root / Path(raw).expanduser())
    if not candidate.is_relative_to(root):
        raise ValueError("path is outside the workspace")
    return candidate
