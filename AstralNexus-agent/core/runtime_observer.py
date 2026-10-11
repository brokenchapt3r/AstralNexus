from __future__ import annotations

from typing import Any, Dict, List, Optional

from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.state_machine import RunStatus, RuntimePhase


class RuntimeObserver:
    """Read-only projection of the canonical runtime state.

    Orchestrator owns the control loop. This observer only projects graph,
    lifecycle, memory and resource state and asks Router for a communication
    view. It cannot plan, mutate the task graph, persist checkpoints, or
    execute recovery.
    """

    def __init__(self, orchestrator: Any, state_manager: RuntimeStateManager):
        self.orchestrator = orchestrator
        self.state_manager = state_manager

    def observe(
        self,
        *,
        goal: str,
        phase: str,
        decision: Optional[Dict[str, Any]] = None,
        track_progress: bool = False,
        stop_reason: str = "",
    ):
        owner = self.orchestrator
        phase_value = str(phase)
        terminal_status = {
            RuntimePhase.COMPLETED.value: RunStatus.COMPLETED.value,
            RuntimePhase.FAILED.value: RunStatus.FAILED.value,
            RuntimePhase.BLOCKED.value: RunStatus.BLOCKED.value,
            RuntimePhase.INTERRUPTED.value: RunStatus.INTERRUPTED.value,
            RuntimePhase.CANCELLED.value: RunStatus.CANCELLED.value,
        }
        run_status = terminal_status.get(
            phase_value,
            (
                RunStatus.CREATED.value
                if phase_value == RuntimePhase.INITIALIZED.value
                else RunStatus.RUNNING.value
            ),
        )
        if phase_value == RuntimePhase.COMPLETED.value and self.state_manager.state.delivery_status != "passed":
            run_status = RunStatus.RUNNING.value  # Execution done, product terminal not committed.
        state = self.state_manager.observe(
            task_graph=owner.task_graph,
            global_goal=goal,
            task_version=int(getattr(owner.governor, "goal_version", 1) or 1),
            iteration=owner.iteration_count,
            phase=phase,
            run_status=run_status,
            lifecycle_manager=owner.lifecycle_manager,
            memory_federation=owner.memory_federation,
            resource_scheduler=owner.resource_scheduler,
            topology_override=owner._current_topology_state,
            decision=decision,
            track_progress=track_progress,
            stop_reason=stop_reason,
        )
        owner.trace_recorder.record(
            f"orchestrator.autonomous_cycle.{phase}",
            {
                "iteration": state.iteration,
                "task_version": state.task_version,
                "node_count": state.node_count,
                "edge_count": state.edge_count,
                "nodes_by_status": state.nodes_by_status,
                "ready_nodes": state.ready_nodes,
                "active_agent_count": len(state.active_agents),
                "uncertainty": state.uncertainty,
                "repeated_no_progress": state.repeated_no_progress,
                "runtime_state_path": str(self.state_manager.path),
                "decision": decision or {},
            },
        )
        return state

    def refresh_cognitive_topology(self, node_ids: List[str]) -> Dict[str, Any]:
        owner = self.orchestrator
        active_ids = [
            str(node_id)
            for node_id in node_ids
            if owner.task_graph.get_node(str(node_id)) is not None
        ]
        if not active_ids:
            owner._current_topology_state = {
                "dependency_edges": [],
                "semantic_edges": [],
                "all_edges": [],
                "metrics": {
                    "total_nodes": 0,
                    "total_edge_count": 0,
                    "sparsity": 1.0,
                },
            }
        else:
            owner._current_topology_state = owner.router.build_communication_plan(
                active_ids
            )
        owner.trace_recorder.record(
            "orchestrator.cognitive_topology.refreshed",
            {
                "iteration": owner.iteration_count,
                "active_node_ids": active_ids,
                "topology": owner._current_topology_state,
            },
        )
        return owner._current_topology_state
