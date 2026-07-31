from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from AstralNexus.core.state_machine import (
    RunStatus,
    RuntimePhase,
    normalize_run_status,
    normalize_runtime_phase,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        return str(value)


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
    stop_reason: str = ""
    run_status: str = RunStatus.CREATED.value
    interrupt_state: Dict[str, Any] = field(default_factory=dict)
    model_config: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        self.run_status = normalize_run_status(self.run_status)
        self.phase = normalize_runtime_phase(self.phase)

    def as_dict(self) -> Dict[str, Any]:
        return _json_safe(asdict(self))


class RuntimeStateManager:
    """Observe, version, and atomically persist the global runtime state."""

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "runtime_state.json"
        self.state = self._load(run_id)
        self._last_progress_fingerprint = self.state.progress_fingerprint

    def _load(self, run_id: str) -> RuntimeState:
        if not self.path.is_file():
            return RuntimeState(run_id=run_id)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
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

    def record_recovery(self, fingerprint: str, *, changed: bool) -> int:
        fingerprint = str(fingerprint or "unknown_recovery")
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

    def persist(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.state.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return self.path
