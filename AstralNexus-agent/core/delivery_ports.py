"""Explicit delivery dependencies and detached read projections."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, NoReturn, Self, cast


@dataclass(frozen=True)
class DeliveryNode:
    node_id: str
    goal: str
    status: str
    dependencies: tuple[str, ...]
    metadata: Mapping[str, Any]


from AstralNexus.runtime.immutable import FrozenDict, freeze


@dataclass(frozen=True)
class DeliveryGraph:
    nodes: Mapping[str, DeliveryNode]


class DeliveryRuntime(Protocol):
    @property
    def task_graph(self) -> DeliveryGraph: ...
    @property
    def governor(self) -> Any: ...
    @property
    def trace_recorder(self) -> Any: ...
    def admit_recovery(self, origin: str, gaps: list[dict]) -> dict | None: ...
    def record_cost(self, root: str, event_type: str, event: dict) -> None: ...
    def run(self, *, goal: str, constraints: list[str]) -> dict: ...
    def owns_runtime(self) -> bool: ...
    def publication_fence(self) -> tuple[str, str, int] | None: ...
    def execution_fence(self): ...


class DeliveryRuntimeAdapter:
    """The only legacy engine adapter; delivery cannot mutate node references."""
    def __init__(self, source: Any):
        self._source = source

    @property
    def run_id(self) -> str:
        return str(self._source.run_id)

    @property
    def task_graph(self) -> DeliveryGraph:
        graph = self._source.task_graph
        lock = getattr(graph, "_mutation_lock", None)
        def snapshot():
            from AstralNexus.core.json_safety import json_safe
            return DeliveryGraph(FrozenDict({key: DeliveryNode(
                getattr(node, "node_id", key), getattr(node, "goal", ""), node.status,
                tuple(getattr(node, "dependencies", ())), freeze(json_safe(node.metadata)),
            ) for key, node in graph.nodes.items()}))
        if lock is None:
            return snapshot()
        with lock:
            return snapshot()

    @property
    def governor(self) -> Any:
        return self._source.governor

    @property
    def trace_recorder(self) -> Any:
        return self._source.trace_recorder

    def admit_recovery(self, origin: str, gaps: list[dict]) -> dict | None:
        return self._source.admit_recovery(origin, gaps)

    def record_cost(self, root: str, event_type: str, event: dict) -> None:
        self._source._record_lineage_event_cost(root, event_type, event)

    def run(self, *, goal: str, constraints: list[str]) -> dict:
        return self._source.run(goal=goal, constraints=constraints)

    def cancel_requested(self) -> bool:
        event = getattr(self._source, "_interrupt_event", None)
        return bool(event is not None and event.is_set()) or bool(getattr(self._source, "_interrupt_reason", ""))

    def owns_runtime(self) -> bool:
        probe = getattr(self._source, "_ownership_ok", None)
        return bool(probe()) if probe is not None else True

    def goal_snapshot(self) -> dict:
        manager = getattr(self._source, "runtime_state_manager", None)
        return deepcopy(getattr(getattr(manager, "state", None), "goal_state", {}) or {})

    def record_goal_state(self, state: dict) -> None:
        record = getattr(self._source, "record_application_state", None)
        if record is not None and not record({"goal_state": state}):
            raise RuntimeError("goal checkpoint ownership lost")

    def execution_fence(self):
        """Delivery keeps the managed run lease after the worker wave exits."""
        return getattr(self._source, "fence_token_provider", None)

    def publication_fence(self) -> tuple[str, str, int] | None:
        provider = getattr(self._source, "fence_token_provider", None)
        if provider is None:
            return None
        token = provider()  # A provider failure must never grant unfenced publication.
        if not isinstance(token, dict):
            raise RuntimeError("publication fence unavailable")
        return str(token["run_id"]), str(token["owner_id"]), int(token["generation"])



def adapt_delivery_runtime(source: Any) -> DeliveryRuntime:
    return source if isinstance(source, DeliveryRuntimeAdapter) else DeliveryRuntimeAdapter(source)
