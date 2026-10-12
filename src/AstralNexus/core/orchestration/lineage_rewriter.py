"""Deterministic lineage rewiring over indexed, affected consumers only."""
from __future__ import annotations
from typing import Callable
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.core.dependency_projection import needs_repaired_version
from .lineage_index import LineageIndex

DEAD = frozenset({'superseded', 'failed', 'cancelled'})
ACTIVE_CONSUMERS = frozenset({'admitted', 'waiting_dependencies', 'ready', 'running', 'blocked', 'result_submitted', 'verifying', 'repair_pending'})


class LineageRewriter:
    def __init__(self, graph: TaskGraph, *, commit: Callable, record: Callable):
        self.graph = graph
        self.commit = commit
        self.record = record

    def apply(self) -> int:
        from AstralNexus.core.governor import TaskGraphPatch
        with self.graph._mutation_lock:
            index = LineageIndex.build(self.graph)
            tails: dict[str, tuple[str, ...]] = {}
            updates: dict[str, list[str]] = {}
            transitions = []
            closure_cache: dict[str, set[str]] = {}
            def ancestors(node_id: str) -> set[str]:
                if node_id not in closure_cache:
                    seen = set()
                    pending = list(self.graph.nodes[node_id].dependencies)
                    while pending:
                        current = pending.pop()
                        if current in seen:
                            continue
                        seen.add(current)
                        node = self.graph.get_node(current)
                        if node is not None:
                            pending.extend(node.dependencies)
                    closure_cache[node_id] = seen
                return closure_cache[node_id]
            for dead in self.graph.nodes.values():
                if dead.status not in DEAD:
                    continue
                dependents = index.dependents_by_dependency.get(dead.node_id, ())
                if not dependents:
                    continue
                candidates = tails.setdefault(dead.node_id, index.living_frontier(self.graph, dead.node_id))
                for consumer_id in dependents:
                    consumer = self.graph.nodes[consumer_id]
                    if consumer.status not in ACTIVE_CONSUMERS:
                        continue
                    possible = [ident for ident in candidates if ident != consumer_id]
                    if len(possible) > 1 and consumer.input_artifacts:
                        required = set(consumer.input_artifacts)
                        matched = [ident for ident in possible if required.issubset(set(self.graph.nodes[ident].output_artifacts))]
                        if len(matched) == 1:
                            possible = matched
                    if len(possible) != 1:
                        if possible:
                            self.record('orchestrator.dependency.rewire_skipped', {'dependent': consumer_id, 'from_superseded': dead.node_id,
                                'reason': 'ambiguous_living_successors', 'candidates': sorted(possible)})
                        continue
                    tail_id = possible[0]
                    tail = self.graph.nodes[tail_id]
                    if not needs_repaired_version(consumer=consumer, tail=tail, artifacts=self.graph.artifacts):
                        self.record('orchestrator.dependency.rewire_kept', {'dependent': consumer_id, 'from_superseded': dead.node_id,
                            'living_tail': tail_id, 'reason': 'consumer_inputs_satisfied_by_accepted_artifact', 'input_artifacts': list(consumer.input_artifacts)})
                        continue
                    if consumer_id in ancestors(tail_id):
                        self.record('orchestrator.dependency.rewire_skipped', {'dependent': consumer_id, 'from_superseded': dead.node_id,
                            'to_living_tail': tail_id, 'reason': 'would_create_cycle'})
                        continue
                    original = updates.get(consumer_id, list(consumer.dependencies))
                    replacement = list(dict.fromkeys(tail_id if dep == dead.node_id else dep for dep in original))
                    if replacement != original:
                        updates[consumer_id] = replacement
                        transitions.append({'dependent': consumer_id, 'from_superseded': dead.node_id, 'to_living_tail': tail_id})
            if not updates:
                return 0
            try:
                self.commit(TaskGraphPatch(update_nodes=[{'node_id': ident, 'dependencies': deps} for ident, deps in updates.items()],
                                           reason_codes=['LINEAGE_DEPENDENCY_REWIRE']))
            except ValueError as exc:
                self.record('orchestrator.dependency.rewire_skipped', {'reason': 'projected_patch_rejected', 'error': str(exc), 'dependents': sorted(updates)})
                return 0
            for transition in transitions:
                self.record('orchestrator.dependency.rewired', transition)
            return len(transitions)
