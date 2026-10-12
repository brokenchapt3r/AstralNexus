"""doc 18 attempt3 三项审计 + 收尾缺陷的反转测试（修复 A-D）。

审计①（孤儿生成入口）：两次孤儿同一序列——worker 被 SSL 风暴击杀 →
节点 failed → 同秒恢复规划 → 撞上同一风暴打开的熔断 → LLMClientError
冒泡 → host 停机、恢复半途而废。修复A：熔断短冷却（≤60s）有界等待
（≤2 次），穷尽才升级——孤儿不再于源头生成。

审计②（supersede 非原子 + 单跳后继误判）：替换只加新边不删旧边，
`_blocked_by_terminal_dep` 不迁移；`_dependency_permanently_dead` 单跳
检查在链深≥2（R-002-R→RECOVER→SSLFIX）时把有活末端的谱系误判永久
死亡 → R-003-R 永久 blocked。修复B：后继链传递遍历；修复C：替换提交
内原子改写下游 depends_on 死边至在世末端。

缺陷④（收尾无界循环）：run() 返回 blocked 后 _resume_task 仍无条件
交付综合，且 repair 上限默认 0 被解释为无限。修复D：综合仅限
completed；`_delivery_repair_budget` 显式有界（默认 2，0=不修）。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.runtime.llm_client import LLMClientError

CIRCUIT_MSG = (
    "provider circuit open after 3 retries: provider circuit open for "
    "https://dashscope.aliyuncs.com/compatible-mode/v1; retry in 15.73s; "
    "request_stats={'message_count': 2}"
)


def _make_orchestrator(tmp_path, run_id="audit-run", **kwargs):
    return Orchestrator(tmp_path, run_id=run_id, **kwargs)


# ---------------------------------------------------------------- 修复A


def test_circuit_wait_absorbs_short_cooldown_and_retries(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    orch = _make_orchestrator(tmp_path)
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMClientError(CIRCUIT_MSG)
        return "plan"

    assert orch._call_with_circuit_wait(flaky, description="plan_fault_recovery") == "plan"
    assert calls["n"] == 2
    # 冷却 15.73s + 2s 余量
    assert sleeps == [pytest.approx(17.73, abs=0.01)]
    assert any(k == "orchestrator.circuit_wait" for k, _ in traces)


def test_circuit_wait_gives_up_after_max_waits(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    orch = _make_orchestrator(tmp_path)

    def always_open():
        raise LLMClientError(CIRCUIT_MSG)

    with pytest.raises(LLMClientError):
        orch._call_with_circuit_wait(always_open, description="plan_fault_recovery")
    # 有界：等满 max_waits=2 次后原样冒泡（host 语义不变）
    assert len(sleeps) == 2


def test_circuit_wait_ignores_non_circuit_and_long_cooldown(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    orch = _make_orchestrator(tmp_path)

    def plain_error():
        raise LLMClientError("connection reset by peer")

    with pytest.raises(LLMClientError):
        orch._call_with_circuit_wait(plain_error, description="x")

    def long_cooldown():
        raise LLMClientError(
            "provider circuit open for https://x; retry in 120.50s"
        )

    with pytest.raises(LLMClientError):
        orch._call_with_circuit_wait(long_cooldown, description="x")
    assert sleeps == []  # 非熔断/长冷却都不等待，立即升级


def test_governed_fault_entry_waits_through_circuit(tmp_path, monkeypatch):
    """attempt3 孤儿入口反转：plan_fault_recovery 第一次撞熔断、第二次
    返回无效提案——修复后不再冒泡成 host 停机，而是走完无效提案分支。"""
    monkeypatch.setattr("time.sleep", lambda s: None)
    orch = _make_orchestrator(tmp_path)
    orch.task_graph.add_node({
        "node_id": "F-1", "goal": "g", "role": "coder", "status": "failed",
    })
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    calls = {"n": 0}

    def plan(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMClientError(CIRCUIT_MSG.replace("15.73s", "0.01s"))
        raise ValueError("invalid recovery JSON")

    monkeypatch.setattr(orch.governor, "plan_fault_recovery", plan)
    fault = orch.fault_detector.detect_node_failure("F-1", "execution failed")
    result = orch._execute_governed_fault(fault)
    assert result["success"] is False
    assert calls["n"] == 2  # 等过冷却后重试成功进入规划
    assert any(k == "orchestrator.circuit_wait" for k, _ in traces)
    assert orch.runtime_state_manager.state.recovery_churn_total == 1


# ---------------------------------------------------------------- 修复B


def _lineage_graph(tmp_path):
    """attempt3 实证谱系：X→Y→Z 三代替换 + 下游 D 仍指 X。"""
    orch = _make_orchestrator(tmp_path)
    orch.task_graph.add_node({
        "node_id": "X", "goal": "g", "role": "resource_broker",
        "status": "superseded",
    })
    orch.task_graph.add_node({
        "node_id": "Y", "goal": "g", "role": "coder", "status": "superseded",
        "metadata": {"recovery_of": "X"},
    })
    orch.task_graph.add_node({
        "node_id": "Z", "goal": "g", "role": "coder", "status": "ready",
        "metadata": {"recovery_of": "Y"},
    })
    orch.task_graph.add_node({
        "node_id": "D", "goal": "g", "role": "governed_worker",
        "status": "admitted", "dependencies": ["X"],
    })
    orch.task_graph.add_edge("X", "D", "depends_on")
    orch.task_graph.add_edge("X", "D", "evidence_for")
    return orch


def test_permanently_dead_walks_successor_chain(tmp_path):
    orch = _lineage_graph(tmp_path)
    # 直接后继 Y 已 superseded，但链深处 Z 在世——单跳检查会误判死亡
    assert orch._dependency_permanently_dead(orch.task_graph.get_node("X")) is False
    # 链再加深一代（Z→W）：传递判定依然成立
    orch.task_graph.get_node("Z").status = "superseded"
    orch.task_graph.add_node({
        "node_id": "W", "goal": "g", "role": "coder", "status": "ready",
        "metadata": {"recovery_of": "Z"},
    })
    assert orch._dependency_permanently_dead(orch.task_graph.get_node("X")) is False
    # 整条链死亡（末端 W cancelled，无在世后继）才判永久死亡
    orch.task_graph.get_node("W").status = "cancelled"
    assert orch._dependency_permanently_dead(orch.task_graph.get_node("X")) is True


def test_lineage_cycle_does_not_hang(tmp_path):
    orch = _make_orchestrator(tmp_path)
    orch.task_graph.add_node({
        "node_id": "A", "goal": "g", "role": "coder", "status": "superseded",
        "metadata": {"recovery_of": "B"},
    })
    orch.task_graph.add_node({
        "node_id": "B", "goal": "g", "role": "coder", "status": "superseded",
        "metadata": {"recovery_of": "A"},
    })
    # 环状谱系：seen 集合防无限递归，判永久死亡
    assert orch._dependency_permanently_dead(orch.task_graph.get_node("A")) is True


# ---------------------------------------------------------------- 修复C


def test_rewire_moves_dependents_to_living_tail(tmp_path):
    orch = _lineage_graph(tmp_path)
    rewired = orch._rewire_superseded_dependents()
    assert rewired == 1
    d = orch.task_graph.get_node("D")
    # 依赖列表原子替换且去重：旧生产者 X → 在世末端 Z
    assert d.dependencies == ["Z"]
    dep_edges = [
        (e.source, e.target) for e in orch.task_graph.edges
        if e.edge_type == "depends_on" and e.target == "D"
    ]
    assert dep_edges == [("Z", "D")]
    # 历史证据归属不可变：evidence_for 边保留
    ev_edges = [
        (e.source, e.target) for e in orch.task_graph.edges
        if e.edge_type == "evidence_for" and e.target == "D"
    ]
    assert ev_edges == [("X", "D")]
    # 幂等：无死边则零变更
    assert orch._rewire_superseded_dependents() == 0


def test_rewire_skips_when_no_living_tail(tmp_path):
    orch = _make_orchestrator(tmp_path)
    orch.task_graph.add_node({
        "node_id": "X", "goal": "g", "role": "coder", "status": "superseded",
    })
    orch.task_graph.add_node({
        "node_id": "Y", "goal": "g", "role": "coder", "status": "failed",
        "metadata": {"recovery_of": "X"},
    })
    orch.task_graph.add_node({
        "node_id": "D", "goal": "g", "role": "tester", "status": "admitted",
        "dependencies": ["X"],
    })
    orch.task_graph.add_edge("X", "D", "depends_on")
    # 末端死亡（failed）：不改写——由修复B正确判定永久死亡
    assert orch._rewire_superseded_dependents() == 0
    assert orch.task_graph.get_node("D").dependencies == ["X"]


def test_rewire_skips_completed_dependents(tmp_path):
    orch = _lineage_graph(tmp_path)
    orch.task_graph.get_node("D").status = "completed"
    # 已完成下游的工作与证据已绑定旧产物，不得改写
    assert orch._rewire_superseded_dependents() == 0
    assert orch.task_graph.get_node("D").dependencies == ["X"]


def test_rewire_skips_tail_self_and_rolls_back_cycles(tmp_path):
    """takeover 回归实证：后继自身依赖被替换节点（谱系移交边）不得被
    改写成自环；D 为末端上游时改写会成环——必须完整回滚保持原状。"""
    orch = _make_orchestrator(tmp_path)
    orch.task_graph.add_node({
        "node_id": "X", "goal": "g", "role": "tester", "status": "superseded",
    })
    orch.task_graph.add_node({
        "node_id": "Y", "goal": "g", "role": "coder", "status": "ready",
        "metadata": {"recovery_of": "X"},
        "dependencies": ["X"],  # 接管谱系移交边
    })
    orch.task_graph.add_edge("X", "Y", "depends_on")
    # 情况1：唯一"下游"就是末端自己——跳过，零改写零异常
    assert orch._rewire_superseded_dependents() == 0
    assert orch.task_graph.get_node("Y").dependencies == ["X"]

    # 情况2：D 依赖 X，但 Y 又依赖 D（D 是末端上游）——改写会成环 → 回滚
    orch.task_graph.add_node({
        "node_id": "D", "goal": "g", "role": "governed_worker",
        "status": "admitted", "dependencies": ["X"],
    })
    orch.task_graph.add_edge("X", "D", "depends_on")
    orch.task_graph.add_edge("D", "Y", "depends_on")
    assert orch._rewire_superseded_dependents() == 0
    d = orch.task_graph.get_node("D")
    assert d.dependencies == ["X"]  # 回滚：依赖列表原状
    x_d_edges = [
        e for e in orch.task_graph.edges
        if e.source == "X" and e.target == "D" and e.edge_type == "depends_on"
    ]
    assert len(x_d_edges) == 1  # 回滚：死边也原样保留（不误删）


# ---------------------------------------------------------------- 修复D




class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 14, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _churn_blocked_run(tmp_path, run_dir_name, budget=2):
    """预算触顶 blocked 停机的 run（复用 test_recovery_churn_budget 契约）。"""
    from AstralNexus.core.governor import GoalSpec
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    clock = Clock()
    run_dir = tmp_path / run_dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    coord = RunCoordinator(
        run_dir, run_id=run_dir_name, lease_seconds=120, grace_seconds=10,
        now_fn=clock,
    )
    lease = coord.acquire("owner-A")
    orch = Orchestrator(
        run_dir=run_dir,
        graph_store=SQLiteTaskGraphStore(run_dir / "task_graph.sqlite"),
        run_id="graph-churn",
        ownership_probe=lambda: coord.owns(lease),
        fence_token_provider=lambda: coord.fence_token(lease),
        recovery_churn_budget=budget,
    )
    orch.task_graph.add_node({
        "node_id": "T-1", "goal": "small service", "role": "coder",
        "status": "completed",
    })
    orch.governor.goal_spec = GoalSpec(
        goal_id="G1", task_goal="small service",
        normalized_goal="small service", hard_constraints=[],
    )
    orch.governor.delivery_contract = {
        "required_outputs": [], "execution_evidence_policy": "runtime_required",
    }
    orch.governor.goal_version = 1
    for _ in range(budget):
        orch._consume_recovery_churn("verification_recovery_applied")
    assert orch._persist_graph_checkpoint(stage="wave_churn") is True
    assert coord.finish(
        lease, "blocked",
        stop_reason="autonomous recovery churn budget exhausted",
        stop_code="no_progress_breaker",
    ) is True
    return run_dir, coord


def test_resume_skips_delivery_synthesis_for_blocked_run(tmp_path, monkeypatch):
    """attempt3 缺陷④反转：run() 返回 blocked 时 _resume_task 不得进入
    交付综合（旧实现无条件综合 + 无界 repair 循环烧 API、续租假 running）。"""
    from AstralNexus.cli import main as cli_main

    run_dir, coord = _churn_blocked_run(tmp_path, "run-gate")

    def forbidden_delivery(*args, **kwargs):
        raise AssertionError("delivery synthesis must not run for blocked result")

    monkeypatch.setattr(cli_main, "_generate_task_delivery", forbidden_delivery)

    captured = {}

    def on_orch(orchestrator, _run_dir):
        captured["orch"] = orchestrator
        orchestrator.run = lambda *a, **kw: {
            "status": "blocked",
            "stop_code": "no_progress_breaker",
            "stop_reason": "autonomous recovery churn budget exhausted",
            "iterations": 21,
        }

    task_file = tmp_path / "task.txt"
    task_file.write_text("continue the service work", encoding="utf-8")
    args = SimpleNamespace(
        run_dir=str(run_dir), task=str(task_file), output=str(run_dir),
        model="test-model", provider="test", max_parallel=1, config=None,
        user_resume=True,
    )
    code = cli_main._resume_task(args, tmp_path / "home", on_orchestrator=on_orch)
    assert code == 1
    assert "orch" in captured
    row = coord.current()
    # 主停机码保真落库，无 finalize_host_error 附加
    assert row["status"] == "blocked"
    assert row["stop_code"] == "no_progress_breaker"
    assert "finalize_host_error" not in str(row["stop_reason"])


# ---------------------------------------------------------------- 修复E


def _seam_graph(tmp_path):
    """attempt4 live 场景复刻：根因文件 client_check.py 属 COMPLETED 的
    R-02；验证节点 F-1 失败；提案落入矛盾形状（recovery_of 指向 completed
    的 R-02 且不声明根因路径）。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch = _make_orchestrator(tmp_path)
    orch.task_graph.add_node({
        "node_id": "R-02", "goal": "client check", "role": "coder",
        "status": "completed",
        "owned_paths": ["deliverables/app/client_check.py"],
    })
    orch.task_graph.add_node({
        "node_id": "F-1", "goal": "verify endpoint", "role": "coder",
        "status": "failed",
    })
    resolved = {
        "client_check.py": ("R-02", ["deliverables/app/client_check.py"]),
    }
    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "F-1-R1",
        "goal": "re-verify with a real socket-level HTTP call",
        "task_type": "implementation",
        "required_capabilities": ["coding"],
        "dependencies": [],
        "acceptance_criteria": ["live receipts"],
        "recovery_of": "R-02",  # 非法目标：completed
        "owned_paths": ["deliverables/runs/verify/receipt.json"],
        "output_artifacts": [],
    }])
    return orch, patch, resolved


def test_seam_normalization_retargets_and_grants(tmp_path):
    orch, patch, resolved = _seam_graph(tmp_path)
    node = orch.task_graph.get_node("F-1")
    out = orch._normalize_root_cause_takeover(node, patch, resolved)
    assert out["retargeted_from"] == "R-02"
    assert out["granted_paths"] == ["deliverables/app/client_check.py"]
    added = patch.add_nodes[0]
    assert added["recovery_of"] == "F-1"  # 重定向到被恢复的失败节点
    assert "deliverables/app/client_check.py" in added["owned_paths"]
    # 规范化后路由验证器通过（不再抛"ignores the declared upstream root cause"）
    assert orch._enforce_root_cause_routing(node, patch, resolved) is True
    # 幂等
    out2 = orch._normalize_root_cause_takeover(node, patch, resolved)
    assert out2 == {"granted_paths": [], "retargeted_from": ""}


def test_seam_last_resort_grant_saves_third_attempt(tmp_path, monkeypatch):
    """attempt4 live 反转：workaround 形状三连拒 → 终判 → 每轮烧 4 单位。
    修复E 后前两次拒绝保留反馈学习（Round 8 语义），第三次确定性授予
    COMPLETED 根因路径并放行——2拒+1救 代替 3拒+终判。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _seam_patch, resolved = _seam_graph(tmp_path)
    node = orch.task_graph.get_node("F-1")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.5,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: client_check.py never performed a real "
                    "socket-level HTTP call",
            evidence_refs=[],
        )],
    )
    attempts = {"n": 0}

    def fake_planner(**_kw):
        attempts["n"] += 1
        return TaskGraphPatch(add_nodes=[{
            "node_id": f"F-1-W{attempts['n']}",
            "goal": "work around the verification gap on the test side",
            "task_type": "testing",
            "required_capabilities": ["testing"],
            "dependencies": [],
            "acceptance_criteria": ["synthetic receipt"],
            "owned_paths": [f"deliverables/runs/workaround/w{attempts['n']}.json"],
            "output_artifacts": [],
        }])

    orch.governor.plan_verification_recovery = fake_planner
    monkeypatch.setattr(
        orch, "_resolve_declared_root_causes", lambda *a, **kw: dict(resolved),
    )
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    result = orch._apply_governed_verification_recovery(node, verification_result)
    assert result.get("rejected") is not True, result
    assert attempts["n"] == 3  # 前两次拒绝反馈保留，第三次补救放行
    saved = orch.task_graph.get_node("F-1-W3")
    assert saved is not None
    assert "deliverables/app/client_check.py" in saved.owned_paths
    # 预算账目：2 拒 + 1 应用 = 3 单位（旧行为 3 拒 + 终判 + 下游级联）
    assert orch.runtime_state_manager.state.recovery_churn_total == 3
    # 未终判
    assert not (node.metadata or {}).get("_terminal_failure")
    assert any(
        k == "governor.verification_recovery.root_cause_last_resort_grant"
        for k, _ in traces
    )


def test_seam_normalization_skips_non_retargetable_node(tmp_path):
    """被恢复节点自身不在 failed/repair_pending/blocked 时整体不动
    （避免把提案改写成另一个非法目标），路由验证器保持原拒绝语义。"""
    orch, patch, resolved = _seam_graph(tmp_path)
    node = orch.task_graph.get_node("F-1")
    node.status = "result_submitted"
    out = orch._normalize_root_cause_takeover(node, patch, resolved)
    assert out == {"granted_paths": [], "retargeted_from": ""}
    assert patch.add_nodes[0]["recovery_of"] == "R-02"
    with pytest.raises(ValueError, match="ignores the declared upstream root cause"):
        orch._enforce_root_cause_routing(node, patch, resolved)


def test_seam_normalization_preserves_round8_workaround_rejection(tmp_path):
    """Round 8 语义边界：纯 workaround 提案（无 recovery_of、不声明根因
    路径）不规范化——拒绝反馈循环是设计行为（r11 六代 workaround 教训），
    修复E 只闭合"接管意图明确但形状非法"的矛盾对。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, resolved = _seam_graph(tmp_path)
    node = orch.task_graph.get_node("F-1")
    workaround = TaskGraphPatch(add_nodes=[{
        "node_id": "F-1-WORKAROUND",
        "goal": "work around the client check gap on the test side",
        "task_type": "testing",
        "required_capabilities": ["testing"],
        "dependencies": [],
        "acceptance_criteria": ["synthetic receipt"],
        "owned_paths": ["deliverables/runs/workaround/note.json"],
        "output_artifacts": [],
    }])
    out = orch._normalize_root_cause_takeover(node, workaround, resolved)
    assert out == {"granted_paths": [], "retargeted_from": ""}
    assert "deliverables/app/client_check.py" not in (
        workaround.add_nodes[0]["owned_paths"]
    )
    with pytest.raises(ValueError, match="ignores the declared upstream root cause"):
        orch._enforce_root_cause_routing(node, workaround, resolved)


def test_seam_normalization_ignores_empty_add_nodes(tmp_path):
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, resolved = _seam_graph(tmp_path)
    node = orch.task_graph.get_node("F-1")
    empty = TaskGraphPatch(add_nodes=[])
    out = orch._normalize_root_cause_takeover(node, empty, resolved)
    assert out == {"granted_paths": [], "retargeted_from": ""}
    # 真空提案仍按原样被路由验证器拒绝
    with pytest.raises(ValueError, match="ignores the declared upstream root cause"):
        orch._enforce_root_cause_routing(node, empty, resolved)


# ---------------------------------------------------------------------------
# doc 18 修复F：所有权冲突接缝（attempt5 live：N-003-R* 谱系每代与活跃
# repair_pending 所有者争 inspection_service.py，三连拒 → 终判 → 级联）
# ---------------------------------------------------------------------------

_CONFLICT_MSG = (
    "graph patch has overlapping output ownership: N-NEW conflicts with X-1: "
    "['owned_path:deliverables/app/inspection_service.py <> "
    "deliverables/app/inspection_service.py']. Assign leaf-level disjoint "
    "owned_paths"
)


def _conflict_node(orch, node_id="X-1", status="repair_pending"):
    orch.task_graph.add_node({
        "node_id": node_id, "goal": "own the service", "role": "coder",
        "status": status,
        "owned_paths": ["deliverables/app/inspection_service.py"],
    })


def test_conflict_conversion_targets_active_owner_only(tmp_path):
    """修复F②单元：只转换 ACTIVE 合法目标（failed/repair_pending/blocked）
    的冲突；completed 冲突归修复E/stripping 管；非冲突消息与已接管形状不动。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    _conflict_node(orch)
    proposal = {
        "node_id": "N-NEW", "goal": "g", "task_type": "implementation",
        "required_capabilities": ["coding"], "dependencies": [],
        "acceptance_criteria": ["a"],
        "recovery_of": "F-1",  # 已指向另一 failed 节点 → 覆盖并留痕
        "owned_paths": ["deliverables/app/inspection_service.py"],
    }
    patch = TaskGraphPatch(add_nodes=[dict(proposal)])
    assert orch._convert_conflict_to_takeover(patch, _CONFLICT_MSG) is True
    added = patch.add_nodes[0]
    assert added["recovery_of"] == "X-1"
    assert added["metadata"]["last_resort_takeover_of"] == "X-1"
    assert added["metadata"]["previous_recovery_of"] == "F-1"
    # 已是接管形状仍冲突 → 不再转换（保持拒绝语义）
    assert orch._convert_conflict_to_takeover(patch, _CONFLICT_MSG) is False
    # completed 冲突（R-02）→ 不转换
    patch2 = TaskGraphPatch(add_nodes=[dict(proposal, recovery_of="")])
    msg2 = _CONFLICT_MSG.replace("X-1", "R-02")
    assert orch._convert_conflict_to_takeover(patch2, msg2) is False
    # 非冲突消息 → 不转换
    assert orch._convert_conflict_to_takeover(
        patch2, "recovery patch ignores the declared upstream root cause: x.py"
    ) is False


def test_stripped_recovery_of_gets_takeover_reattached(tmp_path, monkeypatch):
    """修复F①端到端（attempt5 live 拒绝词复刻）：提案 recovery_of=COMPLETED
    节点 → 剥离 → 裸节点与活跃 repair_pending 所有者冲突 → 旧行为三连拒；
    现在剥离后的二次接管扫描补上 recovery_of=冲突节点，首个提案即应用。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    # live 形状：COMPLETED 目标 R-02 的合同覆盖声明路径（Fix 25c 只保留
    # 目标合同内的声明——attempt5 live 中 inspection_service.py 属已完成的
    # N-001，提案 recovery_of=N-001 时声明得以保留进入 governor 冲突检查）
    orch.task_graph.get_node("R-02").owned_paths.append(
        "deliverables/app/inspection_service.py"
    )
    _conflict_node(orch, node_id="X-1", status="repair_pending")
    node = orch.task_graph.get_node("F-1")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.4,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: the service was never exercised live",
            evidence_refs=[],
        )],
    )
    orch.governor.plan_verification_recovery = lambda **_kw: TaskGraphPatch(add_nodes=[{
        "node_id": "F-1-R9",
        "goal": "re-run the check against a freshly started service",
        "task_type": "implementation",
        "required_capabilities": ["coding"],
        "dependencies": [],
        "acceptance_criteria": ["live receipts"],
        "recovery_of": "R-02",  # 非法（completed）→ 被剥离 → 裸节点
        # F2 (live b31): 除与活跃所有者冲突的那条声明外，再声明一条图上还没有
        # 的证据路径——"新增节点"本身不算进展，被剥离 recovery_of 后由接管扫描
        # 补回的首个提案要仍然算新工作，才能验"首个提案即应用"。
        "owned_paths": [
            "deliverables/app/inspection_service.py",
            "deliverables/app/tests/F-1-R9-live.log",
        ],
        "output_artifacts": [],
    }])
    monkeypatch.setattr(
        orch, "_resolve_declared_root_causes", lambda *a, **kw: {},
    )
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    result = orch._apply_governed_verification_recovery(node, verification_result)
    assert result.get("rejected") is not True, result
    assert not any(
        k == "governor.verification_recovery.rejected" for k, _ in traces
    ), "layer-① rescan should make the first proposal applicable"
    new = orch.task_graph.get_node("F-1-R9")
    assert new is not None
    assert (new.metadata or {}).get("supersedes_node_id") == "X-1"
    assert orch.task_graph.get_node("X-1").status == "superseded"
    # 单一预算单位：applied
    assert orch.runtime_state_manager.state.recovery_churn_total == 1


def test_conflict_last_resort_conversion_end_to_end(tmp_path, monkeypatch):
    """修复F②+H 端到端演进：failed F-1 与 repair_pending X-1 双所有权
    种子 + 提案 recovery_of=F-1——修复H（转移剥离预合并）使**首案即
    应用**（旧行为三连拒 → 修复F② 第三案接管转换救 → 现被 H 提前
    化解，F② 保留为 apply 冲突的第三案防火层，单元覆盖见
    test_conflict_conversion_targets_active_owner_only）。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    # live 形状：被恢复的 failed 节点 F-1 自身拥有服务文件，与活跃
    # repair_pending 的 X-1 双重所有权（Fix 23a 种子模式）。
    orch.task_graph.get_node("F-1").owned_paths.append(
        "deliverables/app/inspection_service.py"
    )
    _conflict_node(orch, node_id="X-1", status="repair_pending")
    node = orch.task_graph.get_node("F-1")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.4,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: the service was never exercised live",
            evidence_refs=[],
        )],
    )
    attempts = {"n": 0}

    def fake_planner(**_kw):
        attempts["n"] += 1
        return TaskGraphPatch(add_nodes=[{
            "node_id": f"F-1-R{attempts['n']}",
            "goal": "re-run the check against a freshly started service",
            "task_type": "implementation",
            "required_capabilities": ["coding"],
            "dependencies": [],
            "acceptance_criteria": ["live receipts"],
            "recovery_of": "F-1",  # 合法目标；冲突由转移剥离+预合并化解
            "owned_paths": ["deliverables/app/inspection_service.py"],
            "output_artifacts": [],
        }])

    orch.governor.plan_verification_recovery = fake_planner
    monkeypatch.setattr(
        orch, "_resolve_declared_root_causes", lambda *a, **kw: {},
    )
    result = orch._apply_governed_verification_recovery(node, verification_result)
    assert result.get("rejected") is not True, result
    assert attempts["n"] == 1, "transfer strip + pre-merge must rescue attempt 1"
    # 预算账目：单一 applied 单位（旧行为 2拒+1救=3 或 3拒+终判）
    assert orch.runtime_state_manager.state.recovery_churn_total == 1
    new = orch.task_graph.get_node("F-1-R1")
    assert new is not None
    assert (new.metadata or {}).get("supersedes_node_id") == "F-1"
    assert "deliverables/app/inspection_service.py" in new.owned_paths
    # X-1 不被 supersede，只失去重复所有权（剥离转移给接管节点）
    x1 = orch.task_graph.get_node("X-1")
    assert x1.status == "repair_pending"
    assert "deliverables/app/inspection_service.py" not in x1.owned_paths
    assert not (node.metadata or {}).get("_terminal_failure")


# ---------------------------------------------------------------------------
# doc 18 修复E2+F③：健康活跃所有者（attempt6 live：N-001/N-001-R2 两代
# 各烧 3 单位终判——提案反复重新声明健康 ready/verifying 节点的合同）
# ---------------------------------------------------------------------------

def test_e2_no_retarget_for_healthy_active_target(tmp_path):
    """修复E2：recovery_of 指向健康活跃节点（verifying 等）不是接管意图，
    不得重定向（attempt6 live：重定向制造跨谱系畸形，三案死于冲突）。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    orch.task_graph.get_node("R-02").status = "verifying"
    node = orch.task_graph.get_node("F-1")
    proposal = TaskGraphPatch(add_nodes=[{
        "node_id": "F-1-R1", "goal": "g", "task_type": "implementation",
        "required_capabilities": ["coding"], "dependencies": [],
        "acceptance_criteria": ["a"],
        "recovery_of": "R-02",
        "owned_paths": ["deliverables/runs/verify/receipt.json"],
    }])
    out = orch._normalize_root_cause_takeover(node, proposal, {})
    assert out == {"granted_paths": [], "retargeted_from": ""}
    assert proposal.add_nodes[0]["recovery_of"] == "R-02"  # 原样保留


def test_redundant_reshape_unit_boundaries(tmp_path):
    """修复F③单元：只重塑健康活跃所有者的冲突；空合同放弃；他类不动。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    _conflict_node(orch, node_id="X-1", status="ready")
    conflict_msg = (
        "graph patch has overlapping output ownership: N-NEW conflicts with "
        "X-1: ['owned_path:deliverables/app/inspection_service.py <> "
        "deliverables/app/inspection_service.py']"
    )
    base = {
        "node_id": "N-NEW", "goal": "g", "task_type": "implementation",
        "required_capabilities": ["coding"], "dependencies": [],
        "acceptance_criteria": ["a"],
        "owned_paths": [
            "deliverables/app/inspection_service.py",
            "deliverables/runs/verify/receipt.json",
        ],
        "output_artifacts": [],
    }
    # (a) 健康 ready 所有者 → 重塑：冲突文件声明转 metadata.expected_inputs
    # （信息性只读引用）+ 挂依赖
    patch = TaskGraphPatch(add_nodes=[dict(base)])
    assert orch._convert_redundant_to_dependent(patch, conflict_msg) is True
    reshaped = patch.add_nodes[0]
    assert reshaped["owned_paths"] == ["deliverables/runs/verify/receipt.json"]
    assert "deliverables/app/inspection_service.py" in (
        reshaped["metadata"]["expected_inputs"]
    )
    assert reshaped["dependencies"] == ["X-1"]
    # 空合同（全部声明冲突）→ 放弃重塑
    only_conflict = TaskGraphPatch(add_nodes=[dict(
        base, owned_paths=["deliverables/app/inspection_service.py"],
    )])
    assert orch._convert_redundant_to_dependent(only_conflict, conflict_msg) is False
    # 可接管目标（repair_pending）→ 修复F②管辖，不重塑
    orch.task_graph.get_node("X-1").status = "repair_pending"
    takeoverable = TaskGraphPatch(add_nodes=[dict(base)])
    assert orch._convert_redundant_to_dependent(takeoverable, conflict_msg) is False
    # completed 所有者 → stripping/修复E管辖
    orch.task_graph.get_node("X-1").status = "completed"
    inactive = TaskGraphPatch(add_nodes=[dict(base)])
    assert orch._convert_redundant_to_dependent(inactive, conflict_msg) is False
    # 非匹配消息 → 不动
    orch.task_graph.get_node("X-1").status = "ready"
    other = TaskGraphPatch(add_nodes=[dict(base)])
    assert orch._convert_redundant_to_dependent(
        other, "recovery patch ignores the declared upstream root cause"
    ) is False


def test_double_ownership_seed_resolved_by_transfer_premerge(tmp_path, monkeypatch):
    """修复H 端到端：failed F-1 与健康 ready X-1 双所有权种子——接管
    提案（recovery_of=F-1，声明目标合同内的冲突文件）经转移 pass 剥离
    X-1 的重复所有权 + governor 预合并后，**首个提案即应用**（旧行为：
    剥离对 add 验证不可见，三连拒终判——attempt6 N-001 谱系烧 7 单位
    的确切机制）。健康 X-1 不被 supersede，只失去重复文件。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    orch.task_graph.get_node("F-1").owned_paths.append(
        "deliverables/app/inspection_service.py"
    )
    _conflict_node(orch, node_id="X-1", status="ready")
    node = orch.task_graph.get_node("F-1")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.4,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: the service was never exercised live",
            evidence_refs=[],
        )],
    )
    attempts = {"n": 0}

    def fake_planner(**_kw):
        attempts["n"] += 1
        return TaskGraphPatch(add_nodes=[{
            "node_id": f"F-1-R{attempts['n']}",
            "goal": "re-run the check against a freshly started service",
            "task_type": "implementation",
            "required_capabilities": ["coding"],
            "dependencies": [],
            "acceptance_criteria": ["live receipts"],
            "recovery_of": "F-1",
            "owned_paths": [
                "deliverables/app/inspection_service.py",
                f"deliverables/runs/verify/receipt_r{attempts['n']}.json",
            ],
            "output_artifacts": [],
        }])

    orch.governor.plan_verification_recovery = fake_planner
    monkeypatch.setattr(orch, "_resolve_declared_root_causes", lambda *a, **kw: {})
    result = orch._apply_governed_verification_recovery(node, verification_result)
    assert result.get("rejected") is not True, result
    assert attempts["n"] == 1, "transfer strip + pre-merge must rescue attempt 1"
    assert orch.runtime_state_manager.state.recovery_churn_total == 1
    new = orch.task_graph.get_node("F-1-R1")
    assert new is not None
    assert "deliverables/app/inspection_service.py" in new.owned_paths
    x1 = orch.task_graph.get_node("X-1")
    assert x1.status == "ready"  # 健康所有者不被 supersede
    assert "deliverables/app/inspection_service.py" not in x1.owned_paths  # 重复所有权已剥离


def test_healthy_owner_conflict_last_resort_reshape(tmp_path, monkeypatch):
    """修复F③(a) 端到端：冲突文件只属健康 ready 节点、提案无 recovery_of
    且无线谱命名（转移规范化/23a/25b 全都够不着的形状）——前两拒保留
    反馈，第三次重塑为"依赖健康所有者+只读引用"后应用。健康节点不被
    supersede（Round 12 原则）。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    _conflict_node(orch, node_id="X-1", status="ready")
    node = orch.task_graph.get_node("F-1")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.4,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: the service was never exercised live",
            evidence_refs=[],
        )],
    )
    attempts = {"n": 0}

    def fake_planner(**_kw):
        attempts["n"] += 1
        return TaskGraphPatch(add_nodes=[{
            "node_id": f"CHECK-REDO-{attempts['n']}",
            "goal": "re-run the check against a freshly started service",
            "task_type": "implementation",
            "required_capabilities": ["coding"],
            "dependencies": [],
            "acceptance_criteria": ["live receipts"],
            "owned_paths": [
                "deliverables/app/inspection_service.py",
                f"deliverables/runs/verify/receipt_r{attempts['n']}.json",
            ],
            "output_artifacts": [],
        }])

    orch.governor.plan_verification_recovery = fake_planner
    monkeypatch.setattr(orch, "_resolve_declared_root_causes", lambda *a, **kw: {})
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    result = orch._apply_governed_verification_recovery(node, verification_result)
    assert result.get("rejected") is not True, result
    assert attempts["n"] == 3
    assert orch.runtime_state_manager.state.recovery_churn_total == 3
    assert any(
        k == "governor.verification_recovery.redundant_conflict_reshaped"
        for k, _ in traces
    )
    # 健康所有者安然无恙（不被 supersede）
    assert orch.task_graph.get_node("X-1").status == "ready"
    new = orch.task_graph.get_node("CHECK-REDO-3")
    assert new is not None
    assert "deliverables/app/inspection_service.py" not in new.owned_paths
    assert "deliverables/runs/verify/receipt_r3.json" in new.owned_paths
    assert "X-1" in new.dependencies
    assert "deliverables/app/inspection_service.py" in (
        (new.metadata or {}).get("expected_inputs") or []
    )
    assert not (node.metadata or {}).get("_terminal_failure")


def test_invalid_recovery_target_healthy_reshaped(tmp_path, monkeypatch):
    """修复F③(b) 端到端（attempt6 live 主形状）：提案要求接管健康
    verifying/ready 节点（recovery_of=健康节点）→ governor "recovery
    target must be failed..." 三连拒；E2 保证不再错误重定向；第三次
    重塑：摘除非法 recovery_of、目标合同转只读引用、挂依赖真生产者。"""
    from AstralNexus.core.governor import TaskGraphPatch

    orch, _patch, _resolved = _seam_graph(tmp_path)
    _conflict_node(orch, node_id="X-1", status="ready")
    node = orch.task_graph.get_node("F-1")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.4,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: the client check never ran live",
            evidence_refs=[],
        )],
    )
    attempts = {"n": 0}

    def fake_planner(**_kw):
        attempts["n"] += 1
        return TaskGraphPatch(add_nodes=[{
            "node_id": f"F-1-V{attempts['n']}",
            "goal": "replace the stuck client-check worker and finish the job",
            "task_type": "implementation",
            "required_capabilities": ["coding"],
            "dependencies": [],
            "acceptance_criteria": ["live receipts"],
            "recovery_of": "X-1",  # 健康 ready 节点——非法接管目标
            "owned_paths": [f"deliverables/runs/verify/v{attempts['n']}.json"],
            "output_artifacts": [],
        }])

    orch.governor.plan_verification_recovery = fake_planner
    monkeypatch.setattr(orch, "_resolve_declared_root_causes", lambda *a, **kw: {})
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    result = orch._apply_governed_verification_recovery(node, verification_result)
    assert result.get("rejected") is not True, result
    assert attempts["n"] == 3
    assert orch.runtime_state_manager.state.recovery_churn_total == 3
    assert any(
        k == "governor.verification_recovery.redundant_conflict_reshaped"
        and v.get("shape") == "invalid_recovery_target"
        for k, v in traces
    )
    assert orch.task_graph.get_node("X-1").status == "ready"
    new = orch.task_graph.get_node("F-1-V3")
    assert new is not None
    assert (new.metadata or {}).get("supersedes_node_id") in (None, "")
    assert "X-1" in new.dependencies
    # 健康目标合同转为只读引用（metadata 信息通道，持久化在节点上）
    assert "deliverables/app/inspection_service.py" in (
        (new.metadata or {}).get("expected_inputs") or []
    )
    assert not (node.metadata or {}).get("_terminal_failure")
