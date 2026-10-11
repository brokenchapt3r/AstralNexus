"""Read-only current graph/cursor projection, separate from historical events."""
from __future__ import annotations
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


def read_current_projection(run_dir: Path, *, run_id: str = "") -> dict[str, Any]:
    path = Path(run_dir) / "task_graph.sqlite"
    if not path.is_file():
        return {"source": "trace_legacy", "verified": False}
    try:
        store = SQLiteTaskGraphStore(path, read_only=True)
        for _ in range(3):
            before = store.latest_commit_fence(run_id)
            if before is None:
                return {"source": "trace_legacy", "verified": False, "reason": "no_authoritative_fence"}
            if not before.get("cursor"):
                raise ValueError("authoritative fence has no cursor")
            graph = store.load_task_graph(before["run_id"])
            after = store.read_commit_fence(before["run_id"])
            if after != {key: value for key, value in before.items() if key != "fence_identity_count"}:
                continue
            if graph is None:
                raise ValueError("authoritative graph missing")
            status = str(before["cursor"].get("run_status") or "unknown")
            ownership: dict[str, Any] = {}
            owners_path = Path(run_dir) / ".astralnexus_state" / "runs.sqlite"
            if owners_path.is_file():
                with closing(sqlite3.connect(owners_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("SELECT * FROM runs").fetchall()
                    if len(rows) != 1:
                        raise ValueError("ambiguous ownership identity")
                    ownership = dict(rows[0])
                    status = str(ownership.get("status") or status)
            return {"source": "checkpoint", "verified": True, "run_id": before["run_id"],
                    "commit_seq": before["commit_seq"], "generation": before["generation"],
                    "cursor": before["cursor"], "graph": graph.to_dict(), "run_status": status,
                    "generation_alignment": "pending_generation" if ownership and int(ownership.get("generation") or 0) != before["generation"] else "current",
                    "ownership": ownership, "execution_only": not ownership and status == "completed"}
        raise ValueError("checkpoint changed during all projection attempts")
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        return {"source": "unavailable", "verified": False, "run_status": "unknown", "reason": str(error)}
