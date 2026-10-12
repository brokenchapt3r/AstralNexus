"""Fix AU：被 completed 后继接管的 failed 死记录不得永久挡住收敛闸门。

实测来源（doc 18, r13b, 2026-09-17）：运行跑到第 28 代仍未返回 completed。
根因是 `Orchestrator.is_execution_complete()` 要求**所有**节点状态属于
{completed, superseded, cancelled}，而图里永久留着三个 failed 节点：

    PATCH-R002-E2E     → R003-E2E(superseded) → R004-E2E → R006-E2E(completed 0.995)
    PATCH-R003-RUNTIME → …R005-RUNTIME(failed)… → R012-SERVE-ENTRY-LEDGER-FREEZE(completed 0.983)
    PATCH-R005-RUNTIME → R006-RUNTIME … → R012-SERVE-ENTRY-LEDGER-FREEZE(completed)

它们的交付契约早已被 completed 后继整体接管，Governor 也在 09-16 03:25 的
patch 里写明"属被取代的死记录而非可重试工作单元，故按取消被替代节点处理"，
但该 patch 的 cancelled 为空、意图未落地。后果是该函数恒为 False，
`_assess_delivery_completion()` 一次都没有被调用（trace 中
`governor.execution_completion.assessed` 事件数为 0），交付闸门永远走不到，
运行只能在重规划环里空烧 churn 预算——**无论多少代都不可能 completed**。

修复口径从严：只豁免 status == "failed" 且谱系确有 completed 后继者。
"""
from __future__ import annotations

from AstralNexus.core.task_graph import TaskNode


def _add(graph, node_id, status, *, recovery_of=None, deps=None):
    meta = {}
    if recovery_of:
        meta["recovery_of"] = recovery_of
    node = TaskNode(
        node_id=node_id,
        goal=f"goal {node_id}",
        role="coder",
        status=status,
        dependencies=list(deps or []),
        metadata=meta,
    )
    graph.add_node(node)
    return node


def _orchestrator(tmp_path, run_id):
    from AstralNexus.core.orchestrator import Orchestrator

    return Orchestrator(tmp_path, run_id=run_id)


def test_failed_node_absorbed_by_completed_successor_is_terminal(tmp_path):
    """线上原型：failed → superseded → completed 多跳接管后，图视为终态。"""
    with _orchestrator(tmp_path, "au-absorbed") as orch:
        graph = orch.task_graph
        _add(graph, "P-001", "completed")
        _add(graph, "R002", "failed", recovery_of="P-001")
        _add(graph, "R003", "superseded", recovery_of="R002")
        _add(graph, "R004", "superseded", recovery_of="R003")
        _add(graph, "R006", "completed", recovery_of="R004")

        assert orch.is_execution_complete() is True
        assert orch._absorbed_dead_records == {"R002"}


def test_failed_node_without_successor_still_blocks(tmp_path):
    """真正无后继的失败不得被粉饰为完成。"""
    with _orchestrator(tmp_path, "au-dead-end") as orch:
        graph = orch.task_graph
        _add(graph, "P-001", "completed")
        _add(graph, "DEAD", "failed")

        assert orch.is_execution_complete() is False
        assert orch._absorbed_dead_records == set()


def test_failed_node_with_unfinished_successor_still_blocks(tmp_path):
    """后继尚未完成（admitted/running/ready）时仍必须阻塞收敛。"""
    for unfinished in ("admitted", "running", "ready", "repair_pending", "blocked"):
        with _orchestrator(tmp_path, f"au-unfinished-{unfinished}") as orch:
            graph = orch.task_graph
            _add(graph, "R002", "failed")
            _add(graph, "R003", unfinished, recovery_of="R002")

            assert orch.is_execution_complete() is False, unfinished
            assert orch._absorbed_dead_records == set(), unfinished


def test_branching_lineage_finds_completed_on_the_other_branch(tmp_path):
    """分叉谱系：一条支死在 cancelled，另一条支走到 completed 即为已接管。

    线上原型：PATCH-R005-RUNTIME 同时派生 PATCH-R006-RUNTIME（该支最终停在
    cancelled 的 PATCH-R009-APP-RUNTIME-CLEANUP）与 PATCH-R010-SERVE-ENTRY
    （该支走到 completed 的 PATCH-R012）。单路径行走会取死支并误判"无后继"。
    """
    with _orchestrator(tmp_path, "au-branch") as orch:
        graph = orch.task_graph
        _add(graph, "R005", "failed")
        _add(graph, "R006", "superseded", recovery_of="R005")
        _add(graph, "R009", "cancelled", recovery_of="R006")
        _add(graph, "R010", "superseded", recovery_of="R005")
        _add(graph, "R011", "superseded", recovery_of="R010")
        _add(graph, "R012", "completed", recovery_of="R011")

        assert orch.is_execution_complete() is True
        assert orch._absorbed_dead_records == {"R005"}


def test_failed_middle_generation_does_not_break_the_walk(tmp_path):
    """中间代也是 failed 时继续行走（R003-RUNTIME → R005-RUNTIME → … → completed）。"""
    with _orchestrator(tmp_path, "au-failed-middle") as orch:
        graph = orch.task_graph
        _add(graph, "R003", "failed")
        _add(graph, "R004", "superseded", recovery_of="R003")
        _add(graph, "R005", "failed", recovery_of="R004")
        _add(graph, "R006", "superseded", recovery_of="R005")
        _add(graph, "R012", "completed", recovery_of="R006")

        assert orch.is_execution_complete() is True


def test_recovery_cycle_does_not_hang(tmp_path):
    """谱系成环（A recovery_of B / B recovery_of A）不得死循环。"""
    with _orchestrator(tmp_path, "au-cycle") as orch:
        graph = orch.task_graph
        _add(graph, "A", "failed", recovery_of="B")
        _add(graph, "B", "failed", recovery_of="A")

        assert orch.is_execution_complete() is False
        assert orch._absorbed_dead_records == set()


def test_absorbed_record_is_only_traced_once(tmp_path):
    """重复判定不得重复入账（幂等）。"""
    with _orchestrator(tmp_path, "au-idempotent") as orch:
        graph = orch.task_graph
        _add(graph, "R002", "failed")
        _add(graph, "R006", "completed", recovery_of="R002")

        for _ in range(3):
            assert orch.is_execution_complete() is True
        assert orch._absorbed_dead_records == {"R002"}


def test_plain_terminal_graph_still_reports_complete(tmp_path):
    """回归：无 failed 节点的终态图行为不变。"""
    with _orchestrator(tmp_path, "au-regression-terminal") as orch:
        graph = orch.task_graph
        _add(graph, "A", "completed")
        _add(graph, "B", "superseded")
        _add(graph, "C", "cancelled")

        assert orch.is_execution_complete() is True
        assert orch._absorbed_dead_records == set()


def test_live_r13b_shape_is_complete(tmp_path):
    """线上三死记录形状：两个 RUNTIME 失败谱系 + 一个 E2E 失败谱系全部被接管。"""
    with _orchestrator(tmp_path, "au-r13b-shape") as orch:
        graph = orch.task_graph
        _add(graph, "N-001", "completed")
        _add(graph, "PATCH-R001-BACKEND", "completed")
        _add(graph, "PATCH-R006-E2E", "completed", recovery_of="PATCH-R004-E2E")

        _add(graph, "PATCH-R002-E2E", "failed", recovery_of="N-007")
        _add(graph, "PATCH-R003-E2E", "superseded", recovery_of="PATCH-R002-E2E")
        _add(graph, "PATCH-R004-E2E", "superseded", recovery_of="PATCH-R003-E2E")

        _add(graph, "PATCH-R003-RUNTIME", "failed")
        _add(graph, "PATCH-R004-RUNTIME", "superseded", recovery_of="PATCH-R003-RUNTIME")
        _add(graph, "PATCH-R005-RUNTIME", "failed", recovery_of="PATCH-R004-RUNTIME")
        _add(graph, "PATCH-R006-RUNTIME", "superseded", recovery_of="PATCH-R005-RUNTIME")
        _add(graph, "PATCH-R009-APP-RUNTIME-CLEANUP", "cancelled", recovery_of="PATCH-R006-RUNTIME")
        _add(graph, "PATCH-R010-SERVE-ENTRY", "superseded", recovery_of="PATCH-R005-RUNTIME")
        _add(graph, "PATCH-R011-SERVE-ENTRY-LEDGER", "superseded", recovery_of="PATCH-R010-SERVE-ENTRY")
        _add(graph, "PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE", "completed", recovery_of="PATCH-R011-SERVE-ENTRY-LEDGER")

        assert orch.is_execution_complete() is True
        assert orch._absorbed_dead_records == {
            "PATCH-R002-E2E", "PATCH-R003-RUNTIME", "PATCH-R005-RUNTIME",
        }


# --- 第 36 轮：R020 完成后的"精确终局形状"预演 -------------------------------
# 线上此刻是：R018(repair_pending→superseded) → R020(running)，而失败的 R017 需要
# 一条 completed 载体。这两条测试把"只差这一个节点"变成可执行断言：
# R020 一 completed 闸门必开（正例），而在它 running 时闸门必不开（反例）。


def _add_final_acceptance_lineage(graph, carrier_status: str) -> None:
    """R017(failed) → R018(superseded) → R020(carrier_status) 的终局谱系。"""
    _add(graph, "PATCH-R017-FINAL-ACCEPTANCE-REPAIR", "failed")
    _add(graph, "PATCH-R018-FINAL-ACCEPTANCE-RUNTIME", "superseded",
         recovery_of="PATCH-R017-FINAL-ACCEPTANCE-REPAIR")
    _add(graph, "PATCH-R020-FINAL-ACCEPTANCE-RUNTIME-PROVENANCE", carrier_status,
         recovery_of="PATCH-R018-FINAL-ACCEPTANCE-RUNTIME")


def _add_three_absorbed_lineages(graph) -> None:
    _add(graph, "PATCH-R002-E2E", "failed")
    _add(graph, "PATCH-R003-E2E", "superseded", recovery_of="PATCH-R002-E2E")
    _add(graph, "PATCH-R004-E2E", "superseded", recovery_of="PATCH-R003-E2E")
    _add(graph, "PATCH-R006-E2E", "completed", recovery_of="PATCH-R004-E2E")

    _add(graph, "PATCH-R003-RUNTIME", "failed")
    _add(graph, "PATCH-R004-RUNTIME", "superseded", recovery_of="PATCH-R003-RUNTIME")
    _add(graph, "PATCH-R005-RUNTIME", "failed", recovery_of="PATCH-R004-RUNTIME")
    _add(graph, "PATCH-R006-RUNTIME", "superseded", recovery_of="PATCH-R005-RUNTIME")
    _add(graph, "PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE", "completed",
         recovery_of="PATCH-R006-RUNTIME")


def test_live_r13b_post_r020_shape_is_complete(tmp_path):
    """R020 completed ⇒ 四个死记录全部被接管 ⇒ 闸门必开。"""
    with _orchestrator(tmp_path, "au-r13b-post-r020") as orch:
        graph = orch.task_graph
        _add(graph, "N-001", "completed")
        _add(graph, "PATCH-R001-BACKEND", "completed")
        _add(graph, "N-010-FINAL-DELIVERY-VERIFY", "completed")
        _add(graph, "PATCH-R019-FINAL-ACCEPTANCE-BEHAVIOR", "completed")
        _add_three_absorbed_lineages(graph)
        _add_final_acceptance_lineage(graph, "completed")

        assert orch.is_execution_complete() is True
        assert orch._absorbed_dead_records == {
            "PATCH-R002-E2E", "PATCH-R003-RUNTIME", "PATCH-R005-RUNTIME",
            "PATCH-R017-FINAL-ACCEPTANCE-REPAIR",
        }


def test_live_r13b_pre_r020_shape_still_blocks(tmp_path):
    """同一形状但 R020 尚未完成 ⇒ 闸门必须仍然关着（R017 还没有载体）。"""
    with _orchestrator(tmp_path, "au-r13b-pre-r020") as orch:
        graph = orch.task_graph
        _add(graph, "N-001", "completed")
        _add(graph, "PATCH-R001-BACKEND", "completed")
        _add(graph, "N-010-FINAL-DELIVERY-VERIFY", "completed")
        _add(graph, "PATCH-R019-FINAL-ACCEPTANCE-BEHAVIOR", "completed")
        _add_three_absorbed_lineages(graph)
        _add_final_acceptance_lineage(graph, "running")

        assert orch.is_execution_complete() is False
        assert orch._absorbed_dead_records == {
            "PATCH-R002-E2E", "PATCH-R003-RUNTIME", "PATCH-R005-RUNTIME",
        }