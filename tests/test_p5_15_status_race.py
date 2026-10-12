"""Fix P5#15 (r17 crash, 2026-10-02): node bookkeeping must never kill a run.

Field anatomy: a recovery rearm (fault_recovery ``_retry_node``/
``_reassign_node``) legally clobbered a leased, actively executing node back
to ``ready`` (running -> ready is the cooperative interruption edge).  The
post-execution failure path then attempted ``ready -> failed`` — illegal —
and the uncaught ValueError propagated through three fallback layers
(execute_task, its except handler, _execute_wave) and killed the whole run
(exit 1 with 10 nodes completed and the publish gate unreached).

Layers under test:
1. ``transition_repair_plan`` — pure plan function: [] legal/identical,
   truthful intermediate writes for the known ready-race, None for skip.
2. ``Orchestrator._transition_node_status_guarded`` — applies the plan on a
   fresh read, records events, never raises.
3. fault_recovery rearms skip leased running nodes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from AstralNexus.core.state_machine import (
    TaskStatus,
    assert_transition,
    transition_repair_plan,
)


def test_legal_and_identical_transitions_need_no_repair():
    assert transition_repair_plan("running", "failed") == []
    assert transition_repair_plan("ready", "running") == []
    assert transition_repair_plan("running", "running") == []
    assert transition_repair_plan(TaskStatus.RUNNING, TaskStatus.RESULT_SUBMITTED) == []


def test_ready_race_repair_restores_the_truthful_running_state():
    assert transition_repair_plan("ready", "failed") == ["running"]
    assert transition_repair_plan("ready", "result_submitted") == ["running"]
    assert transition_repair_plan("ready", "verifying") == ["running", "result_submitted"]


def test_protected_and_unknown_corruption_skip_instead_of_guessing():
    # Terminal history is never clobbered …
    assert transition_repair_plan("completed", "failed") is None
    assert transition_repair_plan("cancelled", "ready") is None
    assert transition_repair_plan("superseded", "failed") is None
    # … and genuinely illegal jumps are skipped, not forced.
    assert transition_repair_plan("waiting_dependencies", "failed") is None
    assert transition_repair_plan("ready", "completed") is None
    assert transition_repair_plan("proposed", "running") is None


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        transition_repair_plan("bogus", "failed")


class _FakeGraph:
    """Enforces the real state machine on every write."""

    def __init__(self, status):
        self.node = SimpleNamespace(status=status)
        self.writes = []

    def get_node(self, node_id):
        return self.node

    def update_node_status(self, node_id, status):
        assert_transition(self.node.status, status)
        self.node.status = status
        self.writes.append(status)


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def record(self, event_type, payload):
        self.events.append((event_type, payload))


def _guard(current, target, context="test"):
    from AstralNexus.core.orchestrator import Orchestrator

    graph = _FakeGraph(current)
    recorder = _FakeRecorder()
    stub = SimpleNamespace(task_graph=graph, trace_recorder=recorder)
    ok = Orchestrator._transition_node_status_guarded(stub, "N-1", target, context)
    return ok, graph, recorder


def test_guarded_write_repairs_the_r17_race():
    ok, graph, recorder = _guard("ready", "failed", "post_execution")
    assert ok is True
    assert graph.writes == ["running", "failed"]
    assert any(event == "task.execution.status_race_repaired" for event, _ in recorder.events)


def test_guarded_write_direct_legal_path_is_quiet():
    ok, graph, recorder = _guard("running", "failed")
    assert ok is True
    assert graph.writes == ["failed"]
    assert not any(event == "task.execution.status_race_repaired" for event, _ in recorder.events)


def test_guarded_write_skips_protected_terminal_states():
    for status in ("completed", "cancelled", "superseded"):
        ok, graph, recorder = _guard(status, "failed")
        assert ok is False, status
        assert graph.writes == [], status
        assert any(event == "task.execution.invalid_transition" for event, _ in recorder.events), status


def test_guarded_write_never_raises_on_unknown_states():
    ok, graph, recorder = _guard("waiting_dependencies", "failed")
    assert ok is False
    assert graph.writes == []
    assert any(event == "task.execution.invalid_transition" for event, _ in recorder.events)


def test_guarded_write_missing_node_degrades():
    from AstralNexus.core.orchestrator import Orchestrator

    recorder = _FakeRecorder()
    stub = SimpleNamespace(
        task_graph=SimpleNamespace(get_node=lambda node_id: None),
        trace_recorder=recorder,
    )
    ok = Orchestrator._transition_node_status_guarded(stub, "N-404", "failed", "test")
    assert ok is False
    assert any(event == "task.execution.invalid_transition" for event, _ in recorder.events)


def test_recovery_rearm_skips_leased_running_node():
    """Root-race guard: fault_recovery must not clobber a running node."""
    from AstralNexus.core.fault_recovery import RecoveryExecutor

    graph = _FakeGraph("running")
    recorder = _FakeRecorder()
    executor = RecoveryExecutor.__new__(RecoveryExecutor)
    executor.task_graph = graph
    executor.trace_recorder = recorder

    def _boom(node_id):
        raise AssertionError("rearm must not reach repair request for a running node")

    executor._request_node_repair = _boom
    assert executor._retry_node("N-1") is False
    assert graph.writes == []
    assert any(event == "recovery.action.skip_running_node" for event, _ in recorder.events)
