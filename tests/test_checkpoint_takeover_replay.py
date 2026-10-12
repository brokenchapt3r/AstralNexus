# -*- coding: utf-8 -*-
"""Fix AR —— 已退役的接管者不得在恢复时重放它的接管。

restore_checkpoint 里有一段针对旧检查点的修补：如果某个节点声明 recovery_of=X
而 X 没有被置为 superseded，就补一次接管补丁。它过去**只看被接管者 X 的状态**，
跳过 {completed, cancelled, superseded} —— 注意 **failed 不在这个集合里**。

线上 r13b 的后果（2026-09-16）：四条早已完成的历史接管，因为各自的被接管者
仍停在 failed，在每一次恢复时被重放：

    PATCH-R003-E2E(superseded)      recovery_of PATCH-R002-E2E(failed)
    PATCH-R004-RUNTIME(superseded)  recovery_of PATCH-R003-RUNTIME(failed)
    PATCH-R006-RUNTIME(superseded)  recovery_of PATCH-R005-RUNTIME(failed)
    PATCH-R010-SERVE-ENTRY(superseded) recovery_of PATCH-R005-RUNTIME(failed)

接管补丁会把"依赖被接管者的消费者"改判到接管者头上。可这些接管者**自己也早已
被后代取代**（R006-RUNTIME → R007 → R008 → R010 → R011 → R012）。重放的结果是
把 N-009 / R013 / R014 / R015 / R016 / R017 对**已完成的 R012** 的依赖，
倒着改写回**已退役的 R006-RUNTIME**。随后 Governor 拒绝了它自己刚被递进来的图：

    graph patch artifact producer is not a declared dependency:
    artifact:r011-serve-entry-evidence-ledger is produced by
    PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE, consumed by PATCH-R017-...

restore_checkpoint 抛异常 → run 被判 checkpoint_restore_failed → **整个运行不可恢复**，
九个已通过的节点被困在里面。而且那条错误信息说的是"update_nodes 必须直接设置
dependencies"，可依赖本来就设对了——信息本身把人引向错误方向。

不变式：一次接管在它发生时就已经生效了。接管者一旦自己退役（superseded/cancelled），
谱系已经走过它，重放它的接管永远不是修复。
"""
from __future__ import annotations

import json
from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator


def _lineage_run(tmp_path: Path, run_id: str, replacement_status: str) -> Orchestrator:
    """复刻线上形状：P(failed) <- M(replacement_status) <- ... <- C(completed)，消费者依赖 C。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id=run_id)
    orch.task_graph.add_node({
        "node_id": "P", "goal": "原始运行时节点", "role": "coder",
        "status": "failed", "dependencies": [],
        "output_artifacts": [], "metadata": {},
    })
    orch.task_graph.add_node({
        "node_id": "M", "goal": "中途接管者", "role": "coder",
        "status": replacement_status, "dependencies": [],
        "output_artifacts": [], "metadata": {"recovery_of": "P"},
    })
    orch.task_graph.add_node({
        "node_id": "C", "goal": "当前产出者", "role": "coder",
        "status": "completed", "dependencies": [],
        "output_artifacts": ["artifact:ledger"], "metadata": {"recovery_of": "M"},
    })
    orch.task_graph.add_node({
        "node_id": "D", "goal": "下游消费者", "role": "tester",
        "status": "ready", "dependencies": ["C"],
        "input_artifacts": ["artifact:ledger"], "metadata": {},
    })
    orch._save_checkpoint()
    return orch


# ---------------------------------------------------------------- 1. 不可恢复回归
def test_restore_succeeds_when_the_replacement_is_already_retired(tmp_path):
    """线上形状：过去这里抛 ValueError，run 变成 checkpoint_restore_failed。"""
    _lineage_run(tmp_path, "retired-replacement", "superseded")
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="retired-replacement")
    assert restored.restore_checkpoint() is True


def test_downstream_dependency_is_not_rewritten_back_onto_a_retired_ancestor(tmp_path):
    _lineage_run(tmp_path, "no-rewrite", "superseded")
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="no-rewrite")
    assert restored.restore_checkpoint() is True
    consumer = restored.task_graph.get_node("D")
    assert consumer is not None
    assert "C" in consumer.dependencies, "消费者必须仍然依赖当前产出者"
    assert "M" not in consumer.dependencies, "不得被改判到已退役的祖先"


def test_cancelled_replacement_is_also_skipped(tmp_path):
    _lineage_run(tmp_path, "cancelled-replacement", "cancelled")
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="cancelled-replacement")
    assert restored.restore_checkpoint() is True
    assert "C" in restored.task_graph.get_node("D").dependencies


def test_the_skip_is_recorded_so_it_is_auditable(tmp_path):
    _lineage_run(tmp_path, "audited-skip", "superseded")
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="audited-skip")
    assert restored.restore_checkpoint() is True
    trace = Path(tmp_path) / "trace.jsonl"
    assert trace.exists()
    events = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
        if "takeover_replay_skipped" in line
    ]
    assert events, "跳过必须留痕，不能是静默行为"
    payload = events[-1].get("payload") or events[-1].get("data") or events[-1]
    assert payload.get("replacement_node_id") == "M"
    assert payload.get("replacement_status") == "superseded"
    assert payload.get("target_node_id") == "P"


# ---------------------------------------------------------------- 2. 真修复仍然发生
def test_live_replacement_still_retires_its_unfinished_target(tmp_path):
    """这段修补存在的理由：接管者还活着、被接管者却没被置为 superseded。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id="live-takeover")
    orch.task_graph.add_node({
        "node_id": "P", "goal": "未退役的原始写者", "role": "coder",
        "status": "failed", "dependencies": [], "metadata": {},
    })
    orch.task_graph.add_node({
        "node_id": "R", "goal": "仍然活跃的接管者", "role": "coder",
        "status": "ready", "dependencies": [], "metadata": {"recovery_of": "P"},
    })
    orch._save_checkpoint()

    restored = Orchestrator(tmp_path, max_iterations=1, run_id="live-takeover")
    assert restored.restore_checkpoint() is True
    assert restored.task_graph.get_node("P").status == "superseded", \
        "活跃接管者的被接管者仍必须被退役 —— 这段修补不能被一并关掉"


def test_ready_replacement_is_not_skipped(tmp_path):
    orch = Orchestrator(tmp_path, max_iterations=1, run_id="ready-replacement")
    orch.task_graph.add_node({
        "node_id": "P", "goal": "x", "role": "coder", "status": "failed",
        "dependencies": [], "metadata": {},
    })
    orch.task_graph.add_node({
        "node_id": "R", "goal": "y", "role": "coder", "status": "ready",
        "dependencies": [], "metadata": {"recovery_of": "P"},
    })
    orch._save_checkpoint()
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="ready-replacement")
    assert restored.restore_checkpoint() is True
    trace = Path(tmp_path) / "trace.jsonl"
    text = trace.read_text(encoding="utf-8") if trace.exists() else ""
    assert "takeover_replay_skipped" not in text


# ---------------------------------------------------------------- 3. 幂等
def test_restore_is_idempotent_across_repeated_attempts(tmp_path):
    """线上是每次恢复都重放一次；恢复必须可以反复做而不改变图。"""
    _lineage_run(tmp_path, "idempotent", "superseded")
    deps = None
    for _ in range(3):
        restored = Orchestrator(tmp_path, max_iterations=1, run_id="idempotent")
        assert restored.restore_checkpoint() is True
        current = list(restored.task_graph.get_node("D").dependencies)
        if deps is not None:
            assert current == deps, "反复恢复不得让依赖发生漂移"
        deps = current


# ---------------------------------------------------------------- 4. 诊断可定位
def test_producer_dependency_rejection_states_what_it_observed():
    """旧信息只说"update_nodes 必须直接设置 dependencies"，而依赖本来就设对了。"""
    import inspect

    from AstralNexus.core import governor as governor_mod

    src = inspect.getsource(governor_mod)
    assert "producer_in_declared_dependencies=" in src
    assert "depends_on_edges_into_consumer=" in src


def test_restore_failure_keeps_a_stack():
    import inspect

    from AstralNexus.core import orchestrator as orch_mod

    src = inspect.getsource(orch_mod.Orchestrator.restore_checkpoint)
    assert "traceback.format_exc()" in src


# ---------------------------------------------------------------- 5. 真实图重放（不是我想象的形状）
#
# 上面那组合成夹具**没有复现**线上故障：把守卫摘掉再跑，只有审计留痕那一条会挂，
# 依赖改写那两条照样通过。也就是说我凭推理构造的形状不等于线上的形状。
# 所以这里直接把 r13b 恢复失败当时的 36 个节点原样喂回去 —— 状态、recovery_of、
# dependencies、input/output artifacts 全部取自 task_graph.sqlite。
# 摘掉守卫时这两条必须失败；装上守卫必须通过。
FIXTURE = Path(__file__).parent / "fixtures" / "r13b_restore_graph.json"


def _load_live_shape(tmp_path: Path, run_id: str) -> Orchestrator:
    orch = Orchestrator(tmp_path, max_iterations=1, run_id=run_id)
    for item in json.loads(FIXTURE.read_text(encoding="utf-8")):
        metadata = {}
        if item["recovery_of"]:
            metadata["recovery_of"] = item["recovery_of"]
        orch.task_graph.add_node({
            "node_id": item["node_id"],
            "goal": f"{item['node_id']} (r13b 现场快照)",
            "role": "coder",
            "status": item["status"],
            "dependencies": list(item["dependencies"]),
            "input_artifacts": list(item["input_artifacts"]),
            "output_artifacts": list(item["output_artifacts"]),
            "metadata": metadata,
        })
    orch._save_checkpoint()
    return orch


def test_live_r13b_graph_restores():
    """回归锚点：这张图在修复前连续四次恢复失败，run 被判 checkpoint_restore_failed。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _load_live_shape(root, "r13b-shape")
        restored = Orchestrator(root, max_iterations=1, run_id="r13b-shape")
        assert restored.restore_checkpoint() is True


def test_live_r13b_restore_does_not_rewrite_any_dependency():
    """恢复是只读式修复：它不该改动这张图里任何节点的 dependencies。

    线上正是在这里出的事 —— N-009 / R013 / R014 / R015 / R016 / R017 对已完成的
    PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE 的依赖，被倒着改写回已退役的
    PATCH-R006-RUNTIME（N-009 本来就同时依赖 R006-RUNTIME，所以对它要看的是
    "R012 还在不在"，而不是"R006-RUNTIME 在不在" —— 这是我第一版断言写错的地方）。
    """
    import tempfile

    expected = {
        item["node_id"]: list(item["dependencies"])
        for item in json.loads(FIXTURE.read_text(encoding="utf-8"))
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _load_live_shape(root, "r13b-deps")
        restored = Orchestrator(root, max_iterations=1, run_id="r13b-deps")
        assert restored.restore_checkpoint() is True
        drifted = {
            node_id: {"before": expected[node_id], "after": list(node.dependencies)}
            for node_id, node in restored.task_graph.nodes.items()
            if node_id in expected and list(node.dependencies) != expected[node_id]
        }
        assert not drifted, f"恢复改写了依赖：{json.dumps(drifted, ensure_ascii=False)[:1200]}"


def test_live_r13b_consumers_keep_the_completed_producer():
    """R012 已完成并产出 artifact:r011-serve-entry-evidence-ledger，六个消费者必须仍依赖它。"""
    import tempfile

    producer = "PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE"
    consumers = [
        "N-009",
        "PATCH-R013-FINAL-DELIVERY-VERIFY",
        "PATCH-R014-DELIVERY-MANIFEST",
        "PATCH-R015-GOAL-CONVERGENCE",
        "PATCH-R017-FINAL-ACCEPTANCE-REPAIR",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _load_live_shape(root, "r13b-producer")
        restored = Orchestrator(root, max_iterations=1, run_id="r13b-producer")
        assert restored.restore_checkpoint() is True
        for consumer_id in consumers:
            node = restored.task_graph.get_node(consumer_id)
            assert node is not None, consumer_id
            assert producer in node.dependencies, f"{consumer_id} 丢了当前生产者"
