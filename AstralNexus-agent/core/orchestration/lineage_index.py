"""Ephemeral lineage indexes rebuilt from one graph view, with no stale cache."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from AstralNexus.core.task_graph import TaskGraph


@dataclass(frozen=True)
class LineageIndex:
    successors: dict[str, tuple[str, ...]]
    carried_failures: frozenset[str]
    dependents_by_dependency: dict[str, tuple[str, ...]]

    @classmethod
    def build(cls, graph: TaskGraph) -> LineageIndex:
        successors: dict[str, list[str]] = {}
        predecessors: dict[str, str] = {}
        completed = []
        dependents: dict[str, list[str]] = {}
        for node in graph.nodes.values():
            for dependency in node.dependencies:
                dependents.setdefault(dependency, []).append(node.node_id)
            parent = str(node.metadata.get('recovery_of') or node.metadata.get('supersedes_node_id') or '')
            if parent and parent != node.node_id:
                successors.setdefault(parent, []).append(node.node_id)
                predecessors[node.node_id] = parent
            if node.status == 'completed':
                completed.append(node.node_id)
        # For failed predecessors, any completed descendant suffices. Propagate
        # backwards once instead of rescanning all nodes at every lineage hop.
        seen = set(completed)
        queue = deque(completed)
        carried = set()
        while queue:
            current = queue.popleft()
            ancestor = predecessors.get(current)
            if not ancestor:
                continue
            parent_node = graph.get_node(ancestor)
            if parent_node is not None and parent_node.status == 'failed':
                carried.add(ancestor)
            if ancestor not in seen:
                seen.add(ancestor)
                queue.append(ancestor)
        return cls({key: tuple(value) for key, value in successors.items()}, frozenset(carried), {key: tuple(value) for key, value in dependents.items()})

    def has_completed_carrier(self, graph: TaskGraph, node_id: str) -> bool:
        node = graph.get_node(node_id)
        if node is not None and node.status == 'failed':
            return node_id in self.carried_failures
        seen = {node_id}
        frontier = [node_id]
        while frontier:
            for successor in self.successors.get(frontier.pop(), ()):
                if successor in seen:
                    continue
                seen.add(successor)
                other = graph.get_node(successor)
                if other is not None and other.status == 'completed':
                    return True
                frontier.append(successor)
        return False

    def living_frontier(self, graph: TaskGraph, node_id: str) -> tuple[str, ...]:
        """All first living carriers across branches; never use insertion order."""
        seen = {node_id}
        pending = [node_id]
        living = set()
        while pending:
            for successor in self.successors.get(pending.pop(), ()):
                if successor in seen:
                    continue
                seen.add(successor)
                node = graph.get_node(successor)
                if node is not None and node.status not in {'failed', 'cancelled', 'superseded'}:
                    living.add(successor)
                else:
                    pending.append(successor)
        return tuple(sorted(living))
