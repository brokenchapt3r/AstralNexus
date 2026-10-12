"""doc 18 attempt3 针对性修复的反转测试（全部源自真实 API 运行证据）：

①孤儿失败再驱动：宿主异常拆机把失败节点（R-001-R）遗弃在图里，其
  admitted 依赖者（R-002-R/R-003-R）永不就绪，重规划提案以 admitted 为
  目标全部验证器无效 → 同指纹无变化 ×3 熔断。修复：停滞路径先经标准
  受治理恢复通道再驱动孤儿失败。
②熔断停机码显式化：两条"无进展"熔断路径必须落闭合停机码
  no_progress_breaker（人工核查类），不得依赖 CLI 兜底推导。
③终态语义保真：run() 已返回 blocked/interrupted 后，收尾阶段的宿主
  异常不得用 host_exception 改写主停机码（attempt3 实测：熔断停机在
  DB 里被记成可自动恢复的 host_exception，普通 --resume 会重入同一
  停滞图景空烧 API）。
④受治理故障恢复的提案/执行计入 run 级 churn 预算。
"""
from types import SimpleNamespace

import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.runtime.llm_client import LLMClientError


def _graph_with_orphaned_failure(orchestrator):
    orchestrator.task_graph.add_node({
        "node_id": "A", "goal": "g", "role": "coder", "status": "failed",
    })
    orchestrator.task_graph.add_node({
        "node_id": "B", "goal": "g2", "role": "tester", "status": "admitted",
        "dependencies": ["A"],
    })


def test_orphaned_failure_is_redriven_into_governed_recovery(tmp_path, monkeypatch):
    orchestrator = Orchestrator(tmp_path, run_id="orphan-1")
    _graph_with_orphaned_failure(orchestrator)
    calls = []
    monkeypatch.setattr(
        orchestrator, "_attempt_recovery",
        lambda node_id: calls.append(node_id) or True,
    )
    assert orchestrator._recover_orphaned_failures() is True
    assert calls == ["A"]


def test_orphaned_failure_skips_terminal_and_recovered_nodes(tmp_path, monkeypatch):
    orchestrator = Orchestrator(tmp_path, run_id="orphan-2")
    _graph_with_orphaned_failure(orchestrator)
    calls = []
    monkeypatch.setattr(
        orchestrator, "_attempt_recovery",
        lambda node_id: calls.append(node_id) or True,
    )
    # 终态失败标记：不再驱动
    orchestrator.task_graph.get_node("A").metadata["_terminal_failure"] = True
    assert orchestrator._recover_orphaned_failures() is False
    assert calls == []
    # 在世修复后继（recovery_of=A）：不再驱动
    orchestrator.task_graph.get_node("A").metadata.pop("_terminal_failure")
    orchestrator.task_graph.add_node({
        "node_id": "A-R1", "goal": "g", "role": "coder", "status": "ready",
        "metadata": {"recovery_of": "A"},
    })
    assert orchestrator._recover_orphaned_failures() is False
    assert calls == []


def test_orphaned_recovery_llm_error_bubbles_node_error_swallowed(tmp_path, monkeypatch):
    orchestrator = Orchestrator(tmp_path, run_id="orphan-3")
    _graph_with_orphaned_failure(orchestrator)

    def raise_llm(node_id):
        raise LLMClientError("provider circuit open after 3 retries")

    monkeypatch.setattr(orchestrator, "_attempt_recovery", raise_llm)
    # 宿主级 LLM 异常必须冒泡（→ blocked+host_exception 可恢复语义）
    with pytest.raises(LLMClientError):
        orchestrator._recover_orphaned_failures()

    def raise_node(node_id):
        raise ValueError("recovery target invalid")

    monkeypatch.setattr(orchestrator, "_attempt_recovery", raise_node)
    # 节点级恢复失败留在本层：不冒泡、不阻塞其余孤儿处理
    assert orchestrator._recover_orphaned_failures() is False


def test_stalled_recovery_breaker_sets_closed_stop_code(tmp_path, monkeypatch):
    """attempt3 实测反转：同指纹无变化重规划 ×3 → 熔断必须携带闭合停机码
    no_progress_breaker（人工核查类，普通 --resume 结构性拒绝）。"""
    orchestrator = Orchestrator(tmp_path, run_id="breaker-1", max_iterations=0)
    orchestrator.task_graph.add_node({
        "node_id": "B", "goal": "g", "role": "tester", "status": "admitted",
        "dependencies": ["MISSING-X"],
    })
    monkeypatch.setattr(
        orchestrator, "_replan_stalled_graph",
        lambda **kwargs: {"changed": False, "repeat_count": 3},
    )
    result = orchestrator.run(goal="small service")
    assert result["status"] == "blocked"
    assert result["stop_code"] == "no_progress_breaker"
    assert "same autonomous recovery outcome repeated three times" in (
        result["stop_reason"]
    )


def test_governed_fault_invalid_proposal_consumes_churn(tmp_path, monkeypatch):
    orchestrator = Orchestrator(tmp_path, run_id="churn-gf", recovery_churn_budget=20)
    orchestrator.task_graph.add_node({
        "node_id": "F-1", "goal": "g", "role": "coder", "status": "failed",
    })

    def bad_plan(*args, **kwargs):
        raise ValueError("invalid recovery JSON")

    monkeypatch.setattr(orchestrator.governor, "plan_fault_recovery", bad_plan)
    fault = orchestrator.fault_detector.detect_node_failure("F-1", "execution failed")
    result = orchestrator._execute_governed_fault(fault)
    assert result["success"] is False
    assert orchestrator.runtime_state_manager.state.recovery_churn_total == 1


def test_finalize_host_error_does_not_overwrite_primary_stop_code(tmp_path, monkeypatch):
    """attempt3 实测反转：熔断停机后收尾 GlobalGovernor 调用又遇熔断 →
    DB 里 stop_code 被 host_exception 覆盖。修复后主停机码必须存活，
    宿主错误降为 stop_reason 附加信息。"""
    from AstralNexus.cli import main as cli_main
    from AstralNexus.runtime.run_coordinator import RunCoordinator

    run_dir = tmp_path / "run"
    task_file = tmp_path / "task.txt"
    task_file.write_text("implement a small inspection service", encoding="utf-8")

    class FakeTrace:
        def record(self, *args, **kwargs):
            return None

    class FakeGovernor:
        def derive_delivery_contract(self, *args, **kwargs):
            return None

    class FakeOrchestrator:
        def attach_interrupt_control(self, event, queue):
            self.interrupt_event = event

        def shutdown(self):
            return None

        def __init__(self, **kwargs):
            from AstralNexus.core.task_graph import TaskGraph
            self.task_graph = TaskGraph()
            self.checkpoint_sink = kwargs.get('checkpoint_sink')
            self.trace_recorder = FakeTrace()
            self.governor = FakeGovernor()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def enable_real_llm_worker_matching(self, config):
            return None

        def register_staged_inputs(self, staged):
            return None

        def run(self, **kwargs):
            return {
                "status": "blocked",
                "stop_code": "no_progress_breaker",
                "stop_reason": (
                    "same autonomous recovery outcome repeated three times"
                ),
                "iterations": 3,
            }

    monkeypatch.setattr(cli_main, "Orchestrator", FakeOrchestrator)

    def broken_checkpoint(self, lease, payload):
        raise RuntimeError("provider circuit open during finalize")

    monkeypatch.setattr(RunCoordinator, "record_checkpoint", broken_checkpoint)

    args = SimpleNamespace(
        task=str(task_file), output=str(run_dir), run_dir=str(run_dir),
        model="test-model", provider="test", max_parallel=1, config=None,
    )
    code = cli_main._run_task(args, tmp_path / "home")
    assert code == 1
    coordinator = RunCoordinator(run_dir, run_id=run_dir.name)
    row = coordinator.current()
    assert row["status"] == "blocked"
    # 主停机码存活：人工核查类，不在自动恢复白名单
    assert row["stop_code"] == "no_progress_breaker"
    assert "finalize_host_error" in row["stop_reason"]
    assert "same autonomous recovery outcome" in row["stop_reason"]
    # 政策核验：普通自动恢复被结构性拒绝（需 --user-resume 人工核查）
    assert coordinator.acquire("monitor-auto") is None
