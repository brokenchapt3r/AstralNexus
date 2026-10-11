"""Checkpoint responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .support import (
    Any,
    Dict,
    Optional,
    TaskGraph,
    _json_safe,
    datetime,
    json,
    logger,
    timezone,
    traceback,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def _ownership_ok(self: Orchestrator) -> bool:
    """Fail-closed ownership probe shared by every authoritative write."""
    if self._ownership_lost:
        return False
    if self.ownership_probe is None:
        return True
    try:
        return bool(self.ownership_probe())
    except Exception:
        return False


def _mark_ownership_lost(self: Orchestrator, event_type: str, payload: Dict[str, Any]) -> None:
    self._ownership_lost = True
    self.trace_recorder.record(event_type, payload)
    if self._interrupt_event is not None:
        self._interrupt_event.set()


def _persist_graph_checkpoint(self: Orchestrator, *, stage: str, node_id: Optional[str] = None) -> bool:
    """Commit through the storage service; False means no further writes are allowed."""
    from .checkpoint_writer import CheckpointHooks, CheckpointWriter
    def prepare_snapshot() -> None:
        self.runtime_state_manager.refresh_graph_projection(self.task_graph, self.lifecycle_manager)
        self.runtime_state_manager.state.governance_state = {
            "goal_spec": self.governor.goal_spec.as_dict() if self.governor.goal_spec else None,
            "delivery_contract": dict(self.governor.delivery_contract or {}),
            "goal_version": self.governor.goal_version,
        }
    with self._execution_lock:
        writer = CheckpointWriter(
            store=self.graph_store, state_manager=self.runtime_state_manager,
            graph=self.task_graph, commit_lock=self._execution_lock,
            run_id=self.run_id, trace=self.trace_recorder,
            hooks=CheckpointHooks(prepare_snapshot, self._ownership_ok, self._mark_ownership_lost),
            fence_token_provider=self.fence_token_provider, checkpoint_sink=self.checkpoint_sink,
        )
        try:
            result = writer.write(stage=stage, node_id=node_id)
            return result.may_continue
        finally:
            self._commit_seq = writer.commit_seq


def load_authoritative_graph(self: Orchestrator) -> TaskGraph:
    """Load an existing run through the Orchestrator control boundary."""
    restored = self.graph_store.load_task_graph(self.run_id)
    if restored is None:
        raise RuntimeError(
            f"No task graph for run_id {self.run_id} in {self.graph_store.db_path}"
        )
    self.task_graph = restored
    self._execution_lock = restored._mutation_lock
    self._sync_task_graph_bindings()
    return restored


def _save_checkpoint(self: Orchestrator) -> None:
    """Atomically persist graph truth and the lightweight runtime cursor."""
    # 第三批 gap①：围栏失败必须中止整个保存操作——不能只跳过内部
    # 图写入而让调用方继续 persist 运行状态（隔离复现教训）。
    # gap⑥重排：游标字段先入戳，随联合提交一并落库（DB 为权威），
    # json 缓存由 _persist_graph_checkpoint 内部统一写出。
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
    if not self._persist_graph_checkpoint(stage="runtime_checkpoint"):
        self.trace_recorder.record("checkpoint.save_fenced", {
            "stage": "runtime_checkpoint",
            "ownership_lost": self._ownership_lost,
        })
        return
    self.trace_recorder.record(
        "checkpoint.saved",
        {
            "runtime_state_path": str(self.runtime_state_manager.path),
            "graph_store_path": str(self.graph_store.db_path) if self.graph_store else "",
        },
    )


def restore_checkpoint(self: Orchestrator) -> bool:
    """Restore SQLite graph truth and revalidate executable node states.

    AstralNexus resumes persisted state, but AstralNexus additionally repairs
    in-flight states: a process dying while a worker was ``running`` must
    not leave a node permanently stranded.  Completed/failed/cancelled
    nodes remain immutable; running nodes are re-armed only when their
    dependencies are still satisfiable.
    """
    try:
        from AstralNexus.core.runtime_state import read_authoritative_cursor
        cursor = read_authoritative_cursor(self.run_dir, run_id=self.run_id)
        if not cursor or cursor.get("source") == "db_read_failed":
            self.trace_recorder.record("checkpoint.not_found", {
                "path": str(self.runtime_state_manager.path),
                "source": (cursor or {}).get("source", "missing"),
            })
            return False
        if not self.runtime_state_manager.restore_from_payload(cursor.get("cursor") or {}):
            raise RuntimeError("Checkpoint cursor identity or payload is invalid")
        self._commit_seq = int(self.runtime_state_manager.state.last_commit_seq or 0)
        restored = self.graph_store.load_task_graph(self.run_id)
        if restored is None:
            raise RuntimeError(
                f"No task graph for run_id {self.run_id} in {self.graph_store.db_path}"
            )
        from AstralNexus.core.checkpoint_migrations import migrate_checkpoint_graph
        schema = self.runtime_state_manager.state.checkpoint_schema_version
        migrated_schema = migrate_checkpoint_graph(restored, schema, self.trace_recorder.record)
        for node in restored.nodes.values():
            if node.status == "running":
                execution_id = node.metadata.get("runtime_execution_id")
                if execution_id:
                    from AstralNexus.runtime.execution_store import ExecutionStore
                    execution = ExecutionStore(self.run_dir).get(execution_id)
                    if execution["node_id"] != node.node_id:
                        raise RuntimeError("Checkpoint execution identity belongs to another node")
                    if execution["status"] == "failed":
                        outcome = execution["outcome"] or {}
                        diagnostic = str(outcome.get("error") or outcome.get("content") or "execution failed")
                        node.metadata["error"] = diagnostic
                        node.metadata["last_failure"] = {
                            "message": diagnostic, "execution_id": execution_id,
                            "source": "execution_journal_reconciliation",
                        }
                        restored.update_node_status(node.node_id, "failed")
                        continue
                target = "ready" if all(
                    (dependency := restored.get_node(dep)) is not None
                    and dependency.status in {"completed", "superseded"}
                    for dep in node.dependencies
                ) else "blocked"
                restored.update_node_status(node.node_id, target)
        self.runtime_state_manager.state.checkpoint_schema_version = migrated_schema
        self.task_graph = restored
        self._execution_lock = restored._mutation_lock
        state = self.runtime_state_manager.state
        governance = state.governance_state
        if governance.get("goal_spec"):
            from AstralNexus.core.governor import GoalSpec
            self.governor.goal_spec = GoalSpec(**governance["goal_spec"])
        from AstralNexus.core.delivery_contract_policy import normalize_delivery_contract
        self.governor.delivery_contract = normalize_delivery_contract(dict(governance.get("delivery_contract") or {}))
        self.governor.goal_version = int(governance.get("goal_version") or state.task_version)
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
        from AstralNexus.runtime.execution_store import ExecutionStore
        for restored_node in restored.nodes.values():
            restored_execution = restored_node.metadata.get("runtime_execution_id")
            if restored_execution:
                ExecutionStore(self.run_dir, fence_token_provider=self.fence_token_provider).interrupt_open(restored_execution)
        # Older checkpoints recorded takeover edges without retiring the
        # original writer. Repair that declared relationship before any
        # ready wave can execute both owners concurrently.
        from AstralNexus.core.governor import TaskGraphPatch
        for replacement in list(restored.nodes.values()):
            target_id = str(replacement.metadata.get("recovery_of") or replacement.metadata.get("supersedes_node_id") or "")
            takeover_target = restored.get_node(target_id) if target_id else None
            if takeover_target is None or takeover_target.status in {"completed", "cancelled", "superseded"}:
                continue
            # Fix AR: this repair only guarded the TARGET's status.  A
            # replacement that has itself been retired is no longer the
            # owner -- the lineage moved past it -- so replaying its
            # takeover is not a repair, it is a regression: the takeover
            # patch rewrites every live consumer's dependency from the
            # current producer back onto that retired ancestor.  Live r13b
            # hit exactly this: four historical takeovers whose targets were
            # still "failed" (failed is absent from the skip set above)
            # replayed on every restore and rewrote the dependency of
            # N-009 / R013 / R014 / R015 / R016 / R017 from the completed
            # PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE back to the superseded
            # PATCH-R006-RUNTIME.  The Governor then rejected the graph it
            # had just been handed ("artifact producer is not a declared
            # dependency"), restore_checkpoint raised, and the run was
            # marked checkpoint_restore_failed -- unrecoverable, with nine
            # approved nodes stranded.  A retired replacement's takeover
            # was already applied when it happened; replaying it is never
            # correct.
            if replacement.status in {"superseded", "cancelled"}:
                self.trace_recorder.record(
                    "checkpoint.takeover_replay_skipped",
                    {
                        "replacement_node_id": replacement.node_id,
                        "replacement_status": replacement.status,
                        "target_node_id": target_id,
                        "target_status": takeover_target.status,
                        "reason": "replacement_already_retired",
                    },
                )
                continue
            takeover = self._normalize_recovery_ownership_transfer(TaskGraphPatch(add_nodes=[{
                "node_id": replacement.node_id, "recovery_of": target_id,
            }]))
            takeover.add_nodes = []
            takeover.update_nodes.append({"node_id": target_id, "status": "superseded"})
            self.governor.apply_task_graph_patch(takeover)
        # Restore responsibility lost by older partial takeover patches.
        # Only retired ancestors can donate ownership; Governor still checks
        # the complete candidate for collisions with other active workers.
        inheritance_updates = []
        for replacement in restored.nodes.values():
            if replacement.status in {"completed", "cancelled", "superseded", "failed"}:
                continue
            # Live r13c generation 12: the walk treated terminal blocked
            # N-122 as a living successor and merged its superseded
            # ancestor's paths back onto it, synthesizing the ownership
            # update that crashed restore.  A node that can never run
            # again — ownership already transferred, or blocked with a
            # terminal reason — must not receive donated responsibility.
            transferred = str(
                replacement.metadata.get("ownership_transfer_to") or ""
            ).strip()
            terminal_blocked = (
                replacement.status == "blocked"
                and bool(replacement.metadata.get("_terminal_reason"))
            )
            if transferred or terminal_blocked:
                self.trace_recorder.record(
                    "checkpoint.inheritance_walk_skipped",
                    {
                        "node_id": replacement.node_id,
                        "status": replacement.status,
                        "reason": (
                            "ownership_transferred" if transferred
                            else "terminal_blocked"
                        ),
                    },
                )
                continue
            paths = list(replacement.owned_paths)
            artifacts = list(replacement.output_artifacts)
            outputs = list(replacement.metadata.get("expected_outputs", []) or [])
            ancestor_id = str(replacement.metadata.get("recovery_of") or replacement.metadata.get("supersedes_node_id") or "")
            seen = {replacement.node_id}
            while ancestor_id and ancestor_id not in seen:
                seen.add(ancestor_id)
                ancestor = restored.get_node(ancestor_id)
                if ancestor is None or ancestor.status != "superseded":
                    break
                paths = list(dict.fromkeys([*ancestor.owned_paths, *paths]))
                artifacts = list(dict.fromkeys([*ancestor.output_artifacts, *artifacts]))
                outputs = list(dict.fromkeys([*ancestor.metadata.get("expected_outputs", []), *outputs]))
                ancestor_id = str(ancestor.metadata.get("recovery_of") or ancestor.metadata.get("supersedes_node_id") or "")
            if paths != replacement.owned_paths or artifacts != replacement.output_artifacts or outputs != replacement.metadata.get("expected_outputs", []):
                inheritance_updates.append({
                    "node_id": replacement.node_id, "owned_paths": paths,
                    "output_artifacts": artifacts,
                    "metadata": {"expected_outputs": outputs},
                })
        if inheritance_updates:
            self.governor.apply_task_graph_patch(TaskGraphPatch(update_nodes=inheritance_updates))
        if not self._persist_graph_checkpoint(stage="post_restore_revalidation"):
            raise RuntimeError("post-restore checkpoint was rejected")
        self.trace_recorder.record("checkpoint.restored", {
            "runtime_state_path": str(self.runtime_state_manager.path),
            "graph_store_path": str(self.graph_store.db_path),
            "run_id": self.run_id,
            "node_count": len(restored.nodes),
            "rearmed_running_nodes": [n.node_id for n in restored.nodes.values() if n.status == "ready"],
        })
        return True
    except Exception as exc:
        # Fix AR：restore 失败是不可恢复的（run 被判 checkpoint_restore_failed），
        # 而这里过去只留一行 str(exc)。线上遇到
        # "graph patch artifact producer is not a declared dependency" 时，
        # 无法判断是哪一个恢复期补丁触发的 —— 图里依赖与边都在。
        # 不可恢复的失败必须留下可定位的栈。
        self.trace_recorder.record("checkpoint.restore_failed", {
            "runtime_state_path": str(self.runtime_state_manager.path),
            "graph_store_path": str(self.graph_store.db_path),
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()[-6000:],
        })
        logger.warning("Checkpoint restore failed: %s", exc)
        raise RuntimeError(
            f"Cannot restore authoritative checkpoint for {self.run_id}: {exc}"
        ) from exc


