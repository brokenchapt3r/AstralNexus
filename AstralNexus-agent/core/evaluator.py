from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.core.state_machine import normalize_status
from AstralNexus.runtime.trace import TraceRecorder


@dataclass(frozen=True)
class EvaluationMetrics:
    """Evaluation metrics for a task execution run."""
    
    # Task completion metrics
    task_success_rate: float = 0.0
    subtask_completion_rate: float = 0.0
    constraint_adherence_rate: float = 0.0
    fact_recall_rate: float = 0.0
    
    # Communication efficiency metrics
    topology_sparsity: float = 0.0
    message_compression_rate: float = 0.0
    
    # Fault recovery metrics
    fault_recovery_rate: float = 0.0
    average_recovery_steps: float = 0.0
    additional_tokens: int = 0
    
    # Resource scheduling metrics
    resource_scheduling_accuracy: float = 0.0
    
    # Overall performance metrics
    total_tokens: int = 0
    total_time_seconds: float = 0.0
    total_cost: float = 0.0
    artifact_acceptance_rate: float = 0.0
    
    # Raw data for detailed analysis
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    """Container for evaluation results with attribute access.
    
    Focused on fault recovery and task completion metrics.
    """
    injected_faults: int
    recovered_faults: int
    recovery_rate: float
    average_recovery_steps: float
    extra_tokens: int
    final_status: str


class EvaluationReporter:
    """Generates evaluation reports based on execution traces and metrics.
    
    This is the unified evaluation reporter for AstralNexus.
    It combines comprehensive metrics reporting with detailed fault recovery analysis.
    
    Usage:
        # Simple metrics reporting
        reporter = EvaluationReporter()
        report = reporter.generate_report(metrics)
        
        # Detailed fault recovery analysis
        reporter = EvaluationReporter(task_graph, trace_recorder)
        result = reporter.evaluate()
        recovery_report = reporter.generate_recovery_report()
    """
    
    def __init__(
        self,
        task_graph: Optional[TaskGraph] = None,
        trace_recorder: Optional[TraceRecorder] = None,
        store: Optional[Any] = None,
        run_id: Optional[str] = None,
    ):
        """Initialize evaluation reporter.
        
        Args:
            task_graph: Optional task graph for detailed analysis, or a
                SQLiteTaskGraphStore (duck-typed via ``load_task_graph``).
            trace_recorder: Optional trace recorder for event-based analysis
            store: Optional SQLiteTaskGraphStore for database-backed analysis
            run_id: Optional run ID for store-based lookups
        """
        # Duck-type: if first arg is a store, swap into the correct slot
        if task_graph is not None and hasattr(task_graph, 'load_task_graph'):
            if store is None:
                store = task_graph
            task_graph = None

        self.task_graph = task_graph
        self.trace_recorder = trace_recorder
        self.store = store
        self.run_id = run_id
    
    def generate_report(self, metrics: EvaluationMetrics) -> str:
        """Generate a comprehensive evaluation report.
        
        Args:
            metrics: Evaluation metrics to report
            
        Returns:
            Formatted markdown report
        """
        lines = [
            "# 天枢任务执行评估报告",
            "",
            "## 核心指标",
            "",
            f"- 任务成功率: {metrics.task_success_rate:.2%}",
            f"- 子任务完成率: {metrics.subtask_completion_rate:.2%}",
            f"- 约束保持率: {metrics.constraint_adherence_rate:.2%}",
            f"- 事实召回率: {metrics.fact_recall_rate:.2%}",
            "",
            "## 通信效率",
            "",
            f"- 拓扑稀疏度: {metrics.topology_sparsity:.2%}",
            f"- 消息压缩率: {metrics.message_compression_rate:.2%}",
            "",
            "## 故障恢复能力",
            "",
            f"- 故障恢复率: {metrics.fault_recovery_rate:.2%}",
            f"- 平均恢复步数: {metrics.average_recovery_steps:.1f}",
            f"- 额外消耗 Token: {metrics.additional_tokens:,}",
            "",
            "## 资源调度",
            "",
            f"- 资源调度准确率: {metrics.resource_scheduling_accuracy:.2%}",
            "",
            "## 性能效率",
            "",
            f"- 总消耗 Token: {metrics.total_tokens:,}",
            f"- 总耗时: {metrics.total_time_seconds:.1f} 秒",
            f"- 总成本: ${metrics.total_cost:.4f}",
            f"- 产物验收通过率: {metrics.artifact_acceptance_rate:.2%}",
        ]
        
        return "\n".join(lines)
    
    def evaluate(self) -> EvaluationResult:
        """Evaluate fault recovery performance.
        
        Analyzes injected faults and recovery actions to produce
        a comprehensive evaluation result. Supports both trace_recorder
        and SQLiteTaskGraphStore as data sources.
        
        Returns:
            EvaluationResult with fault recovery metrics
        """
        # Try to get data from trace_recorder first
        if self.trace_recorder:
            # Count injected faults from trace events
            fault_events = self.trace_recorder.get_events_by_type("fault.injected")
            if not fault_events:
                fault_events = self.trace_recorder.get_events_by_type("fault")
            
            # Count recovered faults from trace events
            recovery_events = self.trace_recorder.get_events_by_type("recovery.executed")
            injected_faults = len(fault_events)
            if injected_faults == 0 and recovery_events:
                fault_ids = {e.get("fault_id") for e in recovery_events if e.get("fault_id")}
                injected_faults = len(fault_ids) if fault_ids else len(recovery_events)
            recovered_faults = sum(
                1 for e in recovery_events
                if e.get("status") == "executed"
                or (e.get("success_count", 0) > 0 and e.get("failure_count", 0) == 0)
            )
            
            # Calculate extra tokens from compression and recovery
            extra_tokens = 0
            
            # Tokens saved by compression
            compression_events = self.trace_recorder.get_events_by_type("context.compressed")
            for event in compression_events:
                tokens_before = event.get("tokens_before", 0)
                tokens_after = event.get("tokens_after", 0)
                if tokens_before > tokens_after:
                    extra_tokens += (tokens_before - tokens_after)
            
            # Tokens consumed by recovery actions
            for event in recovery_events:
                action_tokens = event.get("tokens_used", 500)  # Default 500 if not recorded
                extra_tokens += action_tokens
            
            # Calculate average recovery steps
            total_steps = len(recovery_events)
            average_recovery_steps = total_steps / injected_faults if injected_faults > 0 else 0.0
            
            # Conservative estimate if no real data
            if extra_tokens == 0 and injected_faults > 0:
                extra_tokens = int(average_recovery_steps * 800)  # ~800 tokens per step
            
        # Fall back to store if available
        elif self.store and hasattr(self.store, 'fault_events') and hasattr(self.store, 'recovery_plans'):
            fault_events_list = self.store.fault_events()
            recovery_plans_list = self.store.recovery_plans()
            
            injected_faults = len(fault_events_list)
            recovered_faults = len([p for p in recovery_plans_list if p.get("status") == "executed"])
            
            # Calculate average recovery steps from store
            total_steps = len(recovery_plans_list)
            for plan in recovery_plans_list:
                actions = plan.get("actions", [])
                if len(actions) > 1:
                    total_steps += len(actions) - 1
            average_recovery_steps = total_steps / injected_faults if injected_faults > 0 else 0.0
            
            # Calculate extra tokens (estimate if not available)
            extra_tokens = 0
            if injected_faults > 0:
                extra_tokens = int(average_recovery_steps * 800)  # ~800 tokens per step
        
        # No data available
        else:
            return EvaluationResult(
                injected_faults=0,
                recovered_faults=0,
                recovery_rate=0.0,
                average_recovery_steps=0.0,
                extra_tokens=0,
                final_status="no_data"
            )
        
        # Calculate recovery rate
        recovery_rate = recovered_faults / injected_faults if injected_faults > 0 else 0.0
        
        # Determine final status
        if recovery_rate == 1.0:
            final_status = "recovered"
        elif recovery_rate > 0:
            final_status = "partial"
        else:
            final_status = "failed"
        
        result = EvaluationResult(
            injected_faults=injected_faults,
            recovered_faults=recovered_faults,
            recovery_rate=recovery_rate,
            average_recovery_steps=average_recovery_steps,
            extra_tokens=extra_tokens,
            final_status=final_status
        )
        
        # Persist evaluation results to store if available
        if self.store and hasattr(self.store, 'add_recovery_evaluation'):
            self.store.add_recovery_evaluation(
                fault_id="overall_assessment",
                final_status=result.final_status,
                recovery_rate=result.recovery_rate,
                average_recovery_steps=int(result.average_recovery_steps),
                extra_tokens=result.extra_tokens
            )
        
        return result
    
    def generate_recovery_report(self) -> dict[str, Any]:
        """Generate a detailed recovery evaluation report.
        
        Returns:
            Dictionary containing recovery metrics and analysis
        """
        if not self.task_graph:
            return {"error": "No task graph available for analysis"}
        
        # Count nodes by status
        status_counts: dict[str, int] = {}
        for node in self.task_graph.nodes.values():
            canonical_status = normalize_status(node.status)
            status_counts[canonical_status] = status_counts.get(canonical_status, 0) + 1
        
        # Calculate recovery metrics
        total_nodes = len(self.task_graph.nodes)
        completed_nodes = status_counts.get("completed", 0)
        failed_nodes = status_counts.get("failed", 0)
        repair_pending_nodes = status_counts.get("repair_pending", 0)
        
        success_rate = (completed_nodes / total_nodes * 100) if total_nodes > 0 else 0
        failure_rate = (failed_nodes / total_nodes * 100) if total_nodes > 0 else 0
        
        # Count recovery actions from trace
        recovery_events = []
        if self.trace_recorder:
            recovery_events = [
                event for event in self.trace_recorder.events
                if str(event.get("event_type", "")).startswith("recovery.action")
            ]
        total_recovery_actions = len(recovery_events)
        
        # Analyze recovery action types
        action_type_counts: dict[str, int] = {}
        for event in recovery_events:
            event_type = event.get("event_type", "unknown")
            action_type = event_type.replace("recovery.action.", "")
            action_type_counts[action_type] = action_type_counts.get(action_type, 0) + 1
        
        report: dict[str, Any] = {
            "summary": {
                "total_nodes": total_nodes,
                "completed_nodes": completed_nodes,
                "failed_nodes": failed_nodes,
                "repair_pending_nodes": repair_pending_nodes,
                "success_rate_percent": round(success_rate, 2),
                "failure_rate_percent": round(failure_rate, 2),
            },
            "recovery_actions": {
                "total_actions": total_recovery_actions,
                "by_type": action_type_counts,
            },
            "status_distribution": status_counts,
        }
        
        if self.trace_recorder:
            if hasattr(self.trace_recorder, 'get_current_timestamp'):
                report["timestamp"] = self.trace_recorder.get_current_timestamp()
            else:
                from datetime import datetime, timezone
                report["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        return report
    
    def get_failed_nodes_analysis(self) -> list[dict[str, Any]]:
        """Analyze failed nodes to identify common failure patterns.
        
        Returns:
            List of dictionaries containing failure analysis
        """
        if not self.task_graph:
            return []
        
        failed_nodes = [
            node for node in self.task_graph.nodes.values()
            if node.status == "failed"
        ]
        
        analysis = []
        for node in failed_nodes:
            node_analysis = {
                "node_id": node.node_id,
                "goal": node.goal,
                "role": node.role,
                "retry_count": node.retry_count,
                "error_message": getattr(node, 'error_message', None),
            }
            analysis.append(node_analysis)
        
        return analysis
