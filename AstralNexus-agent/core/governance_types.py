"""Governance wire types and errors; no service or Agent imports."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

EVENT_SCHEMA_VERSION = 1

class EventType(str, Enum):
    """Unified event type constants for Governor event tracing.

    Replaces ad-hoc string literals with a controlled vocabulary.
    All trace_recorder.record() calls in governor.py should use these.
    Each event carries event_version for schema evolution.
    """
    # Task lifecycle
    TASK_CREATED = "governor.task_created"
    TASK_READY = "governor.task_ready"
    TASK_STARTED = "governor.task_started"
    TASK_RESULT_SUBMITTED = "governor.task_result_submitted"
    TASK_VERIFIED = "governor.task_verified"
    TASK_PROGRESS = "governor.task_progress"
    TASK_COMPLETED = "governor.task_completed"
    TASK_FAILED = "governor.task_failed"
    TASK_TIMEOUT = "governor.task_timeout"
    TASK_BLOCKED = "governor.task_blocked"
    TASK_PROPOSED = "governor.task_proposed"
    TASK_EVALUATED = "governor.task_evaluated"
    TASK_ADMITTED = "governor.task_admitted"

    # Budget
    BUDGET_UPDATED = "governor.budget_updated"
    BUDGET_EXHAUSTED = "governor.budget_exhausted"
    BUDGET_WARNING = "governor.budget_warning"

    # Contract
    DELIVERY_CONTRACT_DERIVED = "governor.delivery_contract.derived"
    DELIVERY_CONTRACT_FAILED = "governor.delivery_contract.failed"

    # Graph
    GRAPH_PATCH_PROPOSED = "governor.graph_patch.proposed"
    GRAPH_PATCH_APPLIED = "governor.graph_patch.applied"
    GRAPH_PATCH_FAILED = "governor.graph_patch.failed"
    GRAPH_PATCH_REJECTED = "governor.graph_patch.rejected"
    GRAPH_PATCH_REPAIR_PROPOSED = "governor.graph_patch.repair_proposed"
    GRAPH_PATCH_REPAIR_APPROVED = "governor.graph_patch.repair_approved"
    GRAPH_PATCH_REPAIR_APPLIED = "governor.graph_patch.repair_applied"
    TOPOLOGY_PATCH_PROPOSED = "governor.topology_patch.proposed"
    TOPOLOGY_PATCH_APPLIED = "governor.topology_patch.applied"
    TOPOLOGY_PATCH_REJECTED = "governor.topology_patch.rejected"

    # Worker selection & instantiation
    WORKER_SELECTED = "governor.worker_selected"
    WORKER_LOST = "governor.worker_lost"
    WORKER_INSTANTIATED = "governor.worker_instantiated"

    # Artifacts & evidence
    ARTIFACT_CREATED = "governor.artifact_created"
    EVIDENCE_CONFLICT = "governor.evidence_conflict"

    # Context governance
    CONTEXT_OVERFLOW = "governor.context_overflow"
    CONTEXT_WAKE = "governor.context_wake"
    CONTEXT_GOVERNANCE_DECIDED = "governor.context_governance.decided"
    CONTEXT_GOVERNANCE_FAILED = "governor.context_governance.failed"
    RETRIEVE_ON_DEMAND_SUCCESS = "governor.retrieve_on_demand.success"
    RETRIEVE_ON_DEMAND_DEFERRED = "governor.retrieve_on_demand.deferred_lookup"
    
    # External events
    USER_REQUIREMENT_CHANGED = "governor.user_requirement_changed"
    RESOURCE_UNAVAILABLE = "governor.resource_unavailable"

    # Internal governor lifecycle
    INITIALIZED = "governor.initialized"
    WORKER_MATCHER_ENABLED = "governor.worker_matcher.enabled"
    WORKER_MATCHER_NO_MATCH = "governor.worker_matcher.no_match"
    WORKER_MATCHER_DECIDED = "governor.worker_matcher.decided"
    WORKER_MATCHER_FAILED = "governor.worker_matcher.failed"
    WORKER_MATCHER_REJECTED = "governor.worker_matcher.rejected"
    WORKER_REJECTED_ZERO_MATCH = "governor.worker_rejected_zero_match"
    GRAPH_PATCH_DEP_SKIPPED = "governor.graph_patch.dep_skipped"
    GRAPH_PATCH_EDGE_SKIPPED = "governor.graph_patch.edge_skipped"
    GRAPH_PATCH_STATUS_SKIPPED = "governor.graph_patch.status_skipped"
    DELIVERY_VERIFICATION_LLM_FAILED = "governor.delivery_verification_llm.failed"
    BACKGROUND_SPAWN_DECIDED = "governor.background_spawn_decided"
    CAPABILITY_GAP = "governor.capability_gap"

    # Tool loop guard
    TOOL_LOOP_RECOVERY = "governor.tool_loop_recovery"
    TOOL_LOOP_REPLAN = "governor.tool_loop_replan"

    # Verification
    VERIFIER_COMPLETED = "governor.global_verifier.completed"
    DELIVERY_VERIFIED = "governor.delivery_verified"
    DELIVERY_READY = "governor.delivery_ready"
    DELIVERY_REJECTED = "governor.delivery_rejected"
    DELIVERY_GATE = "governor.delivery_gate"
    SYNTHESIS_VERIFICATION = "governor.global_verification.completed"
    SYNTHESIS_VERIFICATION_FAILED = "governor.global_verification.failed"
    VERIFICATION_FAILED = "governor.verification_failed"

    # Termination
    TERMINATION_REQUESTED = "governor.termination_requested"
    TERMINATION_DECIDED = "governor.termination_decided"
    ALL_TASKS_COMPLETED = "governor.all_tasks_completed"

    @classmethod
    def payload(cls, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Wrap event data with standard versioning fields."""
        return {
            "event_version": EVENT_SCHEMA_VERSION,
            "event_type": str(getattr(event_type, "value", event_type)),
            **data,
        }


@dataclass
class AdmissionDecision:
    """Result of task admission evaluation."""
    admitted: bool
    value_score: float
    reasoning: str
    task_id: Optional[str] = None
    rejection_reason: Optional[str] = None


@dataclass
class TaskGraphPatch:
    """Incremental graph change proposed by the Governor planner."""

    add_nodes: List[Dict[str, Any]] = field(default_factory=list)
    update_nodes: List[Dict[str, Any]] = field(default_factory=list)
    cancel_nodes: List[str] = field(default_factory=list)
    add_edges: List[Dict[str, Any]] = field(default_factory=list)
    remove_edges: List[Dict[str, Any]] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)
    reasoning: str = ""
    background_candidates: List[Dict[str, Any]] = field(default_factory=list)
    candidate_mode: str = "merge"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "add_nodes": self.add_nodes,
            "update_nodes": self.update_nodes,
            "cancel_nodes": self.cancel_nodes,
            "add_edges": self.add_edges,
            "remove_edges": self.remove_edges,
            "reason_codes": self.reason_codes,
            "reasoning": self.reasoning,
            "background_candidates": self.background_candidates,
            "candidate_mode": self.candidate_mode,
        }


class ContextGovernanceUnavailable(RuntimeError):
    """Presentation planning was unavailable; this is not an acceptance verdict."""


class WorkerMatcherTransientError(RuntimeError):
    """Transient Worker Matcher fault: malformed model output or transport error.

    Distinct from governance violations (hallucinated profiles) so
    ``select_worker_profile`` can degrade to the deterministic capability
    validator instead of blocking a healthy node.  Live r13c evidence
    (2026-09-29T11:35:29Z, generation 10): one "worker matcher returned
    non-object" response raised straight out of the selector; the orchestrator
    blocked running node N-122 before any attempt, one malformed fault-recovery
    proposal escalated to terminal ``recovery_exhausted``, and N-119 was
    dragged into ``node_blocked_by_terminal``.
    """


class WorkerMatcherProfileError(RuntimeError):
    """The matcher named a profile that is not in the registered catalogue.

    A hallucination is a governance violation, never a transient fault: it
    fails immediately without retry and without deterministic fallback.
    """

