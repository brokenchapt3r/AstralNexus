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
from typing import Iterator, Optional


_TOOL_WORKSPACE: ContextVar[Optional[Path]] = ContextVar(
    "astralnexus_tool_workspace", default=None
)


def get_tool_workspace() -> Optional[Path]:
    """Return the workspace bound to the current tool invocation."""
    return _TOOL_WORKSPACE.get()


@contextmanager
def tool_workspace(root: str | Path | None) -> Iterator[None]:
    """Bind a normalized workspace for the duration of one tool call."""
    if root is None or not str(root).strip():
        yield
        return
    value = Path(root).expanduser().resolve()
    token = _TOOL_WORKSPACE.set(value)
    try:
        yield
    finally:
        _TOOL_WORKSPACE.reset(token)
