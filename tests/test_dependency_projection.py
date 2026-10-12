"""第 3 步：替换依赖校验（b33 case1 fix10）。

现场形状：``deliverables/verify_case.py`` 被 7 个节点依次改写
（P-104 → P-109 → P-110 → P-112 → P-115 → P-116 → P-117），而
``_rewire_superseded_dependents`` 把**每一个**在世下游都改写到谱系末端——盲目替换。

业主定的口径：

* 消费者若**需要已完成作者的有效产物** ⇒ 可继续引用其版本（保留有效证据）；
* 消费者若**等待修复后的版本** ⇒ 才依赖修复者；
* 补丁提交前对**整张投影图**检查生产者、版本要求与环路。
"""
from __future__ import annotations


def _orchestrator(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskGraph
    from AstralNexus.runtime.trace import TraceRecorder

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = TaskGraph()
    orchestrator.run_dir = tmp_path
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    from AstralNexus.core.governor import GlobalGovernor
    orchestrator.governor = GlobalGovernor(orchestrator.task_graph, orchestrator.trace_recorder)
    return orchestrator


def _node(graph, node_id, *, status, deps=(), inputs=(), outputs=(), recovery_of=""):
    from AstralNexus.core.task_graph import TaskNode

    node = graph.add_node(TaskNode(
        node_id=node_id, goal="g", role="coder", status=status,
        dependencies=list(deps), input_artifacts=list(inputs),
        output_artifacts=list(outputs),
    ))
    if recovery_of:
        node.metadata["recovery_of"] = recovery_of
    return node


def _accept(graph, logical_id, node_id, path):
    from AstralNexus.core.task_graph import Artifact, ArtifactAcceptanceStatus

    graph.add_artifact(Artifact(
        artifact_id="A-%s-001" % node_id, node_id=node_id, artifact_type="file",
        path=path, description="accepted", acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
    ))
    graph.add_artifact(Artifact(
        artifact_id=logical_id, node_id=node_id, artifact_type="contract",
        path=path, description="logical", acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
    ))


def _events(orchestrator, event_type):
    return [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == event_type
    ]


# ---------------------------------------------- (1) 需要有效产物 ⇒ 保留引用


def test_consumer_holding_a_valid_version_is_not_rewired(tmp_path):
    """消费者需要的产物已有**已接受**版本 ⇒ 保留引用、不重接、证据不被撤销。"""
    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    was = _node(graph, "P-104", status="superseded", outputs=["artifact:verify-case-script"])
    repairer = _node(graph, "P-111", status="ready", recovery_of="P-104",
                     outputs=["artifact:dual-run-evidence"])
    consumer = _node(graph, "C-1", status="ready", deps=["P-104"],
                     inputs=["artifact:verify-case-script"])
    _accept(graph, "artifact:verify-case-script", "P-104", "deliverables/verify_case.py")
    graph.add_edge("P-104", "C-1", "depends_on")

    rewired = orchestrator._rewire_superseded_dependents()

    assert rewired == 0
    assert consumer.dependencies == ["P-104"]          # 引用保留
    kept = _events(orchestrator, "orchestrator.dependency.rewire_kept")
    assert kept and kept[-1]["dependent"] == "C-1"
    assert kept[-1]["reason"] == "consumer_inputs_satisfied_by_accepted_artifact"
    # 有效证据不被无故撤销
    assert getattr(graph.artifacts["artifact:verify-case-script"].acceptance_status,
                   "value", "") == "accepted"
    assert graph.artifacts["artifact:verify-case-script"].node_id == "P-104"
    assert was.status == "superseded"


# ------------------------------------------- (2) 等待修复后版本 ⇒ 重接到修复者


def test_consumer_waiting_for_the_repaired_version_is_rewired(tmp_path):
    """修复者**声明产出**该消费者的输入 ⇒ 消费者等待修复后版本 ⇒ 重接。"""
    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "P-104", status="superseded", outputs=["artifact:verify-case-script"])
    _node(graph, "P-111", status="ready", recovery_of="P-104",
          outputs=["artifact:verify-case-script"])
    consumer = _node(graph, "C-2", status="ready", deps=["P-104"],
                     inputs=["artifact:verify-case-script"])
    _accept(graph, "artifact:verify-case-script", "P-104", "deliverables/verify_case.py")
    graph.add_edge("P-104", "C-2", "depends_on")

    rewired = orchestrator._rewire_superseded_dependents()

    assert rewired == 1
    assert consumer.dependencies == ["P-111"]                      # 改指修复者
    assert _events(orchestrator, "orchestrator.dependency.rewired")
    assert not _events(orchestrator, "orchestrator.dependency.rewire_kept")
    # 替换后消费者仍可被调度：依赖指向在世的生产者。
    assert graph.get_node(consumer.dependencies[0]).status == "ready"


def test_consumer_without_an_accepted_version_is_rewired(tmp_path):
    """没有可依赖的已接受旧版本 ⇒ 必须重接（否则是悬空消费者）。"""
    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "P-104", status="superseded", outputs=["artifact:verify-case-script"])
    _node(graph, "P-111", status="ready", recovery_of="P-104",
          outputs=["artifact:dual-run-evidence", "artifact:verify-case-script"])
    consumer = _node(graph, "C-3", status="ready", deps=["P-104"],
                     inputs=["artifact:verify-case-script"])
    graph.add_edge("P-104", "C-3", "depends_on")

    rewired = orchestrator._rewire_superseded_dependents()

    assert rewired == 1
    assert consumer.dependencies == ["P-111"]


# ------------------------------------------------- (3) 提交前的投影图校验


def _projection_kwargs(graph):
    return {
        "nodes": graph.nodes,
        "artifacts": graph.artifacts,
    }


def test_projection_reports_a_dangling_producer(tmp_path):
    from AstralNexus.core.dependency_projection import (
        ISSUE_DANGLING_PRODUCER, projected_issues,
    )

    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "C-9", status="ready", deps=["ghost"])

    issues = projected_issues(**_projection_kwargs(graph))

    assert any(item["issue"] == ISSUE_DANGLING_PRODUCER
               and item["producer"] == "ghost" for item in issues)


def test_projection_reports_a_cycle(tmp_path):
    from AstralNexus.core.dependency_projection import ISSUE_CYCLE, projected_issues

    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "A", status="ready", deps=["B"])
    _node(graph, "B", status="ready", deps=["A"])

    issues = projected_issues(**_projection_kwargs(graph))

    # Cycle responsibility belongs to GraphPatchValidator, not this projection.
    assert not any(item["issue"] == ISSUE_CYCLE for item in issues)
    from AstralNexus.core.graph_patch_engine import GraphPatchValidator
    edges = {("A", "B", "depends_on"): {}, ("B", "A", "depends_on"): {}}
    assert GraphPatchValidator.find_dependency_cycle({"A", "B"}, edges)


def test_projection_reports_an_unsatisfied_version_requirement(tmp_path):
    from AstralNexus.core.dependency_projection import (
        ISSUE_UNSATISFIED_VERSION, projected_issues,
    )

    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "P-104", status="superseded", outputs=["artifact:verify-case-script"])
    _node(graph, "C-4", status="ready", deps=["P-104"],
          inputs=["artifact:verify-case-script"])

    issues = projected_issues(**_projection_kwargs(graph))

    assert any(item["issue"] == ISSUE_UNSATISFIED_VERSION
               and item["consumer"] == "C-4" for item in issues)


def test_projection_is_clean_when_the_version_is_available(tmp_path):
    from AstralNexus.core.dependency_projection import projected_issues

    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "P-104", status="superseded", outputs=["artifact:verify-case-script"])
    _node(graph, "C-5", status="ready", deps=["P-104"],
          inputs=["artifact:verify-case-script"])
    _accept(graph, "artifact:verify-case-script", "P-104", "deliverables/verify_case.py")

    assert projected_issues(**_projection_kwargs(graph)) == []


def test_projection_sees_nodes_introduced_by_the_patch(tmp_path):
    from AstralNexus.core.dependency_projection import (
        ISSUE_DANGLING_PRODUCER, projected_issues,
    )

    orchestrator = _orchestrator(tmp_path)
    graph = orchestrator.task_graph
    _node(graph, "C-6", status="ready", deps=["P-NEW"])

    without = projected_issues(**_projection_kwargs(graph))
    with_patch = projected_issues(
        patch_nodes=[{"node_id": "P-NEW", "dependencies": []}],
        **_projection_kwargs(graph),
    )

    assert any(item["issue"] == ISSUE_DANGLING_PRODUCER for item in without)
    assert not any(item["issue"] == ISSUE_DANGLING_PRODUCER for item in with_patch)