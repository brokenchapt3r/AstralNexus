"""Execution responsibilities on the shared orchestration context."""
from __future__ import annotations
from contextvars import copy_context
from typing import TYPE_CHECKING
import asyncio
from .wave_executor import WaveExecutor, WorkerResult, invoke_worker
from copy import deepcopy
from .support import (
    Any,
    ArtifactAcceptanceStatus,
    Callable,
    DEFAULT_MODEL,
    Dict,
    ExecutionOutcome,
    HETEROGENEOUS_WORKERS,
    List,
    Optional,
    Path,
    ResourceLeaseError,
    TaskStatus,
    ThreadPoolExecutor,
    _json_safe,
    as_completed,
    assert_transition,
    datetime,
    declared_output_claims,
    declared_output_is_required,
    declared_output_path_tokens,
    glob,
    json,
    logger,
    os,
    resolve_declared_output,
    time,
    timezone,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def enable_real_llm_worker_matching(self: Orchestrator, config: Optional[Dict[str, Any]] = None) -> None:
    """Enable the real LLM Worker Profile decision stage for delivery runs."""
    from AstralNexus.runtime.llm_client import LLMClient
    from AstralNexus.runtime.model_selector import get_model_config

    resolved_config = dict(get_model_config())
    if config:
        resolved_config.update({key: value for key, value in config.items() if value})
    config = resolved_config
    self.model_config = dict(config)
    def record_model_event(event: dict[str, Any]) -> None:
        self.trace_recorder.record(event.get("event_type", "llm.event"), event)

    client = LLMClient(
        model=config.get("model") or DEFAULT_MODEL,
        base_url=config.get("base_url"),
        timeout=int(os.environ.get("ASTRALNEXUS_MATCHER_TIMEOUT", "120")),
        interactive=False,
        event_callback=record_model_event,
    )
    self.governor.enable_llm_worker_matching(client)
    self.verifier.llm_client = client
    self.verifier.workspace_root = self.run_dir
    self.governor.set_forced_worker_model(
        config.get("sub_model") or (config.get("model") if config.get("_model_explicit_override") else None)
    )


def execute_task(self: Orchestrator, node_id: str) -> Optional[bool]:
    """Synchronous compatibility entry; graph edits stay on the caller thread."""
    from .async_api import run_sync
    return run_sync(lambda: self.execute_task_async(node_id), async_entry="execute_task_async(node_id)")


async def _execute_task_async(self: Orchestrator, node_id: str) -> Optional[bool]:
    """Execute a specific task with integrated state machine, resource scheduling, and fault recovery.

    Args:
        node_id: The ID of the task node to execute

    Returns:
        True if execution was successful, False otherwise
    """
    if self._cancel_requested():
        return None
    node = self.task_graph.get_node(node_id)
    if not node:
        raise ValueError(f"Node {node_id} not found")

    # Accounting belongs to this attempt, never a previous evidence packet.
    node.metadata.pop("_attempt_accounting", None)
    # doc 18 修复G：worker 线程内标记会话归属——本会话启动的后台
    # 服务在注册表打上 node_id，会话边界按标清理。
    from AstralNexus.tools.builtin_tools import set_tool_node_context
    set_tool_node_context(node_id)

    # Use state machine to validate and perform transition to running
    try:
        assert_transition(node.status, "running")
        self.task_graph.update_node_status(node_id, "running")
    except ValueError as e:
        self.trace_recorder.record(
            "task.execution.invalid_transition",
            {
                "node_id": node_id,
                "current_status": node.status,
                "target_status": "running",
                "error": str(e),
            }
        )
        return False

    # Governor selects a capability profile before the concrete resource
    # scheduler leases local/edge/cloud capacity.  Router may still choose
    # the communication role later; it cannot bypass this control-plane
    # decision.
    try:
        worker_selection = self.governor.select_worker_profile(
            {
                "task_id": node_id,
                "node_id": node_id,
                "goal": node.goal,
                "required_capabilities": node.metadata.get("required_capabilities", []),
                "task_type": node.metadata.get("task_type", "other"),
                "expected_outputs": node.metadata.get("expected_outputs", []),
                "acceptance_criteria": node.acceptance_criteria,
                "execution_target": node.metadata.get("execution_target") or node.resource_tier,
                "risk_level": node.risk_level,
                "approved_worker_type": node.metadata.get("approved_worker_type"),
            },
            HETEROGENEOUS_WORKERS,
            resources=[
                {"tier": resource.tier.value, "available": resource.available}
                for resource in self.resource_scheduler.resource_pool.get_all_resources()
            ],
        )
    except (KeyError, TypeError, AssertionError) as exc:
        self.trace_recorder.record("orchestrator.framework_failure", {
            "node_id": node_id, "error_type": type(exc).__name__, "stage": "worker_profile_matching", "error": str(exc),
        })
        raise
    except Exception as exc:
        self.task_graph.update_node_status(node_id, TaskStatus.BLOCKED.value)
        node.metadata["last_failure"] = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "stage": "worker_profile_matching",
        }
        self.trace_recorder.record(
            "task.execution.worker_match_blocked",
            {"node_id": node_id, "error": str(exc)[:500]},
        )
        return False
    node.metadata["governor_worker_selection"] = worker_selection
    selected_worker = worker_selection.get("selected") or {}
    selected_profile = HETEROGENEOUS_WORKERS.get(str(selected_worker.get("worker_type", "")))
    if selected_profile is None:
        self.task_graph.update_node_status(node_id, TaskStatus.BLOCKED.value)
        node.metadata["last_failure"] = {
            "error_type": "CapabilityGap",
            "message": "GlobalGovernor selected no executable Worker Profile",
            "stage": "worker_profile_matching",
        }
        self.trace_recorder.record(
            "task.execution.worker_match_blocked",
            {
                "node_id": node_id,
                "reason_codes": worker_selection.get("reason_codes", []),
            },
        )
        return False
    if selected_profile:
        # Output contracts are executable capability requirements.  A
        # research/review profile may be the right semantic worker, but
        # it cannot satisfy a file-shaped contract unless the scoped
        # materialization tools are present.  Extend only the selected
        # profile for this node; do not globally mutate the catalogue.
        expected_outputs = node.metadata.get("expected_outputs", []) or []
        # Planner output identifiers (for example ``O-01``) are also
        # contract-bound artifacts.  They may be resolved to concrete
        # paths only by the delivery contract, so do not require the
        # planner to repeat the output type here.
        declared_files = bool(expected_outputs) or any(
            isinstance(item, dict)
            and str(item.get("type", "")).lower() in {"file", "files", "software"}
            for item in expected_outputs
        )
        selected_tools = list(selected_profile.tools)
        if declared_files and "file_write" not in selected_tools:
            selected_tools.append("file_write")
        task_type = str(node.metadata.get("task_type", "")).lower()
        if declared_files and task_type in {"implementation", "verification", "execution"}:
            for tool_name in ("terminal_exec", "python_exec", "pytest_run"):
                if tool_name not in selected_tools:
                    selected_tools.append(tool_name)
        node.metadata["worker_profile"] = {
            "worker_type": selected_profile.worker_type,
            "tools": selected_tools,
            "model_preference": selected_profile.model_preference,
            "selected_model": selected_worker.get("selected_model"),
            "reasoning_level": selected_worker.get(
                "reasoning_level", selected_profile.reasoning_level
            ),
            "resource_tier": selected_profile.resource_tier,
            "input_contract": selected_profile.input_contract,
            "output_contract": selected_profile.output_contract,
            "capabilities": list(selected_profile.capabilities),
            "context_policy": selected_worker.get("context_policy", {}),
        }
    if selected_worker.get("resource_tier"):
        node.resource_tier = selected_worker["resource_tier"]

    # Lease only within the Governor-approved target. This executor never
    # changes tier or recomputes policy.
    try:
        resource = self.resource_scheduler.lease_resource(node)
    except ResourceLeaseError as exc:
        self.task_graph.update_node_status(node_id, TaskStatus.FAILED.value)
        node.metadata["last_failure"] = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "stage": "resource_lease",
        }
        self.trace_recorder.record(
            "task.execution.resource_lease_failed",
            {"node_id": node_id, "error": str(exc)},
        )
        return False

    governor_execution_started_at = time.monotonic()
    budget_started = False
    try:
        scheduling_reason = self.resource_scheduler.get_scheduling_history()[-1]["reason"]
        self.trace_recorder.record(
            "task.execution.resource_leased",
            {
                "node_id": node_id,
                "resource_id": resource.resource_id,
                "resource_tier": resource.tier.value,
                "decision_source": "global_governor",
                "reason": scheduling_reason,
            }
        )
        if self.graph_store:
            try:
                self.graph_store.record_resource_decision(
                    node_id=node_id,
                    role=node.role,
                    resource_tier=resource.tier.value,
                    decision_time=0.0,
                    reason=scheduling_reason,
                    run_id=self.run_id,
                )
            except Exception as e:
                logger.warning(f"Failed to persist resource decision: {e}")
        node.metadata["assigned_resource"] = resource.resource_id
    
        # --- Router Integration: encode features, choose role, retrieve context, build contract ---
        # These routing steps enhance execution with intelligent role selection,
        # minimal context retrieval, and a formal task contract (原方案书 §4.2 Router).
        routing_features = None
        routing_context = None
        task_contract = None
        router_role = None
    
        if self.router is not None:
            try:
                # Step 1: Encode node features for routing decisions
                routing_features = self.router.encode_node_features(node)
                self.trace_recorder.record(
                    "task.routing.features_encoded",
                    {
                        "node_id": node_id,
                        "goal_embedding": routing_features.get("goal_embedding"),
                        "risk_score": routing_features.get("risk_score"),
                        "keywords": routing_features.get("keywords"),
                        "dependency_depth": routing_features.get("dependency_depth"),
                    }
                )
    
                # Governor approves the capability profile; Router alone
                # binds it to an executable registered role.
                governed_profile = node.metadata.get("worker_profile") or {}
                router_role = self.router.match_worker_profile(
                    node, governed_profile
                )
                effective_role = router_role
                self.trace_recorder.record(
                    "task.routing.role_chosen",
                    {
                        "node_id": node_id,
                        "role": effective_role,
                        "worker_type": governed_profile.get("worker_type"),
                        "source": "router_profile_match",
                    }
                )
    
                # Step 3: Retrieve minimal context for execution
                routing_context = self.router.retrieve_minimal_context(node)
                self.trace_recorder.record(
                    "task.routing.context_retrieved",
                    {
                        "node_id": node_id,
                        "relevant_facts_count": len(routing_context.get("relevant_facts", [])),
                        "relevant_artifacts_count": len(routing_context.get("relevant_artifacts", [])),
                        "constraints_count": len(routing_context.get("constraints", [])),
                    }
                )
    
                # 记录 facts.retrieved 事件
                relevant_facts = routing_context.get("relevant_facts", [])
                if relevant_facts:
                    self.trace_recorder.record(
                        "facts.retrieved",
                        {
                            "node_id": node_id,
                            "count": len(relevant_facts),
                            "fact_ids": [f.get("fact_id") if isinstance(f, dict) else str(f) for f in relevant_facts[:10]],
                        }
                    )
    
                # Step 4: Build task contract for execution
                resource_tier = resource.tier.value if resource else node.resource_tier
                task_contract = self.router.build_task_contract(
                    node, effective_role, routing_context, resource_tier
                )
                self.trace_recorder.record(
                    "task.routing.contract_built",
                    {
                        "node_id": node_id,
                        "role": effective_role,
                        "resource": resource_tier,
                        "acceptance_criteria": bool(task_contract.get("acceptance_criteria")),
                        "inputs_count": len(task_contract.get("inputs", [])),
                    }
                )
    
                # Store routing information in node metadata for downstream use
                node.metadata["routing_features"] = routing_features
                node.metadata["routing_context"] = routing_context
                node.metadata["task_contract"] = task_contract
                node.metadata["router_role"] = router_role
                node.metadata["implementation_role"] = router_role
            except Exception as e:
                self.trace_recorder.record(
                    "task.routing.error",
                    {
                        "node_id": node_id,
                        "error": str(e),
                        "action": "fail_and_recover",
                    }
                )
                self.task_graph.update_node_status(node_id, TaskStatus.FAILED.value)
                node.metadata["last_failure"] = {
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "stage": "router",
                }
                return False
    
        effective_role = str(node.metadata["implementation_role"])
        # Persist the concrete Router-bound role on the task node. Keeping the
        # placeholder ``governed_worker`` here made graph snapshots and node
        # lists look homogeneous even though heterogeneous workers were
        # actually running.
        node.role = effective_role
        node.metadata["selected_worker_type"] = (
            node.metadata.get("worker_profile") or {}
        ).get("worker_type")
    
        from AstralNexus.core.governor_budget import BudgetReservationRefused
        try:
            self.governor.record_task_start(
                node_id,
                estimated_tokens=int(
                    (node.metadata.get("budget") or {}).get("estimated_tokens", 0)
                    or 0
                ),
            )
        except BudgetReservationRefused as error:
            self._transition_node_status_guarded(node_id, "ready", "budget_reservation_deferred")
            self.trace_recorder.record("task.execution.budget_deferred", {
                "node_id": node_id, "error": str(error), "worker_started": False,
            })
            return None
        budget_started = True
        # Record start of execution
        self.trace_recorder.record(
            "task.execution.started",
            {
                "node_id": node_id,
                "goal": node.goal,
                "role": effective_role,
                "worker_type": (node.metadata.get("worker_profile") or {}).get("worker_type"),
                "selected_model": (node.metadata.get("worker_profile") or {}).get("selected_model"),
                "resource_id": resource.resource_id if resource else None,
                "resource_tier": resource.tier.value if resource else None,
                "router_enhanced": self.router is not None and task_contract is not None,
            }
        )
        agent = self._instantiate_governed_worker(node, effective_role)

        if agent:
            # Lifecycle: register agent instance and transition to RUNNING
            lifecycle_agent_id = None
            try:
                lc_instance = self.lifecycle_manager.create_agent(
                    role=agent.role,
                    node_id=node_id,
                    metadata={
                        "router_role": router_role,
                        "resource_tier": resource.tier.value if resource else None,
                    },
                )
                lifecycle_agent_id = lc_instance.agent_id
                node.metadata["worker_agent_id"] = lifecycle_agent_id
                worker_profile = node.metadata.get("worker_profile") or {}
                self.trace_recorder.record(
                    "agent.spawned",
                    {
                        "agent_id": lifecycle_agent_id,
                        "node_id": node_id,
                        "role": agent.role,
                        "status": TaskStatus.ADMITTED.value,
                        "capabilities": worker_profile.get("capabilities", []),
                        "tools": worker_profile.get("tools", []),
                        "model": worker_profile.get("selected_model"),
                        "resource_id": resource.resource_id if resource else None,
                        "resource_tier": resource.tier.value if resource else None,
                        "reason": "Governor-approved Worker instantiated",
                    },
                )
                self.lifecycle_manager.transition_agent(
                    lifecycle_agent_id,
                    TaskStatus.READY,
                    reason="Governor-approved Worker ready",
                )
                self.lifecycle_manager.transition_agent(
                    lifecycle_agent_id,
                    TaskStatus.RUNNING,
                    reason=f"executing {node.goal[:60]}",
                )
                self.trace_recorder.record(
                    "agent.state_changed",
                    {
                        "agent_id": lifecycle_agent_id,
                        "node_id": node_id,
                        "role": agent.role,
                        "status": "running",
                        "reason": f"executing {node.goal[:60]}",
                    },
                )
            except Exception as lc_err:
                logger.debug(f"Lifecycle tracking unavailable for {node_id}: {lc_err}")
                lifecycle_agent_id = None

            # Execute the task using the agent
            # In a real implementation, this would involve more complex context preparation
            success = await self._run_agent_task_async(agent, node)
            if success is None:
                if lifecycle_agent_id:
                    self.lifecycle_manager.transition_agent(
                        lifecycle_agent_id, TaskStatus.READY, reason="worker interrupted",
                    )
                self._persist_graph_checkpoint(stage="interrupted", node_id=node_id)
                return None

            # The task contract may contain model-produced, generic
            # background candidates.  Governor is the only component
            # allowed to admit them; absence of candidates means no
            # background work is created.
            if success:
                self._admit_background_candidates(node)

            # Fix BA (doc 26 §41, 2026-09-18 r13b): DeerFlow 2.0 keeps the
            # usable output of an abnormally ended turn and carries the cause
            # on stop_reason instead of discarding the work as a failure
            # (deer-flow@2814bd5d498a subagents/status_contract.py, #3875
            # Phase 2).  ``None`` means the turn produced nothing decidable,
            # so the ordinary failure path below is unchanged.
            turn_stop_reason = None if success else self._aborted_turn_stop_reason(node)

            # Execution may submit a result, but only Verifier can complete
            # the worker lifecycle.
            if lifecycle_agent_id:
                try:
                    if success:
                        self.lifecycle_manager.mark_result_submitted(
                            lifecycle_agent_id,
                            reason="task execution produced a result",
                        )
                        self.trace_recorder.record(
                            "agent.state_changed",
                            {
                                "agent_id": lifecycle_agent_id,
                                "node_id": node_id,
                                "role": agent.role,
                                "status": TaskStatus.RESULT_SUBMITTED.value,
                                "reason": "task execution produced a result",
                            },
                        )
                    elif turn_stop_reason is not None:
                        # Fix BA: usable work survives an abnormal end. The
                        # lifecycle records a submission whose cause rides on
                        # stop_reason — not an agent error; the Verifier and
                        # the runtime hard gate still own acceptance.
                        self.lifecycle_manager.mark_result_submitted(
                            lifecycle_agent_id,
                            reason="aborted turn carried verifiable evidence",
                        )
                        self.trace_recorder.record(
                            "agent.state_changed",
                            {
                                "agent_id": lifecycle_agent_id,
                                "node_id": node_id,
                                "role": agent.role,
                                "status": TaskStatus.RESULT_SUBMITTED.value,
                                "reason": "aborted turn carried verifiable evidence",
                                "stop_reason": turn_stop_reason,
                            },
                        )
                    else:
                        self.lifecycle_manager.mark_agent_error(
                            lifecycle_agent_id,
                            f"task execution returned failure for {node.goal[:60]}",
                            recoverable=True,
                        )
                        self.trace_recorder.record(
                            "agent.failed",
                            {
                                "agent_id": lifecycle_agent_id,
                                "node_id": node_id,
                                "role": agent.role,
                                "status": TaskStatus.REPAIR_PENDING.value,
                                "recoverable": True,
                                "reason": (
                                    "task execution returned failure for "
                                    f"{node.goal[:60]}"
                                ),
                            },
                        )
                    # Update metrics from agent response if available
                    evidence_packet = node.metadata.get("evidence_packet")
                    if evidence_packet:
                        # Filter to numeric values only — response.metrics
                        # may contain strings like runtime_status, model, etc.
                        raw_metrics = evidence_packet.get("token_usage", {})
                        numeric_usage = {
                            k: v for k, v in raw_metrics.items()
                            if isinstance(v, (int, float))
                        }
                        self.lifecycle_manager.update_agent_metrics(
                            lifecycle_agent_id,
                            iteration_count=1,
                            token_usage=numeric_usage,
                        )
                except Exception as lc_err:
                    logger.debug(f"Lifecycle post-execution update failed for {node_id}: {lc_err}")

            # Determine target status: execution produces result_submitted, not
            # final state.  Fix BA: an abnormally ended turn that already
            # produced code-verifiable output submits that work (DeerFlow 2.0
            # status/stop_reason split) instead of being discarded as a failure.
            target_status = "result_submitted" if (success or turn_stop_reason) else "failed"

            # Use state machine to validate and perform transition.
            # Fix P5#15 (r17 crash): guarded fresh-read write — a
            # concurrent recovery rearm may have clobbered this leased
            # node back to "ready" mid-execution; degrade to
            # success=False with a recorded event instead of raising a
            # ValueError that kills the whole run.
            if not self._transition_node_status_guarded(
                node_id, target_status, "post_execution"
            ):
                success = False

            # Make the worker handoff durable before downstream selection.
            # The verifier may run in the orchestration thread afterwards;
            # this checkpoint prevents a timeout from resurrecting the
            # node as ``admitted`` on resume.
            self._persist_graph_checkpoint(
                stage="result_submitted" if success else "execution_failed",
                node_id=node_id,
            )

            # Record completion
            self.trace_recorder.record(
                "task.execution.completed",
                {
                    "node_id": node_id,
                    "success": success,
                    "role": node.role,
                    "resource_id": resource.resource_id if resource else None,
                }
            )

            # Record real runtime execution path for delivery audit.
            self.trace_recorder.record(
                "task.execution.runtime",
                {
                    "node_id": node_id,
                    "role": effective_role,
                    "mode": "real_llm",
                }
            )

            # 记录 topology.sparsity 事件
            total_nodes = len(self.task_graph.nodes)
            total_edges = len(self.task_graph.edges)
            full_edges = total_nodes * (total_nodes - 1) / 2 if total_nodes > 1 else 0
            sparsity = 1 - (total_edges / full_edges) if full_edges > 0 else 1.0
            self.trace_recorder.record(
                "topology.sparsity",
                {
                    "node_id": node_id,
                    "node_count": total_nodes,
                    "edge_count": total_edges,
                    "sparsity_ratio": sparsity,
                }
            )

            if not success:
                # Task failed, update node status (Fix P5#15: guarded —
                # bookkeeping must never raise out of the worker path).
                self._transition_node_status_guarded(node_id, "failed", "execution_failed")

            return success
        else:
            # No agent available for this role
            # P0-1 Fix: Track capability gap failures to prevent infinite retry loops
            capability_gap_count = node.metadata.get("_capability_gap_failures", 0) + 1
            node.metadata["_capability_gap_failures"] = capability_gap_count

            governor_selection = node.metadata.get("governor_worker_selection", {})
            rejection = governor_selection.get("rejection", {})
            required_caps = rejection.get("required", []) or node.metadata.get("required_capabilities", [])

            # After 2 capability gap failures, mark as permanently blocked
            if capability_gap_count >= 2:
                # Fix P5#15: guarded — the node was never dispatched, so
                # it may still be "ready" where ready->failed is illegal.
                self._transition_node_status_guarded(node_id, "failed", "capability_gap_exhausted")
                node.metadata["_terminal_failure"] = True
                node.metadata["_terminal_reason"] = "capability_gap_exhausted"
                node.metadata["_capability_gap_exhausted"] = True
                self.trace_recorder.record("governor.capability_gap.exhausted", {
                    "node_id": node_id,
                    "required_capabilities": required_caps,
                    "gap_count": capability_gap_count,
                    "final_state": "failed",
                    "reason": "No worker profile can satisfy required capabilities after multiple attempts",
                })
                # Converge run state at the terminal boundary (user
                # directive 2026-09-12).
                self._converge_runtime_state_after_terminal(
                    node_id, "capability_gap_exhausted"
                )
            else:
                # Fix P5#15: guarded (same ready->failed hazard as above).
                self._transition_node_status_guarded(node_id, "failed", "capability_gap_replan")
                self.trace_recorder.record("governor.capability_gap.terminal", {
                    "node_id": node_id,
                    "gap_count": capability_gap_count,
                    "required_capabilities": required_caps,
                    "action": "will_replan",
                })

            self.trace_recorder.record(
                "task.execution.failed",
                {
                    "node_id": node_id,
                    "reason": f"No agent available for role: {node.role}",
                    "role": node.role,
                }
            )

            return False
    except (KeyError, TypeError, AssertionError) as exc:
        self.trace_recorder.record("orchestrator.framework_failure", {
            "node_id": node_id, "error_type": type(exc).__name__, "error": str(exc),
        })
        raise
    except Exception as e:
        # Handle execution errors.  Fix P5#15 (r17 crash): this handler
        # re-raised ValueError when the graph status was "ready" (recovery
        # rearm race), propagating out of the future and killing the run;
        # guarded write degrades to a recorded event instead.
        self._transition_node_status_guarded(node_id, "failed", "execution_error")

        self.trace_recorder.record(
            "task.execution.error",
            {
                "node_id": node_id,
                "error": str(e),
                "role": node.role,
            }
        )

        return False
    finally:
        successful_statuses = {
            TaskStatus.RESULT_SUBMITTED.value,
            TaskStatus.VERIFYING.value,
            TaskStatus.COMPLETED.value,
        }
        execution_outcome = (
            "succeeded" if node.status in successful_statuses
            else "interrupted" if node.status == TaskStatus.READY.value
            else "failed"
        )
        measured = node.metadata.get("_attempt_accounting") or {}
        try:
            if budget_started:
                self.governor.record_task_end(
                    node_id,
                    actual_tokens=measured.get("actual_tokens"),
                    actual_time_seconds=max(
                        0.0, time.monotonic() - governor_execution_started_at
                    ),
                    tool_calls=measured.get("tool_calls"),
                    execution_outcome=measured.get("execution_outcome", execution_outcome),
                    stop_reason=(
                        measured.get("stop_reason") or (""
                        if execution_outcome == "succeeded"
                        else str(
                            (node.metadata.get("last_failure") or {}).get("message")
                            or node.metadata.get("error")
                            or "worker_execution_failed"
                        ))
                    ),
                )
        finally:
            self.resource_scheduler.release_resource(resource, node_id=node_id)


def _instantiate_governed_worker(self: Orchestrator, node, role_hint: str):
    """Instantiate the Governor-selected worker and apply its contract.

    If Governor rejected the selection (zero capability match), do NOT
    silently fall back to a fixed role — return None so the orchestrator
    can trigger replan.
    """
    # Check if Governor rejected this worker selection
    governor_selection = node.metadata.get("governor_worker_selection") or {}
    if governor_selection.get("rejection"):
        self.trace_recorder.record("governor.worker_instantiation_blocked", {
            "node_id": node.node_id,
            "rejection": governor_selection["rejection"],
            "action": "governor_must_replan",
        })
        return None

    profile = node.metadata.get("worker_profile") or {}
    worker_type = str(profile.get("worker_type") or "")
    implementation_role = str(
        node.metadata.get("implementation_role") or role_hint or ""
    )
    if not worker_type or not implementation_role:
        self.trace_recorder.record(
            "governor.worker_instantiation_blocked",
            {
                "node_id": node.node_id,
                "role_hint": role_hint,
                "rejection": "missing Governor-approved worker profile",
                "action": "repair_pending_and_replan",
            },
        )
        return None
    agent = self.agent_registry.create_worker(
        implementation_role, profile=profile
    )
    if not agent:
        self.trace_recorder.record("governor.worker_instantiation_failed", {
            "node_id": node.node_id,
            "worker_type": worker_type,
            "implementation_role": implementation_role,
            "action": "repair_pending_and_replan",
        })
        return None
    if profile:
        agent.worker_profile = dict(profile)
        agent.default_tools = list(profile.get("tools", []))
        agent.input_contract = str(profile.get("input_contract", ""))
        agent.output_contract = str(profile.get("output_contract", ""))
        agent.model_preference = str(profile.get("model_preference", ""))
        agent.reasoning_level = str(profile.get("reasoning_level", "medium"))
        agent.selected_model = str(profile.get("selected_model", ""))
        agent.context_policy = dict(profile.get("context_policy", {}) or {})
        agent.capabilities = list(profile.get("capabilities", agent.capabilities))
        node.metadata["selected_worker"] = worker_type
        node.metadata["selected_model"] = agent.selected_model
        self.trace_recorder.record("governor.worker_instantiated", {
            "node_id": node.node_id,
            "worker_type": worker_type,
            "implementation_role": implementation_role,
            "tools": agent.default_tools,
            "selected_model": agent.selected_model,
            "model_preference": agent.model_preference,
            "reasoning_level": agent.reasoning_level,
            "resource_tier": profile.get("resource_tier"),
            "input_contract": agent.input_contract,
            "output_contract": agent.output_contract,
        })
    return agent


def _aborted_turn_stop_reason(self: Orchestrator, node) -> Optional[Dict[str, Any]]:
    """Fix BA (doc 26 §41, 2026-09-18 r13b): usable work across an abnormal end.

    DeerFlow 2.0 (``deer-flow@2814bd5d498a subagents/status_contract.py``,
    #3875 Phase 2) keeps a run that ended by a guardrail cap but produced
    usable output at ``status=completed``, carrying the cause on
    ``stop_reason``; only a capped run with NO usable output is ``failed``.
    AstralNexus derived the node status from the raw execution boolean
    alone, so a turn that had already written its deliverable was recorded
    as a failure and redone from scratch.

    Live gen31 evidence: PATCH-R017-FINAL-ACCEPTANCE-REPAIR and
    PATCH-R018-FINAL-ACCEPTANCE-RUNTIME each produced the complete passing
    acceptance packet (``runs/final-freeze/acceptance-rerun.json``,
    27,863 B, ``unmet_items_final=[]``) and were still converted into repair
    branches; the redone work burned the run-level structural churn budget
    to 20/20 and the run stopped with ``stop_code`` churn exhaustion and no
    delivery.

    Fail-closed: the exemption needs the SAME code-decidable Layer 2
    observations the Verifier uses — at least one decided criterion holds
    and none fails.  A turn with no verifiable output is unchanged, so a
    sustained provider outage still reaches the no-progress breaker on an
    honest signal (project rule 不能增加次数掩盖问题).  The node still goes
    through the Verifier and the runtime hard gate afterwards; this only
    stops verified work from being discarded, it never asserts acceptance.
    """
    try:
        from AstralNexus.runtime.acceptance_checks import (
            TURN_STOP_REASON_ABORTED_WITH_EVIDENCE,
            usable_partial_evidence,
        )

        observations = self.verifier.deterministic_observations(node)
    except Exception as exc:  # noqa: BLE001 — evidence probe must never mask a real failure
        logger.debug("Fix BA evidence probe unavailable for %s: %s", getattr(node, "node_id", "?"), exc)
        return None
    if not usable_partial_evidence(observations):
        return None
    decided = [
        item
        for item in observations
        if item.get("checked") and isinstance(item.get("holds"), bool)
    ]
    stop_reason = {
        "code": TURN_STOP_REASON_ABORTED_WITH_EVIDENCE,
        "cause": str(node.metadata.get("error_type") or "execution_failure"),
        "detail": str(node.metadata.get("error") or "")[:300],
        "decided_criteria": len(decided),
        "holding_criteria": sum(1 for item in decided if item.get("holds")),
    }
    node.metadata["_turn_stop_reason"] = stop_reason
    self.trace_recorder.record(
        "orchestrator.turn.aborted_but_usable",
        {
            "node_id": node.node_id,
            "stop_reason": stop_reason,
            "reason_codes": [TURN_STOP_REASON_ABORTED_WITH_EVIDENCE],
        },
    )
    return stop_reason


def _apply_missing_artifact_verdict(
    self: Orchestrator,
    response,
    missing_outputs: List[str],
    *,
    workspace_delta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """缺产物后置条件：保留既有主失败原因，把缺产物降为附加缺口。

    N-103（doc 51 §边界④）：本处曾无条件覆写 metrics，把 worker 因预算耗尽
    留下的主因（``task_execution_budget.exhausted_reason``，例如
    ``max_tool_calls``）抹成"缺产物"，恢复规划器只看到 MissingDeclaredArtifacts
    就重跑整轮，而不是先处理耗尽原因。

    现在分两支，且 ``error_type`` 恒为 ``MissingDeclaredArtifacts``（既有消费者
    core/router.py、dashboard/state.py、cli/main.py、tui_gateway/entry.py 只按它
    分型，不是二选一）：

    * 存在主因（预算耗尽）→ ``runtime_status`` 置为 ``budget_exhausted``（除非
      metrics 里已有非成功的守卫状态），另置 ``primary_failure_reason``，缺产物
      同时记入 ``missing_outputs``（既有字段）与 ``additional_gaps``；
    * 无主因 → 与修复前逐字一致：``runtime_status="failed"``。
    """
    metrics = dict(response.metrics or {})
    budget_snapshot = metrics.get("task_execution_budget")
    exhausted_reason = ""
    if isinstance(budget_snapshot, dict):
        exhausted_reason = str(budget_snapshot.get("exhausted_reason") or "").strip()
    if not exhausted_reason:
        exhausted_reason = str(metrics.get("exhausted_reason") or "").strip()
    existing_status = str(metrics.get("runtime_status") or "").strip()
    verdict: Dict[str, Any] = {
        "execution_path": "artifact_postcondition_failed",
        "error_type": "MissingDeclaredArtifacts",
        "missing_outputs": list(missing_outputs),
        "workspace_delta_classification": (
            workspace_delta.get("classification")
            if isinstance(workspace_delta, dict) else None
        ),
    }
    if exhausted_reason:
        verdict["runtime_status"] = (
            existing_status
            if existing_status in {"budget_exhausted", "blocked", "guard_blocked", "interrupted"}
            else "budget_exhausted"
        )
        verdict["primary_failure_reason"] = exhausted_reason
        verdict["additional_gaps"] = {
            "missing_declared_artifacts": list(missing_outputs),
        }
    else:
        verdict["runtime_status"] = "failed"
    response.metrics = metrics
    response.metrics.update(verdict)
    return verdict


def _run_agent_task(self: Orchestrator, agent, node) -> Optional[bool]:
    """Synchronous compatibility entry for independently exercised worker calls."""
    from .async_api import run_sync
    return run_sync(lambda: self._run_agent_task_async(agent, node), async_entry="_run_agent_task_async(agent, node)")


async def _run_agent_task_async(self: Orchestrator, agent, node) -> Optional[bool]:
    """Run a task using a BaseAgent, producing StructuredMessage results.

    Implements the real execution path:
    1. Build AgentContext from TaskNode
    2. Call agent.execute(context)
    3. Convert AgentResponse to StructuredMessage
    4. Write facts/artifacts to task graph

    Args:
        agent: A BaseAgent instance
        node: The task node to execute

    Returns:
        True if successful, False otherwise
    """
    from AstralNexus.agents.base import AgentContext, AgentResponse
    from AstralNexus.core.bridge import (
        message_to_evidence_packet,
        validated_response_to_message,
    )
    from AstralNexus.core.message_protocol import MessageProtocol
    from AstralNexus.tools.registry import discover_builtin_tools
    from AstralNexus.tools.tool_router import discover_tools

    # Step 1: Build execution context from task node
    # Incorporate routing information if available (from Router integration in execute_task)
    routing_context = node.metadata.get("routing_context")
    task_contract = node.metadata.get("task_contract")
    routing_features = node.metadata.get("routing_features")

    # P6-D (doc 32 §4): dispatch is the last line of defense — a node that
    # reaches execution without a declared positive budget (initial graph,
    # legacy patch, hand-built state) gets a default tool-round cap so the
    # LLM tool loop can never be unbounded.  Declared budgets pass through
    # verbatim; the fill is recorded for audit.
    from AstralNexus.runtime.task_execution_budget import ensure_task_budget

    # F4 (doc 51 §22): a node that exists only to repair a failed
    # predecessor (``recovery_of`` / ``supersedes_node_id``, attached by
    # governor.py:1391 and :1479) is a framework-issued round: its declared
    # cap is floored at the role default so an inherited, already-spent
    # small quota (r30 N-103: declared 40, resumed at tool_calls_used 40)
    # cannot make the repair unable to act.
    dispatch_budget, budget_defaulted = ensure_task_budget(
        node.metadata.get("budget"),
        task_type=str(
            node.metadata.get("declared_task_type")
            or node.metadata.get("task_type")
            or ""
        ),
        execution_target=str(node.metadata.get("execution_target") or ""),
        repair_round=bool(
            str(
                node.metadata.get("recovery_of")
                or node.metadata.get("supersedes_node_id")
                or ""
            ).strip()
        ),
    )
    if budget_defaulted:
        self.trace_recorder.record("task_budget.defaulted", {
            "node_id": node.node_id,
            "max_tool_calls": dispatch_budget.get("max_tool_calls"),
            "source": "dispatch_default_table",
        })

    # Build enriched metadata with routing info
    execution_metadata = {
        "acceptance_scope": "business" if getattr(getattr(self, "verifier", None), "business_acceptance_checks", True) else "execution",
        "role": agent.role,
        "risk_level": node.risk_level,
        "sensitivity": node.sensitivity,
        "run_dir": str(self.run_dir),
        "run_id": self.run_id,
        "model": getattr(agent, "selected_model", ""),
        "worker_profile": dict(getattr(agent, "worker_profile", {}) or {}),
        "input_contract": getattr(agent, "input_contract", ""),
        "output_contract": getattr(agent, "output_contract", ""),
        "context_policy": dict(getattr(agent, "context_policy", {}) or {}),
        "expected_outputs": deepcopy(node.expected_outputs),
        "acceptance_criteria": list(node.acceptance_criteria),
        "source_requirements": list(node.metadata.get("source_requirements", []) or []),
        "global_hard_constraints": list(self.governor.goal_spec.hard_constraints) if self.governor.goal_spec else [],
        "original_user_goal": self.governor.goal_spec.task_goal if self.governor.goal_spec else "",
        "owned_paths": list(node.owned_paths),
        "input_artifacts": list(node.input_artifacts),
        "output_artifacts": list(node.output_artifacts),
        "handoff_contract": deepcopy(node.handoff_contract),
        # Task-local execution limits must cross the graph/worker boundary;
        # keeping them only in planner metadata leaves the LLM tool loop
        # unbounded even when the node declares max_tool_calls/max_seconds.
        # P6-D: dispatch_budget is the normalized/defaulted form (above).
        "task_budget": dispatch_budget,
        "trigger": node.metadata.get("trigger", ""),
        "parent_node_id": node.metadata.get("parent_node_id", ""),
    }
    # Re-dispatch feedback quality (live r11 resume #10): the backend
    # takeover node N-002-R8-RECOVERY died three identical deaths —
    # every re-armed worker received a byte-identical prompt with no
    # trace of the previous attempt's failure receipt, repeated the
    # same curl-to-a-dead-port / read-old-logs loop, and was terminated
    # by the no-progress guard until rework admission ran out.  A
    # re-dispatched worker must see HOW its predecessor died; the
    # prompt renderer turns this into explicit anti-loop directives.
    # Fresh takeover generations carry no own receipt either — walk the
    # recovery_of / supersedes chain (≤3 hops) for the nearest
    # predecessor failure so the trap record crosses node IDs.
    last_failure = node.metadata.get("last_failure")
    failure_source = node.node_id
    if not (
        isinstance(last_failure, dict)
        and str(last_failure.get("message") or "")
    ):
        predecessor_id = str(
            node.metadata.get("recovery_of")
            or node.metadata.get("supersedes_node_id")
            or ""
        ).strip()
        for _ in range(3):
            if not predecessor_id:
                break
            predecessor = self.task_graph.get_node(predecessor_id)
            if predecessor is None:
                break
            candidate = predecessor.metadata.get("last_failure")
            if isinstance(candidate, dict) and str(candidate.get("message") or ""):
                last_failure = candidate
                failure_source = predecessor.node_id
                break
            predecessor_id = str(
                predecessor.metadata.get("recovery_of")
                or predecessor.metadata.get("supersedes_node_id")
                or ""
            ).strip()
    if isinstance(last_failure, dict) and str(last_failure.get("message") or ""):
        execution_metadata["previous_attempt_failure"] = {
            "message": str(last_failure.get("message") or "")[:800],
            "error_type": str(last_failure.get("error_type") or ""),
            "execution_path": str(last_failure.get("execution_path") or ""),
            "attempt": last_failure.get("attempt"),
            "source_node_id": failure_source,
        }
    # Verification rejections do not populate last_failure. Preserve their
    # actionable handoff independently from the exception-only anti-loop path.
    from AstralNexus.runtime.recovery_handoff import build_recovery_handoff

    recovery_handoff = build_recovery_handoff(self.task_graph, node)
    if recovery_handoff:
        execution_metadata["recovery_handoff"] = recovery_handoff
    # Governor may use opaque output identifiers such as O-01.  Convert
    # every declared output into a concrete, run-scoped evidence path so
    # a Worker can materialize it instead of merely describing it.
    expected_artifact_paths = []
    for raw_output in execution_metadata["expected_outputs"]:
        output_text = str(raw_output).strip()
        if not output_text:
            continue
        normalized_output = output_text.replace("\\", "/")
        # Planner ``expected_outputs`` are often acceptance prose such as
        # "基础配置与模板文件生成成功".  They are not file names and must
        # remain semantic contract text.  Only an explicitly path-shaped
        # output can become a filesystem postcondition.  In particular,
        # never invent ``node_outputs/.../output.md`` from prose: that
        # creates an impossible contract which a real Worker cannot satisfy.
        basename = Path(normalized_output.rstrip("/")).name
        is_directory_path = normalized_output.endswith("/")
        has_file_extension = bool(Path(basename).suffix)
        has_path_separator = "/" in normalized_output
        is_glob = any(char in normalized_output for char in "*?[")
        if is_directory_path or is_glob:
            expected_artifact_paths.append(normalized_output)
        elif has_path_separator and has_file_extension:
            # Fix U (doc 18, 2026-09-15 r13b): the prose guard above only
            # caught declarations WITHOUT a separator.  A sentence that
            # embeds a path — the live r13b N-003 contract
            # ``deliverables/app/backend/ 下的后端源码与 schema.sql`` —
            # has both a separator and a ".sql" basename suffix, so the
            # WHOLE sentence was handed to the Worker as a filesystem
            # postcondition: exactly the impossible contract this block
            # says it must never invent.  Bind the paths the declaration
            # actually contains instead; a clean single path is unchanged.
            embedded = declared_output_path_tokens(normalized_output)
            if embedded == [normalized_output]:
                expected_artifact_paths.append(normalized_output)
            else:
                expected_artifact_paths.extend(embedded)
    execution_metadata["expected_artifact_paths"] = list(dict.fromkeys(expected_artifact_paths))
    # Kimi-style progressive tool loading: expose a bounded, auditable
    # catalogue for this worker instead of broadcasting every registry
    # schema.  The profile remains the hard allow-list.
    # Registry discovery must happen before semantic routing.  Without
    # this explicit boundary, Governor can select a Coding Worker with a
    # valid profile while the actual LLM request receives zero tool
    # schemas and can only describe files instead of creating them.
    discover_builtin_tools()
    # The selected profile is the source of truth for the actual request
    # tool set.  A profile audit entry alone is insufficient: the same
    # effective allow-list must reach AgentContext and the LLM client.
    selected_tools = list(getattr(agent, "default_tools", []) or [])
    if not execution_metadata.get("worker_profile") and execution_metadata["expected_artifact_paths"] and "file_write" not in selected_tools:
        selected_tools.append("file_write")
    tool_catalog = discover_tools(
        agent.role,
        node.goal,
        selected_tools,
        profile_authorized=bool(execution_metadata.get("worker_profile")),
        required_tools=(
            ["file_write"]
            if execution_metadata["expected_artifact_paths"]
            else []
        ),
    )
    execution_metadata["tool_catalog"] = tool_catalog
    if task_contract:
        execution_metadata["task_contract"] = {**task_contract, **{
            field: execution_metadata.get(field, getattr(node, field))
            for field in ("owned_paths", "input_artifacts", "output_artifacts", "handoff_contract", "expected_outputs")
        }}
    if routing_features:
        execution_metadata["routing_features"] = routing_features
        execution_metadata["risk_score"] = routing_features.get("risk_score")
        execution_metadata["keywords"] = routing_features.get("keywords")
    if routing_context:
        execution_metadata["relevant_facts"] = routing_context.get("relevant_facts", [])
        execution_metadata["relevant_artifacts"] = routing_context.get("relevant_artifacts", [])

    # Build an explicit producer-to-consumer handoff.  Router retrieval is
    # intentionally supplemental; a declared graph dependency is always
    # included so a Worker cannot start with an apparently empty input.
    upstream_contracts = []
    upstream_artifacts = []
    for dependency_id in node.dependencies:
        dependency = self.task_graph.get_node(dependency_id)
        if dependency is None:
            continue
        upstream_contracts.append({
            "node_id": dependency.node_id,
            "goal": dependency.goal,
            "status": dependency.status,
            "owned_paths": list(dependency.owned_paths or dependency.metadata.get("owned_paths", [])),
            "output_artifacts": list(dependency.output_artifacts or dependency.metadata.get("output_artifacts", [])),
            "handoff_contract": dict(dependency.handoff_contract or dependency.metadata.get("handoff_contract", {})),
        })
        for artifact in self.task_graph.artifacts.values():
            if artifact.node_id == dependency_id:
                upstream_artifacts.append({
                    "artifact_id": artifact.artifact_id,
                    "path": artifact.path,
                    "description": artifact.description,
                    "acceptance_status": getattr(artifact.acceptance_status, "value", str(artifact.acceptance_status)),
                })
    execution_metadata["upstream_contracts"] = upstream_contracts
    execution_metadata["upstream_artifacts"] = upstream_artifacts

    # Merge routing context resources with node resources
    node_resources = dict(node.metadata.get("resources", {}))
    if routing_context:
        # Add upstream artifacts from routing context to execution resources
        upstream_artifacts = routing_context.get("relevant_artifacts", [])
        if upstream_artifacts:
            node_resources["upstream_artifacts"] = upstream_artifacts
    if upstream_contracts:
        node_resources["upstream_contracts"] = upstream_contracts

    # P3 (DeerFlow durable-context port, doc 30): build the predecessor
    # diagnostic ledger — digest-stamped pointers to the lineage's
    # persisted submission receipts — so a recovery worker cites the
    # verified diagnostics instead of re-deriving them. Live r13c: four
    # successive recovery workers each rebuilt the same analysis from
    # scratch; N-126 burned 104 minutes / 198 calls that way and died at
    # the wall clock with zero landed bytes. Empty for first-attempt
    # nodes (no lineage), so ordinary launches pay one metadata lookup.
    from AstralNexus.runtime.diagnostic_ledger import (
        build_diagnostic_ledger,
        collect_lineage_node_ids,
    )
    lineage_ids = collect_lineage_node_ids(node, self.task_graph.get_node)
    diagnostic_ledger = (
        build_diagnostic_ledger(self.run_dir, lineage_ids) if lineage_ids else []
    )
    if diagnostic_ledger:
        execution_metadata["diagnostic_ledger"] = diagnostic_ledger
        self.trace_recorder.record(
            "task.diagnostic_ledger_built",
            {
                "node_id": node.node_id,
                "lineage": lineage_ids,
                "receipts": [item["receipt_path"] for item in diagnostic_ledger],
            },
        )

    context = AgentContext(
        node_id=node.node_id,
        goal=node.goal,
        constraints=node.constraints,
        tools=[item["name"] for item in tool_catalog],
        resources=node_resources,
        artifacts=node.outputs,
        metadata=execution_metadata,
    )
    from AstralNexus.runtime.trace import TraceOwnershipLost
    try:
        self.trace_recorder.record_node_input(
            node.node_id,
            {
                "node_id": node.node_id,
                "role": execution_metadata["role"],
                "goal": node.goal,
                "status_before": node.status,
                "constraints": list(node.constraints),
                "tools": list(context.tools),
                "resources": context.resources,
                "artifacts": list(context.artifacts),
                "dependencies": list(node.dependencies),
                "inputs": list(node.inputs),
                "input_artifacts": list(execution_metadata["input_artifacts"]),
                "owned_paths": list(execution_metadata["owned_paths"]),
                "output_artifacts": list(execution_metadata["output_artifacts"]),
                "handoff_contract": dict(execution_metadata["handoff_contract"]),
                "upstream_contracts": upstream_contracts,
                "upstream_artifacts": upstream_artifacts,
                "agent_id": str(node.metadata.get("worker_agent_id") or ""),
                "assigned_by": "GlobalGovernor",
                "prompt_source": "Governor Task Contract + Router Context",
                "routing_context": routing_context or {},
                "task_contract": task_contract or {},
                "metadata": execution_metadata,
            },
        )
    except TraceOwnershipLost:
        self._ownership_lost = True
        self.trace_recorder.record("task.dispatch.ownership_lost", {"node_id": node.node_id})
        return None
    # Install the live sink only after the JSON node input has been
    # persisted; callbacks are runtime-only and must never leak into the
    # serialized execution contract.
    loop = asyncio.get_running_loop()
    def record_worker_event(event):
        # Worker threads do not touch graph/runtime projections directly.
        if not loop.is_closed():
            loop.call_soon_threadsafe(self._record_node_runtime_event,
                                      node.node_id, execution_metadata["role"], dict(event))
    context.metadata["_runtime_event_callback"] = record_worker_event
    context.metadata["_cancel_check"] = self._cancel_requested

    from AstralNexus.runtime.execution_store import ExecutionStore
    execution_store = ExecutionStore(self.run_dir, fence_token_provider=self.fence_token_provider)
    previous_execution_id = node.metadata.get("runtime_execution_id")
    predecessor_id = str(node.metadata.get("recovery_of") or node.metadata.get("supersedes_node_id") or "")
    seen_predecessors = {node.node_id}
    while not previous_execution_id and predecessor_id and predecessor_id not in seen_predecessors:
        seen_predecessors.add(predecessor_id)
        predecessor = self.task_graph.get_node(predecessor_id)
        if predecessor is None:
            break
        previous_execution_id = predecessor.metadata.get("runtime_execution_id")
        predecessor_id = str(predecessor.metadata.get("recovery_of") or predecessor.metadata.get("supersedes_node_id") or "")
    execution_id = execution_store.begin(node.node_id, {
        "task_version": self._node_task_version(node), "goal": node.goal,
        "acceptance_criteria": list(node.acceptance_criteria),
        "owned_paths": list(context.metadata.get("owned_paths", [])),
        "upstream_artifacts": list(context.metadata.get("upstream_artifacts", [])),
        "previous_execution_id": previous_execution_id,
    })
    node.metadata["runtime_execution_id"] = execution_id
    context.metadata["runtime_execution_id"] = execution_id
    if not self._persist_graph_checkpoint(stage="execution_started", node_id=node.node_id):
        # The stale writer cannot close the row either. The next valid owner
        # explicitly interrupts open executions during checkpoint recovery.
        return None
    runtime_callbacks = {key: context.metadata[key] for key in ("_runtime_event_callback", "_cancel_check")}
    context.constraints = deepcopy(context.constraints)
    context.artifacts = deepcopy(context.artifacts)
    context.resources = deepcopy(context.resources)
    context.metadata = deepcopy({key: value for key, value in context.metadata.items() if key not in runtime_callbacks})
    context.metadata.update(runtime_callbacks)

    # Step 2: Execute the agent
    try:
        from .worker_dispatcher import WorkerDispatcher
        from AstralNexus.runtime.execution_fence import execution_fence_scope
        def fenced_worker():
            from AstralNexus.core.agent_registry import agent_registry_scope
            with agent_registry_scope(self.agent_registry), execution_fence_scope(self.run_dir, self.fence_token_provider, execution_id):
                return invoke_worker(agent, context)
        response: AgentResponse = await WorkerDispatcher().call(
            fenced_worker,
            stage=lambda value: execution_store.stage_response(execution_id, _json_safe(value)),
        )
    except asyncio.CancelledError:
        if self._ownership_ok():
            execution_store.finish(execution_id, 'interrupted', {'error': 'worker call cancelled'})
        raise
    except Exception as e:
        from AstralNexus.runtime.execution_fence import ExecutionOwnershipLost
        if isinstance(e, ExecutionOwnershipLost) or not self._ownership_ok():
            self._ownership_lost = True
            self.trace_recorder.record('execution.ownership_lost', {'node_id': node.node_id, 'error': str(e)})
            return None
        if self._wave_commit_barrier is not None:
            await self._wave_commit_barrier.wait(node.node_id)
        execution_store.finish(execution_id, "failed", {"error": str(e)})
        logger.error(f"Agent {node.role} execution failed for {node.node_id}: {e}", exc_info=True)
        node.metadata["error_type"] = "agent_execution_exception"
        node.metadata["error"] = str(e)
        node.metadata["last_failure"] = {
            "message": str(e),
            "error_type": "agent_execution_exception",
            "attempt": int(node.retry_count) + 1,
            "execution_path": "runtime_exception",
        }
        self.trace_recorder.record_node_output(
            node.node_id,
            {
                "node_id": node.node_id,
                "role": execution_metadata["role"],
                "execution_outcome": ExecutionOutcome.FAILED.value,
                "content": f"Agent execution raised exception: {e}",
                "artifacts": [],
                "next_actions": [],
                "metrics": {
                    "runtime_status": "failed",
                    "execution_path": "runtime_exception",
                },
            },
        )
        self.trace_recorder.record(
            "task.agent_execution.error",
            {
                "node_id": node.node_id,
                "role": node.role,
                "error": str(e),
            }
        )
        return False
    if self._wave_commit_barrier is not None:
        await self._wave_commit_barrier.wait(node.node_id)

    if self._ownership_lost:
        execution_store.finish(execution_id, "interrupted", {"error": "ownership lost before response commit"})
        return None
    from AstralNexus.runtime.execution_accounting import execution_accounting

    node.metadata["_attempt_accounting"] = execution_accounting(
        response.metrics or {}, response.outcome.value,
    )
    # P1 (DeerFlow capped-semantics port, doc 30): the worker layer books
    # an objective owned-path delta at every exit. Persist it on the node
    # so recovery routing can tell "predecessor landed bytes — continue
    # from them" (capped_with_output) apart from "nothing was ever
    # written — change strategy" (capped_zero_output). The delta is
    # runtime-measured evidence about bytes, never an acceptance claim.
    workspace_delta = context.metadata.get("workspace_delta")
    if not isinstance(workspace_delta, dict):
        workspace_delta = (response.metrics or {}).get("workspace_delta")
    if isinstance(workspace_delta, dict):
        node.metadata["workspace_delta"] = workspace_delta
    else:
        workspace_delta = None
    if response.outcome == ExecutionOutcome.INTERRUPTED:
        # Fix AI (doc 18, 2026-09-15 r13b): a transport cut reaches here as
        # INTERRUPTED rather than FAILED, because it says nothing about the
        # node's work.  Returning to ready must stay BOUNDED, or a sustained
        # provider outage would spin here forever and hide a real problem --
        # which the project rule 不能增加次数掩盖问题 forbids.  Each
        # interruption is counted on the node and, once the budget is spent,
        # falls through to the ordinary failure path so the no-progress
        # breaker still fires on an honest signal.
        if (response.metrics or {}).get("transport_interrupted"):
            try:
                budget = max(0, int(os.environ.get("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", "3")))
            except (TypeError, ValueError):
                budget = 3
            # Fix AZ (doc 26 §18, 2026-09-17 r13b gen30): this counter used to be a
            # node-LIFETIME total, so a node that had been working productively for
            # hours died on the 4th transport cut.  Live evidence: PATCH-R017
            # completed 111 tool rounds (round 104 had just brought the service up
            # and READY) and was converted to node_failure at the 4th cut; governed
            # recovery then applied `reassign_with_resource_tier_escalation`
            # (edge-1 -> local/Testing), which cannot remedy network weather, and the
            # execution restarted at tool_round 1 — hours of LLM work plus one unit of
            # the scarce 20-unit structural churn budget spent on a read timeout.
            #
            # An interrupt that FOLLOWS completed work is not evidence that the node
            # is stuck, so it must not count toward the give-up budget.  The budget
            # keeps its stated purpose: a node that cannot complete a single tool
            # round between cuts still exhausts it and fails, so a sustained provider
            # outage still reaches the no-progress breaker on an honest signal
            # (project rule 不能增加次数掩盖问题).
            stats = (response.metrics or {}).get("request_stats")
            try:
                progress_rounds = max(0, int((stats or {}).get("tool_rounds") or 0))
            except (TypeError, ValueError, AttributeError):
                progress_rounds = 0
            if progress_rounds > 0 and budget > 0:
                # 预算显式设为 0 = 运维要求"不容忍任何传输中断"，此时不做进展豁免。
                node.metadata["transport_interrupts"] = 0
                seen = 0
                disposition = "progress_observed"
            else:
                seen = int(node.metadata.get("transport_interrupts", 0)) + 1
                node.metadata["transport_interrupts"] = seen
                disposition = "requeued" if seen <= budget else "budget_exhausted"
            self.trace_recorder.record("node.transport_interrupted", {
                "node_id": node.node_id,
                "attempt": seen,
                "budget": budget,
                "progress_rounds": progress_rounds,
                "error": str((response.metrics or {}).get("error", ""))[:300],
                "error_type": (response.metrics or {}).get("error_type", ""),
                "disposition": disposition,
                "note": "传输层中断不代表节点工作有误；只有**连续无进展**的中断才计入放弃预算",
            })
            if disposition == "budget_exhausted":
                error_text = str((response.metrics or {}).get("error", "transport_failure"))
                execution_store.finish(execution_id, "failed", {"error": error_text})
                node.metadata["error_type"] = "transport_interrupt_budget_exhausted"
                node.metadata["error"] = error_text
                node.metadata["last_failure"] = {
                    "message": error_text,
                    "error_type": "transport_interrupt_budget_exhausted",
                    "attempt": int(node.retry_count) + 1,
                    "execution_path": "runtime_transport_interrupted",
                    "transport_interrupts": seen,
                }
                self.trace_recorder.record_node_output(node.node_id, {
                    "node_id": node.node_id,
                    "role": execution_metadata["role"],
                    "execution_outcome": ExecutionOutcome.FAILED.value,
                    "content": response.content,
                    "artifacts": [],
                    "next_actions": [],
                    "metrics": dict(response.metrics or {}, runtime_status="failed"),
                })
                return False
        execution_store.finish(execution_id, "interrupted", {"content": response.content})
        # A cancellation is not a task failure.  Return the node to the
        # ready state so checkpoint/resume or a requirement patch can
        # continue it without manufacturing a recovery branch.
        try:
            self.task_graph.update_node_status(node.node_id, "ready")
        except ValueError:
            pass
        self.trace_recorder.record_node_output(node.node_id, {
            "node_id": node.node_id,
            "role": execution_metadata["role"],
            "execution_outcome": ExecutionOutcome.INTERRUPTED.value,
            "content": response.content,
            "metrics": response.metrics,
        })
        self.trace_recorder.record("task.execution.interrupted", {
            "node_id": node.node_id,
            "reason": (response.metrics or {}).get("error", "user_interrupt"),
        })
        return None

    # A file-shaped output contract is a runtime postcondition, not a
    # suggestion for the model.  In particular, a worker must not be
    # considered successful merely because its text claims that it wrote
    # a file.  Accept files in the run directory and in the delivery
    # staging area; logical (non-path) output labels remain descriptive.
    output_contract = [
        str(item).strip()
        for item in (getattr(node, "outputs", []) or [])
        if str(item).strip()
    ]
    from AstralNexus.core.output_paths import runtime_output_claims
    file_outputs = list(dict.fromkeys(
        claim for item in output_contract
        for claim in runtime_output_claims(item, node.owned_paths)
    ))
    # Delivery-contract paths may be carried in metadata when the
    # Governor owns the output contract and the graph node keeps only an
    # opaque output id.  They are still hard physical postconditions.
    for item in execution_metadata.get("expected_artifact_paths", []) or []:
        if item not in file_outputs:
            file_outputs.append(item)

    # 起 A (doc b26 C1): a declaration the contract itself marks
    # optional/conditional is not a runtime postcondition.  Live r30 R-107
    # declared required:false / source:"optional" / condition:{kind:
    # "no_defects", when_false:"structured_empty_manifest"} for
    # deliverables/runs/browser/defects.json; the Governor flattened that
    # declaration into a bare path, this check promoted it to a hard
    # postcondition, and a green 53/53 execution was flipped to FAILED
    # (MissingDeclaredArtifacts) — the recovery planner then reran the whole
    # node.  Only explicitly required declarations stay hard here; a
    # condition that is false or undecidable at runtime must not fail the
    # node, and the final-delivery gate still blocks a missing REQUIRED
    # artifact.
    optional_declared = {
        claim
        for entry in (execution_metadata.get("expected_outputs") or [])
        if not declared_output_is_required(entry)
        for claim in declared_output_claims(entry)
    }
    if optional_declared:
        retained_outputs: List[str] = []
        skipped_outputs: List[str] = []
        for item in file_outputs:
            claims = declared_output_claims(item)
            skip = item.strip() in optional_declared or (
                bool(claims) and all(claim in optional_declared for claim in claims)
            )
            if skip:
                skipped_outputs.append(item)
            else:
                retained_outputs.append(item)
        if skipped_outputs:
            file_outputs = retained_outputs
            self.trace_recorder.record("task.execution.optional_postcondition_skipped", {
                "node_id": node.node_id,
                "skipped_declared_outputs": skipped_outputs,
                "reason": "declared_optional_or_conditional",
            })

    def declared_path_tokens(text: str) -> List[str]:
        """Fix S token extraction; module-level so tests can call it directly."""
        return declared_output_path_tokens(text)

    def materialized_contract_path(output: str) -> Path | None:
        """Resolve existing output; freshness belongs to validation, not existence."""
        return resolve_declared_output(self.run_dir, output)
    # Durable files survive a failed worker, but their existence cannot
    # turn a failed execution into success. Preserve them as partial
    # evidence so recovery can continue from disk without allowing failed
    # upstream nodes to unlock downstream delivery gates.
    if response.outcome != ExecutionOutcome.SUCCEEDED and file_outputs:
        materialized_paths = []
        missing_outputs = []
        for output in file_outputs:
            existing = materialized_contract_path(output)
            if existing is not None:
                materialized_paths.append(str(existing.resolve()))
            elif declared_path_tokens(output):
                missing_outputs.append(output)
            # else: a prose-only declaration makes no physical-file claim,
            # so it can neither certify retention nor justify a failure.
        if not missing_outputs and materialized_paths:
            original_failure = dict(response.metrics or {})
            response.outcome = ExecutionOutcome.PARTIAL
            response.artifacts = sorted(set(
                list(response.artifacts or []) + materialized_paths
            ))
            response.metrics = {
                **original_failure,
                "runtime_status": "partial_artifacts_retained",
                "execution_path": "partial_artifacts_retained",
                "original_runtime_status": "failed",
                "original_failure_preserved": True,
            }
            response.content = (
                (response.content or "").rstrip()
                + "\n\n节点声明的物理文件已落盘，但执行仍未通过；"
                "文件仅作为恢复证据保留，节点不会被判定完成。"
            )
            self.trace_recorder.record(
                "task.execution.partial_artifacts_retained",
                {
                    "node_id": node.node_id,
                    "artifacts": materialized_paths,
                    "original_failure": original_failure,
                },
            )
    if response.outcome == ExecutionOutcome.SUCCEEDED and file_outputs:
        missing_outputs = []
        for output in file_outputs:
            raw_path = Path(output)
            candidates = [raw_path] if raw_path.is_absolute() else [
                self.run_dir / raw_path,
                self.run_dir / ".delivery_staging" / raw_path,
            ]
            has_materialized = materialized_contract_path(output) is not None
            if not has_materialized and any(char in str(raw_path) for char in "*?["):
                # Output contracts may intentionally declare a bounded
                # file set with a glob (for example ``models/*.js``).
                # Treat the pattern as satisfied when at least one real
                # file matches under the run directory or staging root.
                for root in (self.run_dir, self.run_dir / ".delivery_staging"):
                    pattern = root / raw_path
                    if glob.glob(str(pattern), recursive=True):
                        has_materialized = any(
                            Path(match).is_file()
                            for match in glob.glob(str(pattern), recursive=True)
                        )
                        if has_materialized:
                            break
            if not has_materialized and declared_path_tokens(output):
                missing_outputs.append(output)
        if missing_outputs:
            response.outcome = ExecutionOutcome.FAILED
            # N-103: 主失败原因（预算耗尽）必须先被读取并保留，缺产物只能
            # 记为附加缺口——覆写会抹掉耗尽主因，恢复规划器就只看到"缺产物"。
            verdict = self._apply_missing_artifact_verdict(
                response,
                missing_outputs,
                workspace_delta=(
                    workspace_delta if isinstance(workspace_delta, dict) else None
                ),
            )
            response.content = (
                "Worker did not materialize declared output files: "
                + ", ".join(missing_outputs)
            )
            self.trace_recorder.record(
                "task.execution.artifact_postcondition_failed",
                {
                    "node_id": node.node_id,
                    "missing_outputs": missing_outputs,
                    "declared_outputs": file_outputs,
                    "run_dir": str(self.run_dir),
                    "runtime_status": verdict.get("runtime_status"),
                    "primary_failure_reason": verdict.get("primary_failure_reason"),
                    "additional_gaps": verdict.get("additional_gaps"),
                },
            )
        else:
            # Bind only files freshly produced by this execution. Read
            # paths and stale files from an earlier node are context, not
            # proof that the current Worker fulfilled its contract.
            produced = [
                str(path.resolve())
                for output in file_outputs
                if (path := materialized_contract_path(output)) is not None
            ]
            response.artifacts = sorted(set(
                list(response.artifacts or []) + produced
            ))

    # Some providers return a syntactically successful response whose
    # structured conclusion explicitly says that the task was not done
    # (often after the tool-round budget is exhausted).  Treat that as a
    # runtime failure when no artifact was materialized in this run.  The
    # check is contract-neutral: it applies to any domain and does not
    # inspect domain-specific section names.
    if response.outcome == ExecutionOutcome.SUCCEEDED:
        parsed_content = None
        try:
            parsed_content = json.loads(response.content or "")
        except (TypeError, json.JSONDecodeError):
            parsed_content = None
        conclusion = ""
        if isinstance(parsed_content, dict):
            conclusion = " ".join(
                str(parsed_content.get(key, ""))
                for key in (
                    "execution_conclusion",
                    "execution_status",
                    "status",
                    "result",
                    "reason",
                )
            ).lower()
        incomplete_markers = (
            "未完成", "无法完成", "执行失败", "失败", "not completed",
            "unable to complete", "failed", "cannot complete",
        )
        run_root = self.run_dir.resolve()
        run_artifact_exists = False
        for item in (getattr(response, "artifacts", []) or []):
            artifact_path = Path(str(item))
            candidates = [artifact_path]
            if not artifact_path.is_absolute():
                candidates.append(self.run_dir / artifact_path)
            if any(
                candidate.is_file()
                and str(candidate.resolve()).startswith(str(run_root))
                for candidate in candidates
            ):
                run_artifact_exists = True
                break
        if conclusion and any(marker in conclusion for marker in incomplete_markers) and not run_artifact_exists:
            response.outcome = ExecutionOutcome.FAILED
            response.metrics = dict(response.metrics or {})
            response.metrics.update({
                "runtime_status": "failed",
                "execution_path": "semantic_incomplete_postcondition",
                "error_type": "WorkerReportedIncomplete",
                # P1: same cap-outcome classification for the semantic
                # incomplete path (the live r13c honest-confession shape).
                "workspace_delta_classification": (
                    workspace_delta.get("classification")
                    if isinstance(workspace_delta, dict) else None
                ),
            })
            self.trace_recorder.record(
                "task.execution.semantic_incomplete",
                {
                    "node_id": node.node_id,
                    "conclusion": conclusion[:500],
                    "artifacts": list(getattr(response, "artifacts", []) or []),
                },
            )
    output_record = self.trace_recorder.record_node_output(
        node.node_id,
        {
            "node_id": node.node_id,
            "role": execution_metadata["role"],
            "execution_outcome": response.outcome.value,
            "content": response.content,
            "artifacts": list(response.artifacts),
            "next_actions": list(response.next_actions),
            "metrics": dict(response.metrics),
        },
    )
    request_stats = dict((response.metrics or {}).get("request_stats", {}) or {})
    if request_stats:
        self.trace_recorder.record(
            "llm.request.summary",
            {
                "node_id": node.node_id,
                "role": execution_metadata["role"],
                "model": request_stats.get("model", execution_metadata.get("model")),
                "provider_key": request_stats.get("provider_key"),
                "tool_count": request_stats.get("tool_count", 0),
                "tool_rounds": request_stats.get("tool_rounds", 0),
                "request_attempts": request_stats.get("request_attempts", []),
                "network_errors": request_stats.get("network_errors", []),
                "retry_events": request_stats.get("retry_events", []),
                "actual_prompt_tokens": request_stats.get("actual_prompt_tokens"),
                "actual_completion_tokens": request_stats.get("actual_completion_tokens"),
                "elapsed_seconds": request_stats.get("elapsed_seconds"),
            },
        )

    if isinstance(output_record, (tuple, list)) and output_record:
        output_path = Path(output_record[0]).resolve()
        node.metadata["_runtime_output_ref"] = output_path.relative_to(self.run_dir.resolve()).as_posix()
        from hashlib import sha256
        node.metadata["_runtime_output_sha256"] = sha256(output_path.read_bytes()).hexdigest()
    if response.outcome != ExecutionOutcome.SUCCEEDED:
        # Preserve the concrete runtime failure for recovery and the next
        # Router decision.  Do not replace it with a generic graph error.
        node.metadata["error"] = response.content or "Agent returned failure"
        node.metadata["error_type"] = str(
            (response.metrics or {}).get("error_type", "agent_execution_failure")
        )
        node.metadata["last_failure"] = {
            "message": node.metadata["error"],
            "error_type": node.metadata["error_type"],
            "attempt": int(node.retry_count) + 1,
            "execution_path": (response.metrics or {}).get("execution_path"),
            # P1: cap-outcome classification inside the failure record —
            # recovery/replan consumers read last_failure, and "the
            # attempt landed owned bytes before the cap" vs "zero bytes
            # landed" must route to different successor strategies.
            "workspace_delta": (
                {
                    "classification": workspace_delta.get("classification"),
                    "exhausted_reason": workspace_delta.get("exhausted_reason"),
                    "counts": workspace_delta.get("counts"),
                }
                if isinstance(workspace_delta, dict) else None
            ),
        }
    # Step 3: Bridge is the sole AgentResponse -> StructuredMessage
    # conversion and validation boundary. Invalid output is real failure
    # evidence; the Orchestrator must never fabricate a passing message.
    mp = MessageProtocol()
    try:
        msg = validated_response_to_message(
            response,
            node_id=node.node_id,
            role=execution_metadata["role"],
            goal=node.goal,
        )
    except ValueError as exc:
        error_msg = str(exc)
        execution_store.finish(execution_id, "failed", {"error": error_msg})
        logger.error("Structured message rejected for %s: %s", node.node_id, error_msg)
        self.task_graph.update_node_status(
            node.node_id, TaskStatus.FAILED.value
        )
        node.metadata["validation_error"] = error_msg
        self.trace_recorder.record(
            "task.message_validation.blocked",
            {
                "node_id": node.node_id,
                "error": error_msg,
                "action": "blocked_downstream_propagation",
            }
        )
        return False

    execution_store.finish(execution_id, response.outcome.value, {
        "content": response.content, "metrics": response.metrics,
        "artifacts": list(response.artifacts or []),
    })
    node.metadata["evidence_packet"] = message_to_evidence_packet(
        msg,
        task_version=self._node_task_version(node),
    )
    node.metadata["evidence_packet"]["token_usage"] = dict(response.metrics or {})
    node.metadata["evidence_packet"]["execution_time"] = (
        (response.metrics or {}).get("wall_time_s", 0)
    )
    routing_features = mp.extract_key_information(msg)
    self.trace_recorder.record(
        "task.message_validation",
        {
            "node_id": node.node_id,
            "is_valid": True,
            "protocol_owner": "bridge",
        },
    )

    # Step 3.6: P1 Integration - Compress message and calculate compression ratio
    # This helps track message size optimization and context efficiency
    try:
        from AstralNexus.core.message_protocol import MessageProtocol
        mp_compressor = MessageProtocol()

        # Compress the message for storage/transmission
        compressed_msg = mp_compressor.compress_message(msg)

        # Calculate compression ratio to measure efficiency
        compression_ratio = mp_compressor.calculate_compression_ratio(msg, compressed_msg)

        # Calculate quality metrics to prove compression preserves valid evidence
        quality_metrics = mp_compressor.calculate_quality_metrics(msg, compressed_msg)

        self.trace_recorder.record(
            "message.compressed",
            {
                "node_id": node.node_id,
                "original_size": len(mp_compressor.serialize_message(msg)),
                "compressed_size": len(compressed_msg),
                "compression_ratio": compression_ratio,
                "space_saved_percent": (1 - 1/compression_ratio) * 100 if compression_ratio > 1 else 0,
                "quality_metrics": quality_metrics,
            }
        )
    except Exception as e:
        # Message compression is non-fatal - log but continue
        logger.debug(f"Message compression failed for {node.node_id}: {e}")
        self.trace_recorder.record(
            "message.compression.failed",
            {
                "node_id": node.node_id,
                "error": str(e),
            }
        )

    # Step 3.7: Validate cross-references in the produced message (Router integration)
    # This ensures that claims evidence references exist and next actions target valid nodes
    if self.router is not None:
        try:
            from AstralNexus.core.message_protocol import MessageProtocol
            mp_validator = MessageProtocol()

            # Collect artifact IDs from the response
            artifact_ids = response.artifacts if response.artifacts else []

            # Validate cross-references against task graph
            xref_valid, xref_errors = mp_validator.validate_message_cross_references(
                message=msg,
                artifacts=artifact_ids,
                task_graph=self.task_graph
            )

            self.trace_recorder.record(
                "task.message.cross_reference_validation",
                {
                    "node_id": node.node_id,
                    "is_valid": xref_valid,
                    "error_count": len(xref_errors) if xref_errors else 0,
                    "errors": xref_errors[:5] if xref_errors else [],  # Limit to first 5 errors
                }
            )

            if not xref_valid:
                logger.warning(f"Cross-reference validation failed for {node.node_id}: {xref_errors}")
                node.metadata["_cross_reference_errors"] = list(xref_errors or [])
            else:
                node.metadata.pop("_cross_reference_errors", None)

        except Exception as e:
            logger.warning(f"Cross-reference validation error for {node.node_id}: {e}")
            node.metadata["_cross_reference_errors"] = [
                f"cross-reference validator error: {e}"
            ]
            self.trace_recorder.record(
                "task.message.cross_reference_validation.error",
                {
                    "node_id": node.node_id,
                    "error": str(e),
                }
            )

    # Step 4: Stage artifacts and claims. They become authoritative only
    # after the unique Verifier approves this result.
    for previous_id in node.metadata.get("_pending_artifact_ids", []):
        previous = self.task_graph.artifacts.get(previous_id)
        if (
            previous
            and previous.acceptance_status
            == ArtifactAcceptanceStatus.PENDING.value
        ):
            previous.acceptance_status = ArtifactAcceptanceStatus.REJECTED
            previous.metadata["rejection_reason"] = "superseded by a new attempt"
    pending_artifact_ids: list[str] = []
    for response_artifact_path in response.artifacts:
        artifact_id = f"A-{node.node_id}-{len(self.task_graph.artifacts)+1:03d}"
        self.task_graph.add_artifact({
            "artifact_id": artifact_id,
            "node_id": node.node_id,
            "artifact_type": "file",
            "path": response_artifact_path,
            "description": f"Produced by {node.role} for {node.goal[:50]}",
            "acceptance_status": ArtifactAcceptanceStatus.PENDING.value,
            "metadata": {
                "task_version": self._node_task_version(node),
                "retry_count": node.retry_count,
            },
        })
        pending_artifact_ids.append(artifact_id)
    node.metadata["_pending_artifact_ids"] = pending_artifact_ids
    node.metadata["_pending_fact_records"] = [
        {
            "text": str(claim.get("text") or ""),
            "confidence": float(claim.get("confidence", 0.0)),
            "evidence": list(claim.get("evidence") or []),
        }
        for claim in msg.claims
        if isinstance(claim, dict) and str(claim.get("text") or "").strip()
    ]

    # Record structured message to trace
    self.trace_recorder.record(
        "task.structured_message",
        {
            "node_id": msg.node_id,
            "status": msg.status,
            "summary": msg.summary,
            "artifact_count": len(msg.artifacts),
            "claim_count": len(msg.claims),
            "routing_features": routing_features,
        }
    )

    # 记录 message.sent 事件
    self.trace_recorder.record(
        "message.sent",
        {
            "node_id": msg.node_id,
            "message_type": "structured_message",
            "size_bytes": len(msg.summary) if msg.summary else 0,
        }
    )

    # 记录 message.quality 事件
    quality_score = 0.0
    if msg.claims:
        # 计算质量分数：有 claims 且有 evidence 的占比
        claims_with_evidence = sum(1 for c in msg.claims if c.get("evidence"))
        quality_score = claims_with_evidence / len(msg.claims)

    self.trace_recorder.record(
        "message.quality",
        {
            "node_id": msg.node_id,
            "claim_count": len(msg.claims),
            "artifact_count": len(msg.artifacts),
            "has_summary": bool(msg.summary),
            "quality_score": quality_score,
        }
    )

    # Persist StructuredMessage to SQLite messages table (原方案书 §5.3 低熵通信入库)
    if self.graph_store:
        try:
            from dataclasses import asdict
            self.graph_store.add_message(
                node_id=node.node_id,
                message_type="structured_message",
                content=json.dumps(asdict(msg), ensure_ascii=False),
                run_id=self.run_id,
            )
        except Exception as e:
            logger.warning(f"Failed to persist structured message: {e}")

    # Step 5: Route message to downstream nodes via ValueRouter (P1-G2)
    # Find all nodes that depend on this node and route the result to them
    if self.router and self.task_graph:
        try:
            downstream_nodes = self._find_downstream_nodes(node.node_id)
            routed_count = 0

            for downstream_id in downstream_nodes:
                # Determine message type based on execution outcome
                msg_type = (
                    "evidence_update"
                    if response.outcome == ExecutionOutcome.SUCCEEDED
                    else "failure_report"
                )

                # Calculate information gain based on response quality
                info_gain = 0.7 if response.artifacts else 0.4
                if response.outcome == ExecutionOutcome.SUCCEEDED:
                    info_gain += 0.2
                if response.metrics.get("test_passed"):
                    info_gain += 0.1
                info_gain = min(1.0, info_gain)

                # Route through value router
                transmitted = self.router.route_value_message(
                    sender=node.node_id,
                    receiver=downstream_id,
                    msg_type=msg_type,
                    task_id=node.node_id,
                    claims=[{
                        "text": msg.summary or "",
                        "evidence": msg.artifacts,
                        "confidence": msg.claims[0].get("confidence", 0.5) if msg.claims else 0.5
                    }],
                    evidence_refs=msg.artifacts,
                    open_questions=[a.get("reason", "") for a in response.next_actions if a.get("type") == "question"],
                    information_gain=info_gain,
                    relevance=0.8,  # High relevance for direct dependencies
                    urgency=0.9 if msg_type == "failure_report" else 0.5,
                )

                if transmitted:
                    routed_count += 1
                    # Store routed message in downstream node metadata for context retrieval
                    downstream_node = self.task_graph.get_node(downstream_id)
                    if downstream_node:
                        if "_routed_messages" not in downstream_node.metadata:
                            downstream_node.metadata["_routed_messages"] = []
                        downstream_node.metadata["_routed_messages"].append({
                            "from": node.node_id,
                            "summary": msg.summary,
                            "artifacts": msg.artifacts,
                            "status": msg.status.value,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            self.trace_recorder.record(
                "value_routing.executed",
                {
                    "node_id": node.node_id,
                    "downstream_count": len(downstream_nodes),
                    "routed_count": routed_count,
                    "dropped_count": len(downstream_nodes) - routed_count,
                    "execution_outcome": response.outcome.value,
                }
            )

        except Exception as e:
            logger.warning(f"Value routing failed for {node.node_id}: {e}")
            self.trace_recorder.record(
                "value_routing.error",
                {
                    "node_id": node.node_id,
                    "error": str(e),
                }
            )

    # Log execution result
    logger.info(
        "Agent %s returned for %s: %s",
        node.role,
        node.node_id,
        response.outcome.value,
    )

    # Runtime metadata is a durable protocol boundary. Never retain a
    # process-local protocol object here: Verifier, graph patches and
    # checkpoints all consume the same JSON-safe state.
    node.metadata["_structured_message"] = _json_safe(msg)

    if response.outcome == ExecutionOutcome.SUCCEEDED:
        return True
    return False


def _execute_wave(self: Orchestrator, node_ids: List[str], *, on_result: Optional[Callable[[str, bool], None]] = None) -> Dict[str, bool]:
    """Execute a wave of ready tasks in parallel using ThreadPoolExecutor.

    This implements the ready-wave parallel execution pattern,
    executing all dependency-satisfied tasks concurrently in each wave.

    Args:
        node_ids: List of node IDs to execute in parallel

    Returns:
        Dictionary mapping node_id to success status
    """
    results: Dict[str, bool] = {}

    wave_id = f"wave-{self.iteration_count}"
    selected_parallel = self.adaptive_concurrency.choose(
        len(node_ids),
        wave_id=wave_id,
    )
    available = self.governor.budget.available_parallel_slots()
    if available <= 0:
        self.trace_recorder.record("ready_wave.budget_deferred", {"wave_id": wave_id})
        return results
    selected_parallel = min(selected_parallel, available)
    wave_started = time.monotonic()
    self.trace_recorder.record("ready_wave.concurrency_decided", {
        "node_count": len(node_ids),
        "selected_parallel": selected_parallel,
        "max_parallel": self.max_parallel,
        "reason": "adaptive_ready_wave",
        "wave_id": wave_id,
        # Audit reason and penalty terms that produced this width, so the
        # recorded decision cannot be described as a ramp-up it never was.
        **self.adaptive_concurrency.decision_event_fields(),
    })
    if self._cancel_requested():
        self.trace_recorder.record("ready_wave.cancelled_before_start", {
            "node_ids": list(node_ids),
        })
        return {}
    if getattr(self.execute_task, "__func__", None) is execute_task:
        def accept_result(result: WorkerResult) -> None:
            node_id = result.node_id
            try:
                if result.error is not None:
                    if self._cancel_requested():
                        return
                    # Uncaught actor exceptions are framework failures, not a
                    # model verdict. Preserve the exception for the host to stop.
                    raise result.error
                if result.succeeded is not None:
                    results[node_id] = result.succeeded
                    if on_result is not None and not self._cancel_requested():
                        on_result(node_id, result.succeeded)
            finally:
                self._stop_node_services(node_id)
        def set_barrier(barrier):
            self._wave_commit_barrier = barrier
        WaveExecutor().run(node_ids, parallel=selected_parallel,
                           execute=self._execute_task_async, set_barrier=set_barrier,
                           on_result=accept_result, cancelled=self._cancel_requested)
    else:
        with ThreadPoolExecutor(max_workers=selected_parallel) as executor:
            future_to_node = {
                executor.submit(copy_context().run, self.execute_task, node_id): node_id
                for node_id in node_ids
                if not self._cancel_requested()
            }

            for future in as_completed(future_to_node):
                node_id = future_to_node[future]
                try:
                    try:
                        success = future.result()
                        if success is not None:
                            results[node_id] = success
                    except Exception as e:
                        if self._cancel_requested():
                            self.trace_recorder.record("task.execution.interrupted", {
                                "node_id": node_id,
                                "reason": str(e),
                            })
                            continue
                        logger.error(f"Wave execution failed for {node_id}: {e}", exc_info=True)
                        # Fix P5#15 (r17 crash): third uncaught re-raise site —
                        # guarded write keeps a single node's bookkeeping from
                        # aborting the whole wave and run.
                        self._transition_node_status_guarded(node_id, "failed", "wave_fallback")
                        results[node_id] = False
                    if node_id in results and on_result is not None and not self._cancel_requested():
                        on_result(node_id, results[node_id])
                finally:
                    # doc 18 修复G：会话生命周期边界——节点会话结束
                    # （成功/失败/异常/中断）即停掉它启动的后台服务，
                    # 泄漏进程占端口会让后续代际全员 runtime_no_progress。
                    self._stop_node_services(node_id)

    decision = self.adaptive_concurrency.observe(
        total=len(node_ids),
        succeeded=sum(1 for value in results.values() if value),
        elapsed_seconds=time.monotonic() - wave_started,
        wave_id=wave_id,
        actual_parallel=selected_parallel,
    )
    self.trace_recorder.record("ready_wave.concurrency_observed", decision)
    return results


def _admit_background_candidates(self: Orchestrator, parent_node) -> list[str]:
    """Admit model-discovered work through the canonical task graph.

    Background candidates are proposals, not an alternate Future-based
    execution system.  Governor value admission and graph validation add
    accepted work as ordinary TaskGraph nodes depending on the producer.
    Ready-Wave then schedules them with every other governed task.
    """
    candidates = parent_node.metadata.get("background_candidates", [])
    if not isinstance(candidates, list):
        return []
    proposals: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not str(candidate.get("goal", "")).strip():
            continue
        proposal = dict(candidate)
        proposal["dependencies"] = [parent_node.node_id]
        proposal.setdefault("task_type", "background_discovery")
        proposal.setdefault("metadata", {})
        proposal["metadata"] = {
            **dict(proposal["metadata"]),
            "execution_mode": "ready_wave",
            "source": "background_candidate",
        }
        proposals.append(proposal)
    if not proposals:
        return []
    result = self.governor.admit_worker_subtasks(
        parent_node_id=parent_node.node_id,
        proposals=proposals,
    )
    admitted_ids = list(result.get("added", []))
    if admitted_ids:
        parent_node.metadata["spawned_task_ids"] = admitted_ids
        self._persist_graph_checkpoint(
            stage="background_candidates_admitted",
            node_id=parent_node.node_id,
        )
    self.trace_recorder.record("governor.background_candidates_admitted", {
        "parent_node_id": parent_node.node_id,
        "task_node_ids": admitted_ids,
        "rejected": list(result.get("rejected_proposals", [])),
        "execution_mode": "ready_wave",
    })
    return admitted_ids


def shutdown(self: Orchestrator):
    """Cleanup shared runtime resources.

    Called at end of run() to ensure thread pools and other resources
    are properly released.
    """
    try:
        from AstralNexus.tools.executor import ToolExecutor
        wait = not getattr(self, "_wave_timed_out", False)
        ToolExecutor.shutdown_run_pool(self.run_dir, wait=wait)
        self.trace_recorder.record("orchestrator.tool_pool.shutdown", {"status": "completed" if wait else "cancel_requested", "waited": wait})
    except Exception as e:
        self.trace_recorder.record("orchestrator.tool_pool.shutdown.error", {"error": str(e)})
        logger.warning(f"Tool pool cleanup failed: {e}")


def __enter__(self: Orchestrator) -> "Orchestrator":
    """Support ``with Orchestrator(...) as orch: ...`` pattern."""
    return self


def __exit__(self: Orchestrator, exc_type, exc_val, exc_tb) -> None:
    """Ensure shutdown() runs on context exit, including KeyboardInterrupt."""
    self.shutdown()


