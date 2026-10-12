from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from threading import RLock
from weakref import WeakValueDictionary

_append_guard = RLock()
_append_locks: WeakValueDictionary = WeakValueDictionary()


def _append_lock(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _append_guard:
        lock = _append_locks.get(key)
        if lock is None:
            lock = RLock()
            _append_locks[key] = lock
        return lock


def run_root(home: Path) -> Path:
    path = home / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_run_dir(home: Path, prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = run_root(home) / f"{prefix}-{stamp}"
    path = base
    idx = 1
    while path.exists():
        idx += 1
        path = Path(f"{base}-{idx}")
    path.mkdir(parents=True)
    (path / "artifacts").mkdir()
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock(path):
        from AstralNexus.runtime.dshutil.atomic_write import file_lock
        with file_lock(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)

