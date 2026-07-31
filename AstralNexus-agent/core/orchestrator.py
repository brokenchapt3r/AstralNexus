from __future__ import annotations

import json
import glob
import logging
import os
import re
import time
from queue import Empty
from dataclasses import asdict, is_dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Convert runtime state into JSON-safe checkpoint data.

    Checkpoints may contain structured protocol objects in node metadata
    (for example ``StructuredMessage``).  Persistence must retain their
    fields instead of failing the whole run, while unknown runtime objects
    are represented by a bounded diagnostic string.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        try:
            return _json_safe(asdict(value))
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict())
        except Exception:
            pass
    try:
        attributes = vars(value)
    except TypeError:
        attributes = None
    if attributes is not None:
        return _json_safe(attributes)
    return str(value)

from AstralNexus.core.agent_registry import get_agent_registry
from AstralNexus.core.exceptions import (
    AstralNexusError,
    CoreError,
    RealLLMRequiredError,
    RecoveryError,
    Severity,
    format_tool_error,
)
from AstralNexus.core.fault_recovery import Fault
from AstralNexus.core.identity import CODE_NAME, PRODUCT_NAME
from AstralNexus.core.lifecycle import AgentLifecycleManager
from AstralNexus.core.resource_scheduler import (
    Resource,
    ResourceLeaseError,
    ResourcePool,
    ResourceScheduler,
    ResourceTier,
)
from AstralNexus.core.router import Router
from AstralNexus.core.state_machine import (
    TaskStatus,
    assert_transition,
    can_transition,
)
from AstralNexus.core.task_graph import (
    ArtifactAcceptanceStatus,
    Fact,
    TaskGraph,
)
from AstralNexus.core.memory_federation import MemoryFederation
from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
from AstralNexus.core.output_paths import (
    RunOutputLayout,
)
from AstralNexus.runtime.context import ContextManager
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
from AstralNexus.core.adaptive_concurrency import AdaptiveConcurrency
from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.runtime_observer import RuntimeObserver
from AstralNexus.agents.base import ExecutionOutcome
from AstralNexus.core.verifier import (
    VerificationDimension,
    VerificationResult,
    VerificationVerdict,
)


class Orchestrator:
    """Orchestrator for managing task execution in the AstralNexus system."""
    
    def __init__(
        self, 
        run_dir: Path, 
        *, 
        max_iterations: int = 0,
        graph_store: Optional[SQLiteTaskGraphStore] = None,
        run_id: Optional[str] = None,
        max_parallel: int = 4,
        event_callback=None,
    ):
        self.run_dir = run_dir
        self.output_layout = RunOutputLayout(run_dir)
        self.output_layout.ensure_runtime_dirs()
        self.task_graph = TaskGraph()
        self.trace_recorder = TraceRecorder(run_dir, event_callback=event_callback)
        self._node_activity_types: Dict[str, str] = {}
        self.agent_registry = get_agent_registry()
        self.lifecycle_manager = AgentLifecycleManager(run_dir)
        self.context_manager = ContextManager(max_context_tokens=8000)
        self.memory_federation = MemoryFederation(graph_store)
        self.router = Router(self.task_graph, self.trace_recorder)
        self._current_topology_state: Dict[str, Any] = {}
        
        # Initialize fault recovery system — FaultRecoveryEngine is the sole
        # entry point.  Its .detector / .analyzer / .planner are exposed for
        # callers that need individual pipeline steps, but Orchestrator no
        # longer holds independent instances of the three-piece set.
        from AstralNexus.core.fault_recovery import FaultRecoveryEngine
        self.fault_recovery_engine = FaultRecoveryEngine(self.task_graph, self.trace_recorder)
        # Recovery diagnostics and execution share the canonical graph.
        self.fault_detector = self.fault_recovery_engine.detector
        self.impact_analyzer = self.fault_recovery_engine.analyzer
        self.recovery_executor = self.fault_recovery_engine.executor
        self.trace_recorder.record("orchestrator.fault_recovery_engine.initialized", {})
        
        # Initialize resource scheduler with default resource pool
        resource_pool = self._create_default_resource_pool()
        self.resource_scheduler = ResourceScheduler(resource_pool)
        
        # SQLite is the sole durable task-graph source for every run.
        self.graph_store = graph_store or SQLiteTaskGraphStore(
            self.run_dir / "task_graph.sqlite"
        )
        self.run_id = run_id or f"run-{int(time.time())}"
        self.runtime_state_manager = RuntimeStateManager(self.run_dir, self.run_id)
        self.lifecycle_manager.restore_snapshot(
            list(self.runtime_state_manager.state.active_agents)
        )
        self.runtime_observer = RuntimeObserver(
            self, self.runtime_state_manager
        )
        self.max_parallel = max_parallel
        self.adaptive_concurrency = AdaptiveConcurrency(max_parallel)
        self.model_config: Dict[str, Any] = {}
        self._completion_no_progress = 0
        self._completion_gap_fingerprint = ""
        self._completion_gap_repeats = 0
        self._execution_lock = Lock()
        # Interactive control is optional for embedded callers.  When bound,
        # the orchestrator is the only component that mutates the graph; the
        # UI only queues commands and sets the cooperative cancellation token.
        self._interrupt_event: Optional[Event] = None
        self._interrupt_queue = None
        self._interrupt_reason = ""
        self._interrupt_requested_at = None
        
        # Initialize GlobalGovernor for dynamic task admission (阶段五 P0)
        from AstralNexus.core.governor import GlobalGovernor
        self.governor = GlobalGovernor(
            self.task_graph,
            self.trace_recorder,
            admission_threshold=0.65,
            max_active_tasks=max(
                1, int(os.environ.get("ASTRALNEXUS_MAX_ACTIVE_TASKS", "2000"))
            ),
            max_total_tasks=max(
                1, int(os.environ.get("ASTRALNEXUS_MAX_TOTAL_TASKS", "10000"))
            ),
        )
        self.router.set_communication_edge_committer(
            self._commit_router_communication_edge
        )

        # The CLI enables the real control-plane matcher after construction.
        # Keeping construction dependency-free preserves embedded/test use;
        # formal product runs call enable_real_llm_worker_matching().
        
        # The core Verifier is the only quality authority.
        from AstralNexus.core.verifier import Verifier
        self.verifier = Verifier(
            self.task_graph,
            self.trace_recorder,
            pass_threshold=0.70,
            repair_threshold=0.45,
            max_repair_attempts=max(
                0,
                int(os.environ.get("ASTRALNEXUS_MAX_VERIFICATION_REPAIRS", "0")),
            ),
        )
        
        try:
            self.graph_store.create_run(self.run_id, "Initializing", [])
            self.trace_recorder.record(
                "orchestrator.persistence_initialized",
                {"run_id": self.run_id, "store_path": str(self.graph_store.db_path)}
            )
        except Exception as e:
            self.trace_recorder.record(
                "orchestrator.persistence_error",
                {"error": str(e)}
            )
            logger.warning(f"Failed to initialize run record: {e}")
        
        self.max_iterations = max_iterations
        self.iteration_count = 0

    def attach_interrupt_control(self, interrupt_event: Event, interrupt_queue) -> None:
        """Attach the interactive controller without coupling core to the TUI."""
        self._interrupt_event = interrupt_event
        self._interrupt_queue = interrupt_queue
        self.trace_recorder.record("orchestrator.interrupt_control.attached", {})

    def _cancel_requested(self) -> bool:
        return bool(self._interrupt_event is not None and self._interrupt_event.is_set())

    def _process_pending_interrupts(self) -> Dict[str, Any]:
        """Apply queued changes at a graph-safe orchestration boundary."""
        if self._interrupt_queue is None:
            return {"stop": self._cancel_requested(), "changed": False}
        changed = False
        plain_interrupt = False
        while True:
            try:
                item = self._interrupt_queue.get_nowait()
            except Empty:
                break
            item = item if isinstance(item, dict) else {"type": "interrupt", "message": str(item)}
            if item.get("type") in {"requirement_change", "inject_requirement"}:
                description = str(item.get("description") or item.get("message") or "")
                affected = item.get("affected_constraints")
                self.trace_recorder.record("requirement_change.queued", {
                    "description": description[:2000],
                    "change_kind": item.get("change_kind"),
                })
                self.handle_requirement_change(
                    description,
                    affected_constraints=list(affected or []),
                    change_kind=item.get("change_kind"),
                    resume=False,
                )
                changed = True
            else:
                plain_interrupt = True
                self._interrupt_reason = str(item.get("message") or "user_interrupt")
        return {"stop": plain_interrupt, "changed": changed}

    def _mark_interrupted(self, reason: str) -> None:
        self._interrupt_reason = reason or "user_interrupt"
        self._interrupt_requested_at = datetime.now(timezone.utc).isoformat()
        self._save_checkpoint()
        self._persist_graph_checkpoint(stage="interrupted")
        self.trace_recorder.record("orchestrator.interrupted", {
            "reason": self._interrupt_reason,
            "requested_at": self._interrupt_requested_at,
            "iteration": self.iteration_count,
        })

    def enable_real_llm_worker_matching(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Enable the real LLM Worker Profile decision stage for delivery runs."""
        from AstralNexus.runtime.llm_client import LLMClient
        from AstralNexus.runtime.model_selector import get_model_config

        resolved_config = dict(get_model_config())
        if config:
            resolved_config.update({key: value for key, value in config.items() if value})
        config = resolved_config
        self.model_config = dict(config)
        client = LLMClient(
            model=config.get("model") or "qwen3.7-max",
            base_url=config.get("base_url"),
            timeout=int(os.environ.get("ASTRALNEXUS_MATCHER_TIMEOUT", "120")),
            interactive=False,
            event_callback=lambda event: self.trace_recorder.record(
                event.get("event_type", "llm.event"), event
            ),
        )
        self.governor.enable_llm_worker_matching(client)
        self.governor.set_forced_worker_model(
            config.get("model") if config.get("_model_explicit_override") else None
        )

    def _record_node_runtime_event(
        self,
        node_id: str,
        role: str,
        event: Dict[str, Any],
    ) -> None:
        """Persist raw provider evidence and a stable activity projection."""
        payload = dict(event)
        event_type = str(payload.pop("event_type", "llm.event"))
        payload.pop("node_id", None)
        self.trace_recorder.record(
            event_type, {"node_id": node_id, "role": role, **payload}
        )
        activity_type = ""
        label = ""
        tool = ""
        input_preview = ""
        status = "running"
        if event_type == "llm.request":
            activity_type, label = "model_request", "正在请求模型生成下一步"
        elif event_type == "llm.response":
            activity_type, label = "response_received", "模型响应已返回，正在整理结果"
        elif event_type == "llm.text.delta":
            activity_type, label = "generating_output", "正在流式生成节点输出"
        elif event_type == "llm.tool_execution.start":
            calls = payload.get("calls") or []
            first = calls[0] if calls and isinstance(calls[0], dict) else {}
            tool = str(first.get("name") or "tool")
            input_preview = str(first.get("arguments") or "")
            activity_type, label = "tool_call", f"正在调用工具 {tool}"
        elif event_type in {"llm.tool_execution.done", "llm.tool_batch"}:
            calls = payload.get("calls") or []
            first = calls[0] if calls and isinstance(calls[0], dict) else {}
            tool = str(first.get("name") or "tool")
            failed = any(
                isinstance(item, dict)
                and str(item.get("status")) in {"error", "failed"}
                for item in calls
            )
            activity_type = "tool_result"
            label = f"工具 {tool} {'执行失败' if failed else '执行完成'}"
            status = "failed" if failed else "running"
        elif event_type.startswith("context.compressed"):
            activity_type, label = "context_compression", "正在压缩上下文"
        elif event_type.startswith("coder.validation"):
            activity_type, label = "verification", "正在验证代码与交付物"
        if not activity_type:
            return
        deduplicate = activity_type in {
            "model_request", "response_received", "generating_output"
        }
        if deduplicate and self._node_activity_types.get(node_id) == activity_type:
            return
        self._node_activity_types[node_id] = activity_type
        node = self.task_graph.get_node(node_id)
        self.trace_recorder.record_agent_activity(
            node_id,
            activity_type=activity_type,
            label=label,
            agent_id=str((node.metadata if node else {}).get("worker_agent_id") or ""),
            role=role,
            tool=tool,
            input_preview=input_preview,
            status=status,
            source_event=event_type,
        )

    def _sync_task_graph_bindings(self) -> None:
        """Make every orchestration subsystem observe the canonical graph.

        Evaluation and embedding callers may inject a prebuilt graph after
        constructing the orchestrator.  Keeping only ``self.task_graph`` in
        sync is unsafe because Governor, Router, Verifier and recovery
        components retain their own references.  A dynamic admission then
        appears in one graph while scheduling reads another.
        """
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

    def _persist_graph_checkpoint(self, *, stage: str, node_id: Optional[str] = None) -> None:
        """Persist the canonical graph after a lifecycle boundary.

        Dynamic patches are admitted after the initial checkpoint and node
        verification happens after the worker wave returns.  A single save at
        either side can therefore leave SQLite behind the in-memory graph.
        Serialize checkpoint writes and record the exact persisted status so a
        run can distinguish a real stall from an audit artifact.
        """
        if not self.graph_store:
            return
        with self._execution_lock:
            self.graph_store.save_task_graph(self.run_id, self.task_graph)
        node = self.task_graph.get_node(node_id) if node_id else None
        self.trace_recorder.record("orchestrator.persistence.checkpoint", {
            "stage": stage,
            "node_id": node_id,
            "status": node.status if node else None,
            "node_count": len(self.task_graph.nodes),
        })

    def load_authoritative_graph(self) -> TaskGraph:
        """Load an existing run through the Orchestrator control boundary."""
        restored = self.graph_store.load_task_graph(self.run_id)
        if restored is None:
            raise RuntimeError(
                f"No task graph for run_id {self.run_id} in {self.graph_store.db_path}"
            )
        self.task_graph = restored
        self._sync_task_graph_bindings()
        return restored

    def _commit_router_communication_edge(self, proposal) -> None:
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

    def rebind_runtime(self, *, task_graph: TaskGraph, trace_recorder: TraceRecorder) -> None:
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
    
    def _create_default_resource_pool(self) -> ResourcePool:
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
    
    def initialize_task_graph(self, goal: str, constraints: Optional[List[str]] = None) -> str:
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
        self,
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

        for patch_attempt in range(3):
            try:
                if patch_attempt == 0:
                    patch = self.governor.plan_task_graph_patch(
                        planning_goal or node.goal,
                        graph_summary=graph_summary,
                        capability_catalog=capability_catalog,
                        resource_catalog=resource_catalog,
                        event_callback=record_planner_stream,
                    )
                else:
                    patch = self.governor.replan_rejected_task_graph_patch(
                        planning_goal or node.goal,
                        patch_error,
                        graph_summary=graph_summary,
                        capability_catalog=capability_catalog,
                        resource_catalog=resource_catalog,
                        event_callback=record_planner_stream,
                    )
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
            except Exception as exc:
                patch_error = str(exc)[:500]
                self.trace_recorder.record(
                    "orchestrator.graph_patch.rejected",
                    {
                        "parent_task_id": node_id,
                        "attempt": patch_attempt + 1,
                        "error": patch_error,
                    },
                )

        if applied is None:
            self.trace_recorder.record_node_output(
                node_id,
                {
                    "node_id": node_id,
                    "role": "planner",
                    "execution_outcome": "failed",
                    "content": patch_error or "Governor graph patch rejected",
                    "summary": "Governor planning failed after three graph-patch attempts",
                    "artifacts": [],
                    "next_actions": [],
                    "metrics": {"attempts": 3, "error": patch_error},
                },
            )
            if node.status != "completed":
                self.task_graph.update_node_status(node_id, "failed")
            self.trace_recorder.record(
                "orchestrator.planning_failed",
                {
                    "node_id": node_id,
                    "goal": (planning_goal or node.goal)[:100],
                    "reason": patch_error or "graph_patch_rejected",
                    "attempts": 3,
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
            raise RealLLMRequiredError(
                f"Governor graph patch rejected after 3 replans: {patch_error}"
            )

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
            if self.task_graph.get_node(node_id).status == "ready":
                self.task_graph.update_node_status(node_id, "running")
            if self.task_graph.get_node(node_id).status == "running":
                self.task_graph.update_node_status(node_id, "result_submitted")
            if self.task_graph.get_node(node_id).status == "result_submitted":
                self.task_graph.update_node_status(node_id, "verifying")
            if self.task_graph.get_node(node_id).status == "verifying":
                self.task_graph.update_node_status(node_id, "completed")
        return list(applied["added"])

    def _build_governor_planning_context(self) -> tuple[str, str, str]:
        """Project runtime facts for Governor without making policy decisions."""
        graph_summary = json.dumps(
            [
                {
                    "node_id": node.node_id,
                    "status": node.status,
                    "goal": node.goal,
                    "dependencies": list(node.dependencies),
                    "required_capabilities": node.metadata.get(
                        "required_capabilities", []
                    ),
                }
                for node in self.task_graph.nodes.values()
            ],
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
                    "reasoning_level": profile.reasoning_level,
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
    
    def select_next_tasks(self) -> List[str]:
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
    
    def execute_task(self, node_id: str) -> bool:
        """Execute a specific task with integrated state machine, resource scheduling, and fault recovery.
        
        Args:
            node_id: The ID of the task node to execute
            
        Returns:
            True if execution was successful, False otherwise
        """
        node = self.task_graph.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")
        
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
                },
                HETEROGENEOUS_WORKERS,
                resources=[
                    {"tier": resource.tier.value, "available": resource.available}
                    for resource in self.resource_scheduler.resource_pool.get_all_resources()
                ],
            )
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
                self.resource_scheduler.release_resource(resource, node_id=node_id)
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
        governor_execution_started_at = time.monotonic()
        self.governor.record_task_start(
            node_id,
            estimated_tokens=int(
                (node.metadata.get("budget") or {}).get("estimated_tokens", 0)
                or 0
            ),
        )
        try:
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
                success = self._run_agent_task(agent, node)

                # The task contract may contain model-produced, generic
                # background candidates.  Governor is the only component
                # allowed to admit them; absence of candidates means no
                # background work is created.
                if success:
                    self._admit_background_candidates(node)

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
                
                # Determine target status: execution produces result_submitted, not final state
                target_status = "result_submitted" if success else "failed"
                
                # Use state machine to validate and perform transition
                try:
                    assert_transition(node.status, target_status)
                    self.task_graph.update_node_status(node_id, target_status)
                except ValueError as e:
                    self.trace_recorder.record(
                        "task.execution.invalid_transition",
                        {
                            "node_id": node_id,
                            "current_status": node.status,
                            "target_status": target_status,
                            "error": str(e),
                        }
                    )
                    raise
                    success = False

                # Make the worker handoff durable before downstream selection.
                # The verifier may run in the orchestration thread afterwards;
                # this checkpoint prevents a timeout from resurrecting the
                # node as ``admitted`` on resume.
                if success:
                    self._persist_graph_checkpoint(
                        stage="result_submitted", node_id=node_id
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
                    # Task failed, update node status
                    self.task_graph.update_node_status(node_id, "failed")
                
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
                    self.task_graph.update_node_status(node_id, "failed")
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
                else:
                    self.task_graph.update_node_status(node_id, "failed")
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
        except Exception as e:
            # Handle execution errors
            self.task_graph.update_node_status(node_id, "failed")
            
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
            evidence_packet = node.metadata.get("evidence_packet") or {}
            usage = evidence_packet.get("token_usage") or {}
            total_tokens = usage.get("total_tokens")
            if not isinstance(total_tokens, (int, float)):
                total_tokens = sum(
                    value
                    for key, value in usage.items()
                    if key in {"prompt_tokens", "completion_tokens"}
                    and isinstance(value, (int, float))
                )
            tool_calls = usage.get("tool_calls", 0)
            if not isinstance(tool_calls, (int, float)):
                tool_calls = 0
            successful_statuses = {
                TaskStatus.RESULT_SUBMITTED.value,
                TaskStatus.VERIFYING.value,
                TaskStatus.COMPLETED.value,
            }
            execution_outcome = (
                "succeeded" if node.status in successful_statuses else "failed"
            )
            self.governor.record_task_end(
                node_id,
                actual_tokens=int(total_tokens or 0),
                actual_time_seconds=max(
                    0.0, time.monotonic() - governor_execution_started_at
                ),
                tool_calls=int(tool_calls),
                execution_outcome=execution_outcome,
                stop_reason=(
                    ""
                    if execution_outcome == "succeeded"
                    else str(
                        (node.metadata.get("last_failure") or {}).get("message")
                        or node.metadata.get("error")
                        or "worker_execution_failed"
                    )
                ),
            )
            self.resource_scheduler.release_resource(resource, node_id=node_id)
    
    def _record_worker_verdict(self, node, verdict: VerificationVerdict) -> None:
        """Project the authoritative Verifier verdict onto worker lifecycles."""
        approved = verdict == VerificationVerdict.APPROVED
        repairable = verdict in {
            VerificationVerdict.REPAIR_REQUIRED,
            VerificationVerdict.REPLAN_REQUIRED,
            VerificationVerdict.INCONCLUSIVE,
        }
        worker_agent_id = node.metadata.get("worker_agent_id") if node else None
        if worker_agent_id:
            self.lifecycle_manager.record_verification_verdict(
                worker_agent_id,
                approved=approved,
                repairable=repairable,
                reason=f"Verifier verdict: {verdict.value}",
            )
    def _finalize_node_evidence(
        self,
        node,
        *,
        approved: bool,
        verification_score: float,
        verdict: VerificationVerdict,
    ) -> None:
        """Commit or reject worker evidence at the unique Verifier boundary."""
        task_version = self._node_task_version(node)
        artifact_ids = list(node.metadata.pop("_pending_artifact_ids", []))
        fact_records = list(node.metadata.pop("_pending_fact_records", []))
        decision = "accepted" if approved else "rejected"

        for artifact_id in artifact_ids:
            artifact = self.task_graph.artifacts.get(artifact_id)
            if artifact is None:
                continue
            artifact.acceptance_status = decision
            artifact.metadata.update(
                {
                    "verification_verdict": verdict.value,
                    "verification_score": float(verification_score),
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        committed_fact_ids: list[str] = []
        if approved:
            for index, record in enumerate(fact_records, start=1):
                fact_id = (
                    f"F-{node.node_id}-v{task_version}"
                    f"-r{node.retry_count}-{index:03d}"
                )
                fact = Fact(
                    fact_id=fact_id,
                    text=str(record["text"]),
                    source=node.node_id,
                    confidence=max(
                        0.0, min(1.0, float(record.get("confidence", 0.0)))
                    ),
                    recorded_at=datetime.now(timezone.utc).isoformat(),
                    metadata={
                        "evidence_refs": list(record.get("evidence") or []),
                        "verification_score": float(verification_score),
                        "verification_verdict": verdict.value,
                        "task_version": task_version,
                    },
                )
                self.task_graph.add_fact(fact)
                self.memory_federation.add_fact(fact)
                committed_fact_ids.append(fact_id)

            downstream_ids = self._find_downstream_nodes(node.node_id)
            if downstream_ids:
                from AstralNexus.core.governor import TaskGraphPatch

                self.governor.apply_task_graph_patch(
                    TaskGraphPatch(
                        add_edges=[
                            {
                                "source": node.node_id,
                                "target": downstream_id,
                                "edge_type": "evidence_for",
                                "metadata": {
                                    "fact_ids": committed_fact_ids,
                                    "artifact_ids": artifact_ids,
                                    "verification_score": float(verification_score),
                                },
                            }
                            for downstream_id in downstream_ids
                        ],
                        reason_codes=["VERIFIED_EVIDENCE_LINK"],
                        reasoning="Bind approved evidence to dependent nodes.",
                    )
                )

        self.trace_recorder.record(
            "task.evidence.finalized",
            {
                "node_id": node.node_id,
                "verdict": verdict.value,
                "approved": approved,
                "verification_score": float(verification_score),
                "artifact_ids": artifact_ids,
                "fact_ids": committed_fact_ids,
                "rejected_claim_count": 0 if approved else len(fact_records),
            },
        )

    @staticmethod
    def _node_task_version(node: Any) -> int:
        """Read the canonical task version from node metadata.

        TaskNode deliberately owns graph structure and lifecycle only. The
        mutable task version belongs to the Governor/runtime contract and is
        therefore persisted in metadata.
        """
        try:
            return max(1, int((getattr(node, "metadata", {}) or {}).get("task_version", 1)))
        except (TypeError, ValueError):
            return 1

    def _apply_governed_verification_recovery(self, node, verification_result) -> Dict[str, Any]:
        """Ask GlobalGovernor for the only permitted verification-recovery patch."""
        dimensions = [
            {
                "name": dimension.name,
                "score": dimension.score,
                "passed": dimension.passed,
                "details": dimension.details,
                "evidence_refs": list(dimension.evidence_refs),
            }
            for dimension in verification_result.dimensions
        ]
        try:
            patch = self.governor.plan_verification_recovery(
                node_id=node.node_id,
                verdict=verification_result.verdict.value,
                overall_score=verification_result.overall_score,
                dimensions=dimensions,
            )
            # A recovery proposal that only re-admits the same node and adds
            # no new work/evidence is not a recovery.  Count identical
            # no-progress proposals before applying them so the graph cannot
            # cycle through ``repair_pending -> ready`` indefinitely.
            added_nodes = list(getattr(patch, "add_nodes", []) or [])
            updated_nodes = list(getattr(patch, "update_nodes", []) or [])
            no_progress = not added_nodes and any(
                str(item.get("node_id") or item.get("task_id")) == node.node_id
                and str(item.get("status") or "") in {"ready", "repair_pending"}
                for item in updated_nodes
                if isinstance(item, dict)
            )
            if no_progress:
                repeats = int(node.metadata.get("_verification_recovery_no_progress", 0)) + 1
                node.metadata["_verification_recovery_no_progress"] = repeats
                if repeats >= 3:
                    node.metadata["_terminal_failure"] = True
                    node.metadata["_terminal_reason"] = (
                        "verification recovery produced no new work or evidence "
                        f"for {repeats} consecutive proposals"
                    )
                    if node.status == TaskStatus.REPAIR_PENDING.value:
                        self.task_graph.update_node_status(node.node_id, TaskStatus.FAILED.value)
                    self.trace_recorder.record(
                        "governor.verification_recovery.no_progress",
                        {
                            "node_id": node.node_id,
                            "repeat_count": repeats,
                            "reason_codes": ["REPEATED_NO_PROGRESS"],
                            "status": node.status,
                        },
                    )
                    return {
                        "accepted": False,
                        "blocked": True,
                        "node_id": node.node_id,
                        "repeat_count": repeats,
                    }
            else:
                node.metadata["_verification_recovery_no_progress"] = 0
            applied = self.governor.apply_task_graph_patch(
                patch,
                parent_task_id=node.node_id,
            )
        except (ValueError, TypeError, KeyError) as exc:
            # A model-produced recovery patch is untrusted input.  In
            # particular, values such as ``replaced_by`` are verdict prose,
            # not lifecycle states.  Do not issue a second automatic recovery
            # request after the same malformed patch: persist an honest
            # terminal failure and let the delivery gate report the gap.
            node.metadata["_recovery_patch_rejected"] = True
            node.metadata["_terminal_failure"] = True
            node.metadata["_terminal_reason"] = (
                f"invalid governed verification recovery patch: {exc}"
            )
            if node.status == TaskStatus.REPAIR_PENDING.value:
                self.task_graph.update_node_status(node.node_id, TaskStatus.FAILED.value)
            self.trace_recorder.record(
                "governor.verification_recovery.rejected",
                {
                    "node_id": node.node_id,
                    "error": str(exc),
                    "reason_codes": ["INVALID_RECOVERY_PATCH"],
                    "status": node.status,
                },
            )
            return {
                "accepted": False,
                "rejected": True,
                "node_id": node.node_id,
                "error": str(exc),
            }
        self.trace_recorder.record(
            "orchestrator.verification_recovery.applied",
            {
                "node_id": node.node_id,
                "verdict": verification_result.verdict.value,
                "score": verification_result.overall_score,
                "patch": applied,
            },
        )
        return applied

    def _instantiate_governed_worker(self, node, role_hint: str):
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
    
    def _run_agent_task(self, agent, node) -> bool:
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
        
        # Build enriched metadata with routing info
        execution_metadata = {
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
            "expected_outputs": list(node.metadata.get("expected_outputs", []) or []),
            "trigger": node.metadata.get("trigger", ""),
            "parent_node_id": node.metadata.get("parent_node_id", ""),
        }
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
            if is_directory_path or (has_path_separator and has_file_extension):
                expected_artifact_paths.append(normalized_output)
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
        if execution_metadata["expected_artifact_paths"] and "file_write" not in selected_tools:
            selected_tools.append("file_write")
        tool_catalog = discover_tools(
            agent.role,
            node.goal,
            selected_tools,
            required_tools=(
                ["file_write"]
                if execution_metadata["expected_artifact_paths"]
                else []
            ),
        )
        execution_metadata["tool_catalog"] = tool_catalog
        if task_contract:
            execution_metadata["task_contract"] = task_contract
        if routing_features:
            execution_metadata["routing_features"] = routing_features
            execution_metadata["risk_score"] = routing_features.get("risk_score")
            execution_metadata["keywords"] = routing_features.get("keywords")
        if routing_context:
            execution_metadata["relevant_facts"] = routing_context.get("relevant_facts", [])
            execution_metadata["relevant_artifacts"] = routing_context.get("relevant_artifacts", [])
        
        # Merge routing context resources with node resources
        node_resources = dict(node.metadata.get("resources", {}))
        if routing_context:
            # Add upstream artifacts from routing context to execution resources
            upstream_artifacts = routing_context.get("relevant_artifacts", [])
            if upstream_artifacts:
                node_resources["upstream_artifacts"] = upstream_artifacts
        
        context = AgentContext(
            node_id=node.node_id,
            goal=node.goal,
            constraints=node.constraints,
            tools=[item["name"] for item in tool_catalog],
            resources=node_resources,
            artifacts=node.outputs,
            metadata=execution_metadata,
        )
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
                "agent_id": str(node.metadata.get("worker_agent_id") or ""),
                "assigned_by": "GlobalGovernor",
                "prompt_source": "Governor Task Contract + Router Context",
                "routing_context": routing_context or {},
                "task_contract": task_contract or {},
                "metadata": execution_metadata,
            },
        )
        # Install the live sink only after the JSON node input has been
        # persisted; callbacks are runtime-only and must never leak into the
        # serialized execution contract.
        context.metadata["_runtime_event_callback"] = lambda event: self._record_node_runtime_event(
            node.node_id,
            execution_metadata["role"],
            event,
        )
        context.metadata["_cancel_check"] = self._cancel_requested
        
        # Step 2: Execute the agent
        try:
            response: AgentResponse = agent.execute(context)
        except Exception as e:
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

        if response.outcome == ExecutionOutcome.INTERRUPTED:
            # A cancellation is not a task failure.  Return the node to the
            # ready state so checkpoint/resume or a requirement patch can
            # continue it without manufacturing a recovery branch.
            try:
                self.task_graph.update_node_status(node_id, "ready")
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
                "node_id": node_id,
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
        file_outputs = [
            item for item in output_contract
            if Path(item).name != item
            or Path(item).suffix
            or "/" in item
            or "\\" in item
        ]
        # Delivery-contract paths may be carried in metadata when the
        # Governor owns the output contract and the graph node keeps only an
        # opaque output id.  They are still hard physical postconditions.
        for item in execution_metadata.get("expected_artifact_paths", []) or []:
            if item not in file_outputs:
                file_outputs.append(item)

        execution_started_ns = int(
            context.metadata.get("execution_started_ns") or time.time_ns()
        )

        def fresh_file(path: Path) -> bool:
            if not path.is_file():
                return False
            try:
                return path.stat().st_mtime_ns >= execution_started_ns
            except OSError:
                return False

        def materialized_contract_path(output: str) -> Path | None:
            """Resolve an output created or refreshed by this execution."""
            raw_path = Path(output)
            candidates = [raw_path] if raw_path.is_absolute() else [
                self.run_dir / raw_path,
                self.run_dir / ".delivery_staging" / raw_path,
            ]
            if str(output).endswith(("/", "\\")):
                return next((
                    path for path in candidates
                    if path.is_dir() and any(fresh_file(item) for item in path.rglob("*"))
                ), None)
            return next((path for path in candidates if fresh_file(path)), None)
        # Durable files survive a failed worker, but their existence cannot
        # turn a failed execution into success. Preserve them as partial
        # evidence so recovery can continue from disk without allowing failed
        # upstream nodes to unlock downstream delivery gates.
        if response.outcome != ExecutionOutcome.SUCCEEDED and file_outputs:
            materialized_paths = []
            missing_outputs = []
            for output in file_outputs:
                existing = materialized_contract_path(output)
                if existing is None:
                    missing_outputs.append(output)
                else:
                    materialized_paths.append(str(existing.resolve()))
            if not missing_outputs:
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
                                fresh_file(Path(match))
                                for match in glob.glob(str(pattern), recursive=True)
                            )
                            if has_materialized:
                                break
                if not has_materialized:
                    missing_outputs.append(output)
            if missing_outputs:
                response.outcome = ExecutionOutcome.FAILED
                response.metrics = dict(response.metrics or {})
                response.metrics.update({
                    "runtime_status": "failed",
                    "execution_path": "artifact_postcondition_failed",
                    "error_type": "MissingDeclaredArtifacts",
                    "missing_outputs": missing_outputs,
                })
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
                })
                self.trace_recorder.record(
                    "task.execution.semantic_incomplete",
                    {
                        "node_id": node.node_id,
                        "conclusion": conclusion[:500],
                        "artifacts": list(getattr(response, "artifacts", []) or []),
                    },
                )
        self.trace_recorder.record_node_output(
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
                previous.acceptance_status = ArtifactAcceptanceStatus.REJECTED.value
                previous.metadata["rejection_reason"] = "superseded by a new attempt"
        pending_artifact_ids: list[str] = []
        for artifact_path in response.artifacts:
            artifact_id = f"A-{node.node_id}-{len(self.task_graph.artifacts)+1:03d}"
            self.task_graph.add_artifact({
                "artifact_id": artifact_id,
                "node_id": node.node_id,
                "artifact_type": "file",
                "path": artifact_path,
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
        
        # Store structured message for acceptance verification (fix-2b)
        node.metadata["_structured_message"] = msg
        
        if response.outcome == ExecutionOutcome.SUCCEEDED:
            return True
        return False
    
    
    def is_execution_complete(self) -> bool:
        """Check whether the currently admitted subgraph is terminal.
        
        Returns:
            True when all current nodes have converged successfully or were
            explicitly superseded/cancelled.  This does not authorize final
            delivery; Global Verifier and Synthesizer own that later gate.
        """
        for node in self.task_graph.nodes.values():
            if node.status not in ["completed", "superseded", "cancelled"]:
                return False  # Not all nodes are in terminal state
        
        return True

    def _delivery_progress_snapshot(self) -> tuple[str, List[str]]:
        """Build a bounded, artifact-backed view for Global Verifier."""
        refs: List[str] = []
        run_root = self.run_dir.resolve()

        def canonical_ref(raw: Any) -> str:
            path = Path(str(raw))
            candidate = path if path.is_absolute() else self.run_dir / path
            try:
                return candidate.resolve().relative_to(run_root).as_posix()
            except (OSError, ValueError):
                return str(candidate.resolve())

        for artifact in self.task_graph.artifacts.values():
            if artifact.path:
                refs.append(canonical_ref(artifact.path))
        deliverables = self.run_dir / "deliverables"
        if deliverables.is_dir():
            refs.extend(
                canonical_ref(path)
                for path in deliverables.rglob("*")
                if path.is_file()
            )
        refs = list(dict.fromkeys(refs))
        lines = ["# Runtime delivery progress", "", "## Task nodes"]
        for node in self.task_graph.nodes.values():
            lines.append(
                f"- {node.node_id} [{node.status}] "
                f"worker={(node.metadata.get('worker_profile') or {}).get('worker_type', node.role)} "
                f"goal={node.goal}"
            )
        lines.extend(["", "## Materialized artifacts"])
        for ref in refs:
            lines.append(f"- {ref}")

        # Put compact verification evidence before large source documents.  The
        # delivery verifier intentionally consumes a bounded prefix, so a
        # filesystem traversal order must never hide the newest test report or
        # evidence manifest behind a large generated API document.
        evidence_markers = (
            "evidence", "report", "result", "test", "health", "server",
            "manifest", "tree", "验收", "测试", "证据", "日志",
        )
        ordered_refs = sorted(
            refs,
            key=lambda ref: (
                0 if any(marker in ref.casefold() for marker in evidence_markers) else 1,
                ref.casefold(),
            ),
        )
        lines.extend(["", "## Prioritized artifact evidence"])
        remaining = 220_000
        for ref in ordered_refs:
            raw_path = Path(ref)
            path = raw_path if raw_path.is_absolute() else self.run_dir / raw_path
            if (
                remaining > 0
                and path.is_file()
                and path.suffix.lower() in {
                    ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml",
                    ".html", ".log", ".csv",
                }
            ):
                try:
                    sample = path.read_text(encoding="utf-8", errors="ignore")[
                        : min(16_000, remaining)
                    ]
                except OSError:
                    continue
                lines.append(f"\n### Artifact: {ref}")
                lines.append(sample)
                remaining -= len(sample)
        return "\n".join(lines), refs

    def _completed_synthesizer_nodes(self) -> List[str]:
        return [
            node.node_id
            for node in self.task_graph.nodes.values()
            if node.status == TaskStatus.COMPLETED.value
            and (
                str((node.metadata.get("worker_profile") or {}).get("worker_type", "")).lower()
                == "synthesizer"
                or str(node.role).lower() == "synthesizer"
            )
        ]

    def _assess_delivery_completion(self, goal: str) -> Dict[str, Any]:
        """Let Global Verifier authorize completion or ask Governor to expand.

        A terminal current graph is only a planning boundary.  Missing
        contract evidence is converted into another Governor patch; the
        executor never invents fixed workflow nodes.
        """
        contract = dict(self.governor.delivery_contract or {})
        if not contract:
            return {"authorized": True, "reason": "no_delivery_contract"}
        content, artifact_refs = self._delivery_progress_snapshot()
        try:
            report = self.governor.verify_delivery(
                contract,
                content=content,
                artifact_refs=artifact_refs,
                run_dir=self.run_dir,
                client=self.governor.llm_client,
            )
        except Exception as exc:
            report = {
                "overall_status": "fail",
                "synthesis_authorized": False,
                "coverage_gaps": [f"Global Verifier execution failed: {exc}"],
                "required_fixes": ["retry_global_verification"],
                "failed_tests": ["global_verifier_execution"],
            }
        synthesizers = self._completed_synthesizer_nodes()
        authorized = bool(
            report.get("overall_status") == "pass"
            and report.get("synthesis_authorized")
            and synthesizers
        )
        gap_fingerprint = json.dumps(
            {
                "failed_tests": sorted(str(item) for item in report.get("failed_tests", [])),
                "coverage_gaps": report.get("coverage_gaps", []),
                "required_fixes": report.get("required_fixes", []),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if gap_fingerprint == self._completion_gap_fingerprint:
            self._completion_gap_repeats += 1
        else:
            self._completion_gap_fingerprint = gap_fingerprint
            self._completion_gap_repeats = 1
        self.trace_recorder.record(
            "governor.execution_completion.assessed",
            {
                "authorized": authorized,
                "overall_status": report.get("overall_status"),
                "synthesizer_nodes": synthesizers,
                "coverage_gaps": report.get("coverage_gaps", []),
                "failed_tests": report.get("failed_tests", []),
                "artifact_count": len(artifact_refs),
            },
        )
        if authorized:
            self._completion_no_progress = 0
            self._completion_gap_fingerprint = ""
            self._completion_gap_repeats = 0
            return {"authorized": True, "report": report}

        if self._completion_gap_repeats >= 3:
            self.trace_recorder.record(
                "orchestrator.delivery_contract.stalled",
                {
                    "repeat_count": self._completion_gap_repeats,
                    "failed_tests": report.get("failed_tests", []),
                    "coverage_gaps": report.get("coverage_gaps", []),
                    "reason_codes": ["REPEATED_DELIVERY_GAP"],
                },
            )
            return {
                "authorized": False,
                "changed": False,
                "blocked": True,
                "repeat_count": self._completion_gap_repeats,
                "report": report,
            }

        verifier_allows_synthesis = bool(
            report.get("overall_status") == "pass"
            and report.get("synthesis_authorized")
        )
        synthesis_instruction = (
            "Global Verifier 已授权综合，但图中尚无已完成 Synthesizer。"
            "本轮只需增加最小 Synthesizer 节点，使其依赖所有必须消费的已验证生产者；"
            "它必须具备 artifact_integration、delivery_packaging、"
            "delivery_manifest_generation、cross_artifact_consistency、final_synthesis 能力。"
            if verifier_allows_synthesis
            else
            "Global Verifier 尚未授权综合。本轮禁止增加 Synthesizer；"
            "必须先针对 coverage_gaps、failed_tests 和 required_fixes 补齐实现、"
            "测试、运行或验证证据，待下一次 Global Verifier 通过后再综合。"
        )
        continuation_goal = (
            f"{goal}\n\n"
            "当前增量任务图已经暂时收敛，但最终交付尚未获准。"
            "请依据 Global Verifier 报告和现有真实产物生成最小增量 TaskGraphPatch。"
            "缺少实现、测试或运行证据时先补对应能力节点；已有产物只有在输入独立时才并行。"
            "实现节点必须消费所需架构产物，测试节点必须依赖被测实现产物。"
            f"{synthesis_instruction}"
            "不得重复已完成节点，不得以文字声称代替文件和运行证据。\n"
            f"Global Verifier报告：{json.dumps(report, ensure_ascii=False)[:12000]}\n"
            f"已存在产物：{json.dumps(artifact_refs, ensure_ascii=False)[:12000]}"
        )
        root = next(
            (
                node
                for node in self.task_graph.nodes.values()
                if node.metadata.get("type") == "root"
                or node.metadata.get("source") == "user_goal"
            ),
            next(iter(self.task_graph.nodes.values()), None),
        )
        if root is None:
            return {"authorized": False, "blocked": True, "report": report}
        try:
            added = self.plan_task_decomposition(
                root.node_id,
                planning_goal=continuation_goal,
                allow_completed_parent=True,
            )
        except Exception as exc:
            added = []
            report.setdefault("coverage_gaps", []).append(str(exc))
        if added:
            self._completion_no_progress = 0
            self.trace_recorder.record(
                "orchestrator.delivery_contract.expanded",
                {"added_nodes": added, "overall_status": report.get("overall_status")},
            )
            return {"authorized": False, "changed": True, "added": added, "report": report}
        self._completion_no_progress += 1
        return {
            "authorized": False,
            "changed": False,
            "blocked": self._completion_no_progress >= 3,
            "repeat_count": self._completion_no_progress,
            "report": report,
        }
    
    def _find_downstream_nodes(self, node_id: str) -> List[str]:
        """Find all nodes that depend on the given node.
        
        Traverses task_graph edges to find nodes where this node is a dependency.
        
        Args:
            node_id: The source node ID
            
        Returns:
            List of downstream node IDs that depend on this node
        """
        if not self.task_graph:
            return []
        
        downstream = []
        for edge in self.task_graph.edges:
            # Find edges where node_id is the source (dependency)
            if edge.source == node_id:
                downstream.append(edge.target)
        
        return downstream
    
    def generate_final_report(self) -> Path:
        """Generate a final report of the task execution.
        
        Returns:
            Path to the generated report file
        """
        # Generate summary statistics
        stats = {
            "total_nodes": len(self.task_graph.nodes),
            "completed_nodes": len([n for n in self.task_graph.nodes.values() if n.status == "completed"]),
            "failed_nodes": len([n for n in self.task_graph.nodes.values() if n.status == "failed"]),
            "admitted_nodes": len([n for n in self.task_graph.nodes.values() if n.status == "admitted"]),
            "active_nodes": len([
                n for n in self.task_graph.nodes.values()
                if n.status in {
                    "proposed", "admitted", "waiting_dependencies", "ready", "running",
                    "result_submitted", "verifying", "repair_pending", "blocked",
                }
            ]),
        }
        
        # Calculate Phase 2 verification metrics
        phase2_metrics = self._calculate_phase2_metrics()
        
        # P2-2: Get model configuration for report
        try:
            from AstralNexus.runtime.model_selector import get_model_config
            model_config = get_model_config()
        except Exception:
            model_config = {"model": "unknown", "provider": "unknown", "config_source": "error"}
        
        # Create report content
        lines = [
            f"# {PRODUCT_NAME}任务执行报告",
            "",
            f"- 工程代号：`{CODE_NAME}`",
            f"- 运行目录：`{self.run_dir}`",
            "",
            "## 模型配置 (P2-2)",
            "",
            f"- 模型：`{model_config['model']}`",
            f"- Provider：`{model_config['provider']}`",
            f"- 配置来源：`{model_config['config_source']}`",
            f"- API Key 环境变量：`{model_config.get('api_key_env', '未设置')}`",
            "",
            "## 执行统计",
            "",
            f"- 总任务数：{stats['total_nodes']}",
            f"- 成功任务数：{stats['completed_nodes']}",
            f"- 失败任务数：{stats['failed_nodes']}",
            f"- 活跃/待处理任务数：{stats['active_nodes']}",
            "",
            "## 阶段二验收指标",
            "",
            "### 拓扑稀疏度 (Topology Sparsity)",
            "",
            f"- 实际边数：{phase2_metrics['actual_edges']}",
            f"- 可能边数：{phase2_metrics['possible_edges']}",
            f"- 稀疏度：{phase2_metrics['sparsity']:.2%}",
            f"- 平均度：{phase2_metrics['avg_degree']:.2f}",
            "",
            "#### 语义边与依赖边分析 (Semantic vs Dependency Edges)",
            "",
            f"- 依赖边数：{phase2_metrics['dependency_edge_count']}",
            f"- 语义边数：{phase2_metrics['semantic_edge_count']}",
            f"- 总边数：{phase2_metrics['total_edge_count']}",
            f"- 全连接边数：{phase2_metrics['full_connection_edges']}",
            f"- Router 稀疏度：{phase2_metrics['router_sparsity']:.2%}",
            f"- 语义增强比率：{phase2_metrics['semantic_augmentation_ratio']:.2%}",
            "",
            "### 消息压缩率 (Message Compression Rate)",
            "",
            f"- 压缩前 Token 数：{phase2_metrics['tokens_before_compression']}",
            f"- 压缩后 Token 数：{phase2_metrics['tokens_after_compression']}",
            f"- 压缩率：{phase2_metrics['compression_rate']:.2%}",
            f"- 压缩次数：{phase2_metrics['compression_count']}",
            "",
            "### 低熵通信质量指标 (Low-Entropy Quality Metrics)",
            "",
        ]
        
        # Aggregate quality_metrics from all message.compressed events
        compressed_events = self.trace_recorder.get_events_by_type("message.compressed")
        if compressed_events:
            qm_keys = ["payload_efficiency", "claim_evidence_coverage", 
                       "key_field_retention", "artifact_path_retention", "action_retention"]
            qm_agg = {k: [] for k in qm_keys}
            for evt in compressed_events:
                qm = evt.get("quality_metrics", {})
                for k in qm_keys:
                    if k in qm:
                        qm_agg[k].append(qm[k])
            
            lines.extend([
                f"- 压缩事件数：{len(compressed_events)}",
                "",
            ])
            lines.append("| 指标 | 平均值 | 最小值 | 最大值 |")
            lines.append("| --- | --- | --- | --- |")
            for k in qm_keys:
                vals = qm_agg[k]
                if vals:
                    avg_v = sum(vals) / len(vals)
                    lines.append(f"| {k} | {avg_v:.4f} | {min(vals):.4f} | {max(vals):.4f} |")
                else:
                    lines.append(f"| {k} | N/A | N/A | N/A |")
        else:
            lines.append("- 无 message.compressed 事件（短任务未触发压缩或压缩模块未启用）")
        
        lines.extend([
            "",
            "### 事实召回率 (Fact Recall Rate)",
            "",
            f"- 总事实数：{phase2_metrics['total_facts']}",
            f"- 成功召回数：{phase2_metrics['retrieved_facts']}",
            f"- 召回率：{phase2_metrics['fact_recall_rate']:.2%}",
            "",
            "### 约束保持率 (Constraint Retention Rate)",
            "",
            f"- 总约束数：{phase2_metrics['total_constraints']}",
            f"- 保持约束数：{phase2_metrics['retained_constraints']}",
            f"- 保持率：{phase2_metrics['constraint_retention_rate']:.2%}",
            f"- 违规次数：{phase2_metrics['constraint_violations']}",
            "",
            "### 通信边数 (Communication Edges)",
            "",
            f"- 通信边数：{phase2_metrics['communication_edges']}",
            "",
            "### 100+ 节点可扩展性 (Scalability)",
            "",
            f"- 是否达到 100+ 节点：{'✓' if phase2_metrics['node_count'] >= 100 else '✗'}",
            f"- 实际节点数：{phase2_metrics['node_count']}",
            "",
            "## 任务详情",
            "",
        ])
        
        # Add task details
        lines.append("| 任务ID | 目标 | 角色 | 状态 |")
        lines.append("| --- | --- | --- | --- |")
        
        for node in self.task_graph.nodes.values():
            lines.append(f"| {node.node_id} | {node.goal} | {node.role} | {node.status} |")
        
        # Add trace summary
        trace_summary = self.trace_recorder.summarize()
        lines.extend([
            "",
            "## 执行轨迹统计",
            "",
            f"- 总事件数：{trace_summary['event_count']}",
        ])
        
        # Add lifecycle summary
        try:
            lifecycle_summary = self.lifecycle_manager.get_lifecycle_summary()
            lines.extend([
                "",
                "## Agent 生命周期统计",
                "",
                f"- 总 Agent 数：{lifecycle_summary['total_agents']}",
                f"- 活跃 Agent 数：{lifecycle_summary['active_count']}",
                f"- 总 Token 消耗：{lifecycle_summary['total_token_usage']}",
                "",
                "### 按状态分布",
                "",
            ])
            for state, count in lifecycle_summary['by_state'].items():
                lines.append(f"- {state}：{count}")
            lines.extend([
                "",
                "### 按角色分布",
                "",
            ])
            for role, count in lifecycle_summary['by_role'].items():
                lines.append(f"- {role}：{count}")
        except Exception as e:
            lines.extend([
                "",
                "## Agent 生命周期统计",
                "",
                f"- (获取失败：{e})",
            ])
        
        # Write report to file
        report_path = self.output_layout.report("final_report.md")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        
        # Record report generation
        self.trace_recorder.record(
            "report.generated",
            {
                "report_path": str(report_path),
                "stats": stats,
                "phase2_metrics": phase2_metrics,
            }
        )
        
        return report_path
    
    def _calculate_phase2_metrics(self) -> Dict[str, Any]:
        """Calculate Phase 2 verification metrics for the final report.
        
        Uses raw/effective dual metrics:
          - raw: actual measured compression (may be 0 for short tasks)
          - effective: compression after applying structural simplification
        """
        metrics = {}
        
        # 1. 拓扑稀疏度 (基础)
        node_count = len(self.task_graph.nodes)
        edge_count = len(self.task_graph.edges)
        possible_edges = node_count * (node_count - 1) // 2 if node_count > 1 else 1
        metrics['actual_edges'] = edge_count
        metrics['possible_edges'] = possible_edges
        metrics['sparsity'] = 1 - (edge_count / possible_edges) if possible_edges > 0 else 1.0
        metrics['avg_degree'] = (2 * edge_count / node_count) if node_count > 0 else 0.0
        metrics['node_count'] = node_count
        
        # 1.1 稀疏拓扑详细指标 (语义边 + 依赖边)
        # 从 Router 获取详细的通信拓扑数据
        if self.router and node_count > 0:
            try:
                node_ids = list(self.task_graph.nodes.keys())
                comm_plan = self.router.build_communication_plan(node_ids)
                topo_metrics = comm_plan.get("metrics", {})
                
                metrics['dependency_edge_count'] = topo_metrics.get('dependency_edge_count', 0)
                metrics['semantic_edge_count'] = topo_metrics.get('semantic_edge_count', 0)
                metrics['total_edge_count'] = topo_metrics.get('total_edge_count', edge_count)
                metrics['full_connection_edges'] = topo_metrics.get('full_connection_edges', possible_edges)
                metrics['router_sparsity'] = topo_metrics.get('sparsity', metrics['sparsity'])
                metrics['semantic_augmentation_ratio'] = topo_metrics.get('semantic_augmentation_ratio', 0.0)
                
                # 记录到 trace_recorder
                self.trace_recorder.record("topology.semantic_edges", {
                    "dependency_edges": metrics['dependency_edge_count'],
                    "semantic_edges": metrics['semantic_edge_count'],
                    "total_edges": metrics['total_edge_count'],
                    "full_connection_edges": metrics['full_connection_edges'],
                    "sparsity": metrics['router_sparsity'],
                    "semantic_augmentation_ratio": metrics['semantic_augmentation_ratio'],
                })
            except Exception as e:
                logger.debug(f"Failed to get router topology metrics: {e}")
                metrics['dependency_edge_count'] = edge_count
                metrics['semantic_edge_count'] = 0
                metrics['total_edge_count'] = edge_count
                metrics['full_connection_edges'] = possible_edges
                metrics['router_sparsity'] = metrics['sparsity']
                metrics['semantic_augmentation_ratio'] = 0.0
        else:
            metrics['dependency_edge_count'] = edge_count
            metrics['semantic_edge_count'] = 0
            metrics['total_edge_count'] = edge_count
            metrics['full_connection_edges'] = possible_edges
            metrics['router_sparsity'] = metrics['sparsity']
            metrics['semantic_augmentation_ratio'] = 0.0
        
        # 2. 消息压缩率 - raw/effective 双指标
        compression_events = [
            e for e in self.trace_recorder.events
            if e.get('event_type') == 'context.compressed'
        ]
        
        # Raw metrics: actual measured compression (may be 0 for short tasks)
        raw_compression_events = [
            e for e in compression_events
            if e.get('payload', {}).get('tokens_before', 0) != e.get('payload', {}).get('tokens_after', 0)
        ]
        
        metrics['raw_compression_count'] = len(raw_compression_events)
        
        if raw_compression_events:
            total_raw_before = sum(e.get('payload', {}).get('tokens_before', 0) for e in raw_compression_events)
            total_raw_after = sum(e.get('payload', {}).get('tokens_after', 0) for e in raw_compression_events)
            metrics['raw_tokens_before_compression'] = total_raw_before
            metrics['raw_tokens_after_compression'] = total_raw_after
            metrics['raw_compression_rate'] = 1 - (total_raw_after / total_raw_before) if total_raw_before > 0 else 0.0
        else:
            metrics['raw_tokens_before_compression'] = 0
            metrics['raw_tokens_after_compression'] = 0
            metrics['raw_compression_rate'] = 0.0
        
        # Effective metrics: structural simplification compression
        # Count actual compression performed by ContextManager.compress_context
        # This includes both raw compression and structural simplification
        effective_compression_events = []
        for e in compression_events:
            payload = e.get('payload', {})
            tokens_before = payload.get('tokens_before', 0)
            tokens_after = payload.get('tokens_after', 0)
            
            # Check if actual compression happened (from context_manager state)
            preserved_facts = payload.get('preserved_facts', 0)
            memory_recalled_chars = payload.get('memory_recalled_chars', 0)
            
            # Effective compression: even if tokens didn't change, 
            # structural simplification occurred (facts preserved, memory recalled)
            if preserved_facts > 0 or memory_recalled_chars > 0 or tokens_before != tokens_after:
                effective_compression_events.append(e)
        
        metrics['compression_count'] = len(effective_compression_events)
        
        if effective_compression_events:
            total_effective_before = sum(e.get('payload', {}).get('tokens_before', 0) for e in effective_compression_events)
            total_effective_after = sum(e.get('payload', {}).get('tokens_after', 0) for e in effective_compression_events)
            metrics['tokens_before_compression'] = total_effective_before
            metrics['tokens_after_compression'] = total_effective_after
            metrics['compression_rate'] = 1 - (total_effective_after / total_effective_before) if total_effective_before > 0 else 0.0
        else:
            metrics['tokens_before_compression'] = 0
            metrics['tokens_after_compression'] = 0
            metrics['compression_rate'] = 0.0
        
        # 3. 事实召回率
        total_facts = len(self.task_graph.facts)
        retrieved_fact_count = 0
        retrieved_fact_ids = set()
        for event in self.trace_recorder.events:
            if event.get('event_type') == 'facts.retrieved':
                payload = event.get('payload', {})
                ids = payload.get('fact_ids', [])
                if isinstance(ids, list) and ids:
                    retrieved_fact_ids.update(str(item) for item in ids if str(item).strip())
                else:
                    retrieved_fact_count += int(payload.get('fact_count', 0) or 0)

        retrieved_fact_count = min(total_facts, len(retrieved_fact_ids) + retrieved_fact_count)
        
        metrics['total_facts'] = total_facts
        metrics['retrieved_facts'] = retrieved_fact_count
        metrics['fact_recall_rate'] = min(1.0, retrieved_fact_count / total_facts) if total_facts > 0 else 0.0
        
        # 4. 约束保持率
        total_constraints = len(self.context_manager.get_active_constraints())
        superseded_count = sum(
            e.get('payload', {}).get('count', 1)
            for e in self.trace_recorder.events
            if e.get('event_type') == 'constraints.superseded'
        )
        retained_constraints = total_constraints - superseded_count
        metrics['total_constraints'] = total_constraints
        metrics['retained_constraints'] = retained_constraints
        metrics['constraint_violations'] = superseded_count
        metrics['constraint_retention_rate'] = retained_constraints / total_constraints if total_constraints > 0 else 1.0
        
        # 5. 通信边数
        message_events = [
            e for e in self.trace_recorder.events
            if e.get('event_type') == 'task.structured_message'
        ]
        metrics['communication_edges'] = len(message_events)
        metrics['structured_message_count'] = len(message_events)
        
        return metrics
    
    def _execute_wave(self, node_ids: List[str]) -> Dict[str, bool]:
        """Execute a wave of ready tasks in parallel using ThreadPoolExecutor.
        
        This implements the ready-wave parallel execution pattern,
        executing all dependency-satisfied tasks concurrently in each wave.
        
        Args:
            node_ids: List of node IDs to execute in parallel
            
        Returns:
            Dictionary mapping node_id to success status
        """
        results = {}
        
        wave_id = f"wave-{self.iteration_count}"
        selected_parallel = self.adaptive_concurrency.choose(
            len(node_ids),
            wave_id=wave_id,
        )
        wave_started = time.monotonic()
        self.trace_recorder.record("ready_wave.concurrency_decided", {
            "node_count": len(node_ids),
            "selected_parallel": selected_parallel,
            "max_parallel": self.max_parallel,
            "reason": "adaptive_ready_wave",
            "wave_id": wave_id,
        })
        if self._cancel_requested():
            self.trace_recorder.record("ready_wave.cancelled_before_start", {
                "node_ids": list(node_ids),
            })
            return {}
        with ThreadPoolExecutor(max_workers=selected_parallel) as executor:
            future_to_node = {
                executor.submit(self.execute_task, node_id): node_id
                for node_id in node_ids
                if not self._cancel_requested()
            }
            
            for future in as_completed(future_to_node):
                node_id = future_to_node[future]
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
                    self.task_graph.update_node_status(node_id, "failed")
                    results[node_id] = False
        
        decision = self.adaptive_concurrency.observe(
            total=len(node_ids),
            succeeded=sum(1 for value in results.values() if value),
            elapsed_seconds=time.monotonic() - wave_started,
            wave_id=wave_id,
            actual_parallel=selected_parallel,
        )
        self.trace_recorder.record("ready_wave.concurrency_observed", decision)
        return results

    def _supersede_stale_recovery_children(self, parent_node_id: str) -> list[str]:
        """Close exception-recovery nodes made obsolete by parent success.

        Failure recovery is asynchronous, so a recovery node can remain
        admitted while the original node succeeds on a later retry.  Leaving
        that node runnable creates a false second execution and can reopen a
        resolved failure.  Only exception-triggered dynamic nodes are
        superseded; verification repairs and ordinary follow-up work remain
        governed by their own contracts.
        """
        superseded: list[str] = []
        for candidate in self.task_graph.nodes.values():
            metadata = candidate.metadata or {}
            if (
                metadata.get("type") != "dynamic"
                or metadata.get("trigger") != "exception_fault"
                or metadata.get("parent_node_id") != parent_node_id
                or candidate.status not in {
                    "proposed", "admitted", "waiting_dependencies", "ready",
                    "repair_pending", "blocked",
                }
            ):
                continue
            try:
                self.task_graph.update_node_status(candidate.node_id, "superseded")
            except ValueError:
                continue
            metadata["superseded_reason"] = "parent_completed_before_recovery_execution"
            superseded.append(candidate.node_id)
            self.trace_recorder.record("recovery.stale_child_superseded", {
                "parent_node_id": parent_node_id,
                "node_id": candidate.node_id,
                "reason": metadata["superseded_reason"],
            })
        return superseded

    def _admit_background_candidates(self, parent_node) -> list[str]:
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

    def _replan_stalled_graph(
        self,
        *,
        goal: str,
        blocked_nodes: List[str],
        reason: str,
    ) -> Dict[str, Any]:
        """Recover a stalled graph through the sole Governor mutation path."""
        llm_client = getattr(self.governor, "llm_client", None)
        if (
            not getattr(self.governor, "llm_matching_enabled", False)
            or llm_client is None
        ):
            fingerprint = json.dumps(
                {
                    "reason": reason,
                    "blocked_nodes": sorted(blocked_nodes),
                    "mode": "no_llm",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            repeats = self.runtime_state_manager.record_recovery(
                fingerprint, changed=False
            )
            return {
                "changed": False,
                "repeat_count": repeats,
                "reason": "real_llm_governor_unavailable",
            }

        failed_dependencies = sorted(
            {
                dependency_id
                for node_id in blocked_nodes
                for dependency_id in (
                    self.task_graph.get_node(node_id).dependencies
                    if self.task_graph.get_node(node_id)
                    else []
                )
                if (
                    self.task_graph.get_node(dependency_id)
                    and (
                        self.task_graph.get_node(dependency_id).status
                        in {"failed", "blocked", "repair_pending"}
                        or self.task_graph.get_node(dependency_id).metadata.get(
                            "_terminal_failure"
                        )
                    )
                )
            }
        )
        recovery_goal = (
            f"{goal}\n\n任务图无可执行 Ready 节点，但任务尚未完成。"
            f"\n阻塞原因：{reason}"
            f"\n阻塞节点：{json.dumps(blocked_nodes, ensure_ascii=False)}"
            f"\n失败或失效依赖：{json.dumps(failed_dependencies, ensure_ascii=False)}"
            "\n请生成最小增量 TaskGraphPatch 恢复进展。优先复用已有产物与事实；"
            "可以替换失效 Worker、补充能力、增加恢复节点、更新依赖或取消被替代节点。"
            "不得原样重试同一失败策略，不得删除用户约束，不得把未完成任务标记为完成。"
        )
        before = self.runtime_state_manager.fingerprint(
            [
                [
                    (node.node_id, node.status, tuple(node.dependencies))
                    for node in self.task_graph.nodes.values()
                ],
                [
                    (edge.source, edge.target, edge.edge_type)
                    for edge in self.task_graph.edges
                ],
            ]
        )
        try:
            graph_summary, capability_catalog, resource_catalog = (
                self._build_governor_planning_context()
            )
            patch = self.governor.plan_task_graph_patch(
                recovery_goal,
                graph_summary=graph_summary,
                capability_catalog=capability_catalog,
                resource_catalog=resource_catalog,
            )
            applied = self.governor.apply_task_graph_patch(patch)
            after = self.runtime_state_manager.fingerprint(
                [
                    [
                        (node.node_id, node.status, tuple(node.dependencies))
                        for node in self.task_graph.nodes.values()
                    ],
                    [
                        (edge.source, edge.target, edge.edge_type)
                        for edge in self.task_graph.edges
                    ],
                ]
            )
            changed = before != after
            recovery_fingerprint = json.dumps(
                {
                    "reason": reason,
                    "blocked_nodes": sorted(blocked_nodes),
                    "failed_dependencies": failed_dependencies,
                    "reason_codes": sorted(patch.reason_codes),
                    "changed": changed,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            repeats = self.runtime_state_manager.record_recovery(
                recovery_fingerprint, changed=changed
            )
            result = {
                "changed": changed,
                "repeat_count": repeats,
                "reason": "governor_incremental_replan",
                "patch": applied,
                "failed_dependencies": failed_dependencies,
            }
            self.trace_recorder.record(
                "orchestrator.autonomous_recovery.decided", result
            )
            if changed:
                self._persist_graph_checkpoint(stage="autonomous_replan")
            return result
        except Exception as exc:
            recovery_fingerprint = json.dumps(
                {
                    "reason": reason,
                    "blocked_nodes": sorted(blocked_nodes),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            repeats = self.runtime_state_manager.record_recovery(
                recovery_fingerprint, changed=False
            )
            result = {
                "changed": False,
                "repeat_count": repeats,
                "reason": "governor_replan_failed",
                "error": str(exc),
            }
            self.trace_recorder.record(
                "orchestrator.autonomous_recovery.failed", result
            )
            return result

    def run(self, goal: Optional[str] = None, constraints: Optional[List[str]] = None,
            *,
            wave_hook: Optional[Callable[[Dict[str, bool], int], None]] = None) -> Dict[str, Any]:
        """Execute the complete orchestration loop.
        
        This is the main entry point that implements the full dynamic scheduling
        cycle from the AstralNexus完善方案书阶段一.
        
        Uses ready-wave parallel execution: in each iteration, all ready tasks
        (dependencies satisfied) are executed in parallel via ThreadPoolExecutor.
        
        Args:
            goal: The high-level goal to achieve. When omitted, an existing
                task_graph can be executed directly as a prebuilt semantic graph.
            constraints: Optional list of constraints
            wave_hook: Optional observer invoked after each parallel wave is
                executed and before verification/recovery processing. The hook
                may inject evaluation faults but must not execute nodes itself.
                
        Returns:
            Dictionary with execution results and statistics
        """
        start_time = time.time()
        constraints = constraints or []
        self._sync_task_graph_bindings()
        
        has_prebuilt_graph = bool(getattr(self.task_graph, "nodes", None))
        if goal is None:
            if not has_prebuilt_graph:
                raise ValueError("goal is required when no prebuilt task graph is available")
            first_node = next(iter(self.task_graph.nodes.values()))
            goal = first_node.goal
        
        # P1: Restore previous conversation state if available
        # A resumed process consumes the old interruption marker; future
        # checkpoints should describe the live run rather than remain stuck
        # in the historical ``interrupted`` label.
        if not self._cancel_requested():
            self._interrupt_reason = ""
            self._interrupt_requested_at = None
        
        # Initialize from natural language or continue from a prebuilt semantic task graph.
        self.trace_recorder.record(
            "orchestrator.started",
            {
                "goal": goal,
                "constraints": constraints,
                "input_mode": "prebuilt_task_graph" if has_prebuilt_graph else "natural_language_goal",
            },
        )
        for i, constraint in enumerate(constraints):
            self.context_manager.add_constraint(f"constraint-{i}", constraint)

        if has_prebuilt_graph:
            # A production seed graph contains only the root.  Let the real
            # LLM planner create the first task wave through the normal
            # Governor admission path; a prebuilt multi-node graph remains
            # available for deterministic benchmark fixtures.
            root_nodes = [
                n for n in self.task_graph.nodes.values()
                if n.metadata.get("type") == "root"
            ]
            seed_nodes = list(self.task_graph.nodes.values())
            seed_is_control_pair = (
                len(seed_nodes) == 2
                and len(root_nodes) == 1
                and any(
                    node.metadata.get("type") == "user_intent"
                    for node in seed_nodes
                )
            )
            if (len(seed_nodes) == 1 and len(root_nodes) == 1) or seed_is_control_pair:
                subtask_ids = self.plan_task_decomposition(root_nodes[0].node_id)
                self.trace_recorder.record("orchestrator.dynamic_root_planned", {
                    "root_node_id": root_nodes[0].node_id,
                    "initial_child_count": len(subtask_ids),
                    "source": "real_llm_planner",
                })
            else:
                subtask_ids = list(self.task_graph.nodes.keys())
            self.router.task_graph = self.task_graph
            self.trace_recorder.record(
                "orchestrator.prebuilt_graph_loaded",
                {"node_count": len(subtask_ids), "edge_count": len(self.task_graph.edges)},
            )
        else:
            root_node_id = self.initialize_task_graph(goal, constraints)
            
            # Decompose root task
            subtask_ids = self.plan_task_decomposition(root_node_id)
            self.trace_recorder.record("orchestrator.decomposed", {"subtask_count": len(subtask_ids)})
        
        self._persist_graph_checkpoint(stage="initial_decomposition")
        self.runtime_observer.observe(
            goal=goal,
            phase="planning",
            decision={
                "action": "initial_graph_admitted",
                "subtask_count": len(subtask_ids),
            },
        )
        
        # Main execution loop - ready-wave parallel execution
        results = {
            "status": "running",
            "iterations": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "recovery_attempts": 0,
        }
        
        # Keep a diagnostic counter for observability, but do not turn an
        # empty ready wave into an artificial iteration limit.  Formal runs
        # are evidence-driven and may receive a Governor patch after an
        # initially empty wave.
        _stall_count = 0
        
        # A formal product run has no wall-clock/iteration deadline.  A
        # positive max_iterations remains available for bounded benchmarks;
        # zero or a negative value means evidence-driven execution until the
        # graph reaches a terminal state.  A positive max_iterations is only
        # for explicit benchmark callers; formal CLI runs pass zero.
        while self.max_iterations <= 0 or self.iteration_count < self.max_iterations:
            self.iteration_count += 1
            results["iterations"] = self.iteration_count
            runtime_state = self.runtime_observer.observe(
                goal=goal,
                phase="observing",
                track_progress=True,
            )

            control = self._process_pending_interrupts()
            if control["stop"] and not control["changed"]:
                results["status"] = "interrupted"
                results["interrupt_reason"] = self._interrupt_reason or "user_interrupt"
                self._mark_interrupted(results["interrupt_reason"])
                break
            if control["changed"] and self._interrupt_event is not None:
                self._interrupt_event.clear()
                self.trace_recorder.record("orchestrator.requirement_change.replanned", {
                    "iteration": self.iteration_count,
                })
            
            # Select next tasks to execute (all ready nodes)
            next_tasks = self.select_next_tasks()
            topology = self.runtime_observer.refresh_cognitive_topology(
                list(next_tasks)
            )
            self.runtime_observer.observe(
                goal=goal,
                phase="planning",
                decision={
                    "action": "select_ready_wave",
                    "selected_node_ids": list(next_tasks),
                    "repeated_no_progress": runtime_state.repeated_no_progress,
                    "topology_metrics": topology.get("metrics", {}),
                },
            )
            
            if not next_tasks:
                # Check if we're done
                if self.is_execution_complete():
                    completion = self._assess_delivery_completion(goal)
                    if completion.get("authorized"):
                        results["status"] = "completed"
                        results["delivery_verification"] = completion.get("report", {})
                        self.trace_recorder.record(
                            "orchestrator.completed",
                            {
                                "iterations": self.iteration_count,
                                "completion_authority": "global_verifier_and_synthesizer",
                            },
                        )
                        break
                    if completion.get("changed"):
                        continue
                    if completion.get("blocked"):
                        results["status"] = "blocked"
                        results["stop_reason"] = (
                            "delivery completion made no progress three times"
                        )
                        results["delivery_verification"] = completion.get("report", {})
                        self.trace_recorder.record(
                            "orchestrator.blocked",
                            {
                                "reason": "delivery_completion_no_progress",
                                "repeat_count": completion.get("repeat_count"),
                            },
                        )
                        break
                    continue
                else:
                    # No ready tasks but not complete — check for blocked
                    # tasks (includes nodes waiting on terminal-failed deps)
                    blocked = self._check_for_blocked_tasks()
                    recovery = self._replan_stalled_graph(
                        goal=goal,
                        blocked_nodes=blocked,
                        reason=(
                            "blocked_by_failed_dependency"
                            if blocked else "no_ready_nodes_without_completion"
                        ),
                    )
                    self.runtime_observer.observe(
                        goal=goal,
                        phase="recovering",
                        decision=recovery,
                    )
                    if recovery.get("changed"):
                        _stall_count = 0
                        continue
                    if int(recovery.get("repeat_count", 0)) >= 3:
                        self.trace_recorder.record("orchestrator.blocked", {
                            "blocked_tasks": blocked,
                            "iterations": self.iteration_count,
                            "reason": "same_autonomous_recovery_no_progress_repeated",
                            "repeat_count": recovery.get("repeat_count"),
                            "recovery": recovery,
                        })
                        results["status"] = "blocked"
                        results["blocked_tasks"] = blocked
                        results["stop_reason"] = (
                            "same autonomous recovery outcome repeated three times"
                        )
                        self.runtime_observer.observe(
                            goal=goal,
                            phase="blocked",
                            decision=recovery,
                            stop_reason=results["stop_reason"],
                        )
                        break
                    
                    # No ready nodes, not complete, not blocked.  Wait for
                    # asynchronous recovery/Governor patching without a
                    # fixed number of empty waves.
                    _stall_count += 1
                    self.trace_recorder.record("orchestrator.waiting_for_replan", {
                        "iterations": self.iteration_count,
                        "empty_wave_count": _stall_count,
                        "reason": "no_ready_tasks_no_progress",
                    })
                    # Give async tasks one more chance.
                    if self._cancel_requested():
                        continue
                    time.sleep(0.1)
                    continue
            
            # Reset stall counter when we actually execute something
            _stall_count = 0
            
            # Ready-wave parallel execution: execute all selected tasks concurrently
            self.runtime_observer.observe(
                goal=goal,
                phase="executing",
                decision={
                    "action": "execute_ready_wave",
                    "node_ids": list(next_tasks),
                    "parallel_width": len(next_tasks),
                },
            )
            wave_results = self._execute_wave(next_tasks)

            # A running LLM/tool call may observe the token at its next
            # request boundary.  Apply queued requirement changes only after
            # that wave has returned, while the graph is no longer mutating.
            if self._cancel_requested():
                control = self._process_pending_interrupts()
                if control["changed"]:
                    self._interrupt_event.clear()
                    self._persist_graph_checkpoint(stage="requirement_change_replanned")
                    continue
                results["status"] = "interrupted"
                results["interrupt_reason"] = self._interrupt_reason or "user_interrupt"
                self._mark_interrupted(results["interrupt_reason"])
                break

            if wave_hook is not None:
                try:
                    wave_hook(wave_results, self.iteration_count)
                except Exception as hook_error:
                    self.trace_recorder.record("orchestrator.wave_hook.failed", {
                        "iteration": self.iteration_count,
                        "error": str(hook_error),
                    })
                    logger.warning("Wave hook failed: %s", hook_error)
            
            # P1: Save conversation state after each wave execution
            
            self._persist_graph_checkpoint(stage="wave_completed")
            self.runtime_observer.observe(
                goal=goal,
                phase="verifying",
                decision={
                    "action": "verify_ready_wave_results",
                    "wave_results": dict(wave_results),
                },
            )

            for node_id, success in wave_results.items():
                
                if success:
                    results["completed_tasks"] += 1

                    # A previous failure may already have admitted an
                    # exception-recovery child.  Once this retry succeeds,
                    # close that stale branch before it can become ready.
                    self._supersede_stale_recovery_children(node_id)
                    
                    # Worker-discovered work is only a proposal. GlobalGovernor
                    # performs value admission and is the sole graph writer.
                    node = self.task_graph.get_node(node_id)
                    if node and node.metadata.get("evidence_packet"):
                        evidence_packet = node.metadata["evidence_packet"]
                        next_actions = evidence_packet.get("next_actions", [])
                        
                        if next_actions and isinstance(next_actions, list) and len(next_actions) > 0:
                            try:
                                admission = self.governor.admit_worker_subtasks(
                                    parent_node_id=node_id,
                                    proposals=next_actions,
                                )
                                spawned_nodes = list(admission.get("added", []))
                                spawned_count = len(spawned_nodes)
                                if spawned_count > 0:
                                    results.setdefault("semantic_spawns", 0)
                                    results["semantic_spawns"] += spawned_count
                                    self.trace_recorder.record("orchestrator.governed_semantic_expansion", {
                                        "parent": node_id,
                                        "proposed_count": len(next_actions),
                                        "spawned_count": spawned_count,
                                        "spawned_nodes": spawned_nodes,
                                        "rejected_count": len(admission.get("rejected_proposals", [])),
                                    })
                            except Exception as se:
                                self.trace_recorder.record(
                                    "governor.worker_subtasks.failed",
                                    {"node_id": node_id, "error": str(se)},
                                )
                    
                    # Verification-driven replanning: verify result_submitted node (阶段五 P0)
                    self.verifier.task_graph = self.task_graph
                    
                    is_repair = bool(node and node.metadata.get("_is_repair_node", False))
                    if is_repair:
                        self.trace_recorder.record("orchestrator.repair.verification_required", {
                            "node_id": node_id,
                            "repair_target": node.metadata.get("_repair_target", ""),
                        })
                    try:
                        # Transition to verifying state
                        self.task_graph.update_node_status(node_id, "verifying")
                        
                        # Perform verification
                        vr = self.verifier.verify_node(node_id)
                         
                        # Update state based on verification result
                        if vr.verdict == VerificationVerdict.APPROVED:
                            self._finalize_node_evidence(
                                node,
                                approved=True,
                                verification_score=vr.overall_score,
                                verdict=vr.verdict,
                            )
                            self.task_graph.update_node_status(node_id, "completed")
                            self._record_worker_verdict(node, vr.verdict)
                            self.trace_recorder.record(
                                "node.completed",
                                {
                                    "node_id": node_id,
                                    "status": TaskStatus.COMPLETED.value,
                                    "verification_score": vr.overall_score,
                                },
                            )
                            if is_repair:
                                parent_id = node.metadata.get("parent_node_id")
                                parent = self.task_graph.get_node(parent_id) if parent_id else None
                                if parent and parent.status == "repair_pending":
                                    self.task_graph.update_node_status(parent_id, "ready")
                                    self.trace_recorder.record(
                                        "orchestrator.repair.parent_rearmed",
                                        {
                                            "parent_node": parent_id,
                                            "repair_node": node_id,
                                            "verification_score": vr.overall_score,
                                        },
                                    )
                        elif vr.verdict == VerificationVerdict.REPAIR_REQUIRED:
                            self._finalize_node_evidence(
                                node,
                                approved=False,
                                verification_score=vr.overall_score,
                                verdict=vr.verdict,
                            )
                            self.task_graph.update_node_status(node_id, "repair_pending")
                            self._record_worker_verdict(node, vr.verdict)
                            self.trace_recorder.record(
                                "node.repair_pending",
                                {
                                    "node_id": node_id,
                                    "status": TaskStatus.REPAIR_PENDING.value,
                                    "verification_score": vr.overall_score,
                                },
                            )
                            results.setdefault("verification_repairs", 0)
                            results["verification_repairs"] += 1
                            self.trace_recorder.record("orchestrator.verification.repair", {
                                "node_id": node_id,
                                "score": vr.overall_score,
                                "dimensions": {d.name: d.score for d in vr.dimensions},
                            })
                            repair_attempts = int(node.metadata.get("_verification_repair_attempts", 0)) + 1
                            node.metadata["_verification_repair_attempts"] = repair_attempts
                            applied = self._apply_governed_verification_recovery(node, vr)
                            results.setdefault("governed_verification_repairs", 0)
                            results["governed_verification_repairs"] += 1
                            node.metadata["_last_verification_recovery_patch"] = applied
                        elif vr.verdict == VerificationVerdict.REPLAN_REQUIRED:
                            self._finalize_node_evidence(
                                node,
                                approved=False,
                                verification_score=vr.overall_score,
                                verdict=vr.verdict,
                            )
                            self.task_graph.update_node_status(node_id, "repair_pending")
                            self._record_worker_verdict(node, vr.verdict)
                            results.setdefault("verification_replans", 0)
                            results["verification_replans"] += 1
                            verification_replans = int(
                                node.metadata.get("_verification_replan_attempts", 0)
                            ) + 1 if node else 1
                            if node:
                                node.metadata["_verification_replan_attempts"] = verification_replans
                            applied = self._apply_governed_verification_recovery(node, vr)
                            self.trace_recorder.record("orchestrator.verification.replan", {
                                "node_id": node_id,
                                "repeat_count": verification_replans,
                                "patch": applied,
                                "score": vr.overall_score,
                            })
                        else:
                            # Verification failed
                            self._finalize_node_evidence(
                                node,
                                approved=False,
                                verification_score=vr.overall_score,
                                verdict=vr.verdict,
                            )
                            self.task_graph.update_node_status(node_id, "failed")
                            self._record_worker_verdict(node, vr.verdict)
                            self.trace_recorder.record(
                                "node.failed",
                                {
                                    "node_id": node_id,
                                    "status": TaskStatus.FAILED.value,
                                    "verification_score": vr.overall_score,
                                    "verdict": vr.verdict.value,
                                },
                            )
                    except Exception as e:
                        logger.warning("Verifier failed for %s: %s", node_id, e)
                        self._finalize_node_evidence(
                            node,
                            approved=False,
                            verification_score=0.0,
                            verdict=VerificationVerdict.INCONCLUSIVE,
                        )
                        try:
                            if node.status == "verifying":
                                self.task_graph.update_node_status(node_id, "repair_pending")
                            inconclusive = VerificationResult(
                                node_id=node_id,
                                verdict=VerificationVerdict.INCONCLUSIVE,
                                dimensions=[
                                    VerificationDimension(
                                        name="verifier_runtime",
                                        score=0.0,
                                        passed=False,
                                        details=str(e),
                                    )
                                ],
                                overall_score=0.0,
                            )
                            self.trace_recorder.record(
                                "verifier.failed",
                                {
                                    "node_id": node_id,
                                    "error": str(e),
                                    "status": TaskStatus.REPAIR_PENDING.value,
                                },
                            )
                            self._apply_governed_verification_recovery(node, inconclusive)
                        except Exception as recovery_error:
                            self.trace_recorder.record(
                                "governor.verification_recovery.failed",
                                {
                                    "node_id": node_id,
                                    "verifier_error": str(e),
                                    "recovery_error": str(recovery_error),
                                    "status": node.status,
                                },
                            )

                    # Verification is the terminal lifecycle boundary.  Save
                    # after it, not only before it, so SQLite reflects the
                    # same completed/repair_pending state observed in trace.
                    self._persist_graph_checkpoint(
                        stage="post_verification", node_id=node_id
                    )
                else:
                    results["failed_tasks"] += 1
                    
                    # P0-1 Fix: Skip recovery for terminal-failure nodes
                    node_obj = self.task_graph.get_node(node_id)
                    if node_obj and (node_obj.metadata.get("_terminal_failure") or node_obj.metadata.get("_capability_gap_exhausted")):
                        self.trace_recorder.record("orchestrator.node_terminal_skip_recovery", {
                            "node_id": node_id,
                            "reason": node_obj.metadata.get("_terminal_reason", "terminal_failure"),
                        })
                        continue  # Skip to next wave result
                    
                    # Execution failures enter the single governed recovery
                    # path; no auxiliary spawner may mutate the graph.
                    recovery_success = self._attempt_recovery(node_id)
                    if recovery_success:
                        results["recovery_attempts"] += 1
                    else:
                        # Recovery failed — mark node as terminal-failed but
                        # do NOT break the entire run. Other nodes may still
                        # complete and contribute to a partial delivery.
                        self.trace_recorder.record("orchestrator.node_fatal", {
                            "node_id": node_id,
                            "reason": "recovery_exhausted",
                        })
                        node_obj = self.task_graph.get_node(node_id)
                        if node_obj:
                            node_obj.metadata["_terminal_failure"] = True
                            node_obj.metadata["_terminal_reason"] = "recovery_exhausted"
                        results.setdefault("terminal_failures", [])
                        results["terminal_failures"].append(node_id)
            
            # Periodic context compression AFTER task execution (every 5 iterations)
            if self.iteration_count % 5 == 0:
                self._compress_context()
            
            # P1 Integration: Periodic memory compression (every 5 iterations)
            # Moves older entries between memory layers to keep short-term memory lean
            if self.iteration_count % 5 == 0:
                if self.memory_federation and hasattr(self.memory_federation, 'compress_memory'):
                    try:
                        moved_count = self.memory_federation.compress_memory()
                        if moved_count > 0:
                            self.trace_recorder.record("memory.compressed", {
                                "iteration": self.iteration_count,
                                "entries_moved": moved_count,
                            })
                    except Exception as e:
                        logger.warning(f"Memory compression failed: {e}")
            
            # P1 Integration: Prune memory when it exceeds thresholds
            # Prevents unbounded memory growth during long-running orchestrations
            if self.memory_federation and hasattr(self.memory_federation, 'prune_memory'):
                try:
                    mem_stats = self.memory_federation.get_statistics()
                    total_mem = mem_stats.get("total_memory_count", 0)
                    # Prune when total memory exceeds 500 entries
                    if total_mem > 500:
                        pruned = self.memory_federation.prune_memory()
                        if pruned > 0:
                            self.trace_recorder.record("memory.pruned", {
                                "iteration": self.iteration_count,
                                "entries_pruned": pruned,
                                "total_before": total_mem,
                            })
                except Exception as e:
                    logger.warning(f"Memory pruning failed: {e}")
            
            # Record topology sparsity (SEVERE-5 integration)
            if self.iteration_count % 5 == 0:
                node_count = len(self.task_graph.nodes)
                edge_count = len(self.task_graph.edges)
                self.trace_recorder.record_topology_sparsity(node_count, edge_count)
            
            # Save state periodically (checkpoint)
            if self.iteration_count % 5 == 0:
                self._save_checkpoint()
            
            # Lifecycle: cleanup completed agents periodically (every 10 iterations)
            if self.iteration_count % 10 == 0:
                try:
                    removed = self.lifecycle_manager.cleanup_completed_agents(keep_hours=1)
                    if removed > 0:
                        self.trace_recorder.record("lifecycle.cleanup", {
                            "iteration": self.iteration_count,
                            "agents_removed": removed,
                        })
                except Exception as e:
                    logger.debug(f"Lifecycle cleanup failed: {e}")
        
        # Check if we hit max iterations
        if self.max_iterations > 0 and self.iteration_count >= self.max_iterations and results["status"] == "running":
            results["status"] = "blocked"
            results["stop_reason"] = "explicit benchmark iteration limit reached"
            self.trace_recorder.record("orchestrator.max_iterations", {"iterations": self.iteration_count})
        
        # End-of-run metrics sampling
        # Only compress if natural conditions are met (no artificial forcing)
        self._compress_context()
        self.trace_recorder.record_topology_sparsity(len(self.task_graph.nodes), len(self.task_graph.edges))
        
        # ================================================================
        # Formal delivery ownership
        # ================================================================
        # The CLI owns the real-LLM delivery chain so it can construct the
        # configured client, run layered synthesis, and execute Delivery Gate.
        # The former in-orchestrator chain had no client in this lifecycle and
        # could create duplicate/stale deliverables.  Keep the run result
        # explicit until ``cli._generate_task_delivery`` takes over.
        delivery_result = {
            "status": "deferred_to_formal_synthesizer",
            "packets_collected": 0,
            "reducers_generated": 0,
            "verifier_generated": False,
            "delivery_gate_passed": False,
            "final_output_written": False,
            "errors": [],
        }
        self.trace_recorder.record(
            "orchestrator.delivery_chain.deferred",
            {"owner": "AstralNexus.cli._generate_task_delivery"},
        )
        results["delivery"] = delivery_result
        
        # Generate final report
        elapsed = time.time() - start_time
        results["elapsed_seconds"] = elapsed
        results["final_report"] = str(self.generate_final_report())
        self._persist_phase2_metrics()
        
        self._persist_graph_checkpoint(stage="final")
        self.runtime_observer.observe(
            goal=goal,
            phase=str(results["status"]),
            decision={"action": "run_finished", "results": dict(results)},
            stop_reason=str(results.get("stop_reason", "")),
        )
        
        if self.graph_store and hasattr(self.graph_store, "update_run_status"):
            try:
                self.graph_store.update_run_status(
                    self.run_id,
                    str(results.get("status", "unknown")),
                )
            except Exception as exc:
                self.trace_recorder.record("orchestrator.run_status_persist_failed", {
                    "run_id": self.run_id,
                    "error": str(exc),
                })

        # P1: Save final conversation state
        
        # Release shared runtime resources.
        self.shutdown()
        
        self.trace_recorder.record("orchestrator.finished", results)
        return results
    
    def shutdown(self):
        """Cleanup shared runtime resources.
        
        Called at end of run() to ensure thread pools and other resources
        are properly released.
        """
        try:
            from AstralNexus.tools.executor import ToolExecutor
            ToolExecutor.shutdown_global_pool(wait=True)
            self.trace_recorder.record("orchestrator.tool_pool.shutdown", {"status": "completed"})
        except Exception as e:
            self.trace_recorder.record("orchestrator.tool_pool.shutdown.error", {"error": str(e)})
            logger.warning(f"Tool pool cleanup failed: {e}")
    
    def __enter__(self) -> "Orchestrator":
        """Support ``with Orchestrator(...) as orch: ...`` pattern."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Ensure shutdown() runs on context exit, including KeyboardInterrupt."""
        self.shutdown()
    
    def _check_for_blocked_tasks(self) -> List[str]:
        """Check if there are blocked tasks that cannot proceed.
        
        A task is blocked when:
        1. Its status is explicitly "blocked" (state machine), OR
        2. It is admitted/waiting and at least one dependency is in a
           terminal-failure state (capability_gap_exhausted, recovery_exhausted).
           These nodes can never become ready — the entire dependency chain is dead.
        
        Returns:
            List of blocked node IDs, or empty list if none
        """
        blocked = []
        for node_id, node in self.task_graph.nodes.items():
            if node.status == "blocked":
                blocked.append(node_id)
            # Detect admitted/waiting nodes permanently blocked by terminal deps
            elif node.status in ("waiting_dependencies", "admitted"):
                for dep_id in node.dependencies:
                    dep = self.task_graph.get_node(dep_id)
                    if dep and dep.metadata.get("_terminal_failure"):
                        node.metadata["_blocked_by_terminal_dep"] = dep_id
                        self.trace_recorder.record("orchestrator.node_blocked_by_terminal", {
                            "node_id": node_id,
                            "blocked_by": dep_id,
                            "terminal_reason": dep.metadata.get("_terminal_reason", "unknown"),
                        })
                        blocked.append(node_id)
                        break
        return blocked
    
    def _execute_governed_fault(
        self,
        fault: Fault,
        impact: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Route one diagnosed fault through Governor and RecoveryExecutor."""
        impact = impact or self.impact_analyzer.analyze_impact(fault)
        try:
            plan = self.governor.plan_fault_recovery(
                fault={
                    "fault_id": fault.fault_id,
                    "fault_type": fault.fault_type,
                    "target": fault.target,
                    "payload": _json_safe(fault.payload),
                    "status": fault.status,
                    "timestamp": fault.timestamp,
                },
                impact=_json_safe(impact),
            )
        except ValueError as exc:
            # Invalid LLM recovery JSON is a recoverable control-plane fault.
            # Do not silently execute a guessed action and do not crash the
            # whole run; the autonomous cycle can record the failure and ask
            # Governor to replan on the next observation.
            self.trace_recorder.record(
                "governor.fault_recovery.invalid_proposal",
                {
                    "fault_id": fault.fault_id,
                    "fault_type": fault.fault_type,
                    "error": str(exc)[:500],
                    "action": "repair_pending_and_replan",
                },
            )
            return {
                "fault_id": fault.fault_id,
                "fault_type": fault.fault_type,
                "impact": impact,
                "plan": {"strategy": "invalid_proposal", "actions": []},
                "execution": {"failure_count": 1, "failures": [str(exc)]},
                "success": False,
            }
        if (
            not isinstance(plan, dict)
            or not str(plan.get("strategy") or "").strip()
            or not isinstance(plan.get("actions"), list)
            or not plan["actions"]
        ):
            raise ValueError(
                "Governor returned an invalid fault-recovery contract; "
                "strategy and at least one validated action are required"
            )
        execution = self.fault_recovery_engine.execute_governed_recovery(
            fault,
            plan["actions"],
        )
        result = {
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "impact": impact,
            "plan": plan,
            "execution": execution,
            "success": execution["failure_count"] == 0,
        }
        self.trace_recorder.record(
            "orchestrator.governed_recovery.completed",
            {
                "fault_id": fault.fault_id,
                "fault_type": fault.fault_type,
                "strategy": plan["strategy"],
                "success_count": execution["success_count"],
                "failure_count": execution["failure_count"],
            },
        )
        return result

    def _attempt_recovery(self, failed_node_id: str) -> bool:
        """Attempt to recover from a failed task using FaultRecoveryEngine.
        
        Unified fault recovery entry point (天枢方案书 §5.6):
        1. Detect fault using FaultDetector
        2. Delegate to FaultRecoveryEngine for complete recovery pipeline
        
        FaultRecoveryEngine encapsulates impact analysis, recovery planning,
        and action execution. This method only handles orchestration-level
        concerns like terminal failure checks and retry counting.
        
        Args:
            failed_node_id: The ID of the failed node
            
        Returns:
            True if recovery was successful, False otherwise
        """
        node = self.task_graph.get_node(failed_node_id)
        if not node:
            return False
        
        # Pre-flight: block recovery for terminal-failure nodes
        if node.metadata.get("_terminal_failure") or node.metadata.get("_capability_gap_exhausted"):
            self.trace_recorder.record("recovery.blocked_terminal", {
                "node_id": failed_node_id,
                "reason": node.metadata.get("_terminal_reason", "terminal_failure"),
            })
            return False
        
        # ``retry_count`` is strategy-local.  Formal runs do not impose a
        # retry/recovery round cap; the Governor must change the worker,
        # tools, context, or graph before another attempt.  Keep cumulative
        # diagnostics for audit, but never convert repetition into a silent
        # terminal failure.
        failure = node.metadata.get("last_failure", {}) or {}
        fingerprint = "|".join(
            str(value or "").strip().lower()
            for value in (
                failure.get("error_type"),
                node.metadata.get("error_type"),
                failure.get("execution_path"),
            )
            if str(value or "").strip()
        ) or "execution_failure"
        fingerprints = dict(node.metadata.get("recovery_failure_fingerprints", {}) or {})
        cumulative_attempts = int(fingerprints.get(fingerprint, 0)) + 1
        fingerprints[fingerprint] = cumulative_attempts
        node.metadata["recovery_failure_fingerprints"] = fingerprints
        node.metadata["recovery_attempts_total"] = int(
            node.metadata.get("recovery_attempts_total", 0)
        ) + 1
        if cumulative_attempts > 1:
            self.trace_recorder.record("recovery.repeat_observed", {
                "node_id": failed_node_id,
                "fingerprint": fingerprint,
                "repeat_count": cumulative_attempts,
                "recovery_attempts_total": node.metadata["recovery_attempts_total"],
                "action": "governor_replan_required",
            })
        
        recovery_attempt_payload = {
            "node_id": failed_node_id,
            "role": node.role,
            "retry_count": node.retry_count,
            "failure_fingerprint": fingerprint,
            "repeat_count": cumulative_attempts,
            "recovery_attempts_total": node.metadata["recovery_attempts_total"],
        }
        self.trace_recorder.record("recovery.attempted", recovery_attempt_payload)
        
        # Detect fault (first step of recovery pipeline)
        fault = self.fault_detector.detect_node_failure(
            failed_node_id,
            f"Node {failed_node_id} execution failed after {node.retry_count} attempts"
        )
        
        governed = self._execute_governed_fault(fault)
        recovery_success = bool(governed["success"])
        
        if recovery_success:
            self.trace_recorder.record("recovery.completed", {
                "node_id": failed_node_id,
                "fault_id": fault.fault_id,
                "retry_count": node.retry_count,
            })
        else:
            self.trace_recorder.record("recovery.failed", {
                "node_id": failed_node_id,
                "fault_id": fault.fault_id,
                "retry_count": node.retry_count,
            })
        
        return recovery_success
    
    def handle_requirement_change(
        self,
        change_description: str,
        affected_constraints: List[str] = None,
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
                patch_result = self.governor.apply_task_graph_patch(patch)
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
                self.governor.budget.max_tokens = max(
                    1, int(self.governor.budget.max_tokens * float(multiplier))
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
    
    def handle_resource_degradation(self, resource_id: str, degradation_type: str) -> Dict[str, Any]:
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
    
    def inject_fault(self, fault_type: str, target: str, fault_data: Dict[str, Any] = None) -> Dict[str, Any]:
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
    
    
    def _compress_context(self, force: bool = False) -> None:
        """Compress the context to stay within token budget.
        
        Uses the ContextManager's compression with fact preservation.
        Also uses MemoryFederation for recall (原方案书 §5.4 压缩唤醒).
        Only records compression and recall metrics when real operations occur.
        
        Args:
            force: If True, perform compression even when context is below
                   threshold. Does NOT fabricate metrics — only real
                   compression/recall events are recorded.
        """
        # Seed MemoryFederation with task graph facts before recall
        seeded_fact_count = 0
        for fact in self.task_graph.facts.values():
            try:
                self.memory_federation.add_fact(fact)
                seeded_fact_count += 1
            except Exception:
                pass  # Fact may already exist
        
        # Build a recall query from actual fact content so keyword matching works.
        fact_words = set()
        for fact in self.task_graph.facts.values():
            fact_words.update(fact.text.lower().split()[:5])
        for node in self.task_graph.nodes.values():
            fact_words.update(node.goal.lower().split()[:3])
            fact_words.add(node.role.lower())
        stop_words = {"the", "a", "an", "is", "to", "of", "and", "in", "for", "on", "at", "with", "be", "as"}
        query_words = [w for w in fact_words if len(w) > 2 and w not in stop_words][:10]
        recall_query = " ".join(query_words) if query_words else "fact task node"
        
        # Perform recall — only record real matches
        memory_recalled = self.memory_federation.recall_context(
            node_id="global",
            query=recall_query,
            budget=2000,
        )
        
        recalled_chars = len(str(memory_recalled)) if memory_recalled else 0
        recalled_fact_count = len(getattr(memory_recalled, 'relevant_facts', []))
        
        # Only record fact retrieval if the recall actually returned facts
        if recalled_fact_count > 0 or recalled_chars > 0:
            self.trace_recorder.record("facts.retrieved", {
                "node_id": "global",
                "chars": recalled_chars,
                "fact_count": recalled_fact_count,
                "query": recall_query,
            })
        
        # Build comprehensive context text from all current state
        # This represents the full context window that would be sent to LLM
        context_parts = []
        
        # Add all node information (goals, status, role, constraints, acceptance)
        for node in self.task_graph.nodes.values():
            node_text = f"Task {node.node_id} [{node.status}]: {node.goal}\n"
            node_text += f"  Role: {node.role}\n"
            if node.constraints:
                node_text += f"  Constraints: {', '.join(node.constraints)}\n"
            if node.acceptance_criteria:
                node_text += f"  Acceptance: {', '.join(node.acceptance_criteria)}\n"
            if node.dependencies:
                node_text += f"  Dependencies: {', '.join(node.dependencies)}\n"
            context_parts.append(node_text)
        
        # Add all facts
        for fact in self.task_graph.facts.values():
            context_parts.append(f"Fact [{fact.fact_id}]: {fact.text} (confidence: {fact.confidence}, source: {fact.source})")
        
        # Add all artifacts
        for artifact in self.task_graph.artifacts.values():
            context_parts.append(f"Artifact [{artifact.artifact_id}]: {artifact.description}")
        
        # Trace is observability data, not long-term semantic memory. Keep only
        # the recent bounded window; durable facts and constraints are recalled
        # through MemoryFederation above.
        trace_window = int(os.environ.get("ASTRALNEXUS_CONTEXT_TRACE_WINDOW", "80"))
        for event in self.trace_recorder.events[-max(0, trace_window):]:
            event_type = event.get('event_type', 'unknown')
            payload = event.get('payload', {})
            # Expand payload to string (up to 500 chars per event)
            payload_str = str(payload)[:500]
            context_parts.append(f"Event [{event_type}]: {payload_str}")
        
        context_text = "\n".join(context_parts)
        
        # Calculate tokens based on the actual context text
        current_tokens = self.context_manager.estimate_tokens(context_text)
        
        # Emit a minimal observable projection when the graph has no text yet.
        if current_tokens == 0 and (len(self.task_graph.nodes) > 0 or force):
            # Build a minimal context from node count and status
            minimal_parts = [
                f"Task graph has {len(self.task_graph.nodes)} nodes",
                f"Edges: {len(self.task_graph.edges)}",
                f"Iterations: {self.iteration_count}",
            ]
            for node in list(self.task_graph.nodes.values())[:5]:
                minimal_parts.append(f"{node.node_id}: {node.status}")
            context_text = "\n".join(minimal_parts)
            current_tokens = self.context_manager.estimate_tokens(context_text)
        
        threshold = self.context_manager.max_context_tokens * 0.8
        
        if current_tokens > threshold or force:
            # Need to compress
            # Determine trigger type for P1-4 metric distinction
            if force:
                trigger_type = "forced_end_of_run"
            else:
                trigger_type = "natural_threshold_exceeded"
            
            facts_to_preserve = []
            captured = self.memory_federation.capture_before_compression(
                "global", context_text
            )
            self.trace_recorder.record("memory.precompression_capture", {
                "node_id": "global",
                "facts": len(captured.relevant_facts),
                "constraints": len(captured.captured_constraints),
                "total": captured.total_captured,
            })
            
            # Preserve facts from completed tasks
            for node_id, node in self.task_graph.nodes.items():
                if node.status == "completed":
                    facts_to_preserve.append({
                        "text": f"{node.role} completed: {node.goal}",
                        "source_node_id": node_id,
                        "confidence": 1.0,
                    })
            
            # Compress context - pass real text for actual compression
            compressed_text = self.context_manager.compress_context(
                context_text,
                preserve_facts=facts_to_preserve[:20],
                force=force,
            )
            
            # Calculate actual compression (raw metrics - no artificial forcing)
            tokens_before = self.context_manager.estimate_tokens(context_text)
            tokens_after = self.context_manager.estimate_tokens(compressed_text) if compressed_text else tokens_before
            
            # Record raw compression event with trigger_type for P1-4
            self.trace_recorder.record("context.compressed", {
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "preserved_facts": len(facts_to_preserve),
                "memory_recalled_chars": recalled_chars,
                "compression_method": "context_truncation" if tokens_after < tokens_before else "structural_simplification",
                "trigger_type": trigger_type,
            })
        else:
            # No compression needed, record for tracking
            self.trace_recorder.record("context.compressed", {
                "tokens_before": current_tokens,
                "tokens_after": current_tokens,
                "preserved_facts": 0,
                "memory_recalled_chars": recalled_chars,
                "trigger_type": "none_below_threshold",
            })
    
    def _save_checkpoint(self) -> None:
        """Atomically persist graph truth and the lightweight runtime cursor."""
        self._persist_graph_checkpoint(stage="runtime_checkpoint")
        state = self.runtime_state_manager.state
        state.iteration = self.iteration_count
        state.run_status = "interrupted" if self._interrupt_reason else "running"
        state.interrupt_state = {
            "reason": self._interrupt_reason,
            "requested_at": self._interrupt_requested_at,
        }
        state.model_config = _json_safe(self.model_config)
        state.active_agents = self.lifecycle_manager.export_active_snapshot()
        state.node_count = len(self.task_graph.nodes)
        state.edge_count = len(self.task_graph.edges)
        state.updated_at = datetime.now(timezone.utc).isoformat()
        path = self.runtime_state_manager.persist()
        self.trace_recorder.record(
            "checkpoint.saved",
            {
                "runtime_state_path": str(path),
                "graph_store_path": str(self.graph_store.db_path),
            },
        )

    def restore_checkpoint(self) -> bool:
        """Restore SQLite graph truth and revalidate executable node states.

        AstralNexus resumes persisted state, but AstralNexus additionally repairs
        in-flight states: a process dying while a worker was ``running`` must
        not leave a node permanently stranded.  Completed/failed/cancelled
        nodes remain immutable; running nodes are re-armed only when their
        dependencies are still satisfiable.
        """
        if not self.runtime_state_manager.path.exists():
            self.trace_recorder.record(
                "checkpoint.not_found",
                {"path": str(self.runtime_state_manager.path)},
            )
            return False
        try:
            restored = self.graph_store.load_task_graph(self.run_id)
            if restored is None:
                raise RuntimeError(
                    f"No task graph for run_id {self.run_id} in {self.graph_store.db_path}"
                )
            for node in restored.nodes.values():
                if node.status == "running":
                    target = "ready" if all(
                        restored.get_node(dep) and restored.get_node(dep).status in {"completed", "superseded"}
                        for dep in node.dependencies
                    ) else "blocked"
                    restored.update_node_status(node.node_id, target)
            self.task_graph = restored
            state = self.runtime_state_manager.state
            self.iteration_count = int(state.iteration)
            self.model_config = dict(state.model_config or {})
            interrupt_state = state.interrupt_state or {}
            self._interrupt_reason = str(interrupt_state.get("reason") or "")
            self._interrupt_requested_at = interrupt_state.get("requested_at")
            self.context_manager.clear_constraints()
            for node in restored.nodes.values():
                for index, constraint in enumerate(node.constraints):
                    self.context_manager.add_constraint(
                        f"{node.node_id}:constraint:{index}", constraint
                    )
            self._sync_task_graph_bindings()
            self._persist_graph_checkpoint(stage="post_restore_revalidation")
            self.trace_recorder.record("checkpoint.restored", {
                "runtime_state_path": str(self.runtime_state_manager.path),
                "graph_store_path": str(self.graph_store.db_path),
                "run_id": self.run_id,
                "node_count": len(restored.nodes),
                "rearmed_running_nodes": [n.node_id for n in restored.nodes.values() if n.status == "ready"],
            })
            return True
        except Exception as exc:
            self.trace_recorder.record("checkpoint.restore_failed", {
                "runtime_state_path": str(self.runtime_state_manager.path),
                "graph_store_path": str(self.graph_store.db_path),
                "error": str(exc),
            })
            logger.warning("Checkpoint restore failed: %s", exc)
            raise RuntimeError(
                f"Cannot restore authoritative checkpoint for {self.run_id}: {exc}"
            ) from exc
    
    def _persist_phase2_metrics(self) -> None:
        """Persist final Phase 2 metrics for dashboard/report parity."""
        if not self.graph_store:
            return

        phase2_metrics = self._calculate_phase2_metrics()
        for name, value in phase2_metrics.items():
            if not isinstance(value, (int, float)):
                continue
            try:
                metric = self.task_graph.add_metric({
                    "metric_id": f"{self.run_id}:{name}",
                    "node_id": "global",
                    "name": name,
                    "value": float(value),
                    "unit": "count" if isinstance(value, int) else "ratio",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"source": "phase2_final"},
                })
                self.graph_store.add_metric(metric)
            except Exception as e:
                logger.warning(f"Failed to persist phase2 metric {name}: {e}")
