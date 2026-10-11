from pathlib import Path
import json

from AstralNexus.core.governor import TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator
import pytest


@pytest.mark.parametrize('same_failure', [False, True])
def test_planner_distinguishes_progressive_reference_fixes(tmp_path, monkeypatch, same_failure):
    from AstralNexus.core.exceptions import RealLLMRequiredError
    with Orchestrator(tmp_path, run_id='planner-reference-progress') as orchestrator:
        root_id = orchestrator.initialize_task_graph('build verified delivery', [])
        orchestrator.governor.llm_matching_enabled = True
        orchestrator.governor.llm_client = object()
        proposal = TaskGraphPatch(add_nodes=[{
            'node_id': 'design', 'goal': 'design architecture', 'dependencies': [root_id],
            'required_capabilities': ['system_architecture'],
        }])
        monkeypatch.setattr(orchestrator.governor, 'plan_task_graph_patch', lambda *args, **kwargs: proposal)
        monkeypatch.setattr(orchestrator.governor, 'replan_rejected_task_graph_patch', lambda *args, **kwargs: proposal)
        apply = orchestrator.governor.apply_task_graph_patch
        attempts = []
        def progressively_apply(*args, **kwargs):
            attempts.append(1)
            if len(attempts) <= 3:
                artifact = 'schema' if same_failure else ['schema', 'requirements', 'database'][len(attempts)-1]
                raise ValueError(f'graph patch references unknown input artifact: artifact:{artifact} (design)')
            return apply(*args, **kwargs)
        monkeypatch.setattr(orchestrator.governor, 'apply_task_graph_patch', progressively_apply)
        if same_failure:
            with pytest.raises(RealLLMRequiredError, match='same error repeated'):
                orchestrator.plan_task_decomposition(root_id)
            assert len(attempts) == 3
        else:
            assert orchestrator.plan_task_decomposition(root_id) == ['design']
            assert len(attempts) == 4


def test_governor_planner_persists_real_root_input_and_output(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, run_id="planner-io")
    root_id = orchestrator.initialize_task_graph("build verified delivery", [])
    orchestrator.governor.llm_matching_enabled = True
    orchestrator.governor.llm_client = object()
    setattr(orchestrator.governor, "plan_task_graph_patch", lambda *args, **kwargs: TaskGraphPatch(
        add_nodes=[{
            "node_id": "N-ARCH",
            "goal": "design system architecture",
            "dependencies": [root_id],
            "required_capabilities": ["system_architecture", "api_design"],
        }],
        reason_codes=["TEST_DYNAMIC_PLAN"],
        reasoning="separate architecture before implementation",
    ))

    added = orchestrator.plan_task_decomposition(root_id)

    assert added == ["N-ARCH"]
    input_files = list((tmp_path / "node_io").glob(f"*__node-{root_id}__*__input.json"))
    output_files = list((tmp_path / "node_io").glob(f"*__node-{root_id}__*__output.json"))
    assert len(input_files) == 1
    assert len(output_files) == 1
    planner_input = json.loads(input_files[0].read_text(encoding="utf-8"))
    planner_output = json.loads(output_files[0].read_text(encoding="utf-8"))
    assert planner_input["role"] == "planner"
    assert planner_input["goal"] == "build verified delivery"
    assert planner_output["execution_outcome"] == "succeeded"
    assert planner_output["next_actions"][0]["target"] == "N-ARCH"


def test_background_candidates_become_canonical_task_nodes(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    root_id = orchestrator.initialize_task_graph("完成可验证交付", [])
    parent = orchestrator.task_graph.get_node(root_id)
    assert parent is not None
    parent.metadata["background_candidates"] = [
        {
            "goal": "并行核验关键事实",
            "required_capabilities": ["verification"],
            "information_gain": 1.0,
            "risk_reduction": 1.0,
            "parallel_benefit": 1.0,
            "estimated_cost": 100,
        },
        {
            "goal": "并行检查交付约束",
            "required_capabilities": ["review"],
            "information_gain": 1.0,
            "risk_reduction": 1.0,
            "parallel_benefit": 1.0,
            "estimated_cost": 100,
        },
    ]

    admitted = orchestrator._admit_background_candidates(parent)

    assert len(admitted) == 2
    assert parent.metadata["spawned_task_ids"] == admitted
    for node_id in admitted:
        node = orchestrator.task_graph.get_node(node_id)
        assert node is not None
        assert node.dependencies == [root_id]
        assert node.metadata["execution_mode"] == "ready_wave"
        assert node.metadata["created_by"] == "global_governor"
    assert not hasattr(orchestrator, "background_workers")


def test_background_candidate_value_rejection_does_not_create_hidden_execution(
    tmp_path: Path,
):
    orchestrator = Orchestrator(tmp_path)
    root_id = orchestrator.initialize_task_graph("完成可验证交付", [])
    parent = orchestrator.task_graph.get_node(root_id)
    assert parent is not None
    parent.metadata["background_candidates"] = [
        {
            "goal": "低价值重复任务",
            "required_capabilities": [],
            "information_gain": 0.0,
            "risk_reduction": 0.0,
            "parallel_benefit": 0.0,
            "estimated_cost": 1_000_000,
        }
    ]

    admitted = orchestrator._admit_background_candidates(parent)

    assert admitted == []
    assert {
        node.node_id for node in orchestrator.task_graph.nodes.values()
    } == {"U-001", "P-001"}
    checked_node = orchestrator.task_graph.get_node("U-001")
    assert checked_node is not None
    assert checked_node.status == "completed"
    checked_node = orchestrator.task_graph.get_node("P-001")
    assert checked_node is not None
    assert checked_node.role == "planner"
    events = [
        event
        for event in orchestrator.trace_recorder.events
        if event["event_type"] == "governor.background_candidates_admitted"
    ]
    assert events[-1]["payload"]["task_node_ids"] == []
    assert events[-1]["payload"]["rejected"]


def test_fault_injection_uses_governed_pipeline_and_canonical_checkpoint(
    tmp_path: Path,
):
    orchestrator = Orchestrator(tmp_path, run_id="fault-run")
    root_id = orchestrator.initialize_task_graph("执行故障恢复验收", [])
    setattr(orchestrator.governor, "plan_fault_recovery", lambda **kwargs: {
        "strategy": "retry_failed_node",
        "actions": [
            {
                "type": "retry",
                "target": root_id,
                "details": {},
                "reason": "injected test recovery",
            }
        ],
        "reasoning": "test",
    })

    result = orchestrator.inject_fault(
        "node_failure",
        root_id,
        {"error_message": "injected test failure"},
    )

    assert result["target"] == root_id
    assert result["recovery_attempted"] is True
    restored = orchestrator.graph_store.load_task_graph("fault-run")
    assert restored is not None
    checked_node = restored.get_node(root_id)
    assert checked_node is not None
    assert checked_node.status in {"ready", "repair_pending"}


def test_router_communication_edge_is_committed_by_governor(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, run_id="route-run")
    root_id = orchestrator.initialize_task_graph("完成协同任务", [])
    applied = orchestrator.governor.apply_task_graph_patch(
        TaskGraphPatch(
            add_nodes=[
                {
                    "node_id": "peer",
                    "goal": "核验冲突证据",
                    "dependencies": [root_id],
                }
            ],
            reason_codes=["TEST_PEER"],
        ),
        parent_task_id=root_id,
    )
    assert applied["added"] == ["peer"]

    transmitted = orchestrator.router.route_value_message(
        sender=root_id,
        receiver="peer",
        msg_type="conflict_alert",
        task_id=root_id,
        claims=[{"claim": "发现约束冲突"}],
        information_gain=1.0,
        relevance=1.0,
        urgency=1.0,
    )

    assert transmitted is True
    assert any(
        edge.source == root_id
        and edge.target == "peer"
        and edge.edge_type == "communicates_to"
        for edge in orchestrator.task_graph.edges
    )
    assert any(
        event["event_type"] == "governor.graph_patch.applied"
        and event["payload"]["reason_codes"] == ["ROUTER_COMMUNICATION_LINK"]
        for event in orchestrator.trace_recorder.events
    )
    communication_events = [
        event
        for event in orchestrator.trace_recorder.events
        if event["event_type"] == "agent.communication.link_created"
    ]
    assert communication_events[-1]["payload"]["source"] == root_id
    assert communication_events[-1]["payload"]["target"] == "peer"
