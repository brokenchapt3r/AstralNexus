"""Fault diagnostics and Governor-approved recovery execution tests."""

from __future__ import annotations

import json

import pytest

from AstralNexus.core.fault_recovery import (
    Fault,
    FaultDetector,
    FaultRecoveryEngine,
    ImpactAnalyzer,
    RecoveryExecutor,
)
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder


@pytest.fixture
def task_graph() -> TaskGraph:
    graph = TaskGraph()
    graph.add_node(
        {
            "node_id": "T-1",
            "goal": "Collect evidence",
            "role": "researcher",
            "status": "completed",
            "metadata": {"tools_used": ["web_search"]},
        }
    )
    graph.add_node(
        {
            "node_id": "T-2",
            "goal": "Implement feature",
            "role": "coder",
            "status": "completed",
            "metadata": {
                "tools_used": ["terminal_exec"],
                "assigned_resource": "local-1",
                "resource_tier": "local",
            },
        }
    )
    graph.add_node(
        {
            "node_id": "T-3",
            "goal": "Review implementation",
            "role": "reviewer",
            "status": "admitted",
            "metadata": {"tools_used": ["file_read"]},
        }
    )
    graph.add_node(
        {
            "node_id": "T-4",
            "goal": "Verify implementation",
            "role": "verifier",
            "status": "admitted",
            "metadata": {"tools_used": ["terminal_exec"]},
        }
    )
    graph.add_edge({"source": "T-2", "target": "T-1", "edge_type": "depends_on"})
    graph.add_edge({"source": "T-3", "target": "T-2", "edge_type": "depends_on"})
    graph.add_edge({"source": "T-4", "target": "T-2", "edge_type": "depends_on"})
    graph.add_fact(
        {
            "fact_id": "F-1",
            "text": "Feature implementation completed",
            "source": "T-2",
            "confidence": 0.95,
            "recorded_at": "2026-07-13T10:00:00+00:00",
        }
    )
    graph.add_artifact(
        {
            "artifact_id": "A-1",
            "artifact_type": "code",
            "node_id": "T-2",
            "path": "artifacts/feature.py",
            "description": "Evidence for F-2",
        }
    )
    return graph


@pytest.fixture
def trace_recorder(tmp_path) -> TraceRecorder:
    return TraceRecorder(tmp_path / "trace")


def test_detector_emits_all_canonical_fault_types(task_graph, trace_recorder):
    detector = FaultDetector(task_graph, trace_recorder)
    faults = [
        detector.detect_fault("node_failure", "T-3", {"error_message": "timeout"}),
        detector.detect_fault("tool_failure", "terminal_exec", {"error_message": "missing"}),
        detector.detect_fault(
            "requirement_change", "T-2", {"new_constraints": ["tenant isolation"]}
        ),
        detector.detect_fault("memory_loss", "memory_system", {"lost_facts": ["F-1"]}),
        detector.detect_fault(
            "resource_degradation", "local-1", {"degradation_type": "high_latency"}
        ),
    ]
    assert [fault.fault_type for fault in faults] == [
        "node_failure",
        "tool_failure",
        "requirement_change",
        "memory_loss",
        "resource_degradation",
    ]
    assert len(detector.faults) == 5
    assert len([event for event in trace_recorder.events if event["event_type"].startswith("fault.detected")]) == 5


def test_impact_analyzer_uses_graph_dependencies(task_graph, trace_recorder):
    fault = FaultDetector(task_graph, trace_recorder).detect_node_failure("T-2", "timeout")
    impact = ImpactAnalyzer(task_graph).analyze_impact(fault)
    assert set(impact["impacted_nodes"]) == {"T-2", "T-3", "T-4"}
    assert 0 < impact["impact_scope"] <= 1


@pytest.mark.parametrize(
    ("action", "target_status", "metadata_key"),
    [
        ({"type": "retry", "target": "T-3", "details": {}}, "ready", None),
        (
            {
                "type": "reassign",
                "target": "T-3",
                "details": {"resource_tier": "edge", "worker_type": "Coding"},
            },
            "ready",
            "reassigned",
        ),
        (
            {
                "type": "replan",
                "target": "T-3",
                "details": {"new_constraints": ["preserve API"]},
            },
            "repair_pending",
            "replanned",
        ),
    ],
)
def test_executor_applies_governor_actions(
    task_graph, trace_recorder, action, target_status, metadata_key
):
    task_graph.get_node("T-3").status = "failed"
    executor = RecoveryExecutor(task_graph, trace_recorder)
    assert executor.execute_recovery_action(action) is True
    node = task_graph.get_node("T-3")
    assert node.status == target_status
    if metadata_key:
        assert node.metadata[metadata_key] is True


def test_identical_reassignment_does_not_report_recovery_success(task_graph, trace_recorder):
    executor = RecoveryExecutor(task_graph, trace_recorder)
    node = task_graph.get_node('T-3')
    node.status = 'failed'
    action = {'type': 'reassign', 'target': 'T-3',
              'details': {'worker_type': 'Tool', 'resource_tier': 'local'}}
    assert executor.execute_recovery_action(action) is True
    node.status = 'blocked'
    assert executor.execute_recovery_action(action) is False
    assert node.status == 'blocked'
    assert node.metadata['reassignment_count'] == 1
    action['details']['worker_type'] = 'Coding'
    assert executor.execute_recovery_action(action) is True


def test_executor_switches_tool_and_resource(task_graph, trace_recorder):
    executor = RecoveryExecutor(task_graph, trace_recorder)
    assert executor.execute_recovery_action(
        {
            "type": "switch_tool",
            "target": "terminal_exec",
            "details": {"alternative_tool": "python_exec"},
        }
    )
    assert task_graph.get_node("T-4").metadata["tool_switch"]["to"] == "python_exec"

    task_graph.get_node("T-3").metadata["assigned_resource"] = "local-1"
    assert executor.execute_recovery_action(
        {
            "type": "switch_resource",
            "target": "local-1",
            "details": {"alternative_resource": "cloud-1"},
        }
    )
    assert task_graph.get_node("T-3").metadata["resource_switch"]["to"] == "cloud-1"


def test_executor_restores_fact_from_artifact(task_graph, trace_recorder):
    executor = RecoveryExecutor(task_graph, trace_recorder)
    assert executor.execute_recovery_action(
        {"type": "restore_facts", "target": "memory_system", "details": {"lost_facts": ["F-2"]}}
    )
    assert "F-2" in task_graph.facts


def test_executor_rejects_unknown_or_missing_targets(task_graph, trace_recorder):
    executor = RecoveryExecutor(task_graph, trace_recorder)
    assert not executor.execute_recovery_action(
        {"type": "retry", "target": "T-404", "details": {}}
    )
    assert not executor.execute_recovery_action(
        {"type": "invent_policy", "target": "T-3", "details": {}}
    )


def test_engine_executes_only_supplied_governor_plan(task_graph, trace_recorder):
    engine = FaultRecoveryEngine(task_graph, trace_recorder)
    task_graph.get_node("T-3").status = "failed"
    fault = engine.detector.detect_node_failure("T-3", "timeout")
    result = engine.execute_governed_recovery(
        fault,
        [{"type": "retry", "target": "T-3", "reason": "Governor decision", "details": {}}],
    )
    assert result["total_actions"] == 1
    assert result["success_count"] == 1
    assert fault.status == "recovered"
    assert task_graph.get_node("T-3").status == "ready"


def test_engine_requires_real_graph_and_trace(task_graph):
    with pytest.raises(TypeError, match="TaskGraph"):
        FaultRecoveryEngine(object(), object())
    with pytest.raises(TypeError, match="trace_recorder"):
        FaultRecoveryEngine(task_graph, None)


# ---------------------------------------------------------------------------
# Repeat-aware fault-recovery enforcement (live r11 resume #10, user
# directive 2026-09-12): N-002-R8-RECOVERY died three identical deaths
# because plan_fault_recovery kept accepting bare `retry` for repeat counts
# 2 and 3 of the same fingerprint.  Repetition evidence must now police the
# plan shape: identical re-dispatch is rejected with a teaching reason, one
# correction round follows, and persistent identity escalates to
# invalid_proposal (graph-level replan) instead of another clone attempt.
# ---------------------------------------------------------------------------

REPEAT_FINGERPRINT = "llmclienterror|llmclienterror|partial_artifacts_retained"


def _repeat_fault(root_id: str, repeat_count: int) -> dict:
    return {
        "fault_id": "fault:node_failure:X:0",
        "fault_type": "node_failure",
        "target": root_id,
        "status": "detected",
        "payload": {
            "failure_fingerprint": REPEAT_FINGERPRINT,
            "repeat_count": repeat_count,
            "approved_worker_type": "Tool",
            "worker_selection": {
                "selected": {"worker_type": "Tool", "resource_tier": "cloud"},
            },
        },
    }


class _ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _retry_plan(root_id: str) -> str:
    return json.dumps({
        "strategy": "retry_failed_node",
        "reasoning": "try again",
        "actions": [{"type": "retry", "target": root_id, "details": {}}],
    })


def _reassign_plan(root_id: str) -> str:
    return json.dumps({
        "strategy": "change_execution_shape",
        "reasoning": "identical retry rejected",
        "actions": [{
            "type": "reassign",
            "target": root_id,
            "details": {"worker_type": "Coding", "resource_tier": "local"},
        }],
    })


def test_repeat_fingerprint_rejects_identical_retry_then_accepts_changed_shape(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="repeat-recovery") as orchestrator:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])
        governor = orchestrator.governor
        governor.llm_matching_enabled = True
        client = _ScriptedClient([_retry_plan(root_id), _reassign_plan(root_id)])
        governor.llm_client = client

        plan = governor.plan_fault_recovery(
            fault=_repeat_fault(root_id, repeat_count=2),
            impact={"impacted_nodes": [root_id]},
        )

        assert len(client.calls) == 2  # round 1 rejected, correction accepted
        assert plan["actions"][0]["type"] == "reassign"
        assert plan["actions"][0]["details"]["worker_type"] == "Coding"
        rejected = [
            event for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.fault_recovery.identical_shape_rejected"
        ]
        assert rejected and rejected[-1]["payload"]["round"] == 1
        assert rejected[-1]["payload"]["repeat_count"] == 2
        # The correction round must carry the teaching rejection reason.
        assert "不构成有效恢复" in client.calls[1][-1]["content"]
        # First-round prompt must surface the repetition evidence.
        first_user = client.calls[0][1]["content"]
        assert "repeat_evidence" in first_user
        assert REPEAT_FINGERPRINT in first_user


def test_persistent_identical_recovery_escalates_to_invalid_proposal(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="repeat-escalate") as orchestrator:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])
        governor = orchestrator.governor
        governor.llm_matching_enabled = True
        client = _ScriptedClient([_retry_plan(root_id)])
        governor.llm_client = client

        with pytest.raises(ValueError, match="不构成有效恢复"):
            governor.plan_fault_recovery(
                fault=_repeat_fault(root_id, repeat_count=3),
                impact={"impacted_nodes": [root_id]},
            )
        # Exactly one correction round, then escalation — no infinite loop.
        assert len(client.calls) == 2


def test_shape_preserving_reassign_is_also_rejected_on_repeat(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="repeat-reassign") as orchestrator:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])
        governor = orchestrator.governor
        governor.llm_matching_enabled = True
        same_shape = json.dumps({
            "strategy": "reassign_same",
            "reasoning": "keep everything",
            "actions": [{
                "type": "reassign",
                "target": root_id,
                "details": {"worker_type": "Tool", "resource_tier": "cloud"},
            }],
        })
        client = _ScriptedClient([same_shape, same_shape])
        governor.llm_client = client

        with pytest.raises(ValueError, match="保持失败执行形状"):
            governor.plan_fault_recovery(
                fault=_repeat_fault(root_id, repeat_count=2),
                impact={"impacted_nodes": [root_id]},
            )


def test_first_failure_retry_passes_without_repeat_enforcement(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="first-failure") as orchestrator:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])
        governor = orchestrator.governor
        governor.llm_matching_enabled = True
        client = _ScriptedClient([_retry_plan(root_id)])
        governor.llm_client = client

        plan = governor.plan_fault_recovery(
            fault=_repeat_fault(root_id, repeat_count=1),
            impact={"impacted_nodes": [root_id]},
        )

        assert len(client.calls) == 1  # no correction round on first failure
        assert plan["actions"][0]["type"] == "retry"
        assert not [
            event for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.fault_recovery.identical_shape_rejected"
        ]
