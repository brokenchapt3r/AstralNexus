"""Read-only runtime checks of declared local SQLite artifacts."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .execution_store import snapshot_files


def check_sqlite_artifacts(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    root = root.resolve()
    checks = []
    for relative in snapshot_files(root, paths):
        path = root / relative
        if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            continue
        result: dict[str, Any] = {"status": "success", "returncode": 0}
        try:
            # mode=ro must never create, initialize or repair the tested database.
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                deadline = time.monotonic() + 2
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                try:
                    integrity = [row[0] for row in db.execute("PRAGMA quick_check(50)")]
                    violations = db.execute("PRAGMA foreign_key_check").fetchmany(51)
                    result.update(integrity=integrity, foreign_key_violations=violations[:50],
                                  violations_truncated=len(violations) > 50)
                    if integrity != ["ok"] or violations:
                        result.update(status="failed", returncode=1,
                                      error="SQLite integrity check failed: foreign_key_violations are (table, rowid, parent_table, foreign_key_id). Inspect referenced keys and repair their generating/importing code; do not remove constraints or discard required fixture rows.")
                finally:
                    db.set_progress_handler(None, 0)
        except (sqlite3.Error, OSError) as exc:
            result.update(status="failed", returncode=1, error=str(exc))
        checks.append({"check": "sqlite_integrity", "target": relative, "result": result})
    return checks
