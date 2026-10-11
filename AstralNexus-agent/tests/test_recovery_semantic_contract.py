import hashlib
import json
from pathlib import Path

import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.runtime.acceptance_spec import load_spec, case1_api_checks
from AstralNexus.runtime.requirement_identity import frozen_bindings, bind_observations
from AstralNexus.runtime.evidence_identity import satisfied_acceptance_items


def test_completed_node_without_accepted_evidence_does_not_earn_progress(tmp_path):
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path, run_id="progress") as runtime:
        runtime._consume_recovery_churn("baseline")
        runtime.task_graph.add_node(TaskNode(node_id="done", goal="done", role="coder", status="completed"))
        runtime._consume_recovery_churn("changed", structural_changed=True)
        assert runtime.runtime_state_manager.state.recovery_no_progress_churn == 2
        runtime.runtime_state_manager.credit_evidence_key("approved-original-requirement")
        runtime._consume_recovery_churn("recheck", structural_changed=False)
        assert runtime.runtime_state_manager.state.recovery_no_progress_churn == 1


def test_recovery_phases_share_one_identity_and_charge_once_after_reload(tmp_path):
    from AstralNexus.core.runtime_state import RuntimeStateManager
    with Orchestrator(tmp_path, run_id="operation") as runtime:
        with runtime.trace_recorder.recovery_scope() as operation:
            runtime.trace_recorder.record("governor.verification_recovery.proposed", {})
            runtime._consume_recovery_churn("rejected")
            runtime._consume_recovery_churn("applied")
            events = runtime.trace_recorder.events[-3:]
            assert all(event["payload"]["recovery_operation_id"] == operation for event in events)
        assert runtime.runtime_state_manager.state.recovery_churn_total == 1
        restored = RuntimeStateManager(tmp_path, "operation")
        assert restored.record_churn("replayed", operation_id=operation) == 1


def test_operation_gap_and_token_budgets_have_independent_units(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_SAME_GAP_OPERATIONS", "5")
    monkeypatch.setenv("ASTRALNEXUS_TOTAL_RECOVERY_OPERATIONS", "9")
    monkeypatch.setenv("ASTRALNEXUS_RUN_TOKEN_BUDGET", "100")
    with Orchestrator(tmp_path, run_id="limits") as runtime:
        runtime.runtime_state_manager.record_lineage_cost("root", tokens=100, recoveries=1)
        verdict = runtime._lineage_stand_down_verdict()
        assert verdict is not None and verdict["budget_dimension"] == "tokens"
        assert runtime._recovery_limits().same_gap_operations == 5
        assert runtime._recovery_limits().total_operations == 9


def test_frozen_requirement_identity_survives_node_ids_and_rejects_self_authorship():
    bindings = frozen_bindings({"global_acceptance_criteria": [{"criterion_id": "REQ-repeat", "text": "tests_passed:python verify.py"}]})
    for node in ("P-105", "P-111"):
        observations = bind_observations([{"criterion_id": node + ".AC01", "criterion": "tests_passed:python verify.py", "checked": True, "holds": True, "original_requirement_id": "invented"}], bindings)
        assert satisfied_acceptance_items(observations)[0] == ["REQ-repeat"]
    observations = bind_observations([{"criterion_id": "P-111.AC02", "checked": True, "holds": True, "original_requirement_id": "invented"}], bindings)
    assert satisfied_acceptance_items(observations)[0] == []
    unknown = bind_observations([{"criterion": "invented:new report", "checked": True, "holds": True}], bindings, strict=True)
    assert satisfied_acceptance_items(unknown)[0] == []


def test_acceptance_spec_refuses_a_different_task_book(tmp_path):
    task = tmp_path / "task.md"
    task.write_text("case1", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"schema": "astralnexus.acceptance-spec/v1", "profile": "case1", "task_book_sha256": hashlib.sha256(task.read_bytes()).hexdigest()}), encoding="utf-8")
    assert load_spec(spec, task)["profile"] == "case1"
    task.write_text("different contract", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen task book"):
        load_spec(spec, task)


def test_case1_verifier_sends_case1_fields_and_rejects_legacy_response():
    calls = []
    def transport(base, method, path, payload=None):
        calls.append(payload)
        return 400, '{"error":"invalid_payload"}', 0
    results = case1_api_checks(transport, "http://local", "nonce")
    assert calls[0] == {"external_id": "nonce", "title": "nonce", "priority": 1}
    assert results[0]["ok"] is False


def test_stop_categories_distinguish_cost_and_no_progress_without_new_status(tmp_path):
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    coordinator = RunCoordinator(tmp_path, run_id="causes")
    lease = coordinator.acquire("owner")
    assert lease is not None
    assert coordinator.finish(lease, "blocked", stop_code="cost_budget_exhausted", stop_reason="budget")
    assert coordinator.current()["stop_category"] == "cost_exhausted"
    assert coordinator.acquire("new", user_resume=True) is not None
    assert coordinator.current()["stop_category"] == "none"
    assert coordinator.current()["last_stop_category"] == "cost_exhausted"


def test_independent_acceptance_runs_a_copy_and_preserves_the_published_database(tmp_path, monkeypatch):
    import importlib.util
    import sqlite3
    from types import SimpleNamespace
    from AstralNexus.runtime import acceptance_spec
    path = Path(__file__).resolve().parents[3] / "02_运行产物/capability_migration/b18_autonomous/verify_acceptance.py"
    loader = importlib.util.spec_from_file_location("independent_case1", path)
    assert loader is not None and loader.loader is not None
    verifier = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(verifier)
    root = tmp_path / "published"
    database = root / "app/data/tickets.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE tickets (external_id TEXT)")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    (root / "verify_case.py").write_text("# copied test command", encoding="utf-8")
    monkeypatch.setattr(verifier, "REPORT_PATH", tmp_path / "report.json")
    from AstralNexus.tools import builtin_tools
    monkeypatch.setattr(builtin_tools, "_run_foreground_command", lambda *a, **kw: {"returncode": 0, "timed_out": False, "stdout": "CASE-OK"})
    launched = []
    def start(workspace, argv, port, tag, env):
        launched.append(workspace)
        assert workspace != root and workspace.is_dir()
        connection = sqlite3.connect(workspace / "app/data/tickets.sqlite3")
        connection.execute("INSERT INTO tickets VALUES ('independent-42')")
        connection.commit()
        connection.close()
        return SimpleNamespace(poll=lambda: None), None, None, 1
    monkeypatch.setattr(verifier, "start", start)
    monkeypatch.setattr(verifier, "stop", lambda p: None)
    monkeypatch.setattr(verifier, "listeners", lambda p: [])
    monkeypatch.setattr(verifier.time, "time_ns", lambda: 42)
    monkeypatch.setattr(verifier, "call", lambda *a: (200, '{"items":[{"external_id":"independent-42"}]}', 0))
    monkeypatch.setattr(verifier, "finish", lambda: 0 if all(c["ok"] for c in verifier.checks) else 1)
    monkeypatch.setattr(acceptance_spec, "case1_api_checks", lambda *a: [])
    monkeypatch.setattr(acceptance_spec, "case1_browser_checks", lambda *a: [])
    assert verifier.verify_frozen_case1(root, ["python", "app/server.py"], 8099, "probe") == 0
    assert database.read_bytes() == original
    assert len(launched) == 2 and launched[0] == launched[1]
    assert not launched[0].exists()


def test_parallel_cost_updates_do_not_lose_counts_or_collide_on_cursor_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from AstralNexus.core.runtime_state import RuntimeStateManager
    manager = RuntimeStateManager(tmp_path, "parallel-costs")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: manager.record_lineage_cost("root", requests=1), range(40)))
    assert manager.lineage_entry("root")["requests"] == 40
    assert RuntimeStateManager(tmp_path, "parallel-costs").lineage_entry("root")["requests"] == 40


def test_parallel_stream_usage_is_not_deduplicated_across_requests(tmp_path):
    with Orchestrator(tmp_path, run_id="streams") as runtime:
        for request in ("A", "B"):
            runtime._record_lineage_event_cost("__delivery__", "llm.stream.started", {"request_id": request})
        for request, tokens in (("A", 60), ("B", 60), ("A", 80)):
            runtime._record_lineage_event_cost("__delivery__", "llm.usage", {"request_id": request, "prompt_tokens": tokens, "completion_tokens": 0})
        assert runtime.runtime_state_manager.lineage_entry("__delivery__")["tokens"] == 140
