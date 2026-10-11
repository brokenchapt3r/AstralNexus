"""Fix AC：规划恢复时的传输故障不得打死整条运行，也不得消耗 churn 预算。

实测来源（doc 18, r13b, 2026-09-15）：generation 8 与 9 都以 host_exception 结束。
崩溃点不在流式路径，而在 Governor 的故障恢复规划调用（非流式 chat）：节点失败后
连续四次 "SSL: UNEXPECTED_EOF_WHILE_READING"，异常逃出 `_execute_governed_fault`
的 `except ValueError`，orchestrator 整体退出——一个已完成 8 个节点的健康运行，
被一次瞬时 provider 抖动整个葬送。该 handler 的注释本就写着 "do not crash the
whole run"，只是没覆盖传输类异常。

churn 口径：预算约束的是**结构性**恢复/重规划循环；"规划器连不上"属于基础设施，
不是循环。把它计入 churn 正是同一运行早些时候预算被烧干（20/20）的原因。
"""
from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone
from urllib.error import URLError

import pytest

from AstralNexus.core.fault_recovery import Fault
from AstralNexus.runtime.llm_client import LLMClientError


def _fault(node_id: str) -> Fault:
    return Fault(
        fault_id=f"fault:node_failure:{node_id}:0",
        fault_type="node_failure",
        target=node_id,
        payload={"node_id": node_id, "error": "execution failed"},
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _orchestrator(tmp_path, run_id):
    from AstralNexus.core.orchestrator import Orchestrator

    return Orchestrator(tmp_path, run_id=run_id)


def _events(orchestrator, kind):
    return [e for e in orchestrator.trace_recorder.events if e["event_type"] == kind]


@pytest.mark.parametrize("exc", [
    LLMClientError("Network error from LLM API after 30.0s: "
                   "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] ...>"),
    URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")),
    socket.timeout("read timed out"),
    ConnectionResetError("connection reset by peer"),
])
def test_planner_transport_failure_does_not_crash_the_run(tmp_path, exc):
    with _orchestrator(tmp_path, "planner-transport") as orchestrator:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])

        def boom(*args, **kwargs):
            raise exc

        orchestrator.governor.plan_fault_recovery = boom
        before = int(orchestrator.runtime_state_manager.state.recovery_churn_total or 0)

        result = orchestrator._execute_governed_fault(
            _fault(root_id), impact={"impacted_nodes": [root_id]}
        )

        assert result["success"] is False
        assert result["plan"]["strategy"] == "planner_unavailable"
        assert result["plan"]["actions"] == []
        recorded = _events(orchestrator, "governor.fault_recovery.planner_unavailable")
        assert recorded, "必须留下可审计的规划器不可用事件"
        assert recorded[-1]["payload"]["churn_consumed"] is False
        # 关键不变量：基础设施故障不吃结构性恢复预算
        after = int(orchestrator.runtime_state_manager.state.recovery_churn_total or 0)
        assert after == before, "传输故障不得消耗 recovery churn"
        assert orchestrator._recovery_budget_exhausted is False


def test_invalid_plan_still_consumes_churn_and_is_reported_separately(tmp_path):
    """Fix AC 不得削弱原有的畸形提案路径：它仍是结构性事件，仍计预算。"""
    with _orchestrator(tmp_path, "planner-invalid") as orchestrator:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])

        def bad_json(*args, **kwargs):
            raise ValueError("Governor returned malformed recovery JSON")

        orchestrator.governor.plan_fault_recovery = bad_json
        before = int(orchestrator.runtime_state_manager.state.recovery_churn_total or 0)

        result = orchestrator._execute_governed_fault(
            _fault(root_id), impact={"impacted_nodes": [root_id]}
        )

        assert result["success"] is False
        assert result["plan"]["strategy"] == "invalid_proposal"
        assert _events(orchestrator, "governor.fault_recovery.invalid_proposal")
        assert not _events(orchestrator, "governor.fault_recovery.planner_unavailable")
        after = int(orchestrator.runtime_state_manager.state.recovery_churn_total or 0)
        assert after == before + 1, "畸形提案仍是结构性恢复事件"
