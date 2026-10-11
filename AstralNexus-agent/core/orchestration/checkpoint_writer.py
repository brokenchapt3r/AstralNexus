"""Atomic checkpoint writer with explicit storage and ownership collaborators."""
from __future__ import annotations
import json
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Optional
from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
from AstralNexus.runtime.trace import TraceRecorder


@dataclass(frozen=True)
class CheckpointResult:
    committed: bool
    ownership_retained: bool
    commit_seq: int

    @property
    def may_continue(self) -> bool:
        return self.ownership_retained


@dataclass(frozen=True)
class CheckpointHooks:
    prepare_snapshot: Callable[[], None]
    ownership_ok: Callable[[], bool]
    ownership_lost: Callable[[str, dict[str, Any]], None]


class CheckpointWriter:
    def __init__(self, *, store: SQLiteTaskGraphStore,
                 state_manager: RuntimeStateManager, graph: TaskGraph,
                 commit_lock: RLock, run_id: str, trace: TraceRecorder,
                 hooks: CheckpointHooks, fence_token_provider, checkpoint_sink):
        self.store = store
        self.state_manager = state_manager
        self.graph = graph
        self.commit_lock = commit_lock
        self.run_id = run_id
        self.trace = trace
        self.hooks = hooks
        self.fence_token_provider = fence_token_provider
        self.checkpoint_sink = checkpoint_sink
        self.commit_seq = int(state_manager.state.last_commit_seq or 0)

    def write(self, *, stage: str, node_id: str | None = None) -> CheckpointResult:
        with self.commit_lock, self.state_manager._state_lock:
            if not self.store:
                return CheckpointResult(False, True, self.commit_seq)
            # 第三批 gap①：先围栏后写入（廉价前置探测；权威判定在联合提交内）。
            if not self.hooks.ownership_ok():
                self.hooks.ownership_lost("orchestrator.persistence.fenced", {
                    "stage": stage,
                    "node_id": node_id,
                    "fence": "probe",
                })
                return CheckpointResult(False, False, self.commit_seq)
            fence = None
            prev_commit_seq = 0
            prev_fence_generation = 0
            state = self.state_manager.state
            # The scheduler's last observe() can precede worker status transitions
            # in this wave. Commit graph and its displayed/recovery projection from
            # the same in-memory graph snapshot, otherwise a current graph fence
            # can carry an older nodes_by_status/ready_nodes/active_agents view.
            self.hooks.prepare_snapshot()
            if self.fence_token_provider is not None:
                try:
                    token = self.fence_token_provider()
                    if not isinstance(token, dict):
                        raise ValueError("configured fence provider returned no token")
                    for key in ("runs_db_path", "run_id", "owner_id", "generation", "now_iso"):
                        if not token.get(key):
                            raise ValueError(f"incomplete fence token: {key}")
                    if int(token["generation"]) < 1:
                        raise ValueError("fence token has an invalid generation")
                except Exception as exc:
                    self.hooks.ownership_lost("orchestrator.persistence.fenced", {
                        "stage": stage, "node_id": node_id,
                        "fence": "token_unavailable", "error": str(exc),
                    })
                    return CheckpointResult(False, False, self.commit_seq)
                # gap⑥：权威游标并入联合提交。先入戳（last_commit_seq /
                # fence_generation / 治理状态），序列化进 fence 行，与图
                # 写入、所有权验证同一个跨进程事务落库；runtime_state.json
                # 降级为可重建的展示缓存，恢复不再信任可能被迟到写入
                # 覆盖的文件。
                prev_commit_seq = int(state.last_commit_seq or 0)
                prev_fence_generation = int(state.fence_generation or 0)
                self.commit_seq += 1
                state.last_commit_seq = self.commit_seq
                state.fence_generation = int(token["generation"])
                fence = {
                    "runs_db_path": token["runs_db_path"],
                    "run_id": token["run_id"],
                    "owner_id": token["owner_id"],
                    "generation": token["generation"],
                    # The timestamp is part of the token (coordinator clock),
                    # not writer-supplied: expiry verification must not trust
                    # a stale now from the process being fenced.
                    "now_iso": token["now_iso"],
                    "commit_seq": self.commit_seq,
                    "graph_revision": state.graph_revision,
                    "cursor_json": json.dumps(
                        state.as_dict(), ensure_ascii=False, sort_keys=True,
                    ),
                }
            try:
                saved = self.store.save_task_graph(
                    self.run_id, self.graph, fence=fence,
                )
            except Exception:
                if fence is not None:
                    self.commit_seq = prev_commit_seq
                    state.last_commit_seq = prev_commit_seq
                    state.fence_generation = prev_fence_generation
                raise
            if saved is False:
                # 第三批 gap②：联合提交在权威事务内拒绝了过期令牌——探测
                # 与写入之间发生了接管。任务图零写入，序号与游标印记回退。
                if fence is not None:
                    self.commit_seq -= 1
                    state.last_commit_seq = prev_commit_seq
                    state.fence_generation = prev_fence_generation
                self.hooks.ownership_lost("orchestrator.persistence.fenced", {
                    "stage": stage,
                    "node_id": node_id,
                    "fence": "joint_commit_rejected",
                })
                return CheckpointResult(False, False, self.commit_seq)
            self.state_manager.persist()
            node = self.graph.get_node(node_id) if node_id else None
            self.trace.record("orchestrator.persistence.checkpoint", {
                "stage": stage,
                "node_id": node_id,
                "cache_write_error": getattr(self.state_manager, "last_cache_error", None),
                "status": node.status if node else None,
                "node_count": len(self.graph.nodes),
                "commit_seq": fence["commit_seq"] if fence is not None else None,
            })
            if self.checkpoint_sink is not None:
                confirmed: Optional[bool]
                try:
                    confirmed = self.checkpoint_sink({
                        "phase": f"graph_checkpoint:{stage}",
                        "graph_revision": self.state_manager.state.graph_revision,
                        "commit_seq": self.commit_seq,
                        "node_id": node_id,
                        "node_count": len(self.graph.nodes),
                    })
                except Exception:
                    # A broken sink must not break authoritative persistence;
                    # the joint-commit fence still guards ownership atomically.
                    confirmed = None
                if confirmed is False:
                    # The fenced reference refused confirmation: ownership was
                    # lost between the joint commit and this call. The write
                    # already landed under a then-valid token; stop before any
                    # further authoritative persistence.
                    self.hooks.ownership_lost(
                        "orchestrator.persistence.ownership_lost_after_write",
                        {"stage": stage, "node_id": node_id},
                    )
                    return CheckpointResult(True, False, self.commit_seq)
            return CheckpointResult(True, True, self.commit_seq)

