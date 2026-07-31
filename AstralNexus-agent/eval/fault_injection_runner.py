"""Fault injection runner: automatically injects 4 fault types during evaluation.

Injects faults at strategic points during execution and collects recovery evidence.
"""
from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph


class FaultInjectionRunner:
    """Orchestrates fault injection during long-task evaluation."""

    def __init__(self, orchestrator: Orchestrator, seed: Optional[int] = None):
        self.orchestrator = orchestrator
        self.rng = random.Random(seed)
        self.injection_log: List[Dict[str, Any]] = []

    def plan_injections(
        self,
        task_graph: TaskGraph,
        expected_node_count: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Plan fault injections based on task graph structure.

        Strategy:
        - 1 node_failure: inject at ~30% execution
        - 1 tool_failure: inject at ~50% execution
        - 1 resource_degradation: inject at ~70% execution
        - 1 requirement_change: inject at ~90% execution
        """
        nodes = list(task_graph.nodes.values())
        planning_size = expected_node_count or len(nodes)
        if planning_size < 10:
            return []

        # Sample nodes at different execution phases
        phase_30_idx = int(planning_size * 0.3)
        phase_50_idx = int(planning_size * 0.5)
        phase_70_idx = int(planning_size * 0.7)
        phase_90_idx = int(planning_size * 0.9)

        injections = [
            {
                "fault_type": "node_failure",
                # Resolve against the live graph when the first dynamic wave
                # has been admitted; no fixture node ID is assumed.
                "target": "__dynamic_node__",
                "fault_data": {"error_message": "Simulated node execution failure"},
                "trigger_at_task_count": phase_30_idx,
            },
            {
                "fault_type": "tool_failure",
                "target": "simulated_tool",
                "fault_data": {"error_message": "Simulated tool execution timeout"},
                "trigger_at_task_count": phase_50_idx,
            },
            {
                "fault_type": "resource_degradation",
                # Match the default resource pool used by Orchestrator.
                "target": "edge-1",
                "fault_data": {
                    "degradation_factor": 0.5,
                    "affected_capabilities": ["compute", "memory"],
                },
                "trigger_at_task_count": phase_70_idx,
            },
            {
                "fault_type": "requirement_change",
                "target": "system",
                "fault_data": {
                    "description": "新增需求：所有模块必须支持分布式部署",
                    "affected_constraints": ["deployment_mode"],
                },
                "trigger_at_task_count": phase_90_idx,
            },
        ]

        return injections

    def check_and_inject(
        self,
        injections: List[Dict[str, Any]],
        completed_task_count: int,
    ) -> Optional[Dict[str, Any]]:
        """Check if it's time to inject a fault and execute it.

        Args:
            injections: List of planned injections
            completed_task_count: Number of tasks completed so far

        Returns:
            Injection result if a fault was injected, None otherwise
        """
        for injection in injections:
            if injection.get("injected"):
                continue

            trigger_at = injection.get("trigger_at_task_count", 0)
            if completed_task_count >= trigger_at:
                result = self._inject_fault(injection)
                injection["injected"] = True
                injection["injected_at"] = completed_task_count
                injection["result"] = result
                self.injection_log.append(injection)
                return result

        return None

    def _inject_fault(self, injection: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single fault injection."""
        fault_type = injection["fault_type"]
        target = injection["target"]
        if target == "__dynamic_node__":
            candidates = [
                node for node in self.orchestrator.task_graph.nodes.values()
                if node.node_id != "root"
                and node.status in {"admitted", "waiting_dependencies", "ready", "running"}
            ]
            if candidates:
                target = candidates[0].node_id
                injection["target"] = target
            else:
                return {
                    "fault_type": fault_type,
                    "target": target,
                    "injected": False,
                    "reason": "no_live_dynamic_node",
                }
        fault_data = injection.get("fault_data", {})

        print(f"  [FAULT] Injecting fault: {fault_type} -> {target}")

        try:
            result = self.orchestrator.inject_fault(fault_type, target, fault_data)
            print(f"     Recovery: {'success' if result.get('recovery_success') else 'failed'}")
            return result
        except Exception as e:
            print(f"     ✗ Injection failed: {e}")
            return {
                "fault_type": fault_type,
                "target": target,
                "injected": False,
                "error": str(e),
            }

    def generate_injection_report(self) -> str:
        """Generate a human-readable fault injection report."""
        lines = [
            "# Fault Injection Report",
            "",
            f"**Total Injections**: {len(self.injection_log)}",
            "",
        ]

        for i, injection in enumerate(self.injection_log, 1):
            lines.extend([
                f"## Injection {i}: {injection['fault_type']}",
                "",
                f"- **Target**: {injection['target']}",
                f"- **Injected At**: {injection.get('injected_at', 'N/A')} tasks completed",
                f"- **Fault Data**: {injection.get('fault_data', {})}",
                "",
            ])

            result = injection.get("result", {})
            if result:
                lines.extend([
                    "### Result",
                    "",
                    f"- **Injected**: {result.get('injected', False)}",
                    f"- **Recovery Attempted**: {result.get('recovery_attempted', False)}",
                    f"- **Recovery Success**: {result.get('recovery_success', False)}",
                ])
                if "error" in result:
                    lines.append(f"- **Error**: {result['error']}")
                lines.append("")

        return "\n".join(lines)
