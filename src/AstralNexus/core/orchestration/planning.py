"""Planning responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .support import (
    Any,
    Artifact,
    ArtifactAcceptanceStatus,
    Dict,
    HETEROGENEOUS_WORKERS,
    InterruptionRequested,
    List,
    Optional,
    Path,
    RealLLMRequiredError,
    Resource,
    ResourcePool,
    ResourceTier,
    TaskGraph,
    TaskStatus,
    time,
    os,
    TraceRecorder,
    json,
    logger,
    re,
    transition_repair_plan,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def _sync_task_graph_bindings(self: Orchestrator) -> None:
    """Make every orchestration subsystem observe the canonical graph.

    Evaluation and embedding callers may inject a prebuilt graph after
    constructing the orchestrator.  Keeping only ``self.task_graph`` in
    sync is unsafe because Governor, Router, Verifier and recovery
    components retain their own references.  A dynamic admission then
    appears in one graph while scheduling reads another.
    """
    self._execution_lock = self.task_graph._mutation_lock
    graph_bound_components = (
        self.router,
        self.governor,
        self.verifier,
        self.fault_detector,
        self.impact_analyzer,
        self.recovery_executor,
        self.fault_recovery_engine,
    )
    for component in graph_bound_components:
        if component is not None and hasattr(component, "task_graph"):
            component.task_graph = self.task_graph
        if component is not None and hasattr(component, "_task_graph"):
            component._task_graph = self.task_graph


def _commit_router_communication_edge(self: Orchestrator, proposal) -> None:
    """Commit a Router-selected communication link through Governor."""
    from AstralNexus.core.governor import TaskGraphPatch

    applied = self.governor.apply_task_graph_patch(
        TaskGraphPatch(
            add_edges=[
                {
                    "source": proposal.source,
                    "target": proposal.target,
                    "edge_type": "communicates_to",
                    "metadata": {
                        "router_edge_type": proposal.edge_type,
                        "condition": proposal.condition,
                        "value_score": proposal.value_score,
                        "reasoning": proposal.reasoning,
                    },
                }
            ],
            reason_codes=["ROUTER_COMMUNICATION_LINK"],
            reasoning=proposal.reasoning,
        )
    )
    source_node = self.task_graph.get_node(proposal.source)
    target_node = self.task_graph.get_node(proposal.target)
    self.trace_recorder.record(
        "agent.communication.link_created",
        {
            "source": proposal.source,
            "target": proposal.target,
            "source_agent_id": (
                source_node.metadata.get("worker_agent_id")
                if source_node else None
            ),
            "target_agent_id": (
                target_node.metadata.get("worker_agent_id")
                if target_node else None
            ),
            "edge_type": proposal.edge_type,
            "condition": proposal.condition,
            "value_score": proposal.value_score,
            "reason": proposal.reasoning,
            "patch_reason_codes": applied.get("reason_codes", []),
        },
    )
    self._persist_graph_checkpoint(stage="router_communication_link")


def rebind_runtime(self: Orchestrator, *, task_graph: TaskGraph, trace_recorder: TraceRecorder) -> None:
    """Rebind injected graph and trace sink across the control plane.

    Evaluation and embedding callers may provide a prebuilt graph after
    construction.  Governor decisions must then observe the same graph
    and the same event log as Router, Verifier and recovery components.
    """
    self.task_graph = task_graph
    self.trace_recorder = trace_recorder
    components = (
        self.router, self.governor, self.verifier,
        self.fault_detector, self.impact_analyzer,
        self.recovery_executor, self.fault_recovery_engine,
    )
    for component in components:
        if component is None:
            continue
        if hasattr(component, "task_graph"):
            component.task_graph = task_graph
        if hasattr(component, "_task_graph"):
            component._task_graph = task_graph
        if hasattr(component, "trace_recorder"):
            component.trace_recorder = trace_recorder


def _create_default_resource_pool(self: Orchestrator) -> ResourcePool:
    """Create a default resource pool with local, edge, and cloud tiers."""
    pool = ResourcePool()

    # Local resources (fast, private, low cost)
    pool.add_resource(Resource(
        resource_id="local-1",
        tier=ResourceTier.LOCAL,
        privacy="high",
        cost="low",
        latency="low",
        capabilities=["file_operations", "terminal", "code_execution"],
    ))

    # Edge resources (medium balance)
    pool.add_resource(Resource(
        resource_id="edge-1",
        tier=ResourceTier.EDGE,
        privacy="medium",
        cost="medium",
        latency="medium",
        capabilities=["research", "documentation", "medium_reasoning"],
    ))

    # Cloud resources (powerful, expensive)
    pool.add_resource(Resource(
        resource_id="cloud-1",
        tier=ResourceTier.CLOUD,
        privacy="low",
        cost="high",
        latency="high",
        capabilities=["complex_reasoning", "large_context", "programming"],
    ))

    return pool


def register_staged_inputs(self: Orchestrator, paths: List[str]) -> None:
    """Register run-relative input paths for admission with user intent.

    Staging and graph provenance are separate concerns: the CLI copies
    files into the isolated workspace, while Orchestrator makes those
    files auditable graph artifacts before the Planner creates consumers.
    """
    normalized: List[str] = []
    for value in paths or []:
        candidate = Path(str(value).replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"staged input must be run-relative: {value}")
        relative = candidate.as_posix()
        if relative not in normalized:
            normalized.append(relative)
    self._staged_input_paths = normalized


def _input_artifact_id(path: str) -> str:
    name = Path(path).name.lower()
    normalized = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", name).strip("_")
    return f"artifact:{normalized or 'input'}"


def _admit_staged_input_artifacts(self: Orchestrator, intent_node_id: str) -> None:
    used_ids = set(self.task_graph.artifacts)
    for relative_path in self._staged_input_paths:
        absolute_path = (self.run_dir / relative_path).resolve()
        if not absolute_path.is_file() or self.run_dir.resolve() not in absolute_path.parents:
            raise FileNotFoundError(f"staged input is missing from run workspace: {relative_path}")
        base_id = self._input_artifact_id(relative_path)
        artifact_id = base_id
        suffix = 2
        while artifact_id in used_ids:
            artifact_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(artifact_id)
        self.task_graph.add_artifact(Artifact(
            artifact_id=artifact_id,
            node_id=intent_node_id,
            artifact_type=absolute_path.suffix.lstrip(".") or "file",
            path=relative_path,
            description=f"User-provided staged input: {relative_path}",
            acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
            lifecycle_class="durable",
            metadata={"source": "user_input", "immutable": True},
        ))
        self.trace_recorder.record("artifact.created", {
            "artifact_id": artifact_id,
            "node_id": intent_node_id,
            "path": relative_path,
            "source": "user_input",
            "acceptance_status": "accepted",
        })


def initialize_task_graph(self: Orchestrator, goal: str, constraints: Optional[List[str]] = None) -> str:
    """Initialize the task graph with a root goal.

    Args:
        goal: The main goal to achieve
        constraints: List of constraints to consider

    Returns:
        The ID of the root task node
    """
    intent_node_id = "U-001"
    root_node_id = "P-001"
    constraints = list(constraints or [])
    self.governor.admit_root_goal(
        node_id=root_node_id,
        goal=goal,
        constraints=constraints,
        intent_node_id=intent_node_id,
    )
    self._admit_staged_input_artifacts(intent_node_id)
    self.task_graph.update_node_status(root_node_id, "ready")
    self.task_graph.update_node_status(root_node_id, "running")
    for admitted_node_id in (intent_node_id, root_node_id):
        self.memory_federation.sync_task_node(
            self.task_graph.get_node(admitted_node_id)
        )

    # Add constraints to context manager for versioning and recall
    for i, constraint in enumerate(constraints):
        self.context_manager.add_constraint(
            f"constraint-{i}",
            constraint,
            source_node_id=intent_node_id,
        )
        # Persist the same constraint revision in hierarchical memory.
        if self.memory_federation:
            try:
                constraint_id = self.memory_federation.add_constraint_change(
                    constraint_text=constraint,
                    source_node_id=intent_node_id,
                    affected_nodes=[intent_node_id, root_node_id],
                )
                self.trace_recorder.record("constraint.versioned", {
                    "constraint_id": constraint_id,
                    "node_id": root_node_id,
                    "constraint_index": i,
                })
            except Exception as e:
                logger.warning(f"Constraint versioning failed: {e}")

    # Record the initialization in trace
    self.trace_recorder.record(
        "orchestrator.initialized",
        {
            "root_node_id": root_node_id,
            "intent_node_id": intent_node_id,
            "goal": goal,
            "constraint_count": len(constraints),
        }
    )

    return root_node_id


def plan_task_decomposition(
    self: Orchestrator,
    node_id: str,
    *,
    planning_goal: Optional[str] = None,
    allow_completed_parent: bool = False,
) -> List[str]:
    """Ask GlobalGovernor for a patch and atomically apply it.

    Orchestrator owns lifecycle and atomic execution only. It neither
    generates subtasks nor interprets the planning response into a second
    graph representation.
    """
    node = self.task_graph.get_node(node_id)
    if not node:
        raise ValueError(f"Node {node_id} not found")


    # Direct callers and dynamically admitted nodes may arrive here before
    # the next scheduler promotion.  Never execute an admitted node as if
    # it were already running; promote it only when all dependencies are
    # terminally satisfied.
    if node.status in {"admitted", "waiting_dependencies"}:
        self.task_graph.promote_waiting_nodes()
        node = self.task_graph.get_node(node_id)
    allowed_statuses = {"ready", "running"}
    if allow_completed_parent:
        allowed_statuses.add("completed")
    if not node or node.status not in allowed_statuses:
        self.trace_recorder.record("task.execution.not_ready", {
            "node_id": node_id,
            "status": node.status if node else None,
            "dependencies": list(node.dependencies) if node else [],
        })
        return []
    if not self.governor.llm_matching_enabled or self.governor.llm_client is None:
        raise RealLLMRequiredError(
            "GlobalGovernor has no real LLM client; formal planning cannot start"
        )

    graph_summary, capability_catalog, resource_catalog = (
        self._build_governor_planning_context()
    )
    planner_goal = planning_goal or node.goal
    planner_agent_id = f"control-planner-{node_id}"
    self.trace_recorder.record(
        "agent.spawned",
        {
            "agent_id": planner_agent_id,
            "node_id": node_id,
            "role": "planner",
            "status": TaskStatus.RUNNING.value,
            "capabilities": ["task_decomposition", "graph_planning"],
            "resource_tier": "control_plane",
            "source": "control_plane",
            "reason": "GlobalGovernor created the planning execution instance",
        },
    )
    self.trace_recorder.record_node_input(
        node_id,
        {
            "node_id": node_id,
            "role": "planner",
            "goal": planner_goal,
            "constraints": list(node.constraints),
            "acceptance_criteria": list(node.acceptance_criteria),
            "graph_summary": graph_summary,
            "capability_catalog": capability_catalog,
            "resource_catalog": resource_catalog,
            "agent_id": planner_agent_id,
            "assigned_by": "GlobalGovernor",
            "prompt_source": "User Intent + Governor Planning Contract",
            "dependencies": ["U-001"],
            "task_version": node.metadata.get("task_version", 1),
            "attempt": node.retry_count,
        },
    )
    patch_error = ""
    applied = None
    def record_planner_stream(event: Dict[str, Any]) -> None:
        self._record_node_runtime_event(node_id, "planner", event)

    patch_attempt = 0
    calls_made = 0
    max_attempts = max(1, int(os.environ.get("ASTRALNEXUS_PLAN_MAX_ATTEMPTS", "5")))
    run_attempt_budget = max(1, int(os.environ.get("ASTRALNEXUS_RUN_PLAN_ATTEMPTS", "50")))
    deadline = time.monotonic() + max(1.0, float(os.environ.get("ASTRALNEXUS_PLAN_TIMEOUT", "1200")))
    patch_error_counts: Dict[str, int] = {}
    rejected_patch: TaskGraphPatch | None = None

    def patch_error_signature(message: str) -> str:
        lowered = str(message).lower()
        known_classes = {
            "overlapping output ownership": "overlapping_output_ownership",
            "unknown input artifact": "unknown_input_artifact",
            "artifact producer is not a declared dependency": "artifact_dependency_mismatch",
            "false_parallelism_single_wide_bottleneck": "false_parallelism",
            "claims parallelizable but creates one oversized": "false_parallelism",
            "invalid state transition": "invalid_state_transition",
            "unknown dependency": "unknown_dependency",
            "unknown edge endpoint": "unknown_edge_endpoint",
            "dependency_cycle": "dependency_cycle",
            "cycle": "dependency_cycle",
        }
        for marker, signature in known_classes.items():
            if marker in lowered:
                # Validator messages are deterministic. Retain the
                # conflicting node/artifact/path so fixing successive
                # references is not mistaken for repeating one failure.
                return signature + ":" + re.sub(r"\s+", " ", lowered).strip()
        # Remove volatile node IDs, paths and numbers so semantically
        # identical malformed proposals share one repeat budget.
        normalized = re.sub(r"[a-z]:[/\\][^\s'\"]+", "<path>", lowered)
        normalized = re.sub(r"\b(?:[a-z]+-)?\d+\b", "<id>", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized[:240] or "unknown_graph_patch_error"

    while applied is None:
        if self._cancel_requested():
            raise InterruptionRequested("planning interrupted")
        stopped = self._lineage_stand_down_verdict()
        if (patch_attempt >= max_attempts
                or self.runtime_state_manager.state.planning_attempts_total >= run_attempt_budget
                or time.monotonic() >= deadline
                or self._recovery_budget_exhausted or stopped):
            patch_error = patch_error or "planning attempt, cost or time budget exhausted"
            break
        self.runtime_state_manager.state.planning_attempts_total += 1
        if not self._persist_graph_checkpoint(stage="planning_attempt_reserved", node_id=node_id):
            raise InterruptionRequested("Planning ownership was lost before model dispatch")
        patch = None
        calls_made += 1
        try:
            if patch_attempt == 0:
                patch = self.governor.plan_task_graph_patch(
                    planning_goal or node.goal,
                    graph_summary=graph_summary,
                    capability_catalog=capability_catalog,
                    resource_catalog=resource_catalog,
                    event_callback=record_planner_stream,
                    cancel_check=self._cancel_requested,
                )
            else:
                repair_patch = self.governor.replan_rejected_task_graph_patch(
                    planning_goal or node.goal,
                    patch_error,
                    graph_summary=graph_summary,
                    capability_catalog=capability_catalog,
                    resource_catalog=resource_catalog,
                    event_callback=record_planner_stream,
                    rejected_patch=rejected_patch,
                    cancel_check=self._cancel_requested,
                )
                patch = (self.governor.merge_rejected_task_graph_patch(
                    rejected_patch, repair_patch,
                ) if rejected_patch is not None else repair_patch)
            if not patch.add_nodes:
                patch_error = (
                    "Governor returned no executable add_nodes; "
                    f"reason_codes={patch.reason_codes}"
                )
                raise ValueError(patch_error)
            for proposed_node in patch.add_nodes:
                if not isinstance(proposed_node, dict):
                    continue
                proposed_node["metadata"] = {
                    **dict(proposed_node.get("metadata") or {}),
                    "requested_by": planner_agent_id,
                    "submitted_by": "Orchestrator.plan_task_decomposition",
                    "request_stage": (
                        "initial_plan" if patch_attempt == 0 else "replan"
                    ),
                }
            applied = self.governor.apply_task_graph_patch(
                patch, parent_task_id=node_id
            )
            self.trace_recorder.record(
                "orchestrator.graph_patch.applied",
                {
                    "parent_task_id": node_id,
                    "attempt": patch_attempt + 1,
                    **applied,
                },
            )
            break
        except InterruptionRequested:
            raise
        except Exception as exc:
            rejected_patch = patch
            patch_error = str(exc)[:500]
            signature = patch_error_signature(patch_error)
            patch_error_counts[signature] = patch_error_counts.get(signature, 0) + 1
            self.trace_recorder.record(
                "orchestrator.graph_patch.rejected",
                {
                    "parent_task_id": node_id,
                    "attempt": patch_attempt + 1,
                    "error": patch_error,
                    "error_signature": signature,
                    "repeat_count": patch_error_counts[signature],
                },
            )
            self._consume_recovery_churn("planning_patch_rejected", lineage_root=node_id)
            if not self._persist_graph_checkpoint(stage="planning_patch_rejected", node_id=node_id):
                raise InterruptionRequested("Planning ownership was lost after rejection")
            if patch_error_counts[signature] >= 3:
                break
            patch_attempt += 1

    if applied is None:
        self.trace_recorder.record_node_output(
            node_id,
            {
                "node_id": node_id,
                "role": "planner",
                "execution_outcome": "failed",
                "content": patch_error or "Governor graph patch rejected",
                "summary": "Governor planning failed within bounded attempts and run cost",
                "artifacts": [],
                "next_actions": [],
                "metrics": {
                    "attempts": calls_made,
                    "error": patch_error,
                    "error_counts": patch_error_counts,
                },
            },
        )
        if node.status != "completed":
            self._transition_node_status_guarded(node_id, "failed", "planning_failed")
        self.trace_recorder.record(
            "orchestrator.planning_failed",
            {
                "node_id": node_id,
                "goal": (planning_goal or node.goal)[:100],
                "reason": patch_error or "graph_patch_rejected",
                "attempts": calls_made,
                "error_counts": patch_error_counts,
            },
        )
        self.trace_recorder.record(
            "agent.state_changed",
            {
                "agent_id": planner_agent_id,
                "node_id": node_id,
                "role": "planner",
                "status": TaskStatus.FAILED.value,
                "reason": patch_error or "graph_patch_rejected",
            },
        )
        from AstralNexus.core.exceptions import PlanningRejectedError
        raise PlanningRejectedError(
            "Governor graph patch rejected: same error repeated or planning budget exhausted "
            f"across {calls_made} attempts: {patch_error}"
        )

    if patch is None:
        raise RuntimeError("Applied planning result has no proposal identity")
    for added_node_id in applied["added"]:
        added_node = self.task_graph.get_node(added_node_id)
        if added_node is not None:
            self.memory_federation.sync_task_node(added_node)

    self.trace_recorder.record_node_output(
        node_id,
        {
            "node_id": node_id,
            "role": "planner",
            "execution_outcome": "succeeded",
            "content": (
                f"已将原始任务拆分为 {len(applied['added'])} 个可执行节点。"
                "节点会按依赖关系组成 Ready-Wave；无依赖节点并行执行，"
                "有依赖节点在所需证据或产物就绪后启动。"
            ),
            "summary": (
                f"规划完成：新增 {len(applied['added'])} 个节点；"
                "依赖满足后由 Router 形成 Ready-Wave。"
            ),
            "planning_summary": {
                "original_goal": planner_goal,
                "node_count": len(applied["added"]),
                "execution_strategy": (
                    "按能力分工构建增量 DAG；可并行节点进入同一 Ready-Wave，"
                    "下游节点等待依赖产物后继续。"
                ),
            },
            "planning_explanation": str(patch.reasoning or "").strip()[:1200],
            "planning_history": [
                {
                    "round": 1,
                    "kind": "initial_plan",
                    "planning_explanation": str(patch.reasoning or "").strip()[:1200],
                    "reason_codes": list(applied.get("reason_codes", [])),
                    "added": list(applied.get("added", [])),
                }
            ],
            "reason_codes": list(applied.get("reason_codes", [])),
            "planned_nodes": [
                {
                    "node_id": str(item.get("node_id") or ""),
                    "role": str(item.get("role") or "dynamic"),
                    "goal": str(item.get("goal") or ""),
                    "dependencies": list(item.get("dependencies") or []),
                    "source_requirements": list(
                        item.get("source_requirements") or []
                    ),
                    "derivation": str(item.get("derivation") or ""),
                }
                for item in patch.add_nodes
                if isinstance(item, dict)
            ],
            "artifacts": [],
            "next_actions": [
                {
                    "type": "execute",
                    "target": added_node_id,
                    "reason": "Governor-approved task graph",
                }
                for added_node_id in applied["added"]
            ],
            "metrics": {
                "added": len(applied["added"]),
                "updated": len(applied.get("updated", [])),
                "cancelled": len(applied.get("cancelled", [])),
                "reason_codes": list(applied.get("reason_codes", [])),
            },
        },
    )
    self.trace_recorder.record(
        "agent.state_changed",
        {
            "agent_id": planner_agent_id,
            "node_id": node_id,
            "role": "planner",
            "status": TaskStatus.COMPLETED.value,
            "reason": "planning patch applied",
        },
    )

    # The planning node is an execution boundary. Complete it before its
    # dependent children are promoted into the first Ready-Wave.
    parent = self.task_graph.get_node(node_id)
    if parent and parent.status in {
        "admitted", "waiting_dependencies", "ready", "running"
    }:
        if parent.status in {"admitted", "waiting_dependencies"}:
            self.task_graph.update_node_status(node_id, "ready")
        if parent.status == "ready":
            self.task_graph.update_node_status(node_id, "running")
        if parent.status == "running":
            self.task_graph.update_node_status(node_id, "result_submitted")
        if parent.status == "result_submitted":
            self.task_graph.update_node_status(node_id, "verifying")
        if parent.status == "verifying":
            self.task_graph.update_node_status(node_id, "completed")
    return list(applied["added"])


def _build_governor_planning_context(self: Orchestrator) -> tuple[str, str, str]:
    """Project runtime facts for Governor without making policy decisions."""
    from AstralNexus.runtime.planning_projection import project_planning_graph
    graph_summary = json.dumps(
        project_planning_graph([
            {
                "node_id": node.node_id,
                "status": node.status,
                "goal": node.goal,
                "dependencies": list(node.dependencies),
                "owned_paths": list(node.owned_paths or node.metadata.get("owned_paths", [])),
                "input_artifacts": list(node.input_artifacts or node.metadata.get("input_artifacts", [])),
                "output_artifacts": list(node.output_artifacts or node.metadata.get("output_artifacts", [])),
                "handoff_contract": dict(node.handoff_contract or node.metadata.get("handoff_contract", {})),
                "readiness_blockers": self.task_graph.readiness_blockers(node),
                "available_input_artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "path": artifact.path,
                        "type": artifact.artifact_type,
                        "acceptance_status": getattr(artifact.acceptance_status, "value", artifact.acceptance_status),
                    }
                    for artifact in self.task_graph.artifacts.values()
                    if artifact.node_id == node.node_id
                ],
                "required_capabilities": node.metadata.get(
                    "required_capabilities", []
                ),
            }
            for node in self.task_graph.nodes.values()
        ]),
        ensure_ascii=False,
    )
    capability_catalog = json.dumps(
        [
            {
                "worker_type": profile.worker_type,
                "description": profile.description,
                "capabilities": list(profile.capabilities),
                "tools": list(profile.tools),
                "model_preference": profile.model_preference,
                "reasoning_level": profile.reasoning_level,
                "resource_tier": profile.resource_tier,
                "input_contract": profile.input_contract,
                "output_contract": profile.output_contract,
            }
            for profile in HETEROGENEOUS_WORKERS.values()
        ],
        ensure_ascii=False,
    )
    resource_catalog = json.dumps(
        [
            {
                "resource_id": resource.resource_id,
                "tier": resource.tier.value,
                "privacy": resource.privacy,
                "cost": resource.cost,
                "latency": resource.latency,
                "capabilities": list(resource.capabilities),
                "capacity": resource.capacity,
                "available": resource.available,
                "metadata": dict(resource.metadata),
            }
            for resource in self.resource_scheduler.resource_pool.get_all_resources()
        ],
        ensure_ascii=False,
    )
    return graph_summary, capability_catalog, resource_catalog


def select_next_tasks(self: Orchestrator) -> List[str]:
    """Select the next tasks to execute based on readiness and dependencies.

    First promotes eligible admitted nodes to ready, then uses Router to select
    ready nodes based on task graph structure and routing logic.

    Returns:
        List of node IDs to execute
    """
    # Step 1: Promote admitted nodes whose dependencies are now satisfied
    promoted = self.task_graph.promote_waiting_nodes()
    if promoted:
        self.trace_recorder.record(
            "task.promotion",
            {
                "promoted_count": len(promoted),
                "promoted_nodes": promoted,
            }
        )

    # Step 2: Sync Router's task_graph reference before selection
    # Router was initialized with empty task_graph, need to update it
    self.router.task_graph = self.task_graph

    # Step 3: Use Router to select ready nodes (SEVERE-10 fix)
    ready_nodes = self.router.select_ready_nodes()
    selected_tasks = [node.node_id for node in ready_nodes]

    # Record selection in trace
    self.trace_recorder.record(
        "task.selection",
        {
            "selected_count": len(selected_tasks),
            "selected_tasks": selected_tasks,
            "selection_method": "router",
        }
    )

    return selected_tasks


def _transition_node_status_guarded(
    self: Orchestrator, node_id: str, target_status: str, context: str
) -> bool:
    """Race-tolerant node status write (Fix P5#15, r17 crash 2026-10-02).

    A recovery rearm can legally clobber a leased, executing node back to
    ``ready``; post-execution bookkeeping then faced ``ready -> failed`` /
    ``ready -> result_submitted``, and the uncaught ValueError killed the
    whole run (exit 1, 10 nodes completed, publish gate unreached).  Node
    bookkeeping must degrade to a recorded event instead: re-read the
    fresh status, apply ``transition_repair_plan`` (truthful intermediate
    writes for the known dispatch/rearm race), skip protected terminal
    states, and never raise.
    """
    node = self.task_graph.get_node(node_id)
    current_status = str(node.status) if node is not None else ""
    if not current_status:
        self.trace_recorder.record(
            "task.execution.invalid_transition",
            {
                "node_id": node_id,
                "current_status": current_status,
                "target_status": target_status,
                "error": "node not found",
                "context": context,
            },
        )
        return False
    try:
        plan = transition_repair_plan(current_status, target_status)
    except ValueError as e:
        plan = None
        plan_error = str(e)
    else:
        plan_error = ""
    if plan is None:
        self.trace_recorder.record(
            "task.execution.invalid_transition",
            {
                "node_id": node_id,
                "current_status": current_status,
                "target_status": target_status,
                "error": plan_error or "transition rejected; write skipped",
                "context": context,
            },
        )
        return False
    try:
        for intermediate in plan:
            self.task_graph.update_node_status(node_id, intermediate)
        if plan:
            self.trace_recorder.record(
                "task.execution.status_race_repaired",
                {
                    "node_id": node_id,
                    "current_status": current_status,
                    "target_status": target_status,
                    "intermediate_states": list(plan),
                    "context": context,
                },
            )
        if current_status != target_status:
            self.task_graph.update_node_status(node_id, target_status)
        return True
    except ValueError as e:
        self.trace_recorder.record(
            "task.execution.invalid_transition",
            {
                "node_id": node_id,
                "current_status": current_status,
                "target_status": target_status,
                "error": str(e),
                "context": context,
            },
        )
        return False


