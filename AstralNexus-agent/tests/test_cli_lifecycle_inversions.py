"""Offline counterexamples for CLI termination, isolation and observer order."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from AstralNexus.cli import main
from AstralNexus.cli.input_staging import stage_task_inputs


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid_utf8"])
def test_early_input_failure_does_not_mask_error_or_sweep_other_run(tmp_path, monkeypatch, kind):
    task = tmp_path / "task.md"
    if kind == "directory":
        task.mkdir()
    elif kind == "invalid_utf8":
        task.write_bytes(b"\xff")
    def forbidden(*args, **kwargs):
        pytest.fail("an early failure must not clean services or acquire ownership")
    monkeypatch.setattr(main, "_sweep_run_services", forbidden)
    monkeypatch.setattr(main, "_acquire_run_ownership", forbidden)
    assert main._run_task(SimpleNamespace(task=str(task), output=None), tmp_path) == 1


def test_staging_prunes_configuration_dependencies_and_its_own_output(tmp_path):
    task = tmp_path / "task.md"
    task.write_text("task", encoding="utf-8")
    for name in [".env", ".env.local", ".git/config", "node_modules/pkg/index.js", "data/input.csv", "docs/spec.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("input", encoding="utf-8")
    run = tmp_path / "active_output"
    run.mkdir()
    (run / "old.txt").write_text("old", encoding="utf-8")
    staged = {Path(p).as_posix() for p in stage_task_inputs(task, run)}
    assert staged == {"inputs/task.md", "inputs/data/input.csv", "inputs/task_bundle/docs/spec.md"}


@pytest.mark.parametrize("setting,value", [("MAX_FILES", "1"), ("MAX_FILE_BYTES", "2"), ("MAX_TOTAL_BYTES", "6")])
def test_oversized_input_bundle_is_rejected_before_copy(tmp_path, monkeypatch, setting, value):
    task = tmp_path / "task.md"
    task.write_text("task", encoding="utf-8")
    (tmp_path / "data.csv").write_text("data", encoding="utf-8")
    monkeypatch.setenv("ASTRALNEXUS_INPUT_" + setting, value)
    run = tmp_path / "output"
    with pytest.raises(ValueError, match="exceeds"):
        stage_task_inputs(task, run)
    assert not (run / "inputs").exists()




def test_default_runs_are_distinct_and_rejected_owner_never_sweeps(tmp_path, monkeypatch):
    task = tmp_path / "task.md"
    task.write_text("task", encoding="utf-8")
    acquired = []
    monkeypatch.setattr(main, "_acquire_run_ownership", lambda path, **kwargs: (acquired.append(path), None))
    def forbidden(*args, **kwargs):
        pytest.fail("a rejected owner must not sweep another owner's services")
    monkeypatch.setattr(main, "_sweep_run_services", forbidden)
    args = SimpleNamespace(task=str(task), output=None, max_parallel=None)
    assert main._run_task(args, tmp_path) == 2
    assert main._run_task(args, tmp_path) == 2
    assert acquired[0] != acquired[1]


def test_observer_is_reentrant_and_started_precedes_finished(tmp_path, monkeypatch):
    controller = main._InteractiveRunController(tmp_path)
    events = []
    entered, release = threading.Event(), threading.Event()
    def run(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return 1
    monkeypatch.setattr(main, "_run_task", run)
    def observe(event):
        controller.status()  # This used to reacquire an already held Lock.
        events.append(event["event_type"])
    controller.set_event_callback(observe)
    assert controller.dispatch_command("/run example.md")["ok"]
    assert entered.wait(3)
    rejected = []
    thread = threading.Thread(target=lambda: rejected.append(controller.start("second.md")), daemon=True)
    thread.start()
    thread.join(2)
    try:
        assert not thread.is_alive(), "callback/status deadlock"
        assert rejected == [False]
    finally:
        release.set()
        controller.thread.join(3)
    assert events[0] == "interactive.run.started"
    assert events[-1] == "interactive.run.finished"


def test_human_resume_passes_authorization_and_allocates_new_channels(tmp_path, monkeypatch):
    controller = main._InteractiveRunController(tmp_path)
    old_queue = controller.interrupt_queue
    captured = []
    monkeypatch.setattr(main, "_resume_task", lambda args, *a, **kw: captured.append(args) or 0)
    controller.set_event_callback(lambda event: None)
    assert controller.dispatch_command(f'/resume "{tmp_path}"')["ok"]
    controller.thread.join(3)
    assert captured[0].user_resume is True
    assert captured[0].authorization_source == "interactive:/resume"
    old_queue.put({"type": "interrupt", "message": "late old heartbeat"})
    assert controller.interrupt_queue.empty()


def test_parallel_pointer_writers_leave_a_complete_pointer(tmp_path):
    runs = [tmp_path / f"run-{index}" for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda run: main._write_current_run_pointer(tmp_path, run), runs))
    payload = json.loads((tmp_path / "current_run.json").read_text(encoding="utf-8"))
    assert payload["run_dir"] in {str(run.resolve()) for run in runs}
    assert not list(tmp_path.glob("*.tmp"))


def _delivery_seams(monkeypatch):
    from AstralNexus.runtime import llm_client
    import importlib
    registry = importlib.import_module("AstralNexus.tools.registry")
    tool_router = importlib.import_module("AstralNexus.tools.tool_router")
    calls, reasons, recoveries = [], [], []
    class Client:
        def __init__(self, **kwargs):
            pass
        def chat(self, *args, **kwargs):
            calls.append(1)
            return "missing required heading"
    monkeypatch.setattr(llm_client, "LLMClient", Client)
    monkeypatch.setattr(registry, "discover_builtin_tools", lambda: None)
    monkeypatch.setattr(tool_router, "discover_tools", lambda *args: [])
    monkeypatch.setattr(main, "_generate_failure_report", lambda **kwargs: reasons.append(kwargs["reason"]) or {"status": "failed"})
    runtime = SimpleNamespace(governor=object(), trace_recorder=SimpleNamespace(record=lambda *args: None),
        _record_lineage_event_cost=lambda *args: None,
        admit_recovery=lambda *args: recoveries.append(args) or None)
    return runtime, calls, reasons, recoveries






def test_active_fault_injection_cannot_create_second_orchestrator(tmp_path, monkeypatch):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    run = tmp_path / "runs" / "fault-active"
    with Orchestrator(run, run_id=run.name) as runtime:
        runtime.task_graph.add_node(TaskNode(node_id="worker", goal="task", role="coder", status="ready"))
        runtime._save_checkpoint()
    coordinator = RunCoordinator(run, run_id=run.name)
    lease = coordinator.acquire("real-owner")
    assert lease is not None
    def forbidden(*args, **kwargs):
        pytest.fail("inject-fault must not instantiate a second manager for a live run")
    monkeypatch.setattr(Orchestrator, "__init__", forbidden)
    assert main._inject_fault(SimpleNamespace(run_id=run.name, type="node_failure", payload=None), tmp_path) == 2
    assert coordinator.current()["owner_id"] == "real-owner"
    assert coordinator.current()["generation"] == lease.generation


def test_inactive_fault_injection_commits_with_fence_and_requires_explicit_resume(tmp_path, monkeypatch):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
    run = tmp_path / "runs" / "fault-idle"
    with Orchestrator(run, run_id=run.name) as runtime:
        runtime.task_graph.add_node(TaskNode(node_id="worker", goal="task", role="coder", status="ready"))
        runtime._save_checkpoint()
    def inject(runtime, *args):
        runtime.task_graph.nodes["worker"].metadata["injected"] = True
        assert runtime._persist_graph_checkpoint(stage="fault_injection")
        return {"fault_id": "owned", "recovery_success": False}
    monkeypatch.setattr(Orchestrator, "inject_fault", inject)
    assert main._inject_fault(SimpleNamespace(run_id=run.name, type="node_failure", payload=None), tmp_path) == 0
    store = SQLiteTaskGraphStore(run / "task_graph.sqlite")
    fence = store.read_commit_fence(run.name)
    assert fence is not None and fence["generation"] == 1
    assert store.load_task_graph(run.name).nodes["worker"].metadata["injected"]
    coordinator = RunCoordinator(run, run_id=run.name)
    assert coordinator.current()["status"] == "paused"
    assert coordinator.acquire("automatic-resumer") is None


def test_stopped_heartbeat_suppresses_a_late_renew_failure():
    import queue
    from AstralNexus.runtime.run_lifecycle import _start_lease_heartbeat, _stop_lease_heartbeat
    entered, release = threading.Event(), threading.Event()
    interrupt, messages = threading.Event(), queue.Queue()
    def renew(lease):
        entered.set()
        assert release.wait(3)
        return None
    coordinator = SimpleNamespace(lease_seconds=1, renew=renew)
    _, stop, thread, lost = _start_lease_heartbeat(coordinator, object(), interrupt, messages)
    try:
        assert entered.wait(7)
        stop.set()
        release.set()
        _stop_lease_heartbeat(stop, thread)
        assert not thread.is_alive()
        assert not lost.is_set() and not interrupt.is_set() and messages.empty()
    finally:
        stop.set()
        release.set()
        thread.join(3)



def test_planning_rejection_is_not_a_host_exception():
    from AstralNexus.core.exceptions import PlanningRejectedError
    status, code, reason = main._entry_exception_disposition(PlanningRejectedError("unknown input reference"))
    assert (status, code) == ("failed", "planning_failed")
    assert "unknown input reference" in reason
    assert main._entry_exception_disposition(ConnectionError("read timeout"))[:2] == ("blocked", "host_exception")
