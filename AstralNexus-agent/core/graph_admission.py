"""Serialized graph admission: validate detached candidates before publishing."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from AstralNexus.core.governor import GlobalGovernor
    from AstralNexus.core.governance_types import TaskGraphPatch, AdmissionDecision


TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'superseded'})


class GraphAdmissionService:
    @staticmethod
    def detached_patch(patch):
        from AstralNexus.core.governance_types import TaskGraphPatch
        if isinstance(patch, dict):
            unknown = set(patch) - set(TaskGraphPatch.__dataclass_fields__)
            if unknown:
                raise ValueError(f'graph patch has unknown fields: {sorted(unknown)}')
            patch = TaskGraphPatch(**deepcopy(patch))
        elif isinstance(patch, TaskGraphPatch):
            patch = deepcopy(patch)
        else:
            raise ValueError('graph patch must be an object')
        for field in ('add_nodes', 'update_nodes', 'cancel_nodes', 'add_edges', 'remove_edges', 'background_candidates', 'reason_codes'):
            if not isinstance(getattr(patch, field), list):
                raise ValueError(f'graph patch {field} must be a list')
        for i, raw in enumerate(patch.add_nodes):
            if not isinstance(raw, dict) or not isinstance(raw.get('goal'), str) or not raw['goal'].strip():
                raise ValueError(f'graph patch add_nodes[{i}] requires a nonempty goal')
            metadata = raw.get('metadata')
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f'graph patch add_nodes[{i}] metadata must be an object')
            raw['metadata'] = dict(metadata or {})
            for field in ('estimated_cost', 'estimated_tokens'):
                if field in raw:
                    raw['metadata']['estimated_cost'] = raw[field]
        for i, raw in enumerate(patch.update_nodes):
            if not isinstance(raw, dict) or not str(raw.get('node_id') or raw.get('task_id') or '').strip():
                raise ValueError(f'graph patch update_nodes[{i}] requires a node identity')
        for raw in [*patch.add_nodes, *patch.update_nodes]:
            metadata = raw.get('metadata')
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError('graph patch metadata must be an object')
            for container in (raw, metadata or {}):
                for name in ('dependencies', 'delivery_criterion_ids', 'delivery_output_ids'):
                    value = container.get(name)
                    if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value)):
                        raise ValueError(f'graph patch {name} requires nonempty strings')
        patch.candidate_mode = str(patch.candidate_mode).strip().lower()
        if patch.candidate_mode not in {'merge', 'replace'}:
            raise ValueError('invalid candidate_mode')
        for field in ('add_edges', 'remove_edges'):
            for i, raw in enumerate(getattr(patch, field)):
                if not isinstance(raw, dict) or not isinstance(raw.get('source'), str) or not raw['source'].strip() or not isinstance(raw.get('target'), str) or not raw['target'].strip():
                    raise ValueError(f'graph patch {field}[{i}] has unknown edge endpoint; source and target are required')
        if any(not isinstance(value, str) or not value.strip() for value in patch.cancel_nodes):
            raise ValueError('graph patch cancel_nodes requires nonempty node identities')
        return patch

    @staticmethod
    def preview_reason(governor: GlobalGovernor, estimated_cost: int) -> str:
        if len(governor.task_graph.nodes) >= governor.max_total_tasks:
            return 'max_total_tasks_exceeded'
        if governor._count_active_tasks() >= governor.max_active_tasks:
            return 'max_active_tasks_exceeded'
        if not governor.budget.can_admit_task(estimated_cost):
            return 'budget_exhausted'
        return ''

    @staticmethod
    def validate_projection(governor: GlobalGovernor, current: dict, additions: list[dict]) -> None:
        if len(current) > governor.max_total_tasks:
            raise ValueError('graph admission max_total_tasks_exceeded')
        active = sum(item.get('status', 'admitted') not in TERMINAL for item in current.values())
        if active > governor.max_active_tasks:
            raise ValueError('graph admission max_active_tasks_exceeded')
        if additions:
            estimate = sum(max(0, int((node.get('metadata') or {}).get('estimated_cost') or node.get('estimated_tokens') or 0)) for node in additions)
            remaining = governor.budget.remaining_tokens()
            if ((governor.budget.max_tokens > 0 and remaining <= 0)
                    or estimate > remaining or governor.budget.remaining_time() <= 0):
                raise ValueError('graph admission budget_exhausted')

    def apply(self, governor: GlobalGovernor, patch, *, parent_task_id=None, record_stats=True, trusted_control_nodes=()):
        # This is the same reentrant graph lock used by GraphTransaction. No
        # candidate can be validated against a graph that changes before commit.
        with governor.task_graph._mutation_lock, governor.budget._lock:
            count = len(patch.get('add_nodes', [])) if isinstance(patch, dict) and isinstance(patch.get('add_nodes', []), list) else len(getattr(patch, 'add_nodes', [])) if isinstance(getattr(patch, 'add_nodes', []), list) else 0
            try:
                candidate = self.detached_patch(patch)
                result = governor._apply_task_graph_patch(candidate, parent_task_id=parent_task_id, _trusted_control_nodes=trusted_control_nodes)
            except Exception:
                if record_stats:
                    governor.total_tasks_created += count
                    governor.total_tasks_rejected += count
                raise
            if record_stats:
                governor.total_tasks_created += count
                governor.total_tasks_admitted += len(result.get('added', []))
                governor.total_tasks_merged += count - len(result.get('added', []))
            return result

    def preview(self, governor: GlobalGovernor, patch, *, parent_task_id=None):
        with governor.task_graph._mutation_lock, governor.budget._lock:
            candidate = self.detached_patch(patch)
            return governor._apply_task_graph_patch(candidate, parent_task_id=parent_task_id, _validate_only=True)

    def evaluate(self, governor: GlobalGovernor, proposal, evaluate: Callable[[], AdmissionDecision], *, record_stats=True):
        with governor.task_graph._mutation_lock, governor.budget._lock:
            decision = evaluate()
            if record_stats:
                governor.total_proposals_evaluated += 1
                if not decision.admitted:
                    governor.total_tasks_created += 1
                    governor.total_tasks_rejected += 1
                governor.admission_history.append(decision)
            return decision
