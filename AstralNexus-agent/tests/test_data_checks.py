import json
import sqlite3
from contextlib import closing

import pytest

from AstralNexus.agents.base import BaseAgent
from AstralNexus.runtime.data_checks import check_sqlite_artifacts
from AstralNexus.runtime.execution_ledger import ExecutionLedger
from AstralNexus.runtime.execution_store import ExecutionStore


def test_detect_feedback_repair_and_recheck(tmp_path):
    path = tmp_path / "app.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE assets(id TEXT PRIMARY KEY); "
                         "CREATE TABLE orders(asset TEXT REFERENCES assets(id)); "
                         "INSERT INTO assets VALUES('EQ-001'); "
                         "INSERT INTO orders VALUES('EQ-0001');")
    store = ExecutionStore(tmp_path)
    execution = store.begin("backend", {"owned_paths": ["app.sqlite"]})
    ledger = ExecutionLedger(tmp_path, ["app.sqlite"], execution)
    payload = json.loads(ledger.message()["content"].split("\n")[-1])
    assert payload["current_data_checks"][0]["result"]["foreign_key_violations"]
    assert BaseAgent._validation_failures(payload["current_data_checks"])
    ledger.observe({"function": {"name": "terminal_exec"}}, {"status": "success", "returncode": 0})
    assert '"status": "failed"' in ledger.message()["content"]
    store.finish(execution, "succeeded", {})
    with pytest.raises(ValueError, match="Data validation failed"):
        store.accept(execution, {"approved": True})
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE orders SET asset='EQ-001'")
        db.commit()
    repaired = store.begin("backend", {"owned_paths": ["app.sqlite"], "previous_execution_id": execution})
    ledger = ExecutionLedger(tmp_path, ["app.sqlite"], repaired)
    payload = json.loads(ledger.message()["content"].split("\n")[-1])
    assert not BaseAgent._validation_failures(payload["current_data_checks"])
    assert payload["previous_execution"]["evidence"]["receipts"][0]["status"] == "failed"
    store.finish(repaired, "succeeded", {})
    store.accept(repaired, {"approved": True})


def test_read_only_scope_and_invalid_database(tmp_path):
    path = tmp_path / "bad.sqlite"
    path.write_bytes(b"not a database")
    checks = check_sqlite_artifacts(tmp_path, ["missing.sqlite", "bad.sqlite", "../outside.sqlite"])
    assert len(checks) == 1
    assert checks[0]["result"]["status"] == "failed"
    assert path.read_bytes() == b"not a database"
    assert not (tmp_path / "missing.sqlite").exists()
