from copy import deepcopy

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.recovery_handoff import build_recovery_handoff


def rejected_node():
    return TaskNode(node_id="old", goal="inspect", role="verifier", metadata={
        "runtime_execution_id": "execution-1",
        "_verification_receipt": {"verdict": "replan_required", "failed_dimensions": ["goal_coverage"]},
        "evidence_packet": {
            "summary": "Collector has incorrect method extraction",
            "next_actions": [{"goal": "Repair collector then rerun", "role": "coder", "reason": "POST treated as GET"}],
            "evidence_refs": ["runs/collector.log"],
        },
    })


def test_rejected_predecessor_without_exception_reaches_actual_worker_prompt(tmp_path):
    class Capture:
        role = "coder"
        default_tools = ["file_read"]
        captured = None

        def execute(self, context):
            self.captured = context
            raise RuntimeError("capture only")

    with Orchestrator(tmp_path, run_id="handoff") as orchestrator:
        old = rejected_node()
        node = TaskNode(node_id="repair", goal="repair collector", role="coder", metadata={"recovery_of": "old"})
        orchestrator.task_graph.add_node(old)
        orchestrator.task_graph.add_node(node)
        original = deepcopy(old.metadata)
        worker = Capture()
        try:
            orchestrator._run_agent_task(worker, node)
        except RuntimeError:
            pass
        assert worker.captured is not None
        handoff = worker.captured.metadata["recovery_handoff"]
        assert handoff["source_execution_id"] == "execution-1"
        assert handoff["verdict"] == "replan_required"
        assert handoff["proposed_actions_unapproved"][0]["goal"] == "Repair collector then rerun"
        prompt = Coder()._build_runtime_prompt(worker.captured, "implementation")
        assert "POST treated as GET" in prompt
        assert "未获准建议" in prompt
        assert "直接执行最小修复并运行对应复验" in prompt
        assert old.metadata == original


def test_cycle_and_fresh_nodes_do_not_invent_diagnosis():
    graph = TaskGraph()
    node = TaskNode(node_id="new", goal="fresh", role="coder", metadata={"recovery_of": "new"})
    graph.add_node(node)
    assert build_recovery_handoff(graph, node) == {}
    prompt = Coder()._build_runtime_prompt(AgentContext(node_id="new", goal="fresh"), "implementation")
    assert "恢复交接（" not in prompt


def test_nearest_rejection_wins_and_projection_is_bounded():
    graph = TaskGraph()
    old = rejected_node()
    node = rejected_node()
    node.node_id = "new"
    node.metadata["recovery_of"] = "old"
    node.metadata["evidence_packet"]["summary"] = "x" * 10000
    node.metadata["evidence_packet"]["next_actions"] *= 100
    graph.add_node(old)
    graph.add_node(node)
    packet = build_recovery_handoff(graph, node)
    assert packet["source_node_id"] == "new"
    assert len(packet["worker_summary_unverified"]) == 2000
    assert len(packet["proposed_actions_unapproved"]) == 6
