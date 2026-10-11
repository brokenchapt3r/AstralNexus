"""Tests for core.state_machine module with 12-state lifecycle."""

import pytest

from AstralNexus.core.state_machine import (
    VALID_TRANSITIONS,
    TaskStatus,
    assert_transition,
    can_transition,
)


class TestTaskStatus:
    """Test TaskStatus enum."""

    def test_all_states_defined(self):
        """All 12 states are defined in TaskStatus enum."""
        expected = {
            "proposed", "admitted", "waiting_dependencies", "ready",
            "running", "result_submitted", "verifying", "completed",
            "repair_pending", "cancelled", "failed", "superseded", "blocked"
        }
        actual = {s.value for s in TaskStatus}
        assert actual == expected

    def test_valid_transitions_matches_enum(self):
        """VALID_TRANSITIONS keys match TaskStatus values."""
        assert set(VALID_TRANSITIONS.keys()) == {s.value for s in TaskStatus}


class TestCanTransition:
    """Test can_transition() function."""

    # Initial flow
    def test_proposed_to_admitted(self):
        assert can_transition("proposed", "admitted") is True

    def test_proposed_to_cancelled(self):
        assert can_transition("proposed", "cancelled") is True

    def test_proposed_to_ready_invalid(self):
        assert can_transition("proposed", "ready") is False

    def test_admitted_to_waiting_dependencies(self):
        assert can_transition("admitted", "waiting_dependencies") is True

    def test_admitted_to_ready(self):
        assert can_transition("admitted", "ready") is True

    def test_admitted_to_cancelled(self):
        assert can_transition("admitted", "cancelled") is True

    def test_admitted_to_running_invalid(self):
        assert can_transition("admitted", "running") is False

    # Dependency waiting
    def test_waiting_dependencies_to_ready(self):
        assert can_transition("waiting_dependencies", "ready") is True

    def test_waiting_dependencies_to_cancelled(self):
        assert can_transition("waiting_dependencies", "cancelled") is True

    def test_waiting_dependencies_to_running_invalid(self):
        assert can_transition("waiting_dependencies", "running") is False

    # Ready to execute
    def test_ready_to_running(self):
        assert can_transition("ready", "running") is True

    def test_ready_to_blocked(self):
        assert can_transition("ready", "blocked") is True

    def test_ready_to_cancelled(self):
        assert can_transition("ready", "cancelled") is True

    def test_ready_to_admitted_invalid(self):
        assert can_transition("ready", "admitted") is False

    # Execution
    def test_running_to_result_submitted(self):
        assert can_transition("running", "result_submitted") is True

    def test_running_to_failed(self):
        assert can_transition("running", "failed") is True

    def test_running_to_blocked(self):
        assert can_transition("running", "blocked") is True

    def test_running_to_cancelled(self):
        assert can_transition("running", "cancelled") is True

    def test_running_to_completed_invalid(self):
        assert can_transition("running", "completed") is False

    def test_blocked_to_ready(self):
        assert can_transition("blocked", "ready") is True

    def test_blocked_to_running(self):
        assert can_transition("blocked", "running") is False

    # Post-execution verification
    def test_result_submitted_to_verifying(self):
        assert can_transition("result_submitted", "verifying") is True

    def test_result_submitted_to_failed(self):
        assert can_transition("result_submitted", "failed") is True

    def test_result_submitted_to_completed_invalid(self):
        assert can_transition("result_submitted", "completed") is False

    def test_verifying_to_completed(self):
        assert can_transition("verifying", "completed") is True

    def test_verifying_to_repair_pending(self):
        assert can_transition("verifying", "repair_pending") is True

    def test_verifying_to_failed(self):
        assert can_transition("verifying", "failed") is True

    def test_verifying_to_running_invalid(self):
        assert can_transition("verifying", "running") is False

    # Repair cycle
    def test_repair_pending_to_running(self):
        assert can_transition("repair_pending", "running") is False

    def test_repair_pending_to_failed(self):
        assert can_transition("repair_pending", "failed") is True

    def test_repair_pending_to_completed_invalid(self):
        assert can_transition("repair_pending", "completed") is False

    # Failure recovery
    def test_failed_to_repair_pending(self):
        assert can_transition("failed", "repair_pending") is True

    def test_failed_to_ready(self):
        assert can_transition("failed", "ready") is False

    def test_failed_to_running(self):
        assert can_transition("failed", "running") is False

    def test_failed_to_completed_invalid(self):
        assert can_transition("failed", "completed") is False

    # Terminal states
    def test_completed_to_superseded(self):
        assert can_transition("completed", "superseded") is True

    def test_completed_to_running_invalid(self):
        assert can_transition("completed", "running") is False

    def test_completed_to_failed_invalid(self):
        assert can_transition("completed", "failed") is False

    def test_cancelled_is_terminal(self):
        """cancelled has no outgoing transitions."""
        for state in VALID_TRANSITIONS:
            if state != "cancelled":
                assert can_transition("cancelled", state) is False

    def test_superseded_is_terminal(self):
        """superseded has no outgoing transitions."""
        for state in VALID_TRANSITIONS:
            if state != "superseded":
                assert can_transition("superseded", state) is False


class TestAssertTransition:
    """Test assert_transition() function."""

    def test_valid_transition_no_exception(self):
        """Valid transition does not raise."""
        assert_transition("proposed", "admitted")  # Should not raise

    def test_invalid_transition_raises(self):
        """Invalid transition raises ValueError."""
        with pytest.raises(ValueError, match="Invalid task graph state transition"):
            assert_transition("proposed", "ready")

    def test_invalid_transition_message(self):
        """Error message includes state names."""
        with pytest.raises(ValueError, match="proposed -> ready"):
            assert_transition("proposed", "ready")


class TestValidTransitions:
    """Test VALID_TRANSITIONS constant."""

    def test_all_states_defined(self):
        """All 13 states are defined in VALID_TRANSITIONS."""
        expected_states = {
            "proposed", "admitted", "waiting_dependencies", "ready",
            "running", "result_submitted", "verifying", "completed",
            "repair_pending", "cancelled", "failed", "superseded", "blocked"
        }
        assert set(VALID_TRANSITIONS.keys()) == expected_states

    def test_superseded_is_terminal(self):
        """superseded has no outgoing transitions."""
        assert VALID_TRANSITIONS["superseded"] == set()

    def test_cancelled_is_terminal(self):
        """cancelled has no outgoing transitions."""
        assert VALID_TRANSITIONS["cancelled"] == set()

    def test_completed_near_terminal(self):
        """completed only transitions to superseded."""
        assert VALID_TRANSITIONS["completed"] == {"superseded"}

    def test_proposed_has_two_targets(self):
        """proposed can transition to admitted or cancelled."""
        assert VALID_TRANSITIONS["proposed"] == {"admitted", "cancelled"}

    def test_running_allows_cooperative_interrupt(self):
        """running can return to ready for a resumable interactive interrupt."""
        assert VALID_TRANSITIONS["running"] == {
            "result_submitted", "ready", "failed", "blocked", "cancelled", "superseded"
        }

    def test_verifying_can_requeue_after_a_transport_interruption(self):
        """No-verdict transport cuts requeue verification without a failure verdict."""
        assert VALID_TRANSITIONS["verifying"] == {
            "result_submitted", "completed", "repair_pending", "failed", "cancelled", "superseded"
        }
