import json
import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskNode
from AstralNexus.runtime.execution_accounting import execution_accounting


def test_unscored_followup_reaches_verification_recovery_without_write_grant(tmp_path):
    with Orchestrator(tmp_path, run_id="repair-review") as o:
        node = TaskNode(node_id="N", goal="check collector", role="verifier", status="result_submitted")
        o.task_graph.add_node(node)
        actions = [{"goal": "repair collector", "role": "coder", "reason": "wrong method extraction", "owned_paths": ["collector.py"]}]
        result = o.governor.admit_worker_subtasks(parent_node_id="N", proposals=actions)
        assert result["added"] == []
        assert result["rejected_proposals"] == []
        assert result["deferred_proposals"][0]["goal"] == "repair collector"
        assert [item.node_id for item in o.task_graph.nodes.values()] == ["N"]
        assert node.owned_paths == []
        captured = []

        class Client:
            def chat(self, messages, **kwargs):
                captured.extend(messages)
                return '{"add_nodes":[],"update_nodes":[],"reasoning":"inspect candidate"}'

        o.governor.llm_client = Client()
        o.governor.llm_matching_enabled = True
        o.governor.plan_verification_recovery(node_id="N", verdict="replan_required", overall_score=0.5, dimensions=[])
        payload = json.loads(captured[1]["content"])
        assert payload["failed_node_report"]["repair_candidates_pending_review"][0]["reason"] == "wrong method extraction"
        assert "不是已批准任务" in captured[0]["content"]


def test_completed_parent_optional_expansion_keeps_value_gate(tmp_path):
    with Orchestrator(tmp_path, run_id="optional") as o:
        o.task_graph.add_node(TaskNode(node_id="N", goal="done", role="coder", status="completed"))
        result = o.governor.admit_worker_subtasks(parent_node_id="N", proposals=[{"goal": "optional new feature"}])
        assert not result["added"]
        assert result["rejected_proposals"]
        assert not result["deferred_proposals"]


def test_exhausted_usage_is_partial_and_unknown_tokens_stay_unknown(tmp_path):
    measured = execution_accounting({"task_execution_budget": {"tool_calls_used": 60, "exhausted_reason": "max_tool_calls"}}, "succeeded")
    assert measured == {"actual_tokens": None, "tool_calls": 60, "execution_outcome": "partial", "stop_reason": "max_tool_calls"}
    with Orchestrator(tmp_path, run_id="accounting") as o:
        o.governor.record_task_start("N")
        snapshot = o.governor.record_task_end("N", **measured)
        assert snapshot["actual_tokens"] is None
        assert snapshot["token_usage_known"] is False
        assert snapshot["tool_calls"] == 60
        assert snapshot["execution_outcome"] == "partial"


def test_known_zero_and_failed_execution_are_not_changed():
    measured = execution_accounting({"prompt_tokens": 0, "completion_tokens": 0, "task_execution_budget": {"tool_calls_used": 2, "exhausted_reason": "max_seconds"}}, "failed")
    assert measured["actual_tokens"] == 0
    assert measured["execution_outcome"] == "failed"
    assert execution_accounting({}, "interrupted")["tool_calls"] is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_runtime_exception_preserves_consumed_budget(tmp_path, monkeypatch, interrupted):
    from AstralNexus.agents.base import AgentContext
    from AstralNexus.agents.coder import Coder
    from AstralNexus.core.exceptions import InterruptionRequested
    from AstralNexus.runtime import llm_client

    class Client:
        def __init__(self, **kwargs):
            pass

        def stream_chat(self, *args, **kwargs):
            budget = kwargs["task_execution_budget"]
            assert budget.admit_tool_call()[0]
            assert budget.admit_tool_call()[0]
            if interrupted:
                raise InterruptionRequested("cancelled")
            raise RuntimeError("worker error")

    monkeypatch.setattr(llm_client, "LLMClient", Client)
    response = Coder().try_execute_with_runtime(AgentContext(node_id="N", goal="test", metadata={"run_dir": str(tmp_path), "task_budget": {"max_tool_calls": 2}}), task_kind="implementation")
    assert response.metrics["task_execution_budget"]["tool_calls_used"] == 2
    assert response.outcome.value == ("interrupted" if interrupted else "failed")
