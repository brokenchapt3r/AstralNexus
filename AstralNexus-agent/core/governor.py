"""Global Governor: manages global task graph governance, budget, and termination.

The Global Governor is responsible for:
- Maintaining global objectives and constraints
- Approving new task node creation (dynamic admission)
- Managing global budget (tokens, time, parallel slots)
- Controlling maximum concurrency and recursion depth
- Handling cross-subgraph dependencies
- Deciding final termination conditions

This implements the "Global Governor" layer from the advanced improvement spec.
"""
from __future__ import annotations 

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from AstralNexus.core.capability_ontology import (
    CATEGORY_WORKER_TYPES,
    IMPLEMENTATION_CAPABILITIES,
    IMPLEMENTATION_MARKERS,
    RETRIEVAL_MARKERS,
    contains_marker,
    score_capability_categories,
)
from AstralNexus.core.graph_patch_engine import (
    GraphCycleError,
    GraphPatchCommitter,
    GraphPatchValidator,
)
from AstralNexus.core.governor_policy import GovernorPolicy
from AstralNexus.core.policy_rule_engine import PolicyRuleEngine
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.state_machine import TaskStatus, can_transition, normalize_status
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.core.output_paths import normalize_worker_output_path


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
class TaskProposal:
    """A candidate task proposed for admission to the task graph."""
    goal: str
    required_capabilities: List[str]
    expected_information_gain: float
    risk_reduction: float
    parallel_benefit: float
    estimated_cost: int
    dependencies: List[str]
    parent_task_id: Optional[str] = None
    preferred_node_id: Optional[str] = None
    acceptance_criteria: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    constraints: List[str] = field(default_factory=list)


@dataclass
class AdmissionDecision:
    """Result of task admission evaluation."""
    admitted: bool
    value_score: float
    reasoning: str
    task_id: Optional[str] = None
    rejection_reason: Optional[str] = None


@dataclass
class InteractiveRouteDecision:
    """Governor decision for an ordinary interactive user turn."""

    mode: str
    complexity_score: float
    parallel_benefit: float
    required_capabilities: List[str] = field(default_factory=list)
    estimated_nodes: int = 1
    needs_artifacts: bool = False
    needs_verification: bool = False
    reasoning: str = ""
    task_contract_summary: str = ""
    reason_codes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "complexity_score": self.complexity_score,
            "parallel_benefit": self.parallel_benefit,
            "required_capabilities": list(self.required_capabilities),
            "estimated_nodes": self.estimated_nodes,
            "needs_artifacts": self.needs_artifacts,
            "needs_verification": self.needs_verification,
            "reasoning": self.reasoning,
            "task_contract_summary": self.task_contract_summary,
            "reason_codes": list(self.reason_codes),
        }


@dataclass
class GlobalBudget:
    """Global resource budget tracking."""
    # Zero means that formal product execution is evidence-driven rather than
    # stopped by an arbitrary global token or wall-clock ceiling.  Benchmark
    # and deployment profiles may still provide explicit positive limits.
    max_tokens: int = 0
    max_time_seconds: int = 0
    max_parallel_slots: int = 8
    max_recursion_depth: int = 256
    used_tokens: int = 0
    used_time_seconds: float = 0.0
    active_parallel_tasks: int = 0
    
    def remaining_tokens(self) -> int:
        if self.max_tokens <= 0:
            return 2**63 - 1
        return max(0, self.max_tokens - self.used_tokens)
    
    def remaining_time(self) -> float:
        if self.max_time_seconds <= 0:
            return float(2**63 - 1)
        return max(0.0, self.max_time_seconds - self.used_time_seconds)
    
    def available_parallel_slots(self) -> int:
        return max(0, self.max_parallel_slots - self.active_parallel_tasks)
    
    def can_admit_task(self, estimated_cost: int) -> bool:
        """Check if budget allows admitting a new task."""
        return (
            self.remaining_tokens() >= estimated_cost
            and self.remaining_time() > 0
            and self.available_parallel_slots() > 0
        )


@dataclass
class ContextGovernanceDecision:
    """Task-independent working-set decision made by the Global Governor."""

    groups: List[Dict[str, Any]] = field(default_factory=list)
    retained_packet_ids: List[str] = field(default_factory=list)
    compressed_packet_ids: List[str] = field(default_factory=list)
    deferred_packet_ids: List[str] = field(default_factory=list)
    delivery_contract: Dict[str, Any] = field(default_factory=dict)
    action_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    task_version: int = 1
    reason_codes: List[str] = field(default_factory=list)
    reasoning: str = ""


@dataclass
class GoalSpec:
    """Versioned, domain-neutral representation of the user objective."""

    goal_id: str
    task_goal: str
    normalized_goal: str
    hard_constraints: List[str] = field(default_factory=list)
    soft_preferences: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    forbidden_claims: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    task_version: int = 1
    needs_clarification: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "task_goal": self.task_goal,
            "normalized_goal": self.normalized_goal,
            "hard_constraints": self.hard_constraints,
            "soft_preferences": self.soft_preferences,
            "success_criteria": self.success_criteria,
            "forbidden_claims": self.forbidden_claims,
            "uncertainties": self.uncertainties,
            "assumptions": self.assumptions,
            "task_version": self.task_version,
            "needs_clarification": self.needs_clarification,
        }


@dataclass
class DeliveryContract:
    """Structured delivery contract with artifact binding and version tracking."""
    
    goal_id: str
    task_version: int
    delivery_mode: str = "mixed"
    # The Governor decides whether the task is primarily an artifact
    # execution or a user-readable response.  This is intentionally not a
    # domain enum: a task may produce any artifact types selected at runtime.
    delivery_strategy: str = "model_decided"
    summary_required: bool = True
    execution_evidence_policy: str = "mixed"
    required_outputs: List[Dict[str, Any]] = field(default_factory=list)
    required_sections: List[str] = field(default_factory=list)
    recommended_sections: List[str] = field(default_factory=list)
    hard_constraints: List[str] = field(default_factory=list)
    soft_preferences: List[str] = field(default_factory=list)
    global_acceptance_criteria: List[str] = field(default_factory=list)
    completion_evidence: List[str] = field(default_factory=list)
    forbidden_claims: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    needs_clarification: bool = False
    needs_citations: bool = False
    needs_artifact_refs: bool = False
    
    def bind_artifact_paths(self, artifact_map: Dict[str, str]) -> None:
        """Bind real artifact paths to required outputs.
        
        Args:
            artifact_map: Mapping from output_id to artifact path
        """
        for output in self.required_outputs:
            output_id = output.get("output_id")
            if output_id and output_id in artifact_map:
                output["artifact_path"] = artifact_map[output_id]
                output["artifact_verified"] = True
    
    def as_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "task_version": self.task_version,
            "delivery_mode": self.delivery_mode,
            "delivery_strategy": self.delivery_strategy,
            "summary_required": self.summary_required,
            "execution_evidence_policy": self.execution_evidence_policy,
            "required_outputs": self.required_outputs,
            "required_sections": self.required_sections,
            "recommended_sections": self.recommended_sections,
            "hard_constraints": self.hard_constraints,
            "soft_preferences": self.soft_preferences,
            "global_acceptance_criteria": self.global_acceptance_criteria,
            "completion_evidence": self.completion_evidence,
            "forbidden_claims": self.forbidden_claims,
            "uncertainties": self.uncertainties,
            "assumptions": self.assumptions,
            "needs_clarification": self.needs_clarification,
            "needs_citations": self.needs_citations,
            "needs_artifact_refs": self.needs_artifact_refs,
        }


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
        }


def _parse_governor_json(text: str) -> Any:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for offset, character in enumerate(value):
            if character not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(value[offset:])
            except json.JSONDecodeError:
                continue
            return parsed
        return None


def strings_from(value: Any) -> List[str]:
    """Normalize a JSON list of scalar values into non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
class GlobalGovernor:
    """Global governance layer for task graph management.
    
    Responsibilities:
    - Task admission: evaluate and approve/reject candidate tasks
    - Budget management: track and enforce global resource limits
    - Termination control: decide when to stop execution
    - Cross-subgraph coordination: handle dependencies across subgraphs
    """
    
    def __init__(
        self,
        task_graph: TaskGraph,
        trace_recorder: TraceRecorder,
        *,
        admission_threshold: float = 0.65,
        max_active_tasks: int = 2000,
        max_total_tasks: int = 10000,
        llm_client: Any = None,
        policy: Optional[GovernorPolicy] = None,
    ):
        self.policy = policy or GovernorPolicy(
            admission_threshold=admission_threshold,
            max_active_tasks=max_active_tasks,
            max_total_tasks=max_total_tasks,
        )
        self.rule_engine = PolicyRuleEngine(self.policy)
        self.task_graph = task_graph
        self.trace_recorder = trace_recorder
        self.admission_threshold = self.policy.admission_threshold
        self.max_active_tasks = self.policy.max_active_tasks
        self.max_total_tasks = self.policy.max_total_tasks
        
        # Budget tracking
        self.budget = GlobalBudget()
        
        # Task tracking
        self.total_tasks_created = 0
        self.total_tasks_admitted = 0
        self.total_tasks_rejected = 0
        self.admission_history: List[AdmissionDecision] = []
        
        # Global state
        self.goal_version = 1  # Incremented when goal changes
        self.termination_requested = False
        self.goal_spec: Optional[GoalSpec] = None
        self.delivery_contract: Dict[str, Any] = {}
        self.context_decisions: List[ContextGovernanceDecision] = []
        # Optional real-LLM control-plane matcher.  Unit tests and embedded
        # callers can omit it; the formal CLI enables it explicitly. The
        # deterministic catalogue scorer remains a candidate-ranking and
        # validation layer, never a hidden replacement for a failed LLM
        # control-plane decision.
        self.llm_client = llm_client
        self.llm_matching_enabled = llm_client is not None
        self.forced_worker_model: Optional[str] = None
        
        self.trace_recorder.record(EventType.INITIALIZED, {
            "admission_threshold": self.admission_threshold,
            "max_active_tasks": self.max_active_tasks,
            "max_total_tasks": self.max_total_tasks,
        })

    def enable_llm_worker_matching(self, llm_client: Any) -> None:
        """Enable the real-LLM Worker Profile decision stage."""
        if llm_client is None or not callable(getattr(llm_client, "chat", None)):
            raise ValueError("A real LLM client with chat() is required")
        self.llm_client = llm_client
        self.llm_matching_enabled = True
        self.trace_recorder.record(EventType.WORKER_MATCHER_ENABLED, {
            "mode": "real_llm_control_plane",
        })

    def set_forced_worker_model(self, model: Optional[str]) -> None:
        """Apply an explicit operator override without confusing it with the
        Governor model used for planning and matching.
        """
        self.forced_worker_model = str(model or "").strip() or None

    def decide_interactive_route(
        self,
        user_input: str,
        *,
        conversation_context: str = "",
        active_run: bool = False,
    ) -> InteractiveRouteDecision:
        """Choose direct conversation or a governed heterogeneous task graph.

        The real model estimates semantic task characteristics.  Governor then
        validates the structured decision so a high-complexity, multi-capability
        delivery cannot silently fall back to the single interactive tool loop.
        """
        if self.llm_client is None or not callable(getattr(self.llm_client, "chat", None)):
            raise RuntimeError("interactive routing requires a real LLM client")
        prompt = (
            "You are the Global Governor of AstralNexus. Decide how to route the "
            "current user turn. direct_chat is for questions, explanations, tiny "
            "lookups, or one bounded edit that does not benefit from independent "
            "planning and verification. governed_run is for multi-step engineering, "
            "research, creation, data work, or delivery that spans capabilities, "
            "creates artifacts, needs tests/review, has dependencies, or benefits "
            "from parallel heterogeneous workers. requirement_change is only for a "
            "new requirement that must alter the currently active governed run.\n"
            "Return one JSON object with exactly these fields:\n"
            '{"mode":"direct_chat|governed_run|requirement_change",'
            '"complexity_score":0.0,"parallel_benefit":0.0,'
            '"required_capabilities":["capability"],"estimated_nodes":1,'
            '"needs_artifacts":false,"needs_verification":false,'
            '"reasoning":"concise reason","task_contract_summary":"concise objective"}\n'
            f"active_run={str(bool(active_run)).lower()}\n"
            f"recent_context={conversation_context[-self.policy.interactive_context_chars:]}\n"
            f"user_turn={user_input}"
        )
        def record_control_event(event: Dict[str, Any]) -> None:
            payload = dict(event)
            event_type = str(payload.pop("event_type", "llm.control"))
            payload.setdefault("node_id", "interactive-route")
            payload.setdefault("control_plane", "interactive_route")
            self.trace_recorder.record(event_type, payload)

        raw = self.llm_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Perform control-plane routing only. Do not solve the task. "
                        "Use task semantics, not a fixed domain keyword list."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format="json",
            max_tokens=self.policy.interactive_response_tokens,
            temperature=0.0,
            tools=None,
            event_callback=record_control_event,
        )
        payload = _parse_governor_json(raw)
        if not isinstance(payload, dict):
            self.trace_recorder.record("governor.interactive_route.failed", {
                "reason": "invalid_json",
                "response_preview": str(raw)[:1000],
            })
            raise ValueError("Governor interactive route returned invalid JSON")

        enforced = self.rule_engine.evaluate_interactive_route(
            payload,
            active_run=active_run,
        )
        if not enforced["task_contract_summary"]:
            enforced["task_contract_summary"] = user_input.strip()
        decision = InteractiveRouteDecision(**enforced)
        self.trace_recorder.record("governor.interactive_route.decided", decision.as_dict())
        return decision

    def _llm_worker_match(
        self,
        task: Dict[str, Any],
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Ask the real LLM to choose among registered Worker Profiles.

        The model is a decision-maker, not a registry.  Its answer is
        accepted only when it names an existing profile; capability coverage,
        resource availability and contracts remain code-enforced below.
        """
        if not self.llm_matching_enabled or self.llm_client is None:
            return None
        catalogue = [
            {
                "worker_type": item.get("worker_type"),
                "capabilities": item.get("capabilities", []),
                "tools": item.get("tools", []),
                "model_preference": item.get("model_preference"),
                "resource_tier": item.get("resource_tier"),
                "capabilities": item.get("capabilities", []),
                "input_contract": item.get("input_contract", ""),
                "output_contract": item.get("output_contract", ""),
                "reasoning_level": item.get("reasoning_level", "medium"),
            }
            for item in candidates
        ]
        prompt = json.dumps({
            "task": {
                "task_id": task.get("task_id") or task.get("node_id"),
                "goal": task.get("goal", ""),
                "required_capabilities": task.get("required_capabilities", []),
                "execution_target": task.get("execution_target") or task.get("resource_tier"),
                "risk_level": task.get("risk_level", "medium"),
            },
            "registered_worker_profiles": catalogue,
            "output_schema": {
                "worker_type": "existing profile name only",
                "reason_codes": "list of short reasons",
                "confidence": "number 0..1",
            },
        }, ensure_ascii=False)
        try:
            text = self.llm_client.chat([
                {
                    "role": "system",
                    "content": (
                        "你是天枢 AstralNexus Global Governor 的 Worker Matcher。"
                        "只能从已注册 Profile 中选择，不能虚构角色、模型或工具。"
                        "优先选择能力字段有明确匹配的 Profile；如果任务使用了目录中没有的领域能力词，"
                        "必须结合任务目标、输入输出契约、工具和 Profile 描述选择最接近且可安全承接的已注册 Profile，"
                        "并在 reason_codes 中标记 SEMANTIC_PROFILE_MATCH。只有所有已注册 Profile 都无法安全承接时，"
                        "才返回 worker_type=null，以便 Governor 补充能力或重规划。不得虚构能力覆盖，不得把猜测写成已验证事实。"
                        "只输出 JSON。"
                    ),
                },
                {"role": "user", "content": prompt},
            ])
            parsed = _parse_governor_json(text)
            if not isinstance(parsed, dict):
                raise ValueError("worker matcher returned non-object")
            worker_type = str(parsed.get("worker_type") or "").strip()
            if not worker_type:
                self.trace_recorder.record(EventType.WORKER_MATCHER_NO_MATCH, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "reason_codes": parsed.get("reason_codes", []),
                })
                return {
                    "worker_type": None,
                    "reason_codes": [
                        str(x) for x in parsed.get("reason_codes", [])
                        if str(x).strip()
                    ],
                    "confidence": max(
                        0.0,
                        min(1.0, float(parsed.get("confidence", 0.0) or 0.0)),
                    ),
                }
            selected = next(
                (item for item in candidates if str(item.get("worker_type")) == worker_type),
                None,
            )
            if selected is None:
                raise ValueError(f"unknown worker profile: {worker_type}")
            decision = {
                "worker_type": worker_type,
                "reason_codes": [str(x) for x in parsed.get("reason_codes", []) if str(x).strip()],
                "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0))),
            }
            self.trace_recorder.record(EventType.WORKER_MATCHER_DECIDED, {
                "task_id": task.get("task_id") or task.get("node_id"),
                "decision": decision,
            })
            return decision
        except Exception as exc:
            self.trace_recorder.record(EventType.WORKER_MATCHER_FAILED, {
                "task_id": task.get("task_id") or task.get("node_id"),
                "error": str(exc)[:500],
            })
            raise RuntimeError(
                f"real LLM Worker Matcher failed: {str(exc)[:500]}"
            ) from exc

    def admit_root_goal(
        self,
        *,
        node_id: str,
        goal: str,
        constraints: List[str],
        intent_node_id: str = "U-001",
    ) -> Dict[str, Any]:
        """Admit immutable user intent and its control-plane planner atomically."""
        return self.apply_task_graph_patch(
            TaskGraphPatch(
                add_nodes=[
                    {
                        "node_id": intent_node_id,
                        "goal": goal,
                        "role": "user_intent",
                        "status": TaskStatus.COMPLETED.value,
                        "constraints": list(constraints),
                        "dependencies": [],
                        "metadata": {
                            "type": "user_intent",
                            "plane": "control",
                            "source": "user",
                            "immutable": True,
                        },
                    },
                    {
                        "node_id": node_id,
                        "goal": goal,
                        "role": "planner",
                        "status": TaskStatus.ADMITTED.value,
                        "constraints": list(constraints),
                        "dependencies": [intent_node_id],
                        "metadata": {
                            "type": "root",
                            "plane": "control",
                            "source": "governor_planning",
                        },
                    }
                ],
                reason_codes=["USER_GOAL_ADMISSION"],
                reasoning=(
                    "将用户原始需求冻结为不可变意图节点，并创建独立的控制平面"
                    " Planner 生成后续动态任务图。"
                ),
            )
        )

    def apply_task_graph_patch(
        self,
        patch: TaskGraphPatch | Dict[str, Any],
        *,
        parent_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate and atomically apply a Governor graph patch.

        The planner only proposes changes.  This method is the sole mutation
        boundary for incremental planning: it merges duplicates, applies all
        node/edge operations, and rejects the complete patch when the proposed
        graph is cyclic or references unknown nodes.
        """
        if isinstance(patch, dict):
            patch = TaskGraphPatch(
                add_nodes=list(patch.get("add_nodes", [])),
                update_nodes=list(patch.get("update_nodes", [])),
                cancel_nodes=[str(x) for x in patch.get("cancel_nodes", [])],
                add_edges=list(patch.get("add_edges", [])),
                remove_edges=list(patch.get("remove_edges", [])),
                reason_codes=[str(x) for x in patch.get("reason_codes", [])],
                reasoning=str(patch.get("reasoning", "")),
                background_candidates=list(patch.get("background_candidates", [])),
            )

        node_count_before = len(self.task_graph.nodes)
        edge_count_before = len(self.task_graph.edges)
        current = {
            node_id: {
                "status": node.status,
                "goal": node.goal,
                "metadata": dict(node.metadata),
                "dependencies": list(node.dependencies),
            }
            for node_id, node in self.task_graph.nodes.items()
        }
        edge_type_aliases = {
            "dependency": "depends_on",
            "communicates": "communicates_to",
        }
        allowed_edge_types = {
            "depends_on",
            "communicates_to",
            "evidence_for",
            "produces",
            "revises",
            "verifies",
            "blocks",
            "supersedes",
        }

        def normalize_edge_type(value: Any) -> str:
            edge_type = str(value or "depends_on").strip()
            edge_type = edge_type_aliases.get(edge_type, edge_type)
            if edge_type not in allowed_edge_types:
                raise ValueError(f"unsupported graph edge type: {edge_type}")
            return edge_type

        def edge_key(item: Dict[str, Any]) -> tuple[str, str, str]:
            return (
                str(item.get("source", "")),
                str(item.get("target", "")),
                normalize_edge_type(item.get("edge_type", "depends_on")),
            )

        proposed_edges = {
            (edge.source, edge.target, edge.edge_type): edge
            for edge in self.task_graph.edges
        }
        for raw in patch.remove_edges:
            if isinstance(raw, dict):
                proposed_edges.pop(edge_key(raw), None)

        id_map: Dict[str, str] = {}
        additions: List[Dict[str, Any]] = []
        next_index = len(current) + 1
        for raw in patch.add_nodes:
            if not isinstance(raw, dict) or not str(raw.get("goal", "")).strip():
                continue
            requested = str(raw.get("node_id") or raw.get("task_id") or "").strip()
            caps = [str(x) for x in raw.get("required_capabilities", []) if str(x).strip()]
            metadata = dict(raw.get("metadata") or {})
            node_type = str(metadata.get("type") or "").lower()
            role = str(raw.get("role") or "").lower()
            is_control_node = (
                metadata.get("plane") == "control"
                or node_type in {"user_intent", "root", "planning_root"}
                or role in {"user_intent", "planner", "governor"}
            )
            duplicate = None if is_control_node else GraphPatchValidator.find_duplicate(
                str(raw["goal"]),
                caps,
                current,
                similarity_threshold=self.policy.duplicate_similarity_threshold,
            )
            if duplicate:
                if requested:
                    id_map[requested] = duplicate
                continue
            node_id = requested or f"dynamic-{next_index:04d}"
            while node_id in current:
                next_index += 1
                node_id = f"dynamic-{next_index:04d}"
            next_index += 1
            normalized = dict(raw)
            normalized["node_id"] = node_id
            normalized["dependencies"] = [str(x) for x in raw.get("dependencies", [])]
            normalized.setdefault("role", "governed_worker")
            normalized.setdefault("status", "admitted")
            normalized["status"] = normalize_status(normalized["status"])
            valid_statuses = {status.value for status in TaskStatus}
            if normalized["status"] not in valid_statuses:
                raise ValueError(
                    f"invalid node status in graph patch: {normalized['status']} ({node_id})"
                )
            normalized["metadata"] = {
                **dict(raw.get("metadata") or {}),
                "required_capabilities": caps,
                "task_type": raw.get("task_type", "other"),
                "expected_outputs": [
                    normalize_worker_output_path(item)
                    for item in (raw.get("expected_outputs", []) or [])
                    if str(item).strip()
                ],
                "source_requirements": [
                    str(item) for item in (raw.get("source_requirements", []) or [])
                    if str(item).strip()
                ],
                "derivation": str(raw.get("derivation") or "").strip(),
                "priority": raw.get("priority", 50),
                "criticality": raw.get("criticality", "medium"),
                "execution_target": raw.get("execution_target", "cloud"),
                "budget": dict(raw.get("budget") or {}),
                "created_by": "global_governor",
                "requested_by": str(
                    (raw.get("metadata") or {}).get("requested_by")
                    or raw.get("requested_by")
                    or "GlobalGovernor"
                ),
                "submitted_by": str(
                    (raw.get("metadata") or {}).get("submitted_by")
                    or raw.get("submitted_by")
                    or "Orchestrator"
                ),
                "approved_by": "GlobalGovernor",
                "applied_by": "GlobalGovernor.apply_task_graph_patch",
                "scheduled_by": "Orchestrator/Router",
                "task_version": self.goal_version,
                "reason_codes": list(patch.reason_codes),
            }
            current[node_id] = {
                "status": normalized["status"], "goal": normalized["goal"],
                "metadata": normalized["metadata"], "dependencies": list(normalized["dependencies"]),
            }
            additions.append(normalized)
            if requested:
                id_map[requested] = node_id

        all_ids = set(current)
        root_aliases = {
            "root", "root_node", "root_task", "N-001", "T-001", "P-001",
            "node-001", "task-001",
        }

        def remap(value: Any) -> str:
            candidate = id_map.get(str(value), str(value))
            # LLM planners sometimes use a local graph alias for the root.
            # Resolve it only when the real parent is supplied and the alias
            # is not itself an actual node; arbitrary unknown dependencies
            # must still be rejected below.
            if parent_task_id and candidate not in all_ids and candidate in root_aliases:
                return parent_task_id
            return candidate

        for node in additions:
            node["dependencies"] = [remap(x) for x in node.get("dependencies", [])]
            if parent_task_id and not node["dependencies"] and parent_task_id in all_ids:
                node["dependencies"] = [parent_task_id]
            resolved_deps = []
            for dep in node["dependencies"]:
                if dep not in all_ids:
                    self.trace_recorder.record(EventType.GRAPH_PATCH_DEP_SKIPPED, {
                        "node_id": node["node_id"], "unknown_dep": dep,
                        "reason": "unknown_dependency",
                    })
                    raise ValueError(f"graph patch references unknown dependency: {dep}")
                resolved_deps.append(dep)
            node["dependencies"] = resolved_deps
            for dep in resolved_deps:
                proposed_edges[(dep, node["node_id"], "depends_on")] = {"source": dep, "target": node["node_id"], "edge_type": "depends_on", "metadata": {"created_by": "global_governor"}}

        # Reject "parallel in prose, serial in topology" plans before they can
        # monopolize a whole run behind one oversized first node.  This is a
        # domain-neutral graph quality gate: it only applies when Governor
        # explicitly claims parallel value and the sole initial node combines
        # several capabilities, artifacts and acceptance boundaries.
        addition_ids = {node["node_id"] for node in additions}
        initial_frontier = [
            node for node in additions
            if not any(dep in addition_ids for dep in node.get("dependencies", []))
        ]
        claims_parallel = any(
            str(code).upper() == "PARALLELIZABLE" for code in patch.reason_codes
        )
        bottleneck = initial_frontier[0] if len(initial_frontier) == 1 else None
        bottleneck_is_wide = bool(
            bottleneck
            and len(bottleneck.get("metadata", {}).get("required_capabilities", []))
            >= self.policy.false_parallel_wide_capabilities
            and len(bottleneck.get("metadata", {}).get("expected_outputs", []))
            >= self.policy.false_parallel_wide_outputs
            and len(bottleneck.get("acceptance_criteria", []))
            >= self.policy.false_parallel_wide_acceptance
        )
        topology_quality = {
            "add_node_count": len(additions),
            "initial_frontier_width": len(initial_frontier),
            "initial_frontier": [node["node_id"] for node in initial_frontier],
            "claims_parallel": claims_parallel,
            "bottleneck_node": bottleneck["node_id"] if bottleneck_is_wide else None,
        }
        self.trace_recorder.record(
            "governor.graph_patch.topology_evaluated", topology_quality
        )
        if (
            len(additions) >= self.policy.false_parallel_min_nodes
            and claims_parallel
            and len(initial_frontier) < self.policy.false_parallel_min_frontier
            and bottleneck_is_wide
        ):
            self.trace_recorder.record(
                EventType.GRAPH_PATCH_REJECTED,
                {
                    **topology_quality,
                    "reason": "false_parallelism_single_wide_bottleneck",
                    "reason_codes": list(patch.reason_codes),
                },
            )
            raise ValueError(
                "graph patch claims PARALLELIZABLE but creates one oversized "
                "initial bottleneck; split independent capability/artifact/"
                "acceptance boundaries so at least two initial nodes can run, "
                "or remove PARALLELIZABLE and justify the true dependency"
            )

        proposed_communication_links: List[Dict[str, str]] = []
        for raw in patch.add_edges:
            if not isinstance(raw, dict):
                continue
            source, target = remap(raw.get("source")), remap(raw.get("target"))
            # Resolve root aliases that remap() may not have caught when
            # the alias is in root_aliases but the real parent node ID is
            # not in all_ids yet (e.g. the LLM used "T-001" to refer to
            # a parent that was created outside this patch).
            if source not in all_ids and source in root_aliases and parent_task_id:
                source = parent_task_id
            if target not in all_ids and target in root_aliases and parent_task_id:
                target = parent_task_id
            if source not in all_ids or target not in all_ids:
                self.trace_recorder.record(EventType.GRAPH_PATCH_EDGE_SKIPPED, {
                    "source": source, "target": target,
                    "reason": "unknown_endpoint",
                    "known_ids_sample": sorted(list(all_ids))[:20],
                })
                raise ValueError(
                    f"graph patch references unknown edge endpoint: {source} -> {target}"
                )
            if source == target:
                raise ValueError(f"graph patch self-cycle: {source}")
            edge_type = normalize_edge_type(raw.get("edge_type", "depends_on"))
            item = {
                "source": source,
                "target": target,
                "edge_type": edge_type,
                "metadata": {
                    **dict(raw.get("metadata") or {}),
                    "created_by": "global_governor",
                    "task_version": self.goal_version,
                },
            }
            proposed_edges[(source, target, item["edge_type"])] = item
            if edge_type != "depends_on":
                proposed_communication_links.append({
                    "source": source,
                    "target": target,
                    "edge_type": edge_type,
                })

        if proposed_communication_links:
            self.trace_recorder.record(
                EventType.TOPOLOGY_PATCH_PROPOSED,
                {
                    "add_links": proposed_communication_links,
                    "reason_codes": patch.reason_codes,
                    "planning_explanation": patch.reasoning,
                    "task_version": self.goal_version,
                },
            )

        for raw in patch.update_nodes:
            if not isinstance(raw, dict):
                continue
            node_id = remap(raw.get("node_id") or raw.get("task_id"))
            if node_id not in all_ids:
                raise ValueError(f"graph patch updates unknown node: {node_id}")
            if "status" in raw:
                current_status = normalize_status(current[node_id]["status"])
                target_status = normalize_status(raw["status"])
                # TaskGraph state updates are idempotent.  A Governor repair
                # patch may restate the current state while changing evidence,
                # dependencies, or acceptance criteria; that is not a lifecycle
                # transition and must agree with TaskGraph.assert_transition.
                if current_status != target_status and not can_transition(
                    current_status, target_status
                ):
                    self.trace_recorder.record(EventType.GRAPH_PATCH_STATUS_SKIPPED, {
                        "node_id": node_id,
                        "current_status": current_status,
                        "target_status": target_status,
                        "reason": "invalid_state_transition",
                    })
                    raise ValueError(
                        f"graph patch has invalid state transition: "
                        f"{current_status} -> {target_status} ({node_id})"
                    )
                else:
                    current[node_id]["status"] = target_status
            if "dependencies" in raw:
                deps = [remap(x) for x in raw.get("dependencies", [])]
                resolved_deps = []
                for dep in deps:
                    if dep not in all_ids:
                        self.trace_recorder.record(EventType.GRAPH_PATCH_DEP_SKIPPED, {
                            "node_id": node_id, "unknown_dep": dep,
                            "reason": "update_unknown_dependency",
                        })
                        raise ValueError(
                            f"graph patch update references unknown dependency: {dep}"
                        )
                    resolved_deps.append(dep)
                current[node_id]["dependencies"] = resolved_deps
                proposed_edges = {key: edge for key, edge in proposed_edges.items() if not (key[1] == node_id and key[2] == "depends_on")}
                for dep in resolved_deps:
                    proposed_edges[(dep, node_id, "depends_on")] = {"source": dep, "target": node_id, "edge_type": "depends_on", "metadata": {"created_by": "global_governor"}}

        # Validate the complete proposal without altering its semantics.
        # A cyclic LLM proposal must be rejected and replanned; silently
        # deleting one edge can change evidence and verification dependencies.
        cycle_repairs: List[Dict[str, Any]] = []
        try:
            GraphPatchValidator.assert_acyclic(all_ids, proposed_edges)
        except GraphCycleError:
            self.trace_recorder.record(
                EventType.GRAPH_PATCH_REPAIR_PROPOSED,
                {
                    "reason": "dependency_cycle_detected",
                    "new_node_ids": sorted(addition_ids),
                    "reason_codes": patch.reason_codes,
                },
            )
            if not self.policy.allow_safe_cycle_repair:
                self.trace_recorder.record(
                    EventType.GRAPH_PATCH_REJECTED,
                    {
                        "reason": "cycle_repair_disabled",
                        "reason_codes": patch.reason_codes,
                    },
                )
                raise
            try:
                proposed_edges, cycle_repairs = (
                    GraphPatchValidator.repair_new_node_cycles(
                        all_ids,
                        proposed_edges,
                        new_node_ids=addition_ids,
                        max_repairs=self.policy.max_cycle_repairs_per_patch,
                    )
                )
            except GraphCycleError as repair_error:
                self.trace_recorder.record(
                    EventType.GRAPH_PATCH_REJECTED,
                    {
                        "reason": "cycle_not_safely_repairable",
                        "error": str(repair_error),
                        "reason_codes": patch.reason_codes,
                    },
                )
                raise
            for repair in cycle_repairs:
                source = repair["source"]
                target = repair["target"]
                if target in current:
                    current[target]["dependencies"] = [
                        dep for dep in current[target]["dependencies"]
                        if dep != source
                    ]
                for addition in additions:
                    if addition["node_id"] == target:
                        addition["dependencies"] = [
                            dep for dep in addition.get("dependencies", [])
                            if dep != source
                        ]
            self.trace_recorder.record(
                EventType.GRAPH_PATCH_REPAIR_APPROVED,
                {
                    "repairs": cycle_repairs,
                    "policy": "new_nodes_soft_dependencies_only",
                    "reason_codes": patch.reason_codes,
                },
            )

        # Validate cancellation transitions before committing any operation.
        # Terminal nodes are already converged and remain unchanged; every
        # cancellable active node must pass the canonical state machine.
        terminal_statuses = {"completed", "failed", "cancelled", "superseded"}
        cancelled_ids: set[str] = set()
        for raw_id in patch.cancel_nodes:
            node_id = remap(raw_id)
            node = self.task_graph.get_node(node_id)
            if node is None:
                continue
            cancelled_ids.add(node_id)
            current_status = normalize_status(current[node_id]["status"])
            if current_status not in terminal_statuses and not can_transition(current_status, "cancelled"):
                raise ValueError(
                    f"graph patch has invalid cancellation transition: "
                    f"{current_status} -> cancelled ({node_id})"
                )
        if cancelled_ids:
            # A cancelled node remains in the graph as an audit fact, but it is
            # no longer an active communication target or dependency consumer.
            # Outgoing dependency edges are retained so active downstream work
            # cannot silently forget that its prerequisite was cancelled.
            proposed_edges = {
                key: edge
                for key, edge in proposed_edges.items()
                if not (
                    (key[2] == "depends_on" and key[1] in cancelled_ids)
                    or (key[2] != "depends_on" and (
                        key[0] in cancelled_ids or key[1] in cancelled_ids
                    ))
                )
            }
            for node_id in cancelled_ids:
                current[node_id]["dependencies"] = []

        # Delegate the mutation phase to the transaction-backed committer.
        try:
            cancelled = GraphPatchCommitter(self.task_graph).commit(
                additions=additions,
                update_nodes=patch.update_nodes,
                proposed_edges=proposed_edges,
                cancel_nodes=patch.cancel_nodes,
                remap=remap,
                resolved_state=current,
                reasoning=patch.reasoning,
                reason_codes=patch.reason_codes,
            )
        except Exception as exc:
            self.trace_recorder.record(
                EventType.GRAPH_PATCH_FAILED,
                {
                    "reason": "transaction_rolled_back",
                    "error": str(exc),
                    "reason_codes": list(patch.reason_codes),
                },
            )
            if proposed_communication_links:
                self.trace_recorder.record(
                    EventType.TOPOLOGY_PATCH_REJECTED,
                    {
                        "add_links": proposed_communication_links,
                        "reason": str(exc),
                        "reason_codes": patch.reason_codes,
                    },
                )
            raise
        communication_links = proposed_communication_links
        node_count_after = len(self.task_graph.nodes)
        edge_count_after = len(self.task_graph.edges)
        result = {
            "added": [node["node_id"] for node in additions],
            "updated": [
                remap(x.get("node_id") or x.get("task_id"))
                for x in patch.update_nodes
                if isinstance(x, dict)
            ],
            "cancelled": cancelled,
            "communication_links_added": communication_links,
            "reason_codes": patch.reason_codes,
            # Internal API compatibility. Trace and Dashboard consumers use
            # the explicitly public planning_explanation field below.
            "reasoning": patch.reasoning,
            "planning_explanation": patch.reasoning,
            "task_version": self.goal_version,
            "node_count_before": node_count_before,
            "node_count_after": node_count_after,
            "node_growth": node_count_after - node_count_before,
            "edge_count_before": edge_count_before,
            "edge_count_after": edge_count_after,
            "edge_growth": edge_count_after - edge_count_before,
        }
        self.trace_recorder.record(EventType.GRAPH_PATCH_APPLIED, result)
        if communication_links:
            self.trace_recorder.record(
                EventType.TOPOLOGY_PATCH_APPLIED,
                {
                    "added_links": communication_links,
                    "reason_codes": patch.reason_codes,
                    "planning_explanation": patch.reasoning,
                    "task_version": self.goal_version,
                },
            )
        if cycle_repairs:
            self.trace_recorder.record(
                EventType.GRAPH_PATCH_REPAIR_APPLIED,
                {
                    "repairs": cycle_repairs,
                    "task_version": self.goal_version,
                    "reason_codes": patch.reason_codes,
                },
            )
        self.trace_recorder.record(
            "task_graph.snapshot",
            {
                "nodes": [
                    {
                        "node_id": node.node_id,
                        "goal": node.goal,
                        "role": node.role,
                        "status": node.status,
                        "dependencies": list(node.dependencies),
                        "metadata": dict(node.metadata),
                    }
                    for node in self.task_graph.nodes.values()
                ],
                "edges": [
                    {
                        "source": edge.source,
                        "target": edge.target,
                        "kind": edge.edge_type,
                    }
                    for edge in self.task_graph.edges
                ],
            },
        )
        return result

    def plan_requirement_change(
        self,
        change_description: str,
        *,
        change_kind: Optional[str] = None,
        current_goal: str = "",
        current_constraints: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Use the real Governor LLM to convert a runtime change into a plan.

        A requirement change is not a new independent task.  It is a versioned
        control-plane event that may alter constraints, graph nodes, workers,
        resources and context policy.  The LLM only proposes the decision;
        ``apply_task_graph_patch`` remains the code-enforced mutation boundary.
        """
        if not self.llm_matching_enabled or self.llm_client is None:
            raise ValueError("A real LLM client is required for requirement changes")
        if not str(change_description or "").strip():
            raise ValueError("change_description must not be empty")

        graph = [
            {
                "node_id": node.node_id,
                "goal": node.goal,
                "role": node.role,
                "status": node.status,
                "dependencies": list(node.dependencies),
                "acceptance_criteria": list(node.acceptance_criteria),
                "worker_profile": node.metadata.get("worker_profile", {}),
            }
            for node in self.task_graph.nodes.values()
        ]
        schema = {
            "change_kind": "requirement|environment|worker_failure|budget|other",
            "impact_summary": "string",
            "affected_node_ids": ["existing node ids only"],
            "new_constraints": ["strings that must remain active"],
            "worker_takeover": {
                "lost_node_ids": ["existing node ids"],
                "required_capabilities": ["capability names"],
                "reason": "string",
            },
            "context_policy": {
                "budget_multiplier": "number 0..1 or null",
                "broadcast_policy": "structured_local_only|normal|retain_all",
                "reason": "string",
            },
            "patch": TaskGraphPatch().as_dict(),
            "reverification": ["acceptance checks that must be rerun"],
        }
        prompt = json.dumps({
            "current_goal": current_goal,
            "current_constraints": current_constraints or [],
            "requested_change": change_description,
            "declared_change_kind": change_kind or "auto_classify",
            "task_version_before": self.goal_version,
            "task_graph": graph,
            "output_schema": schema,
        }, ensure_ascii=False)
        response = self.llm_client.chat([
            {
                "role": "system",
                "content": (
                    "你是天枢 AstralNexus Global Governor 的运行时需求变更决策器。"
                    "只输出 JSON，不执行代码，不声称已经完成迁移或测试。"
                    "必须区分已完成证据与待执行动作。"
                    "只能引用输入中存在的节点 ID；新增节点必须通过 patch.add_nodes 提议。"
                    "数据库、接口、权限、部署和测试影响必须拆成可验证的任务节点。"
                    "后端 Worker 失效时必须提出能力匹配的接管或补图方案，不能只改 role 字符串。"
                    "预算变化必须体现在 context_policy，不得删除已有需求、接口约定或测试证据。"
                ),
            },
            {"role": "user", "content": prompt},
        ])
        decision = _parse_governor_json(response)
        if not isinstance(decision, dict):
            raise ValueError("requirement change planner returned invalid JSON")
        patch_data = decision.get("patch") or {}
        if not isinstance(patch_data, dict):
            patch_data = {}
        decision["patch"] = TaskGraphPatch(
            add_nodes=list(patch_data.get("add_nodes", [])),
            update_nodes=list(patch_data.get("update_nodes", [])),
            cancel_nodes=[str(x) for x in patch_data.get("cancel_nodes", [])],
            add_edges=list(patch_data.get("add_edges", [])),
            remove_edges=list(patch_data.get("remove_edges", [])),
            reason_codes=[str(x) for x in patch_data.get("reason_codes", [])],
            reasoning=str(patch_data.get("reasoning", decision.get("impact_summary", ""))),
        )
        decision["affected_node_ids"] = [
            str(x) for x in decision.get("affected_node_ids", [])
            if str(x) in self.task_graph.nodes
        ]
        decision["new_constraints"] = strings_from(decision.get("new_constraints", []))
        decision["reverification"] = strings_from(decision.get("reverification", []))
        decision["task_version_before"] = self.goal_version
        self.trace_recorder.record(EventType.USER_REQUIREMENT_CHANGED, {
            "change_kind": decision.get("change_kind", change_kind or "other"),
            "description": change_description,
            "task_version_before": self.goal_version,
            "affected_node_ids": decision["affected_node_ids"],
            "reason_codes": decision["patch"].reason_codes,
        })
        return decision

    def plan_verification_recovery(
        self,
        *,
        node_id: str,
        verdict: str,
        overall_score: float,
        dimensions: List[Dict[str, Any]],
    ) -> TaskGraphPatch:
        """Convert a Verifier quality gap into a governed graph patch.

        Verifier reports facts about quality. GlobalGovernor alone decides
        whether to retry, replace, expand, or cancel work. The returned patch
        still passes through ``apply_task_graph_patch`` for state, identity,
        dependency, duplicate, and cycle validation.
        """
        if not self.llm_matching_enabled or self.llm_client is None:
            raise ValueError("A real LLM client is required for verification recovery")
        node = self.task_graph.get_node(node_id)
        if node is None:
            raise KeyError(f"Unknown verification target: {node_id}")
        graph = [
            {
                "node_id": item.node_id,
                "goal": item.goal,
                "role": item.role,
                "status": item.status,
                "dependencies": list(item.dependencies),
                "acceptance_criteria": list(item.acceptance_criteria),
                "retry_count": item.retry_count,
            }
            for item in self.task_graph.nodes.values()
        ]
        prompt = json.dumps(
            {
                "failed_node": {
                    "node_id": node.node_id,
                    "goal": node.goal,
                    "role": node.role,
                    "status": node.status,
                    "dependencies": list(node.dependencies),
                "acceptance_criteria": list(node.acceptance_criteria),
                    "retry_count": node.retry_count,
                },
                "verification": {
                    "verdict": verdict,
                    "overall_score": overall_score,
                    "dimensions": dimensions,
                },
                "task_graph": graph,
                "output_schema": TaskGraphPatch().as_dict(),
            },
            ensure_ascii=False,
        )
        response = self.llm_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是天枢 AstralNexus Global Governor 的验证恢复决策器。"
                        "Verifier 只提供质量事实，你负责决定最小影响恢复策略。"
                        "只输出 TaskGraphPatch JSON，不执行任务，不声称修复完成。"
                        "已有节点只能通过 update_nodes 更新；新工作只能放入 add_nodes。"
                        "所有依赖必须引用当前图节点或本 patch 新节点，不得制造环。"
                        "若原节点可通过新方法重试，将其从 repair_pending 更新为 ready；"
                        "若需要替代方案，新增具备明确能力、验收条件和依赖的节点。"
                        "不得因重试次数机械通过质量门，也不得删除已有约束或证据。"
                        "验证恢复通常不需要新增通信边；add_edges 默认必须为空。"
                        "如果确实需要通信边，每项必须同时提供非空字符串 source、target、edge_type；"
                        "禁止输出 null、None、空字符串或缺字段的边对象。"
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        parsed = _parse_governor_json(response)
        if not isinstance(parsed, dict):
            raise ValueError("verification recovery planner returned invalid JSON")
        # Recovery communication links are optional metadata, unlike node
        # dependencies. Keep the graph mutation boundary strict, but do not
        # let one malformed optional edge discard an otherwise valid repair
        # node/update. Every discarded edge is recorded and named in the
        # reason codes so this is observable rather than a silent fallback.
        valid_edges: List[Dict[str, Any]] = []
        malformed_edge_count = 0
        for raw_edge in parsed.get("add_edges", []) or []:
            if not isinstance(raw_edge, dict):
                malformed_edge_count += 1
                continue
            source = str(raw_edge.get("source") or "").strip()
            target = str(raw_edge.get("target") or "").strip()
            edge_type = str(raw_edge.get("edge_type") or "").strip()
            if not source or not target or not edge_type:
                malformed_edge_count += 1
                continue
            valid_edges.append({**raw_edge, "source": source, "target": target, "edge_type": edge_type})
        recovery_reason_codes = [
            "VERIFICATION_GAP",
            *[str(x) for x in parsed.get("reason_codes", [])],
        ]
        if malformed_edge_count:
            recovery_reason_codes.append("MALFORMED_OPTIONAL_EDGE_DROPPED")
            self.trace_recorder.record(
                "governor.verification_recovery.malformed_optional_edges",
                {
                    "node_id": node_id,
                    "count": malformed_edge_count,
                    "action": "discard_optional_edges_keep_repair_patch",
                },
            )
        patch = TaskGraphPatch(
            add_nodes=list(parsed.get("add_nodes", [])),
            update_nodes=list(parsed.get("update_nodes", [])),
            cancel_nodes=[str(x) for x in parsed.get("cancel_nodes", [])],
            add_edges=valid_edges,
            remove_edges=list(parsed.get("remove_edges", [])),
            reason_codes=recovery_reason_codes,
            reasoning=str(parsed.get("reasoning") or ""),
            background_candidates=list(parsed.get("background_candidates", [])),
        )
        self.trace_recorder.record(
            "governor.verification_recovery.proposed",
            {
                "node_id": node_id,
                "verdict": verdict,
                "overall_score": overall_score,
                "reason_codes": patch.reason_codes,
                "add_node_count": len(patch.add_nodes),
                "update_node_count": len(patch.update_nodes),
            },
        )
        return patch

    def admit_worker_subtasks(
        self,
        *,
        parent_node_id: str,
        proposals: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Evaluate Worker-discovered work and mutate the graph via one patch.

        Worker output is a proposal, never an alternate graph mutation path.
        Value admission is deterministic policy validation over LLM-generated
        proposals; graph invariants are enforced by ``apply_task_graph_patch``.
        """
        if parent_node_id not in self.task_graph.nodes:
            raise KeyError(f"Unknown parent task: {parent_node_id}")
        patch = TaskGraphPatch(
            reason_codes=["WORKER_DISCOVERY", "GOVERNOR_ADMISSION"],
            reasoning=f"Governed follow-up work proposed by {parent_node_id}",
        )
        rejected: List[Dict[str, str]] = []
        for raw in proposals:
            if not isinstance(raw, dict):
                rejected.append({"goal": "", "reason": "proposal_not_object"})
                continue
            goal = str(raw.get("goal") or raw.get("description") or raw.get("summary") or "").strip()
            if not goal:
                rejected.append({"goal": "", "reason": "missing_goal"})
                continue
            capabilities = strings_from(
                raw.get("required_capabilities") or raw.get("capabilities") or []
            )
            proposal = TaskProposal(
                goal=goal,
                required_capabilities=capabilities,
                expected_information_gain=float(raw.get("information_gain", 0.5) or 0.5),
                risk_reduction=float(raw.get("risk_reduction", 0.3) or 0.3),
                parallel_benefit=float(raw.get("parallel_benefit", 0.4) or 0.4),
                estimated_cost=max(1, int(raw.get("estimated_cost", 1000) or 1000)),
                dependencies=[
                    str(item) for item in (raw.get("dependencies") or [parent_node_id])
                ],
                parent_task_id=parent_node_id,
            )
            decision = self.evaluate_task_proposal(proposal)
            if not decision.admitted:
                rejected.append({"goal": goal, "reason": decision.reasoning})
                continue
            patch.add_nodes.append(
                {
                    "goal": goal,
                    "role": "governed_worker",
                    "required_capabilities": capabilities,
                    "dependencies": list(proposal.dependencies),
                    "acceptance_criteria": strings_from(
                        raw.get("acceptance_criteria") or []
                    ),
                    "risk_level": str(raw.get("risk_level") or "medium"),
                    "resource_tier": str(raw.get("resource_tier") or "cloud"),
                    "metadata": {
                        **dict(raw.get("metadata") or {}),
                        "source": "worker_discovery",
                        "proposed_by": parent_node_id,
                        "admission_score": decision.value_score,
                        "admission_reasoning": decision.reasoning,
                    },
                }
            )
        applied = self.apply_task_graph_patch(patch, parent_task_id=parent_node_id)
        applied["rejected_proposals"] = rejected
        self.trace_recorder.record(
            "governor.worker_subtasks.admitted",
            {
                "parent_node_id": parent_node_id,
                "proposal_count": len(proposals),
                "admitted_count": len(applied.get("added", [])),
                "rejected_count": len(rejected),
            },
        )
        return applied

    def plan_fault_recovery(
        self,
        *,
        fault: Dict[str, Any],
        impact: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Select recovery actions for a diagnosed runtime fault."""
        if not self.llm_matching_enabled or self.llm_client is None:
            raise ValueError("A real LLM client is required for fault recovery")
        allowed_actions = {
            "retry": {"target": "existing node id"},
            "reassign": {
                "target": "existing node id",
                "details": {
                    "resource_tier": "approved local|edge|cloud, optional",
                    "worker_type": "approved registered Worker Profile, optional",
                },
            },
            "replan": {"target": "existing node id"},
            "switch_tool": {
                "target": "registered tool name",
                "details": {"alternative_tool": "approved registered tool name"},
            },
            "restore_facts": {"target": "memory_system"},
            "switch_resource": {
                "target": "resource id",
                "details": {"alternative_resource": "approved resource id"},
            },
        }
        response = self.llm_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是天枢 AstralNexus Global Governor 的故障恢复决策器。"
                        "只输出 JSON，不执行恢复。根据故障、影响范围和当前任务图选择最小影响动作。"
                        "禁止固定三次重试规则；必须依据错误指纹、已有尝试和信息增益判断。"
                        "不得引用不存在的任务节点，不得将失败直接标记为完成。"
                        "输出 {strategy, reasoning, actions}，actions 仅可使用给定动作类型。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "fault": fault,
                            "impact": impact,
                            "task_graph": [
                                {
                                    "node_id": node.node_id,
                                    "goal": node.goal,
                                    "status": node.status,
                                    "retry_count": node.retry_count,
                                    "dependencies": list(node.dependencies),
                                    "worker_profile": node.metadata.get("worker_profile", {}),
                                }
                                for node in self.task_graph.nodes.values()
                            ],
                            "allowed_actions": allowed_actions,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        parsed = _parse_governor_json(response)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list):
            raise ValueError("fault recovery planner returned invalid JSON")
        actions: List[Dict[str, Any]] = []
        for raw in parsed["actions"]:
            if not isinstance(raw, dict):
                continue
            action_type = str(raw.get("type") or "")
            if not action_type:
                # A model may emit an empty placeholder object alongside valid
                # recovery actions.  It is not an executable action and must
                # not abort the whole control loop; retain an audit trail and
                # let the valid actions (if any) proceed.
                self.trace_recorder.record(
                    "governor.fault_recovery.malformed_action",
                    {
                        "fault_id": fault.get("fault_id"),
                        "reason": "MALFORMED_EMPTY_ACTION_DROPPED",
                    },
                )
                continue
            if action_type not in allowed_actions:
                raise ValueError(f"Governor proposed unsupported recovery action: {action_type}")
            target = str(raw.get("target") or "")
            if action_type in {"retry", "reassign", "replan"} and target not in self.task_graph.nodes:
                raise ValueError(f"Governor recovery references unknown node: {target}")
            details = dict(raw.get("details") or {})
            if action_type == "reassign":
                tier = str(details.get("resource_tier") or "").strip()
                worker_type = str(details.get("worker_type") or "").strip()
                if not tier and not worker_type:
                    raise ValueError(
                        "Governor reassign action requires resource_tier or worker_type"
                    )
                if tier and tier not in {"local", "edge", "cloud"}:
                    raise ValueError(f"Governor selected invalid resource tier: {tier}")
            if action_type == "switch_tool" and not str(
                details.get("alternative_tool") or ""
            ).strip():
                raise ValueError(
                    "Governor switch_tool action requires alternative_tool"
                )
            if action_type == "switch_resource" and not str(
                details.get("alternative_resource") or ""
            ).strip():
                raise ValueError(
                    "Governor switch_resource action requires alternative_resource"
                )
            actions.append(
                {
                    "type": action_type,
                    "target": target,
                    "reason": str(raw.get("reason") or parsed.get("reasoning") or ""),
                    "details": details,
                }
            )
        if not actions:
            raise ValueError("Governor fault recovery returned no executable actions")
        plan = {
            "strategy": str(parsed.get("strategy") or "governed_recovery"),
            "reasoning": str(parsed.get("reasoning") or ""),
            "actions": actions,
        }
        self.trace_recorder.record(
            "governor.fault_recovery.planned",
            {
                "fault_id": fault.get("fault_id"),
                "fault_type": fault.get("fault_type"),
                "strategy": plan["strategy"],
                "action_count": len(actions),
                "actions": [item["type"] for item in actions],
            },
        )
        return plan

    def derive_delivery_contract(
        self,
        task_content: str,
        client: Any,
        *,
        environment_summary: str = "",
        permission_constraints: str = "",
        previous_contract: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Derive a domain-neutral delivery contract from the user's task.

        The Governor owns this decision because delivery shape is a property of
        the task, not of a particular reducer or scenario.  A coding task may
        require files and tests, while a creative task may require chapters;
        neither shape is embedded in the runtime.
        """
        if self.delivery_contract and not force and self.goal_spec and self.goal_spec.task_goal == task_content:
            return self.delivery_contract
        prompt = f"""你是 Governor 控制平面的交付契约管理器，只输出 JSON。
将用户任务转换成领域无关、可执行、可验收的契约；不得执行任务或生成业务内容。
只提取用户明确要求或完成任务不可缺少的约束。未明确要求长度时不得设置最低字数；
未要求文件或可下载产物时 needs_artifact_refs 默认为 false。不要假设任务属于科研、编程、
写作或任何固定领域。

区分约束来源：
- explicit：用户明确要求
- inferred：为完成任务必须推导的约束
- optional：增强项，不作为硬性验收条件

先判断交付证据策略：
- plan_only：用户要的是方案、设计、计划、分析、模板或决策建议，验收交付物本身即可；
- runtime_required：用户明确要求执行、部署、修复、测试或产生实际运行结果；
- mixed：同时要求方案和实际运行证据。
无法确定时选择 mixed，不得把计划冒充执行结果。

任务：
{task_content}
环境摘要：{environment_summary}
权限限制：{permission_constraints}
既有契约：{json.dumps(previous_contract, ensure_ascii=False) if previous_contract else "null"}
JSON 字段：
{{
  "delivery_mode": "model_decided",
  "delivery_strategy": "artifact_first|text_first|hybrid|model_decided",
  "summary_required": true,
  "execution_evidence_policy": "plan_only|runtime_required|mixed",
  "required_outputs": [{{"output_id":"O-01","name":"由任务语义决定的交付物","type":"model_decided|text|file|software|data|other","required":true,"source":"explicit|inferred|optional","acceptance_criteria":[]}}],
  "required_sections": ["只有任务明确要求的文本结构标题；没有则为空数组"],
  "recommended_sections": [],
  "hard_constraints": [],
  "soft_preferences": [],
  "global_acceptance_criteria": ["可检查的完成条件"],
  "completion_evidence": ["验证完成所需的证据类型"],
  "forbidden_claims": ["不能无依据声称已完成的内容"],
  "uncertainties": [],
  "assumptions": [],
  "needs_clarification": false,
  "needs_citations": false,
  "needs_artifact_refs": false
}}
"""
        try:
            contract = _parse_governor_json(client.chat([
                {"role": "system", "content": "你是通用任务契约分析器，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ]))
        except Exception as exc:
            self.trace_recorder.record(EventType.DELIVERY_CONTRACT_FAILED, {"error": str(exc)[:500]})
            raise RuntimeError(
                f"GlobalGovernor delivery contract generation failed: {str(exc)[:500]}"
            ) from exc
        if not isinstance(contract, dict):
            self.trace_recorder.record(
                EventType.DELIVERY_CONTRACT_FAILED,
                {"error": "delivery contract response was not a JSON object"},
            )
            raise ValueError("GlobalGovernor delivery contract response was not a JSON object")
        def strings(key: str) -> List[str]:
            value = contract.get(key, [])
            return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        outputs = []
        for index, item in enumerate(contract.get("required_outputs", []), 1):
            if isinstance(item, dict):
                outputs.append({
                    "output_id": str(item.get("output_id") or f"O-{index:02d}"),
                    "name": str(item.get("name") or "required deliverable"),
                    "type": str(item.get("type") or "other"),
                    "required": bool(item.get("required", True)),
                    "source": str(item.get("source") or "explicit"),
                    "acceptance_criteria": strings_from(item.get("acceptance_criteria", [])),
                })
            elif str(item).strip():
                outputs.append({
                    "output_id": f"O-{index:02d}", "name": str(item).strip(),
                    "type": "other", "required": True, "source": "explicit",
                    "acceptance_criteria": [],
                })
        evidence_policy = str(contract.get("execution_evidence_policy") or "mixed")
        if evidence_policy not in {"plan_only", "runtime_required", "mixed"}:
            evidence_policy = "mixed"
        
        # Create DeliveryContract object
        delivery_contract = DeliveryContract(
            goal_id=f"G-{self.goal_version:03d}",
            task_version=self.goal_version,
            delivery_mode=str(contract.get("delivery_mode") or "mixed"),
            delivery_strategy=str(contract.get("delivery_strategy") or "model_decided"),
            summary_required=bool(contract.get("summary_required", True)),
            execution_evidence_policy=evidence_policy,
            required_outputs=outputs,
            required_sections=strings("required_sections"),
            recommended_sections=strings("recommended_sections"),
            hard_constraints=strings("hard_constraints"),
            soft_preferences=strings("soft_preferences"),
            global_acceptance_criteria=strings("global_acceptance_criteria"),
            completion_evidence=strings("completion_evidence"),
            forbidden_claims=strings("forbidden_claims"),
            uncertainties=strings("uncertainties"),
            assumptions=strings("assumptions"),
            needs_clarification=bool(contract.get("needs_clarification", False)),
            needs_citations=bool(contract.get("needs_citations", False)),
            needs_artifact_refs=bool(contract.get("needs_artifact_refs", False)),
        )
        
        self.goal_spec = GoalSpec(
            goal_id=delivery_contract.goal_id,
            task_goal=task_content,
            normalized_goal=str(contract.get("normalized_goal") or task_content),
            hard_constraints=delivery_contract.hard_constraints,
            soft_preferences=delivery_contract.soft_preferences,
            success_criteria=delivery_contract.global_acceptance_criteria,
            forbidden_claims=delivery_contract.forbidden_claims,
            uncertainties=delivery_contract.uncertainties,
            assumptions=delivery_contract.assumptions,
            task_version=self.goal_version,
            needs_clarification=delivery_contract.needs_clarification,
        )
        self.delivery_contract = delivery_contract.as_dict()
        # Preserve an explicit input-boundary policy from the task text.  It
        # prevents reducers from turning an unavailable optional external
        # source into an endless evidence-repair loop.  This is domain-neutral:
        # the same rule applies to any task that supplies embedded inputs and
        # explicitly allows unavailable sources to remain pending.
        task_lower = str(task_content or "").lower()
        self.delivery_contract["external_evidence_policy"] = (
            "optional_when_unavailable"
            if (
                ("无外部" in task_lower or "无外部data" in task_lower or "no external" in task_lower)
                and ("待检索" in task_lower or "不得编造" in task_lower or "标注" in task_lower)
            )
            else "required_if_explicit"
        )
        self.trace_recorder.record(EventType.DELIVERY_CONTRACT_DERIVED, {
            "delivery_mode": delivery_contract.delivery_mode,
            "required_outputs": delivery_contract.required_outputs,
            "required_sections": delivery_contract.required_sections,
            "global_acceptance_criteria_count": len(delivery_contract.global_acceptance_criteria),
            "completion_evidence_count": len(delivery_contract.completion_evidence),
        })
        return dict(self.delivery_contract)

    def plan_task_graph_patch(
        self,
        goal: str,
        client: Any = None,
        *,
        graph_summary: str = "",
        capability_catalog: str = "",
        resource_catalog: str = "",
        event_callback: Any = None,
    ) -> TaskGraphPatch:
        """Generate a minimal incremental DAG patch without fixed roles/counts."""
        client = client or self.llm_client
        if client is None or not callable(getattr(client, "chat", None)):
            raise ValueError("GlobalGovernor requires a real LLM client for task-graph planning")
        goal_context = ""
        if self.goal_spec:
            goal_context = (
                f"目标ID: {self.goal_spec.goal_id}\n"
                f"任务版本: {self.goal_spec.task_version}\n"
                f"硬约束: {json.dumps(self.goal_spec.hard_constraints, ensure_ascii=False)}\n"
                f"成功标准: {json.dumps(self.goal_spec.success_criteria, ensure_ascii=False)}\n"
                f"必需交付物: {json.dumps(self.delivery_contract.get('required_outputs', []), ensure_ascii=False)}\n"
                f"必需章节: {json.dumps(self.delivery_contract.get('required_sections', []), ensure_ascii=False)}\n"
                f"完成证据: {json.dumps(self.delivery_contract.get('completion_evidence', []), ensure_ascii=False)}\n"
                f"需要引用: {self.delivery_contract.get('needs_citations', False)}\n"
                f"需要文件引用: {self.delivery_contract.get('needs_artifact_refs', False)}"
            )
        prompt = f"""你是 Governor 控制平面的全局任务图规划器，只输出 JSON。
生成近期可执行的最小 DAG 增量，不要完成业务任务，不要一次性展开所有远期细节。
不要使用固定 Agent 角色或固定节点数；使用 required_capabilities 描述能力需求。
只有独立可验收目标、能力差异、并行价值、独立验证、资源/权限差异或独立失败边界
足以成立时才拆分节点。对于复杂任务，不能把多个必需交付物、完成证据或互不依赖的
证据族合并成一个宽泛节点；应按“独立证据/独立验收边界/独立失败恢复边界”拆成可并行的
最小工作单元，并在 expected_outputs 中声明该节点将产生的证据类型或 artifact。不要为了增加
节点数量而拆分，节点粒度由当前交付契约和证据覆盖决定。每个节点必须有独立验收边界。禁止循环依赖。
若 reason_codes 包含 PARALLELIZABLE 且近期 Patch 有多个工作单元，必须检查首个 Ready-Wave：
只依赖当前父节点、彼此不需要产物的能力边界应同时进入首波。不得把需求、架构、数据、接口、
实现或证据等多个边界塞入唯一首节点，再让所有节点排队等待。确有共享前置契约时，只保留最小
契约节点；其余不依赖该契约的探索、数据检查、环境检查或风险验证仍应并行。若只能串行，移除
PARALLELIZABLE，并在 reasoning 中明确不可消除的输入依赖。

当前目标上下文：
{goal_context}

强制规则：
1. dependencies 必须引用已有节点ID 或同一 Patch 中新增的节点ID。
   若存在未知依赖，必须返回 {{"planning_status":"ERROR","error":{{"code":"UNKNOWN_DEPENDENCY","unknown_ids":["..."]}}}}
   不得自行虚构不存在的节点。
2. update_nodes 必须说明字段级变更：每个 update 条目包含 node_id 和具体要改的字段
   （goal/status/dependencies/acceptance_criteria/metadata 等），不得仅传空对象。
3. cancel_nodes 必须附带取消原因和影响范围：{{"node_id":"...","cancel_reason":"...","impact":"..."}}。
4. 新增节点之间的调度依赖只通过 dependencies 表达；不要再用 add_edges 重复表达新增节点依赖。
   add_edges 用于稀疏认知通信拓扑：只有当目标节点确实需要消费源节点的证据、复核结果、
   修订意见或阻断信号时才添加，不得生成全连接广播。端点可以是已有节点或本 Patch 新增节点。
5. add_edges/remove_edges 必须说明边类型（edge_type）：
   depends_on | communicates_to | evidence_for | revises | verifies | blocks | supersedes。
6. 禁止引入环路；Governor 会在原子提交前进行 Kahn 校验，并拒绝现有图被破坏的 Patch。
7. 失败时必须返回结构化 planning_status，不得返回部分 JSON。
8. 父节点和根节点的生命周期由 Orchestrator 独占管理。规划 Patch 不得通过
   update_nodes/cancel_nodes 把当前父节点或根节点改为 completed/failed/cancelled。
9. 并行必须基于真实输入独立性。若节点 B 的验收需要节点 A 产生的 schema、接口契约、
   文件或测试结果，B 必须依赖 A；只有输入已具备、输出路径不冲突且验收互不依赖的节点
   才能进入同一 Ready-Wave。expected_outputs 应明确产物，dependencies 应表达生产者到
   消费者的 artifact 依赖，禁止用“并行”标签制造伪并行。
10. 若交付契约要求可运行软件、代码或其他可执行产物，初始化、目录创建、脚手架、设计文档
    或测试模板都不等于功能实现。近期 DAG 必须包含至少一个负责真实功能源码的 implementation
    节点，其验收条件应覆盖可运行行为；执行、测试和验收节点必须依赖该实现节点的实际产物。
11. 运行日志、测试报告、健康检查、截图等执行证据只能由实际启动、访问或测试动作产生。
    不得让设计/脚手架节点预写成功报告，不得把“待执行脚本”当作“已执行证据”。
12. 每个新增节点必须给出公开可审计的需求谱系：source_requirements 只摘录或概括直接
    支撑该节点的用户要求/交付契约；derivation 只说明为何从这些要求形成当前节点契约，
    不得输出私有逐步推理。两者都必须能让用户追溯“原始输入 -> Planner -> Worker 任务”。

输出 schema：
{{"add_nodes":[{{"node_id":"patch-local-id-1","goal":"...","task_type":"analysis|implementation|verification|other",
"required_capabilities":["..."],"dependencies":["已有节点ID"],"acceptance_criteria":["独立可检查条件"],
"source_requirements":["用户原文或交付契约中的直接依据"],"derivation":"公开的任务拆分说明",
"expected_outputs":[],"priority":50,"criticality":"low|medium|high","execution_target":"local|edge|cloud",
"budget":{{"max_tokens":0,"max_seconds":0,"max_tool_calls":0}},
"background_candidates":[{{"goal":"可选的独立后台核验目标","role":"仅作能力提示",
"required_capabilities":["..."],"expected_information_gain":0.0,"risk_reduction":0.0,
"parallel_benefit":0.0,"estimated_cost":0}}]}}],
"update_nodes":[{{"node_id":"...","field_name":"new_value","cancel_reason":"..."}}],
"cancel_nodes":[{{"node_id":"...","cancel_reason":"...","impact":"受影响下游节点列表"}}],
"add_edges":[{{"source":"...","target":"...","edge_type":"depends_on|communicates|supersedes"}}],
"remove_edges":[{{"source":"...","target":"...","edge_type":"..."}}],
"reason_codes":["GOAL_DECOMPOSITION|CAPABILITY_SEPARATION|PARALLELIZABLE|CRITICAL_PATH|VERIFICATION_GAP"],
"reasoning":"面向用户的简短规划说明，只说明拆分依据、能力选择、依赖与并行策略；禁止输出私有逐步推理",
"planning_status":"OK|ERROR"}}
目标：{goal}
当前任务图：{graph_summary}
能力目录：{capability_catalog}
资源目录：{resource_catalog}
"""
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是通用任务图规划器，只输出 JSON。reasoning 字段只能给出"
                        "可公开的规划说明，不得输出 chain-of-thought 或逐步私有推理。"
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            if callable(getattr(client, "stream_chat", None)):
                response = client.stream_chat(
                    messages,
                    response_format="json",
                    event_callback=event_callback,
                )
            else:
                response = client.chat(messages)
            parsed = _parse_governor_json(response)
        except Exception as exc:
            self.trace_recorder.record(EventType.GRAPH_PATCH_FAILED, {"error": str(exc)[:500]})
            return TaskGraphPatch(reason_codes=["PLANNING_FAILED"], reasoning=str(exc)[:500])
        if not isinstance(parsed, dict):
            return TaskGraphPatch(reason_codes=["INVALID_PLAN"], reasoning="planner output was not an object")
        nodes = []
        for item in parsed.get("add_nodes", []):
            if not isinstance(item, dict) or not str(item.get("goal", "")).strip():
                continue
            nodes.append({
                # Preserve the planner's patch-local ID so dependencies and
                # add_edges can be remapped to the committed dynamic ID.
                "node_id": str(item.get("node_id") or item.get("task_id") or "").strip(),
                "goal": str(item["goal"]).strip(),
                "task_type": str(item.get("task_type") or "other"),
                "required_capabilities": [str(x) for x in item.get("required_capabilities", []) if str(x).strip()],
                "dependencies": [str(x) for x in item.get("dependencies", []) if str(x).strip()],
                "acceptance_criteria": strings_from(item.get("acceptance_criteria", [])),
                "source_requirements": strings_from(item.get("source_requirements", [])),
                "derivation": str(item.get("derivation") or "").strip(),
                "expected_outputs": item.get("expected_outputs", []) if isinstance(item.get("expected_outputs", []), list) else [],
                "priority": max(0, min(100, int(item.get("priority", 50)))),
                "criticality": str(item.get("criticality") or "medium"),
                "execution_target": str(item.get("execution_target") or "cloud"),
            "budget": item.get("budget") if isinstance(item.get("budget"), dict) else {},
            "background_candidates": item.get("background_candidates", []) if isinstance(item.get("background_candidates"), list) else [],
            })
        patch = TaskGraphPatch(
            add_nodes=nodes,
            update_nodes=parsed.get("update_nodes", []) if isinstance(parsed.get("update_nodes", []), list) else [],
            cancel_nodes=[
                str(x.get("node_id") or x.get("task_id") or "").strip()
                if isinstance(x, dict) else str(x)
                for x in parsed.get("cancel_nodes", [])
                if (str(x.get("node_id") or x.get("task_id") or "").strip() if isinstance(x, dict) else str(x).strip())
            ],
            add_edges=parsed.get("add_edges", []) if isinstance(parsed.get("add_edges", []), list) else [],
            remove_edges=parsed.get("remove_edges", []) if isinstance(parsed.get("remove_edges", []), list) else [],
            reason_codes=[str(x) for x in parsed.get("reason_codes", [])],
            reasoning=str(parsed.get("reasoning") or ""),
            background_candidates=(
                parsed.get("background_candidates", [])
                if isinstance(parsed.get("background_candidates", []), list) else []
            ),
        )
        self.trace_recorder.record(EventType.GRAPH_PATCH_PROPOSED, {
            "add_node_count": len(patch.add_nodes),
            "reason_codes": patch.reason_codes,
            "planning_explanation": patch.reasoning[:2000],
        })
        return patch

    def replan_rejected_task_graph_patch(
        self,
        goal: str,
        rejection_reason: str,
        *,
        graph_summary: str = "",
        capability_catalog: str = "",
        resource_catalog: str = "",
        event_callback: Any = None,
    ) -> TaskGraphPatch:
        """Regenerate a rejected graph patch without delegating policy to the executor."""
        corrected_goal = (
            f"{goal}\n\n上一版 TaskGraphPatch 未通过原子图校验。"
            f"\n拒绝原因：{str(rejection_reason)[:800]}"
            "\n请只修正导致拒绝的局部规划，保留原目标、硬约束和交付契约。"
            "返回最小、无环的 TaskGraphPatch；依赖只能引用当前图节点或同一 Patch 新节点。"
            "不得返回空 add_nodes，不得用说明文字替代结构化字段。"
        )
        if "false_parallelism_single_wide_bottleneck" in str(rejection_reason):
            corrected_goal += (
                "\n这是首波并行门禁拒绝：禁止新增一个共享前置节点再让全部工作依赖它。"
                "请把至少两个真实独立的工作单元直接挂到当前父节点（或 dependencies 为空，"
                "由 Governor 绑定当前父节点），使它们同时进入首个 Ready-Wave；"
                "架构、实现、数据准备、环境检查等只有在真实产物依赖存在时才串行。"
                "不得把 N-001、patch-local-id-1 或任何新增节点作为所有其他节点的共同前置。"
            )
        self.trace_recorder.record(
            "governor.graph_patch.replanning",
            {
                "rejection_reason": str(rejection_reason)[:500],
                "goal_version": self.goal_version,
            },
        )
        return self.plan_task_graph_patch(
            corrected_goal,
            graph_summary=graph_summary,
            capability_catalog=capability_catalog,
            resource_catalog=resource_catalog,
            event_callback=event_callback,
        )

    def select_worker_profile(
        self,
        task: Dict[str, Any],
        worker_profiles: Any,
        resources: Optional[List[Dict[str, Any]]] = None,
        *,
        history: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Select a worker profile by capability and resource fit.

        The caller supplies a capability catalogue; the Governor never maps a
        task directly to a hard-coded role name.  ``resources`` is optional so
        this method can be used before a concrete resource lease exists.
        """
        if isinstance(worker_profiles, dict):
            profiles = list(worker_profiles.values())
        else:
            profiles = list(worker_profiles or [])
        reasoning_attributes = {"logical_reasoning", "analysis", "reasoning"}
        required = {
            str(item).strip().lower()
            for item in task.get("required_capabilities", [])
            if str(item).strip()
            and str(item).strip().lower() not in reasoning_attributes
        }
        # A Governor patch may omit capabilities for a newly spawned node.
        # Never let the free-form ``role`` field become the routing decision:
        # derive a small, ontology-bound capability set from the goal so a
        # node such as "build an API" cannot silently become a Retrieval
        # worker merely because the patch said ``researcher``. This is
        # capability-ontology inference, not a task-domain template.
        goal_text = str(task.get("goal") or task.get("description") or "").strip().lower()
        explicit_required = bool(required)
        goal_implementation = contains_marker(goal_text, IMPLEMENTATION_MARKERS)
        goal_retrieval = contains_marker(goal_text, RETRIEVAL_MARKERS)
        # Goal intent is a hard routing boundary even when a planner supplied
        # a partial domain capability such as ``document_retrieval``.  A node
        # whose actual goal is to implement a module must receive an
        # implementation-capable Worker; domain retrieval can remain an
        # additional capability for the Worker contract.
        if goal_implementation and not (required & IMPLEMENTATION_CAPABILITIES):
            required.update({"implementation", "code_generation"})
            if any(marker in goal_text for marker in ("api", "接口", "service", "服务")):
                required.add("api_design")
            if any(marker in goal_text for marker in ("database", "sql", "数据库")):
                required.add("database_design")
        elif not explicit_required and goal_retrieval:
            required.update({"document_retrieval", "knowledge_base"})
        inferred_required = sorted(required) if not explicit_required or goal_implementation else []
        target = str(task.get("execution_target") or task.get("resource_tier") or "").lower()
        risk = str(task.get("risk_level") or "medium").lower()
        history = history or {}
        
        # Semantic capability alias map — maps LLM-generated capability names
        # to ACTUAL profile capability tokens so matching works correctly.
        # Each alias maps to a list of real tokens from HETEROGENEOUS_WORKERS
        # profiles (not category names, which would never match).
        _capability_aliases = {
            # Domain knowledge → Reasoning profile tokens
            "quantum_physics_knowledge": ["constraint_reasoning", "causal_inference"],
            "quantum_computing_knowledge": ["constraint_reasoning", "causal_inference"],
            "science_writing": ["comparison", "knowledge_generation"],
            "science_communication": ["comparison", "knowledge_generation"],
            "technology_trend_analysis": ["trend_analysis", "comparison"],
            "editing": ["cross_artifact_consistency"],
            # Research/Academic → Research profile tokens (LLM is one of research tools)
            "literature_search": ["web_search", "knowledge_base"],
            "literature_review": ["literature_synthesis", "academic_writing", "knowledge_base"],
            "evidence_extraction": ["document_retrieval", "knowledge_base"],
            "data_analysis": ["comparison", "causal_inference"],
            "data_synthesis": ["knowledge_generation", "literature_synthesis"],
            "trend_analysis": ["trend_analysis", "knowledge_generation"],
            "cross_domain_research": ["cross_domain_research", "literature_synthesis"],
            "technology_forecasting": ["trend_analysis", "knowledge_generation", "cross_domain_research"],
            "strategic_planning": ["knowledge_generation", "constraint_reasoning", "scheme_evaluation"],
            "technology_evaluation": ["scheme_evaluation", "comparison", "knowledge_generation"],
            "academic_survey": ["literature_synthesis", "academic_writing", "knowledge_generation"],
            "industry_report_analysis": ["comparison", "trend_analysis", "research_synthesis"],
            "research": ["knowledge_generation", "literature_synthesis", "web_search"],
            "research_synthesis": ["research_synthesis", "literature_synthesis"],
            "knowledge_synthesis": ["knowledge_generation", "literature_synthesis"],
            "scientific_writing": ["academic_writing", "knowledge_generation"],
            "innovation_analysis": ["knowledge_generation", "cross_domain_research"],
            # Research planning and evaluation capabilities.  Keep the
            # planner vocabulary separate from worker implementation names;
            # these aliases resolve to the Research profile's contract tokens
            # and work for research, engineering, and other planning tasks.
            "ai_research_direction_selection": ["knowledge_generation", "trend_analysis", "cross_domain_research"],
            "feasibility_analysis": ["constraint_reasoning", "scheme_evaluation"],
            "strategic_evaluation": ["constraint_reasoning", "scheme_evaluation", "comparison"],
            "research_design": ["research_design", "constraint_reasoning", "scheme_evaluation"],
            "experimental_methodology": ["experimental_methodology", "experimental_design", "constraint_reasoning"],
            "evaluation_metrics_design": ["evaluation_metrics_design", "evaluation_design", "benchmark_design", "constraint_reasoning"],
            "experimental_design": ["experimental_design", "experimental_methodology", "evaluation_design"],
            "benchmark_design": ["benchmark_design", "evaluation_metrics_design", "evaluation_design"],
            "evaluation_design": ["evaluation_design", "evaluation_metrics_design", "analysis"],
            # Common LLM planning variants.  These remain domain-neutral
            # capability aliases and resolve to the same Research contract.
            "critical_evaluation": ["scheme_evaluation", "comparison", "research_synthesis"],
            "domain_expertise_ai": ["knowledge_generation", "cross_domain_research", "research_synthesis"],
            "technical_analysis": ["constraint_reasoning", "causal_inference", "comparison"],
            "feasibility_assessment": ["constraint_reasoning", "scheme_evaluation"],
            "idea_generation": ["knowledge_generation", "causal_inference", "cross_domain_research"],
            "strategic_foresight": ["trend_analysis", "strategic_evaluation", "cross_domain_research"],
            # Coding → Coding profile tokens
            "code_generation": ["code_generation"],
            "programming": ["programming", "code_generation"],
            "code_modification": ["code_modification", "code_generation"],
            "code_review": ["static_analysis", "code_generation"],
            "coding": ["code_generation", "debugging"],
            "debugging": ["debugging"],
            "refactoring": ["refactoring"],
            "test_writing": ["test_writing"],
            "backend_development": ["backend_development", "api_design", "code_generation"],
            "frontend_development": ["frontend_development", "web_app_development", "code_generation"],
            "api_design": ["api_design", "api_integration", "code_generation"],
            "database_design": ["database_design", "dependency_analysis", "code_generation"],
            "web_app_development": ["web_app_development", "frontend_development", "backend_development"],
            "software_implementation": ["software_implementation", "implementation", "code_generation"],
            "implementation": ["implementation", "code_generation"],
            "full_stack_development": ["full_stack_development", "backend_development", "frontend_development", "database_design"],
            "ui_development": ["ui_development", "frontend_development", "web_app_development"],
            "sql_design": ["sql_design", "database_design", "code_generation"],
            "database_migration": ["database_migration", "database_design", "dependency_management"],
            "api_testing": ["api_testing", "test_execution"],
            "integration_testing": ["integration_testing", "test_execution"],
            "deployment": ["deployment", "terminal_exec", "build_automation"],
            "containerization": ["containerization", "deployment", "terminal_exec"],
            "build_automation": ["build_automation", "dependency_management", "terminal_exec"],
            "dependency_management": ["dependency_management", "dependency_analysis", "terminal_exec"],
            "test_orchestration": ["test_execution", "test_generation"],
            # Architecture → Architect profile tokens
            "architecture": ["system_architecture", "requirement_analysis"],
            "system_design": ["system_architecture", "requirement_analysis"],
            "system_architecture": ["system_architecture"],
            "requirement_analysis": ["requirement_analysis"],
            "database_modeling": ["database_modeling", "database_design"],
            "state_machine_design": ["state_machine_design"],
            "permission_design": ["permission_design"],
            # Testing and final delivery are independent capabilities.
            "testing": ["test_execution", "test_generation"],
            "unit_testing": ["unit_testing", "test_execution"],
            "end_to_end_testing": ["end_to_end_testing", "test_execution"],
            "regression_testing": ["regression_testing", "test_execution"],
            "delivery_packaging": ["delivery_packaging", "artifact_integration"],
            "artifact_integration": ["artifact_integration", "cross_artifact_consistency"],
            "final_synthesis": ["final_synthesis", "delivery_manifest_generation"],
            # Vision → Vision profile tokens
            "visualization": ["image_analysis"],
            "image_analysis": ["image_analysis"],
            "document_ocr": ["document_ocr"],
            # Tool → Tool profile tokens
            "tool_usage": ["python_execution", "shell_commands"],
            "api_integration": ["api_calls"],
            "python_execution": ["python_execution"],
            # General reasoning → Reasoning profile tokens
            "writing": ["knowledge_generation"],
            "translation": ["cross_artifact_consistency"],
            "summarization": ["research_synthesis", "comparison"],
            "planning": ["constraint_reasoning", "scheme_evaluation"],
            "verification": ["evidence_check", "validation"],
            "critique": ["risk_identification", "comparison"],
            "general_reasoning": ["constraint_reasoning", "causal_inference"],
            # Recovery → Recovery profile tokens
            "error_recovery": ["error_recovery"],
            "plan_repair": ["plan_repair"],
            "exception_handling": ["exception_handling"],
        }
        # Expand required with aliases — add actual profile capability tokens
        expanded_required = set(required)
        for cap in list(required):
            alias_tokens = _capability_aliases.get(cap)
            if alias_tokens:
                expanded_required.update(alias_tokens)

        # LLM planners may produce a capability name not present in the
        # explicit alias table.  Use the registered profile catalogue as the
        # ontology boundary and add only profile tokens sharing a meaningful
        # word stem with the requested capability.  This is deliberately
        # profile-driven: it never invents a role or silently accepts a zero
        # match, and it also works for future task domains.
        def capability_terms(value: str) -> set[str]:
            return {
                token for token in re.split(r"[^a-z0-9]+", str(value).lower())
                if len(token) >= 5 and token not in {
                    # These are broad planning vocabulary.  Treating them as
                    # semantic stems makes research_design accidentally match
                    # api_design/database_design and is exactly how a coding
                    # node was misrouted to a Research/implementation mix.
                    "other", "general", "level", "design", "analysis",
                    "evaluation", "research", "methodology", "development",
                    "testing", "verification", "planning", "management",
                    "generation", "execution", "validation", "analysis",
                }
            }

        catalogue_tokens = set()
        for profile in profiles:
            profile_caps = getattr(profile, "capabilities", []) if not isinstance(profile, dict) else profile.get("capabilities", [])
            catalogue_tokens.update(str(item).lower() for item in (profile_caps or []))
        for cap in list(required):
            # An explicit alias is already the authoritative ontology
            # mapping.  Do not additionally stem-match it against every
            # catalogue capability, otherwise e.g. ``technical_analysis``
            # can leak generic ``analysis`` into unrelated profiles.
            if cap in _capability_aliases:
                continue
            requested_terms = capability_terms(cap)
            if not requested_terms:
                continue
            for catalogue_cap in catalogue_tokens:
                catalogue_terms = capability_terms(catalogue_cap)
                if requested_terms & catalogue_terms:
                    expanded_required.add(catalogue_cap)
        required = expanded_required - reasoning_attributes
        model_by_preference = {
            "small_fast": "qwen-turbo",
            "medium_balanced": "qwen3.7-plus",
            "large_reasoning": "qwen3.7-max",
            "vision_specialized": "qwen3-vl-8b-instruct",
            "architect_reasoning": "qwen3.7-max",
            "coding_specialized": "qwen3-coder-plus",
            "testing_balanced": "qwen-plus",
            "verifier_reasoning": "qwen3.7-max",
            "synthesis_reasoning": "qwen3.7-max",
        }
        # A CLI --model override is an explicit operator decision.  Keep the
        # heterogeneous worker profile and resource selection intact, but use
        # the requested model for every worker in that run.  Without this
        # override, a Research profile silently changed qwen-flash back to the
        # profile default qwen3.7-max.
        forced_model = str(self.forced_worker_model or "").strip()
        resource_by_tier: Dict[str, List[Dict[str, Any]]] = {}
        for resource in resources or []:
            if isinstance(resource, dict):
                resource_by_tier.setdefault(str(resource.get("tier", "")).lower(), []).append(resource)
        synthesis_terms = {
            "artifact_integration", "delivery_packaging",
            "delivery_manifest_generation", "cross_artifact_consistency",
            "final_synthesis",
        }
        synthesis_required = bool(required & synthesis_terms)
        candidates = []
        for profile in profiles:
            def value(name: str, default: Any = "") -> Any:
                return getattr(profile, name, default) if not isinstance(profile, dict) else profile.get(name, default)
            capabilities = {str(item).lower() for item in (value("capabilities", []) or [])}
            capabilities -= reasoning_attributes
            match = len(required & capabilities) / max(1, len(required)) if required else 0.5
            tier = str(value("resource_tier", "cloud")).lower()
            tier_fit = 1.0 if not target or target == tier else 0.35
            availability = 1.0
            tier_resources = resource_by_tier.get(tier, [])
            if tier_resources and not any(resource.get("available", True) for resource in tier_resources):
                availability = 0.0
            success = float(history.get(str(value("worker_type", "")), 0.5))
            latency = float(value("max_latency_seconds", 60.0) or 60.0)
            cost = str(value("cost_per_call", "medium")).lower()
            cost_penalty = {"low": 0.05, "medium": 0.12, "high": 0.2}.get(cost, 0.12)
            risk_penalty = 0.12 if risk == "high" and tier == "cloud" else 0.0
            # Implementation intent is a capability-level signal, not a
            # task-specific rule.  When the requested contract contains
            # software delivery capabilities, a Coding profile must outrank
            # a Research profile that only happens to share generic words
            # such as evaluation or analysis.
            implementation_terms = {
                "code_generation", "programming", "code_modification", "debugging",
                "refactoring", "backend_development", "frontend_development",
                "web_app_development", "software_implementation", "implementation",
                "full_stack_development", "ui_development", "database_migration",
                "deployment", "containerization", "build_automation",
                "dependency_management",
            }
            # A Synthesizer may write a manifest or package files, but it must
            # not be reclassified as a Coding Worker merely because an LLM
            # added generic ``implementation``/``code_generation`` tokens.
            # Software changes belong to an upstream implementation node;
            # final integration remains a distinct heterogeneous capability.
            implementation_required = bool(required & implementation_terms) and not synthesis_required
            implementation_fit = 1.0 if implementation_required and capabilities & implementation_terms else 0.0
            implementation_penalty = 0.20 if implementation_required and not (capabilities & implementation_terms) else 0.0
            architecture_terms = {
                "system_architecture", "requirement_analysis",
                "database_modeling", "state_machine_design",
                "permission_design",
            }
            architecture_required = bool(required & architecture_terms)
            architecture_fit = (
                1.0
                if architecture_required and capabilities & architecture_terms
                else 0.0
            )
            architecture_penalty = (
                0.40
                if architecture_required and not (capabilities & architecture_terms)
                else 0.0
            )
            testing_terms = {
                "test_execution", "unit_testing", "integration_testing",
                "end_to_end_testing", "api_testing", "regression_testing",
            }
            testing_required = bool(required & testing_terms)
            testing_fit = 1.0 if testing_required and capabilities & testing_terms else 0.0
            testing_penalty = 0.30 if testing_required and not (capabilities & testing_terms) else 0.0
            research_terms = {
                "literature_synthesis", "trend_analysis", "cross_domain_research",
                "research_design", "experimental_methodology", "experimental_design",
                "evaluation_metrics_design", "benchmark_design", "evaluation_design",
                "knowledge_generation", "knowledge_base", "academic_writing",
            }
            research_required = bool(required & research_terms)
            research_fit = 1.0 if research_required and capabilities & research_terms else 0.0
            research_penalty = 0.18 if research_required and not (capabilities & research_terms) else 0.0
            score = max(0.0, min(1.0, 0.45 * match + 0.2 * tier_fit + 0.15 * availability + 0.15 * success - cost_penalty - risk_penalty + 0.05 * (1.0 / max(1.0, latency / 30.0)) + 0.25 * implementation_fit - implementation_penalty + 0.40 * architecture_fit - architecture_penalty + 0.30 * testing_fit - testing_penalty + 0.12 * research_fit - research_penalty))
            candidates.append({
                "profile_id": str(value("worker_type", "unknown")),
                "worker_type": str(value("worker_type", "unknown")),
                "capability_match": round(match, 4),
                "resource_fit": round(tier_fit, 4),
                "availability": round(availability, 4),
                "history_success": round(success, 4),
                "score": round(score, 4),
                "resource_tier": tier,
                "model_preference": str(value("model_preference", "")),
                "selected_model": forced_model or model_by_preference.get(str(value("model_preference", "")), "qwen3.7-max"),
                "reasoning_level": str(value("reasoning_level", "medium")),
                "tools": list(value("tools", []) or []),
                "capabilities": sorted(capabilities),
                "implementation_fit": implementation_fit,
                "architecture_fit": architecture_fit,
                "testing_fit": testing_fit,
                "research_fit": research_fit,
                "input_contract": str(value("input_contract", "")),
                "output_contract": str(value("output_contract", "")),
                "context_policy": dict(value("metadata", {}) or {}).get("context_policy", {}),
                "reason_codes": ["CAPABILITY_MATCH" if match > 0 else "CAPABILITY_GAP", "RESOURCE_FIT" if tier_fit >= 1 else "RESOURCE_FALLBACK"],
            })
        candidates.sort(key=lambda item: item["score"], reverse=True)
        # The LLM matcher is only allowed to choose from profiles that have
        # at least one normalized capability match whenever requirements are
        # present.  This prevents a valid partial match from being discarded
        # because the model selected an unrelated profile from the catalogue.
        matched_candidates = [
            item for item in candidates
            if not required or item.get("capability_match", 0) > 0
        ]
        matcher_candidates = matched_candidates or candidates
        # The real LLM selects a registered profile; code still validates the
        # selection and computes capability coverage.
        selected = (
            (matched_candidates[0] if matched_candidates else candidates[0])
            if candidates and not self.llm_matching_enabled
            else None
        )
        # When lexical/capability normalization finds no candidate, the real
        # Matcher must still inspect the full registered catalogue.  This is
        # the generic semantic matching path for domain-specific vocabulary;
        # only an explicit null decision leaves the task as a true capability
        # gap that requires Governor expansion or replanning.
        semantic_match = False
        matcher_profile_rejected = False
        llm_match = self._llm_worker_match(task, matcher_candidates or candidates)
        if llm_match and llm_match.get("worker_type"):
            llm_selected = next(
                (item for item in candidates if item["worker_type"] == llm_match["worker_type"]),
                None,
            )
            if llm_selected is not None:
                # The LLM may rank profiles, but cannot override a hard
                # capability boundary.  In particular, an implementation
                # task must not be routed to a research profile merely
                # because both profiles mention evaluation/analysis.
                llm_is_implementation = bool(
                    set(llm_selected.get("capabilities", [])) & implementation_terms
                )
                llm_is_testing = bool(
                    set(llm_selected.get("capabilities", [])) & testing_terms
                )
                llm_is_architecture = bool(
                    set(llm_selected.get("capabilities", [])) & architecture_terms
                )
                llm_match_allowed = (
                    (not implementation_required or llm_is_implementation)
                    and (not architecture_required or llm_is_architecture)
                    and (not testing_required or llm_is_testing)
                )
                if llm_match_allowed:
                    selected = llm_selected
                    selected["llm_match_confidence"] = llm_match["confidence"]
                    selected["reason_codes"] = list(dict.fromkeys(
                        selected.get("reason_codes", []) + ["LLM_PROFILE_MATCH"] + llm_match["reason_codes"]
                    ))
                    semantic_match = not bool(llm_selected.get("capability_match", 0))
                    if semantic_match:
                        selected["reason_codes"] = list(dict.fromkeys(
                            selected.get("reason_codes", []) + ["SEMANTIC_PROFILE_MATCH"]
                        ))
                else:
                    matcher_profile_rejected = True
                    # Do not retain the deterministic preselection after the
                    # LLM matcher has proposed an incompatible profile.  The
                    # validator below must rematch from the catalogue; keeping
                    # the old selection could execute a different Worker than
                    # the audit decision records.
                    selected = None
                    self.trace_recorder.record(EventType.WORKER_MATCHER_REJECTED, {
                        "task_id": task.get("task_id") or task.get("node_id"),
                        "worker_type": llm_selected.get("worker_type"),
                        "reason": "implementation_capability_boundary",
                        "required_capabilities": sorted(required),
                    })

        # The deterministic capability validator is authoritative. If the
        # semantic matcher proposes an incompatible profile, continue with
        # the highest-ranked registered profile that has real capability
        # coverage and satisfies the hard implementation boundary. This is a
        # validated rematch, not a generic-agent fallback.
        if selected is None and matcher_profile_rejected:
            validated_candidates = [
                item
                for item in candidates
                if item.get("capability_match", 0) > 0
                and (
                    not implementation_required
                    or bool(set(item.get("capabilities", [])) & implementation_terms)
                )
                and (
                    not architecture_required
                    or bool(set(item.get("capabilities", [])) & architecture_terms)
                )
                and (
                    not testing_required
                    or bool(set(item.get("capabilities", [])) & testing_terms)
                )
            ]
            if validated_candidates:
                selected = validated_candidates[0]
                selected["reason_codes"] = list(dict.fromkeys(
                    selected.get("reason_codes", [])
                    + ["CAPABILITY_VALIDATOR_REMATCH"]
                ))
                self.trace_recorder.record(EventType.WORKER_SELECTED, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "selected": selected,
                    "candidate_count": len(candidates),
                    "required_capabilities": sorted(required),
                    "selection_source": "capability_validator_rematch",
                })

        # Zero-match gate: if required capabilities exist and the best candidate
        # has capability_match=0, reject selection — do NOT silently pick a
        # zero-match worker. This prevents Vision being chosen for literature_search.
        if selected and required and selected.get("capability_match", 0) == 0 and not semantic_match:
            task_id = task.get("task_id") or task.get("node_id")
            self.trace_recorder.record(EventType.WORKER_REJECTED_ZERO_MATCH, {
                "task_id": task_id,
                "required_capabilities": sorted(required),
                "best_candidate": selected.get("worker_type"),
                "best_score": selected.get("score"),
                "action": "governor_must_expand_capability_catalogue_or_replan",
            })
            decision = {
                "selected": None,
                "candidates": candidates,
                "required_capabilities": sorted(required),
                "task_id": task.get("task_id") or task.get("node_id"),
                "reason_codes": ["ZERO_CAPABILITY_MATCH_REJECTED"],
                "rejection": {
                    "reason": "No candidate has any matching capability",
                    "required": sorted(required),
                    "action_required": "expand_catalogue_or_replan",
                },
            }
            return decision
        
        decision = {
            "selected": selected,
            "candidates": candidates,
            "matched_candidate_count": len(matched_candidates),
            "semantic_match": semantic_match,
            "required_capabilities": sorted(required),
            "task_id": task.get("task_id") or task.get("node_id"),
            "reason_codes": list((selected or {}).get("reason_codes", ["NO_CAPABILITY_CANDIDATE"]))
            + (["GOAL_CAPABILITY_INFERRED"] if inferred_required else []),
            "capability_inference": {
                "source": "goal_semantics" if inferred_required else "explicit_task_contract",
                "goal_implementation": goal_implementation,
                "goal_retrieval": goal_retrieval,
                "inferred_required_capabilities": inferred_required,
            },
        }
        self.trace_recorder.record(EventType.WORKER_SELECTED, {
            "task_id": decision["task_id"],
            "selected": selected,
            "candidate_count": len(candidates),
            "required_capabilities": sorted(required),
            "capability_inference": decision.get("capability_inference", {}),
        })
        return decision

    def verify_evidence_summaries(
        self,
        contract: Dict[str, Any],
        reducers: Dict[str, Dict[str, Any]],
        *,
        artifact_refs: Optional[List[str]] = None,
        task_version: Optional[int] = None,
        run_dir: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Verify reducer evidence before outline/final synthesis."""
        results = []
        required = {
            "group_id", "source_nodes", "verified_claims", "conflicts",
            "coverage_gaps", "constraints", "recommended_followups",
            "source_artifacts",
        }
        for group_id, summary in reducers.items():
            schema_ok = isinstance(summary, dict) and required.issubset(summary)
            claims = summary.get("verified_claims", []) if isinstance(summary, dict) else []
            evidence_ok = all(
                isinstance(claim, dict)
                and str(claim.get("claim", "")).strip()
                and (claim.get("evidence_refs") or claim.get("source_nodes"))
                for claim in claims
            )
            results.append({
                "group_id": group_id,
                "status": "pass" if schema_ok and evidence_ok else "fail",
                "checks": {"schema": schema_ok, "claim_evidence_binding": evidence_ok},
                "source_nodes": summary.get("source_nodes", []) if isinstance(summary, dict) else [],
                "explanation": "结构化摘要和主张证据绑定有效" if schema_ok and evidence_ok else "摘要字段缺失或主张没有证据引用",
            })
        failures = [item for item in results if item["status"] == "fail"]
        # Reducers distinguish evidence gaps from work that belongs to the
        # synthesis or delivery stage.  Only a required evidence gap blocks
        # the pre-synthesis verifier; later-stage work is retained for the
        # Final Synthesizer and Delivery Gate rather than causing a false
        # pre-synthesis replan.
        coverage_gaps = []
        deferred_gaps = []
        external_policy = str(contract.get("external_evidence_policy", "required_if_explicit"))
        required_output_names = [
            str(item.get("name", "")).lower()
            for item in contract.get("required_outputs", [])
            if isinstance(item, dict)
        ]
        contract_text = json.dumps(contract, ensure_ascii=False).lower()
        explicit_contract_paths = set(
            re.findall(r"deliverables/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*", contract_text)
        )

        def _path_exists(value: str) -> bool:
            if not run_dir:
                return False
            candidate = str(value).replace("\\", "/").strip()
            if not candidate:
                return False
            path = Path(str(run_dir)) / Path(candidate)
            if path.exists():
                return True
            if candidate.startswith("deliverables/"):
                staged = Path(str(run_dir)) / ".delivery_staging" / Path(candidate.split("/", 1)[1])
                if staged.exists():
                    return True
            return False

        def _is_contract_path_gap(text: str) -> bool:
            """Reject only path obligations that the contract actually names.

            Reducers are allowed to describe semantic evidence gaps, but they
            must not turn an invented filename into a new delivery contract.
            A path is blocking only when it is explicitly named by the
            contract, or when the same physical path already exists in the
            current run.  Directory-level contracts are handled separately:
            an existing non-empty directory satisfies the directory evidence
            and an invented child filename is downgraded rather than replanned.
            """
            paths = re.findall(r"deliverables/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*", text.lower())
            if not paths:
                return True
            for raw in paths:
                normalized = raw.rstrip("/")
                if normalized in explicit_contract_paths or _path_exists(normalized):
                    continue
                # A reducer may mention a child file under a declared
                # directory.  That is a real gap only when the declared
                # directory itself is absent/empty; an existing non-empty
                # directory already satisfies the directory-level contract.
                declared_parent = next(
                    (
                        declared
                        for declared in explicit_contract_paths
                        if normalized.startswith(declared.rstrip("/") + "/")
                    ),
                    None,
                )
                if declared_parent:
                    parent = Path(str(run_dir)) / Path(declared_parent) if run_dir else None
                    if parent and parent.is_dir() and any(item.is_file() for item in parent.rglob("*")):
                        return False
                    return True
                # A path outside the explicit contract is not allowed to
                # create a new delivery obligation.  Keep it as a deferred
                # diagnostic instead of triggering dynamic graph expansion.
                return False
            return True
        synthesis_markers = {
            "document content", "complete content", "full text", "draft", "section",
            "文档内容", "完整内容", "全文", "章节", "文本草案", "交付内容",
        }
        external_markers = {
            "external", "database", "patent", "paper", "专利", "论文", "数据库",
            "外部", "来源", "检索",
        }
        for gid, item in reducers.items():
            if not isinstance(item, dict):
                continue
            for gap in item.get("coverage_gaps", []) or []:
                normalized = gap if isinstance(gap, dict) else {
                    "gap": str(gap), "severity": "major",
                    "required_for_delivery": True,
                    "blocking_stage": "evidence",
                }
                entry = {"group_id": gid, **normalized}
                stage = str(normalized.get("blocking_stage") or "evidence").lower()
                gap_text = str(normalized.get("gap", "")).lower()
                evidence_needed = str(normalized.get("evidence_needed", "")).lower()
                combined_gap = f"{gap_text} {evidence_needed}"
                if stage == "evidence" and not _is_contract_path_gap(combined_gap):
                    entry["blocking_stage"] = "synthesis"
                    entry["required_for_delivery"] = False
                    entry["reason_code"] = "UNDECLARED_PATH_OBLIGATION"
                    deferred_gaps.append(entry)
                    continue
                # Reducers are not allowed to promote a final-output shaping
                # obligation into an evidence dependency.  Likewise, when a
                # task explicitly permits unavailable external sources to be
                # marked pending, keep that uncertainty in the delivery
                # contract rather than repeatedly expanding the DAG.
                output_owned = any(name and name in combined_gap for name in required_output_names)
                synthesis_owned = output_owned or any(marker in combined_gap for marker in synthesis_markers)
                optional_external = external_policy == "optional_when_unavailable" and any(
                    marker in combined_gap for marker in external_markers
                )
                if stage == "evidence" and (synthesis_owned or optional_external):
                    entry["blocking_stage"] = "synthesis" if synthesis_owned else "delivery"
                    entry["required_for_delivery"] = bool(normalized.get("required_for_delivery", True))
                    deferred_gaps.append(entry)
                elif bool(normalized.get("required_for_delivery", True)) and stage == "evidence":
                    coverage_gaps.append(entry)
                else:
                    deferred_gaps.append(entry)
        if coverage_gaps:
            overall = "fail"
        elif reducers and not failures:
            overall = "pass"
        elif results and len(failures) < len(results):
            overall = "partial"
        else:
            overall = "fail"
        report = {
            "verifier": "GlobalVerifier",
            "overall_status": overall,
            "criterion_results": results,
            "missing_outputs": [] if reducers else ["structured_reducer_summaries"],
            "conflicts": [
                {"group_id": gid, "items": item.get("conflicts", [])}
                for gid, item in reducers.items() if isinstance(item, dict) and item.get("conflicts")
            ],
            "coverage_gaps": coverage_gaps,
            "deferred_gaps": deferred_gaps,
            "optional_gaps": deferred_gaps,
            "failed_tests": [item["group_id"] for item in failures],
            "recommended_recovery_tasks": [],
            "task_version": task_version or self.goal_version,
            "source_artifacts": list(artifact_refs or []),
        }
        self.trace_recorder.record(EventType.VERIFIER_COMPLETED, {
            "overall_status": report["overall_status"],
            "group_count": len(reducers),
            "failed_groups": report["failed_tests"],
        })
        return report

    def verify_delivery(
        self,
        contract: Dict[str, Any],
        *,
        content: str,
        artifact_refs: Optional[List[str]] = None,
        run_dir: Optional[Any] = None,
        client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Run a domain-neutral global verification over the proposed delivery."""
        content = str(content or "")
        artifact_refs = [str(ref) for ref in (artifact_refs or [])]
        results: List[Dict[str, Any]] = []

        def resolve_artifact(ref: str) -> Optional[Path]:
            if run_dir is None:
                return None
            raw = Path(ref)
            candidates = [raw] if raw.is_absolute() else [Path(run_dir) / raw]
            if raw.parts and raw.parts[0].lower() == "deliverables":
                candidates.append(Path(run_dir) / ".delivery_staging" / Path(*raw.parts[1:]))
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.is_file():
                    return resolved
            return None

        artifact_catalog: List[Dict[str, Any]] = []
        failing_test_reports: List[str] = []
        invalid_test_reports: List[str] = []
        for ref in artifact_refs:
            path = resolve_artifact(ref)
            if path is None:
                continue
            entry: Dict[str, Any] = {"ref": ref, "bytes": path.stat().st_size}
            is_text = path.suffix.casefold() in {
                ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml",
                ".html", ".log", ".csv",
            }
            if is_text and any(
                marker in path.name.casefold()
                for marker in ("evidence", "report", "result", "test", "health", "manifest", "tree")
            ):
                try:
                    entry["sample"] = path.read_text(
                        encoding="utf-8", errors="ignore"
                    )[:8000]
                except OSError:
                    pass
            if path.suffix.casefold() == ".json" and any(
                marker in path.name.casefold() for marker in ("report", "result", "test")
            ):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    payload = None
                failure_count = 0
                if isinstance(payload, dict):
                    generated_at = payload.get("generated_at")
                    generated_timestamp = None
                    if generated_at:
                        try:
                            generated_timestamp = datetime.fromisoformat(
                                str(generated_at).replace("Z", "+00:00")
                            )
                            if generated_timestamp.tzinfo is None:
                                generated_timestamp = generated_timestamp.replace(tzinfo=timezone.utc)
                        except (TypeError, ValueError):
                            generated_timestamp = None
                    file_timestamp = datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=timezone.utc,
                    )
                    # A runtime report whose internal clock differs wildly
                    # from the file actually delivered is not execution
                    # evidence. This catches copied templates and model-made
                    # reports while remaining domain-neutral.
                    provenance_valid = bool(
                        generated_timestamp is not None
                        and abs((file_timestamp - generated_timestamp.astimezone(timezone.utc)).total_seconds()) <= 3600
                    )
                    summary_for_shape = payload.get("summary")
                    total_cases = None
                    if isinstance(summary_for_shape, dict):
                        total_cases = summary_for_shape.get(
                            "total",
                            summary_for_shape.get("total_cases"),
                        )
                    has_case_evidence = bool(
                        isinstance(payload.get("tests"), list)
                        or isinstance(payload.get("suites"), list)
                    )
                    if (
                        not provenance_valid
                        or not isinstance(total_cases, (int, float))
                        or int(total_cases) <= 0
                        or not has_case_evidence
                    ):
                        invalid_test_reports.append(ref)
                        entry["machine_test_provenance_valid"] = False
                    summary = payload.get("summary")
                    scopes = [payload, summary] if isinstance(summary, dict) else [payload]
                    for scope in scopes:
                        for key in ("failed", "failures", "errors", "error_count", "failure_count"):
                            value = scope.get(key)
                            if isinstance(value, (int, float)):
                                failure_count += max(0, int(value))
                            elif isinstance(value, list):
                                failure_count += len(value)
                    tests = payload.get("tests")
                    if isinstance(tests, list):
                        failure_count = max(
                            failure_count,
                            sum(
                                1 for item in tests
                                if isinstance(item, dict) and item.get("passed") is False
                            ),
                        )
                if failure_count > 0:
                    failing_test_reports.append(ref)
                    entry["machine_test_failures"] = failure_count
            artifact_catalog.append(entry)

        def criterion(criterion_id: str, name: str, passed: bool, explanation: str, refs: Optional[List[str]] = None) -> None:
            results.append({
                "criterion_id": criterion_id,
                "name": name,
                "status": "pass" if passed else "fail",
                "evidence_refs": refs or [],
                "explanation": explanation,
            })

        criterion("AC-OUTPUT", "non_empty_delivery", bool(content.strip()), "交付内容非空")
        for index, section in enumerate(contract.get("required_sections", []) or [], 1):
            title = str(section)
            criterion(f"AC-SECTION-{index:02d}", f"required_section:{title}", title in content, "已找到要求结构" if title in content else "缺少要求结构")
        for index, claim in enumerate(contract.get("forbidden_claims", []) or [], 1):
            marker = str(claim)
            criterion(f"AC-FORBIDDEN-{index:02d}", f"forbidden_claim_absent:{marker}", marker not in content, "未发现禁止声明" if marker not in content else "发现禁止声明")
        acceptance_criteria = contract.get("global_acceptance_criteria", []) or []
        completion_evidence = contract.get("completion_evidence", []) or []
        evidence_policy = str(contract.get("execution_evidence_policy") or "mixed")
        has_required_structure = bool(content.strip()) and all(
            str(section) in content for section in (contract.get("required_sections", []) or [])
        )
        has_audit_markers = any(
            marker in content
            for marker in ("完成状态", "执行状态", "验证状态", "证据", "验收", "待执行", "未执行", "已完成")
        )
        for index, rule in enumerate(acceptance_criteria, 1):
            # Acceptance criteria are semantic predicates, not strings the
            # final response must repeat.  With a real verifier, keep the
            # predicate pending until it is judged against actual artifacts;
            # never fail a file/code task merely because its prose omits the
            # wording of the predicate.
            rule_text = str(rule)
            literal_pass = rule_text in content
            if client is not None:
                results.append({
                    "criterion_id": f"AC-RULE-{index:02d}",
                    "name": rule_text,
                    "status": "unknown",
                    "evidence_refs": [],
                    "explanation": "等待 Global Verifier 基于真实交付物和执行证据判断",
                })
            else:
                criterion(
                    f"AC-RULE-{index:02d}",
                    rule_text,
                    literal_pass,
                    "文本中存在验收条件表述" if literal_pass else "需要人工或专用 Verifier 复核",
                )

        # Verify output-artifact binding: each required output with required=true
        # must have a corresponding artifact or evidence of generation.
        for index, output in enumerate(contract.get("required_outputs", []) or [], 1):
            if not isinstance(output, dict):
                continue
            if not output.get("required", True):
                continue
            output_name = str(output.get("name", ""))
            output_type = str(output.get("type", "other"))
            # Match artifacts by the output contract, not only by literal
            # filename equality.  A contract normally describes a semantic
            # output (for example, "机器可读 JSON 摘要"), while the runtime
            # uses stable technical filenames (for example, ``summary.json``).
            # The mapping remains domain-neutral and is driven by output type
            # and format/path terms in the contract.
            output_criteria = " ".join(
                str(item) for item in output.get("acceptance_criteria", []) or []
            )
            contract_text = (output_name + " " + output_criteria).lower()
            directory_terms = [
                match.group(0).rstrip("/")
                for match in re.finditer(
                    r"deliverables/[a-z0-9_.-]+/", contract_text
                )
            ]
            format_terms = {
                "json": ("json", ".json"),
                "markdown": ("markdown", ".md"),
                "md": ("markdown", ".md"),
                "html": ("html", ".html"),
                "csv": ("csv", ".csv"),
                "pdf": ("pdf", ".pdf"),
            }
            required_suffix = next(
                suffix for token, suffix in format_terms.values()
                if token in contract_text
            ) if any(token in contract_text for token, _ in format_terms.values()) else None
            path_output = any(token in contract_text for token in ("路径", "path", "文件位置", "location"))
            matched = False
            for ref in artifact_refs:
                ref_lower = str(ref).lower()
                if directory_terms and any(
                    ref_lower.startswith(term + "/")
                    or ref_lower == term
                    for term in directory_terms
                ):
                    matched = True
                    break
                if output_name.lower() and output_name.lower() in ref_lower:
                    matched = True
                    break
                if required_suffix and ref_lower.endswith(required_suffix):
                    matched = True
                    break
                if path_output and any(token in ref_lower for token in ("path", "manifest", "location")):
                    matched = True
                    break
            # A file output must point to an existing artifact.  For a
            # semantic file name, the contract-aware checks above identify the
            # intended file; do not accept an unrelated arbitrary file.
            if matched and output_type in {"file", "files", "software", "data"} and run_dir is not None:
                def artifact_exists(ref: Any) -> bool:
                    ref_path = Path(str(ref))
                    candidates = [run_dir / ref_path]
                    # Delivery Gate evaluates a draft before promotion.  The
                    # draft lives under .delivery_staging while its public
                    # reference intentionally remains deliverables/....
                    if ref_path.parts and ref_path.parts[0].lower() == "deliverables":
                        candidates.append(run_dir / ".delivery_staging" / Path(*ref_path.parts[1:]))
                    return any(candidate.resolve().exists() for candidate in candidates)

                matched = any(
                    artifact_exists(ref)
                    for ref in artifact_refs
                    if (
                        (directory_terms and any(
                            str(ref).lower().startswith(term + "/")
                            or str(ref).lower() == term
                            for term in directory_terms
                        ))
                        or
                        (output_name.lower() and output_name.lower() in str(ref).lower())
                        or (required_suffix and str(ref).lower().endswith(required_suffix))
                        or (path_output and any(token in str(ref).lower() for token in ("path", "manifest", "location")))
                    )
                )
            # Text deliverables are sections of the user-facing main document;
            # only file/software/data outputs require an independently bound
            # artifact.  Otherwise a valid mixed report is rejected once per
            # textual section merely because it has one canonical Markdown file.
            # A multi-output contract requires independently addressable
            # artifacts even when each artifact is textual.  Otherwise one
            # combined Markdown response can falsely satisfy four required
            # deliverables.  Single-output text contracts retain the compact
            # response behavior.
            required_output_count = sum(
                1 for item in (contract.get("required_outputs", []) or [])
                if isinstance(item, dict) and item.get("required", True)
            )
            binding_required = (
                (bool(contract.get("needs_artifact_refs", False)) or required_output_count > 1)
                and output_type in {"file", "files", "software", "data", "text", "markdown", "report"}
            )
            criterion(
                f"AC-OUTPUT-BIND-{index:02d}",
                f"output_binding:{output_name}",
                matched or not binding_required,
                f"输出物 '{output_name}' 已绑定 artifact" if matched else f"输出物 '{output_name}' 未找到对应 artifact",
            )

        # Verify completion evidence: check that expected evidence types are present
        for index, evidence in enumerate(completion_evidence, 1):
            evidence_text = str(evidence)
            # Use heuristic matching for descriptive evidence (not literal substring)
            has_evidence = False
            
            # Direct match
            if evidence_text in content or any(
                evidence_text.lower() in str(ref).lower() for ref in artifact_refs
            ):
                has_evidence = True
            else:
                # Heuristic: check if content structure matches evidence description
                evidence_lower = evidence_text.lower()
                if "章节结构" in evidence_text or "markdown" in evidence_lower:
                    # Check for markdown headers
                    has_evidence = bool(re.search(r"^#{1,3}\s+.+$", content, re.MULTILINE))
                elif "json" in evidence_lower or "结构化数据" in evidence_text:
                    # Check for JSON-like structure
                    has_evidence = bool(re.search(r"[{\[].*[}\]]", content, re.DOTALL))
                elif "验收指标" in evidence_text or "评测" in evidence_text:
                    # Check for evaluation-related keywords
                    eval_keywords = ["指标", "评测", "验证", "baseline", "metric", "accuracy", "f1", "precision"]
                    has_evidence = any(kw in content.lower() for kw in eval_keywords)
                elif "实验设计" in evidence_text or "失败判据" in evidence_text:
                    # Check for experiment design keywords
                    exp_keywords = ["实验", "对照", "基线", "失败判据", "实验设计", "experiment", "control"]
                    has_evidence = any(kw in content.lower() for kw in exp_keywords)
                # Unknown evidence descriptions are not satisfied by document
                # length. They remain unresolved until the Global Verifier
                # binds an explicit artifact or semantic proof.
            
            evidence_by_structure = (
                evidence_policy == "plan_only"
                and has_required_structure
                and has_audit_markers
            )
            criterion(
                f"AC-EVIDENCE-{index:02d}",
                f"completion_evidence:{evidence_text}",
                has_evidence or evidence_by_structure,
                "已找到完成证据" if (has_evidence or evidence_by_structure) else "缺少完成证据",
            )

        if failing_test_reports:
            criterion(
                "AC-EXECUTABLE-TESTS",
                "machine_readable_test_reports_have_no_failures",
                False,
                "机器可读测试报告仍包含失败或错误",
                failing_test_reports,
            )
        if invalid_test_reports:
            criterion(
                "AC-EXECUTION-PROVENANCE",
                "machine_readable_test_reports_are_current_and_traceable",
                False,
                "机器可读测试报告缺少本次执行时间或用例级证据，或内部时间与文件时间不一致",
                invalid_test_reports,
            )

        if client is not None and (acceptance_criteria or completion_evidence):
            semantic_criteria = [
                {
                    "criterion_id": item["criterion_id"],
                    "name": item["name"],
                }
                for item in results
                if item["criterion_id"].startswith(("AC-RULE-", "AC-EVIDENCE-"))
            ]
            verifier_prompt = f"""依据任务契约判断每条语义条件是否满足，只输出 JSON 数组。
每项格式：{{"criterion_id":"AC-RULE-01","status":"pass|fail|unknown","explanation":"...","evidence_refs":[]}}
交付证据策略：{evidence_policy}
如果策略是 plan_only，验收的是可执行、可追踪的计划交付物，不要求声称外部行动已经发生；
只要交付物明确区分计划、负责人、时间、依赖、完成状态和验证方式，就可以对计划类条件判定 pass。
如果策略是 runtime_required，外部行动必须有运行产物或明确 artifact 引用，否则判 unknown 或 fail。
任何策略下都不得把未发生的外部行动写成已完成。
待判定语义条件（必须逐项返回且保持 criterion_id）：{json.dumps(semantic_criteria, ensure_ascii=False)}
契约验收条件：{json.dumps(acceptance_criteria, ensure_ascii=False)}
            完成证据要求：{json.dumps(completion_evidence, ensure_ascii=False)}
            交付物清单：{json.dumps(contract.get('required_outputs', []), ensure_ascii=False)}
            已绑定 artifact 引用（这些路径必须结合实际存在性判断）：{json.dumps(artifact_refs, ensure_ascii=False)}
            优先证据目录（含真实文件摘要与机器测试失败数）：{json.dumps(artifact_catalog, ensure_ascii=False)[:60000]}
            运行目录：{str(run_dir or '')}
            交付内容：
{content[:30000]}
"""
            try:
                judged = _parse_governor_json(client.chat([
                    {"role": "system", "content": "你是全局交付验收器，只输出 JSON。"},
                    {"role": "user", "content": verifier_prompt},
                ]))
                if isinstance(judged, list):
                    semantic_ids = {item["criterion_id"] for item in semantic_criteria}
                    result_by_id = {item["criterion_id"]: item for item in results}
                    for index, item in enumerate(judged, 1):
                        if not isinstance(item, dict):
                            continue
                        criterion_id = str(item.get("criterion_id") or "")
                        if criterion_id not in semantic_ids:
                            continue
                        status = str(item.get("status") or "unknown")
                        if status not in {"pass", "fail", "unknown"}:
                            status = "unknown"
                        previous = result_by_id[criterion_id]
                        cited_refs = [
                            str(ref) for ref in item.get("evidence_refs", []) or []
                            if str(ref) in artifact_refs
                        ]
                        result_by_id[criterion_id] = {
                            "criterion_id": criterion_id,
                            "name": previous["name"],
                            "status": status,
                            "evidence_refs": cited_refs,
                            "explanation": str(item.get("explanation") or ""),
                        }
                    results = [result_by_id[item["criterion_id"]] for item in results]
            except Exception as exc:
                self.trace_recorder.record(EventType.DELIVERY_VERIFICATION_LLM_FAILED, {"error": str(exc)[:500]})
        if contract.get("needs_artifact_refs"):
            existing_refs = []
            for ref in artifact_refs:
                path = None
                if run_dir is not None:
                    try:
                        path = (run_dir / ref).resolve()
                    except (TypeError, OSError):
                        path = None
                if path is not None and path.exists():
                    existing_refs.append(ref)
            criterion("AC-ARTIFACT-REFS", "artifact references exist", bool(existing_refs), f"有效引用 {len(existing_refs)}/{len(artifact_refs)}", existing_refs)

        failures = [item for item in results if item["status"] == "fail"]
        unknowns = [item for item in results if item["status"] == "unknown"]
        # For a plan-only deliverable, an unknown conditional runtime result
        # is an auditable limitation, not a failed plan.  Hard failures still
        # block publication; unknowns remain visible in the verification JSON.
        if evidence_policy == "plan_only" and not failures:
            status = "pass"
        else:
            status = "pass" if not failures and not unknowns else ("partial" if len(failures) < len(results) else "fail")
        report = {
            "overall_status": status,
            "synthesis_authorized": status == "pass",
            "criterion_results": results,
            "missing_outputs": [],
            "unsupported_claims": [item["name"] for item in failures if "forbidden" in item["name"]],
            "conflicts": [],
            "failed_tests": [item["criterion_id"] for item in failures],
            "unknown_tests": [item["criterion_id"] for item in unknowns],
            "recommended_recovery_tasks": [],
            "task_version": contract.get("task_version", self.goal_version),
        }
        self.trace_recorder.record(EventType.DELIVERY_VERIFIED, {
            "overall_status": status,
            "criterion_count": len(results),
            "failure_count": len(failures),
            "task_version": report["task_version"],
        })
        return report

    def verify_synthesis_evidence(
        self,
        contract: Dict[str, Any],
        *,
        reducers: Dict[str, str],
        packets: List[Dict[str, Any]],
        artifact_refs: List[str],
        client: Any,
    ) -> Dict[str, Any]:
        """Globally verify reducer evidence before final synthesis.

        This is deliberately separate from ``run_delivery_gate``: the latter
        validates the final user-facing text, while this gate authorizes the
        synthesizer's evidence boundary.
        """
        reducer_text = "\n\n---\n\n".join(
            f"[{group_id}]\n{str(summary)[:7000]}"
            for group_id, summary in reducers.items()
        )
        prompt = f"""你是 Global Verifier，只验证最终汇总前的证据，不生成交付正文，只输出 JSON。
任务契约：{json.dumps(contract, ensure_ascii=False)}
交付证据策略：{contract.get('execution_evidence_policy', 'mixed')}
已批准证据包：{json.dumps(packets, ensure_ascii=False)}
Reducer 结果：
{reducer_text}

输出字段：
{{"overall_status":"pass|fail|partial","synthesis_authorized":true,
"supported_claims":[],"unsupported_claims":[],"conflicts":[],
"coverage_gaps":[],"required_fixes":[],"evidence_refs":[]}}
规则：只能把证据支持的内容放入 supported_claims；冲突、缺口和未验证内容不得授权为事实。
当 execution_evidence_policy=plan_only 时，用户要的是可执行计划/分析/设计，
条件性实验、外部联络、数据集下载或真实运行结果属于 limitations/open questions，
不得把它们当作当前交付的硬阻塞；最终正文必须明确“待验证/未执行”，并给出验证方案。
当 execution_evidence_policy=runtime_required 或 mixed 时，只有契约明确要求的运行证据才阻塞；
不能因为可选增强或后续实验尚未完成而无限扩图。"""
        try:
            parsed = _parse_governor_json(client.chat([
                {"role": "system", "content": "你是 Global Verifier，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ]))
        except Exception as exc:
            self.trace_recorder.record(EventType.SYNTHESIS_VERIFICATION_FAILED, {"error": str(exc)[:500]})
            parsed = None
        if not isinstance(parsed, dict):
            parsed = {
                "overall_status": "fail",
                "synthesis_authorized": False,
                "supported_claims": [], "unsupported_claims": [],
                "conflicts": [], "coverage_gaps": [],
                "required_fixes": ["invalid_global_verifier_output"],
                "evidence_refs": artifact_refs,
            }
        report = {
            "overall_status": str(parsed.get("overall_status") or "fail"),
            "synthesis_authorized": bool(parsed.get("synthesis_authorized", False)),
            "supported_claims": strings_from(parsed.get("supported_claims", [])),
            "unsupported_claims": strings_from(parsed.get("unsupported_claims", [])),
            "conflicts": strings_from(parsed.get("conflicts", [])),
            "coverage_gaps": strings_from(parsed.get("coverage_gaps", [])),
            "required_fixes": strings_from(parsed.get("required_fixes", [])),
            "evidence_refs": strings_from(parsed.get("evidence_refs", [])) or list(artifact_refs),
            "task_version": contract.get("task_version", self.goal_version),
        }
        # A plan-only contract may contain unresolved empirical follow-ups by
        # design.  Preserve them as auditable gaps, but do not turn optional
        # runtime work into an endless graph-expansion blocker.
        if contract.get("execution_evidence_policy") == "plan_only":
            report["synthesis_authorized"] = not bool(report["conflicts"])
            if report["coverage_gaps"] and report["overall_status"] == "fail":
                report["overall_status"] = "partial"
        self.trace_recorder.record(EventType.SYNTHESIS_VERIFICATION, {
            "overall_status": report["overall_status"],
            "synthesis_authorized": report["synthesis_authorized"],
            "supported_claim_count": len(report["supported_claims"]),
            "conflict_count": len(report["conflicts"]),
            "coverage_gap_count": len(report["coverage_gaps"]),
        })
        return report

    def run_delivery_gate(
        self,
        contract: Dict[str, Any],
        *,
        content: str,
        artifact_refs: Optional[List[str]] = None,
        run_dir: Optional[Any] = None,
        client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Verify and authorize delivery; the final writer cannot bypass this."""
        report = self.verify_delivery(contract, content=content, artifact_refs=artifact_refs, run_dir=run_dir, client=client)
        report["delivery_authorized"] = report["overall_status"] == "pass"
        if not report["delivery_authorized"]:
            report["recommended_recovery_tasks"] = [{
                "task_type": "delivery_repair",
                "goal": "修复未通过的交付契约条件",
                "failed_tests": report["failed_tests"],
            }]
        self.trace_recorder.record(EventType.DELIVERY_GATE, {
            "authorized": report["delivery_authorized"],
            "overall_status": report["overall_status"],
            "failed_tests": report["failed_tests"],
        })
        # Record delivery lifecycle event
        if report["delivery_authorized"]:
            self.trace_recorder.record(EventType.DELIVERY_READY, {
                "overall_status": report["overall_status"],
                "reason_codes": ["DELIVERY_AUTHORIZED"],
            })
        else:
            self.trace_recorder.record(EventType.DELIVERY_REJECTED, {
                "overall_status": report["overall_status"],
                "failed_tests": report["failed_tests"],
                "reason_codes": ["DELIVERY_NOT_AUTHORIZED"],
            })
        return report

    def plan_context_governance(
        self,
        task_content: str,
        packets: List[Dict[str, Any]],
        client: Any,
    ) -> ContextGovernanceDecision:
        """Choose semantic groups and a bounded working set for reducers.

        The model may retain, compress, defer, or remove packets from the
        current working set.  Raw node artifacts are never deleted here;
        deferred data remains available for later on-demand retrieval.
        """
        # Filter expired/superseded nodes before building catalog
        terminal_statuses = {"completed", "failed", "cancelled", "superseded"}
        expired_ids = set()
        active_packets = []
        for p in packets:
            nid = str(p.get("node_id"))
            status = str(p.get("status", "")).lower()
            task_ver = p.get("task_version", 0)
            # Exclude expired nodes and stale-version nodes
            if status in terminal_statuses:
                expired_ids.add(nid)
                continue
            if isinstance(task_ver, int) and task_ver < self.goal_version - 1:
                expired_ids.add(nid)
                continue
            active_packets.append(p)

        packet_ids = {str(packet.get("node_id")) for packet in active_packets}
        catalog = "\n".join(
            f"{p.get('node_id')} | confidence={p.get('confidence', 0.8)} | "
            f"task_version={p.get('task_version', 1)} | goal={str(p.get('goal', ''))[:300]} | "
            f"excerpt={str(p.get('conclusion', ''))[:500]}"
            for p in active_packets
        )
        expired_note = f"已排除过期/终结节点: {sorted(expired_ids)}" if expired_ids else ""
        goal_summary = ""
        if self.goal_spec:
            goal_summary = (
                f"目标ID: {self.goal_spec.goal_id}\n"
                f"任务版本: {self.goal_spec.task_version}\n"
                f"硬约束: {json.dumps(self.goal_spec.hard_constraints, ensure_ascii=False)}\n"
                f"成功标准: {json.dumps(self.goal_spec.success_criteria, ensure_ascii=False)}\n"
                f"禁止声明: {json.dumps(self.goal_spec.forbidden_claims, ensure_ascii=False)}"
            )
        budget_summary = (
            f"剩余token: {self.budget.remaining_tokens()}, "
            f"剩余时间: {self.budget.remaining_time()}s, "
            f"已用token: {self.budget.used_tokens}"
        )
        prompt = f"""你是超长程任务的全局上下文治理器，只输出 JSON。
这是通用任务，不得根据某个领域预设固定角色、固定分组数量或固定章节。
不得以 role 作为默认分组依据；分组必须基于语义相关性。

{expired_note}

当前目标上下文：
{goal_summary}

预算状态：
{budget_summary}

根据任务目标、硬约束和证据包目录决定当前 reducer 工作集：语义相关的节点组成子图；
每个节点只能进入一个子图。action 只能是 retain、compress、defer、drop_from_working_set、retrieve_on_demand。

action 字段级差异：
- retain：原样进入当前 reducer 工作集，包含完整输出、证据引用和 artifact 路径
- compress：进入 reducer 但仅保留结构化摘要字段：goal, decision, confidence, artifact_refs, limitations
- defer：暂不进入当前 reducer，保留 artifact 引用供按需检索，必须提供 wake_conditions
- drop_from_working_set：当前任务排除但原始 artifact 不得删除
- retrieve_on_demand：仅在下游 Worker/Reducer 提出匹配请求时读取原始 artifact
  触发条件：下游检测到证据不足、出现冲突、或 Verifier 要求核对
  返回格式：{{"status":"retrieved|lookup_required","node_id":"...","packet":{{...}},"wake_event":{{...}}}}

defer/retrieve_on_demand 必须提供 wake_conditions（唤醒条件必须具体：下游任务类型、缺失证据类型、冲突触发条件）。
每个被 defer 的包必须保存 wake_conditions，没有唤醒条件的 defer 会变成永久遗忘。
优先保留与硬约束和成功标准直接相关的证据。
每个节点必须有独立的验收边界。

返回：
{{"groups":[{{"group_id":"...","title":"语义相关标题","node_ids":["..."]}}],
"actions":[{{"node_id":"...","action":"retain|compress|defer|drop_from_working_set|retrieve_on_demand",
"reason":"...","wake_conditions":["..."],"compress_fields":["goal","decision","confidence","artifact_refs","limitations"]}}],
"reasoning":"..."}}
任务：
{task_content}
证据包目录：
{catalog}
"""
        try:
            parsed = _parse_governor_json(client.chat([
                {"role": "system", "content": "你是全局上下文治理器，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ]))
        except Exception as exc:
            self.trace_recorder.record(EventType.CONTEXT_GOVERNANCE_FAILED, {"error": str(exc)[:500]})
            raise RuntimeError(
                f"GlobalGovernor context governance failed: {str(exc)[:500]}"
            ) from exc
        if not isinstance(parsed, dict):
            self.trace_recorder.record(
                EventType.CONTEXT_GOVERNANCE_FAILED,
                {"error": "context governance response was not a JSON object"},
            )
            raise ValueError("GlobalGovernor context governance response was not a JSON object")
        actions: Dict[str, str] = {}
        for item in parsed.get("actions", []):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id", ""))
            action = str(item.get("action", "retain"))
            if node_id in packet_ids and action in {"retain", "compress", "defer", "drop_from_working_set", "retrieve_on_demand"}:
                actions[node_id] = action
        missing_actions = packet_ids - set(actions)
        if missing_actions:
            raise ValueError(
                "GlobalGovernor context decision omitted packet actions: "
                f"{sorted(missing_actions)}"
            )
        working = {
            node_id for node_id, action in actions.items()
            if action in {"retain", "compress"}
        }
        if not working:
            raise ValueError("GlobalGovernor context decision produced an empty working set")
        groups: List[Dict[str, Any]] = []
        assigned: Set[str] = set()
        for index, group in enumerate(parsed.get("groups", []), 1):
            if not isinstance(group, dict):
                continue
            ids = [str(item) for item in group.get("node_ids", [])]
            ids = [item for item in ids if item in working and item not in assigned]
            if not ids:
                continue
            assigned.update(ids)
            groups.append({
                "group_id": str(group.get("group_id") or f"G-{index:03d}"),
                "title": str(group.get("title") or "semantic-subgraph"),
                "node_ids": ids,
            })
        ungrouped_working = working - assigned
        if ungrouped_working:
            raise ValueError(
                "GlobalGovernor context decision omitted working packets from groups: "
                f"{sorted(ungrouped_working)}"
            )
        action_details: Dict[str, Dict[str, Any]] = {}
        for item in parsed.get("actions", []):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id", ""))
            if node_id not in packet_ids:
                continue
            details = {
                "action": actions[node_id],
                "reason": str(item.get("reason") or ""),
                "wake_conditions": strings_from(item.get("wake_conditions", [])),
            }
            if details["action"] in {"defer", "retrieve_on_demand"} and not details["wake_conditions"]:
                raise ValueError(
                    f"GlobalGovernor deferred packet without wake conditions: {node_id}"
                )
            action_details[node_id] = details
        decision = ContextGovernanceDecision(
            groups=[{**group, "node_ids": [node_id for node_id in group["node_ids"] if node_id in working]} for group in groups],
            retained_packet_ids=[node_id for node_id, action in actions.items() if action == "retain"],
            compressed_packet_ids=[node_id for node_id, action in actions.items() if action == "compress"],
            deferred_packet_ids=[node_id for node_id, action in actions.items() if action in {"defer", "drop_from_working_set", "retrieve_on_demand"}],
            action_details=action_details,
            task_version=self.goal_version,
            reason_codes=["CONTEXT_OVERFLOW" if len(packets) > 20 else "WORKING_SET_SELECTION"],
            reasoning=str(parsed.get("reasoning") or ""),
        )
        decision.groups = [group for group in decision.groups if group["node_ids"]]
        self.trace_recorder.record(EventType.CONTEXT_GOVERNANCE_DECIDED, {
            "packet_count": len(packets),
            "working_set_count": len(working),
            "retained_count": len(decision.retained_packet_ids),
            "compressed_count": len(decision.compressed_packet_ids),
            "deferred_count": len(decision.deferred_packet_ids),
            "group_count": len(decision.groups),
            "groups": decision.groups,
            "action_details": decision.action_details,
            "task_version": decision.task_version,
            "reason_codes": decision.reason_codes,
        })
        return decision

    def retrieve_on_demand(
        self,
        node_id: str,
        *,
        requester: str = "",
        wake_reason: str = "",
        packets: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Retrieve a deferred/on-demand packet when downstream tasks require it.

        This method implements the context governance wake mechanism: when a
        downstream task encounters evidence gaps, conflicts, or needs data that
        was previously deferred, it can call this method to retrieve the packet
        and bring it back into the working set.

        Args:
            node_id: The deferred node ID to retrieve.
            requester: The task/node requesting retrieval.
            wake_reason: Why retrieval is needed (evidence gap, conflict, etc.).
            packets: Optional packet list for lookup; if None, caller must
                     supply the packet separately.

        Returns:
            A dict with status, the packet (if found), and the wake event.
        """
        packet = None
        for p in packets or []:
            if str(p.get("node_id")) == str(node_id):
                packet = p
                break

        action_detail = {}
        for detail in (self.delivery_contract or {}).values():
            if isinstance(detail, dict) and detail.get("node_id") == node_id:
                action_detail = detail
                break

        wake_event = {
            "event_type": "CONTEXT_WAKE",
            "node_id": node_id,
            "requester": requester,
            "wake_reason": wake_reason,
            "task_version": self.goal_version,
            "timestamp": __import__("time").time(),
        }

        if packet is not None:
            self.trace_recorder.record(EventType.CONTEXT_WAKE, {
                **wake_event,
                "reason_codes": ["EVIDENCE_RETRIEVED"],
            })
            self.trace_recorder.record(EventType.RETRIEVE_ON_DEMAND_SUCCESS, wake_event)
            return {
                "status": "retrieved",
                "node_id": node_id,
                "packet": packet,
                "wake_event": wake_event,
            }

        # Packet not in local list — record intent so orchestrator can fetch
        self.trace_recorder.record(EventType.CONTEXT_WAKE, {
            **wake_event,
            "reason_codes": ["LOOKUP_REQUIRED"],
        })
        self.trace_recorder.record(EventType.RETRIEVE_ON_DEMAND_DEFERRED, wake_event)
        return {
            "status": "lookup_required",
            "node_id": node_id,
            "wake_event": wake_event,
            "instruction": "Orchestrator must load artifact for deferred node",
        }

    def handle_tool_loop_decision(
        self,
        *,
        node_id: str,
        tool_name: str,
        guard_decision: Dict[str, Any],
        role_toolset: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Handle ToolCallGuard recovery decisions at the Governor level.

        When a tool call triggers loop protection (repeated calls or failures),
        the Governor decides whether to:
        1. Switch to an alternative tool (if available and registered)
        2. Trigger a local replan (create a repair task node)

        Args:
            node_id: The task node experiencing the tool loop
            tool_name: The tool that triggered the guard
            guard_decision: The ToolCallGuard.decision() output
            role_toolset: Optional allow-list of tools for this worker role

        Returns:
            Dict with "strategy" (switch_tool | replan | halt), "alternative_tool"
            (if applicable), and "reason"
        """
        action = guard_decision.get("action", "halt")
        reason = guard_decision.get("reason", "unknown")
        repeat_count = guard_decision.get("repeat_count", 0)
        failure_count = guard_decision.get("failure_count", 0)

        # Strategy 1: Try to find an alternative tool
        if action == "switch_tool_or_replan":
            from AstralNexus.tools.tool_router import alternative_tools
            alts = alternative_tools(tool_name, role_toolset)
            if alts:
                alt = alts[0]
                self.trace_recorder.record(EventType.TOOL_LOOP_RECOVERY, {
                    "node_id": node_id,
                    "original_tool": tool_name,
                    "alternative_tool": alt,
                    "reason": reason,
                    "repeat_count": repeat_count,
                    "failure_count": failure_count,
                    "reason_codes": ["TOOL_SWITCH"],
                })
                return {
                    "strategy": "switch_tool",
                    "alternative_tool": alt,
                    "reason": f"Switching to alternative: {alt}",
                }

        # Strategy 2: Trigger local replan
        if action in ("switch_tool_or_replan", "halt"):
            # Check if we have budget for a repair task
            if self._count_active_tasks() < self.max_active_tasks:
                repair_goal = f"Replan task {node_id}: tool {tool_name} failed ({reason})"
                self.trace_recorder.record(EventType.TOOL_LOOP_REPLAN, {
                    "node_id": node_id,
                    "tool_name": tool_name,
                    "reason": reason,
                    "repeat_count": repeat_count,
                    "failure_count": failure_count,
                    "repair_goal": repair_goal,
                    "reason_codes": ["TOOL_LOOP_REPLAN"],
                })
                return {
                    "strategy": "replan",
                    "repair_goal": repair_goal,
                    "reason": f"Tool loop detected, creating repair task: {reason}",
                }

        # Strategy 3: Hard halt - no alternatives and no budget
        self.trace_recorder.record(EventType.TOOL_LOOP_RECOVERY, {
            "node_id": node_id,
            "tool_name": tool_name,
            "reason": reason,
            "repeat_count": repeat_count,
            "failure_count": failure_count,
            "reason_codes": ["TOOL_LOOP_HALT"],
        })
        return {
            "strategy": "halt",
            "reason": f"No alternatives available and repair budget exhausted: {reason}",
        }

    def evaluate_background_spawn(
        self,
        *,
        goal: str,
        parent_task_id: str,
        required_capabilities: List[str],
        expected_information_gain: float,
        risk_reduction: float,
        parallel_benefit: float,
        estimated_cost: int,
        acceptance_criteria: Optional[List[str]] = None,
    ) -> AdmissionDecision:
        """Admit a background Worker only when its expected value justifies it."""
        proposal = TaskProposal(
            goal=goal,
            required_capabilities=list(required_capabilities),
            expected_information_gain=float(expected_information_gain),
            risk_reduction=float(risk_reduction),
            parallel_benefit=float(parallel_benefit),
            estimated_cost=max(0, int(estimated_cost)),
            dependencies=[parent_task_id] if parent_task_id else [],
            parent_task_id=parent_task_id,
            acceptance_criteria=list(acceptance_criteria or []),
            metadata={
                "background": True,
                "goal_contribution": min(1.0, max(0.0, float(expected_information_gain + risk_reduction) / 2.0)),
            },
        )
        decision = self.evaluate_task_proposal(proposal)
        self.trace_recorder.record(EventType.BACKGROUND_SPAWN_DECIDED, {
            "goal": goal,
            "parent_task_id": parent_task_id,
            "admitted": decision.admitted,
            "value_score": decision.value_score,
            "reasoning": decision.reasoning,
            "rejection_reason": decision.rejection_reason,
        })
        return decision

    def evaluate_task_proposal(self, proposal: TaskProposal) -> AdmissionDecision:
        """Evaluate a task proposal for admission.
        
        Uses the value scoring formula from the advanced improvement spec:
        V(T) = w1*G + w2*P + w3*R + w4*I - w5*C - w6*D
        
        Where:
        - G: goal contribution
        - P: parallel benefit
        - R: risk reduction
        - I: information gain
        - C: estimated cost
        - D: redundancy/duplication
        
        Args:
            proposal: The task proposal to evaluate
            
        Returns:
            AdmissionDecision with approval status and reasoning
        """
        # Check hard constraints first
        if self.total_tasks_created >= self.max_total_tasks:
            return AdmissionDecision(
                admitted=False,
                value_score=0.0,
                reasoning="Maximum total tasks reached",
                rejection_reason="max_total_tasks_exceeded",
            )
        
        active_tasks = self._count_active_tasks()
        if active_tasks >= self.max_active_tasks:
            return AdmissionDecision(
                admitted=False,
                value_score=0.0,
                reasoning="Maximum active tasks reached",
                rejection_reason="max_active_tasks_exceeded",
            )
        
        if not self.budget.can_admit_task(proposal.estimated_cost):
            return AdmissionDecision(
                admitted=False,
                value_score=0.0,
                reasoning="Insufficient budget",
                rejection_reason="budget_exhausted",
            )
        
        # Check for duplicate tasks
        redundancy = self._check_redundancy(proposal)
        if redundancy > 0.8:
            return AdmissionDecision(
                admitted=False,
                value_score=0.0,
                reasoning=f"High redundancy with existing tasks ({redundancy:.2f})",
                rejection_reason="duplicate_task",
            )
        
        # Calculate value score
        # Weights from advanced improvement spec
        w_goal = 0.30
        w_parallel = 0.15
        w_risk = 0.20
        w_info = 0.20
        w_cost = 0.10
        w_redundancy = 0.05
        
        # Normalize cost to 0-1 range (assume max cost is 10000 tokens)
        normalized_cost = min(1.0, proposal.estimated_cost / 10000.0)
        
        # Goal contribution: use explicit value from metadata if provided,
        # otherwise default to 0.8 (generic heuristic)
        goal_contribution = proposal.metadata.get("goal_contribution", 0.8)
        
        value_score = (
            w_goal * goal_contribution
            + w_parallel * proposal.parallel_benefit
            + w_risk * proposal.risk_reduction
            + w_info * proposal.expected_information_gain
            - w_cost * normalized_cost
            - w_redundancy * redundancy
        )
        
        # Decision
        admitted = value_score >= self.admission_threshold
        
        reasoning = (
            f"Value score: {value_score:.3f} "
            f"(goal={0.8:.2f}, parallel={proposal.parallel_benefit:.2f}, "
            f"risk={proposal.risk_reduction:.2f}, info={proposal.expected_information_gain:.2f}, "
            f"cost={normalized_cost:.2f}, redundancy={redundancy:.2f})"
        )
        
        if admitted:
            reasoning += f" >= threshold {self.admission_threshold:.2f} -> ADMITTED"
        else:
            reasoning += f" < threshold {self.admission_threshold:.2f} -> REJECTED"
        
        self.total_tasks_created += 1
        if admitted:
            self.total_tasks_admitted += 1
        else:
            self.total_tasks_rejected += 1
        
        decision = AdmissionDecision(
            admitted=admitted,
            value_score=value_score,
            reasoning=reasoning,
            rejection_reason=None if admitted else "value_below_threshold",
        )
        
        self.admission_history.append(decision)
        
        self.trace_recorder.record(EventType.TASK_EVALUATED, {
            "goal": proposal.goal,
            "value_score": value_score,
            "admitted": admitted,
            "reasoning": reasoning,
        })
        
        return decision
    
    def admit_task(self, proposal: TaskProposal, decision: AdmissionDecision) -> Optional[str]:
        """Admit a task to the task graph after approval.
        
        Args:
            proposal: The approved task proposal
            decision: The admission decision (must be admitted=True)
            
        Returns:
            The created task node ID, or None if admission failed
        """
        if not decision.admitted:
            return None
        
        # Generate task ID — prefer caller-specified ID for plan decomposition
        task_id = proposal.preferred_node_id or f"dynamic-{self.total_tasks_admitted:03d}"
        
        # Create task node
        node = TaskNode(
            node_id=task_id,
            goal=proposal.goal,
            role=self._infer_role_from_capabilities(proposal.required_capabilities),
            status="admitted",
            risk_level="medium",
            sensitivity="internal",
            resource_tier="cloud",
            dependencies=proposal.dependencies,
            constraints=list(proposal.constraints),
            acceptance_criteria=proposal.acceptance_criteria,
            # Preserve the Governor's explicit artifact contract on the
            # canonical TaskNode.  Router derives the Worker input contract
            # from ``node.outputs``; dropping this field here made every
            # implementation node appear to have no deliverables.
            outputs=[
                str(item).strip()
                for item in (proposal.metadata.get("expected_outputs", []) or [])
                if str(item).strip()
            ],
            metadata={
                **proposal.metadata,
                "admission_value_score": decision.value_score,
                "estimated_cost": proposal.estimated_cost,
                "information_gain": proposal.expected_information_gain,
                "risk_reduction": proposal.risk_reduction,
                "parallel_benefit": proposal.parallel_benefit,
                "dynamic": True,
                "parent_task_id": proposal.parent_task_id,
            },
        )
        
        self.task_graph.add_node(node)
        
        # Add dependency edges
        for dep_id in proposal.dependencies:
            if dep_id in self.task_graph.nodes:
                self.task_graph.add_edge(dep_id, task_id, edge_type="dependency")
        
        decision.task_id = task_id
        
        self.trace_recorder.record(EventType.TASK_ADMITTED, {
            "task_id": task_id,
            "goal": proposal.goal,
            "value_score": decision.value_score,
            "dependencies": proposal.dependencies,
        })
        
        return task_id
    
    def update_budget(self, tokens_used: int = 0, time_used: float = 0.0, parallel_delta: int = 0):
        """Update budget usage after task execution.

        This method should be called by the orchestrator after each task
        completes or fails to maintain accurate budget accounting.
        """
        self.budget.used_tokens += tokens_used
        self.budget.used_time_seconds += time_used
        self.budget.active_parallel_tasks += parallel_delta

        # Clamp to non-negative
        if self.budget.active_parallel_tasks < 0:
            self.budget.active_parallel_tasks = 0

        self.trace_recorder.record(EventType.BUDGET_UPDATED, {
            "tokens_used": tokens_used,
            "time_used": time_used,
            "parallel_delta": parallel_delta,
            "total_tokens_used": self.budget.used_tokens,
            "total_time_used": self.budget.used_time_seconds,
            "active_parallel": self.budget.active_parallel_tasks,
            "remaining_tokens": self.budget.remaining_tokens(),
            "remaining_time": self.budget.remaining_time(),
        })

    def record_task_start(self, task_id: str, estimated_tokens: int = 0):
        """Record task start: occupy a parallel slot and reserve token budget.

        Called by the orchestrator when a worker begins execution.
        Increments active_parallel_tasks and pre-reserves estimated tokens.
        """
        self.update_budget(
            tokens_used=0,
            time_used=0.0,
            parallel_delta=+1,
        )
        self.trace_recorder.record(EventType.TASK_STARTED, {
            "task_id": task_id,
            "estimated_tokens": estimated_tokens,
            "active_parallel": self.budget.active_parallel_tasks,
            "remaining_slots": self.budget.available_parallel_slots(),
        })

    def record_task_end(
        self,
        task_id: str,
        actual_tokens: int = 0,
        actual_time_seconds: float = 0.0,
        tool_calls: int = 0,
        execution_outcome: str = "succeeded",
        stop_reason: str = "",
    ) -> Dict[str, Any]:
        """Record task completion: release parallel slot, update budget with real usage.

        Args:
            task_id: The completed task/node ID.
            actual_tokens: Real token consumption from the LLM provider.
            actual_time_seconds: Real wall-clock duration.
            tool_calls: Number of tool invocations made.
            execution_outcome: succeeded, failed, interrupted, or partial.
            stop_reason: Structured reason if not succeeded (e.g. BUDGET_LIMIT,
                         TIMEOUT, EXCEPTION, CANCELLED_BY_GOVERNOR).

        Returns:
            A budget snapshot dict for audit.
        """
        allowed_outcomes = {"succeeded", "failed", "interrupted", "partial"}
        if execution_outcome not in allowed_outcomes:
            raise ValueError(
                f"Unknown worker execution outcome: {execution_outcome}"
            )
        self.update_budget(
            tokens_used=actual_tokens,
            time_used=actual_time_seconds,
            parallel_delta=-1,
        )
        snapshot = {
            "task_id": task_id,
            "actual_tokens": actual_tokens,
            "actual_time_seconds": actual_time_seconds,
            "tool_calls": tool_calls,
            "execution_outcome": execution_outcome,
            "stop_reason": stop_reason,
            "total_tokens_used": self.budget.used_tokens,
            "total_time_used": self.budget.used_time_seconds,
            "remaining_tokens": self.budget.remaining_tokens(),
            "remaining_time": self.budget.remaining_time(),
            "active_parallel": self.budget.active_parallel_tasks,
            "remaining_slots": self.budget.available_parallel_slots(),
            "budget_remaining_ratio": (
                1.0
                if self.budget.max_tokens <= 0 else
                self.budget.remaining_tokens() / self.budget.max_tokens
            ),
        }
        # Alert when budget pressure is high
        if (
            self.budget.max_tokens > 0
            and self.budget.remaining_tokens() < self.budget.max_tokens * 0.1
        ):
            snapshot["alert"] = "TOKEN_BUDGET_CRITICAL"
            self.trace_recorder.record(EventType.BUDGET_WARNING, snapshot)
        self.trace_recorder.record(EventType.TASK_COMPLETED, snapshot)
        return snapshot

    def get_termination_reason(self) -> Dict[str, Any]:
        """Return a structured termination reason when execution must stop.

        Returns a dict with reason_code and details explaining why the
        Governor has decided to halt further task expansion.
        """
        if self.termination_requested:
            return {"reason_code": "EXPLICIT_TERMINATION", "details": "Termination explicitly requested"}
        if self.budget.max_tokens > 0 and self.budget.remaining_tokens() <= 0:
            return {
                "reason_code": "TOKEN_BUDGET_EXHAUSTED",
                "details": f"All {self.budget.max_tokens} tokens consumed",
                "used_tokens": self.budget.used_tokens,
                "max_tokens": self.budget.max_tokens,
            }
        if self.budget.max_time_seconds > 0 and self.budget.remaining_time() <= 0:
            return {
                "reason_code": "TIME_BUDGET_EXHAUSTED",
                "details": f"All {self.budget.max_time_seconds}s consumed",
                "used_time": self.budget.used_time_seconds,
                "max_time": self.budget.max_time_seconds,
            }
        if self.budget.available_parallel_slots() <= 0:
            return {
                "reason_code": "PARALLEL_SLOTS_EXHAUSTED",
                "details": f"All {self.budget.max_parallel_slots} parallel slots occupied",
                "active": self.budget.active_parallel_tasks,
                "max": self.budget.max_parallel_slots,
            }
        if self.total_tasks_created >= self.max_total_tasks:
            return {
                "reason_code": "MAX_TASKS_REACHED",
                "details": f"Total task limit {self.max_total_tasks} reached",
                "total_created": self.total_tasks_created,
            }
        active = self._count_active_tasks()
        if active == 0 and self.total_tasks_created > 0:
            return {
                "reason_code": "ALL_TASKS_TERMINAL",
                "details": "No active tasks remaining",
                "total_created": self.total_tasks_created,
                "total_admitted": self.total_tasks_admitted,
            }
        return {"reason_code": "NO_TERMINATION", "details": "Execution may continue"}

    def check_termination(self) -> bool:
        """Check if execution should terminate.

        Termination conditions:
        - All tasks completed/failed/cancelled
        - Budget exhausted
        - Termination explicitly requested
        - No progress for N iterations

        Returns:
            True if execution should terminate
        """
        reason = self.get_termination_reason()
        should_terminate = reason["reason_code"] != "NO_TERMINATION"
        if should_terminate:
            self.trace_recorder.record(EventType.TERMINATION_DECIDED, {
                "reason_code": reason["reason_code"],
                "details": reason["details"],
                "budget_snapshot": {
                    "tokens_remaining": self.budget.remaining_tokens(),
                    "time_remaining": self.budget.remaining_time(),
                    "parallel_slots_remaining": self.budget.available_parallel_slots(),
                    "active_parallel": self.budget.active_parallel_tasks,
                },
            })
        return should_terminate
    
    def request_termination(self, reason: str = "explicit_request"):
        """Request execution termination."""
        self.termination_requested = True
        self.trace_recorder.record(EventType.TERMINATION_REQUESTED, {"reason": reason})
    
    def get_admission_stats(self) -> Dict[str, Any]:
        """Get admission statistics."""
        return {
            "total_proposals": self.total_tasks_created,
            "admitted": self.total_tasks_admitted,
            "rejected": self.total_tasks_rejected,
            "admission_rate": (
                self.total_tasks_admitted / max(1, self.total_tasks_created)
            ),
            "budget": {
                "tokens_used": self.budget.used_tokens,
                "tokens_remaining": self.budget.remaining_tokens(),
                "time_used": self.budget.used_time_seconds,
                "time_remaining": self.budget.remaining_time(),
                "active_parallel": self.budget.active_parallel_tasks,
            },
        }
    
    def _count_active_tasks(self) -> int:
        """Count currently active (non-terminal) tasks."""
        terminal_statuses = {"completed", "failed", "cancelled", "superseded"}
        return sum(
            1 for node in self.task_graph.nodes.values()
            if node.status not in terminal_statuses
        )
    
    def _check_redundancy(self, proposal: TaskProposal) -> float:
        """Check redundancy with existing tasks.
        
        Simple heuristic: check goal similarity and capability overlap.
        Returns redundancy score in [0, 1].
        """
        if not self.task_graph.nodes:
            return 0.0
        
        # Simple keyword-based similarity
        proposal_keywords = set(proposal.goal.lower().split())
        
        max_similarity = 0.0
        for node in self.task_graph.nodes.values():
            if node.status in {"completed", "failed", "cancelled"}:
                continue
            
            node_keywords = set(node.goal.lower().split())
            if not node_keywords or not proposal_keywords:
                continue
            
            # Jaccard similarity
            intersection = proposal_keywords & node_keywords
            union = proposal_keywords | node_keywords
            similarity = len(intersection) / len(union) if union else 0.0
            
            max_similarity = max(max_similarity, similarity)
        
        return max_similarity
    
    def _infer_role_from_capabilities(self, capabilities: List[str]) -> str:
        """Infer worker_type from required capabilities using capability-based scoring.

        Capability matching is the PRIMARY decision path. Fixed role names are
        only compatibility adapters, not the default assignment. When no
        capability matches, returns 'governed_worker' and records a structured
        capability gap so the Governor can decide to expand the catalogue or
        replan — never silently falls back to a fixed role like 'researcher'.
        """
        if not capabilities:
            self.trace_recorder.record(EventType.CAPABILITY_GAP, {
                "requested_capabilities": capabilities,
                "gap_type": "EMPTY_CAPABILITIES",
                "action": "governor_must_replan_or_expand_catalogue",
            })
            return "governed_worker"

        scores = score_capability_categories(capabilities)

        # Find best category
        best_cat = max(scores, key=lambda k: scores[k])
        if scores[best_cat] == 0:
            # No capability matched — record structured gap, return generic type
            self.trace_recorder.record(EventType.CAPABILITY_GAP, {
                "requested_capabilities": capabilities,
                "unmatched_capabilities": list(capabilities),
                "gap_type": "NO_CATEGORY_MATCH",
                "action": "governor_must_expand_catalogue_or_replan",
            })
            return "governed_worker"

        return CATEGORY_WORKER_TYPES.get(best_cat, "governed_worker")
