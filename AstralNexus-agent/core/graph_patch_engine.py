"""Validation and commit services for Governor task-graph patches."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from AstralNexus.core.graph_transaction import TaskGraphTransaction
from AstralNexus.core.task_graph import TaskGraph, TaskNode


class GraphCycleError(ValueError):
    """Raised when a complete proposed dependency graph is cyclic."""


class GraphPatchValidator:
    """Pure validation helpers; these methods never mutate a TaskGraph."""

    @staticmethod
    def find_duplicate(
        goal: str,
        capabilities: list[str],
        existing: Mapping[str, Mapping[str, Any]],
        *,
        similarity_threshold: float,
    ) -> str | None:
        target = GraphPatchValidator._text_key(goal)
        for node_id, item in existing.items():
            if item.get("status") in {"cancelled", "superseded", "failed"}:
                continue
            candidate = GraphPatchValidator._text_key(item.get("goal"))
            overlap = target & candidate
            similarity = len(overlap) / max(1, len(target | candidate))
            same_caps = set(capabilities) & set(
                item.get("metadata", {}).get("required_capabilities", [])
            )
            if similarity >= similarity_threshold and (
                not capabilities or same_caps
            ):
                return node_id
        return None

    @staticmethod
    def assert_acyclic(
        node_ids: Iterable[str],
        proposed_edges: Mapping[tuple[str, str, str], Any],
    ) -> None:
        ids = set(node_ids)
        dag_edges = [
            (source, target)
            for source, target, kind in proposed_edges
            if kind in {"depends_on", "dependency"}
        ]
        indegree = {node_id: 0 for node_id in ids}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
        for source, target in dag_edges:
            if source not in ids or target not in ids:
                raise ValueError(
                    f"dependency edge references unknown endpoint: "
                    f"{source} -> {target}"
                )
            if target not in outgoing[source]:
                outgoing[source].append(target)
                indegree[target] += 1
        queue = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            source = queue.pop()
            visited += 1
            for target in outgoing[source]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(ids):
            raise GraphCycleError(
                "graph patch would introduce a dependency cycle"
            )

    @staticmethod
    def repair_new_node_cycles(
        node_ids: Iterable[str],
        proposed_edges: Mapping[tuple[str, str, str], Any],
        *,
        new_node_ids: set[str],
        max_repairs: int,
    ) -> tuple[dict[tuple[str, str, str], Any], list[dict[str, Any]]]:
        """Remove the least costly soft edge from an all-new-node cycle.

        The method never repairs a cycle involving a committed node or an edge
        marked ``hard_dependency``.  Every semantic change is returned to the
        Governor for trace emission.
        """
        working = dict(proposed_edges)
        repairs: list[dict[str, Any]] = []
        for _ in range(max(0, max_repairs)):
            cycle = GraphPatchValidator.find_dependency_cycle(
                node_ids, working
            )
            if not cycle:
                return working, repairs
            cycle_nodes = {
                endpoint
                for source, target, _kind in cycle
                for endpoint in (source, target)
            }
            if not cycle_nodes.issubset(new_node_ids):
                raise GraphCycleError(
                    "dependency cycle touches an already committed node"
                )
            candidates: list[
                tuple[float, tuple[str, str, str], dict[str, Any]]
            ] = []
            for key in cycle:
                raw = working[key]
                metadata = GraphPatchValidator._edge_metadata(raw)
                if metadata.get("hard_dependency") is True:
                    continue
                if metadata.get("repairable") is False:
                    continue
                try:
                    strength = float(
                        metadata.get("dependency_strength", 1.0)
                    )
                except (TypeError, ValueError):
                    strength = 1.0
                candidates.append((strength, key, metadata))
            if not candidates:
                raise GraphCycleError(
                    "dependency cycle contains no repairable soft edge"
                )
            strength, key, metadata = min(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            working.pop(key)
            repairs.append({
                "source": key[0],
                "target": key[1],
                "edge_type": key[2],
                "dependency_strength": strength,
                "reason": "minimum_cost_soft_edge_removed",
                "original_metadata": metadata,
            })
        GraphPatchValidator.assert_acyclic(node_ids, working)
        return working, repairs

    @staticmethod
    def find_dependency_cycle(
        node_ids: Iterable[str],
        proposed_edges: Mapping[tuple[str, str, str], Any],
    ) -> list[tuple[str, str, str]]:
        ids = set(node_ids)
        adjacency: dict[str, list[tuple[str, str, str]]] = {
            node_id: [] for node_id in ids
        }
        for key in proposed_edges:
            source, target, kind = key
            if kind in {"depends_on", "dependency"} and source in adjacency:
                adjacency[source].append(key)

        state = {node_id: 0 for node_id in ids}
        parent_edge: dict[str, tuple[str, str, str]] = {}

        def visit(node_id: str) -> list[tuple[str, str, str]]:
            state[node_id] = 1
            for edge in adjacency[node_id]:
                target = edge[1]
                if state.get(target, 0) == 0:
                    parent_edge[target] = edge
                    cycle = visit(target)
                    if cycle:
                        return cycle
                elif state.get(target) == 1:
                    cycle = [edge]
                    cursor = node_id
                    while cursor != target:
                        previous = parent_edge[cursor]
                        cycle.append(previous)
                        cursor = previous[0]
                    return cycle
            state[node_id] = 2
            return []

        for node_id in sorted(ids):
            if state[node_id] == 0:
                cycle = visit(node_id)
                if cycle:
                    return cycle
        return []

    @staticmethod
    def _edge_metadata(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw.get("metadata") or {})
        return dict(getattr(raw, "metadata", {}) or {})

    @staticmethod
    def _text_key(value: Any) -> set[str]:
        text = re.sub(r"\s+", "", str(value or "").lower())
        if len(text) < 2:
            return {text} if text else set()
        return {text[index:index + 2] for index in range(len(text) - 1)}


class GraphPatchCommitter:
    """Apply an already validated patch inside one rollback boundary."""

    def __init__(self, task_graph: TaskGraph):
        self.task_graph = task_graph

    def commit(
        self,
        *,
        additions: list[dict[str, Any]],
        update_nodes: list[dict[str, Any]],
        proposed_edges: Mapping[tuple[str, str, str], Any],
        cancel_nodes: list[str],
        remap: Callable[[Any], str],
        resolved_state: Mapping[str, Mapping[str, Any]],
        reasoning: str,
        reason_codes: list[str],
    ) -> list[str]:
        cancelled: list[str] = []
        with TaskGraphTransaction(self.task_graph):
            for node in additions:
                self.task_graph.add_node(TaskNode(
                    node_id=node["node_id"],
                    goal=str(node["goal"]),
                    role=str(node.get("role", "governed_worker")),
                    status=str(node.get("status", "admitted")),
                    dependencies=list(node.get("dependencies", [])),
                    constraints=[str(item) for item in node.get("constraints", [])],
                    acceptance_criteria=[
                        str(item)
                        for item in node.get("acceptance_criteria", [])
                    ],
                    resource_tier=str(node.get("execution_target", "cloud")),
                    metadata=node["metadata"],
                ))
            for raw in update_nodes:
                node_id = remap(raw.get("node_id") or raw.get("task_id"))
                node = self.task_graph.get_node(node_id)
                if node is None:
                    raise ValueError(f"graph patch updates unknown node: {node_id}")
                for field_name in (
                    "goal", "role", "constraints", "acceptance_criteria",
                    "inputs", "outputs", "resource_tier",
                    "risk_level", "sensitivity",
                ):
                    if field_name in raw:
                        setattr(node, field_name, raw[field_name])
                if "metadata" in raw:
                    node.metadata.update(raw["metadata"] or {})
                if "dependencies" in raw:
                    node.dependencies = list(
                        resolved_state[node_id]["dependencies"]
                    )
                if "status" in raw:
                    self.task_graph.update_node_status(
                        node_id, resolved_state[node_id]["status"]
                    )
            # ``proposed_edges`` is the complete topology that passed cycle,
            # endpoint and protocol validation. Commit that exact set instead
            # of appending it to stale edges from the previous graph.
            self.task_graph.edges.clear()
            for raw in proposed_edges.values():
                self.task_graph.add_edge(raw)
            dependencies_by_target: dict[str, list[str]] = {
                node_id: [] for node_id in self.task_graph.nodes.keys()
            }
            for edge in self.task_graph.edges:
                if edge.edge_type == "depends_on":
                    dependencies_by_target.setdefault(edge.target, []).append(
                        edge.source
                    )
            for node_id, task_node in self.task_graph.nodes.items():
                task_node.dependencies = list(
                    dict.fromkeys(dependencies_by_target.get(node_id, []))
                )
            for raw_id in cancel_nodes:
                node_id = remap(raw_id)
                node = self.task_graph.get_node(node_id)
                if node and node.status not in {
                    "completed", "failed", "cancelled", "superseded",
                }:
                    self.task_graph.update_node_status(node_id, "cancelled")
                    node.metadata["cancel_reason"] = (
                        reasoning or "Governor incremental replanning"
                    )
                    node.metadata["reason_codes"] = list(reason_codes)
                    cancelled.append(node_id)
        return cancelled
