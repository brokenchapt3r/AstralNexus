"""Tests for Governor tool loop recovery strategy (问题12)."""
import pytest
from AstralNexus.core.governor import GlobalGovernor, EventType
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder


def _make_governor(tmp_path, max_active=10):
    g = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    g.max_active_tasks = max_active
    return g


class TestToolLoopRecovery:
    """Governor consumes ToolCallGuard decisions and selects recovery strategies."""

    def test_switch_tool_when_alternative_exists(self, tmp_path):
        g = _make_governor(tmp_path)
        decision = {
            "action": "switch_tool_or_replan",
            "reason": "repeated_tool_failure",
            "repeat_count": 1,
            "failure_count": 3,
        }
        result = g.handle_tool_loop_decision(
            node_id="task_1",
            tool_name="openalex_search",
            guard_decision=decision,
            role_toolset=["openalex_search", "crossref_search", "arxiv_search", "web_search"],
        )
        assert result["strategy"] == "switch_tool"
        assert result["alternative_tool"] in ("crossref_search", "arxiv_search", "web_search")

    def test_replan_when_no_alternative_available(self, tmp_path):
        g = _make_governor(tmp_path)
        decision = {
            "action": "switch_tool_or_replan",
            "reason": "repeated_tool_failure",
            "repeat_count": 1,
            "failure_count": 3,
        }
        result = g.handle_tool_loop_decision(
            node_id="task_2",
            tool_name="unique_tool",
            guard_decision=decision,
            role_toolset=["unique_tool"],
        )
        assert result["strategy"] == "replan"
        assert "Replan" in result["repair_goal"]

    def test_halt_when_budget_exhausted(self, tmp_path):
        g = _make_governor(tmp_path, max_active=0)
        decision = {
            "action": "halt",
            "reason": "repeated_identical_tool_call",
            "repeat_count": 5,
            "failure_count": 0,
        }
        result = g.handle_tool_loop_decision(
            node_id="task_3",
            tool_name="some_tool",
            guard_decision=decision,
            role_toolset=["some_tool"],
        )
        assert result["strategy"] == "halt"
        assert "exhausted" in result["reason"]

    def test_halt_action_with_no_alternatives_triggers_replan(self, tmp_path):
        g = _make_governor(tmp_path)
        decision = {
            "action": "halt",
            "reason": "repeated_identical_tool_call",
            "repeat_count": 5,
            "failure_count": 0,
        }
        result = g.handle_tool_loop_decision(
            node_id="task_4",
            tool_name="unique_tool",
            guard_decision=decision,
            role_toolset=["unique_tool"],
        )
        assert result["strategy"] == "replan"

    def test_trace_events_recorded(self, tmp_path):
        g = _make_governor(tmp_path)
        decision = {
            "action": "switch_tool_or_replan",
            "reason": "repeated_tool_failure",
            "repeat_count": 1,
            "failure_count": 3,
        }
        g.handle_tool_loop_decision(
            node_id="task_5",
            tool_name="openalex_search",
            guard_decision=decision,
            role_toolset=["openalex_search", "crossref_search"],
        )
        events = [e for e in g.trace_recorder.events if e.get("event_type") == EventType.TOOL_LOOP_RECOVERY]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["original_tool"] == "openalex_search"
        assert payload["reason_codes"] == ["TOOL_SWITCH"]
