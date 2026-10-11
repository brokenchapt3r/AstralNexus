"""doc 18 attempt2 churn 修复：run 级单调恢复预算（DeerFlow 2814bd5d 对照 #1/#4/#6）。

真实 API 短程闭环 attempt2 证据：37 分钟 / 166 请求 / 节点 7→20 不收敛。
既有熔断全部以"无变化的重复"为键：repeated_recovery 任何图变化清零、
谱系深度上限（6 代）可被豁免配方与重规划重新 rooting 绕过、主循环熔断
只看 changed=False——"永远在变化"的 churn 对其全部不可见。

DeerFlow 语义：续跑帽挂在同一 goal/run 身份的单调计数器上，触顶即终态
stand-down（capped run 永不派生新 run），人工门（用户消息/HITL）才授予
新一轮。本文件验证天枢的对齐实现：
- recovery_churn_total 单调累计（恢复补丁应用/拒绝、停滞重规划计入）；
- 触顶 → run blocked + stop_code=no_progress_breaker（闭合枚举 → 自动
  恢复被拒）；
- 计数经 checkpoint/联合提交游标持久化，重建后触顶标志恢复；
- 显式 --user-resume 授予新预算（审计入账）；自动恢复被结构性拒绝。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.runtime_state import RuntimeStateManager


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 14, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _coord(tmp_path, clock, run_id):
    from AstralNexus.runtime.run_coordinator import RunCoordinator

    return RunCoordinator(
        tmp_path, run_id=run_id, lease_seconds=120, grace_seconds=10, now_fn=clock,
    )


def _make_orchestrator(run_dir, **kwargs):
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        run_dir=run_dir,
        graph_store=SQLiteTaskGraphStore(run_dir / "task_graph.sqlite"),
        **kwargs,
    )


def test_record_churn_is_monotonic_and_immune_to_change_resets(tmp_path):
    manager = RuntimeStateManager(tmp_path, "churn-1")
    assert manager.record_churn("verification_recovery_applied") == 1
    # "有变化"的恢复会清零 repeated_recovery——但 churn 计数不清零
    manager.record_recovery("fingerprint-a", changed=True)
    assert manager.state.repeated_recovery == 0
    assert manager.record_churn("stalled_replan_changed") == 2
    assert manager.record_churn("verification_recovery_rejected") == 3
    # 持久化往返
    reloaded = RuntimeStateManager(tmp_path, "churn-1")
    assert reloaded.state.recovery_churn_total == 3


def test_churn_budget_exhaustion_blocks_run_with_closed_stop_code(tmp_path):
    orchestrator = Orchestrator(
        tmp_path, run_id="churn-run", max_iterations=0, recovery_churn_budget=2,
    )
    # 预置多节点非 root 种子图：跳过规划 LLM，直接进入主循环
    orchestrator.task_graph.add_node({
        "node_id": "T-1", "goal": "g", "role": "coder", "status": "completed",
    })
    orchestrator.task_graph.add_node({
        "node_id": "T-2", "goal": "g2", "role": "tester", "status": "ready",
    })
    assert orchestrator._recovery_budget_exhausted is False
    orchestrator._consume_recovery_churn("verification_recovery_applied")
    assert orchestrator._recovery_budget_exhausted is False
    orchestrator._consume_recovery_churn("stalled_replan_changed")
    assert orchestrator._recovery_budget_exhausted is True

    result = orchestrator.run(goal="implement a small service")
    assert result["status"] == "blocked"
    assert result["stop_code"] == "no_progress_breaker"
    assert "churn budget exhausted" in result["stop_reason"]
    # T-2 未被执行（帽在选择任务之前生效）
    assert orchestrator.task_graph.get_node("T-2").status == "ready"


def test_churn_total_survives_checkpoint_and_rearms_flag_on_rebuild(tmp_path):
    first = Orchestrator(
        tmp_path, run_id="churn-persist", max_iterations=1, recovery_churn_budget=5,
    )
    first.task_graph.add_node({
        "node_id": "T-1", "goal": "g", "role": "coder", "status": "completed",
    })
    first._consume_recovery_churn("verification_recovery_applied")
    first._consume_recovery_churn("verification_recovery_rejected")
    first._save_checkpoint()

    # 预算未触顶：重建后计数恢复、标志为 False
    second = Orchestrator(
        tmp_path, run_id="churn-persist", max_iterations=1, recovery_churn_budget=5,
    )
    assert second.runtime_state_manager.state.recovery_churn_total == 2
    assert second._recovery_budget_exhausted is False
    # 预算触顶重建（人工调低预算等场景）：init 即恢复触顶标志
    third = Orchestrator(
        tmp_path, run_id="churn-persist", max_iterations=1, recovery_churn_budget=2,
    )
    assert third._recovery_budget_exhausted is True


def _churn_blocked_run(tmp_path, run_dir_name, budget=2):
    """构造预算触顶后 blocked 停机的 run（带围栏游标 + 闭合停机码）。"""
    from AstralNexus.core.governor import GoalSpec

    clock = Clock()
    run_dir = tmp_path / run_dir_name
    coord = _coord(run_dir, clock, run_id=run_dir_name)
    lease = coord.acquire("owner-A")
    orch = _make_orchestrator(
        run_dir,
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
    assert orch._recovery_budget_exhausted is True
    assert orch._persist_graph_checkpoint(stage="wave_churn") is True
    # 真实停机路径：CLI 把 blocked 结果落成闭合停机码
    assert coord.finish(
        lease, "blocked",
        stop_reason="autonomous recovery churn budget exhausted",
        stop_code="no_progress_breaker",
    ) is True
    return run_dir, coord, budget


def _resume_args(tmp_path, run_dir):
    task_file = tmp_path / "task.txt"
    if not task_file.exists():
        task_file.write_text("continue the service work", encoding="utf-8")
    return SimpleNamespace(
        run_dir=str(run_dir), task=str(task_file), output=str(run_dir),
        model="test-model", provider="test", max_parallel=1, config=None,
    )


def test_auto_resume_structurally_rejected_for_churn_blocked_run(tmp_path):
    """churn 触顶停机（no_progress_breaker）不在自动可恢复白名单：
    监控/套件的普通 --resume 被准入层结构性拒绝，run 状态零写入。"""
    from AstralNexus.cli.main import _resume_task

    run_dir, coord, budget = _churn_blocked_run(tmp_path, "run-auto")
    assert coord.acquire("monitor-auto") is None  # 协调器层拒绝

    captured = {}

    def on_orch(orchestrator, _run_dir):
        captured["orch"] = orchestrator

    args = _resume_args(tmp_path, run_dir)
    args.user_resume = False
    code = _resume_task(args, tmp_path / "home", on_orchestrator=on_orch)
    assert code == 2  # CLI 统一入口拒绝，未触碰 orchestrator
    assert captured == {}
    assert coord.current()["status"] == "blocked"  # 停机依据原样保留


def test_user_resume_resets_churn_budget_with_audit(tmp_path):
    """显式人工恢复 = 核查通过 → 授予新预算（审计入账）；授权来源、
    原停机码、代数变迁全部落库。"""
    from AstralNexus.cli.main import _resume_task

    run_dir, coord, budget = _churn_blocked_run(tmp_path, "run-user")
    captured = {}

    def on_orch(orchestrator, _run_dir):
        captured["orch"] = orchestrator
        orchestrator.run = lambda *a, **kw: {
            "status": "completed", "iterations": 1, "stop_reason": "",
        }

    args = _resume_args(tmp_path, run_dir)
    args.user_resume = True
    code = _resume_task(args, tmp_path / "home", on_orchestrator=on_orch)
    assert code == 0  # No new outputs: presentation is not required.
    orch = captured["orch"]
    # 人工核查授权：churn 预算清零、触顶标志解除
    assert orch.runtime_state_manager.state.recovery_churn_total == 0
    assert orch._recovery_budget_exhausted is False
    # 审计：授权来源 + 原停机码 + 代数变迁
    audit = coord.last_resume_audit()
    assert audit is not None
    assert audit["source_stop_code"] == "no_progress_breaker"
    assert audit["source_status"] == "blocked"
    assert audit["new_generation"] == int(audit["source_generation"]) + 1
    assert coord.current()["status"] == "completed"
    assert coord.current()["stop_code"] == "completed"
