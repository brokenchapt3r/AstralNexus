from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from functools import wraps
from threading import RLock
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from AstralNexus.core.state_machine import (
    RunStatus,
    RuntimePhase,
    normalize_run_status,
    normalize_runtime_phase,
)


def synchronized_state(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        manager = getattr(self, "runtime_state_manager", self)
        lock = getattr(manager, "_state_lock", None)
        if lock is None:
            return function(self, *args, **kwargs)
        with lock:
            return function(self, *args, **kwargs)
    return wrapped


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# JSON 安全化只有**一份**实现（`core/json_safety.py`）。本文件曾自己实现一份，
# 语义与 orchestrator 版不同：dataclass / 带 ``to_dict()`` 的对象在这里被压成
# ``str(...)`` —— 而本文件写的正是 ``last_decision`` / ``topology_state`` / agent
# 快照，于是这些位置一旦出现结构化对象，**落盘的就是无意义 repr、字段全丢**。
# 现统一到字段保留的那套。别名保持原有调用点不变。
from AstralNexus.core.json_safety import json_safe as _json_safe


@dataclass
class RuntimeState:
    """Persistent global state observed by the autonomous runtime kernel."""

    run_id: str
    global_goal: str = ""
    task_version: int = 1
    iteration: int = 0
    phase: str = RuntimePhase.INITIALIZED.value
    graph_revision: int = 0
    node_count: int = 0
    edge_count: int = 0
    nodes_by_status: Dict[str, int] = field(default_factory=dict)
    ready_nodes: List[str] = field(default_factory=list)
    active_agents: List[Dict[str, Any]] = field(default_factory=list)
    memory_state: Dict[str, Any] = field(default_factory=dict)
    topology_state: Dict[str, Any] = field(default_factory=dict)
    resource_state: Dict[str, Any] = field(default_factory=dict)
    uncertainty: Dict[str, Any] = field(default_factory=dict)
    last_decision: Dict[str, Any] = field(default_factory=dict)
    progress_fingerprint: str = ""
    repeated_no_progress: int = 0
    recovery_fingerprint: str = ""
    repeated_recovery: int = 0
    planning_attempts_total: int = 0
    recovery_churn_total: int = 0
    charged_recovery_operations: List[str] = field(default_factory=list)
    # doc 18 / live b31 (F12): the run-level churn cap must distinguish a
    # recovery event that provably widened the executable frontier from one
    # that re-proposed a frozen premise.  This tally only advances for the
    # latter; recovery_churn_total stays the monotone audit ledger.
    recovery_no_progress_churn: int = 0
    # b33 case1 fix10 §7：进展信用挂在**证据身份**上，不挂在节点世代上。
    # 键 =（契约逻辑 id 原始验收项，内容哈希证据版本），跨接管稳定，因此
    # "同一份证据经多个接管节点传递"只记一次，"换节点"不会清零已成立的进展。
    # 默认空列表：pre-fix11 的 runtime_state.json 没有这个键也能加载。
    credited_evidence_keys: List[str] = field(default_factory=list)
    # b33 case1 fix10 第 2 步（确定性修复归属准入）：**历史归属**与**当前写权限**
    # 分开记账。已完成作者保留 owned_paths（历史归属，用于证据/契约溯源），当前
    # 写权限由这里的转移记录决定：每个规范化路径至多一个当前持有者。
    # 形如 [{"path": "...", "from_node": "P-104", "to_node": "P-109", "at": "..."}]。
    # 默认空列表：pre-fix14 的 runtime_state.json 没有这个键也能加载。
    ownership_transfers: List[Dict[str, Any]] = field(default_factory=list)
    # b33 case1 fix10 第 4 步：谱系级成本与进展，键 = **谱系根**（原始任务）。
    # 所以"换节点"不清零：接管/换代累在同一个根上。每条含
    # requests/tokens/tool_calls/recoveries/elapsed_seconds + satisfied_items
    # + gap_fingerprint/gap_recoveries。默认空 dict：旧状态文件照常加载。
    lineage_accounting: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recovery_error: str = ""
    stop_reason: str = ""
    run_status: str = RunStatus.CREATED.value
    execution_status: str = "not_started"
    delivery_status: str = "not_started"
    stop_code: str = ""
    publication_status: str = "not_published"
    delivery_receipt: Dict[str, Any] = field(default_factory=dict)
    goal_state: Dict[str, Any] = field(default_factory=dict)
    interrupt_state: Dict[str, Any] = field(default_factory=dict)
    model_config: Dict[str, Any] = field(default_factory=dict)
    governance_state: Dict[str, Any] = field(default_factory=dict)
    # Doc 17 第三批 gap③: shared commit identity. last_commit_seq is stamped
    # only by jointly-committed fenced checkpoints (graph store + ownership
    # verification in one transaction), so recovery reconciles the three
    # stores by commit identity, not by revision magnitude guesses.
    last_commit_seq: int = 0
    fence_generation: int = 0
    updated_at: str = field(default_factory=_utc_now)
    checkpoint_schema_version: int = 0

    def __post_init__(self) -> None:
        self.run_status = normalize_run_status(self.run_status)
        self.phase = normalize_runtime_phase(self.phase)

    def as_dict(self) -> Dict[str, Any]:
        return _json_safe(asdict(self))


class RuntimeStateManager:
    """Observe, version, and atomically persist the global runtime state."""

    def __init__(self, run_dir: Path, run_id: str):
        self._state_lock = RLock()
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "runtime_state.json"
        self.last_cache_error: Optional[str] = None
        self.state = self._load(run_id)
        self._last_progress_fingerprint = self.state.progress_fingerprint
        # Doc 17 第三批 gap①: optional ownership fence shared by ALL persist
        # paths (snapshot / record_recovery / checkpoint). When attached and
        # the probe denies ownership, persist() skips the authoritative write
        # entirely — a zombie executor cannot corrupt the cursor between
        # checkpoints. The joint-commit fence in the graph store remains the
        # atomic authority; this closes the json-only write paths.
        self.write_fence: Optional[Callable[[], bool]] = None

    def _load(self, run_id: str) -> RuntimeState:
        try:
            authoritative = read_authoritative_cursor(self.run_dir, run_id=run_id)
            if authoritative and authoritative.get("source") == "db_read_failed":
                raise ValueError("authoritative database cursor unavailable; refusing stale cache")
            if authoritative and authoritative.get("source") == "commit_fence":
                raw = authoritative["cursor"]
            elif self.path.is_file():
                # Only an unfenced legacy run can depend on a JSON checkpoint.
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                from AstralNexus.core.checkpoint_migrations import CURRENT_CHECKPOINT_SCHEMA
                return RuntimeState(run_id=run_id, checkpoint_schema_version=CURRENT_CHECKPOINT_SCHEMA)
            if not isinstance(raw, dict):
                raise ValueError("runtime_state.json must contain a JSON object")
            allowed = set(RuntimeState.__dataclass_fields__)
            restored = {
                key: value for key, value in raw.items()
                if key in allowed
            }
            persisted_run_id = str(restored.get("run_id") or "")
            if persisted_run_id and persisted_run_id != run_id:
                raise ValueError(
                    f"runtime state belongs to {persisted_run_id}, not {run_id}"
                )
            restored["run_id"] = run_id
            return RuntimeState(**restored)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot restore authoritative runtime state from {self.path}: {exc}"
            ) from exc

    @staticmethod
    def fingerprint(parts: Iterable[Any]) -> str:
        payload = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _nodes_by_status(task_graph: Any) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for node in task_graph.nodes.values():
            status = str(getattr(node, "status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _active_agents(lifecycle_manager: Any) -> List[Dict[str, Any]]:
        if lifecycle_manager is None:
            return []
        if hasattr(lifecycle_manager, "export_active_snapshot"):
            return _json_safe(lifecycle_manager.export_active_snapshot())
        try:
            agents = lifecycle_manager.get_active_agents()
        except Exception:
            return []
        snapshots = []
        for agent in agents:
            if hasattr(agent, "to_dict"):
                snapshots.append(_json_safe(agent.to_dict()))
            else:
                snapshots.append({
                    "agent_id": str(getattr(agent, "agent_id", "")),
                    "role": str(getattr(agent, "role", "")),
                    "node_id": str(getattr(agent, "node_id", "")),
                    "state": str(getattr(agent, "state", "")),
                })
        return snapshots

    @staticmethod
    def _memory_state(memory_federation: Any) -> Dict[str, Any]:
        if memory_federation is None:
            return {}
        try:
            return _json_safe(memory_federation.get_statistics())
        except Exception:
            return {}

    @staticmethod
    def _resource_state(resource_scheduler: Any) -> Dict[str, Any]:
        if resource_scheduler is None:
            return {}
        try:
            utilization = resource_scheduler.get_resource_utilization()
            return _json_safe(utilization)
        except Exception:
            return {}

    @staticmethod
    def _topology_state(task_graph: Any) -> Dict[str, Any]:
        node_count = len(task_graph.nodes)
        dependency_edges = 0
        communication_edges = 0
        for edge in task_graph.edges:
            edge_type = str(getattr(edge, "edge_type", ""))
            if edge_type == "depends_on":
                dependency_edges += 1
            elif edge_type in {"communicates", "communicates_to", "evidence_for"}:
                communication_edges += 1
        possible_edges = max(1, node_count * max(0, node_count - 1))
        return {
            "dependency_edges": dependency_edges,
            "communication_edges": communication_edges,
            "communication_sparsity": 1.0 - (communication_edges / possible_edges),
        }

    @staticmethod
    def _uncertainty(task_graph: Any) -> Dict[str, Any]:
        low_confidence_facts = [
            fact_id
            for fact_id, fact in task_graph.facts.items()
            if float(getattr(fact, "confidence", 0.0) or 0.0) < 0.7
        ]
        failed_nodes = [
            node.node_id for node in task_graph.nodes.values()
            if str(getattr(node, "status", "")) in {"failed", "repair_pending", "blocked"}
        ]
        open_nodes = [
            node.node_id for node in task_graph.nodes.values()
            if str(getattr(node, "status", "")) not in {
                "completed", "cancelled", "superseded", "failed"
            }
        ]
        return {
            "low_confidence_fact_ids": low_confidence_facts,
            "failed_or_repair_node_ids": failed_nodes,
            "open_node_ids": open_nodes,
            "risk_score": min(
                1.0,
                (len(low_confidence_facts) + len(failed_nodes) * 2)
                / max(1, len(task_graph.nodes) * 2),
            ),
        }

    def observe(
        self,
        *,
        task_graph: Any,
        global_goal: str,
        task_version: int,
        iteration: int,
        phase: str,
        run_status: str,
        lifecycle_manager: Any = None,
        memory_federation: Any = None,
        resource_scheduler: Any = None,
        topology_override: Optional[Dict[str, Any]] = None,
        decision: Optional[Dict[str, Any]] = None,
        track_progress: bool = False,
        stop_reason: str = "",
    ) -> RuntimeState:
        nodes_by_status = self._nodes_by_status(task_graph)
        ready_nodes = sorted(
            node.node_id for node in task_graph.nodes.values()
            if str(getattr(node, "status", "")) == "ready"
        )
        progress_fingerprint = self.fingerprint([
            sorted(
                (
                    node.node_id,
                    str(getattr(node, "status", "")),
                    int(getattr(node, "retry_count", 0) or 0),
                )
                for node in task_graph.nodes.values()
            ),
            sorted(
                (
                    str(getattr(edge, "source", "")),
                    str(getattr(edge, "target", "")),
                    str(getattr(edge, "edge_type", "")),
                )
                for edge in task_graph.edges
            ),
            len(task_graph.facts),
            len(task_graph.artifacts),
        ])
        if track_progress:
            if progress_fingerprint == self._last_progress_fingerprint:
                self.state.repeated_no_progress += 1
            else:
                self.state.repeated_no_progress = 0
                self._last_progress_fingerprint = progress_fingerprint

        self.state.global_goal = str(global_goal or "")
        self.state.task_version = max(1, int(task_version or 1))
        self.state.iteration = max(0, int(iteration or 0))
        self.state.phase = normalize_runtime_phase(phase)
        self.state.run_status = normalize_run_status(run_status)
        self.state.graph_revision += 1
        self.state.node_count = len(task_graph.nodes)
        self.state.edge_count = len(task_graph.edges)
        self.state.nodes_by_status = nodes_by_status
        self.state.ready_nodes = ready_nodes
        self.state.active_agents = self._active_agents(lifecycle_manager)
        self.state.memory_state = self._memory_state(memory_federation)
        self.state.topology_state = _json_safe(
            topology_override or self._topology_state(task_graph)
        )
        self.state.resource_state = self._resource_state(resource_scheduler)
        self.state.uncertainty = self._uncertainty(task_graph)
        self.state.last_decision = _json_safe(decision or self.state.last_decision)
        self.state.progress_fingerprint = progress_fingerprint
        self.state.stop_reason = str(stop_reason or self.state.stop_reason)
        self.state.updated_at = _utc_now()
        self.persist()
        return self.state

    def refresh_graph_projection(
        self, task_graph: Any, lifecycle_manager: Any = None,
    ) -> RuntimeState:
        """Refresh graph-derived fields immediately before a joint checkpoint.

        ``observe()`` runs at scheduler observation boundaries, while graph
        mutations and worker lifecycle changes can be committed later in the
        same iteration. A joint graph/cursor commit must not pair the current
        graph with an older status summary from that earlier observation.
        This method deliberately does not persist or advance iteration/phase;
        the caller includes the refreshed projection in the same graph fence.
        """
        self.state.node_count = len(task_graph.nodes)
        self.state.edge_count = len(task_graph.edges)
        self.state.nodes_by_status = self._nodes_by_status(task_graph)
        self.state.ready_nodes = sorted(
            node.node_id for node in task_graph.nodes.values()
            if str(getattr(node, "status", "")) == "ready"
        )
        self.state.active_agents = self._active_agents(lifecycle_manager)
        self.state.uncertainty = self._uncertainty(task_graph)
        self.state.progress_fingerprint = self.fingerprint([
            sorted(
                (
                    node.node_id,
                    str(getattr(node, "status", "")),
                    int(getattr(node, "retry_count", 0) or 0),
                )
                for node in task_graph.nodes.values()
            ),
            sorted(
                (
                    str(getattr(edge, "source", "")),
                    str(getattr(edge, "target", "")),
                    str(getattr(edge, "edge_type", "")),
                )
                for edge in task_graph.edges
            ),
            len(task_graph.facts),
            len(task_graph.artifacts),
        ])
        self.state.updated_at = _utc_now()
        return self.state

    def record_recovery(self, fingerprint: str, *, changed: bool, error: str = "") -> int:
        fingerprint = str(fingerprint or "unknown_recovery")
        self.state.recovery_error = "" if changed else str(error)
        if changed:
            self.state.recovery_fingerprint = ""
            self.state.repeated_recovery = 0
        elif fingerprint == self.state.recovery_fingerprint:
            self.state.repeated_recovery += 1
        else:
            self.state.recovery_fingerprint = fingerprint
            self.state.repeated_recovery = 1
        self.state.updated_at = _utc_now()
        self.persist()
        return self.state.repeated_recovery

    @synchronized_state
    def record_churn(self, kind: str, *, weight: int = 1, operation_id: str = "") -> int:
        """run 级结构性恢复/重规划 churn 的单调累计（doc 18 attempt2 修复）。

        与 repeated_recovery（任何图变化即清零）不同：每次恢复补丁应用/
        拒绝、每次停滞重规划都累计，永不因"有变化"重置——"永远在变化"
        的无进展循环（churn）由此获得结构性上界。只有显式人工恢复
        （--user-resume，审计入账）才授权重新授予预算。
        """
        if operation_id:
            if operation_id in self.state.charged_recovery_operations:
                return int(self.state.recovery_churn_total or 0)
            self.state.charged_recovery_operations.append(operation_id)
        current = max(0, int(self.state.recovery_churn_total or 0))
        self.state.recovery_churn_total = current + max(1, int(weight))
        self.state.updated_at = _utc_now()
        self.persist()
        return self.state.recovery_churn_total

    @synchronized_state
    def record_no_progress_churn(self, *, weight: int = 1) -> int:
        """Structural recovery events that carry no provable progress.

        doc 18 / live b31 (F12): a total counter cannot tell a repair that
        restored a ready frontier from a re-proposal of a frozen premise — the
        live run reached 20/20 while its last patch had already unblocked three
        nodes that then never ran.  Only events without provable progress
        advance this tally; the monotone total stays the audit ledger.
        """
        current = max(0, int(self.state.recovery_no_progress_churn or 0))
        self.state.recovery_no_progress_churn = current + max(1, int(weight))
        self.state.updated_at = _utc_now()
        self.persist()
        return self.state.recovery_no_progress_churn

    @synchronized_state
    def record_recovery_progress(self) -> int:
        """Decay the no-progress tally when a recovery event proves progress.

        Halving (never zeroing) keeps a real upper bound: staying under the cap
        forever would take exponentially many productive patches, so the run
        budget still bounds the run while a productive patch can never trip the
        breaker by itself.
        """
        current = max(0, int(self.state.recovery_no_progress_churn or 0))
        self.state.recovery_no_progress_churn = current // 2
        self.state.updated_at = _utc_now()
        self.persist()
        return self.state.recovery_no_progress_churn

    @synchronized_state
    def credit_evidence_key(self, key: str) -> bool:
        """Record one progress credit against an evidence identity.

        幂等、跨接管稳定：同一个 key 只入账一次，返回**本次是否新入账**。
        身份来自 ``runtime/evidence_identity``（契约逻辑 id + 内容哈希），
        不是节点世代——所以接管节点转手同一份证据不会二次计功，反过来
        "换节点"也不会把已经成立的进展清零（b33 case1 fix10 §7）。
        """
        token = str(key or "").strip()
        if not token:
            return False
        keys = list(self.state.credited_evidence_keys or [])
        if token in keys:
            return False
        keys.append(token)
        self.state.credited_evidence_keys = keys
        self.state.updated_at = _utc_now()
        self.persist()
        return True

    def record_ownership_transfer(
        self, *, path: str, from_node: str, to_node: str
    ) -> bool:
        """Record one deterministic write-permission transfer; idempotent.

        返回**本次是否新记账**。已完成作者不会被改写 owned_paths（历史归属保留），
        当前写权限由这条转移决定。同一路径再次转移给同一节点不重复记账。
        """
        normalized = str(path or "").strip().replace("\\", "/").strip("/")
        source = str(from_node or "").strip()
        target = str(to_node or "").strip()
        if not normalized or not target:
            return False
        transfers = list(self.state.ownership_transfers or [])
        previous = next((entry for entry in reversed(transfers)
                         if str(entry.get("path") or "") == normalized), None)
        if previous is not None and str(previous.get("to_node") or "") == target:
            return False
        transfers.append({
            "path": normalized,
            "from_node": source,
            "to_node": target,
            "at": _utc_now(),
        })
        self.state.ownership_transfers = transfers
        self.state.updated_at = _utc_now()
        self.persist()
        return True

    def current_write_holder(self, path: str) -> str:
        """The node currently holding write permission for ``path`` (``""`` if none).

        取**最后一次**转移到该路径的 ``to_node``；没有转移记录时返回空串——
        "谁历史上拥有过"与"谁现在能写"是两件事。
        """
        normalized = str(path or "").strip().replace("\\", "/").strip("/")
        if not normalized:
            return ""
        holder = ""
        for entry in self.state.ownership_transfers or []:
            if str(entry.get("path") or "") == normalized:
                holder = str(entry.get("to_node") or "") or holder
        return holder

    def lineage_entry(self, root: str) -> Dict[str, Any]:
        """该谱系根的成本/进展条目（不存在则为空条目）。"""
        from AstralNexus.core.lineage_accounting import empty_entry

        token = str(root or "").strip()
        if not token:
            return empty_entry()
        return dict((self.state.lineage_accounting or {}).get(token) or empty_entry())

    @synchronized_state
    def record_lineage_cost(self, root: str, *, persist: bool = True, **cost: Any) -> Dict[str, Any]:
        """把成本累到**谱系根**上（换节点不清零）。只记账，不判罚。"""
        from AstralNexus.core.lineage_accounting import record_cost

        token = str(root or "").strip()
        if not token:
            return {}
        ledger = dict(self.state.lineage_accounting or {})
        entry = record_cost(ledger.get(token), **cost)
        ledger[token] = entry
        self.state.lineage_accounting = ledger
        self.state.updated_at = _utc_now()
        if persist:
            self.persist()
        return entry

    @synchronized_state
    def record_lineage_progress(
        self,
        root: str,
        satisfied_items: Any,
        *,
        gap_fingerprint: str = "",
        counted_as_recovery: bool = True,
    ) -> bool:
        """记一次进展判定，返回**是否出现新成立的验收项**。"""
        from AstralNexus.core.lineage_accounting import record_progress

        token = str(root or "").strip()
        if not token:
            return False
        ledger = dict(self.state.lineage_accounting or {})
        entry, progressed = record_progress(
            ledger.get(token),
            satisfied_items,
            gap_fingerprint=gap_fingerprint,
            counted_as_recovery=counted_as_recovery,
        )
        ledger[token] = entry
        self.state.lineage_accounting = ledger
        self.state.updated_at = _utc_now()
        self.persist()
        return progressed

    def lineage_breaker(self, root: str, *, budget: int, recovery_budget: Optional[int] = None) -> Dict[str, Any]:
        """两种不同的熔断判断（见 ``core/lineage_accounting.breaker_verdict``）。"""
        from AstralNexus.core.lineage_accounting import breaker_verdict

        return breaker_verdict(
            (self.state.lineage_accounting or {}).get(str(root or "").strip()),
            budget=budget, recovery_budget=recovery_budget,
        )

    def restore_from_payload(self, payload: Dict[str, Any]) -> bool:
        """Rebuild the authoritative cursor from a fenced DB commit payload.

        Doc 17 第三批第二轮修正③：runtime_state.json 是可重建的展示缓存
        ——文件替换不在联合事务保护内，旧执行者可能在探测成功后停顿、
        待新所有者写入后再覆盖它。恢复以联合提交内的游标载荷为准，
        不信任可能被迟到写入覆盖的文件。
        """
        if not isinstance(payload, dict):
            return False
        allowed = set(RuntimeState.__dataclass_fields__)
        restored = {
            key: value for key, value in payload.items()
            if key in allowed
        }
        persisted_run_id = str(restored.get("run_id") or "")
        if persisted_run_id and persisted_run_id != self.state.run_id:
            return False
        restored["run_id"] = self.state.run_id
        try:
            self.state = RuntimeState(**restored)
        except (TypeError, ValueError):
            return False
        self._last_progress_fingerprint = self.state.progress_fingerprint
        return True

    @synchronized_state
    def persist(self) -> Path:
        if self.write_fence is not None:
            try:
                allowed = bool(self.write_fence())
            except Exception:
                allowed = False  # fail closed: unprovable ownership
            if not allowed:
                # Skip the write; the file keeps the last legitimately
                # committed state. The owner's cooperative stop is triggered
                # by the fenced checkpoint path / heartbeat watcher.
                return self.path
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(self.state.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self.last_cache_error = None
        for attempt in range(4):
            if self.write_fence is not None:
                try:
                    allowed = bool(self.write_fence())
                except Exception:
                    allowed = False
                if not allowed:
                    self.last_cache_error = "cache ownership unavailable"
                    return self.path
            try:
                temporary.replace(self.path)
                break
            except PermissionError as exc:
                if attempt < 3:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                authoritative = read_authoritative_cursor(self.run_dir, run_id=self.state.run_id)
                if not authoritative or authoritative.get("source") != "commit_fence":
                    raise
                self.last_cache_error = str(exc)
                logging.getLogger(__name__).warning("JSON cache replacement failed; SQLite cursor remains authoritative: %s", exc)

        return self.path


def read_authoritative_cursor(
    run_dir: Path, *, run_id: str = "",
) -> Optional[Dict[str, Any]]:
    """外部观察者（网页/监控）读取运行游标的权威入口（第五轮重写）。

    原则（用户复核修正）：
    - 权威游标从明确的运行身份（``run_id`` 参数）或数据库记录本身定位，
      **不依赖 JSON 缓存的存在、run_id 或序号**——缓存正是被这份记录
      认证的对象，不能反过来决定是否读取数据库；
    - 有有效 DB 游标就始终使用它；缓存自报更大序号、错误 run_id、损坏
      或缺失都只记入 ``anomalies``，不改变来源选择；
    - 数据库读取失败**不等同于"没有权威提交"**：返回 source=db_read_failed
      且不回退缓存（监控应显示未知而非可能陈旧的数值）；
    - 只有确认不存在权威提交（无图数据库/无围栏表/无围栏行/行无游标）
      的 legacy 运行，才带 warning 回退 JSON 缓存。
    """
    run_dir = Path(run_dir)
    anomalies: List[str] = []

    fence: Optional[Dict[str, Any]] = None
    db_read_failed = False
    graph_path = run_dir / "task_graph.sqlite"
    if graph_path.is_file():
        try:
            from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

            fence = SQLiteTaskGraphStore(graph_path, read_only=True).latest_commit_fence(run_id)
        except Exception:
            db_read_failed = True
    if db_read_failed:
        return {
            "source": "db_read_failed",
            "warning": (
                "任务图数据库读取失败：不能等同于没有权威提交，"
                "不回退可能被迟到写入覆盖的 JSON 缓存；请检查存储后重试"
            ),
        }

    cache: Optional[Dict[str, Any]] = None
    json_path = run_dir / "runtime_state.json"
    if json_path.is_file():
        try:
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
            else:
                anomalies.append("json_cache_not_an_object")
        except (OSError, ValueError):
            anomalies.append("json_cache_unreadable")
    else:
        anomalies.append("json_cache_missing")

    if fence is not None and fence.get("cursor"):
        cache_seq = 0
        if int(fence.get("fence_identity_count") or 1) > 1 and not str(run_id or "").strip():
            anomalies.append("multiple_fence_identities")
        if cache is not None:
            try:
                cache_seq = int(cache.get("last_commit_seq") or 0)
            except (TypeError, ValueError):
                cache_seq = 0
                anomalies.append("cache_seq_not_an_integer")
            if cache_seq > int(fence.get("commit_seq") or 0):
                # 缓存虚高：只记录异常，权威仍是 DB 联合提交载荷。
                anomalies.append("cache_seq_ahead_of_authoritative")
            cache_run_id = str(cache.get("run_id") or "")
            if cache_run_id and cache_run_id != str(fence.get("run_id") or ""):
                anomalies.append("cache_run_id_mismatch")
        return {
            "source": "commit_fence",
            "commit_seq": int(fence.get("commit_seq") or 0),
            "generation": int(fence.get("generation") or 0),
            "fence_run_id": str(fence.get("run_id") or ""),
            "cursor": fence["cursor"],
            "cache_commit_seq": cache_seq,
            "anomalies": anomalies,
        }
    if fence is not None:
        anomalies.append("fence_row_without_cursor")

    # 确认不存在权威提交：legacy 运行才允许带警告回退缓存。
    if cache is not None:
        try:
            cache_seq = int(cache.get("last_commit_seq") or 0)
        except (TypeError, ValueError):
            cache_seq = 0
        return {
            "source": "json_cache",
            "commit_seq": cache_seq,
            "cursor": cache,
            "anomalies": anomalies,
            "warning": (
                "无联合提交记录（legacy 运行）：json 缓存可能被迟到写入覆盖"
                "（文件替换不在联合事务内），仅作展示兜底"
            ),
        }
    return None
