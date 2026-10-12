"""Metrics aggregator: collects 12 competition dimensions from trace and task graph.

Generates a comprehensive metrics.json for Phase 4 evaluation.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.core.output_paths import RunOutputLayout
from AstralNexus.runtime.trace import TraceRecorder


class MetricsAggregator:
    """Aggregates metrics across 12 competition dimensions."""

    def __init__(self, trace_recorder: TraceRecorder, task_graph: TaskGraph, run_dir: Path):
        self.trace = trace_recorder
        self.graph = task_graph
        self.run_dir = run_dir

    def aggregate_all(self, execution_stats: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate all 12 dimensions into a single metrics dict."""
        metrics = {
            "timestamp": datetime.utcnow().isoformat(),
            "run_dir": str(self.run_dir),
            "execution_stats": execution_stats,
        }

        metrics["task_graph_metrics"] = self._compute_task_graph_metrics()
        metrics["router_metrics"] = self._compute_router_metrics()
        metrics["compression_metrics"] = self._compute_compression_metrics()
        metrics["communication_metrics"] = self._compute_communication_metrics()
        metrics["fact_recall_metrics"] = self._compute_fact_recall_metrics()
        metrics["constraint_metrics"] = self._compute_constraint_metrics()
        metrics["fault_recovery_metrics"] = self._compute_fault_recovery_metrics()
        metrics["scheduling_metrics"] = self._compute_scheduling_metrics()
        metrics["execution_path_metrics"] = self._compute_execution_path_metrics()
        metrics["ready_wave_metrics"] = self._compute_ready_wave_metrics()
        metrics["quality_metrics"] = self._compute_quality_metrics()
        metrics["performance_metrics"] = self._compute_performance_metrics(execution_stats)
        
        # 阶段五新增：Governor/Verifier/Replan 指标
        metrics["governor_metrics"] = self._compute_governor_metrics()
        metrics["verification_metrics"] = self._compute_verification_metrics()
        metrics["replan_metrics"] = self._compute_replan_metrics()
        
        # 阶段六新增：动态异构群体指标
        metrics["dynamic_spawning_metrics"] = self._compute_dynamic_spawning_metrics()
        metrics["value_routing_metrics"] = self._compute_value_routing_metrics()
        metrics["heterogeneous_worker_metrics"] = self._compute_heterogeneous_worker_metrics()

        return metrics

    def _compute_task_graph_metrics(self) -> Dict[str, Any]:
        """Dimension 1: Task graph structure."""
        nodes = list(self.graph.nodes.values())
        edges = self.graph.edges

        status_counts: dict[str, int] = {}
        role_counts: dict[str, int] = {}
        for node in nodes:
            status_counts[node.status] = status_counts.get(node.status, 0) + 1
            role_counts[node.role] = role_counts.get(node.role, 0) + 1

        return {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "status_distribution": status_counts,
            "role_distribution": role_counts,
            "dependency_edges": sum(1 for e in edges if e.edge_type == "dependency"),
            "communication_edges": sum(1 for e in edges if e.edge_type == "communication"),
            "avg_dependencies_per_node": (
                len([e for e in edges if e.edge_type == "dependency"]) / max(1, len(nodes))
            ),
        }

    def _compute_router_metrics(self) -> Dict[str, Any]:
        """Dimension 2: Router sparsity and topology."""
        events = self.trace.get_events_by_type("topology.sparsity")
        if not events:
            return {"samples": 0, "avg_sparsity": 0.0}

        sparsities = [e.get("sparsity_ratio", 0) for e in events if "sparsity_ratio" in e]
        return {
            "samples": len(events),
            "avg_sparsity": sum(sparsities) / max(1, len(sparsities)),
            "min_sparsity": min(sparsities) if sparsities else 0,
            "max_sparsity": max(sparsities) if sparsities else 0,
            "final_node_count": events[-1].get("node_count", 0) if events else 0,
            "final_edge_count": events[-1].get("edge_count", 0) if events else 0,
        }

    def _compute_compression_metrics(self) -> Dict[str, Any]:
        """Dimension 3: Message compression rate and quality."""
        compressed_events = self.trace.get_events_by_type("message.compressed")
        if not compressed_events:
            return {"compression_count": 0, "avg_ratio": 0.0}

        ratios = []
        trigger_types = {"natural": 0, "forced": 0}
        quality_metrics_list = []

        for event in compressed_events:
            ratio = event.get("compression_ratio", 0)
            if ratio:
                ratios.append(ratio)
            trigger = event.get("trigger_type", "unknown")
            trigger_types[trigger] = trigger_types.get(trigger, 0) + 1
            if "quality_metrics" in event:
                quality_metrics_list.append(event["quality_metrics"])

        return {
            "compression_count": len(compressed_events),
            "avg_ratio": sum(ratios) / max(1, len(ratios)),
            "min_ratio": min(ratios) if ratios else 0,
            "max_ratio": max(ratios) if ratios else 0,
            "trigger_distribution": trigger_types,
            "quality_metrics_samples": len(quality_metrics_list),
            "avg_payload_efficiency": (
                sum(qm.get("payload_efficiency", 0) for qm in quality_metrics_list)
                / max(1, len(quality_metrics_list))
            ),
        }

    def _compute_communication_metrics(self) -> Dict[str, Any]:
        """Dimension 4: Low-entropy communication quality."""
        msg_events = self.trace.get_events_by_type("message.sent")
        quality_events = self.trace.get_events_by_type("message.quality")

        # payload_efficiency 和 claim_evidence_coverage 在 message.compressed 事件的
        # quality_metrics 子对象中，而非 message.quality 事件的顶层字段。
        # 必须从 message.compressed 事件中聚合这些指标。
        compressed_events = self.trace.get_events_by_type("message.compressed")
        quality_metrics_list = []
        for event in compressed_events:
            qm = event.get("quality_metrics")
            if isinstance(qm, dict):
                quality_metrics_list.append(qm)

        if quality_metrics_list:
            avg_payload_efficiency = (
                sum(qm.get("payload_efficiency", 0) for qm in quality_metrics_list)
                / len(quality_metrics_list)
            )
            avg_claim_evidence_coverage = (
                sum(qm.get("claim_evidence_coverage", 0) for qm in quality_metrics_list)
                / len(quality_metrics_list)
            )
            avg_key_field_retention = (
                sum(qm.get("key_field_retention", 0) for qm in quality_metrics_list)
                / len(quality_metrics_list)
            )
            avg_artifact_path_retention = (
                sum(qm.get("artifact_path_retention", 0) for qm in quality_metrics_list)
                / len(quality_metrics_list)
            )
            avg_action_retention = (
                sum(qm.get("action_retention", 0) for qm in quality_metrics_list)
                / len(quality_metrics_list)
            )
        else:
            avg_payload_efficiency = 0.0
            avg_claim_evidence_coverage = 0.0
            avg_key_field_retention = 0.0
            avg_artifact_path_retention = 0.0
            avg_action_retention = 0.0

        return {
            "messages_sent": len(msg_events),
            "quality_checks": len(quality_events),
            "quality_metrics_samples": len(quality_metrics_list),
            "avg_payload_efficiency": avg_payload_efficiency,
            "avg_claim_evidence_coverage": avg_claim_evidence_coverage,
            "avg_key_field_retention": avg_key_field_retention,
            "avg_artifact_path_retention": avg_artifact_path_retention,
            "avg_action_retention": avg_action_retention,
        }

    def _compute_fact_recall_metrics(self) -> Dict[str, Any]:
        """Dimension 5: Fact recall rate."""
        recall_events = self.trace.get_events_by_type("facts.retrieved")
        preserve_events = self.trace.get_events_by_type("facts.preserved")

        total_preserved = sum(e.get("count", 0) for e in preserve_events)
        total_recalled = sum(e.get("count", 0) for e in recall_events)

        return {
            "preserve_events": len(preserve_events),
            "recall_events": len(recall_events),
            "total_facts_preserved": total_preserved,
            "total_facts_recalled": total_recalled,
            "recall_rate": (
                total_recalled / max(1, total_preserved) if total_preserved > 0 else 0.0
            ),
        }

    def _compute_constraint_metrics(self) -> Dict[str, Any]:
        """Dimension 6: Constraint retention rate."""
        constraint_events = self.trace.get_events_by_type("constraint.checked")
        violation_events = self.trace.get_events_by_type("constraint.violated")

        total_checks = len(constraint_events)
        total_violations = len(violation_events)

        return {
            "total_checks": total_checks,
            "total_violations": total_violations,
            "retention_rate": (
                (total_checks - total_violations) / max(1, total_checks)
                if total_checks > 0
                else 1.0
            ),
        }

    def _compute_fault_recovery_metrics(self) -> Dict[str, Any]:
        """Dimension 7: Fault recovery success rate."""
        injection_events = self.trace.get_events_by_type("fault_injection.requested")
        recovery_events = self.trace.get_events_by_type("recovery.attempted")
        recovery_success_events = self.trace.get_events_by_type("recovery.completed")

        total_injections = len(injection_events)
        total_attempts = len(recovery_events)
        total_successes = len(recovery_success_events)

        fault_type_counts: dict[str, int] = {}
        for event in injection_events:
            ft = event.get("fault_type", "unknown")
            fault_type_counts[ft] = fault_type_counts.get(ft, 0) + 1

        return {
            "total_injections": total_injections,
            "total_recovery_attempts": total_attempts,
            "total_recovery_successes": total_successes,
            "recovery_success_rate": (
                total_successes / max(1, total_attempts) if total_attempts > 0 else 0.0
            ),
            "fault_type_distribution": fault_type_counts,
        }

    def _compute_scheduling_metrics(self) -> Dict[str, Any]:
        """Dimension 8: Edge/cloud/local scheduling decisions."""
        scheduling_events = self.trace.get_events_by_type("task.execution.resource_scheduled")

        tier_counts = {"local": 0, "edge": 0, "cloud": 0}
        for event in scheduling_events:
            tier = event.get("resource_tier", "unknown")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        total = sum(tier_counts.values())
        return {
            "total_scheduling_decisions": total,
            "tier_distribution": tier_counts,
            "local_ratio": tier_counts["local"] / max(1, total),
            "edge_ratio": tier_counts["edge"] / max(1, total),
            "cloud_ratio": tier_counts["cloud"] / max(1, total),
        }

    def _compute_execution_path_metrics(self) -> Dict[str, Any]:
        """Dimension 9: governed runtime execution coverage."""
        runtime_events = self.trace.get_events_by_type("task.execution.runtime")
        runtime_count = len(runtime_events)
        return {
            "total_executions": runtime_count,
            "runtime_count": runtime_count,
            "runtime_ratio": 1.0 if runtime_count else 0.0,
        }

    def _compute_ready_wave_metrics(self) -> Dict[str, Any]:
        """Dimension 10: Ready-wave parallel batch statistics."""
        selection_events = self.trace.get_events_by_type("task.selection")

        wave_sizes = [e.get("selected_count", 0) for e in selection_events]
        return {
            "total_waves": len(selection_events),
            "avg_wave_size": sum(wave_sizes) / max(1, len(wave_sizes)),
            "max_wave_size": max(wave_sizes) if wave_sizes else 0,
            "min_wave_size": min(wave_sizes) if wave_sizes else 0,
            "total_tasks_selected": sum(wave_sizes),
        }

    def _compute_quality_metrics(self) -> Dict[str, Any]:
        """Dimension 11: Low-entropy communication quality indicators."""
        quality_events = self.trace.get_events_by_type("message.quality")
        compressed_events = self.trace.get_events_by_type("message.compressed")

        # Aggregate quality metrics from compressed events
        payload_efficiencies = []
        key_field_retentions = []
        for event in compressed_events:
            qm = event.get("quality_metrics", {})
            if "payload_efficiency" in qm:
                payload_efficiencies.append(qm["payload_efficiency"])
            if "key_field_retention" in qm:
                key_field_retentions.append(qm["key_field_retention"])

        return {
            "quality_samples": len(quality_events) + len(compressed_events),
            "avg_payload_efficiency": (
                sum(payload_efficiencies) / max(1, len(payload_efficiencies))
            ),
            "avg_key_field_retention": (
                sum(key_field_retentions) / max(1, len(key_field_retentions))
            ),
        }

    def _compute_performance_metrics(self, execution_stats: Dict[str, Any]) -> Dict[str, Any]:
        """Dimension 12: Performance and timing."""
        elapsed = execution_stats.get("elapsed_seconds", 0)
        iterations = execution_stats.get("iterations", 0)
        completed = execution_stats.get("completed_tasks", 0)

        return {
            "total_elapsed_seconds": elapsed,
            "total_iterations": iterations,
            "completed_tasks": completed,
            "failed_tasks": execution_stats.get("failed_tasks", 0),
            "recovery_attempts": execution_stats.get("recovery_attempts", 0),
            "avg_seconds_per_iteration": elapsed / max(1, iterations),
            "avg_seconds_per_task": elapsed / max(1, completed),
            "throughput_tasks_per_second": completed / max(0.001, elapsed),
        }

    def _compute_governor_metrics(self) -> Dict[str, Any]:
        """Governor dynamic admission and budget tracking (阶段五)."""
        admission_events = self.trace.get_events_by_type("governor.admission")
        rejection_events = self.trace.get_events_by_type("governor.rejection")
        budget_events = self.trace.get_events_by_type("governor.budget")
        prune_events = self.trace.get_events_by_type("governor.pruned")

        scores = [e.get("admission_score", 0) for e in admission_events if "admission_score" in e]
        reasons: dict[str, int] = {}
        for event in rejection_events:
            r = event.get("rejection_reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1

        total_admitted = len(admission_events)
        total_rejected = len(rejection_events)
        total_proposed = total_admitted + total_rejected

        return {
            "total_proposals": total_proposed,
            "total_admitted": total_admitted,
            "total_rejected": total_rejected,
            "admission_rate": total_admitted / max(1, total_proposed),
            "avg_admission_score": sum(scores) / max(1, len(scores)),
            "rejection_reasons": reasons,
            "budget_updates": len(budget_events),
            "prune_events": len(prune_events),
        }

    def _compute_verification_metrics(self) -> Dict[str, Any]:
        """Verifier Mesh verification statistics (阶段五)."""
        verify_events = self.trace.get_events_by_type("verification.completed")
        repair_events = self.trace.get_events_by_type("verification.repair")
        replan_events = self.trace.get_events_by_type("verification.replan")

        scores = [e.get("overall_score", 0) for e in verify_events if "overall_score" in e]
        statuses: dict[str, int] = {}
        for event in verify_events:
            s = event.get("status", "unknown")
            statuses[s] = statuses.get(s, 0) + 1

        # Aggregate dimension scores
        dimension_scores: Dict[str, list] = {}
        for event in verify_events:
            dims = event.get("dimensions", {})
            for name, score in dims.items():
                dimension_scores.setdefault(name, []).append(score)

        avg_dim_scores = {
            name: sum(vals) / max(1, len(vals))
            for name, vals in dimension_scores.items()
        }

        return {
            "total_verifications": len(verify_events),
            "repairs_triggered": len(repair_events),
            "replans_triggered": len(replan_events),
            "avg_overall_score": sum(scores) / max(1, len(scores)),
            "status_distribution": statuses,
            "avg_dimension_scores": avg_dim_scores,
        }

    def _compute_replan_metrics(self) -> Dict[str, Any]:
        """Replan execution and success rate (阶段五)."""
        replan_proposed = self.trace.get_events_by_type("replan.proposed")
        replan_executed = self.trace.get_events_by_type("replan.executed")
        replan_success = self.trace.get_events_by_type("replan.success")
        replan_failed = self.trace.get_events_by_type("replan.failed")

        # Count from orchestrator-level replan events
        orch_replan = self.trace.get_events_by_type("orchestrator.verification.replan")

        action_types: Dict[str, int] = {}
        for event in replan_proposed:
            a = event.get("action_type", "unknown")
            action_types[a] = action_types.get(a, 0) + 1

        return {
            "orchestrator_replans": len(orch_replan),
            "total_proposed": len(replan_proposed),
            "total_executed": len(replan_executed),
            "total_success": len(replan_success),
            "total_failed": len(replan_failed),
            "execution_rate": len(replan_executed) / max(1, len(replan_proposed)),
            "success_rate": len(replan_success) / max(1, len(replan_executed)),
            "action_type_distribution": action_types,
        }

    def _compute_dynamic_spawning_metrics(self) -> Dict[str, Any]:
        """Dynamic node spawning events (阶段六: 动态异构群体)."""
        spawn_vf = self.trace.get_events_by_type("orchestrator.dynamic_spawn.verification_failure")
        spawn_ex = self.trace.get_events_by_type("orchestrator.dynamic_spawn.exception")
        spawn_other = self.trace.get_events_by_type("orchestrator.dynamic_spawn")
        dynamic_spawns = self.trace.get_events_by_type("task.dynamic_spawn")

        all_spawn_events = spawn_vf + spawn_ex + spawn_other + dynamic_spawns

        trigger_dist: Dict[str, int] = {}
        for e in spawn_vf:
            trigger_dist["verification_failure"] = trigger_dist.get("verification_failure", 0) + 1
        for e in spawn_ex:
            trigger_dist["exception"] = trigger_dist.get("exception", 0) + 1
        for e in spawn_other:
            trigger_dist["other"] = trigger_dist.get("other", 0) + 1
        for e in dynamic_spawns:
            t = e.get("trigger", "unknown")
            trigger_dist[t] = trigger_dist.get(t, 0) + 1

        total = len(all_spawn_events)
        # Approximate success: events where spawned_node is present
        spawned_count = sum(1 for e in all_spawn_events if e.get("spawned_node"))

        return {
            "total_spawn_events": total,
            "spawned_nodes": spawned_count,
            "spawn_success_rate": spawned_count / max(1, total),
            "trigger_distribution": trigger_dist,
            "verification_failure_spawns": len(spawn_vf),
            "exception_spawns": len(spawn_ex),
        }

    def _compute_value_routing_metrics(self) -> Dict[str, Any]:
        """Value-based message routing statistics (阶段六: 低熵通信)."""
        routing_events = self.trace.get_events_by_type("router.value_routing")

        if not routing_events:
            trace_path = self.run_dir / "trace.jsonl"
            if trace_path.exists():
                try:
                    with trace_path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            event = json.loads(line)
                            if event.get("event_type") == "router.value_routing":
                                routing_events.append(dict(event.get("payload", {})))
                except (OSError, json.JSONDecodeError):
                    pass

        if not routing_events:
            return {
                "total_messages_routed": 0,
                "total_transmitted": 0,
                "total_filtered": 0,
                "avg_value_score": 0.0,
                "rejection_rate": 0.0,
                "top_message_types": {},
            }

        transmitted = [e for e in routing_events if e.get("transmitted")]
        filtered = [e for e in routing_events if not e.get("transmitted")]
        values = [e.get("value", 0.0) for e in routing_events]

        msg_type_dist: Dict[str, int] = {}
        for e in routing_events:
            t = e.get("type", "unknown")
            msg_type_dist[t] = msg_type_dist.get(t, 0) + 1

        return {
            "total_messages_routed": len(routing_events),
            "total_transmitted": len(transmitted),
            "total_filtered": len(filtered),
            "avg_value_score": sum(values) / len(values) if values else 0.0,
            "max_value_score": max(values) if values else 0.0,
            "rejection_rate": len(filtered) / len(routing_events) if routing_events else 0.0,
            "top_message_types": msg_type_dist,
        }

    def _compute_heterogeneous_worker_metrics(self) -> Dict[str, Any]:
        """Heterogeneous worker diversity and utilization (阶段六: 异构Worker)."""
        nodes = list(self.graph.nodes.values())

        role_counts: Dict[str, int] = {}
        resource_tier_counts: Dict[str, int] = {}
        for node in nodes:
            role_counts[node.role] = role_counts.get(node.role, 0) + 1
            resource_tier_counts[node.resource_tier] = resource_tier_counts.get(node.resource_tier, 0) + 1

        unique_roles = len(role_counts)

        # Heterogeneity score: Shannon entropy of role distribution normalized
        import math
        total_nodes = max(1, len(nodes))
        entropy = 0.0
        for count in role_counts.values():
            p = count / total_nodes
            if p > 0:
                entropy -= p * math.log2(p)
        max_entropy = math.log2(max(1, unique_roles)) if unique_roles > 1 else 1.0
        heterogeneity_score = entropy / max_entropy if max_entropy > 0 else 0.0

        # Check how many of the 9 canonical worker types are represented
        canonical_types = {
            "Retrieval", "Reasoning", "Coding", "Tool", "Vision",
            "Device", "Critic", "Verifier", "Recovery",
        }
        matched_types = set()
        for role in role_counts:
            for ct in canonical_types:
                if ct.lower() in role.lower():
                    matched_types.add(ct)

        return {
            "total_worker_types": unique_roles,
            "unique_roles_used": unique_roles,
            "heterogeneity_score": heterogeneity_score,
            "canonical_worker_types_matched": len(matched_types),
            "canonical_worker_types_total": len(canonical_types),
            "role_distribution": role_counts,
            "resource_tier_distribution": resource_tier_counts,
        }


    def save_metrics(self, metrics: Dict[str, Any]) -> Path:
        """Save metrics to metrics.json in run_dir."""
        metrics_path = RunOutputLayout(self.run_dir).report("metrics.json")
        metrics_path.write_text(
            json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return metrics_path

    def generate_metrics_table(self, metrics: Dict[str, Any]) -> str:
        """Generate a human-readable metrics table."""
        lines = [
            "# AstralNexus Phase 4 Competition Metrics",
            "",
            f"**Generated**: {metrics.get('timestamp', 'N/A')}",
            f"**Run Directory**: {metrics.get('run_dir', 'N/A')}",
            "",
        ]

        # Performance summary
        perf = metrics.get("performance_metrics", {})
        lines.extend([
            "## Performance Summary",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Elapsed | {perf.get('total_elapsed_seconds', 0):.2f}s |",
            f"| Total Iterations | {perf.get('total_iterations', 0)} |",
            f"| Completed Tasks | {perf.get('completed_tasks', 0)} |",
            f"| Failed Tasks | {perf.get('failed_tasks', 0)} |",
            f"| Recovery Attempts | {perf.get('recovery_attempts', 0)} |",
            f"| Avg Seconds/Iteration | {perf.get('avg_seconds_per_iteration', 0):.3f}s |",
            f"| Throughput | {perf.get('throughput_tasks_per_second', 0):.2f} tasks/s |",
            "",
        ])

        # Task graph
        tg = metrics.get("task_graph_metrics", {})
        lines.extend([
            "## 1. Task Graph Structure",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Nodes | {tg.get('total_nodes', 0)} |",
            f"| Total Edges | {tg.get('total_edges', 0)} |",
            f"| Dependency Edges | {tg.get('dependency_edges', 0)} |",
            f"| Communication Edges | {tg.get('communication_edges', 0)} |",
            f"| Avg Dependencies/Node | {tg.get('avg_dependencies_per_node', 0):.2f} |",
            "",
        ])

        # Router
        router = metrics.get("router_metrics", {})
        lines.extend([
            "## 2. Router Topology",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Sparsity Samples | {router.get('samples', 0)} |",
            f"| Avg Sparsity | {router.get('avg_sparsity', 0):.4f} |",
            f"| Min Sparsity | {router.get('min_sparsity', 0):.4f} |",
            f"| Max Sparsity | {router.get('max_sparsity', 0):.4f} |",
            "",
        ])

        # Ready-wave
        wave = metrics.get("ready_wave_metrics", {})
        lines.extend([
            "## 3. Ready-Wave Parallel Execution",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Waves | {wave.get('total_waves', 0)} |",
            f"| Avg Wave Size | {wave.get('avg_wave_size', 0):.2f} |",
            f"| Max Wave Size | {wave.get('max_wave_size', 0)} |",
            f"| Min Wave Size | {wave.get('min_wave_size', 0)} |",
            "",
        ])

        # Compression
        comp = metrics.get("compression_metrics", {})
        lines.extend([
            "## 4. Context Compression",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Compression Events | {comp.get('compression_count', 0)} |",
            f"| Avg Ratio | {comp.get('avg_ratio', 0):.4f} |",
            f"| Min Ratio | {comp.get('min_ratio', 0):.4f} |",
            f"| Max Ratio | {comp.get('max_ratio', 0):.4f} |",
            f"| Natural Triggers | {comp.get('trigger_distribution', {}).get('natural', 0)} |",
            f"| Forced Triggers | {comp.get('trigger_distribution', {}).get('forced', 0)} |",
            "",
        ])

        # Communication
        comm = metrics.get("communication_metrics", {})
        lines.extend([
            "## 5. Low-Entropy Communication",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Messages Sent | {comm.get('messages_sent', 0)} |",
            f"| Quality Checks | {comm.get('quality_checks', 0)} |",
            f"| Avg Payload Efficiency | {comm.get('avg_payload_efficiency', 0):.4f} |",
            f"| Avg Claim Evidence Coverage | {comm.get('avg_claim_evidence_coverage', 0):.4f} |",
            "",
        ])

        # Fact recall
        fact = metrics.get("fact_recall_metrics", {})
        lines.extend([
            "## 6. Fact Recall",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Facts Preserved | {fact.get('total_facts_preserved', 0)} |",
            f"| Facts Recalled | {fact.get('total_facts_recalled', 0)} |",
            f"| Recall Rate | {fact.get('recall_rate', 0):.4f} |",
            "",
        ])

        # Constraint
        constraint = metrics.get("constraint_metrics", {})
        lines.extend([
            "## 7. Constraint Retention",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Checks | {constraint.get('total_checks', 0)} |",
            f"| Total Violations | {constraint.get('total_violations', 0)} |",
            f"| Retention Rate | {constraint.get('retention_rate', 0):.4f} |",
            "",
        ])

        # Fault recovery
        fault = metrics.get("fault_recovery_metrics", {})
        lines.extend([
            "## 8. Fault Recovery",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Injections | {fault.get('total_injections', 0)} |",
            f"| Recovery Attempts | {fault.get('total_recovery_attempts', 0)} |",
            f"| Recovery Successes | {fault.get('total_recovery_successes', 0)} |",
            f"| Success Rate | {fault.get('recovery_success_rate', 0):.4f} |",
            "",
            "**Fault Type Distribution**:",
            "",
        ])
        for ft, count in fault.get("fault_type_distribution", {}).items():
            lines.append(f"- {ft}: {count}")
        lines.append("")

        # Scheduling
        sched = metrics.get("scheduling_metrics", {})
        lines.extend([
            "## 9. Resource Scheduling",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Decisions | {sched.get('total_scheduling_decisions', 0)} |",
            f"| Local Ratio | {sched.get('local_ratio', 0):.4f} |",
            f"| Edge Ratio | {sched.get('edge_ratio', 0):.4f} |",
            f"| Cloud Ratio | {sched.get('cloud_ratio', 0):.4f} |",
            "",
        ])

        # Execution path
        exec_path = metrics.get("execution_path_metrics", {})
        lines.extend([
            "## 10. Execution Path",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Executions | {exec_path.get('total_executions', 0)} |",
            f"| Runtime Ratio | {exec_path.get('runtime_ratio', 0):.4f} |",
            "",
        ])

        # Quality
        quality = metrics.get("quality_metrics", {})
        lines.extend([
            "## 11. Quality Indicators",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Quality Samples | {quality.get('quality_samples', 0)} |",
            f"| Avg Payload Efficiency | {quality.get('avg_payload_efficiency', 0):.4f} |",
            f"| Avg Key Field Retention | {quality.get('avg_key_field_retention', 0):.4f} |",
            "",
        ])

        # 阶段六新增：动态异构群体指标
        dyn_spawn = metrics.get("dynamic_spawning_metrics", {})
        lines.extend([
            "## 12. Dynamic Spawning (阶段六)",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Spawn Events | {dyn_spawn.get('total_spawn_events', 0)} |",
            f"| Spawn Success Rate | {dyn_spawn.get('spawn_success_rate', 0):.4f} |",
            f"| Trigger Distribution | {dyn_spawn.get('trigger_distribution', {})} |",
            "",
        ])

        val_route = metrics.get("value_routing_metrics", {})
        lines.extend([
            "## 13. Value Routing (阶段六)",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Messages Routed | {val_route.get('total_messages_routed', 0)} |",
            f"| Avg Value Score | {val_route.get('avg_value_score', 0):.4f} |",
            f"| Rejection Rate | {val_route.get('rejection_rate', 0):.4f} |",
            f"| Top Message Types | {val_route.get('top_message_types', {})} |",
            "",
        ])

        hetero = metrics.get("heterogeneous_worker_metrics", {})
        lines.extend([
            "## 14. Heterogeneous Workers (阶段六)",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Worker Types | {hetero.get('total_worker_types', 0)} |",
            f"| Unique Roles Used | {hetero.get('unique_roles_used', 0)} |",
            f"| Heterogeneity Score | {hetero.get('heterogeneity_score', 0):.4f} |",
            f"| Role Distribution | {hetero.get('role_distribution', {})} |",
            "",
        ])

        return "\n".join(lines)
