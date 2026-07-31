"""Unit tests for core.evaluator module.

Validates EvaluationReporter and EvaluationMetrics functionality.
Implements 天枢方案书 §7 故障恢复评测 requirements.
"""
from __future__ import annotations

import pytest

from AstralNexus.core.evaluator import (
    EvaluationMetrics,
    EvaluationReporter,
    EvaluationResult,
)
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder


@pytest.fixture
def sample_metrics():
    """Create sample evaluation metrics."""
    return EvaluationMetrics(
        task_success_rate=0.85,
        subtask_completion_rate=0.90,
        constraint_adherence_rate=0.95,
        fact_recall_rate=0.80,
        topology_sparsity=0.30,
        message_compression_rate=0.50,
        fault_recovery_rate=0.75,
        average_recovery_steps=2.5,
        additional_tokens=1500,
        resource_scheduling_accuracy=0.88,
        total_tokens=50000,
        total_time_seconds=120.5,
        total_cost=0.25,
        artifact_acceptance_rate=0.92,
    )


@pytest.fixture
def task_graph_with_faults():
    """Create a task graph with some failed nodes."""
    graph = TaskGraph()
    graph.add_node({"node_id": "T-001", "goal": "Task 1", "role": "coder", "status": "completed"})
    graph.add_node({"node_id": "T-002", "goal": "Task 2", "role": "verifier", "status": "failed"})
    graph.add_node({"node_id": "T-003", "goal": "Task 3", "role": "researcher", "status": "repair_pending"})
    return graph


@pytest.fixture
def trace_with_events(tmp_path):
    """Create a trace recorder with fault and recovery events."""
    from pathlib import Path
    run_dir = tmp_path / "test_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    recorder = TraceRecorder(run_dir=run_dir)
    recorder.record("fault.injected", {"fault_id": "F-001", "type": "timeout"})
    recorder.record("fault.injected", {"fault_id": "F-002", "type": "api_error"})
    recorder.record("recovery.executed", {
        "fault_id": "F-001",
        "status": "executed",
        "success_count": 1,
        "failure_count": 0,
        "tokens_used": 800,
    })
    recorder.record("recovery.executed", {
        "fault_id": "F-002",
        "status": "executed",
        "success_count": 1,
        "failure_count": 0,
        "tokens_used": 600,
    })
    return recorder


class TestEvaluationMetrics:
    """Test EvaluationMetrics dataclass."""

    def test_default_values(self):
        metrics = EvaluationMetrics()
        assert metrics.task_success_rate == 0.0
        assert metrics.fault_recovery_rate == 0.0
        assert metrics.total_tokens == 0

    def test_custom_values(self, sample_metrics):
        assert sample_metrics.task_success_rate == 0.85
        assert sample_metrics.fault_recovery_rate == 0.75
        assert sample_metrics.total_tokens == 50000

    def test_raw_data_default(self):
        metrics = EvaluationMetrics()
        assert isinstance(metrics.raw_data, dict)
        assert len(metrics.raw_data) == 0

    def test_raw_data_custom(self):
        metrics = EvaluationMetrics(raw_data={"key": "value"})
        assert metrics.raw_data["key"] == "value"


class TestEvaluationResult:
    """Test EvaluationResult dataclass."""

    def test_creation(self):
        result = EvaluationResult(
            injected_faults=5,
            recovered_faults=4,
            recovery_rate=0.8,
            average_recovery_steps=2.0,
            extra_tokens=2000,
            final_status="partial",
        )
        assert result.injected_faults == 5
        assert result.recovered_faults == 4
        assert result.recovery_rate == 0.8
        assert result.final_status == "partial"


class TestEvaluationReporter:
    """Test EvaluationReporter functionality."""

    def test_generate_report_with_metrics(self, sample_metrics):
        reporter = EvaluationReporter()
        report = reporter.generate_report(sample_metrics)
        
        assert "天枢任务执行评估报告" in report
        assert "85.00%" in report  # task_success_rate
        assert "75.00%" in report  # fault_recovery_rate
        assert "50,000" in report  # total_tokens

    def test_evaluate_with_trace_recorder(self, trace_with_events):
        reporter = EvaluationReporter(trace_recorder=trace_with_events)
        result = reporter.evaluate()
        
        assert isinstance(result, EvaluationResult)
        assert result.injected_faults == 2
        assert result.recovered_faults == 2
        assert result.recovery_rate == 1.0
        assert result.final_status == "recovered"

    def test_evaluate_no_data_returns_zero(self):
        reporter = EvaluationReporter()
        result = reporter.evaluate()
        
        assert result.injected_faults == 0
        assert result.recovered_faults == 0
        assert result.recovery_rate == 0.0
        assert result.final_status == "no_data"

    def test_evaluate_partial_recovery(self, tmp_path):
        from pathlib import Path
        run_dir = tmp_path / "partial_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        recorder = TraceRecorder(run_dir=run_dir)
        recorder.record("fault.injected", {"fault_id": "F-001"})
        recorder.record("fault.injected", {"fault_id": "F-002"})
        recorder.record("recovery.executed", {
            "fault_id": "F-001",
            "status": "executed",
            "success_count": 1,
            "failure_count": 0,
        })
        # F-002 not recovered
        
        reporter = EvaluationReporter(trace_recorder=recorder)
        result = reporter.evaluate()
        
        assert result.injected_faults == 2
        assert result.recovered_faults == 1
        assert result.recovery_rate == 0.5
        assert result.final_status == "partial"

    def test_generate_recovery_report(self, task_graph_with_faults, trace_with_events):
        reporter = EvaluationReporter(
            task_graph=task_graph_with_faults,
            trace_recorder=trace_with_events,
        )
        report = reporter.generate_recovery_report()
        
        assert "summary" in report
        assert "total_nodes" in report["summary"]
        assert report["summary"]["total_nodes"] == 3
        assert report["summary"]["failed_nodes"] == 1

    def test_generate_recovery_report_no_graph(self):
        reporter = EvaluationReporter()
        report = reporter.generate_recovery_report()
        assert "error" in report
        assert "No task graph" in report["error"]

    def test_get_failed_nodes_analysis(self, task_graph_with_faults):
        reporter = EvaluationReporter(task_graph=task_graph_with_faults)
        analysis = reporter.get_failed_nodes_analysis()
        
        assert len(analysis) == 1
        assert analysis[0]["node_id"] == "T-002"
        assert "role" in analysis[0]

    def test_get_failed_nodes_analysis_no_graph(self):
        reporter = EvaluationReporter()
        analysis = reporter.get_failed_nodes_analysis()
        assert analysis == []

    def test_reporter_with_store_duck_type(self):
        """Test that reporter accepts store as first arg (duck-typed)."""
        class FakeStore:
            def load_task_graph(self, run_id):
                return TaskGraph()
        
        store = FakeStore()
        reporter = EvaluationReporter(store, run_id="test-run")
        assert reporter.store is store


class TestEvaluationMetricsFormatting:
    """Test report formatting with various metric values."""

    def test_zero_metrics(self):
        metrics = EvaluationMetrics()
        reporter = EvaluationReporter()
        report = reporter.generate_report(metrics)
        
        assert "0.00%" in report
        assert "0" in report

    def test_perfect_metrics(self):
        metrics = EvaluationMetrics(
            task_success_rate=1.0,
            fault_recovery_rate=1.0,
            constraint_adherence_rate=1.0,
        )
        reporter = EvaluationReporter()
        report = reporter.generate_report(metrics)
        
        assert "100.00%" in report

    def test_large_token_counts(self):
        metrics = EvaluationMetrics(total_tokens=1_000_000)
        reporter = EvaluationReporter()
        report = reporter.generate_report(metrics)
        
        assert "1,000,000" in report

    def test_cost_formatting(self):
        metrics = EvaluationMetrics(total_cost=0.1234)
        reporter = EvaluationReporter()
        report = reporter.generate_report(metrics)
        
        assert "$0.1234" in report
