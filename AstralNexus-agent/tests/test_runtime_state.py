import json

from AstralNexus.core.governor import GlobalBudget
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.task_graph import Fact, TaskEdge, TaskGraph, TaskNode


def _graph() -> TaskGraph:
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="T-001",
        goal="root",
        role="planner",
        status="completed",
    ))
    graph.add_node(TaskNode(
        node_id="T-002",
        goal="collect evidence",
        role="researcher",
        status="ready",
        dependencies=["T-001"],
    ))
    graph.add_edge(TaskEdge("T-002", "T-001", "depends_on"))
    graph.add_fact(Fact(
        fact_id="F-001",
        text="unverified observation",
        source="T-001",
        confidence=0.5,
        recorded_at="2026-01-01T00:00:00+00:00",
    ))
    return graph


def test_runtime_state_observes_and_persists_global_state(tmp_path):
    manager = RuntimeStateManager(tmp_path, "run-1")
    graph = _graph()

    state = manager.observe(
        task_graph=graph,
        global_goal="deliver",
        task_version=2,
        iteration=7,
        phase="observing",
        run_status="running",
        track_progress=True,
        topology_override={
            "all_edges": [["T-001", "T-002"]],
            "metrics": {"sparsity": 0.5},
        },
    )

    assert state.global_goal == "deliver"
    assert state.task_version == 2
    assert state.nodes_by_status == {"completed": 1, "ready": 1}
    assert state.ready_nodes == ["T-002"]
    assert state.uncertainty["low_confidence_fact_ids"] == ["F-001"]
    assert state.topology_state["metrics"]["sparsity"] == 0.5
    persisted = json.loads((tmp_path / "runtime_state.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "observing"


def test_runtime_state_detects_repeated_no_progress_and_restores(tmp_path):
    graph = _graph()
    manager = RuntimeStateManager(tmp_path, "run-1")
    manager.observe(
        task_graph=graph,
        global_goal="deliver",
        task_version=1,
        iteration=1,
        phase="observing",
        run_status="running",
        track_progress=True,
    )
    repeated = manager.observe(
        task_graph=graph,
        global_goal="deliver",
        task_version=1,
        iteration=2,
        phase="observing",
        run_status="running",
        track_progress=True,
    )
    assert repeated.repeated_no_progress == 1

    restored = RuntimeStateManager(tmp_path, "run-1")
    assert restored.state.repeated_no_progress == 1
    assert restored.state.progress_fingerprint == repeated.progress_fingerprint


def test_recovery_converges_only_after_same_noop_repeats(tmp_path):
    manager = RuntimeStateManager(tmp_path, "run-1")
    assert manager.record_recovery("same", changed=False) == 1
    assert manager.record_recovery("same", changed=False) == 2
    assert manager.record_recovery("same", changed=False) == 3
    assert manager.record_recovery("different", changed=False) == 1
    assert manager.record_recovery("different", changed=True) == 0


def test_formal_global_budget_is_unbounded_until_configured():
    budget = GlobalBudget()
    budget.used_tokens = 9_000_000
    budget.used_time_seconds = 9_000_000
    assert budget.remaining_tokens() > budget.used_tokens
    assert budget.remaining_time() > budget.used_time_seconds
    assert budget.can_admit_task(1_000_000)


def test_stalled_graph_recovery_applies_governor_patch(tmp_path):
    class FakeClient:
        def chat(self, _messages):
            return json.dumps({
                "add_nodes": [{
                    "node_id": "replacement",
                    "goal": "replace failed evidence collector",
                    "task_type": "implementation",
                    "required_capabilities": ["research"],
                    "dependencies": [],
                    "acceptance_criteria": ["evidence exists"],
                    "expected_outputs": [],
                }],
                "update_nodes": [
                    {"node_id": "T-001", "status": "superseded"},
                    {"node_id": "T-002", "dependencies": ["replacement"]},
                ],
                "cancel_nodes": [],
                "add_edges": [],
                "remove_edges": [],
                "reason_codes": ["CAPABILITY_SEPARATION"],
                "reasoning": "replace failed strategy",
                "planning_status": "OK",
            }, ensure_ascii=False)

    orchestrator = Orchestrator(tmp_path, max_iterations=0)
    failed = TaskNode(
        node_id="T-001",
        goal="collect evidence",
        role="researcher",
        status="failed",
    )
    failed.metadata["_terminal_failure"] = True
    blocked = TaskNode(
        node_id="T-002",
        goal="use evidence",
        role="reviewer",
        status="waiting_dependencies",
        dependencies=["T-001"],
    )
    orchestrator.task_graph.add_node(failed)
    orchestrator.task_graph.add_node(blocked)
    orchestrator.task_graph.add_edge(TaskEdge("T-002", "T-001", "depends_on"))
    orchestrator.governor.enable_llm_worker_matching(FakeClient())

    result = orchestrator._replan_stalled_graph(
        goal="deliver",
        blocked_nodes=["T-002"],
        reason="blocked_by_failed_dependency",
    )

    assert result["changed"] is True
    assert orchestrator.task_graph.get_node("T-001").status == "superseded"
    assert orchestrator.task_graph.get_node("T-002").dependencies != ["T-001"]
    replacement_id = orchestrator.task_graph.get_node("T-002").dependencies[0]
    assert orchestrator.task_graph.get_node(replacement_id).goal == (
        "replace failed evidence collector"
    )
