import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.execution_ledger import ExecutionLedger


def test_evidence_retains_failed_command_through_validation_burst(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("tests", {"owned_paths": []})
    failed = store.append(execution, {"tool": "terminal_exec", "status": "failed",
                                     "returncode": 1, "stdout_tail": "intentional failure probe"})
    store.append(execution, {"tool": "pytest_run", "status": "success", "returncode": 0})
    for _ in range(25):
        store.append(execution, {"tool": "validation", "status": "success", "returncode": 0})
    evidence = store.evidence(execution)
    assert len(evidence["receipts"]) == 16
    assert evidence["omitted_receipts"] == 11
    assert any(r["receipt_id"] == failed["receipt_id"] and r["returncode"] == 1
               for r in evidence["receipts"])
    assert any(r["tool"] == "pytest_run" for r in evidence["receipts"])
    assert len(store.get(execution)["receipts"]) == 27


def test_receipts_and_failure_survive_reopen_and_cannot_be_accepted(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("backend", {"owned_paths": ["main.py"]})
    ledger = ExecutionLedger(tmp_path, ["main.py"], execution)
    ledger.observe({"id": "provider-id", "function": {"name": "python_exec", "arguments": "test"}},
                   {"status": "failed", "returncode": 1, "stderr": "assertion failed"})
    store.finish(execution, "failed", {"error": "test failed"})
    restored = ExecutionStore(tmp_path).get(execution)
    assert restored["receipts"][0]["raw_result"]["returncode"] == 1
    assert restored["acceptance"] is None
    with pytest.raises(ValueError, match="cannot be accepted"):
        store.accept(execution, {"approved": True})
    with pytest.raises(ValueError, match="terminal"):
        ledger.observe({"function": {"name": "file_read"}}, {"status": "success"})


def test_changed_upstream_or_owned_files_invalidate_acceptance(tmp_path):
    (tmp_path / "contract.md").write_text("v1", encoding="utf-8")
    source = tmp_path / "main.py"
    source.write_text("original", encoding="utf-8")
    store = ExecutionStore(tmp_path)
    execution = store.begin("backend", {"owned_paths": ["main.py"], "upstream_artifacts": [{"path": "contract.md"}]})
    store.finish(execution, "succeeded", {})
    source.write_text("changed", encoding="utf-8")
    assert store.evidence(execution)["owned_files_changed_since_finish"]
    with pytest.raises(ValueError, match="stale"):
        store.accept(execution, {"approved": True})
    source.write_text("original", encoding="utf-8")
    (tmp_path / "contract.md").write_text("v2", encoding="utf-8")
    assert store.evidence(execution)["upstream_files_changed"]
    with pytest.raises(ValueError, match="stale"):
        store.accept(execution, {"approved": True})


def test_same_provider_call_id_does_not_collide_between_executions(tmp_path):
    store = ExecutionStore(tmp_path)
    first, second = (store.begin("worker", {}) for _ in range(2))
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(lambda execution: store.append(execution, {"call_id": "same", "tool": "read"}), [first, second]))
    assert receipts[0]["receipt_id"] != receipts[1]["receipt_id"]
    assert len(store.get(first)["receipts"]) == len(store.get(second)["receipts"]) == 1


@pytest.mark.parametrize("upstream", ["inputs", "inputs/new.md"])
def test_new_upstream_file_invalidates_old_execution(tmp_path, upstream):
    (tmp_path / "inputs").mkdir()
    store = ExecutionStore(tmp_path)
    execution = store.begin("node", {"upstream_artifacts": [{"path": upstream}]})
    store.finish(execution, "succeeded", {})
    (tmp_path / "inputs/new.md").write_text("new requirement", encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        store.accept(execution, {"approved": True})


def test_resume_inherits_failure_as_reference_not_current_success(tmp_path):
    store = ExecutionStore(tmp_path)
    old = store.begin("old", {})
    store.append(old, {"tool": "validation", "status": "failed", "returncode": 1, "files": {}})
    store.interrupt_open(old)
    new = store.begin("replacement", {"previous_execution_id": old})
    ledger = ExecutionLedger(tmp_path, [], new)
    payload = json.loads(ledger.message()["content"].split("\n")[-1])
    assert payload["previous_execution"]["status"] == "interrupted"
    assert payload["previous_execution"]["evidence"]["receipts"][0]["returncode"] == 1
    assert store.get(new)["receipts"] == []


def test_valid_finished_execution_still_requires_explicit_verifier_decision(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("node", {})
    store.finish(execution, "succeeded", {})
    assert store.get(execution)["acceptance"] is None
    store.accept(execution, {"approved": True, "verdict": "approved"})
    assert store.get(execution)["acceptance"]["approved"]
