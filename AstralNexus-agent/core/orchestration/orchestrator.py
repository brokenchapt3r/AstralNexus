"""Scheduling loop and shared runtime context; domain logic lives in components."""
from __future__ import annotations
from AstralNexus.runtime.run_context import get_env
from . import checkpoint, interruption, reporting, completion, planning, execution, verification, recovery
from .support import (
    AdaptiveConcurrency,
    AgentLifecycleManager,
    Any,
    Callable,
    ContextManager,
    Dict,
    Event,
    ExecutionRunResult,
    List,
    MemoryFederation,
    Optional,
    Path,
    RLock,
    ResourceScheduler,
    Router,
    RunOutputLayout,
    RuntimeObserver,
    RuntimeStateManager,
    SQLiteTaskGraphStore,
    TaskGraph,
    TraceRecorder,
    _existing_run_id,
    get_agent_registry,
    logger,
    os,
    time,
)

from .protocols import RuntimeContext
from queue import Queue
from .wave_executor import CommitBarrier, WaveTimeoutError
from .models import WaveResult
from . import async_api

class Orchestrator(RuntimeContext):
    """Coordinate execution, verification, recovery and durable checkpoints."""

    _SELF_LOCK_REASON_TOKENS = (
        "STALE_EXECUTION_EVIDENCE", "CANNOT_REFRESH_EVIDENCE",
        "FROZEN_BYTE_IDENTITY", "PREMISE_REFUTED",
    )

    def run_complete(self, task_content: str, *, constraints: Optional[List[str]] = None,
                     model_config: Optional[dict] = None) -> Dict[str, Any]:
        """Execute and verify publication through the core application service."""
        from AstralNexus.core.run_application import RunApplicationService
        return RunApplicationService(self).execute(task_content=task_content, run_dir=self.run_dir,
                                                   constraints=constraints, model_config=model_config)

    def __init__(
        self, 
        run_dir: Path, 
        *, 
        max_iterations: int = 0,
        graph_store: Optional[SQLiteTaskGraphStore] = None,
        run_id: Optional[str] = None,
        max_parallel: int = 4,
        event_callback=None,
        checkpoint_sink=None,
        ownership_probe=None,
        fence_token_provider=None,
        recovery_churn_budget: Optional[int] = None,
    ):
        self.run_dir = run_dir
        self.checkpoint_sink = checkpoint_sink
        self.ownership_probe = ownership_probe
        self._ownership_lost = False
        # Doc 17 第三批 gap②: fencing token for the joint commit (ownership
        # verification inside the graph store transaction). Provider returns
        # {"runs_db_path", "run_id", "owner_id", "generation"} or None.
        self.fence_token_provider = fence_token_provider
        self._commit_seq = 0
        self.output_layout = RunOutputLayout(run_dir)
        self.output_layout.ensure_runtime_dirs()
        self.task_graph = TaskGraph()
        self.trace_recorder = TraceRecorder(run_dir, event_callback=event_callback,
                                            owner_token_provider=fence_token_provider)
        self._node_activity_types: Dict[str, str] = {}
        self._staged_input_paths: List[str] = []
        self.agent_registry = get_agent_registry().clone()
        self.lifecycle_manager = AgentLifecycleManager(run_dir)
        self.context_manager = ContextManager(max_context_tokens=8000)
        self.router = Router(self.task_graph, self.trace_recorder)
        self._current_topology_state: Dict[str, Any] = {}
        
        from AstralNexus.core.fault_recovery import FaultRecoveryEngine
        self.fault_recovery_engine = FaultRecoveryEngine(self.task_graph, self.trace_recorder)
        # Recovery diagnostics and execution share the canonical graph.
        self.fault_detector = self.fault_recovery_engine.detector
        self.impact_analyzer = self.fault_recovery_engine.analyzer
        self.recovery_executor = self.fault_recovery_engine.executor
        self.trace_recorder.record("orchestrator.fault_recovery_engine.initialized", {})
        
        # Initialize resource scheduler with default resource pool
        resource_pool = self._create_default_resource_pool()
        self.resource_scheduler: ResourceScheduler = ResourceScheduler(resource_pool)
        
        # SQLite is the sole durable task-graph source for every run.
        self.graph_store: SQLiteTaskGraphStore = graph_store or SQLiteTaskGraphStore(
            self.run_dir / "task_graph.sqlite"
        )
        self.run_id: str = run_id or _existing_run_id(self.run_dir) or f"run-{int(time.time())}"
        self.memory_federation: MemoryFederation = MemoryFederation(self.graph_store, run_id=self.run_id)
        self.runtime_state_manager: RuntimeStateManager = RuntimeStateManager(self.run_dir, self.run_id)
        self._commit_seq = int(self.runtime_state_manager.state.last_commit_seq or 0)
        self.runtime_state_manager.write_fence = self._ownership_ok
        budget_override = recovery_churn_budget
        if budget_override is None:
            raw_budget = str(
                get_env("ASTRALNEXUS_RECOVERY_CHURN_BUDGET", "")
            ).strip()
            if raw_budget:
                try:
                    budget_override = int(raw_budget)
                except ValueError:
                    budget_override = None
        self.recovery_churn_budget: int = max(1, int(budget_override or 20))
        self._recovery_budget_exhausted: bool = (
            int(self.runtime_state_manager.state.recovery_no_progress_churn or 0)
            >= self._recovery_limits().no_progress_operations
        )
        self._churn_progress_marker: Optional[int] = None
        self._verification_unavailable: Optional[Dict[str, Any]] = None
        self.lifecycle_manager.restore_snapshot(
            list(self.runtime_state_manager.state.active_agents)
        )
        self.runtime_observer: RuntimeObserver = RuntimeObserver(
            self, self.runtime_state_manager
        )
        self.max_parallel: int = max_parallel
        self.adaptive_concurrency: AdaptiveConcurrency = AdaptiveConcurrency(max_parallel)
        self.delivery_evidence_visible: Optional[Callable[[Path], bool]] = None
        self.model_config: Dict[str, Any] = {}
        self._lineage_stream_usage: dict[tuple[str, str], int] = {}
        self._lineage_envelope_cache: dict[str, list[dict[str, Any]]] = {}
        self._completion_no_progress: int = 0
        self._completion_gap_fingerprint: str = ""
        self._completion_gap_repeats: int = 0
        # Fix AU：被 completed 后继接管的 failed 死记录（谱系接管）每个节点
        # 只入账一次，避免收敛判定每轮重复刷同一事件。
        self._absorbed_dead_records: set[str] = set()
        self._execution_lock: RLock = self.task_graph._mutation_lock
        self._wave_timed_out: bool = False
        self._wave_commit_barrier: Optional[CommitBarrier] = None
        # Interactive control is optional for embedded callers.  When bound,
        # the orchestrator is the only component that mutates the graph; the
        # UI only queues commands and sets the cooperative cancellation token.
        self._interrupt_event: Optional[Event] = None
        self._interrupt_queue: Queue[dict[str, Any]] | None = None
        self._interrupt_reason: str = ""
        self._interrupt_requested_at: str | None = None
        
        # Initialize GlobalGovernor for dynamic task admission (阶段五 P0)
        from AstralNexus.core.governor import GlobalGovernor
        self.governor: GlobalGovernor = GlobalGovernor(
            self.task_graph,
            self.trace_recorder,
            admission_threshold=0.65,
            max_active_tasks=max(
                1, int(get_env("ASTRALNEXUS_MAX_ACTIVE_TASKS", "2000"))
            ),
            max_total_tasks=max(
                1, int(get_env("ASTRALNEXUS_MAX_TOTAL_TASKS", "10000"))
            ),
        )
        self.governor.ownership_transfers_provider = lambda: list(
            self.runtime_state_manager.state.ownership_transfers or []
        )
        self.router.set_communication_edge_committer(
            self._commit_router_communication_edge
        )

        # The CLI enables the real control-plane matcher after construction.
        # Keeping construction dependency-free preserves embedded/test use;
        # formal product runs call enable_real_llm_worker_matching().
        
        # The core Verifier is the only quality authority.
        from AstralNexus.core.verifier import Verifier
        self.governor.resource_ids_provider = lambda: {resource.resource_id for resource in self.resource_scheduler.resource_pool.get_all_resources()}
        self.verifier: Verifier = Verifier(
            self.task_graph,
            self.trace_recorder,
            business_acceptance_checks=False,
            workspace_root=self.run_dir,
            pass_threshold=0.70,
            repair_threshold=0.45,
            max_repair_attempts=max(
                0,
                int(get_env("ASTRALNEXUS_MAX_VERIFICATION_REPAIRS", "0")),
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
            raise RuntimeError("Run persistence initialization failed") from e
        
        self.max_iterations = max_iterations
        self.iteration_count: int = 0


    def run(self, goal: Optional[str] = None, constraints: Optional[List[str]] = None,
            *,
            wave_hook: Optional[Callable[[Dict[str, bool], int], None]] = None) -> Dict[str, Any]:
        """Execute the task graph; completion does not authorize publication.
        
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
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("An event loop is already running; await run_async(...)")
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
        results: ExecutionRunResult = {
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
            # Fix AE (doc 18, 2026-09-15 r13b): heal stale lineage edges every
            # cycle, not only inside a recovery.  All five existing call sites
            # live on recovery/replan paths, so a graph that stops needing
            # recovery keeps whatever dead edges it already has — forever.
            # Live r13b: N-009 (seven documents, required for delivery) still
            # depended on N-007(superseded) and PATCH-R003-E2E(superseded)
            # after the lineage had moved on to PATCH-R004-*; had the R004
            # generation simply succeeded, nothing would ever have called the
            # rewire again and N-009 could never become ready.  This also heals
            # edges left behind by an earlier build, which is how those two
            # arose.  The rewire is idempotent and a no-op without dead edges.
            self._rewire_superseded_dependents()
            runtime_state = self.runtime_observer.observe(
                goal=goal,
                phase="observing",
                track_progress=True,
            )

            control = self._process_pending_interrupts()
            if control["stop"]:
                results["status"] = "interrupted"
                results["interrupt_reason"] = self._interrupt_reason or "user_interrupt"
                self._mark_interrupted(results["interrupt_reason"])
                break
            if control["changed"] and self._interrupt_event is not None:
                self._clear_requirement_interrupt()
                self.trace_recorder.record("orchestrator.requirement_change.replanned", {
                    "iteration": self.iteration_count,
                })

            lineage_verdict = self._lineage_stand_down_verdict()
            if lineage_verdict is not None:
                # b33 case1 fix10 第 4 步：谱系级熔断（两种判断互不冒充）。
                results["status"] = "blocked"
                results["stop_code"] = lineage_verdict["stop_code"]
                results["stop_reason"] = lineage_verdict["stop_reason"]
                self.trace_recorder.record("orchestrator.blocked", {
                    "reason": lineage_verdict["stop_code"],
                    "lineage_root": lineage_verdict["lineage_root"],
                    "state": lineage_verdict["state"],
                    "budget": lineage_verdict["budget"],
                    "cost": lineage_verdict["cost"],
                    "iterations": self.iteration_count,
                })
                self.runtime_observer.observe(
                    goal=goal,
                    phase="blocked",
                    decision={"action": lineage_verdict["stop_code"]},
                    stop_reason=results["stop_reason"],
                )
                break

            if self._recovery_budget_exhausted:
                # doc 18 / DeerFlow 对齐（2814bd5d 对照 #1/#4/#6）：预算
                # 触顶即终态 stand-down——blocked + no_progress_breaker
                # （闭合枚举 → 自动恢复被拒，交人工核查；显式
                # --user-resume 才授予新预算并留审计）。既有"无变化重复"
                # 熔断对"永远在变化"的 churn 不可见，此帽与其正交。
                churn_total = int(
                    self.runtime_state_manager.state.recovery_churn_total or 0
                )
                # F12: the breaker trips on the no-progress counter (progress
                # decays it) while the monotone ledger stays the audit trail.
                no_progress_total = int(
                    self.runtime_state_manager.state.recovery_no_progress_churn or 0
                )
                results["status"] = "blocked"
                results["stop_reason"] = (
                    "autonomous recovery churn budget exhausted: "
                    f"no_progress={no_progress_total} reached the run-level "
                    f"no-progress budget={int(self.recovery_churn_budget)} "
                    f"(total_churn={churn_total}; only events that produced no "
                    "new work or evidence are charged against the no-progress "
                    "budget, productive structural repairs are not) without "
                    "delivery completion"
                )
                results["stop_code"] = "no_progress_breaker"
                self.trace_recorder.record("orchestrator.blocked", {
                    "reason": "recovery_churn_budget_exhausted",
                    "churn_total": churn_total,
                    "no_progress_total": no_progress_total,
                    "budget": int(self.recovery_churn_budget),
                    "iterations": self.iteration_count,
                })
                self.runtime_observer.observe(
                    goal=goal,
                    phase="blocked",
                    decision={"action": "recovery_churn_budget_exhausted"},
                    stop_reason=results["stop_reason"],
                )
                break

            if self._verification_unavailable is not None:
                unavailable = self._verification_unavailable
                results["status"] = "blocked"
                results["stop_code"] = "verification_unavailable"
                results["stop_reason"] = (
                    "Verifier could not produce a verdict after bounded transport "
                    f"retries for {unavailable.get('node_id')}"
                )
                results["verification_unavailable"] = dict(unavailable)
                self.trace_recorder.record(
                    "orchestrator.blocked",
                    {
                        "reason": "verification_unavailable",
                        **unavailable,
                        "iteration": self.iteration_count,
                    },
                )
                self.runtime_observer.observe(
                    goal=goal,
                    phase="blocked",
                    decision={"action": "verification_unavailable", **unavailable},
                    stop_reason=results["stop_reason"],
                )
                break
            
            # Select next tasks to execute (all ready nodes)
            pending_verification = [
                node.node_id for node in self.task_graph.nodes.values()
                if node.status in {"result_submitted", "verifying"}
            ]
            next_tasks = pending_verification or self.select_next_tasks()
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
                            completion.get("stop_reason")
                            or "delivery completion made no progress three times"
                        )
                        # doc 18：闭合停机码随结果落地——CLI 直通持久化，
                        # 后续收尾异常不得改写主停机原因（attempt3 实测
                        # 熔断停机被 host_exception 覆盖）。
                        results["stop_code"] = completion.get("stop_code") or "no_progress_breaker"
                        results["delivery_verification"] = completion.get("report", {})
                        self.trace_recorder.record(
                            "orchestrator.blocked",
                            {
                                "reason": results["stop_code"],
                                "repeat_count": completion.get("repeat_count"),
                            },
                        )
                        break
                    continue
                else:
                    # No ready tasks but not complete — check for blocked
                    # tasks (includes nodes waiting on terminal-failed deps)
                    blocked = self._check_for_blocked_tasks()
                    # doc 18 attempt3：宿主异常拆机会把失败节点遗弃在图里
                    # ——没有 wave 会再调度它，重规划提案指向其 admitted
                    # 依赖者又全部验证器无效（recovery target 必须 failed/
                    # repair_pending/blocked），形成无解死锁（attempt3 实测：
                    # R-001-R failed → R-002-R/R-003-R admitted 永不就绪 →
                    # 同指纹重规划 ×3 熔断）。先经标准受治理恢复通道再驱动
                    # 孤儿失败，穷尽后才落入重规划。
                    if self._recover_orphaned_failures():
                        continue
                    recovery = self._replan_stalled_graph(
                        goal=goal,
                        blocked_nodes=blocked,
                        reason=(
                            "unsatisfied_dependencies_or_artifacts"
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
                        # doc 18：无进展熔断 = 人工核查类停机码（不在自动
                        # 恢复白名单），普通 --resume 结构性拒绝。
                        results["stop_code"] = "no_progress_breaker"
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
            # A checkpoint can land after workers submit results but before
            # the verifier runs. Resume that phase without rerunning workers
            # or treating their waiting consumers as a stalled graph.
            if pending_verification:
                self._process_worker_results(
                    {node_id: WaveResult.RESUME_VERIFICATION for node_id in pending_verification}, results,
                )
                wave_results: Dict[str, bool] = {}
            else:
                try:
                    wave_results = self._execute_wave(next_tasks)
                except WaveTimeoutError as exc:
                    self._wave_timed_out = True
                    for interrupted_node in [*exc.pending, *exc.waiting]:
                        self._stop_node_services(interrupted_node)
                    results["status"] = "blocked"
                    results["stop_code"] = "worker_wave_timeout"
                    results["stop_reason"] = str(exc)
                    if self._interrupt_event is None:
                        self._interrupt_event = Event()
                    self._interrupt_event.set()
                    self.trace_recorder.record("ready_wave.deadline_exceeded", {
                        "pending_nodes": exc.pending, "staged_nodes": exc.waiting,
                        "graph_results_committed": False, "stop_code": "worker_wave_timeout",
                    })
                    break

            # A running LLM/tool call may observe the token at its next
            # request boundary.  Apply queued requirement changes only after
            # that wave has returned, while the graph is no longer mutating.
            if self._cancel_requested():
                control = self._process_pending_interrupts()
                if control["changed"] and not control["stop"]:
                    if self._interrupt_event is not None:
                        self._clear_requirement_interrupt()
                    self._persist_graph_checkpoint(stage="requirement_change_replanned")
                    continue
                results["status"] = "interrupted"
                results["interrupt_reason"] = self._interrupt_reason or "user_interrupt"
                self._mark_interrupted(results["interrupt_reason"])
                break

            if wave_hook is not None and not pending_verification:
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

            self._process_worker_results(
                wave_results, results,
            )

            self._maintain_runtime()

        # Check if we hit max iterations
        if self.max_iterations > 0 and self.iteration_count >= self.max_iterations and results["status"] == "running":
            results["status"] = "blocked"
            results["stop_reason"] = "explicit benchmark iteration limit reached"
            self.trace_recorder.record("orchestrator.max_iterations", {"iterations": self.iteration_count})
        
        return self._finish_run(results, goal=goal, start_time=start_time)

    _finish_run = reporting._finish_run

    _ownership_ok = checkpoint._ownership_ok
    _mark_ownership_lost = checkpoint._mark_ownership_lost
    _persist_graph_checkpoint = checkpoint._persist_graph_checkpoint
    load_authoritative_graph = checkpoint.load_authoritative_graph
    _save_checkpoint = checkpoint._save_checkpoint
    restore_checkpoint = checkpoint.restore_checkpoint
    _clear_requirement_interrupt = interruption._clear_requirement_interrupt
    attach_interrupt_control = interruption.attach_interrupt_control
    _cancel_requested = interruption._cancel_requested
    _process_pending_interrupts = interruption._process_pending_interrupts
    _mark_interrupted = interruption._mark_interrupted
    handle_requirement_change = interruption.handle_requirement_change
    handle_resource_degradation = interruption.handle_resource_degradation
    inject_fault = interruption.inject_fault
    _record_node_runtime_event = reporting._record_node_runtime_event
    _completed_node_count = reporting._completed_node_count
    _refresh_result_counts = reporting._refresh_result_counts
    generate_final_report = reporting.generate_final_report
    _calculate_phase2_metrics = reporting._calculate_phase2_metrics
    _maintain_runtime = reporting._maintain_runtime
    _compress_context = reporting._compress_context
    _persist_phase2_metrics = reporting._persist_phase2_metrics
    is_execution_complete = completion.is_execution_complete
    _lineage_has_completed_carrier = completion._lineage_has_completed_carrier
    _delivery_progress_snapshot = completion._delivery_progress_snapshot
    _completed_synthesizer_nodes = completion._completed_synthesizer_nodes
    _assess_delivery_completion = completion._assess_delivery_completion
    _converge_runtime_state_after_terminal = completion._converge_runtime_state_after_terminal
    _sync_task_graph_bindings = planning._sync_task_graph_bindings
    _commit_router_communication_edge = planning._commit_router_communication_edge
    rebind_runtime = planning.rebind_runtime
    _create_default_resource_pool = planning._create_default_resource_pool
    register_staged_inputs = planning.register_staged_inputs
    _input_artifact_id = staticmethod(planning._input_artifact_id)
    _admit_staged_input_artifacts = planning._admit_staged_input_artifacts
    initialize_task_graph = planning.initialize_task_graph
    plan_task_decomposition = planning.plan_task_decomposition
    _build_governor_planning_context = planning._build_governor_planning_context
    select_next_tasks = planning.select_next_tasks
    _transition_node_status_guarded = planning._transition_node_status_guarded
    enable_real_llm_worker_matching = execution.enable_real_llm_worker_matching
    execute_task_async = async_api.execute_task_async
    run_async = async_api.run_async
    _execute_task_async = execution._execute_task_async
    _run_agent_task_async = execution._run_agent_task_async
    execute_task = execution.execute_task
    _instantiate_governed_worker = execution._instantiate_governed_worker
    _aborted_turn_stop_reason = execution._aborted_turn_stop_reason
    _apply_missing_artifact_verdict = execution._apply_missing_artifact_verdict
    _run_agent_task = execution._run_agent_task
    _execute_wave = execution._execute_wave
    _admit_background_candidates = execution._admit_background_candidates
    shutdown = execution.shutdown
    __enter__ = execution.__enter__
    __exit__ = execution.__exit__
    _record_verification_basis = verification._record_verification_basis
    _record_worker_verdict = verification._record_worker_verdict
    _declared_contract_paths = staticmethod(verification._declared_contract_paths)
    _contract_physical_path = verification._contract_physical_path
    _finalize_node_evidence = verification._finalize_node_evidence
    _node_task_version = staticmethod(verification._node_task_version)
    _verification_self_lock_codes = verification._verification_self_lock_codes
    _verification_claim_sets = verification._verification_claim_sets
    _added_nodes_claim_new_evidence = verification._added_nodes_claim_new_evidence
    _patch_superseded_owners = verification._patch_superseded_owners
    _verification_premise_baseline = verification._verification_premise_baseline
    _verification_premise_changed = verification._verification_premise_changed
    _executable_ready_frontier = verification._executable_ready_frontier
    _complete_report_gap_locally = verification._complete_report_gap_locally
    _apply_governed_verification_recovery = verification._apply_governed_verification_recovery
    _process_worker_results = verification._process_worker_results
    _persist_verification_receipt = verification._persist_verification_receipt
    _latest_verification_view = verification._latest_verification_view
    _consume_recovery_churn = recovery._consume_recovery_churn
    admit_recovery = recovery.admit_recovery
    _lineage_root_for = recovery._lineage_root_for
    _bind_requirement_observations = recovery._bind_requirement_observations
    _recovery_limits = recovery._recovery_limits
    _lineage_stand_down_verdict = recovery._lineage_stand_down_verdict
    _record_lineage_event_cost = recovery._record_lineage_event_cost
    _find_downstream_nodes = recovery._find_downstream_nodes
    _supersede_stale_recovery_children = recovery._supersede_stale_recovery_children
    _replan_stalled_graph = recovery._replan_stalled_graph
    _check_for_blocked_tasks = recovery._check_for_blocked_tasks
    _call_with_circuit_wait = recovery._call_with_circuit_wait
    _recover_orphaned_failures = recovery._recover_orphaned_failures
    _execute_governed_fault = recovery._execute_governed_fault
    _worker_report_for_node = recovery._worker_report_for_node
    _inherit_lineage_next_actions = recovery._inherit_lineage_next_actions
    _strip_unregistered_artifact_refs = recovery._strip_unregistered_artifact_refs
    _normalize_recovery_targets = recovery._normalize_recovery_targets
    _ensure_artifact_producer_deps = recovery._ensure_artifact_producer_deps
    _clip_head_tail = staticmethod(recovery._clip_head_tail)
    _resolve_declared_root_causes = recovery._resolve_declared_root_causes
    _patch_covers_root_causes = recovery._patch_covers_root_causes
    _convert_redundant_to_dependent = recovery._convert_redundant_to_dependent
    _convert_conflict_to_takeover = recovery._convert_conflict_to_takeover
    _grant_completed_root_cause_paths = recovery._grant_completed_root_cause_paths
    _normalize_root_cause_takeover = recovery._normalize_root_cause_takeover
    _enforce_root_cause_routing = recovery._enforce_root_cause_routing
    _lineage_root_node_id = recovery._lineage_root_node_id
    _lineage_root_of_id = recovery._lineage_root_of_id
    _lineage_recovery_fingerprint = recovery._lineage_recovery_fingerprint
    _lineage_history = recovery._lineage_history
    _patch_derives_from_lineage = recovery._patch_derives_from_lineage
    _check_lineage_recovery_repeat = recovery._check_lineage_recovery_repeat
    _lineage_attempt_ever_dispatched = recovery._lineage_attempt_ever_dispatched
    _record_lineage_recovery_attempt = recovery._record_lineage_recovery_attempt
    _convert_completed_reopen_to_repair_node = recovery._convert_completed_reopen_to_repair_node
    _normalize_reverifier_ownership = recovery._normalize_reverifier_ownership
    _dependency_permanently_dead = recovery._dependency_permanently_dead
    _lineage_has_living_successor = recovery._lineage_has_living_successor
    _stop_node_services = recovery._stop_node_services
    _rewire_superseded_dependents = recovery._rewire_superseded_dependents
    _is_takeover_eligible_conflict_owner = recovery._is_takeover_eligible_conflict_owner
    _is_gated_ready_conflict_owner = recovery._is_gated_ready_conflict_owner
    _strip_foreign_takeover_claims = recovery._strip_foreign_takeover_claims
    _normalize_recovery_ownership_transfer = recovery._normalize_recovery_ownership_transfer
    _downstream_consumers = recovery._downstream_consumers
    _escalate_invalid_recovery_proposal = recovery._escalate_invalid_recovery_proposal
    _attempt_recovery = recovery._attempt_recovery

    def record_application_state(self, payload: dict) -> bool:
        """Owner-thread product state shares the authoritative fenced cursor."""
        state = self.runtime_state_manager.state
        state.execution_status = str(payload.get("execution_status") or state.execution_status)
        state.delivery_status = str(payload.get("delivery_status") or state.delivery_status)
        state.publication_status = str(payload.get("publication_status") or state.publication_status)
        if "delivery_receipt" in payload:
            state.delivery_receipt = dict(payload["delivery_receipt"] or {})
        if "goal_state" in payload:
            state.goal_state = dict(payload["goal_state"] or {})
        if "phase" in payload:
            state.phase = str(payload["phase"])
        if "run_status" in payload:
            state.run_status = str(payload["run_status"])
        if "stop_code" in payload:
            state.stop_code = str(payload["stop_code"] or "")
        if "stop_reason" in payload:
            state.stop_reason = str(payload["stop_reason"] or "")
        persisted = self._persist_graph_checkpoint(stage="application_state")
        if persisted and self.graph_store and hasattr(self.graph_store, "update_run_status"):
            self.graph_store.update_run_status(self.run_id, state.run_status)
        return persisted
