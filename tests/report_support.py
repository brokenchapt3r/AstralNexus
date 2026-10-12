"""Shared report-recovery test data; no verdicts or assertions are hidden here."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

from AstralNexus.runtime.execution_store import ExecutionStore


def write_file(root: Path, relative: str, text: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def terminal_receipt(
    files: dict[str, Any], *, command: str, call_id: str,
    stdout_tail: str, status: str = "success", returncode: int | None = 0,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "tool": "terminal_exec", "call_id": call_id, "status": status,
        "returncode": returncode, "stdout_tail": stdout_tail,
        "files": dict(files), "arguments": json.dumps({"command": command}),
    }
    if receipt_id is not None:
        receipt["receipt_id"] = receipt_id
    return receipt


def execution_row_count(root: Path) -> int:
    with closing(sqlite3.connect(root / ".astralnexus_state" / "executions.sqlite")) as db:
        return int(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0])


def receipt_count(root: Path, execution: str) -> int:
    return len(ExecutionStore(root).get(execution)["receipts"])


def stored_receipt_id(root: Path, execution: str) -> str:
    """Use the ledger-assigned identity, not the ID requested by a fixture."""
    return str(ExecutionStore(root).get(execution)["receipts"][0]["receipt_id"])
