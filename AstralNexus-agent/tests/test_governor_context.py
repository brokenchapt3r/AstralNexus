import json
from pathlib import Path

import pytest

from AstralNexus.core.governor import (
    EventType,
    GlobalGovernor,
    TaskGraphPatch,
    _parse_governor_json,
)
from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
from AstralNexus.core.agent_registry import get_agent_registry
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.runtime.model_selector import get_model_config


def test_model_config_cli_overrides(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  default: old-model\nprovider:\n  default: old-provider\n",
        encoding="utf-8",
    )
    config = get_model_config(str(config_path), overrides={
        "model": "new-model",
        "provider": "new-provider",
    })
    assert config["model"] == "new-model"
    assert config["provider"] == "new-provider"


def test_governor_routes_implementation_to_coding_profile(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    decision = governor.select_worker_profile(
        {
            "task_id": "implementation-node",
            "goal": "实现工单服务的 RESTful API，并运行自动化测试",
            "required_capabilities": [
                "api_design", "backend_development", "test_writing",
            ],
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Coding"


def test_governor_routes_test_writing_to_testing_profile(tmp_path: Path):
    """Writing tests is a Testing capability, not a Coding-only boundary."""
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    decision = governor.select_worker_profile(
        {
            "task_id": "test-node",
            "goal": "执行 API 和端到端测试并生成机器可读测试报告",
            "required_capabilities": [
                "api_testing",
                "end_to_end_testing",
                "test_execution",
                "test_writing",
            ],
            "task_type": "verification",
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Testing"


def test_incompatible_llm_profile_cannot_keep_stale_preselection(tmp_path: Path):
    """An LLM mismatch must rematch or reject, never run the old profile."""
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))

    governor._llm_worker_match = lambda task, candidates: {
        "worker_type": "Architect",
        "confidence": 0.99,
        "reason_codes": [],
    }
    governor.llm_matching_enabled = True
    decision = governor.select_worker_profile(
        {
            "task_id": "frontend-node",
            "goal": "实现前端页面和 API 联调",
            "required_capabilities": ["frontend_development", "api_design"],
            "task_type": "implementation",
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Coding"
    assert "terminal_exec" in decision["selected"]["tools"]
    assert "file_write" in decision["selected"]["tools"]


def test_governor_rejects_false_parallelism_behind_one_wide_node(
    tmp_path: Path,
):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="T-001",
        goal="root",
        role="planner",
        status="running",
    ))
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    patch = TaskGraphPatch(
        reason_codes=["GOAL_DECOMPOSITION", "PARALLELIZABLE"],
        add_nodes=[
            {
                "node_id": "N-01",
                "goal": "define every shared contract",
                "required_capabilities": ["architecture", "database", "api"],
                "dependencies": ["T-001"],
                "expected_outputs": ["a.md", "b.md", "c.md"],
                "acceptance_criteria": ["a", "b", "c"],
            },
            {
                "node_id": "N-02",
                "goal": "database",
                "required_capabilities": ["database"],
                "dependencies": ["N-01"],
            },
            {
                "node_id": "N-03",
                "goal": "backend",
                "required_capabilities": ["backend"],
                "dependencies": ["N-02"],
            },
            {
                "node_id": "N-04",
                "goal": "frontend",
                "required_capabilities": ["frontend"],
                "dependencies": ["N-01"],
            },
        ],
    )

    with pytest.raises(ValueError, match="oversized initial bottleneck"):
        governor.apply_task_graph_patch(patch, parent_task_id="T-001")

    assert {node.node_id for node in graph.nodes.values()} == {"T-001"}


def test_governor_infers_implementation_when_patch_omits_capabilities(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    decision = governor.select_worker_profile(
        {
            "task_id": "patched-api-node",
            "goal": "构建问答 API 接口并返回来源引用",
            "role": "researcher",
            "required_capabilities": [],
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Coding"
    assert "GOAL_CAPABILITY_INFERRED" in decision["reason_codes"]
    assert "api_design" in decision["capability_inference"]["inferred_required_capabilities"]


def test_governor_goal_boundary_overrides_partial_retrieval_capability(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    decision = governor.select_worker_profile(
        {
            "task_id": "retrieval-module-node",
            "goal": "实现关键词检索模块并运行相关性排序测试",
            "required_capabilities": ["document_retrieval"],
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Coding"
    assert "implementation" in decision["required_capabilities"]
    assert "GOAL_CAPABILITY_INFERRED" in decision["reason_codes"]


def test_governor_does_not_cross_route_research_to_coding(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    decision = governor.select_worker_profile(
        {
            "task_id": "research-node",
            "goal": "分析未来技术趋势并形成研究综述",
            "required_capabilities": [
                "literature_synthesis", "trend_analysis", "research_design",
            ],
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Research"


def test_worker_profiles_only_advertise_registered_tools(tmp_path: Path):
    from AstralNexus.tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    registered = set(registry.get_all_tool_names())
    for profile in HETEROGENEOUS_WORKERS.values():
        assert set(profile.tools) <= registered, profile.worker_type


def test_compatibility_role_toolsets_only_advertise_registered_tools():
    from AstralNexus.core.role_registry import initialize_role_registry
    from AstralNexus.tools.registry import discover_builtin_tools, registry
    from AstralNexus.tools.toolsets import TOOLSETS, resolve_toolset

    discover_builtin_tools()
    initialize_role_registry()
    registered = set(registry.get_all_tool_names())
    for role_name in get_agent_registry().list_roles():
        role = get_agent_registry().get_role(role_name)
        assert set(role.default_tools) <= registered, role_name
    for toolset_name in TOOLSETS:
        assert set(resolve_toolset(toolset_name)) <= registered, toolset_name


def test_governor_patch_rejects_unknown_dependency(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[{
        "node_id": "root", "goal": "Root", "role": "planner", "status": "completed",
    }])
    governor = GlobalGovernor(graph, trace)
    with pytest.raises(ValueError, match="unknown dependency"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "child", "goal": "Child", "dependencies": ["missing"],
        }]), parent_task_id="root")
    assert graph.get_node("child") is None


def test_governor_patch_rejects_invalid_status_transition(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[{
        "node_id": "root", "goal": "Root", "role": "planner", "status": "completed",
    }])
    governor = GlobalGovernor(graph, trace)
    with pytest.raises(ValueError, match="invalid state transition"):
        governor.apply_task_graph_patch(TaskGraphPatch(update_nodes=[{
            "node_id": "root", "status": "running",
        }]))
    assert graph.get_node("root").status == "completed"


def test_governor_patch_allows_idempotent_status_update(tmp_path: Path):
    """Reasserting a state while changing repair metadata is not a transition."""
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[{
        "node_id": "repair",
        "goal": "Repair",
        "role": "tester",
        "status": "repair_pending",
    }])
    governor = GlobalGovernor(graph, trace)
    result = governor.apply_task_graph_patch(TaskGraphPatch(update_nodes=[{
        "node_id": "repair",
        "status": "repair_pending",
        "acceptance_criteria": ["rerun the integration test"],
    }]))
    assert result["updated"] == ["repair"]
    assert graph.get_node("repair").status == "repair_pending"


class GovernorClient:
    def chat(self, messages, **kwargs):
        prompt = messages[-1]["content"]
        if "提取一个通用" in prompt:
            return json.dumps({
                "delivery_mode": "mixed",
                "required_outputs": ["用户交付物"],
                "required_sections": [],
                "acceptance_criteria": ["直接回答任务"],
                "forbidden_claims": ["未验证结果"],
                "needs_artifact_refs": True,
            }, ensure_ascii=False)
        return json.dumps({
            "groups": [
                {"group_id": "G-a", "title": "输入分析", "node_ids": ["T-001", "T-002"]},
                {"group_id": "G-b", "title": "补充验证", "node_ids": ["T-003"]},
            ],
            "actions": [
                {"node_id": "T-001", "action": "retain"},
                {"node_id": "T-002", "action": "compress"},
                {
                    "node_id": "T-003",
                    "action": "defer",
                    "wake_conditions": ["验证阶段发现证据冲突时"],
                },
            ],
            "reasoning": "仅将当前必要证据放入工作集",
        }, ensure_ascii=False)


def test_governor_owns_generic_contract_and_working_set(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)
    client = GovernorClient()
    contract = governor.derive_delivery_contract("完成一个不限定领域的用户任务", client)
    decision = governor.plan_context_governance(
        "完成一个不限定领域的用户任务",
        [
            {"node_id": "T-001", "role": "a", "goal": "a", "conclusion": "a"},
            {"node_id": "T-002", "role": "b", "goal": "b", "conclusion": "b"},
            {"node_id": "T-003", "role": "c", "goal": "c", "conclusion": "c"},
        ],
        client,
    )

    assert contract["delivery_mode"] == "mixed"
    assert contract["required_sections"] == []
    assert decision.retained_packet_ids == ["T-001"]
    assert decision.compressed_packet_ids == ["T-002"]
    assert decision.deferred_packet_ids == ["T-003"]
    assert decision.groups[0]["node_ids"] == ["T-001", "T-002"]
    assert all("T-003" not in group["node_ids"] for group in decision.groups)
    assert any(event["event_type"] == "governor.context_governance.decided" for event in trace.events)


def test_governor_keeps_optional_external_evidence_out_of_replan_gate(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)
    contract = {
        "required_outputs": [{"name": "交付文档", "required": True}],
        "external_evidence_policy": "optional_when_unavailable",
    }
    report = governor.verify_evidence_summaries(
        contract,
        {
            "g": {
                "group_id": "g",
                "source_nodes": ["T-1"],
                "verified_claims": [{"claim": "已分析输入", "evidence_refs": ["T-1"]}],
                "conflicts": [],
                "coverage_gaps": [{
                    "gap": "外部数据库不可访问，专利来源待检索",
                    "evidence_needed": "外部专利数据库结果",
                    "required_for_delivery": True,
                    "blocking_stage": "evidence",
                }],
                "constraints": [], "recommended_followups": [], "source_artifacts": [],
            }
        },
    )
    assert report["overall_status"] == "pass"
    assert report["coverage_gaps"] == []
    assert report["deferred_gaps"][0]["blocking_stage"] == "delivery"


def test_governor_graph_patch_is_incremental_and_capability_based(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)

    class PlannerClient:
        def chat(self, messages, **kwargs):
            return json.dumps({
                "add_nodes": [{
                    "goal": "验证接口契约",
                    "task_type": "verification",
                    "required_capabilities": ["api_validation", "test_execution"],
                    "dependencies": [],
                    "acceptance_criteria": ["输出可复现测试证据"],
                    "priority": 80,
                    "criticality": "high",
                    "execution_target": "edge",
                }],
                "update_nodes": [], "cancel_nodes": [], "add_edges": [], "remove_edges": [],
                "reason_codes": ["VERIFICATION_GAP"],
            }, ensure_ascii=False)

    patch = governor.plan_task_graph_patch("交付一个可验证的接口改动", PlannerClient())
    assert len(patch.add_nodes) == 1
    assert patch.add_nodes[0]["required_capabilities"] == ["api_validation", "test_execution"]
    assert patch.reason_codes == ["VERIFICATION_GAP"]


def test_governor_applies_complete_patch_atomically(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[
        {"node_id": "root", "goal": "Root", "role": "planner", "status": "completed"},
        {"node_id": "old", "goal": "Old branch", "role": "coder", "status": "ready"},
        {"node_id": "keep", "goal": "Keep branch", "role": "researcher", "status": "admitted"},
    ], edges=[
        {"source": "root", "target": "old", "edge_type": "depends_on"},
        {"source": "root", "target": "keep", "edge_type": "depends_on"},
    ])
    governor = GlobalGovernor(graph, trace)
    patch = TaskGraphPatch(
        add_nodes=[
            {"node_id": "replacement", "goal": "Replacement branch", "required_capabilities": ["coding"]},
            {"node_id": "duplicate", "goal": "Replacement branch", "required_capabilities": ["coding"]},
        ],
        update_nodes=[{
            "node_id": "keep", "goal": "Updated branch", "role": "verifier",
            "status": "ready", "dependencies": ["replacement"],
        }],
        cancel_nodes=["old"],
        add_edges=[{"source": "root", "target": "replacement", "edge_type": "depends_on"}],
        remove_edges=[{"source": "root", "target": "keep", "edge_type": "depends_on"}],
        reason_codes=["VERIFICATION_GAP", "REDUNDANT_TASK"],
        reasoning="Replace only the affected branch",
    )

    result = governor.apply_task_graph_patch(patch)

    assert result["added"] == ["replacement"]
    assert result["cancelled"] == ["old"]
    assert result["reason_codes"] == ["VERIFICATION_GAP", "REDUNDANT_TASK"]
    assert result["reasoning"] == "Replace only the affected branch"
    assert result["node_count_before"] == 3
    assert result["node_count_after"] == 4
    assert result["node_growth"] == 1
    assert result["edge_count_before"] == 2
    assert result["edge_count_after"] == 2
    assert result["edge_growth"] == 0
    assert graph.get_node("old").status == "cancelled"
    assert graph.get_node("old").updated_at is not None
    assert graph.get_node("keep").goal == "Updated branch"
    assert graph.get_node("keep").role == "verifier"
    assert graph.get_node("keep").status == "ready"
    assert graph.get_node("keep").dependencies == ["replacement"]
    assert graph.get_node("duplicate") is None
    edge_keys = {(edge.source, edge.target, edge.edge_type) for edge in graph.edges}
    assert ("root", "keep", "depends_on") not in edge_keys
    assert ("replacement", "keep", "depends_on") in edge_keys
    assert ("root", "replacement", "depends_on") in edge_keys


def test_governor_rejects_cyclic_patch_without_partial_mutation(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[
        {"node_id": "a", "goal": "A", "role": "worker"},
        {"node_id": "b", "goal": "B", "role": "worker"},
    ], edges=[{"source": "a", "target": "b", "edge_type": "depends_on"}])
    governor = GlobalGovernor(graph, trace)

    with pytest.raises(ValueError, match="cycle"):
        governor.apply_task_graph_patch(TaskGraphPatch(
            add_nodes=[{"node_id": "new", "goal": "Must not be committed"}],
            add_edges=[{"source": "b", "target": "a", "edge_type": "depends_on"}],
            reason_codes=["EVIDENCE_CONFLICT"],
        ))

    assert graph.get_node("new") is None
    assert [(edge.source, edge.target) for edge in graph.edges] == [("a", "b")]


def test_governor_rejects_invalid_added_node_status_atomically(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph()
    governor = GlobalGovernor(graph, trace)

    with pytest.raises(ValueError, match="Unknown task lifecycle status"):
        governor.apply_task_graph_patch(TaskGraphPatch(
            add_nodes=[{"node_id": "bad", "goal": "Invalid state", "status": "not_a_state"}],
            reason_codes=["INVALID_PLAN"],
        ))

    assert graph.get_node("bad") is None


def test_governor_cancellation_uses_state_machine(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[
        {"node_id": "done", "goal": "Completed", "role": "worker", "status": "completed"},
    ])
    governor = GlobalGovernor(graph, trace)

    result = governor.apply_task_graph_patch(TaskGraphPatch(cancel_nodes=["done"]))

    assert result["cancelled"] == []
    assert graph.get_node("done").status == "completed"
    assert graph.get_node("done").updated_at is None


def test_governor_selects_worker_by_capability_and_resource(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)
    decision = governor.select_worker_profile(
        {"task_id": "T-1", "required_capabilities": ["evidence_check", "validation"], "execution_target": "edge"},
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["profile_id"] == "Verifier"
    assert decision["selected"]["worker_type"] == "Verifier"
    assert decision["selected"]["selected_model"] == "qwen3.7-max"
    assert "CAPABILITY_MATCH" in decision["reason_codes"]


def test_governor_selects_research_worker_for_design_and_evaluation_capabilities(tmp_path: Path):
    """Research planning vocabulary must resolve to a real heterogeneous worker."""
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)
    decision = governor.select_worker_profile(
        {
            "task_id": "research-design",
            "required_capabilities": [
                "ai_research_direction_selection",
                "feasibility_analysis",
                "research_design",
                "experimental_methodology",
                "evaluation_metrics_design",
            ],
            "execution_target": "cloud",
        },
        HETEROGENEOUS_WORKERS,
    )

    assert decision["selected"]["worker_type"] == "Research"
    assert decision["selected"]["capability_match"] > 0
    assert "ZERO_CAPABILITY_MATCH_REJECTED" not in decision["reason_codes"]


def test_governor_semantically_matches_unseen_planner_capabilities(tmp_path: Path):
    """Unknown but compositional LLM capability names must not cause a false gap."""
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)
    decision = governor.select_worker_profile(
        {
            "task_id": "semantic-research",
            "required_capabilities": [
                "technical_analysis",
                "idea_generation",
                "feasibility_assessment",
            ],
            "execution_target": "cloud",
        },
        HETEROGENEOUS_WORKERS,
    )

    assert decision["selected"] is not None
    assert decision["selected"]["worker_type"] in {"Research", "Reasoning"}
    assert decision["selected"]["capability_match"] > 0


def test_governor_llm_matcher_is_registry_bounded(tmp_path: Path):
    """The LLM may choose a profile, but cannot invent a Worker type."""
    trace = TraceRecorder(tmp_path)

    class Matcher:
        def chat(self, messages):
            return '{"worker_type":"Coding","capability_extensions":["static_analysis","invented_skill"],"confidence":0.9}'

    governor = GlobalGovernor(TaskGraph(), trace, llm_client=Matcher())
    decision = governor.select_worker_profile(
        {"task_id": "llm-match", "required_capabilities": ["code_generation"]},
        HETEROGENEOUS_WORKERS,
    )

    assert decision["selected"]["worker_type"] == "Coding"
    assert "capability_extensions" not in decision["selected"]
    assert "extension_tools" not in decision["selected"]


def test_governor_llm_matcher_rejects_unknown_profile(tmp_path: Path):
    trace = TraceRecorder(tmp_path)

    class Matcher:
        def chat(self, messages):
            return '{"worker_type":"InventedWorker","confidence":1.0}'

    governor = GlobalGovernor(TaskGraph(), trace, llm_client=Matcher())
    with pytest.raises(RuntimeError, match="real LLM Worker Matcher failed"):
        governor.select_worker_profile(
            {"task_id": "llm-invalid", "required_capabilities": ["research_design"]},
            HETEROGENEOUS_WORKERS,
        )


def test_governor_matcher_cannot_override_positive_capability_match(tmp_path: Path):
    trace = TraceRecorder(tmp_path)

    class WrongMatcher:
        def chat(self, messages):
            return '{"worker_type":"Vision","confidence":1.0}'

    governor = GlobalGovernor(TaskGraph(), trace, llm_client=WrongMatcher())
    with pytest.raises(RuntimeError, match="real LLM Worker Matcher failed"):
        governor.select_worker_profile(
            {"task_id": "bounded-match", "required_capabilities": ["research_design"]},
            HETEROGENEOUS_WORKERS,
        )


def test_governor_llm_matcher_explicit_no_match_does_not_fallback(tmp_path: Path):
    trace = TraceRecorder(tmp_path)

    class NoMatch:
        def chat(self, messages):
            return '{"worker_type":null,"confidence":0.9,"reason_codes":["CAPABILITY_GAP"]}'

    governor = GlobalGovernor(TaskGraph(), trace, llm_client=NoMatch())
    decision = governor.select_worker_profile(
        {"task_id": "llm-no-match", "required_capabilities": ["research_design"]},
        HETEROGENEOUS_WORKERS,
    )

    assert decision["selected"] is None
    assert "NO_CAPABILITY_CANDIDATE" in decision["reason_codes"]


def test_governor_semantic_matcher_handles_domain_capability_gap(tmp_path: Path):
    """Unknown domain vocabulary is delegated to the real Matcher before replanning."""
    trace = TraceRecorder(tmp_path)

    class Matcher:
        def chat(self, messages):
            return '{"worker_type":"Retrieval","confidence":0.92,"reason_codes":["SEMANTIC_PROFILE_MATCH"]}'

    governor = GlobalGovernor(TaskGraph(), trace, llm_client=Matcher())
    decision = governor.select_worker_profile(
        {
            "task_id": "domain-gap",
            "goal": "检索遥感图像压缩论文、专利和数据集并核验来源",
            "required_capabilities": ["compression_algorithm_survey", "vision_task_performance_benchmarking"],
            "execution_target": "cloud",
        },
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"]["worker_type"] == "Retrieval"
    assert decision["semantic_match"] is True
    assert "ZERO_CAPABILITY_MATCH_REJECTED" not in decision["reason_codes"]


def test_governor_patch_preserves_patch_local_node_ids(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(nodes=[{"node_id": "root", "goal": "Root", "role": "planner", "status": "completed"}]), trace)

    class Planner:
        def chat(self, messages):
            return json.dumps({
                "add_nodes": [
                    {"node_id": "patch-a", "goal": "A", "required_capabilities": ["research"]},
                    {"node_id": "patch-b", "goal": "B", "required_capabilities": ["research"], "dependencies": ["patch-a"]},
                ],
                "add_edges": [],
                "reason_codes": ["VERIFICATION_GAP"],
                "planning_status": "OK",
            })

    patch = governor.plan_task_graph_patch("fill gap", Planner())
    result = governor.apply_task_graph_patch(patch, parent_task_id="root")
    assert len(result["added"]) == 2
    second = governor.task_graph.get_node(result["added"][1])
    first = governor.task_graph.get_node(result["added"][0])
    assert first is not None and second is not None
    assert second.dependencies == [first.node_id]


def test_governor_repairs_soft_cycle_inside_new_patch_with_trace(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[{"node_id": "root", "goal": "Root", "role": "planner", "status": "completed"}])
    governor = GlobalGovernor(graph, trace)
    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[
        {"node_id": "patch-a", "goal": "A", "dependencies": ["patch-b"]},
        {"node_id": "patch-b", "goal": "B", "dependencies": ["patch-a"]},
    ]), parent_task_id="root")
    assert set(result["added"]) == {"patch-a", "patch-b"}
    dependency_edges = [
        edge for edge in graph.edges
        if edge.edge_type == "depends_on"
        and edge.source in {"patch-a", "patch-b"}
        and edge.target in {"patch-a", "patch-b"}
    ]
    assert len(dependency_edges) == 1
    assert any(
        event["event_type"] == "governor.graph_patch.repair_applied"
        for event in trace.events
    )


def test_governor_rejects_hard_cycle_without_mutation(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[
        {"node_id": "root", "goal": "Root", "role": "planner", "status": "completed"},
    ])
    governor = GlobalGovernor(graph, trace)
    with pytest.raises(ValueError, match="no repairable soft edge"):
        governor.apply_task_graph_patch(
            TaskGraphPatch(
                add_nodes=[
                    {"node_id": "patch-a", "goal": "A"},
                    {"node_id": "patch-b", "goal": "B"},
                ],
                add_edges=[
                    {
                        "source": "patch-a",
                        "target": "patch-b",
                        "edge_type": "depends_on",
                        "metadata": {"hard_dependency": True},
                    },
                    {
                        "source": "patch-b",
                        "target": "patch-a",
                        "edge_type": "depends_on",
                        "metadata": {"hard_dependency": True},
                    },
                ],
            ),
            parent_task_id="root",
        )
    assert set(graph.nodes.keys()) == {"root"}
    assert graph.edges == []


def test_governor_applies_dynamic_communication_topology_patch(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[
        {"node_id": "worker-a", "goal": "A", "role": "researcher"},
        {"node_id": "worker-b", "goal": "B", "role": "verifier"},
    ])
    governor = GlobalGovernor(graph, trace)
    result = governor.apply_task_graph_patch(
        TaskGraphPatch(
            add_edges=[{
                "source": "worker-a",
                "target": "worker-b",
                "edge_type": "communicates_to",
                "metadata": {"semantic_relevance": 0.91},
            }],
            reason_codes=["EVIDENCE_ROUTE"],
            reasoning="route evidence only to the verifier",
        )
    )
    assert result["communication_links_added"][0]["edge_type"] == "communicates_to"
    assert any(
        edge.edge_type == "communicates_to"
        and edge.source == "worker-a"
        and edge.target == "worker-b"
        for edge in graph.edges
    )
    assert any(
        event["event_type"] == "governor.topology_patch.applied"
        for event in trace.events
    )


def test_governor_json_parser_extracts_first_complete_value():
    value = _parse_governor_json('prefix {"mode":"direct_chat"} suffix {"mode":"governed_run"}')
    assert value == {"mode": "direct_chat"}


def test_governor_event_type_members_are_unique():
    names = [member.name for member in EventType]
    assert len(names) == len(set(names))


def test_governor_patch_transaction_rolls_back_commit_failure(tmp_path: Path, monkeypatch):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph(nodes=[
        {"node_id": "root", "goal": "Root", "role": "planner", "status": "completed"},
    ])
    governor = GlobalGovernor(graph, trace)

    def fail_add_edge(*args, **kwargs):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(graph, "add_edge", fail_add_edge)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        governor.apply_task_graph_patch(
            TaskGraphPatch(add_nodes=[
                {"node_id": "child", "goal": "Child", "dependencies": ["root"]},
            ]),
            parent_task_id="root",
        )

    assert set(graph.nodes.keys()) == {"root"}
    assert graph.edges == []
    assert any(
        event["event_type"] == "governor.graph_patch.failed"
        and event["payload"].get("reason") == "transaction_rolled_back"
        for event in trace.events
    )


def test_orchestrator_instantiates_governor_selected_profile(tmp_path: Path, monkeypatch):
    orchestrator = Orchestrator(tmp_path)
    node = orchestrator.task_graph.add_node({
        "node_id": "T-profile",
        "goal": "Verify evidence",
        "role": "coder",
        "metadata": {
            "worker_profile": {
                "worker_type": "Verifier",
                "tools": ["evidence_checker", "acceptance_runner"],
                "model_preference": "medium_balanced",
                "selected_model": "qwen3.7-plus",
                "resource_tier": "edge",
                "input_contract": "artifact + acceptance criteria",
                "output_contract": "verified evidence",
                "capabilities": ["validation"],
            }
        },
    })
    node.metadata["implementation_role"] = "verifier"
    requested_roles = []
    real_create_agent = orchestrator.agent_registry.create_agent

    def capture_create_agent(role_name, **kwargs):
        requested_roles.append(role_name)
        return real_create_agent(role_name, **kwargs)

    monkeypatch.setattr(orchestrator.agent_registry, "create_agent", capture_create_agent)
    agent = orchestrator._instantiate_governed_worker(node, "coder")

    assert requested_roles[0] == "verifier"
    assert agent.role == "verifier"
    assert agent.default_tools == ["evidence_checker", "acceptance_runner"]
    assert agent.selected_model == "qwen3.7-plus"
    assert agent.input_contract == "artifact + acceptance criteria"
    assert agent.output_contract == "verified evidence"


def test_delivery_gate_produces_global_verification_report(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    governor = GlobalGovernor(TaskGraph(), trace)
    report = governor.run_delivery_gate(
        {
            "required_sections": ["结果"],
            "acceptance_criteria": [],
            "forbidden_claims": ["未验证"],
            "needs_artifact_refs": False,
            "task_version": 1,
        },
        content="# 结果\n已完成。",
    )
    assert report["overall_status"] == "pass"
    assert report["delivery_authorized"] is True
    assert any(event["event_type"] == "governor.delivery_gate" for event in trace.events)


def _reducer_with_gap(required_for_delivery: bool) -> dict:
    return {
        "group_id": "G-1",
        "source_nodes": ["T-1"],
        "verified_claims": [{
            "claim": "evidence-backed claim",
            "evidence_refs": ["node_io/T-1_output.json"],
            "confidence": 0.9,
            "epistemic_status": "derived",
        }],
        "conflicts": [],
        "coverage_gaps": [{
            "gap": "optional follow-up evidence",
            "severity": "minor",
            "required_for_delivery": required_for_delivery,
            "evidence_needed": "additional benchmark",
            "recommended_action": "defer",
        }],
        "constraints": [],
        "recommended_followups": [],
        "source_artifacts": ["node_io/T-1_output.json"],
    }


def test_global_verifier_keeps_optional_gap_without_blocking(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    report = governor.verify_evidence_summaries({}, {"G-1": _reducer_with_gap(False)})
    assert report["overall_status"] == "pass"
    assert report["coverage_gaps"] == []
    assert len(report["optional_gaps"]) == 1


def test_global_verifier_blocks_required_gap(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    report = governor.verify_evidence_summaries({}, {"G-1": _reducer_with_gap(True)})
    assert report["overall_status"] == "fail"
    assert len(report["coverage_gaps"]) == 1


def test_global_verifier_defers_required_synthesis_gap(tmp_path: Path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    summary = _reducer_with_gap(True)
    summary["coverage_gaps"][0]["blocking_stage"] = "delivery"
    report = governor.verify_evidence_summaries({}, {"G-1": summary})
    assert report["overall_status"] == "pass"
    assert report["coverage_gaps"] == []
    assert report["deferred_gaps"][0]["blocking_stage"] == "delivery"


def test_requirement_change_is_versioned_and_planned_by_governor(tmp_path: Path):
    trace = TraceRecorder(tmp_path)
    graph = TaskGraph()
    graph.add_node({"node_id": "T-001", "goal": "实现设备管理系统", "role": "planner"})

    class ChangeClient:
        def chat(self, messages, **kwargs):
            return json.dumps({
                "change_kind": "requirement",
                "impact_summary": "增加租户隔离与权限测试",
                "affected_node_ids": ["T-001"],
                "new_constraints": ["租户数据必须严格隔离"],
                "worker_takeover": {"lost_node_ids": [], "required_capabilities": []},
                "context_policy": {"broadcast_policy": "structured_local_only"},
                "patch": {
                    "add_nodes": [{
                        "node_id": "N-TENANT-VERIFY",
                        "goal": "验证多租户数据隔离和权限模型",
                        "role": "verifier",
                        "dependencies": ["T-001"],
                        "acceptance_criteria": ["输出可复现测试证据"],
                    }],
                    "reason_codes": ["REQUIREMENT_DELTA", "SECURITY_IMPACT"],
                },
                "reverification": ["多租户隔离测试", "权限回归测试"],
            }, ensure_ascii=False)

    governor = GlobalGovernor(graph, trace)
    governor.enable_llm_worker_matching(ChangeClient())
    decision = governor.plan_requirement_change(
        "新增需求：系统必须支持多租户模式，不同租户数据严格隔离",
        current_goal="实现设备管理系统",
    )
    assert decision["affected_node_ids"] == ["T-001"]
    assert decision["patch"].add_nodes[0]["node_id"] == "N-TENANT-VERIFY"
    assert any(event["event_type"] == "governor.user_requirement_changed" for event in trace.events)
