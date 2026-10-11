"""第 4 步接线：真实取数点 → 谱系账本 → 两种停机判断。

取数点（`core/orchestrator.py`）：
* `_record_node_runtime_event` —— LLM 请求 / Token / 工具调用的唯一咽喉；
* `_consume_recovery_churn` —— 恢复次数；
* `_finalize_node_evidence` —— 进展（成立项 + 缺口）；
* `_lineage_stand_down_verdict` —— 停机判据（两种，互不冒充）。
"""
from __future__ import annotations


def _orchestrator(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.runtime_state import RuntimeStateManager
    from AstralNexus.core.task_graph import TaskGraph, TaskNode
    from AstralNexus.runtime.trace import TraceRecorder

    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="P-104", goal="g", role="tester", status="running"))
    graph.add_node(TaskNode(node_id="P-111", goal="g", role="tester", status="running"))
    graph.get_node("P-111").metadata["recovery_of"] = "P-104"

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    orchestrator.run_dir = tmp_path
    orchestrator.runtime_state_manager = RuntimeStateManager(tmp_path, "lineage-wiring")
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    orchestrator.recovery_churn_budget = 3
    # 真构造器会就地初始化的两个字段：桩必须与**被测代码用到的字段**一致。
    orchestrator._node_activity_types = {}
    orchestrator._churn_progress_marker = None
    return orchestrator


def _emit(orchestrator, node_id, event_type, **payload):
    event = {"event_type": event_type, **payload}
    orchestrator._record_node_runtime_event(node_id, "tester", event)


def test_llm_requests_and_tokens_are_charged_to_the_lineage_root(tmp_path):
    """接管节点（P-111 recovery_of=P-104）的用量累到**同一个根**上。"""
    orchestrator = _orchestrator(tmp_path)

    _emit(orchestrator, "P-104", "llm.request")
    _emit(orchestrator, "P-111", "llm.request")
    _emit(orchestrator, "P-111", "llm.response",
          metrics={"prompt_tokens": 120, "completion_tokens": 30})

    entry = orchestrator.runtime_state_manager.lineage_entry("P-104")

    assert entry["requests"] == 2          # 两代都算在同一根上
    assert entry["tokens"] == 150
    assert orchestrator.runtime_state_manager.lineage_entry("P-111")["requests"] == 0


def test_tool_calls_are_counted_once_per_batch(tmp_path):
    """同一批工具的两个视角只计一次（``tool_batch`` 不再重复计）。"""
    orchestrator = _orchestrator(tmp_path)

    calls = [{"name": "file_write"}, {"name": "python_exec"}]
    _emit(orchestrator, "P-104", "llm.tool_execution.done", calls=calls)
    _emit(orchestrator, "P-104", "llm.tool_batch", calls=calls)

    assert orchestrator.runtime_state_manager.lineage_entry("P-104")["tool_calls"] == 2


def test_recovery_churn_is_charged_to_the_lineage_root(tmp_path):
    orchestrator = _orchestrator(tmp_path)

    orchestrator._consume_recovery_churn("stalled_replan_unchanged")
    orchestrator._consume_recovery_churn("verification_recovery_rejected")

    entry = orchestrator.runtime_state_manager.lineage_entry(
        orchestrator._lineage_root_for("P-111")
    )
    assert entry["recoveries"] == 2
    assert orchestrator._lineage_root_for("P-111") == "P-104"


def test_same_gap_repeatedly_unimproved_stands_down_with_the_blocking_gap(tmp_path):
    """无有效进展 ⇒ `no_effective_progress_breaker`，并点名具体阻塞项。"""
    orchestrator = _orchestrator(tmp_path)
    manager = orchestrator.runtime_state_manager

    for _ in range(3):
        manager.record_lineage_cost("P-104", requests=1, tokens=40, recoveries=1)
        manager.record_lineage_progress(
            "P-104", [], gap_fingerprint="gap:P-104|artifact:verify-case-script",
        )

    verdict = orchestrator._lineage_stand_down_verdict()

    assert verdict is not None
    assert verdict["stop_code"] == "no_effective_progress_breaker"
    assert "artifact:verify-case-script" in verdict["stop_reason"]
    assert "tokens=120" in verdict["stop_reason"]


def test_progress_with_excessive_cost_is_not_called_a_dead_loop(tmp_path):
    """有进展但成本过高 ⇒ `cost_budget_exhausted`，措辞**明确否认**死循环。"""
    orchestrator = _orchestrator(tmp_path)
    manager = orchestrator.runtime_state_manager

    manager.record_lineage_cost("P-104", requests=6, tokens=900, recoveries=6)
    manager.record_lineage_progress("P-104", ["file_written:自测报告.md"],
                                    gap_fingerprint="gap:P-104|artifact:x")

    verdict = orchestrator._lineage_stand_down_verdict()

    assert verdict is not None
    assert verdict["stop_code"] == "cost_budget_exhausted"
    assert "NOT a no-progress dead loop" in verdict["stop_reason"]
    assert "file_written:自测报告.md" in verdict["stop_reason"]


def test_healthy_run_does_not_stand_down(tmp_path):
    """未触顶 ⇒ 返回 None，闸门不开（预算不被放宽也不被收紧）。"""
    orchestrator = _orchestrator(tmp_path)
    manager = orchestrator.runtime_state_manager

    manager.record_lineage_cost("P-104", requests=1, tokens=10, recoveries=1)
    manager.record_lineage_progress("P-104", [], gap_fingerprint="gap:A")

    assert orchestrator._lineage_stand_down_verdict() is None