"""Fix AZ：传输中断的放弃预算必须衡量"节点是否卡住"，而不是生命周期累计次数。

实测缺陷（r13b gen30，2026-09-17 21:16:57 本地）：

* `PATCH-R017-FINAL-ACCEPTANCE-REPAIR` 已成功完成 **111 个 tool_round**
  （第 104 轮刚把服务拉起并 READY）；
* 第 **4** 次传输中断把节点判为 ``FAILED``（``transport_interrupt_budget_exhausted``），
  因为计数是 ``node.metadata["transport_interrupts"]`` 的**生命周期累计**、默认预算只有 3；
* 治理恢复随即下药 ``reassign_with_resource_tier_escalation``（edge-1 → local/Testing）
  —— 换工作器与资源层级**治不了网络抖动**；
* 执行从 ``tool_round 1`` 重来（实测 13:17:30 reassign 后 13:31:40 才回到第 18 轮），
  数小时 LLM 工作 + 20 份结构 churn 中的 1 份就此白费。
* 全 trace 归因：30 次 ``recovery.attempted`` 中 **19 次是传输类**
  （urlerror 8 / timeouterror 5 / providercircuitopen 4 / llmclienterror 2）；
  ``llm.stream.failed`` 累计 **65** 次（TimeoutError 41 / URLError 16 / …）。

本组用例**驱动真实的 `Orchestrator._run_agent_task`**。既有的
`tests/test_transport_interrupt_not_node_failure.py` 只是把预算规则在本地重写了一遍
（`_disposition`），既不驱动真实代码，也不可能发现这个缺陷——这正是本轮要避免的假信心。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskNode

NODE_ID = "PATCH-R999-TRANSPORT-BUDGET"


class _InterruptAgent:
    """每次执行都返回 INTERRUPTED，并声明本轮已完成的 tool_round 数。"""

    role = "tester"
    name = "tester"
    selected_model = "test-model"
    worker_profile: dict = {}
    input_contract = ""
    output_contract = ""
    context_policy: dict = {}
    default_tools = ["python_exec"]

    def __init__(self, tool_rounds: int) -> None:
        self.tool_rounds = tool_rounds

    def execute(self, context):  # noqa: ANN001, ANN201 - 形状与既有测试夹具一致
        return AgentResponse(
            outcome=ExecutionOutcome.INTERRUPTED,
            content="Node execution interrupted; durable state is ready for resume.",
            metrics={
                "runtime_status": "interrupted",
                "execution_path": "runtime_transport_interrupted",
                "transport_interrupted": True,
                "error_type": "TimeoutError",
                "error": "The read operation timed out",
                "request_stats": {"tool_rounds": self.tool_rounds},
            },
        )


def _interrupt_events(tmp_path: Path) -> list[dict[str, Any]]:
    """从真实落盘的 trace 里读事件（不是拦截内存调用）。"""
    found: list[dict[str, Any]] = []
    for path in sorted(tmp_path.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event_type") == "node.transport_interrupted":
                found.append(event.get("payload") or {})
    return found


def _execute(tmp_path: Path, *, tool_rounds: int, prior_interrupts: int):
    """真实跑一次执行，返回 (返回值, node, 中断事件列表)。"""
    with Orchestrator(tmp_path, run_id="fix-az") as orchestrator:
        node = TaskNode(node_id=NODE_ID, goal="Final acceptance repair", role="tester", status="running")
        node.metadata["transport_interrupts"] = prior_interrupts
        orchestrator.task_graph.add_node(node)
        result = orchestrator._run_agent_task(_InterruptAgent(tool_rounds), node)
        events = _interrupt_events(tmp_path)
        return result, node, events


def test_interrupt_after_completed_work_does_not_consume_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心回归：已经跑到预算上限的节点，只要本轮有完成的工作，就不该死。"""
    monkeypatch.delenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", raising=False)
    result, node, events = _execute(tmp_path, tool_rounds=111, prior_interrupts=3)

    assert result is None, "有进展的中断必须回到 ready（None），而不是 False（失败）"
    assert node.metadata.get("error_type") != "transport_interrupt_budget_exhausted"
    assert node.metadata["transport_interrupts"] == 0, "有进展即清零，不能继续累计成死刑"
    assert events, "必须留下可审计的 node.transport_interrupted 事件"
    payload = events[-1]
    assert payload["disposition"] == "progress_observed"
    assert payload["progress_rounds"] == 111


def test_interrupt_without_progress_still_exhausts_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不放松：一次完整轮次都做不成的持续中断，仍必须耗尽预算并按失败处理。"""
    monkeypatch.delenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", raising=False)
    result, node, events = _execute(tmp_path, tool_rounds=0, prior_interrupts=3)

    assert result is False
    assert node.metadata["error_type"] == "transport_interrupt_budget_exhausted"
    assert node.metadata["transport_interrupts"] == 4
    assert events[-1]["disposition"] == "budget_exhausted"
    assert events[-1]["progress_rounds"] == 0


def test_no_progress_interrupts_accumulate_up_to_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算本身仍然有界：默认 3 次容忍，第 4 次才转失败。"""
    monkeypatch.delenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", raising=False)
    seen = []
    for attempt in (1, 2, 3):
        result, node, events = _execute(tmp_path / f"a{attempt}", tool_rounds=0, prior_interrupts=attempt - 1)
        assert result is None, f"第 {attempt} 次应仍在容忍范围内"
        assert node.metadata["transport_interrupts"] == attempt
        seen.append(events[-1]["disposition"])
    assert seen == ["requeued"] * 3


def test_progress_resets_the_streak_instead_of_decrementing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清零语义：连续两次无进展后出现一次有进展，下一次无进展必须从 1 重新数。"""
    monkeypatch.delenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", raising=False)
    _, node, _ = _execute(tmp_path / "streak", tool_rounds=0, prior_interrupts=2)
    assert node.metadata["transport_interrupts"] == 3

    _, node2, ev2 = _execute(tmp_path / "progress", tool_rounds=7, prior_interrupts=3)
    assert node2.metadata["transport_interrupts"] == 0
    assert ev2[-1]["disposition"] == "progress_observed"

    _, node3, ev3 = _execute(tmp_path / "after", tool_rounds=0, prior_interrupts=0)
    assert node3.metadata["transport_interrupts"] == 1, "清零后应从 1 重新累计（而非递减）"
    assert ev3[-1]["disposition"] == "requeued"


def test_zero_budget_disables_the_progress_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运维把预算显式设为 0 = 零容忍，此时即使有进展也按失败处理（意图优先）。"""
    monkeypatch.setenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", "0")
    result, node, events = _execute(tmp_path, tool_rounds=50, prior_interrupts=0)

    assert result is False
    assert node.metadata["error_type"] == "transport_interrupt_budget_exhausted"
    assert events[-1]["disposition"] == "budget_exhausted"


def test_missing_or_malformed_request_stats_is_treated_as_no_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取不到进展证据时保守处理：按无进展计入预算，而不是乐观豁免。"""
    monkeypatch.delenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", raising=False)

    class _NoStatsAgent(_InterruptAgent):
        def execute(self, context):  # noqa: ANN001, ANN201
            response = super().execute(context)
            response.metrics.pop("request_stats", None)
            return response

    with Orchestrator(tmp_path, run_id="fix-az-nostats") as orchestrator:
        node = TaskNode(node_id=NODE_ID, goal="g", role="tester", status="running")
        node.metadata["transport_interrupts"] = 3
        orchestrator.task_graph.add_node(node)
        result = orchestrator._run_agent_task(_NoStatsAgent(0), node)

    assert result is False
    assert node.metadata["transport_interrupts"] == 4