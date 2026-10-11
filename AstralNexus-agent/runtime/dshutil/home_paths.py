"""Shared filesystem path helpers for DeepSeek Harness user data.

Port of ``@deepseek-ai/dsh-home-paths`` (commit 5badb15). Contract kept:
- Single-root home resolution with precedence: explicit configured path >
  non-blank ``$DSH_HOME`` > ``~/.dsh``; an empty or whitespace-only
  ``$DSH_HOME`` is treated as unset (never resolves to the CWD).
- Tilde expansion supports exactly ``~``, ``~/`` and ``~\\`` prefixes.
- ``dsh_home_display`` distinguishes the default root (``~/.dsh``) from an
  override (``$DSH_HOME``).
- ``canonicalize_watch_path`` gives native watchers one canonical spelling
  even when trailing components do not exist: the deepest existing ancestor is
  realpath'd and — when a suffix is missing — proved to be an enumerable
  directory before the suffix is restored; non-absence errors propagate.

Python adaptation: ``canonicalize_watch_path`` is synchronous (os.lstat/
os.path.realpath/os.scandir); existence is probed with lstat so a missing
component raises FileNotFoundError exactly like upstream's ENOENT path, and a
regular-file ancestor fails the enumerability proof with NotADirectoryError
or PermissionError instead of being mistaken for absence.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

DSH_HOME_DIR_NAME = ".dsh"
DEFAULT_DSH_HOME_DISPLAY = f"~/{DSH_HOME_DIR_NAME}"
DSH_HOME_ENV = "DSH_HOME"


def default_dsh_home() -> str:
    """Absolute default harness home using platform path rules."""
    return str(Path(os.path.expanduser("~")) / DSH_HOME_DIR_NAME)


def expand_home_path(path: str) -> str:
    """Expand ``~``, ``~/``, ``~\\`` prefixes against the OS home; otherwise verbatim."""
    if path == "~":
        return os.path.expanduser("~")
    if path.startswith("~/") or path.startswith("~\\"):
        return os.path.join(os.path.expanduser("~"), path[2:])
    return path


def resolve_dsh_home(configured: Optional[str] = None,
                     env: Optional[Dict[str, str]] = None) -> str:
    """Normalized absolute harness home: configured > non-blank $DSH_HOME > default."""
    environment = os.environ if env is None else env
    from_env = environment.get(DSH_HOME_ENV)
    if configured is not None:
        chosen = configured
    elif from_env is not None and from_env.strip():
        chosen = from_env
    else:
        chosen = default_dsh_home()
    return os.path.abspath(expand_home_path(chosen))


def dsh_home_path(*segments: str) -> str:
    """Join segments onto the resolved harness home; empty returns the home itself."""
    return os.path.join(resolve_dsh_home(), *segments)


def dsh_cache_path(options_or_segment: Union[str, Dict[str, Any], None] = None,
                   *segments: str) -> str:
    """Join onto the home's ``cache`` directory without creating it."""
    if isinstance(options_or_segment, str):
        return os.path.join(resolve_dsh_home(), "cache", options_or_segment, *segments)
    if isinstance(options_or_segment, dict):
        override = options_or_segment.get("dsh_home", options_or_segment.get("dshHome"))
        home = resolve_dsh_home(str(override) if override is not None else None)
        return os.path.join(home, "cache", *segments)
    return os.path.join(resolve_dsh_home(), "cache", *segments)


def dsh_home_display(resolved: str) -> str:
    """Stable user-facing form: ``~/.dsh`` for the default root, else ``$DSH_HOME``."""
    return DEFAULT_DSH_HOME_DISPLAY if os.path.abspath(resolved) == os.path.abspath(default_dsh_home()) else "$DSH_HOME"


def canonicalize_watch_path(path: Union[str, "os.PathLike[str]"]) -> str:
    """Canonical spelling for watcher targets whose trailing components may not exist."""
    current = os.path.abspath(os.fspath(path))
    missing = []
    while True:
        try:
            os.lstat(current)
        except FileNotFoundError:
            parent = os.path.dirname(current)
            if parent == current:  # a filesystem root exists; never reached in practice
                raise
            missing.append(os.path.basename(current))
            current = parent
            continue
        canonical = os.path.realpath(current)
        if missing:
            # Prove the existing ancestor is an enumerable directory: Windows
            # must not treat a regular-file ancestor as ordinary absence.
            entries = os.scandir(canonical)
            entries.close()
        return os.path.join(canonical, *reversed(missing))
