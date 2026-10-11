import json
from AstralNexus.runtime.execution_ledger import ExecutionLedger


def test_ledger_resume_and_file_change_preserve_failed_execution(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old", encoding="utf-8")
    ledger = ExecutionLedger(tmp_path, ["app.py"])
    ledger.observe({"id": "check", "function": {"name": "python_exec", "arguments": "check"}},
                   {"status": "error", "returncode": 1, "stderr": "syntax error"})
    source.write_text("new", encoding="utf-8")
    ledger.observe({"id": "write", "function": {"name": "file_write"}}, {"status": "success"})
    resumed = ExecutionLedger(tmp_path, ["app.py"])
    data = json.loads(resumed.message()["content"].split("\n")[-1])
    failed = data["recent_observations"][0]
    assert failed["returncode"] == 1
    assert failed["owned_files_changed_since_observation"] is True
    assert data["files"]["app.py"]["bytes"] == 3
    assert len(resumed.observations) == 2


def test_rejected_write_does_not_claim_artifact_or_execution_success(tmp_path):
    ledger = ExecutionLedger(tmp_path, ["app.py"])
    ledger.observe({"id": "bad", "function": {"name": "file_write"}},
                   {"status": "error", "error": "truncated"})
    assert ledger.files() == {}
    assert ledger.observations[0]["status"] == "error"
    assert ledger.observations[0]["returncode"] is None


def test_command_evidence_snapshot_precedes_later_write_in_same_batch(tmp_path, monkeypatch):
    from AstralNexus.tools.executor import ToolExecutor
    source = tmp_path / "app.py"
    source.write_text("before", encoding="utf-8")
    ledger = ExecutionLedger(tmp_path, ["app.py"])
    executor = ToolExecutor(workspace_root=tmp_path, owned_paths=["app.py"])
    executor.result_observer = ledger.observe
    def execute(name, arguments):
        if name == "file_write":
            source.write_text("after", encoding="utf-8")
        return {"status": "success", "returncode": 0}
    monkeypatch.setattr(executor, "execute_tool", execute)
    executor.execute_tool_calls([
        {"id": "check", "function": {"name": "python_exec", "arguments": {"code": "check"}}},
        {"id": "write", "function": {"name": "file_write", "arguments": {"path": "app.py", "content": "after"}}},
    ])
    data = json.loads(ledger.message()["content"].split("\n")[-1])
    assert data["recent_observations"][0]["owned_files_changed_since_observation"]


def test_read_index_survives_resume_and_tracks_versions_without_claiming_validation(tmp_path):
    from AstralNexus.runtime.execution_store import ExecutionStore
    source = tmp_path / "app.py"
    source.write_text("original", encoding="utf-8")
    execution = ExecutionStore(tmp_path).begin("coder", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    call = {"function": {"name": "file_read"}}
    for _ in range(2):
        ledger.observe(call, {"status": "success", "path": str(source), "truncated": True})
    resumed = ExecutionLedger(tmp_path, ["app.py"], execution)
    data = json.loads(resumed.message()["content"].split("\n")[-1])
    read = data["read_index"]["app.py"]
    assert read["reads_of_version"] == 2 and read["unchanged"] and read["truncated"]
    assert data["recent_observations"] == []
    source.write_text("changed", encoding="utf-8")
    data = json.loads(resumed.message()["content"].split("\n")[-1])
    assert not data["read_index"]["app.py"]["unchanged"]
    resumed.observe(call, {"status": "error", "path": str(source)})
    resumed.observe(call, {"status": "success", "path": str(source), "truncated": False})
    data = json.loads(resumed.message()["content"].split("\n")[-1])
    assert data["read_index"]["app.py"]["reads_of_version"] == 1
    assert data["read_index"]["app.py"]["unchanged"]
    assert not data["read_index"]["app.py"]["truncated"]


def test_background_log_refresh_survives_resume_without_claiming_ready(tmp_path):
    from AstralNexus.runtime.execution_store import ExecutionStore
    execution = ExecutionStore(tmp_path).begin("tester", {"owned_paths": ["reports"]})
    log = tmp_path / "server.log"
    log.write_text("starting", encoding="utf-8")
    ledger = ExecutionLedger(tmp_path, ["reports"], execution)
    ledger.observe({"function": {"name": "terminal_exec"}}, {
        "status": "started", "background": True, "pid": 123,
        "log_path": str(log), "cwd": str(tmp_path), "command": ["server"],
    })
    resumed = ExecutionLedger(tmp_path, ["reports"], execution)
    log.write_text('Error loading ASGI app: could not import main', encoding="utf-8")
    data = json.loads(resumed.message()["content"].split("\n")[-1])
    process = data["background_processes"][0]
    assert "could not import main" in process["log_tail"]
    assert process["readiness"] == "unverified"
    assert process["launch_status"] == "started"
    assert process["cwd"] == str(tmp_path)


def test_rejected_call_receipts_share_batch_order_and_never_execute(tmp_path, monkeypatch):
    from AstralNexus.runtime.execution_store import ExecutionStore
    from AstralNexus.tools.executor import ToolExecutor
    store = ExecutionStore(tmp_path)
    execution = store.begin("verifier", {"owned_paths": []})
    ledger = ExecutionLedger.for_context(tmp_path, [], execution)
    executor = ToolExecutor(workspace_root=tmp_path, owned_paths=[])
    executor.result_observer = ledger.observe
    executed = []
    def execute(name, arguments):
        executed.append(arguments["command"])
        return {"status": "success", "returncode": 0}
    monkeypatch.setattr(executor, "execute_tool", execute)
    calls = [{"id": ident, "function": {"name": "terminal_exec", "arguments": {"command": ident}}}
             for ident in ("first", "denied", "last")]
    results = executor.execute_model_calls(calls, {"denied": {"status": "budget_exhausted", "error": "not executed"}})
    assert executed == ["first", "last"]
    assert [result["status"] for result in results] == ["success", "budget_exhausted", "success"]
    receipts = store.get(execution)["receipts"]
    assert [receipt["call_id"] for receipt in receipts] == ["first", "denied", "last"]
    assert receipts[1]["returncode"] is None


def test_receipt_write_failure_stops_batch_before_next_mutation(tmp_path, monkeypatch):
    import pytest
    from AstralNexus.tools.executor import ToolExecutor
    executor = ToolExecutor(workspace_root=tmp_path)
    executed = []
    def execute(name, arguments):
        executed.append(name)
        return {"status": "success"}
    def fail_receipt(call, result):
        raise OSError("receipt store unavailable")
    monkeypatch.setattr(executor, "execute_tool", execute)
    executor.result_observer = fail_receipt
    calls = [{"id": name, "function": {"name": name, "arguments": {}}}
             for name in ("terminal_exec", "file_write")]
    with pytest.raises(OSError, match="receipt store unavailable"):
        executor.execute_model_calls(calls)
    assert executed == ["terminal_exec"]
