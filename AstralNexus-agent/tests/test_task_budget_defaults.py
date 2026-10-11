"""P6-D (doc 32 §4): default task-budget table + ensure_task_budget.

Guards the r14 P5#9 pathology: a graph-patch node created with budget={}
escaped to an unbounded execution (4.9h / r1100+ tool rounds) because zero
means unlimited downstream.  These tests pin: declared positive budgets pass
through verbatim, missing/zero/junk caps are filled from the role/target
table, and the fill is never zero.
"""

from __future__ import annotations

from AstralNexus.runtime.task_execution_budget import (
    TaskExecutionBudget,
    default_max_tool_calls,
    ensure_task_budget,
)


class TestDefaultMaxToolCalls:
    def test_known_pairs(self) -> None:
        assert default_max_tool_calls("implementation", "cloud") == 240
        assert default_max_tool_calls("synthesis", "cloud") == 300
        assert default_max_tool_calls("verification", "edge") == 120
        assert default_max_tool_calls("analysis", "local") == 160

    def test_task_type_aliases(self) -> None:
        assert default_max_tool_calls("coder", "cloud") == 240
        assert default_max_tool_calls("synthesizer", "cloud") == 300
        assert default_max_tool_calls("tester", "cloud") == 200
        assert default_max_tool_calls("docs", "cloud") == 200

    def test_planner_other_vocabulary(self) -> None:
        assert default_max_tool_calls("other", "cloud") == 150
        assert default_max_tool_calls("", "edge") == 100

    def test_unknown_degrades_never_zero(self) -> None:
        for task_type in ("", "quantum-wizard", "OTHER", " Implementation "):
            for target in ("", "cloud", "edge", "local", "moon"):
                value = default_max_tool_calls(task_type, target)
                assert isinstance(value, int) and value > 0


class TestEnsureTaskBudget:
    def test_declared_positive_budget_passes_through(self) -> None:
        declared = {"max_tool_calls": 80, "max_seconds": 900, "max_tokens": 100000}
        budget, defaulted = ensure_task_budget(declared, task_type="implementation", execution_target="cloud")
        assert defaulted is False
        assert budget == declared

    def test_missing_budget_filled_from_table(self) -> None:
        budget, defaulted = ensure_task_budget({}, task_type="synthesizer", execution_target="cloud")
        assert defaulted is True
        assert budget["max_tool_calls"] == 300

    def test_non_dict_budget_filled(self) -> None:
        for junk in (None, "big", 42, ["max_tool_calls"]):
            budget, defaulted = ensure_task_budget(junk, task_type="implementation", execution_target="cloud")
            assert defaulted is True
            assert budget["max_tool_calls"] == 240

    def test_zero_and_negative_caps_count_as_undeclared(self) -> None:
        budget, defaulted = ensure_task_budget(
            {"max_tool_calls": 0, "max_seconds": -5}, task_type="verification", execution_target="edge"
        )
        assert defaulted is True
        assert budget["max_tool_calls"] == 120
        # max_seconds is never defaulted (P5#2 wall-clock pathology).
        assert "max_seconds" not in budget

    def test_declared_seconds_survive_without_calls(self) -> None:
        budget, defaulted = ensure_task_budget({"max_seconds": 600}, task_type="other", execution_target="cloud")
        assert defaulted is True
        assert budget == {"max_seconds": 600, "max_tool_calls": 150}

    def test_bool_values_rejected(self) -> None:
        budget, defaulted = ensure_task_budget({"max_tool_calls": True}, task_type="coder", execution_target="cloud")
        assert defaulted is True
        assert budget["max_tool_calls"] == 240

    def test_filled_budget_is_enforceable(self) -> None:
        """The whole point: a filled budget must actually build a cap."""
        budget, _ = ensure_task_budget({}, task_type="synthesizer", execution_target="cloud")
        calls = budget.get("max_tool_calls")
        assert isinstance(calls, int)
        cap = TaskExecutionBudget(max_tool_calls=calls, max_seconds=0)
        assert cap.max_tool_calls == 300
        for _ in range(300):
            admitted, reason = cap.admit_tool_call()
            assert admitted and reason is None
        admitted, reason = cap.admit_tool_call()
        assert not admitted
        assert reason == "max_tool_calls"
