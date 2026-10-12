"""Interruption responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .support import (
    Any,
    Dict,
    Empty,
    Event,
    List,
    Optional,
    RealLLMRequiredError,
    TaskStatus,
    can_transition,
    datetime,
    timezone,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def attach_interrupt_control(self: Orchestrator, interrupt_event: Event, interrupt_queue) -> None:
    """Attach the interactive controller without coupling core to the TUI."""
    self._interrupt_event = interrupt_event
    self._interrupt_queue = interrupt_queue
    self.trace_recorder.record("orchestrator.interrupt_control.attached", {})


def _cancel_requested(self: Orchestrator) -> bool:
    return bool(self._ownership_lost or (self._interrupt_event is not None and self._interrupt_event.is_set()))


def _process_pending_interrupts(self: Orchestrator) -> Dict[str, Any]:
    """Apply queued changes at a graph-safe orchestration boundary."""
    if self._interrupt_queue is None:
        return {"stop": self._cancel_requested(), "changed": False}
    pending = []
    while True:
        try:
            item = self._interrupt_queue.get_nowait()
        except Empty:
            break
        pending.append(item if isinstance(item, dict) else {"type": "interrupt", "message": str(item)})
    changes = [item for item in pending if item.get("type") in {"requirement_change", "inject_requirement"}]
    stops = [item for item in pending if item not in changes]
    stop = bool(stops or self._ownership_lost or (self._cancel_requested() and not changes))
    if stop:
        if stops:
            self._interrupt_reason = str(stops[-1].get("message") or "user_interrupt")
        # Do not invoke a replanner after an explicit stop. Retain queued
        # requirements for the controlling session to decide on resumption.
        for item in changes:
            self._interrupt_queue.put(item)
        return {"stop": True, "changed": False, "deferred_changes": len(changes)}
    for item in changes:
        description = str(item.get("description") or item.get("message") or "")
        self.trace_recorder.record("requirement_change.queued", {
            "description": description[:2000], "change_kind": item.get("change_kind"),
        })
        self.handle_requirement_change(description,
            affected_constraints=list(item.get("affected_constraints") or []),
            change_kind=item.get("change_kind"), resume=False)
    return {"stop": False, "changed": bool(changes)}


def _clear_requirement_interrupt(self: Orchestrator) -> None:
    """Clear only a consumed change signal, atomically with the standard queue."""
    queue = self._interrupt_queue
    if self._interrupt_event is None or queue is None or self._ownership_lost:
        return
    # Producers enqueue before setting the event. Holding queue.mutex closes
    # the drain/clear race: a new stop is either already queued or sets its
    # event after this critical section.
    with queue.mutex:
        if not queue.queue:
            self._interrupt_event.clear()


def _mark_interrupted(self: Orchestrator, reason: str) -> None:
    self._interrupt_reason = reason or "user_interrupt"
    self._interrupt_requested_at = datetime.now(timezone.utc).isoformat()
    self._save_checkpoint()
    self._persist_graph_checkpoint(stage="interrupted")
    self.trace_recorder.record("orchestrator.interrupted", {
        "reason": self._interrupt_reason,
        "requested_at": self._interrupt_requested_at,
        "iteration": self.iteration_count,
    })


def handle_requirement_change(
    self: Orchestrator,
    change_description: str,
    affected_constraints: List[str] | None = None,
    *,
    change_kind: Optional[str] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Handle requirement changes at runtime (Phase 3: 需求变更处理).

    Implements the requirement change recovery strategy from 天枢方案书 §5.6:
    1. Detect requirement change fault
    2. Analyze impact on task graph (locate affected subgraph)
    3. Generate recovery plan (replan affected nodes)
    4. Execute recovery (reset affected nodes to pending)

    Args:
        change_description: Description of the requirement change
        affected_constraints: Optional list of constraint IDs that changed

    Returns:
        Dictionary with recovery results including affected nodes and recovery status
    """
    # GlobalGovernor is the only authority allowed to classify and plan a
    # requirement change before graph, worker, or context mutation.
    if getattr(self.governor, "llm_matching_enabled", False):
        decision = self.governor.plan_requirement_change(
            change_description,
            change_kind=change_kind,
            current_goal=(
                self.governor.goal_spec.task_goal
                if self.governor.goal_spec else
                next((n.goal for n in self.task_graph.nodes.values()), "")
            ),
            current_constraints=list(affected_constraints or []),
        )
        previous_version = self.governor.goal_version
        self.governor.goal_version = previous_version + 1
        version = self.governor.goal_version
        affected = list(decision.get("affected_node_ids", []))
        patch = decision.get("patch")
        patch_result: Dict[str, Any] = {}
        if patch is not None:
            try:
                for repair_attempt in range(3):
                    try:
                        patch_result = self.governor.apply_task_graph_patch(patch)
                        break
                    except ValueError as exc:
                        self.trace_recorder.record("requirement_change.patch_rejected", {
                            "attempt": repair_attempt + 1, "error": str(exc),
                            "previous_version": previous_version,
                        })
                        if repair_attempt == 2:
                            raise
                        graph_summary, capabilities, resources = self._build_governor_planning_context()
                        repair = self.governor.replan_rejected_task_graph_patch(
                            change_description, rejection_reason=str(exc),
                            graph_summary=graph_summary, capability_catalog=capabilities,
                            resource_catalog=resources, rejected_patch=patch,
                        )
                        patch = self.governor.merge_rejected_task_graph_patch(patch, repair)
            except Exception:
                # A rejected atomic patch must not consume a task version
                # or be reported as an applied requirement change.
                self.governor.goal_version = previous_version
                raise
            # doc 18 修复C：需求变更补丁若产生替换，下游死边同场改写。
            self._rewire_superseded_dependents()
            for added_node_id in patch_result.get("added", []):
                added_node = self.task_graph.get_node(added_node_id)
                if added_node is not None:
                    self.memory_federation.sync_task_node(added_node)

        new_constraints = list(decision.get("new_constraints", []))
        all_constraints = list(dict.fromkeys(
            [str(x) for x in (affected_constraints or []) + new_constraints]
        ))
        for index, constraint in enumerate(all_constraints):
            self.context_manager.add_constraint(
                f"requirement-v{version}-{index}", constraint
            )
            self.memory_federation.add_constraint_change(
                constraint,
                source=f"requirement-v{version}",
                affected_nodes=affected,
            )
        if self.governor.goal_spec:
            self.governor.goal_spec.hard_constraints = list(dict.fromkeys(
                self.governor.goal_spec.hard_constraints + all_constraints
            ))
            self.governor.goal_spec.task_version = version

        # A lost worker is a capability failure, not a text-only role
        # rename.  Preserve the node and evidence, then make it eligible
        # for a Governor-selected replacement or repair worker.
        takeover = decision.get("worker_takeover") or {}
        lost_nodes = [str(x) for x in takeover.get("lost_node_ids", [])]
        for node_id in dict.fromkeys(affected + lost_nodes):
            node = self.task_graph.get_node(node_id)
            if not node:
                continue
            node.metadata.setdefault("requirement_history", []).append({
                "task_version": version,
                "description": change_description,
                "change_kind": decision.get("change_kind", change_kind or "other"),
            })
            if node_id in lost_nodes:
                node.metadata["worker_lost"] = True
                node.metadata["worker_takeover_required"] = True
                if can_transition(node.status, "failed"):
                    self.task_graph.update_node_status(node_id, "failed")
                if can_transition(node.status, "ready"):
                    self.task_graph.update_node_status(node_id, "ready")

        context_policy = decision.get("context_policy") or {}
        multiplier = context_policy.get("budget_multiplier")
        if isinstance(multiplier, (int, float)) and 0 < multiplier < 1:
            from AstralNexus.core.governor_budget import resize_token_limit
            self.governor.budget.max_tokens = resize_token_limit(
                self.governor.budget.max_tokens, float(multiplier)
            )
            self.context_manager.max_context_tokens = max(
                1024, int(self.context_manager.max_context_tokens * float(multiplier))
            )
        self.trace_recorder.record("requirement_change.applied", {
            "task_version": version,
            "previous_version": previous_version,
            "change_kind": decision.get("change_kind", change_kind or "other"),
            "affected_node_ids": affected,
            "lost_node_ids": lost_nodes,
            "patch": patch_result,
            "new_constraints": all_constraints,
            "context_policy": context_policy,
            "reverification": decision.get("reverification", []),
        })
        self._persist_graph_checkpoint(stage="requirement_change")
        result = {
            "fault_type": "requirement_change",
            "injected": True,
            "task_version": version,
            "change_kind": decision.get("change_kind", change_kind or "other"),
            "affected_nodes": affected,
            "lost_nodes": lost_nodes,
            "patch": patch_result,
            "new_constraints": all_constraints,
            "context_policy": context_policy,
            "reverification": decision.get("reverification", []),
            "resume_required": bool(patch_result.get("added") or lost_nodes),
        }
        if resume and result["resume_required"]:
            root_goal = next((n.goal for n in self.task_graph.nodes.values()), "")
            result["resume_result"] = self.run(goal=root_goal, constraints=[])
        return result

    raise RealLLMRequiredError(
        "Requirement changes require GlobalGovernor real-LLM planning"
    )


def handle_resource_degradation(self: Orchestrator, resource_id: str, degradation_type: str) -> Dict[str, Any]:
    """Handle resource degradation through Governor-approved reassignment.

    Implements the resource degradation recovery strategy from 天枢方案书 §5.6:
    1. Detect resource degradation fault
    2. Analyze impact on tasks using the resource
    3. Ask the Governor for a replacement-resource recovery plan
    4. Execute recovery (reassign affected tasks)

    Args:
        resource_id: ID of the degraded resource
        degradation_type: Type of degradation (e.g., "high_latency", "low_memory", "unavailable")

    Returns:
        Dictionary with recovery results including affected tasks and reassignment status
    """
    self.trace_recorder.record("resource_degradation.detected", {
        "resource_id": resource_id,
        "degradation_type": degradation_type,
    })

    # Detect the resource degradation fault
    fault = self.fault_detector.detect_resource_degradation(
        resource_id,
        degradation_type
    )

    # Analyze impact - find tasks using this resource
    impact = self.impact_analyzer.analyze_impact(fault)
    impacted_nodes = impact.get("impacted_nodes", [])

    self.trace_recorder.record("resource_degradation.impact_analysis", {
        "fault_id": fault.fault_id,
        "impacted_node_count": len(impacted_nodes),
        "impacted_nodes": impacted_nodes[:20],
    })

    governed = self._execute_governed_fault(fault, impact)
    result = {
        "fault_type": "resource_degradation",
        "injected": True,
        "fault_id": fault.fault_id,
        "resource_id": resource_id,
        "degradation_type": degradation_type,
        "impacted_nodes": impacted_nodes,
        "recovery_attempted": True,
        "recovery_success": governed["success"],
        "recovery_plan": governed["plan"],
        "recovery_execution": governed["execution"],
    }

    self.trace_recorder.record("resource_degradation.recovery_complete", result)

    return result


def inject_fault(self: Orchestrator, fault_type: str, target: str, fault_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Inject a fault through the canonical governed recovery pipeline."""
    fault_type = str(fault_type).strip().lower()
    fault_data = dict(fault_data or {})
    supported = {
        "requirement_change",
        "node_failure",
        "tool_failure",
        "memory_loss",
        "resource_degradation",
    }
    if fault_type not in supported:
        raise ValueError(f"Unsupported fault type: {fault_type}")
    self.trace_recorder.record(
        "fault_injection.requested",
        {
            "fault_type": fault_type,
            "target": target,
            "fault_data": fault_data,
        },
    )
    if fault_type == "requirement_change":
        result = self.handle_requirement_change(
            str(
                fault_data.get("description")
                or "Injected requirement change"
            ),
            list(fault_data.get("affected_constraints") or []),
        )
        result.update(
            {
                "fault_type": fault_type,
                "target": target,
                "injected": True,
            }
        )
    else:
        if fault_type == "node_failure":
            node = self.task_graph.get_node(target)
            if node is None:
                raise KeyError(f"Unknown fault target node: {target}")
            if can_transition(node.status, TaskStatus.FAILED.value):
                self.task_graph.update_node_status(
                    target, TaskStatus.FAILED.value
                )
        fault = self.fault_detector.detect_fault(
            fault_type,
            target,
            fault_data,
        )
        impact = self.impact_analyzer.analyze_impact(fault)
        governed = self._execute_governed_fault(fault, impact)
        result = {
            "fault_type": fault_type,
            "target": target,
            "injected": True,
            "fault_id": fault.fault_id,
            "impacted_nodes": list(impact.get("impacted_nodes", [])),
            "recovery_attempted": True,
            "recovery_success": governed["success"],
            "recovery_plan": governed["plan"],
            "recovery_execution": governed["execution"],
        }
    self._persist_graph_checkpoint(stage="fault_injection", node_id=target)
    self.trace_recorder.record("fault_injection.completed", result)
    return result


