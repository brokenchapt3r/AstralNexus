"""第 2 步：确定性修复归属准入（b33 case1 fix10 §REPLAY §6）。

现场依据：15 次被拒提案的理由同源——"根因文件属已完成的作者 ⇒ 必须新开节点拥有它"，
而每次接管都只产出一个不绑必需交付物的节点，于是接管链自我复制。
本文件锁住四条要求：

1. **已完成作者保留历史归属**，合法修复者通过**一次**确定性准入取得当前写权限；
2. **当前持有者还活着**时，另一个节点的重叠声明被明确拒绝，并返回**冲突路径与所有者**；
3. 非法补丁**不改变图**；
4. 已成立的证据（产物验收状态）**不被无故撤销**。
"""
from __future__ import annotations

import pytest


def _orchestrator(tmp_path):
    """最小 Orchestrator：图 + run 级账本（授权走 runtime_state_manager）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.runtime_state import RuntimeStateManager
    from AstralNexus.core.task_graph import TaskGraph, TaskNode

    graph = TaskGraph()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    orchestrator.run_dir = tmp_path
    orchestrator.runtime_state_manager = RuntimeStateManager(tmp_path, "ownership")
    return orchestrator, graph, TaskNode


def _patch_with_new_owner(node_id, *, recovery_of=""):
    from AstralNexus.core.governor import TaskGraphPatch

    raw = {
        "node_id": node_id,
        "goal": "repair the upstream root-cause file",
        "task_type": "implementation",
        "role": "coder",
        "dependencies": [],
        "owned_paths": [],
    }
    if recovery_of:
        raw["recovery_of"] = recovery_of
    patch = TaskGraphPatch(add_nodes=[raw])
    return patch


# ---------------------------------------------------------------- (1) 一次准入


def test_legitimate_repairer_is_admitted_once_from_a_completed_author(tmp_path):
    """P-104 已完成 ⇒ 合法修复者一次准入取得**当前写权限**，作者保留**历史归属**。"""
    from AstralNexus.core.task_graph import TaskNode

    orchestrator, graph, _ = _orchestrator(tmp_path)
    graph.add_node(TaskNode(
        node_id="P-104", goal="author the acceptance script", role="tester",
        status="completed", owned_paths=["deliverables/verify_case.py"],
    ))
    patch = _patch_with_new_owner("P-109")
    resolved = {"verify_case.py": ("P-104", ["deliverables/verify_case.py"])}

    granted = orchestrator._grant_completed_root_cause_paths(patch, resolved)

    assert granted == ["deliverables/verify_case.py"]
    assert patch.add_nodes[0]["owned_paths"] == ["deliverables/verify_case.py"]
    manager = orchestrator.runtime_state_manager
    # Planning is not authority. Only the accepted graph commit grants writes.
    assert manager.state.ownership_transfers == []
    from AstralNexus.core.governor import GlobalGovernor
    from AstralNexus.runtime.trace import TraceRecorder
    from AstralNexus.core.write_authority import committed_transfers
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    governor.apply_task_graph_patch(patch)
    assert governor.current_write_holder("deliverables/verify_case.py") == "P-109"
    transfers = committed_transfers(graph.nodes)
    assert len(transfers) == 1
    assert transfers[0]["from_node"] == "P-104"
    assert transfers[0]["to_node"] == "P-109"

    # 幂等：再授予一次不重复记账、不重复追加。
    again = orchestrator._grant_completed_root_cause_paths(patch, resolved)
    assert again == []
    assert len(committed_transfers(graph.nodes)) == 1


def test_completed_author_keeps_its_historical_ownership_record(tmp_path):
    """作者的历史归属（owned_paths）与已成立的证据都不被准入改写。"""
    from AstralNexus.core.task_graph import Artifact, ArtifactAcceptanceStatus, TaskNode

    orchestrator, graph, _ = _orchestrator(tmp_path)
    author = graph.add_node(TaskNode(
        node_id="P-104", goal="author the acceptance script", role="tester",
        status="completed", owned_paths=["deliverables/verify_case.py"],
    ))
    graph.add_artifact(Artifact(
        artifact_id="A-P-104-011", node_id="P-104", artifact_type="file",
        path="deliverables/verify_case.py", description="accepted script",
        acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
    ))

    orchestrator._grant_completed_root_cause_paths(
        _patch_with_new_owner("P-109"),
        {"verify_case.py": ("P-104", ["deliverables/verify_case.py"])},
    )

    assert author.owned_paths == ["deliverables/verify_case.py"]      # 历史归属保留
    accepted = graph.artifacts["A-P-104-011"]
    assert accepted.acceptance_status == ArtifactAcceptanceStatus.ACCEPTED  # 证据不被撤销
    assert accepted.node_id == "P-104"


def test_transfer_needs_a_claimant_and_is_ignored_without_one(tmp_path):
    """没有新增节点就没有接管者：不记账、不改动。"""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.task_graph import TaskNode

    orchestrator, graph, _ = _orchestrator(tmp_path)
    graph.add_node(TaskNode(
        node_id="P-104", goal="g", role="tester", status="completed",
        owned_paths=["deliverables/verify_case.py"],
    ))

    granted = orchestrator._grant_completed_root_cause_paths(
        TaskGraphPatch(add_nodes=[]),
        {"verify_case.py": ("P-104", ["deliverables/verify_case.py"])},
    )

    assert granted == []
    assert orchestrator.runtime_state_manager.state.ownership_transfers == []


def test_only_completed_authors_are_transferred(tmp_path):
    """活跃作者的路径不能被"授予"——那是重叠冲突，不是准入。"""
    from AstralNexus.core.task_graph import TaskNode

    orchestrator, graph, _ = _orchestrator(tmp_path)
    graph.add_node(TaskNode(
        node_id="P-123", goal="g", role="tester", status="running",
        owned_paths=["deliverables/verify_case.py"],
    ))

    granted = orchestrator._grant_completed_root_cause_paths(
        _patch_with_new_owner("P-124"),
        {"verify_case.py": ("P-123", ["deliverables/verify_case.py"])},
    )

    assert granted == []
    assert orchestrator.runtime_state_manager.state.ownership_transfers == []


# ------------------------------------------- (2)(3) 活跃持有者 ⇒ 明确拒绝且不改图


def _governor(tmp_path):
    from AstralNexus.core.governor import GlobalGovernor
    from AstralNexus.core.task_graph import TaskGraph, TaskNode
    from AstralNexus.runtime.trace import TraceRecorder

    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="root", goal="deliver", role="planner",
                            status="running"))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def test_second_writer_is_rejected_with_holder_and_conflicting_paths(tmp_path):
    """P-123 正持有写权限时，P-124 被明确拒绝，错误里同时给出**所有者与冲突路径**。"""
    from AstralNexus.core.governor import TaskGraphPatch

    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "P-123", "goal": "own the verification index",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/runs/verification/p-112-expected-index/"],
    }]))

    with pytest.raises(ValueError) as raised:
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "P-124", "goal": "own the same index",
            "dependencies": ["root"],
            "owned_paths": ["deliverables/runs/verification/p-112-expected-index/"],
        }]))

    message = str(raised.value)
    assert "overlapping output ownership" in message
    assert "P-124" in message and "P-123" in message          # 所有者被点名
    assert "deliverables/runs/verification/p-112-expected-index/" in message
    # (3) 非法补丁不改变图：被拒节点根本没进图。
    assert graph.get_node("P-124") is None


def _ids(container):
    return {getattr(item, "node_id", item) for item in container}


def test_rejection_leaves_the_previous_graph_intact(tmp_path):
    """被拒之后，原持有者的声明与既有节点数一字不变。"""
    from AstralNexus.core.governor import TaskGraphPatch

    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "P-123", "goal": "own the index", "dependencies": ["root"],
        "owned_paths": ["deliverables/runs/verification/"],
    }]))
    before = _ids(graph.nodes)
    holder_paths = list(graph.get_node("P-123").owned_paths)

    with pytest.raises(ValueError, match="overlapping output ownership"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "P-124", "goal": "own the same tree", "dependencies": ["root"],
            "owned_paths": ["deliverables/runs/verification/p-124/"],
            "output_artifacts": ["artifact:index"],
        }], update_nodes=[{"node_id": "P-123", "dependencies": []}]))

    assert _ids(graph.nodes) == before
    assert list(graph.get_node("P-123").owned_paths) == holder_paths