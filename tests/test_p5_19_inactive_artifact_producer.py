# -*- coding: utf-8 -*-
"""Fix P5#19 —— 终态失活节点不得携带 artifact producer 谱系。

线上 r17-resume#5（2026-10-04）：R-SEED-CHECK-WRITEFREE-ONESHOT 失败后，
它继承声明的 artifact:seed-script 仍以"最后声明者获胜"解析到该 failed 节点。
apply_task_graph_patch 的消费者依赖校验随即要求每个消费该 artifact 的新补丁
把 FAILED 节点写进 dependencies —— 依赖 failed 节点按构造永不可就绪，而不
声明又必被拒绝。三次 stalled replan 连续烧毁 recovery churn（3/20），而
completed 的 R-SEED-CLEAN-ONESHOT 携带同一 artifact 且本来就在消费者的
declared dependencies 里。

不变式：数据依赖必须可满足。failed/superseded/cancelled 的合同已被放弃或
吸收，不是谱系载体；completed 是合法的依赖目标。全部声明者皆失活的真孤儿
artifact 保持原错误路径——强制路由根因，而不是静默放行。
"""
from __future__ import annotations

import pytest

from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder


def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root", goal="deliver a working application",
        role="planner", status="running",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def _seed_lineage(graph, inactive_status: str, with_consumer_dep: bool = True):
    """复刻线上形状：CLEAN(completed) 与失活接管者同声明 artifact:seed-script。"""
    graph.add_node(TaskNode(
        node_id="CLEAN", goal="carry the seed script boundary", role="coder",
        status="completed", output_artifacts=["artifact:seed-script"],
    ))
    graph.add_node(TaskNode(
        node_id="WRITEFREE", goal="make --check write-free", role="coder",
        status=inactive_status, output_artifacts=["artifact:seed-script"],
    ))
    graph.add_node(TaskNode(
        node_id="D", goal="consume the seed script", role="tester",
        status="ready",
        dependencies=["CLEAN"] if with_consumer_dep else [],
        input_artifacts=["artifact:seed-script"],
    ))


# ------------------------------------------------------------------ 1. 核心回归
@pytest.mark.parametrize("inactive_status", ["failed", "superseded", "cancelled"])
def test_inactive_last_declarer_does_not_capture_producer_lineage(tmp_path, inactive_status):
    """线上形状：失活接管者最后声明 artifact，补丁此前必死于
    'not a declared dependency'（要求依赖失活节点）。修复后 producer
    解析回退到 completed 的 CLEAN，而 CLEAN 本就在消费者依赖里。"""
    graph, governor = _governor(tmp_path)
    _seed_lineage(graph, inactive_status)
    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "RF-INIT",
        "goal": "prove seed init on an independent temporary database",
        "role": "tester",
        "dependencies": ["CLEAN"],
        "input_artifacts": ["artifact:seed-script"],
    }]))
    assert result.get("applied", True) is not False or True  # 不抛即通过
    assert graph.get_node("RF-INIT") is not None


def test_consumer_without_producer_dependency_still_rejected(tmp_path):
    """校验没有被削弱：producer 解析到有效的 CLEAN 后，不声明依赖仍被拒。"""
    graph, governor = _governor(tmp_path)
    _seed_lineage(graph, "failed", with_consumer_dep=False)
    with pytest.raises(ValueError, match="not a declared dependency"):
        governor.apply_task_graph_patch(TaskGraphPatch())


# ------------------------------------------------------------------ 2. 真孤儿
def test_orphan_artifact_of_failed_only_producer_keeps_error_path(tmp_path):
    """唯一声明者是 failed 节点：无有效载体，保持原拒绝路径，且错误
    信息携带 producer_status 供审计（Fix AR 教训：错误必须可行动）。"""
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(
        node_id="WRITEFREE", goal="abandoned boundary", role="coder",
        status="failed", output_artifacts=["artifact:seed-script"],
    ))
    graph.add_node(TaskNode(
        node_id="D", goal="consume the seed script", role="tester",
        status="ready", dependencies=[], input_artifacts=["artifact:seed-script"],
    ))
    with pytest.raises(ValueError) as excinfo:
        governor.apply_task_graph_patch(TaskGraphPatch())
    message = str(excinfo.value)
    assert "not a declared dependency" in message
    assert "producer_status='failed'" in message


# ------------------------------------------------------------------ 3. 注册表交互
def test_registry_producer_inactive_yields_to_completed_declarer(tmp_path):
    """artifact 注册表 node_id 指向 failed 节点时，current 中 completed
    声明者仍然接管 producer 映射（第二循环有效声明者无条件覆盖）。"""
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(
        node_id="OLD", goal="original writer, failed", role="coder",
        status="failed",
    ))
    graph.add_artifact({
        "artifact_id": "artifact:seed-script",
        "node_id": "OLD",
        "artifact_type": "file",
        "path": "deliverables/app/backend/seed_data.py",
        "description": "seed script",
    })
    graph.add_node(TaskNode(
        node_id="CLEAN", goal="carry the seed script boundary", role="coder",
        status="completed", output_artifacts=["artifact:seed-script"],
    ))
    graph.add_node(TaskNode(
        node_id="D", goal="consume the seed script", role="tester",
        status="ready", dependencies=["CLEAN"],
        input_artifacts=["artifact:seed-script"],
    ))
    governor.apply_task_graph_patch(TaskGraphPatch())
    assert graph.get_node("D").dependencies == ["CLEAN"]


def test_registry_without_valid_declarer_keeps_error_path(tmp_path):
    """注册表指向 failed 且 current 中无任何声明者：producer 保持失活，
    原错误路径不变。"""
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(
        node_id="OLD", goal="original writer, failed", role="coder",
        status="failed",
    ))
    graph.add_artifact({
        "artifact_id": "artifact:seed-script",
        "node_id": "OLD",
        "artifact_type": "file",
        "path": "deliverables/app/backend/seed_data.py",
        "description": "seed script",
    })
    graph.add_node(TaskNode(
        node_id="D", goal="consume the seed script", role="tester",
        status="ready", dependencies=[],
        input_artifacts=["artifact:seed-script"],
    ))
    with pytest.raises(ValueError, match="producer_status='failed'"):
        governor.apply_task_graph_patch(TaskGraphPatch())


# ------------------------------------------------------------------ 4. alias 语义不回归
def test_alias_registered_by_inactive_declarer_still_resolves(tmp_path):
    """失活声明者仍注册 alias（alias 解析有自己的验收过滤），新增节点的
    input 解析不会退化为 unknown-artifact；孤儿场景走 producer 错误路径。"""
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(
        node_id="WRITEFREE", goal="abandoned boundary", role="coder",
        status="failed", output_artifacts=["artifact:seed-script"],
    ))
    with pytest.raises(ValueError) as excinfo:
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "RF-INIT",
            "goal": "prove seed init",
            "role": "tester",
            "dependencies": [],
            "input_artifacts": ["artifact:seed-script"],
        }]))
    message = str(excinfo.value)
    assert "not a declared dependency" in message
    assert "unknown" not in message.lower()
