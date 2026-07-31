"""Fault diagnostics and Governor-approved recovery execution tests."""

from __future__ import annotations

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
