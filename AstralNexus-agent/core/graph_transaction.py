"""Atomic in-memory mutation boundary for the semantic task graph."""
from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any

from AstralNexus.core.task_graph import TaskGraph


class TaskGraphTransaction:
    """Roll back every graph collection when a patch commit fails.

    Governor validates a candidate graph before mutation, but individual
    mutations can still fail because of state-machine enforcement or malformed
    integration data.  This transaction preserves collection identity so
    components holding references to ``nodes`` or ``edges`` remain valid.
    """

    def __init__(self, task_graph: TaskGraph):
        self.task_graph = task_graph
        lock = getattr(task_graph, "_mutation_lock", None)
        if lock is None:
            lock = RLock()
            setattr(task_graph, "_mutation_lock", lock)
        self._lock: RLock = lock
        self._snapshot: dict[str, Any] | None = None

    def __enter__(self) -> "TaskGraphTransaction":
        self._lock.acquire()
        self._snapshot = {
            "nodes": deepcopy(self.task_graph.nodes),
            "edges": deepcopy(self.task_graph.edges),
            "facts": deepcopy(self.task_graph.facts),
            "artifacts": deepcopy(self.task_graph.artifacts),
            "metrics": deepcopy(self.task_graph.metrics),
        }
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is not None and self._snapshot is not None:
                self.task_graph.nodes.clear()
                self.task_graph.nodes.update(self._snapshot["nodes"])
                self.task_graph.edges[:] = self._snapshot["edges"]
                self.task_graph.facts.clear()
                self.task_graph.facts.update(self._snapshot["facts"])
                self.task_graph.artifacts.clear()
                self.task_graph.artifacts.update(self._snapshot["artifacts"])
                self.task_graph.metrics.clear()
                self.task_graph.metrics.update(self._snapshot["metrics"])
        finally:
            self._snapshot = None
            self._lock.release()
        return False
