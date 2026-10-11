"""Checkpoint/cancellation invariants ported as AstralNexus contracts.

The scenario set is based on DeerFlow's MIT-licensed cancellation and
checkpoint regressions; persistence objects and assertions are AstralNexus's.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.runtime.run_coordinator import RunCoordinator


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _coordinator(tmp_path, clock, run_id="ported-run"):
    return RunCoordinator(
        tmp_path,
        run_id=run_id,
        lease_seconds=30,
        grace_seconds=0,
        now_fn=clock,
    )


def test_repeated_live_cancel_is_idempotent_and_stops_the_same_owner(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(tmp_path, clock)
    lease = coordinator.acquire("owner-a")
    assert lease is not None

    assert coordinator.request_cancel() == "requested"
    assert coordinator.request_cancel() == "requested"
    assert coordinator.is_cancel_requested() is True
    assert coordinator.should_stop(lease) is True


def test_repeated_cancel_of_halted_run_has_a_stable_terminal_result(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(tmp_path, clock)
    lease = coordinator.acquire("owner-a")
    assert lease is not None
    assert coordinator.finish(
        lease, status="blocked", stop_reason="waiting", stop_code="user_interrupt"
    )

    assert coordinator.request_cancel() == "taken_over"
    assert coordinator.request_cancel() == "already_terminal"
    assert coordinator.current()["status"] == "cancelled"


def test_expired_owner_cannot_renew_finish_or_checkpoint_after_takeover(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(tmp_path, clock)
    stale = coordinator.acquire("owner-a")
    assert stale is not None
    clock.advance(31)
    current = coordinator.acquire("owner-b")
    assert current is not None

    assert coordinator.renew(stale) is None
    assert coordinator.finish(stale, status="completed") is False
    assert coordinator.record_checkpoint(stale, {"commit": "stale"}) is False
    assert coordinator.record_checkpoint(current, {"commit": "current"}) is True
    assert coordinator.last_checkpoint() == {"commit": "current"}


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_terminal_runs_are_never_reacquired(tmp_path, terminal_status):
    clock = _Clock()
    coordinator = _coordinator(tmp_path, clock, run_id=terminal_status)
    lease = coordinator.acquire("owner-a")
    assert lease is not None
    assert coordinator.finish(lease, status=terminal_status)
    clock.advance(300)

    assert coordinator.acquire("owner-b") is None
    assert coordinator.acquire("owner-b", user_resume=True) is None


def test_checkpoint_restore_is_repeatable_and_preserves_terminal_nodes(tmp_path):
    with Orchestrator(tmp_path, run_id="ported-checkpoint") as original:
        original.task_graph.add_node(
            {"node_id": "done", "goal": "done", "role": "coder", "status": "completed"}
        )
        original.task_graph.add_node(
            {"node_id": "running", "goal": "resume", "role": "tester", "status": "running"}
        )
        original._save_checkpoint()

    observed = []
    for _ in range(2):
        with Orchestrator(tmp_path, run_id="ported-checkpoint") as restored:
            assert restored.restore_checkpoint() is True
            observed.append(
                (
                    restored.task_graph.get_node("done").status,
                    restored.task_graph.get_node("running").status,
                )
            )

    assert observed == [("completed", "ready"), ("completed", "ready")]

