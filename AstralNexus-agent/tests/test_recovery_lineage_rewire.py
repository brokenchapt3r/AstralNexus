"""Fix AD：恢复谱系中出现 failed 中间代时，下游死边仍必须被改写。

实测来源（doc 18, r13b, 2026-09-15）：N-009（七类文档节点，交付必经）的依赖里
同时挂着 N-007(superseded) 与 PATCH-R002-E2E(failed)，而真正的生产者在四跳之外的
PATCH-R004-E2E。`_rewire_superseded_dependents` 有两处漏洞：

  1. 只扫描 status == "superseded" 的节点；failed 但已被 recovery_of 接管的节点
     完全不参与改写。
  2. living_tail() 走到 failed 中间代就 `tail = None; break`；多跳谱系中只要有一代
     是 failed 而非 superseded，整条链就解析不出在世末端。

后果是永久性交付死锁：无论后续代次是否全部通过，N-009 都不可能 ready。
"""
from __future__ import annotations

from AstralNexus.core.task_graph import TaskNode


def _add(graph, node_id, status, *, deps=None, recovery_of=None):
    meta = {}
    if recovery_of:
        meta["recovery_of"] = recovery_of
    node = TaskNode(node_id=node_id, goal=f"goal {node_id}", role="coder",
                    status=status, dependencies=list(deps or []), metadata=meta)
    graph.add_node(node)
    for dep in deps or []:
        try:
            graph.add_edge(dep, node_id, "depends_on")
        except ValueError:
            pass
    return node


def test_failed_middle_generation_still_resolves_to_the_living_tail(tmp_path):
    """线上原型：N-007(superseded) → R002(failed) → R003(superseded) → R004(在世)。"""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="lineage-failed-middle") as orch:
        graph = orch.task_graph
        _add(graph, "N-001", "completed")
        _add(graph, "N-007", "superseded")
        _add(graph, "R002", "failed", recovery_of="N-007")
        _add(graph, "R003", "superseded", recovery_of="R002")
        _add(graph, "R004", "admitted", recovery_of="R003")
        downstream = _add(graph, "N-009", "blocked", deps=["N-001", "N-007", "R002"])

        orch._rewire_superseded_dependents()

        deps = list(downstream.dependencies)
        assert "N-007" not in deps, deps
        assert "R002" not in deps, deps
        assert "R004" in deps, deps
        assert "N-001" in deps, "健康依赖不得被动"
        assert len(deps) == len(set(deps)), "依赖必须去重"


def test_failed_node_without_successor_is_left_alone(tmp_path):
    """真正无法满足的依赖不得被悄悄抹掉。"""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="lineage-dead-end") as orch:
        graph = orch.task_graph
        _add(graph, "N-001", "completed")
        _add(graph, "DEAD", "failed")
        downstream = _add(graph, "N-009", "blocked", deps=["N-001", "DEAD"])

        orch._rewire_superseded_dependents()

        assert "DEAD" in downstream.dependencies, "无后继的失败依赖必须保留为阻塞"


def test_superseded_chain_without_failures_still_works(tmp_path):
    """既有行为回归：纯 superseded 多跳链不受影响。"""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="lineage-superseded") as orch:
        graph = orch.task_graph
        _add(graph, "A", "superseded")
        _add(graph, "B", "superseded", recovery_of="A")
        _add(graph, "C", "running", recovery_of="B")
        downstream = _add(graph, "D", "admitted", deps=["A"])

        orch._rewire_superseded_dependents()

        assert downstream.dependencies == ["C"]


def test_rewire_is_idempotent(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="lineage-idempotent") as orch:
        graph = orch.task_graph
        _add(graph, "A", "failed")
        _add(graph, "B", "ready", recovery_of="A")
        downstream = _add(graph, "D", "admitted", deps=["A"])

        orch._rewire_superseded_dependents()
        first = list(downstream.dependencies)
        orch._rewire_superseded_dependents()
        assert list(downstream.dependencies) == first == ["B"]


def test_successor_own_handover_edge_is_not_rewired_into_a_self_loop(tmp_path):
    """接管节点自身对被替换节点的谱系移交边不得被改写（既有护栏）。"""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="lineage-selfloop") as orch:
        graph = orch.task_graph
        _add(graph, "A", "failed")
        successor = _add(graph, "B", "ready", deps=["A"], recovery_of="A")

        orch._rewire_superseded_dependents()

        assert "B" not in successor.dependencies, "不得产生自环"


def test_rewire_runs_on_every_cycle_not_only_during_recovery(tmp_path, monkeypatch):
    """Fix AE：五个既有调用点全在恢复/重规划路径上。

    图一旦不再需要恢复，已有的死边就永远留着。线上 N-009 正是这种状态：
    谱系早已推进到 PATCH-R004-*，它却仍指着 N-007(superseded) 与
    PATCH-R003-E2E(superseded)；只要 R004 一代顺利跑完，重连线再也不会被调用，
    N-009 永远 ready 不了。
    """
    import inspect

    from AstralNexus.core.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator.run)
    # 主循环体内、观察调用之前，必须有一次无条件的死边治愈
    loop_body = source.split("self.iteration_count += 1", 1)[1]
    head = loop_body.split("runtime_state = self.runtime_observer.observe", 1)[0]
    assert "_rewire_superseded_dependents()" in head, (
        "每轮观察前必须无条件治愈死边，否则不再发生恢复的图会永远带着死边"
    )
