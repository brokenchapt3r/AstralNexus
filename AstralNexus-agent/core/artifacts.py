from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

