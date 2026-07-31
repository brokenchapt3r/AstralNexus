"""Tests for Governor.record_task_end() - real Worker lifecycle tracking."""
import pytest
from unittest.mock import MagicMock
from AstralNexus.core.governor import GlobalGovernor, GlobalBudget


@pytest.fixture
def governor():
    budget = GlobalBudget(max_tokens=10000, max_time_seconds=600, max_parallel_slots=4)
    trace_recorder = MagicMock()
    gov = GlobalGovernor.__new__(GlobalGovernor)
    gov.budget = budget
    gov.trace_recorder = trace_recorder
    gov.total_tasks_created = 0
    gov.max_total_tasks = 100
    gov.termination_requested = False
    return gov


def test_record_task_end_updates_budget(governor):
    """Task end should update real token/time usage and release parallel slot."""
    # Start a task
    governor.record_task_start("task_1", estimated_tokens=500)
    assert governor.budget.active_parallel_tasks == 1
    
    # End the task with real usage
    snapshot = governor.record_task_end(
        task_id="task_1",
        actual_tokens=620,
        actual_time_seconds=3.5,
        tool_calls=2,
        execution_outcome="succeeded",
    )
    
    # Budget updated with real consumption
    assert governor.budget.used_tokens == 620
    assert governor.budget.used_time_seconds == 3.5
    assert governor.budget.active_parallel_tasks == 0  # slot released
    
    # Snapshot contains audit info
    assert snapshot["task_id"] == "task_1"
    assert snapshot["actual_tokens"] == 620
    assert snapshot["execution_outcome"] == "succeeded"
    assert snapshot["remaining_tokens"] == 10000 - 620
    assert snapshot["budget_remaining_ratio"] == pytest.approx(0.938)


def test_record_task_end_multiple_tasks(governor):
    """Multiple tasks should accumulate real usage correctly."""
    # Start 3 tasks
    governor.record_task_start("task_1", 500)
    governor.record_task_start("task_2", 800)
    governor.record_task_start("task_3", 300)
    assert governor.budget.active_parallel_tasks == 3
    
    # End tasks with real usage
    governor.record_task_end("task_1", actual_tokens=480, actual_time_seconds=2.1)
    assert governor.budget.active_parallel_tasks == 2
    assert governor.budget.used_tokens == 480
    
    governor.record_task_end("task_2", actual_tokens=850, actual_time_seconds=4.3)
    assert governor.budget.active_parallel_tasks == 1
    assert governor.budget.used_tokens == 480 + 850
    
    governor.record_task_end("task_3", actual_tokens=290, actual_time_seconds=1.8)
    assert governor.budget.active_parallel_tasks == 0
    assert governor.budget.used_tokens == 480 + 850 + 290
    assert governor.budget.used_time_seconds == pytest.approx(2.1 + 4.3 + 1.8)


def test_record_task_end_timeout(governor):
    """Task end should record timeout status and stop_reason."""
    governor.record_task_start("task_1", 500)
    
    snapshot = governor.record_task_end(
        task_id="task_1",
        actual_tokens=300,
        actual_time_seconds=60.0,
        execution_outcome="interrupted",
        stop_reason="TIMEOUT",
    )
    
    assert snapshot["execution_outcome"] == "interrupted"
    assert snapshot["stop_reason"] == "TIMEOUT"
    assert governor.budget.active_parallel_tasks == 0


def test_record_task_end_budget_critical_alert(governor):
    """Should emit BUDGET_WARNING when remaining < 10%."""
    governor.budget.used_tokens = 8500  # 85% used
    
    governor.record_task_start("task_1", 500)
    snapshot = governor.record_task_end("task_1", actual_tokens=800, actual_time_seconds=5.0)
    
    # Now at 93% usage, should alert
    assert governor.budget.used_tokens == 8500 + 800
    assert governor.budget.remaining_tokens() == 700  # 7% remaining
    assert snapshot.get("alert") == "TOKEN_BUDGET_CRITICAL"
    
    # Verify trace recorder was called at least twice (BUDGET_WARNING + TASK_COMPLETED)
    assert governor.trace_recorder.record.call_count >= 2


def test_record_task_end_zero_usage(governor):
    """Task with zero real usage should still update correctly."""
    governor.record_task_start("task_1", 500)
    
    snapshot = governor.record_task_end(
        task_id="task_1",
        actual_tokens=0,
        actual_time_seconds=0.0,
        execution_outcome="failed",
        stop_reason="EXCEPTION",
    )
    
    assert snapshot["actual_tokens"] == 0
    assert governor.budget.used_tokens == 0
    assert governor.budget.active_parallel_tasks == 0
