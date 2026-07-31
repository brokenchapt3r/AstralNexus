"""Tests for the canonical Worker lifecycle protocol."""
from __future__ import annotations

from pathlib import Path

import pytest

from AstralNexus.core.lifecycle import AgentInstance, AgentLifecycleManager
from AstralNexus.core.role_registry import initialize_role_registry
from AstralNexus.core.state_machine import TaskStatus


@pytest.fixture
def lifecycle_manager(tmp_path: Path) -> AgentLifecycleManager:
    initialize_role_registry()
    return AgentLifecycleManager(tmp_path)


def _start(manager: AgentLifecycleManager, agent_id: str) -> None:
    manager.transition_agent(agent_id, TaskStatus.READY, "worker ready")
    manager.transition_agent(agent_id, TaskStatus.RUNNING, "worker started")


def test_worker_uses_task_status_as_its_only_state_protocol():
    instance = AgentInstance(agent_id="agent-001", role="coder", node_id="T-001")

    assert instance.state == TaskStatus.ADMITTED
    instance.transition(TaskStatus.READY)
    instance.transition(TaskStatus.RUNNING)
    assert instance.started_at is not None


def test_worker_rejects_lifecycle_shortcuts():
    instance = AgentInstance(agent_id="agent-001", role="coder", node_id="T-001")

    with pytest.raises(ValueError):
        instance.transition(TaskStatus.COMPLETED)


def test_worker_completes_only_after_verifier_verdict(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)
    lifecycle_manager.mark_result_submitted(instance.agent_id)

    assert instance.state == TaskStatus.RESULT_SUBMITTED
    lifecycle_manager.record_verification_verdict(
        instance.agent_id,
        approved=True,
        reason="Verifier approved",
    )
    assert instance.state == TaskStatus.COMPLETED
    assert instance.completed_at is not None


def test_rejected_worker_enters_repair_pending(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)
    lifecycle_manager.mark_result_submitted(instance.agent_id)
    lifecycle_manager.record_verification_verdict(
        instance.agent_id,
        approved=False,
        repairable=True,
        reason="Verifier requested repair",
    )

    assert instance.state == TaskStatus.REPAIR_PENDING


def test_recoverable_execution_error_uses_repair_pending(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)

    lifecycle_manager.mark_agent_error(
        instance.agent_id,
        "API timeout",
        recoverable=True,
    )

    assert instance.state == TaskStatus.REPAIR_PENDING
    assert instance.error_message == "API timeout"


def test_fatal_execution_error_uses_failed(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)

    lifecycle_manager.mark_agent_error(
        instance.agent_id,
        "invalid request",
        recoverable=False,
    )

    assert instance.state == TaskStatus.FAILED


def test_background_is_execution_metadata_not_a_state(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)

    assert lifecycle_manager.mark_background(instance.agent_id)
    assert instance.state == TaskStatus.RUNNING
    assert instance.metadata["execution_mode"] == "background"


def test_cancel_replaces_the_old_terminated_state(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)

    assert lifecycle_manager.terminate_agent(instance.agent_id, "manual stop")
    assert instance.state == TaskStatus.CANCELLED


def test_state_and_metrics_survive_runtime_snapshot(
    lifecycle_manager: AgentLifecycleManager,
    tmp_path: Path,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)
    lifecycle_manager.update_agent_metrics(
        instance.agent_id,
        iteration_count=2,
        token_usage={"prompt": 100, "completion": 20},
    )
    restored = AgentLifecycleManager(tmp_path)
    restored.restore_snapshot(lifecycle_manager.export_active_snapshot())
    loaded = restored.get_agent(instance.agent_id)

    assert loaded is not None
    assert loaded.state == TaskStatus.RUNNING
    assert loaded.iteration_count == 2
    assert loaded.token_usage["prompt"] == 100


def test_lifecycle_summary_counts_only_nonterminal_workers(
    lifecycle_manager: AgentLifecycleManager,
):
    running = lifecycle_manager.create_agent("coder", "T-001")
    cancelled = lifecycle_manager.create_agent("verifier", "T-002")
    _start(lifecycle_manager, running.agent_id)
    lifecycle_manager.cancel_agent(cancelled.agent_id)

    summary = lifecycle_manager.get_lifecycle_summary()

    assert summary["total_agents"] == 2
    assert summary["active_count"] == 1
    assert summary["by_state"][TaskStatus.RUNNING.value] == 1
    assert summary["by_state"][TaskStatus.CANCELLED.value] == 1


def test_health_check_reports_canonical_state(
    lifecycle_manager: AgentLifecycleManager,
):
    instance = lifecycle_manager.create_agent("coder", "T-001")
    _start(lifecycle_manager, instance.agent_id)

    health = lifecycle_manager.health_check(instance.agent_id)

    assert health["healthy"] is True
    assert health["state"] == TaskStatus.RUNNING.value
