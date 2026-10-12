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
from AstralNexus.core.governance_types import (
    EventType, AdmissionDecision, TaskGraphPatch, ContextGovernanceUnavailable,
    WorkerMatcherTransientError, WorkerMatcherProfileError, EVENT_SCHEMA_VERSION,
)

import json
import platform
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set
from AstralNexus.runtime.planning_projection import project_planning_graph
from AstralNexus.runtime.model_selector import DEFAULT_MODEL

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
from AstralNexus.core.output_paths import (
    PathIdentityError,
    canonical_relative,
    declared_output_claims,
    declared_output_entry,
    declared_output_paths,
    declared_relative,
    identity_key,
    join_identity,
    normalize_declared_output,
    normalize_worker_output_path,
    paths_overlap,
    same_or_under,
    workspace_identity,
)
from AstralNexus.core.task_declaration import (
    criterion_id,
    declared_criterion_ids,
    declared_criterion_texts,
    declared_requirement,
)
from AstralNexus.runtime.verification_identity import is_report_class_path
from AstralNexus.runtime.report_gap import (
    LOCAL_COMPLETION_RECORD_MARKERS,
    CompletionRecordVerdict,
    evaluate_completion_record,
    report_requirement_satisfiable_from_receipts,
)
from AstralNexus.runtime.acceptance_checks import SUMMARY_EVIDENCE_FORMS, EXECUTION_CRITERION_GUIDANCE, bind_criterion_paths
from AstralNexus.core.exceptions import InterruptionRequested



#: 框架自己补出的报告类记录的**文本**标记（用户 2026-10-06「报告不自证」）。
#: 唯一定义处是 ``runtime/report_gap.py``（与生成器同源），这里直接复用同一常量，
#: 不另立第二份标记表；标记来自 ``local_completion_document`` 的渲染（json 的
#: ``report_kind``／``generator``，markdown 的同名字段）。
_LOCAL_COMPLETION_RECORD_MARKERS = LOCAL_COMPLETION_RECORD_MARKERS


def _is_local_completion_record(path: Path, sample: str) -> bool:
    """Is this artifact the framework's own report-completion provenance record?

    认证只看**文本本身**（不新立判定点，也不放宽任何 ``AC-*`` 阈值）：优先用目录里
    已有的 8 000 字样本；样本为空时（文件名不带 ``evidence/report/result/test/...``
    等标记的条目不会被采样）读一次文件，上限 262 144 字节——框架自己写出的记录只
    引用收据，远小于该上限；超过上限的文件按"不是补全记录"处理，这一点在 P5 报告里
    作为诚实边界记明。
    """
    for marker in _LOCAL_COMPLETION_RECORD_MARKERS:
        if marker in sample:
            return True
    if sample:
        return False
    try:
        if path.stat().st_size > 262144:
            return False
        head = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(marker in head for marker in _LOCAL_COMPLETION_RECORD_MARKERS)


def _literal_relative(raw: Any) -> str:
    """The literal parsed spelling of ``raw`` — no namespace invention.

    Used when a declaration cannot be bound to one file: the string stays what
    the planner wrote, so the next check reports the truth instead of a guess.
    """
    try:
        return canonical_relative(str(raw or "").strip())
    except PathIdentityError:
        return str(raw or "").strip().replace("\\", "/")


def _forbidden_claim_is_asserted(content: str, claim: Any) -> bool:
    """Detect an asserted forbidden claim, not a quoted policy directive."""
    raw = str(claim).strip()
    prefixes = (
        "不得声称", "不得宣称", "禁止声称", "禁止宣称", "不要声称", "不要宣称",
        "must not claim", "do not claim", "don't claim", "must not assert",
    )
    marker = raw
    for prefix in prefixes:
        if marker.casefold().startswith(prefix.casefold()):
            marker = marker[len(prefix):].strip(" ：:，,。；;\"'“”‘’")
            break
    if not marker:
        return False

    folded_content = content.casefold()
    folded_marker = marker.casefold()
    search_from = 0
    while True:
        index = folded_content.find(folded_marker, search_from)
        if index < 0:
            return False
        preceding = folded_content[max(0, index - 48):index]
        # The contract language itself, or an explicit negation, is not proof
        # that the forbidden action happened.
        if not re.search(
            r"(?:不得|禁止|不要|不可|未|尚未|并未|没有|未曾|不曾|not|never|don't|do not)"
            r"(?:.{0,24})$",
            preceding,
            flags=re.IGNORECASE,
        ):
            return True
        search_from = index + max(1, len(folded_marker))




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


from AstralNexus.core.governor_budget import GlobalBudget
from AstralNexus.core.graph_admission import GraphAdmissionService


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
    contract_schema_version: int = field(default=2, kw_only=True)
    gate_policy_version: str = field(default="staged-contract-v1", kw_only=True)
    delivery_mode: str = "mixed"
    # The Governor decides whether the task is primarily an artifact
    # execution or a user-readable response.  This is intentionally not a
    # domain enum: a task may produce any artifact types selected at runtime.
    delivery_strategy: str = "model_decided"
    summary_required: bool = False
    execution_evidence_policy: str = "mixed"
    external_evidence_policy: str = "required_if_explicit"
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
            "contract_schema_version": self.contract_schema_version,
            "gate_policy_version": self.gate_policy_version,
            "delivery_mode": self.delivery_mode,
            "delivery_strategy": self.delivery_strategy,
            "summary_required": self.summary_required,
            "execution_evidence_policy": self.execution_evidence_policy,
            "external_evidence_policy": self.external_evidence_policy,
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
    from .patch_fields import text_items
    return text_items(value)
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
        self.ownership_transfers_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None
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
        self.total_tasks_merged = 0
        self.total_proposals_evaluated = 0
        self.admission_history: List[AdmissionDecision] = []
        
        # Global state
        self.goal_version = 1  # Incremented when goal changes
        self.termination_requested = False
        self.goal_spec: Optional[GoalSpec] = None
        self.delivery_contract: Dict[str, Any] = {}
        # F11 (live b31): frozen contract clauses that name a concrete file no
        # node owns.  Kept so the next planning round can see the gap instead of
        # re-discovering it after a repair round has already been spent.
        self.contract_clause_gaps: List[Dict[str, Any]] = []
        self.context_decisions: List[ContextGovernanceDecision] = []
        self.resource_ids_provider: Optional[Callable[[], Set[str]]] = None
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

    def _llm_worker_match(self, task: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from AstralNexus.core.governor_worker_matcher import WorkerMatcherService
        return WorkerMatcherService(client=self.llm_client, trace=self.trace_recorder,
                                    enabled=self.llm_matching_enabled, parser=_parse_governor_json).match(task, candidates)

    def admit_root_goal(
        self,
        *,
        node_id: str,
        goal: str,
        constraints: List[str],
        intent_node_id: str = "U-001",
    ) -> Dict[str, Any]:
        """Admit immutable user intent and its control-plane planner atomically."""
        return GraphAdmissionService().apply(self,
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
            ),
            trusted_control_nodes=(intent_node_id,),
        )

    def merge_rejected_task_graph_patch(
        self,
        rejected: TaskGraphPatch,
        repair: TaskGraphPatch,
    ) -> TaskGraphPatch:
        """Merge an LLM repair delta into an uncommitted rejected candidate.

        Replanning models may return either a full corrected candidate or only
        the nodes that fix the reported defect. Because the rejected candidate
        was never committed, treating a repair delta as a replacement silently
        drops valid delivery coverage. Node IDs are the stable merge key:
        repeated IDs replace prior definitions, new IDs append, and explicit
        candidate cancellations remove uncommitted nodes.
        """
        rejected_ids = {
            str(item.get("node_id") or item.get("task_id") or "").strip()
            for item in rejected.add_nodes
            if isinstance(item, dict)
            and str(item.get("node_id") or item.get("task_id") or "").strip()
        }
        repair_ids = {
            str(item.get("node_id") or item.get("task_id") or "").strip()
            for item in repair.add_nodes
            if isinstance(item, dict)
            and str(item.get("node_id") or item.get("task_id") or "").strip()
        }
        repair.candidate_mode = str(repair.candidate_mode).strip().lower()
        if repair.candidate_mode not in {"merge", "replace"}:
            raise ValueError(f"Invalid candidate_mode: {repair.candidate_mode}")
        # Node count cannot distinguish a large delta from a full replacement.
        # Omitted work survives unless replacement or cancellation is explicit.
        full_replacement = repair.candidate_mode == "replace"
        candidate_nodes: Dict[str, Dict[str, Any]] = {}
        anonymous_nodes: List[Dict[str, Any]] = []

        def absorb(nodes: Iterable[Dict[str, Any]]) -> None:
            for raw in nodes:
                if not isinstance(raw, dict):
                    continue
                node_id = str(
                    raw.get("node_id") or raw.get("task_id") or ""
                ).strip()
                if node_id:
                    candidate_nodes[node_id] = dict(raw)
                else:
                    anonymous_nodes.append(dict(raw))

        if not full_replacement:
            absorb(rejected.add_nodes)
        rejected_candidate_ids = rejected_ids
        cancelled_candidate_ids = {
            str(item).strip()
            for item in repair.cancel_nodes
            if str(item).strip() in rejected_candidate_ids
        }
        if full_replacement:
            cancelled_candidate_ids.update(rejected_ids - repair_ids)
        for node_id in cancelled_candidate_ids:
            candidate_nodes.pop(node_id, None)
        absorb(repair.add_nodes)

        committed_updates: List[Dict[str, Any]] = []
        for update in ([] if full_replacement else rejected.update_nodes) + repair.update_nodes:
            update_id = str(update.get("node_id") or update.get("task_id") or "").strip()
            if update_id not in candidate_nodes:
                committed_updates.append(dict(update))
                continue
            if "field_name" in update or "new_value" in update:
                raise ValueError("candidate updates must use direct fields, not field_name/new_value")
            candidate = candidate_nodes[update_id]
            merged_metadata = {**dict(candidate.get("metadata") or {}), **dict(update.get("metadata") or {})}
            candidate.update(update)
            if merged_metadata:
                candidate["metadata"] = merged_metadata

        def merge_edges(
            left: Iterable[Dict[str, Any]],
            right: Iterable[Dict[str, Any]],
        ) -> List[Dict[str, Any]]:
            merged: Dict[tuple[str, str, str], Dict[str, Any]] = {}
            for raw in [*left, *right]:
                if not isinstance(raw, dict):
                    continue
                key = (
                    str(raw.get("source") or ""),
                    str(raw.get("target") or ""),
                    str(raw.get("edge_type") or "depends_on"),
                )
                merged[key] = dict(raw)
            return [
                raw
                for key, raw in merged.items()
                if key[0] not in cancelled_candidate_ids
                and key[1] not in cancelled_candidate_ids
            ]

        reason_codes = list(dict.fromkeys([
            *(repair.reason_codes or ([] if full_replacement else rejected.reason_codes)),
            "REJECTED_PATCH_REPAIRED",
        ]))
        merged = TaskGraphPatch(
            add_nodes=[*candidate_nodes.values(), *anonymous_nodes],
            update_nodes=committed_updates,
            cancel_nodes=[
                item for item in [*rejected.cancel_nodes, *repair.cancel_nodes]
                if str(item).strip() not in rejected_candidate_ids
            ],
            add_edges=(
                list(repair.add_edges)
                if full_replacement
                else merge_edges(rejected.add_edges, repair.add_edges)
            ),
            remove_edges=merge_edges(
                [] if full_replacement else rejected.remove_edges,
                repair.remove_edges,
            ),
            reason_codes=reason_codes,
            reasoning=repair.reasoning or rejected.reasoning,
            background_candidates=(list(repair.background_candidates) if full_replacement else [*rejected.background_candidates, *repair.background_candidates]),
            candidate_mode=repair.candidate_mode,
        )
        self.trace_recorder.record(
            "governor.graph_patch.repair_merged",
            {
                "base_node_count": len(rejected.add_nodes),
                "repair_node_count": len(repair.add_nodes),
                "merged_node_count": len(merged.add_nodes),
                "merge_mode": "full_replacement" if full_replacement else "delta",
                "replaced_node_ids": sorted(
                    rejected_candidate_ids
                    & {
                        str(item.get("node_id") or item.get("task_id") or "").strip()
                        for item in repair.add_nodes
                        if isinstance(item, dict)
                    }
                ),
                "cancelled_candidate_ids": sorted(cancelled_candidate_ids),
            },
        )
        return merged

    # F11 (live b31): the frozen delivery contract demanded "python app.py"
    # while no node owned that file and no acceptance criterion anchored it, so
    # the graph satisfied the clause by spending an entire repair round on an
    # empty shim (R-701, task.workspace_delta 02:07:20Z).  A FROZEN CONTRACT
    # clause that names a concrete file is a delivery obligation: some node must
    # own it, or the clause is an interface constraint (port / endpoint / field /
    # enum / selector) and must be stated as one.  The suffix classes below are
    # lexical (never a fixed file name), so the check stays domain-neutral.
    #
    # b33 S4: this extractor is the HINT reader.  It answers "which files does
    # this prose mention" for the frozen contract; it is no longer a promotion
    # path — an ordinary document (prose mentions, same-suffix names, the
    # md/rst/txt this run wrote) never becomes a hard acceptance obligation
    # through it.  Obligations come from a structured declaration (F16) or from
    # an admitted node's owned_paths.
    CONCRETE_FILE_SUFFIXES = (
        "py", "pyi", "js", "mjs", "cjs", "jsx", "ts", "tsx", "sql", "db",
        "sqlite", "sqlite3", "md", "rst", "json", "jsonl", "yaml", "yml",
        "toml", "ini", "cfg", "conf", "csv", "tsv", "txt", "log", "sh",
        "bat", "ps1", "psm1", "html", "htm", "css", "xml", "lock",
    )
    # A bare file name (no directory part) counts as a clause path as well: the
    # live clause named the entry file with no directory part, which is exactly
    # the demand that stayed unowned.  Interface clauses (port / endpoint /
    # field / enum / selector) carry no file-suffixed token at all, so they are
    # never reported.
    #
    # b33 S4: these tokens are HINTS, not obligations.  A token becomes a delivery
    # obligation only when a structured declaration names it (F16) or an admitted
    # node's owned_paths covers it.  The suffix whitelist above keeps PROSE from
    # inventing files; it no longer promotes a document this run wrote into a hard
    # acceptance obligation (F11b deleted), which is why the WRITTEN_DOC_*
    # constants and their reader are gone.
    CLAUSE_GAP_LIMIT = 24

    @classmethod
    def _concrete_file_tokens(cls, text: str) -> List[str]:
        """Concrete file paths named by prose.

        Mirrors the token shape of the criterion-binding gate, but a bare file
        name is a token here as well: the live clause named "app.py" with no
        directory part, which is exactly the demand that stayed unowned.  No
        concrete file name is ever hardcoded — only suffix classes are.
        """
        tokens: List[str] = []
        for raw in re.findall(r"[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,10}", str(text or "")):
            token = raw.replace("\\", "/").strip().strip("`'\"()[]{}<>,;:")
            if not token or token.startswith("http") or "/" == token:
                continue
            token = re.sub(r"/{2,}", "/", token)
            if token.startswith("/"):
                # An absolute machine path (a file server path, a doubled
                # separator from an escaped artifact) is not run-relative, so no
                # owned_paths could ever cover it: reporting it as an obligation
                # would only add noise.  Bare names and relative paths stay.
                continue
            suffix = token.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
            if suffix not in cls.CONCRETE_FILE_SUFFIXES:
                continue
            if token not in tokens:
                tokens.append(token)
        return tokens

    @staticmethod
    def _relocate_declared_path(token: str, declared: Sequence[str]) -> str:
        """The declared path a cited spelling names, by unique suffix match only.

        Fix S1: relocation happens inside the DECLARED path set (every node's
        ``owned_paths``) through the shared identity parser -- never a filesystem
        search for a same-named file.  A spelling that matches nothing keeps its
        literal form, and an ambiguous one (two declared files share the suffix)
        keeps it too, so no file is ever guessed.
        """
        try:
            return workspace_identity("", token, declared=list(declared)).rel
        except PathIdentityError:
            return _literal_relative(token)

    @staticmethod
    def _clause_path_covered(token: str, owned_paths: List[str]) -> bool:
        """Whether one path identity is inside some owned_paths.

        Fix S1: coverage is decided by the shared path identity only.  The
        basename fallback that used to sit here ("same file name under a
        different root") equated two different files, so a clause could be
        reported as covered by a node that owns a same-named file somewhere
        else -- and, in the live B+ run, the obligation noise it produced was
        reported 63 times over 14 paths.
        """
        return any(same_or_under(path, token) for path in owned_paths)

    @staticmethod
    def _suggest_clause_owner(token: str, owners: List[tuple]) -> str:
        """Best ownership candidate for a clause path.

        Longest shared path prefix first; when a bare file name shares no prefix
        with anything (the live case: the clause named the entry file, the graph
        owned only files inside the app directory), fall back to a node that
        already owns a file of the same suffix class.  The suggestion is
        advisory — the planner still decides.
        """
        target = str(token).replace("\\", "/").strip("/").lower()
        best_id = ""
        best_score = 0
        same_suffix_id = ""
        for node_id, owned_paths in owners:
            for path in owned_paths:
                candidate = str(path).replace("\\", "/").strip("/").lower()
                score = 0
                for left, right in zip(target, candidate):
                    if left != right:
                        break
                    score += 1
                if score > best_score:
                    best_id, best_score = str(node_id), score
                if not same_suffix_id and candidate.rsplit(".", 1)[-1] == target.rsplit(".", 1)[-1]:
                    same_suffix_id = str(node_id)
        if best_score > 0:
            return best_id
        return same_suffix_id

    def _clause_path_exists_on_disk(self, token: str) -> bool:
        """Whether the run directory already holds the file a clause names.

        Mirrors the delivery-root / staging lookup of the evidence coverage
        gate: a clause whose file already exists is anchored by reality and must
        not be reported as an unowned obligation.
        """
        run_dir = getattr(self.trace_recorder, "run_dir", None)
        if run_dir is None or not str(token or "").strip():
            return False
        # One candidate plane, from the shared resolver: the run root and the
        # publication staging.  (This used to be a third hand-written probe.)
        root = Path(run_dir).resolve()
        try:
            if not Path(token).is_absolute():
                canonical_relative(token, root=root)
            return any(path.resolve().is_relative_to(root) and path.exists()
                       for path in declared_output_paths(run_dir, token))
        except (PathIdentityError, OSError, ValueError):
            return False

    def _frozen_contract_clauses(self) -> List[Dict[str, str]]:
        """The frozen prose that binds this delivery, clause by clause.

        The task goal plus the contract string fields, split per line so a single
        unowned obligation can be reported without quoting the whole task book.
        """
        texts: List[tuple] = []
        spec = self.goal_spec
        if spec is not None:
            for key in ("task_goal", "normalized_goal"):
                value = str(getattr(spec, key, "") or "").strip()
                if value:
                    texts.append(("goal_spec." + key, value))
            for key in ("hard_constraints", "success_criteria", "forbidden_claims"):
                for item in getattr(spec, key, []) or []:
                    if str(item).strip():
                        texts.append(("goal_spec." + key, str(item)))
        for key in (
            "global_acceptance_criteria",
            "completion_evidence",
            "required_sections",
            "hard_constraints",
        ):
            contract_value = (self.delivery_contract or {}).get(key)
            if isinstance(contract_value, (list, tuple)):
                for item in contract_value:
                    if isinstance(item, str) and item.strip():
                        texts.append(("contract." + key, item))
            elif isinstance(contract_value, str) and contract_value.strip():
                texts.append(("contract." + key, contract_value))
        for item in (self.delivery_contract or {}).get("required_outputs", []) or []:
            if not isinstance(item, dict):
                continue
            output_id = str(item.get("output_id") or "").strip()
            for text in item.get("acceptance_criteria", []) or []:
                if str(text).strip():
                    texts.append((f"required_outputs[{output_id}].acceptance_criteria", str(text)))
        clauses: List[Dict[str, str]] = []
        for source, text in texts:
            for line in re.split(r"[\r\n]+", str(text)):
                clause = line.strip().strip("|").strip()
                if clause:
                    clauses.append({"source": source, "clause": clause[:400]})
        return clauses

    def _unanchored_contract_clauses(
        self,
        current: Dict[str, Dict[str, Any]],
        additions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """F11: clauses that name a concrete file nobody owns.

        The clause source is the frozen contract prose.  A clause like that
        cannot be satisfied by any worker's owned_paths, so it is either given an
        owner or downgraded to an interface constraint.  It is recorded here and
        fed to the next planning round; it is never dropped silently, and the
        total list is capped so clause noise cannot flood the planning prompt.

        b33 S4: documents a node of this run wrote are no longer a clause source,
        and neither is any other ordinary document.  Prose mentions, same-suffix
        names and the md/rst/txt a run writes itself are hints, not delivery
        obligations: a file becomes an obligation only when a structured
        declaration names it (F16) or an admitted node's owned_paths covers
        it — and the ownership gate already enforces the second one.
        """
        owners: List[tuple] = []
        for node_id, item in list(current.items()):
            if item.get("status") in {"cancelled", "superseded"}:
                continue
            if str((item.get("metadata") or {}).get("plane") or "execution") == "control":
                continue
            owned = [
                str(path).replace("\\", "/").strip().strip("/")
                for path in (item.get("owned_paths") or [])
                if str(path).strip()
            ]
            if owned:
                owners.append((str(node_id), owned))
        for item in additions:
            owned = [
                str(path).replace("\\", "/").strip().strip("/")
                for path in (item.get("owned_paths") or [])
                if str(path).strip()
            ]
            if owned:
                owners.append((str(item.get("node_id") or ""), owned))
        gaps: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        # The declared set every cited spelling may be relocated into: every
        # node's owned_paths, planned and existing alike.
        declared_paths = [path for _node_id, owned in owners for path in owned]
        for entry in self._frozen_contract_clauses():
            clause = entry["clause"]
            for token in self._concrete_file_tokens(clause):
                if len(gaps) >= self.CLAUSE_GAP_LIMIT:
                    break
                key = token.lower()
                if key in seen:
                    continue
                target = self._relocate_declared_path(token, declared_paths)
                if any(self._clause_path_covered(target, owned) for _, owned in owners):
                    continue
                if self._clause_path_exists_on_disk(target):
                    continue
                seen.add(key)
                gaps.append(
                    {
                        "clause": clause,
                        "source": entry["source"],
                        "path": token,
                        "suggested_owner": self._suggest_clause_owner(token, owners),
                        "reason": (
                            "the frozen contract"
                            + " names this concrete file but no node owned_paths covers it and "
                            "no criterion anchors it; assign an owner or state the clause as an "
                            "interface constraint (port / endpoint / field / enum / selector)"
                        ),
                    }
                )
            if len(gaps) >= self.CLAUSE_GAP_LIMIT:
                break
        return gaps

    def _recent_path_evidence(self, paths: List[str]) -> Dict[str, Dict[str, Any]]:
        """F14: the most recent recorded evidence for each cited path, if any.

        Reads the recorder's in-memory event window backwards (bounded) so a
        replan can see that a file was already written and verified without
        re-reading the whole trace file.
        """
        wanted: Dict[str, str] = {}
        for token in paths:
            normalized = identity_key(token)
            if not normalized:
                continue
            wanted.setdefault(normalized, normalized)
            wanted.setdefault(normalized.rsplit("/", 1)[-1], normalized)
        found: Dict[str, Dict[str, Any]] = {}
        if not wanted:
            return found
        events = list(getattr(self.trace_recorder, "events", []) or [])[-4000:]
        for event in reversed(events):
            if len(found) >= len(wanted):
                break
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            recorded_at = str(event.get("recorded_at") or "")
            if event_type == "task.workspace_delta":
                for entry in payload.get("entries", []) or []:
                    if not isinstance(entry, dict):
                        continue
                    path = identity_key(entry.get("path"))
                    if path and path in wanted and path not in found:
                        found[path] = {
                            "source": event_type,
                            "node_id": str(payload.get("node_id") or ""),
                            "delta_kind": str(entry.get("kind") or ""),
                            "recorded_at": recorded_at,
                        }
            elif "verifier" in event_type:
                observed_paths: Set[str] = set()
                def collect(value: Any) -> None:
                    if isinstance(value, dict):
                        for field, item in value.items():
                            if field in {"path", "file", "artifact_refs", "evidence_refs", "source_artifacts"}:
                                for reference in item if isinstance(item, list) else [item]:
                                    if isinstance(reference, str):
                                        observed_paths.add(identity_key(reference))
                            elif isinstance(item, (dict, list)):
                                collect(item)
                    elif isinstance(value, list):
                        for item in value:
                            collect(item)
                collect(payload)
                for key in set(wanted) & observed_paths:
                    if key not in found:
                        found[key] = {"source": event_type, "recorded_at": recorded_at}
        return found

    def _refresh_path_premises(self, paths: Iterable[str]) -> List[Dict[str, Any]]:
        """F14: re-read the path facts a plan relies on.

        The final b31 replan asserted that the delivered entry file "does not
        exist on disk" while that file had been on disk and already verified for
        seventeen minutes, and the shim-only patch that followed consumed the
        last repair-budget event.  Every cited path is therefore refreshed here:
        disk first, then the most recent workspace-delta / verifier evidence for
        that path.  A path that cannot be refreshed is reported as unverified,
        never as absent.
        """
        candidates: List[str] = []
        for value in paths or []:
            token = str(value or "").strip()
            if token and token not in candidates:
                candidates.append(token)
        if not candidates:
            return []
        evidence = self._recent_path_evidence(candidates)
        has_run_dir = getattr(self.trace_recorder, "run_dir", None) is not None
        premises: List[Dict[str, Any]] = []
        for token in candidates[:24]:
            key = identity_key(token)
            observed = evidence.get(key) or evidence.get(key.rsplit("/", 1)[-1])
            if has_run_dir:
                premise: Dict[str, Any] = {
                    "path": token,
                    "exists": self._clause_path_exists_on_disk(token),
                    "source": "disk",
                }
                if observed and not premise["exists"]:
                    premise["last_evidence"] = observed
                premises.append(premise)
                continue
            if observed:
                premises.append(
                    {"path": token, "exists": True, "source": observed.get("source", "evidence"),
                     "last_evidence": observed}
                )
            else:
                premises.append({"path": token, "exists": None, "source": "unverified"})
        return premises

    def current_write_holder(self, path: str) -> str:
        """Read committed graph authority; legacy transfers remain readable."""
        from AstralNexus.core.write_authority import committed_transfers, INACTIVE
        grants = committed_transfers(self.task_graph.nodes)
        if self.ownership_transfers_provider is not None:
            grants = list(self.ownership_transfers_provider()) + grants
        for transfer in reversed(grants):
            node = self.task_graph.get_node(str(transfer.get("to_node") or ""))
            if node is not None and node.status not in INACTIVE and paths_overlap(path, transfer.get("path", "")):
                return node.node_id
        return ""

    def apply_task_graph_patch(self, patch: TaskGraphPatch | Dict[str, Any], *, parent_task_id: Optional[str] = None, _record_stats: bool = True) -> Dict[str, Any]:
        return GraphAdmissionService().apply(self, patch, parent_task_id=parent_task_id, record_stats=_record_stats)

    def _apply_task_graph_patch(
        self,
        patch: TaskGraphPatch | Dict[str, Any],
        *,
        parent_task_id: Optional[str] = None,
        _trusted_control_nodes: tuple[str, ...] = (),
        _validate_only: bool = False,
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

        from AstralNexus.runtime.development_playbook import validate_replacement_rechecks
        validate_replacement_rechecks(self.task_graph, patch)
        # b33 case1 fix10 第 4 步：提交前对**整张投影图**检查生产者 / 版本要求 /
        # 环路。失败即抛，图一字不改（本函数是唯一的增量规划变更边界）。
        from AstralNexus.core.dependency_projection import projected_issues
        projection_issues = [
            item for item in projected_issues(
                nodes=self.task_graph.nodes,
                patch_nodes=list(patch.add_nodes or []),
                patch_updates=list(patch.update_nodes or []),
                cancelled=list(patch.cancel_nodes or []),
                artifacts=self.task_graph.artifacts,
            )
        ]
        if projection_issues:
            detail = "; ".join(
                "%s:%s" % (
                    item.get("issue"),
                    item.get("producer") or item.get("consumer") or "",
                )
                for item in projection_issues[:3]
            )
            raise ValueError(
                "graph patch rejected by the projected-graph check: " + detail
            )
        node_count_before = len(self.task_graph.nodes)
        edge_count_before = len(self.task_graph.edges)
        current: dict[str, dict[str, Any]] = {
            node_id: {
                "status": node.status,
                "goal": node.goal,
                "role": node.role,
                "metadata": dict(node.metadata),
                "dependencies": list(node.dependencies),
                "owned_paths": list(node.owned_paths or node.metadata.get("owned_paths", [])),
                "input_artifacts": list(node.input_artifacts or node.metadata.get("input_artifacts", [])),
                "output_artifacts": list(node.output_artifacts or node.metadata.get("output_artifacts", [])),
            }
            for node_id, node in self.task_graph.nodes.items()
        }
        preexisting_node_ids = set(current)
        # doc 18 修复H：所有权转移剥离预合并——带 metadata.ownership_
        # transfer_to 标记的 update_nodes 是编排器接管规范化
        # （_normalize_recovery_ownership_transfer 转移 pass）对活跃重复
        # 所有者生成的剥离，其存在意义就是让本补丁的接管通过 add 侧
        # 所有权验证；但 update 处理在 add 验证之后——不预合并则剥离
        # 对验证不可见，接管必死于 "overlapping output ownership"
        # （live attempt6 实证：failed+ready 双所有权种子，Fix 23a 的
        # 文档意图只对非活跃重复所有者成立）。只合并 owned_paths/
        # output_artifacts 两字段、只认 transfer 标记；预合并仅作用于
        # 内存验证态，完整 update 验证仍在后续执行，任何失败整个补丁
        # 不落图。
        for pre_raw in patch.update_nodes:
            if not isinstance(pre_raw, dict):
                continue
            pre_meta = pre_raw.get("metadata")
            if not isinstance(pre_meta, dict) or not str(
                pre_meta.get("ownership_transfer_to") or ""
            ).strip():
                continue
            pre_id = str(pre_raw.get("node_id") or pre_raw.get("task_id") or "").strip()
            if pre_id not in current:
                continue
            if isinstance(pre_raw.get("owned_paths"), list):
                current[pre_id]["owned_paths"] = [
                    str(item).strip().replace("\\", "/")
                    for item in pre_raw["owned_paths"]
                    if str(item).strip()
                ]
            if isinstance(pre_raw.get("output_artifacts"), list):
                current[pre_id]["output_artifacts"] = [
                    str(item).strip()
                    for item in pre_raw["output_artifacts"]
                    if str(item).strip()
                ]
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

        def contract_list(raw: Any, metadata: Dict[str, Any], key: str) -> List[str]:
            value = raw if raw is not None else metadata.get(key, [])
            if key in {"input_artifacts", "output_artifacts"}:
                from .patch_fields import artifact_references
                return artifact_references(value)
            if isinstance(value, str):
                value = [value]
            return [str(item).strip().replace("\\", "/") for item in (value or []) if str(item).strip()]

        def declared_output_items(raw: Any, metadata: Dict[str, Any], key: str) -> List[Any]:
            """Declared outputs with their structured fields PRESERVED (起 A).

            ``contract_list`` flattens every entry through ``str()``, which
            erased ``required`` / ``source`` / ``condition`` and left the
            Verifier (core/verifier.py) and the Orchestrator postcondition check
            (core/orchestrator.py) unable to tell a conditional artifact from a
            hard one.  Live r30 R-107 was failed and rerun for a conditional
            ``defects.json`` that was correctly absent.  Path-like keys keep
            using ``contract_list``; only declared outputs come through here.
            """
            value = raw if raw is not None else metadata.get(key, [])
            if isinstance(value, str):
                value = [value]
            items: List[Any] = []
            for item in (value or []):
                if isinstance(item, dict):
                    entry = {str(key_name): value_ for key_name, value_ in item.items()}
                    path = entry.get("path") or entry.get("file") or entry.get("location")
                    if path is not None and str(path).strip():
                        entry["path"] = normalize_worker_output_path(str(path).strip())
                    items.append(entry)
                elif str(item).strip():
                    # 起 A: a PROSE declaration whose conditional clause is
                    # locatable and carries exactly one file claim becomes a
                    # structured entry here (``required: False`` +
                    # ``source: "prose_conditional"``), so the downstream gates
                    # read a field instead of a sentence.  Every other string —
                    # no clause, several claims, a directory-only token —
                    # normalizes to ``None`` and is kept verbatim, which means
                    # REQUIRED (无法判定即必需).
                    normalized_output = normalize_declared_output(item)
                    if isinstance(normalized_output, dict):
                        items.append(normalized_output)
                    else:
                        items.append(str(item).strip().replace("\\", "/"))
            return items

        def dedupe_declared_outputs(items: List[Any]) -> List[Any]:
            """Order-preserving dedupe that tolerates structured declarations.

            ``list(dict.fromkeys(...))`` raises ``TypeError: unhashable type:
            'dict'`` as soon as declarations keep their structure (起 A).
            """
            seen: Set[str] = set()
            result: List[Any] = []
            for item in items:
                identity = (
                    json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if isinstance(item, dict) else str(item)
                )
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(item)
            return result

        def output_contract_conflicts(
            left_paths: List[str], left_artifacts: List[str],
            right_paths: List[str], right_artifacts: List[str],
        ) -> List[str]:
            # Owned filesystem namespaces conflict on exact or parent/child overlap
            # (``paths_overlap``).  Artifact IDs are checked separately by exact
            # identity; treating an ID such as ``e2e_report.json`` as a child path
            # of ``deliverables/runs`` creates false graph rejections.
            conflicts = [
                f"owned_path:{left} <> {right}"
                for left in left_paths
                for right in right_paths
                if paths_overlap(left, right)
            ]
            conflicts.extend(
                f"artifact_id:{left} <> {right}"
                for left in left_artifacts
                for right in right_artifacts
                if left.rstrip("/") == right.rstrip("/")
            )
            return conflicts

        from AstralNexus.core.lifecycle_policy import WRITE_ACTIVE_STATUSES, RECOVERY_TARGET_STATUSES
        active_owner_statuses = WRITE_ACTIVE_STATUSES

        proposed_edges: Dict[tuple[str, str, str], Any] = {
            (edge.source, edge.target, edge.edge_type): edge
            for edge in self.task_graph.edges
        }
        # Dependencies on nodes are also persisted by the graph API. A patch
        # must not erase them merely because a legacy/manual checkpoint lacks
        # the corresponding edge records.
        for existing_node in self.task_graph.nodes.values():
            for dependency in existing_node.dependencies:
                proposed_edges.setdefault((dependency, existing_node.node_id, "depends_on"), {
                    "source": dependency, "target": existing_node.node_id,
                    "edge_type": "depends_on", "metadata": {},
                })
        proposed_communication_links: List[Dict[str, str]] = []
        for raw in patch.remove_edges:
            if isinstance(raw, dict):
                proposed_edges.pop(edge_key(raw), None)

        id_map: Dict[str, str] = {}
        additions: List[Dict[str, Any]] = []
        # F-A (P4 Round-1): one run root for every identity computation in
        # this patch — path normalization in the add loop below AND the
        # artifact alias key near input resolution both read this binding,
        # so it must be assigned before the add_nodes loop runs.
        identity_root = getattr(self.trace_recorder, "run_dir", None)
        # F-A (P4 Round-1): raw declared output plane of this patch.  Input
        # resolution may see artifacts that do not exist yet (a replan patch
        # consuming a file a sibling NEW node will produce).  Those inputs are
        # legal planned dependencies when the patch itself declares the
        # producer; the raw (pre-normalization) spellings are collected here
        # because namespace normalization rewrites run paths before the
        # consumer's raw input is ever compared against them.
        patch_projection_paths: Dict[str, List[str]] = {}
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
            # Similar goals do not imply interchangeable delivery contracts.
            # Keep explicit workers for ownership and coverage validation.
            has_explicit_contract = any(
                contract_list(raw.get(key), dict(raw.get("metadata") or {}), key)
                for key in (
                    "owned_paths", "expected_outputs", "output_artifacts",
                    "input_artifacts", "delivery_output_ids", "delivery_criterion_ids",
                )
            )
            duplicate = None if is_control_node or has_explicit_contract else GraphPatchValidator.find_duplicate(
                str(raw["goal"]),
                caps,
                current,
                similarity_threshold=self.policy.duplicate_similarity_threshold,
            )
            if duplicate:
                previous = self.task_graph.get_node(duplicate)
                if previous is not None and (
                    not set(strings_from(raw.get("dependencies", []))).issubset(previous.dependencies)
                    or not set(strings_from(raw.get("acceptance_criteria", []))).issubset(previous.acceptance_criteria)
                ):
                    duplicate = None
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
            metadata = dict(raw.get("metadata") or {})
            # identity_root is hoisted to function scope by F-A (P4 Round-1)
            # so the alias key below and path normalization share one root.
            # F-A: remember this node's RAW declared outputs on the projection
            # plane (pre-normalization spellings, exactly what the planner
            # wrote) for the planned-dependency admission in input resolution.
            patch_projection_paths[node_id] = sorted(
                {
                    str(item).strip().replace("\\", "/")
                    for key in ("owned_paths", "expected_outputs", "output_artifacts")
                    for item in contract_list(raw.get(key), metadata, key)
                    if str(item).strip()
                }
            )
            ownership_normalizations: List[Dict[str, str]] = []
            # Fix S1 (b32_path_identity): one root, one identity for every path
            # this node declares.  owned_paths used to be namespaced only when
            # expected_outputs happened to be non-empty, so one node held two
            # identities for one file -- live B+ run task-1791256754557 had C-01
            # owning deliverables/docs/contract.md while its criterion leaf said
            # docs/contract.md; the leaf was then resolved literally, the 40 006 B
            # deliverable was reported FILE_NOT_FOUND, and the node was failed
            # and re-planned (recovery_churn 14, 0 delivered nodes).
            owned_paths = [
                path if path.startswith("artifact:")
                else declared_relative(path, root=identity_root)
                + ("/" if path.endswith("/") else "")
                for path in contract_list(raw.get("owned_paths"), metadata, "owned_paths")
            ]
            declared_outputs = declared_output_items(raw.get("expected_outputs"), metadata, "expected_outputs")

            # Diagnosis of a declared output this node's own declaration set
            # cannot bind to ONE file (an ambiguous suffix match).  The patch is
            # rejected either way; keeping the reason here means the rejection
            # names the raw path, the candidates and the root.
            identity_notes: List[str] = []

            def resolve_declared_output_path(declared_output: str) -> str:
                """Bind one declared output string to its declared path."""
                if declared_output.startswith("artifact:"):
                    return declared_output
                try:
                    identity = workspace_identity(
                        identity_root, declared_output, declared=owned_paths, namespace=True,
                    )
                except PathIdentityError as exc:
                    # Ambiguous inside this node's own declaration set: never pick
                    # one, never search the filesystem.  The literal spelling
                    # stays, so the ownership check below tells the truth.
                    identity_notes.append(str(exc))
                    return _literal_relative(declared_output)
                relative = identity.rel
                if identity.status == "unbound" and "/" not in declared_output:
                    # A bare file name inside this node's single owned directory
                    # is that directory's child, not a new namespace.
                    directories = [
                        path for path in owned_paths
                        if path.endswith("/") or not Path(path).suffix
                    ]
                    if len(directories) == 1:
                        relative = join_identity(directories[0], declared_output)
                if declared_output.endswith("/") and not relative.endswith("/"):
                    relative += "/"
                return relative

            expected_outputs: List[Any] = []
            for declared_output in declared_outputs:
                if isinstance(declared_output, dict):
                    # 起 A: keep the structured contract verbatim (required /
                    # source / condition survive) and only resolve its path.
                    entry = dict(declared_output)
                    declared_path = str(entry.get("path") or "").strip()
                    if declared_path:
                        entry["path"] = resolve_declared_output_path(declared_path)
                    expected_outputs.append(entry)
                    continue
                expected_outputs.append(resolve_declared_output_path(declared_output))
            # The declaration set a criterion leaf may be relocated into: this
            # node's own owned_paths plus the paths its own outputs claim.  Never
            # a filesystem search -- a same-named file elsewhere in the workspace
            # belongs to another node and can neither satisfy nor steal a leaf.
            declared_scope = list(owned_paths)
            for output in expected_outputs:
                declared_scope.extend(
                    declared_output_claims(output)
                    if isinstance(output, dict) else [str(output)]
                )

            def bind_criteria(values: Any) -> List[str]:
                texts = [
                    str(item).strip() for item in (values or []) if str(item).strip()
                ]
                if not texts:
                    return []
                bound, relocations = bind_criterion_paths(
                    texts, declared=declared_scope, root=identity_root,
                )
                ownership_normalizations.extend({
                    "from": item["raw"], "to": item["identity"],
                    "reason": "criterion_path_relocated",
                } for item in relocations)
                return bound

            if raw.get("acceptance_criteria") is not None:
                normalized["acceptance_criteria"] = bind_criteria(raw.get("acceptance_criteria"))
            if metadata.get("acceptance_criteria"):
                metadata["acceptance_criteria"] = bind_criteria(metadata.get("acceptance_criteria"))
            # expected_outputs are minimum postconditions, not a complete
            # list of files implementation workers may need to create.
            # Preserve their explicitly granted directory ownership. Real
            # sibling overlaps are rejected below and repaired by Governor.
            input_artifacts = contract_list(raw.get("input_artifacts"), metadata, "input_artifacts")
            # 起 A：expected_outputs 的 artifact fallback 只取非结构化条目——
            # 结构化声明是契约字段（path/required/source），不是 artifact id；
            # str(dict) 会把整条 repr 变成伪造的 artifact 名，污染
            # artifact_producers 与交付契约（报告补全节点正是这种形状）。
            fallback_outputs = raw.get("expected_outputs")
            if fallback_outputs is not None:
                fallback_outputs = [
                    item for item in fallback_outputs
                    if not isinstance(item, dict)
                ] or None
            output_artifacts = contract_list(
                raw.get("output_artifacts") or fallback_outputs,
                metadata,
                "output_artifacts",
            )
            handoff_contract = dict(raw.get("handoff_contract") or metadata.get("handoff_contract") or {})
            recovery_of = str(
                raw.get("recovery_of")
                or raw.get("supersedes")
                or metadata.get("recovery_of")
                or metadata.get("supersedes_node_id")
                or ""
            ).strip()
            task_type = str(raw.get("task_type") or "other").strip().lower()
            declared_task_type = task_type
            documentation_capability_markers = (
                "documentation", "document_authoring", "instruction_authoring",
                "schema_documentation", "interface_documentation", "user_guidance",
            )
            writes_only_document_tree = bool(owned_paths) and all(
                "/docs" in f"/{path.lower().strip('/')}"
                for path in owned_paths
            )
            has_document_contract = any(
                marker in capability.lower()
                for capability in caps
                for marker in documentation_capability_markers
            ) or (
                bool(output_artifacts)
                and all(
                    any(
                        suffix in artifact.lower()
                        for suffix in ("_md", ".md", "document", "guide")
                    )
                    for artifact in output_artifacts
                )
            )
            if (
                task_type == "implementation"
                and writes_only_document_tree
                and has_document_contract
            ):
                task_type = "documentation"
            evidence_task_types = {
                "testing", "test", "verification", "validation", "tool",
                "evidence", "documentation", "reporting",
            }
            # 起 A / doc 51 边界①：结构化 expected_outputs 用字段携带契约
            # （Verifier 经 core/output_paths 按字段读取）。命名空间规整只关心
            # PATH，先把每条声明投影成它的文件声明路径；此处曾对 dict 条目直接
            # 调用 startswith / Path(...).suffix —— 报告补全节点正是这种形状，
            # 会以 AttributeError/TypeError 崩掉整份补丁。非结构化条目逐字保留
            # 原字符串（含尾随 "/" 与 artifact: 形式），否则聚合目录所有者会被
            # 误判为"已收窄"，进而放过两个节点对同一目录的重复拥有。
            declared_output_paths: List[str] = [
                claim
                for output in expected_outputs
                for claim in (
                    declared_output_claims(output)
                    if isinstance(output, dict)
                    else [str(output)]
                )
            ]
            if owned_paths:
                node_namespace = re.sub(
                    r"[^0-9a-zA-Z_-]+", "-", node_id.lower()
                ).strip("-") or "worker"
                normalized_paths: List[str] = []
                for candidate_path in owned_paths:
                    base_path = candidate_path.rstrip("/")
                    already_namespaced = (
                        base_path.lower().rsplit("/", 1)[-1] == node_namespace
                    )
                    is_directory_namespace = bool(base_path) and not Path(
                        base_path
                    ).suffix
                    # Keep contractual filenames stable. Narrow an aggregate
                    # owner to its declared files instead of moving only the
                    # owner and making its own outputs appear out of bounds.
                    declared_files = [
                        output for output in declared_output_paths
                        if not output.startswith("artifact:")
                        and Path(output).suffix
                        and output.startswith(base_path + "/")
                    ]
                    declared_directories = [
                        output for output in declared_output_paths
                        if not output.startswith("artifact:") and not Path(output).suffix
                        and (output.rstrip("/") == base_path or output.startswith(base_path + "/"))
                    ]
                    if (task_type != "implementation" and is_directory_namespace
                            and declared_files and not declared_directories):
                        normalized_paths.extend(declared_files)
                        ownership_normalizations.extend({
                            "from": candidate_path, "to": output,
                            "reason": "declared_output_ownership",
                        } for output in declared_files)
                        continue
                    if (task_type in evidence_task_types and is_directory_namespace
                            and not already_namespaced and not declared_directories):
                        replacement = f"{base_path}/{node_namespace}/"
                        ownership_normalizations.append({
                            "from": candidate_path,
                            "to": replacement,
                            "reason": "evidence_worker_namespace",
                        })
                        normalized_paths.append(replacement)
                    else:
                        normalized_paths.append(candidate_path)
                owned_paths = normalized_paths
            predecessor_id = recovery_of
            inherited_from: set[str] = set()
            inherited_output_ids: List[str] = []
            inherited_criterion_ids: List[str] = []
            while predecessor_id and predecessor_id in current and predecessor_id not in inherited_from:
                # Superseding retires the whole producer, including inherited
                # responsibility from older checkpoints with narrowed repairs.
                inherited_from.add(predecessor_id)
                predecessor = current[predecessor_id]
                predecessor_metadata = predecessor.get("metadata", {})
                owned_paths = list(dict.fromkeys([*predecessor.get("owned_paths", []), *owned_paths]))
                output_artifacts = list(dict.fromkeys([*predecessor.get("output_artifacts", []), *output_artifacts]))
                expected_outputs = dedupe_declared_outputs([
                    *predecessor_metadata.get("expected_outputs", []), *expected_outputs,
                ])
                # Fix 22 (live r11 acceptance-reference audit): delivery
                # commitments survive rewording.  The security-track rebuild
                # (fix 15b rewrote goals toward evidence normalization)
                # silently dropped O-02.C04 — successors declared only what
                # the reworded goal restated, the orphaned criterion made
                # final acceptance unevaluable forever.  A takeover inherits
                # the UNION of its lineage's delivery ids; the planner may
                # add, never subtract.
                inherited_output_ids.extend(
                    str(item).strip()
                    for item in (predecessor_metadata.get("delivery_output_ids") or [])
                    if str(item).strip()
                )
                inherited_criterion_ids.extend(
                    str(item).strip()
                    for item in (predecessor_metadata.get("delivery_criterion_ids") or [])
                    if str(item).strip()
                )
                predecessor_id = str(predecessor_metadata.get("recovery_of")
                                     or predecessor_metadata.get("supersedes_node_id") or "")
            if task_type == "implementation" and not (owned_paths or output_artifacts):
                raise ValueError(
                    "implementation graph node must declare owned_paths or output_artifacts: "
                    f"{raw.get('node_id') or raw.get('task_id') or '<dynamic>'}"
                )
            normalized.setdefault("role", "governed_worker")
            normalized.setdefault("status", "admitted")
            normalized["status"] = normalize_status(normalized["status"])
            trusted_control = (node_id in _trusted_control_nodes and is_control_node
                               and role == "user_intent" and normalized["status"] == "completed")
            if normalized["status"] not in {"admitted", "waiting_dependencies", "ready"} and not trusted_control:
                raise ValueError(f"new graph node cannot claim an execution lifecycle status: {normalized['status']} ({node_id})")
            valid_statuses = {status.value for status in TaskStatus}
            if normalized["status"] not in valid_statuses:
                raise ValueError(
                    f"invalid node status in graph patch: {normalized['status']} ({node_id})"
                )
            normalized["owned_paths"] = owned_paths
            normalized["input_artifacts"] = input_artifacts
            normalized["output_artifacts"] = output_artifacts
            normalized["handoff_contract"] = handoff_contract
            # P6-D (doc 32 §4): a patch-added node must never escape to an
            # unbounded execution.  r14's stalled-replan successor ran 4.9h /
            # r1100+ tool rounds with budget={} because zero means unlimited
            # downstream (agents/base.py only builds TaskExecutionBudget when
            # a positive cap exists).  Declared positive budgets are preserved
            # verbatim; only missing/zero caps are filled from the default
            # table, and the fill is recorded for audit.
            from AstralNexus.runtime.task_execution_budget import ensure_task_budget

            node_execution_target = str(raw.get("execution_target") or "cloud")
            # F4 (doc 51 §22): a recovery successor's declared cap is floored at
            # the role default — it is taking over a failed node, not restating
            # a plan, and must not inherit an already-spent small quota.
            node_budget, budget_defaulted = ensure_task_budget(
                raw.get("budget"),
                task_type=str(task_type or ""),
                execution_target=node_execution_target,
                repair_round=bool(str(recovery_of or "").strip()),
            )
            if budget_defaulted:
                self.trace_recorder.record("task_budget.defaulted", {
                    "node_id": node_id,
                    "task_type": str(task_type or ""),
                    "execution_target": node_execution_target,
                    "max_tool_calls": node_budget.get("max_tool_calls"),
                    "source": "graph_patch_default_table",
                })
            normalized["metadata"] = {
                **metadata,
                "required_capabilities": caps,
                "task_type": task_type,
                "declared_task_type": declared_task_type,
                "expected_outputs": expected_outputs,
                "owned_paths": owned_paths,
                "input_artifacts": input_artifacts,
                "output_artifacts": output_artifacts,
                "handoff_contract": handoff_contract,
                "recovery_of": recovery_of,
                "source_requirements": [
                    str(item) for item in (raw.get("source_requirements", []) or [])
                    if str(item).strip()
                ],
                "derivation": str(raw.get("derivation") or "").strip(),
                "delivery_output_ids": list(dict.fromkeys([
                    *[
                        str(item).strip()
                        for item in (raw.get("delivery_output_ids", []) or [])
                        if str(item).strip()
                    ],
                    *inherited_output_ids,
                ])),
                "delivery_criterion_ids": list(dict.fromkeys([
                    *[
                        str(item).strip()
                        for item in (raw.get("delivery_criterion_ids", []) or [])
                        if str(item).strip()
                    ],
                    *inherited_criterion_ids,
                ])),
                "priority": raw.get("priority", 50),
                "criticality": raw.get("criticality", "medium"),
                "execution_target": raw.get("execution_target", "cloud"),
                "budget": node_budget,
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
                "ownership_normalizations": ownership_normalizations,
            }
            if owned_paths:
                for output in normalized["metadata"]["expected_outputs"]:
                    # 起 A / doc 51 边界① + b33 S2 (M1)：dict 与 str 两种声明形状
                    # 现在走同一份声明层归一（core/task_declaration）。此前 dict 条目
                    # 会被整体 repr 成字符串、str 条目会连说明尾巴一起当路径，合法声明
                    # 于是被误判成 "declared output is outside owned_paths" 而整份补丁
                    # 被拒。归一后的键与 same_or_under 同处 S1 的 identity_key 平面。
                    # b33 S4 §8vigesies（用户 2026-10-06 指令，方向更正）：这里原先刻意
                    # 放宽的那一侧（MIXED-DECLARATION-LENIENCY「多个候选只要一个落在可写
                    # 范围内就整条放行」）已按用户要求关闭——一条声明只能表达一个明确路径
                    # 或一个明确目录范围。字符串混杂多个路径时在**准入阶段**返回具体错误，
                    # 而不是让后续文件写入检查替任务归属检查兜底。候选集合仍由同一份
                    # task_declaration 归一（dict 与字符串不分叉判定），所以这次收口不是
                    # 重新引入分支不对称。
                    # 判据与落地前等价：先用 path_spelling 复现旧跳过规则（观察对象
                    # 不是路径的条目一律不进比较），再对候选键集合做归属比较。
                    # b33 S4 (F16)：结构化条目在具名路径字段里写出的路径本身就是显式文件
                    # 主张，无后缀名（README）不再被后缀白名单静默跳过；散文条目保持旧后缀
                    # 规则（散文里提到的无后缀名字仍然只是 hint）。结构化条目被看见得更多，
                    # 于是这条门只会更严不会更松：声明了却无人拥有的文件依旧在此被拒。
                    requirement = declared_requirement(output)
                    spelling = requirement.path_spelling
                    structured = bool(declared_output_entry(output))
                    if spelling.startswith("artifact:"):
                        continue
                    if not structured and not Path(spelling).suffix:
                        continue
                    # §8vigesies: how many paths does this ONE declaration name?
                    # The extracted claims answer that; the entry's own verbatim
                    # spelling is only a fallback observation, never a second path
                    # (which is why a single path followed by domain prose stays a
                    # legal single-path declaration).
                    claims = list(dict.fromkeys(
                        str(claim) for claim in declared_output_claims(output)
                        if not str(claim).startswith("artifact:")
                    ))
                    if len(claims) > 1:
                        raise ValueError(
                            f"ambiguous declared output: {node_id}: {spelling!r} names "
                            f"{len(claims)} paths ({'; '.join(claims)}); one declaration "
                            "must express exactly one path or one directory scope — "
                            "split it into one entry per path"
                        )
                    candidates = list(dict.fromkeys(
                        path for path in requirement.paths
                        if not path.startswith("artifact:")
                        and (structured or Path(path).suffix)
                    ))
                    if not candidates:
                        continue
                    if not any(
                        same_or_under(owned, path)
                        for owned in owned_paths
                        for path in candidates
                    ):
                        raise ValueError(
                            f"declared output is outside owned_paths: {node_id}: {candidates[0]}; "
                            "expected_outputs must list this node's own writable files, not upstream inputs"
                            + (
                                "; path identity: " + " | ".join(identity_notes)
                                if identity_notes else ""
                            )
                        )
            # A Worker owns its declared output namespace.  Two independent
            # nodes may not silently write the same app/backend or frontend
            # tree; that is a contract error, not a scheduling detail.
            if owned_paths or output_artifacts:
                for existing_id, existing_item in current.items():
                    if existing_id == node_id or existing_item.get("status") not in active_owner_statuses:
                        continue
                    conflicts = output_contract_conflicts(
                        owned_paths,
                        output_artifacts,
                        list(existing_item.get("owned_paths", [])),
                        list(existing_item.get("output_artifacts", [])),
                    )
                    if conflicts and recovery_of != existing_id:
                        raise ValueError(
                            "graph patch has overlapping output ownership: "
                            f"{node_id} conflicts with {existing_id}: {conflicts[:3]}. "
                            "Assign leaf-level disjoint owned_paths (for example "
                            "deliverables/runs/e2e/ and deliverables/runs/security/); "
                            "never assign a shared aggregate directory to multiple nodes. "
                            "若冲突的活跃所有者本身是 blocked/repair_pending 的同职责节点"
                            "（例如既有安全测试或报告聚合节点），优先复用而非新增："
                            "用 update_nodes 把它的 dependencies 重接到新生产者；"
                            "或在新增节点上显式设置 recovery_of=该活跃节点ID 完成接管"
                            "（该节点将被 superseded，谱系保留）。"
                        )
            if recovery_of:
                if recovery_of not in current:
                    raise ValueError(f"graph patch recovery references unknown node: {recovery_of}")
                if current[recovery_of].get("status") not in RECOVERY_TARGET_STATUSES:
                    raise ValueError(
                        "graph patch recovery target must be failed, repair_pending, or blocked: "
                        f"{recovery_of} is {current[recovery_of].get('status')}"
                    )
                normalized["metadata"]["supersedes_node_id"] = recovery_of
            current[node_id] = {
                "status": normalized["status"], "goal": normalized["goal"],
                "metadata": normalized["metadata"], "dependencies": list(normalized["dependencies"]),
                "owned_paths": owned_paths, "input_artifacts": input_artifacts,
                "output_artifacts": output_artifacts,
            }
            additions.append(normalized)
            if requested:
                id_map[requested] = node_id

        # Fix 26 (live r12 A-001 lineage immortality): a criterion binding
        # must be satisfiable inside the node's own contract. O-03.C02
        # demands "File deliverables/docs/architecture.md exists" — a file
        # owned by the docs-bundle node D-001 — yet the architect lineage
        # carried the binding (and fix 22 faithfully propagated it through
        # every takeover). Workers cannot write outside owned_paths, so each
        # generation was verified against an impossible demand and the
        # lineage looped forever. Strip such bindings when ANOTHER node
        # already carries the same criterion (the commitment survives with
        # its real carrier); never strip a sole carrier (orphan risk). This
        # is a post-pass so intra-patch ordering cannot shield a
        # mis-binding (the initial r12 graph added A-001 before D-001).
        criterion_texts = declared_criterion_texts(
            (self.delivery_contract or {}).get("required_outputs", []) or []
        )
        if criterion_texts:
            for normalized in additions:
                normalized_metadata = normalized.get("metadata", {})
                bound_ids = [
                    str(cid).strip()
                    for cid in (normalized_metadata.get("delivery_criterion_ids") or [])
                    if str(cid).strip()
                ]
                if not bound_ids:
                    continue
                owned_norm = [
                    identity_key(path)
                    for path in (normalized.get("owned_paths") or [])
                    if str(path).strip()
                ]
                stripped_ids: List[str] = []
                kept_ids: List[str] = []
                for cid in bound_ids:
                    text = criterion_texts.get(cid, "")
                    file_tokens = [
                        identity_key(token)
                        for token in re.findall(
                            r"[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,10}", text
                        )
                        if "/" in token or "\\" in token
                    ]
                    outside_files = [
                        token for token in file_tokens
                        if token and not any(
                            same_or_under(owned, token) for owned in owned_norm
                        )
                    ]
                    has_other_carrier = False
                    if outside_files:
                        this_id = str(normalized.get("node_id") or "")
                        for other_id, other in current.items():
                            if other_id == this_id:
                                continue
                            other_ids = {
                                str(item).strip()
                                for item in (
                                    (other.get("metadata") or {}).get("delivery_criterion_ids") or []
                                )
                            }
                            if cid in other_ids:
                                has_other_carrier = True
                                break
                    if outside_files and has_other_carrier:
                        stripped_ids.append(cid)
                    else:
                        kept_ids.append(cid)
                if stripped_ids:
                    normalized_metadata["delivery_criterion_ids"] = kept_ids
                    self.trace_recorder.record(
                        "governor.patch.criterion_binding_stripped",
                        {
                            "node_id": str(normalized.get("node_id") or ""),
                            "stripped_criterion_ids": stripped_ids,
                            "reason": (
                                "criterion demands concrete files outside owned_paths "
                                "and another node carries the same criterion"
                            ),
                        },
                    )

        # F11 (live b31): the frozen contract prose named the entry file that the
        # whole delivery is supposed to start from, yet no node owned that file
        # and no criterion anchored it; the graph then "satisfied" the clause by
        # spending a whole repair round on an empty shim.  A clause that names a
        # concrete file is a delivery obligation: it must be owned by some node,
        # or it is an interface constraint and must be stated as one.  Record the
        # unanchored ones as gaps — a silently dropped contract clause is exactly
        # what the frozen contract exists to prevent.
        clause_projection = {key: dict(value) for key, value in current.items()}
        for cancellation_id in patch.cancel_nodes:
            if cancellation_id in clause_projection:
                clause_projection[cancellation_id]["status"] = "cancelled"
        candidate_clause_gaps = self._unanchored_contract_clauses(clause_projection, additions)
        # b33 S4 §1.2: ONE scan event per scan, not one per gap.  The same gaps,
        # clauses and sources are still reported — in a single payload — so the
        # reported event count stops being a function of how many documents a run
        # happened to write.  The authoritative full list is the applied-patch
        # result below; this event is the auditable record of that one scan.
        if candidate_clause_gaps:
            self.trace_recorder.record("governor.contract.clause_unanchored", {
                "count": len(candidate_clause_gaps),
                "paths": [dict(gap) for gap in candidate_clause_gaps],
            })

        # The first execution-plane patch is the executable interpretation of
        # the delivery contract. Reject a locally valid but incomplete graph
        # before it can become runtime truth. Later incremental patches may be
        # intentionally narrow because the existing graph already carries the
        # rest of the contract.
        execution_nodes_exist = any(
            str(item.get("metadata", {}).get("plane") or "execution") != "control"
            and str(item.get("role") or "").lower()
            not in {"user_intent", "planner", "governor"}
            and str(item.get("metadata", {}).get("type") or "")
            not in {"user_intent", "root", "planning_root"}
            and str(item.get("metadata", {}).get("source") or "")
            not in {"user_intent", "user_goal", "governor_planning"}
            for node_id, item in current.items()
            if node_id in preexisting_node_ids
        )
        known_output_ids = {
            str(item.get("output_id") or "").strip()
            for item in self.delivery_contract.get("required_outputs", [])
            if isinstance(item, dict) and str(item.get("output_id") or "").strip()
        }
        known_criterion_ids = {
            str(cid).strip()
            for item in self.delivery_contract.get("required_outputs", [])
            if isinstance(item, dict)
            for cid in declared_criterion_ids(item)
            if str(cid).strip()
        }
        required_output_ids = {
            str(item.get("output_id") or "").strip()
            for item in self.delivery_contract.get("required_outputs", [])
            if isinstance(item, dict)
            and bool(item.get("required", True))
            and str(item.get("output_id") or "").strip()
        }
        required_criterion_ids = {
            str(cid).strip()
            for item in self.delivery_contract.get("required_outputs", [])
            if isinstance(item, dict) and bool(item.get("required", True))
            for cid in declared_criterion_ids(item)
            if str(cid).strip()
        }
        # Doc 17 第二批② (proof: 恢复目标不凭空增加格式要求): the unknown-ID
        # gate below only guards the FIRST execution patch. A recovery patch
        # binding an invented criterion ID (a format requirement nobody asked
        # for) would ride into the lineage via fix-22 inheritance and become
        # immortal — the r12 O-03 mis-binding lesson in its active form.
        # Whenever the contract defines criterion IDs, every addition of every
        # patch may only bind known IDs; inherited lineage IDs were validated
        # at their own admission, so the merged set stays inside the contract.
        if known_criterion_ids:
            for node in [*additions, *patch.update_nodes]:
                declared_ids = {
                    str(criterion_id).strip()
                    for criterion_id in [*((node.get("metadata") or {}).get("delivery_criterion_ids") or []),
                                         *(node.get("delivery_criterion_ids") or [])]
                    if str(criterion_id).strip()
                }
                invented_ids = declared_ids - known_criterion_ids
                if invented_ids:
                    raise ValueError(
                        "graph patch binds delivery criterion IDs unknown to the "
                        f"contract: {sorted(invented_ids)} (node "
                        f"{node.get('node_id') or node.get('task_id') or '<dynamic>'}); "
                        "recovery nodes may not invent acceptance requirements"
                    )
        execution_additions = [
            node
            for node in additions
            if str(node.get("metadata", {}).get("plane") or "execution")
            != "control"
            and str(node.get("role") or "").lower()
            not in {"user_intent", "planner", "governor"}
            and str(node.get("metadata", {}).get("type") or "")
            not in {"user_intent", "root", "planning_root"}
        ]
        if required_output_ids and not execution_nodes_exist and execution_additions:
            covered_output_ids = {
                output_id
                for node in execution_additions
                for output_id in node.get("metadata", {}).get(
                    "delivery_output_ids", []
                )
            }
            unknown_output_ids = covered_output_ids - known_output_ids
            if unknown_output_ids:
                raise ValueError(
                    "graph patch claims unknown delivery output IDs: "
                    f"{sorted(unknown_output_ids)}"
                )
            missing_output_ids = required_output_ids - covered_output_ids
            if missing_output_ids:
                raise ValueError(
                    "graph patch does not cover required delivery outputs: "
                    f"{sorted(missing_output_ids)}. Every required output must "
                    "be assigned to at least one execution node through "
                    "delivery_output_ids."
                )
            covered_criterion_ids = {
                criterion_id
                for node in execution_additions
                for criterion_id in node.get("metadata", {}).get(
                    "delivery_criterion_ids", []
                )
            }
            unknown_criterion_ids = covered_criterion_ids - known_criterion_ids
            if unknown_criterion_ids:
                raise ValueError(
                    "graph patch claims unknown delivery criterion IDs: "
                    f"{sorted(unknown_criterion_ids)}"
                )
            missing_criterion_ids = required_criterion_ids - covered_criterion_ids
            if missing_criterion_ids:
                criterion_ids_by_output: Dict[str, Set[str]] = {}
                for criterion_id in required_criterion_ids:
                    output_id = criterion_id.split(".C", 1)[0]
                    criterion_ids_by_output.setdefault(output_id, set()).add(
                        criterion_id
                    )
                coverage_by_output = {
                    output_id: (
                        len(criteria & covered_criterion_ids) / len(criteria)
                        if criteria else 1.0
                    )
                    for output_id, criteria in criterion_ids_by_output.items()
                }
                total_coverage = (
                    len(required_criterion_ids & covered_criterion_ids)
                    / len(required_criterion_ids)
                )
                below_minimum = {
                    output_id: round(ratio, 3)
                    for output_id, ratio in coverage_by_output.items()
                    if ratio
                    < self.policy.initial_contract_min_output_criterion_coverage
                }
                if (
                    below_minimum
                    or total_coverage
                    < self.policy.initial_contract_min_total_criterion_coverage
                ):
                    raise ValueError(
                        "graph patch does not cover enough required delivery criteria: "
                        f"missing={sorted(missing_criterion_ids)}, "
                        f"coverage_by_output={coverage_by_output}, "
                        f"total_coverage={total_coverage:.3f}. Initial execution "
                        "graph must cover every delivery boundary; remaining details "
                        "may be expanded only after Global Verifier feedback."
                    )
                for node in execution_additions:
                    node_outputs = set(
                        node.get("metadata", {}).get("delivery_output_ids", [])
                    )
                    deferred = sorted(
                        criterion_id
                        for criterion_id in missing_criterion_ids
                        if criterion_id.split(".C", 1)[0] in node_outputs
                    )
                    if deferred:
                        node["metadata"]["deferred_delivery_criterion_ids"] = deferred

        # Resolve model-written artifact aliases before admitting the graph.
        # Planner output commonly varies only by case, extension punctuation,
        # or by using the staged path instead of the durable Artifact ID. A
        # unique semantic match is safe; an unknown or ambiguous handoff is a
        # graph-contract error and must trigger replanning.
        # F-A (P4 Round-1): the alias key used to collapse every path to its
        # basename, so three registrations of ONE file (absolute, relative,
        # artifact: id of a contract bundle) landed in THREE buckets and the
        # consumer saw "ambiguous" for a single physical file.  The key is now
        # path-anchored: relative paths resolve against the run's identity
        # root so relative and absolute spellings of one file share one key,
        # while bare basenames no longer match files in different directories.
        # artifact: IDs keep their own semantic channel (id text, case- and
        # punctuation-folded) so ID references stay exact-channel resolvable.
        # identity_root is bound once before the add_nodes loop (F-A).

        def artifact_alias_key(value: Any) -> str:
            candidate = str(value or "").strip().replace("\\", "/").lower()
            if not candidate:
                return ""
            if candidate.startswith("artifact:"):
                # ID channel: fold case/punctuation, never path-anchored.
                return re.sub(
                    r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", candidate.split(":", 1)[1]
                ).strip("_")
            if re.match(r"^(?:[a-z]:|//|/)", candidate):
                return candidate
            if identity_root is not None:
                rooted = Path(str(identity_root)) / candidate
                return str(rooted).replace("\\", "/").lower()
            return candidate

        artifact_aliases: Dict[str, Set[str]] = {}
        artifact_producers: Dict[str, Optional[str]] = {}
        # Fix P5#19: a terminal-inactive declarer (failed/superseded/cancelled)
        # must not carry artifact producer lineage. r17-resume#5 field evidence
        # (2026-10-04): artifact:seed-script resolved to the failed
        # R-SEED-CHECK-WRITEFREE-ONESHOT because the last declarer won, so the
        # consumer-dependency check below forced every stalled-replan patch to
        # declare a dependency on a FAILED node — unreadyable by construction.
        # Three consecutive rejected patches burned recovery churn toward
        # exhaustion while the completed R-SEED-CLEAN-ONESHOT carried the same
        # artifact and was already a declared dependency of the consumers.
        # Aliases stay registered from inactive declarers too (the alias
        # resolution below applies its own acceptance-status filter); only the
        # canonical producer mapping is demoted: a valid declarer (any status
        # except failed/superseded/cancelled — completed counts, it is a
        # satisfiable dependency target) overrides, an inactive declarer fills
        # only unmapped canonicals, and when NO valid carrier exists anywhere
        # the original error path stands, so genuinely orphaned artifacts still
        # force root-cause routing instead of silently passing.
        inactive_producer_statuses = {"failed", "superseded", "cancelled"}

        def status_value(raw: Any) -> str:
            if raw is None:
                return ""
            return raw.value if isinstance(raw, TaskStatus) else str(raw)

        def producer_status(producer_id: Optional[str]) -> str:
            if not producer_id:
                return ""
            item = current.get(str(producer_id))
            if item is not None:
                return status_value(item.get("status"))
            node = self.task_graph.get_node(str(producer_id))
            return status_value(node.status) if node is not None else ""

        def register_artifact_alias(alias: Any, canonical: str, producer: Optional[str]) -> None:
            key = artifact_alias_key(alias)
            if key:
                artifact_aliases.setdefault(key, set()).add(canonical)
            artifact_producers[canonical] = producer

        for artifact_id, artifact in self.task_graph.artifacts.items():
            canonical = str(artifact_id)
            register_artifact_alias(canonical, canonical, artifact.node_id)
            register_artifact_alias(artifact.path, canonical, artifact.node_id)
        for producer_id, item in current.items():
            inactive = status_value(item.get("status")) in inactive_producer_statuses
            for output_id in item.get("output_artifacts", []):
                canonical = str(output_id)
                key = artifact_alias_key(output_id)
                if key:
                    artifact_aliases.setdefault(key, set()).add(canonical)
                if not inactive or canonical not in artifact_producers:
                    artifact_producers[canonical] = producer_id

        input_contract_errors: List[str] = []
        for node in additions:
            resolved_inputs: List[str] = []
            expanded_inputs: List[str] = []
            for required in node.get("input_artifacts", []):
                directory = str(required).replace("\\", "/")
                if directory.endswith("/"):
                    parts = directory.strip("/").split("/")
                    members = []
                    if parts and all(part not in {"", ".", ".."} for part in parts):
                        prefix = "/" + identity_key(declared_relative(directory, root=identity_root)).rstrip("/") + "/"
                        for artifact_id, artifact in self.task_graph.artifacts.items():
                            producer = self.task_graph.get_node(artifact.node_id) if artifact.node_id else None
                            try:
                                artifact_path = Path(artifact.path)
                                relative = artifact_path.relative_to(Path(identity_root)) if artifact_path.is_absolute() and identity_root else artifact_path
                                path = "/" + identity_key(declared_relative(relative.as_posix(), root=identity_root))
                            except (PathIdentityError, ValueError):
                                continue
                            if (path.startswith(prefix)
                                    and getattr(artifact.acceptance_status, "value", artifact.acceptance_status) == "accepted"
                                    and producer and producer.status not in {"superseded", "cancelled"}):
                                members.append(str(artifact_id))
                    if not members:
                        input_contract_errors.append(
                            f"graph patch directory input has no accepted artifacts: {required} ({node['node_id']})"
                        )
                    expanded_inputs.extend(sorted(members))
                else:
                    expanded_inputs.append(str(required))
            for required in expanded_inputs:
                raw_required = str(required)
                exact = raw_required if raw_required in artifact_producers else ""
                alias_candidates = artifact_aliases.get(artifact_alias_key(raw_required), set())
                if not exact and len(alias_candidates) > 1:
                    accepted = set()
                    for candidate in alias_candidates:
                        candidate_record = self.task_graph.artifacts.get(candidate)
                        producer = self.task_graph.get_node(candidate_record.node_id) if candidate_record and candidate_record.node_id else None
                        if (candidate_record and getattr(candidate_record.acceptance_status, "value", candidate_record.acceptance_status) == "accepted"
                                and producer and producer.status not in {"superseded", "cancelled"}):
                            accepted.add(candidate)
                    if accepted:
                        alias_candidates = accepted
                    # F-A (P4 Round-1): multiple registrations can be ONE
                    # artifact seen through different spellings (absolute,
                    # relative, contract bundle id) or one producer that
                    # re-registered the same file.  Collapse them only when
                    # they agree on the contract that matters: same producer
                    # node, no competing task_version/retry_count values
                    # (absent metadata is undeclared, never a conflict), and
                    # all accepted.  Exact-path unification already happened
                    # in the alias key, so what is left here is producer+version
                    # agreement — never "same node+same path family" alone.
                    records: List[Any] = []
                    for alias_key in sorted(alias_candidates):
                        alias_record = self.task_graph.artifacts.get(alias_key)
                        if alias_record is None:
                            # An unregistered id in the bucket cannot be
                            # compared for contract agreement: no collapse
                            # (falls through to the ambiguity error).
                            records.clear()
                            break
                        records.append(alias_record)
                    if len(records) > 1:
                        node_ids = {str(record.node_id) for record in records}
                        versions = {
                            str(record.metadata.get("task_version"))
                            for record in records
                            if record.metadata.get("task_version") is not None
                        }
                        retries = {
                            str(record.metadata.get("retry_count"))
                            for record in records
                            if record.metadata.get("retry_count") is not None
                        }
                        all_accepted = all(
                            getattr(record.acceptance_status, "value", record.acceptance_status) == "accepted"
                            for record in records
                        )
                        if len(node_ids) == 1 and len(versions) <= 1 and len(retries) <= 1 and all_accepted:
                            representative = sorted(
                                records,
                                key=lambda record: (
                                    0 if str(record.artifact_type) != "contract_bundle" else 1,
                                    str(record.artifact_id),
                                ),
                            )[0]
                            alias_candidates = {str(representative.artifact_id)}
                if exact:
                    canonical = exact
                elif len(alias_candidates) == 1:
                    canonical = next(iter(alias_candidates))
                elif not alias_candidates:
                    # F-A (P4 Round-1): an unregistered input is not
                    # automatically illegal.  A replan patch legitimately
                    # consumes artifacts a NEW node in this same patch will
                    # produce (round-1 N-202 reading
                    # runs/e2e_realserver/run1.log owned by sibling N-201).
                    # Admit it as a planned dependency when the RAW projection
                    # plane of this patch declares a covering producer; the
                    # graph contract (no fabrication: a producer must exist
                    # somewhere) is still enforced, only across the whole
                    # patch instead of across already-registered artifacts
                    # only.
                    producer_id = ""
                    covering_path = ""
                    projection_plane: List[tuple] = list(patch_projection_paths.items())
                    for update_raw in patch.update_nodes:
                        if not isinstance(update_raw, dict):
                            continue
                        updater_id = str(
                            update_raw.get("node_id") or update_raw.get("task_id") or ""
                        ).strip()
                        updater_id = id_map.get(updater_id, updater_id)
                        if not updater_id:
                            continue
                        declared = []
                        for key in ("owned_paths", "expected_outputs", "output_artifacts"):
                            raw_declared = update_raw.get(key)
                            if not isinstance(raw_declared, list):
                                continue
                            for item in raw_declared:
                                if str(item).strip():
                                    declared.append(str(item).strip().replace("\\", "/"))
                        projection_plane.append((updater_id, declared))
                    for owner_id, declared_paths in projection_plane:
                        if owner_id == str(node["node_id"]):
                            continue
                        for declared_path in declared_paths:
                            if same_or_under(declared_path, raw_required) or (
                                identity_key(declared_path) == identity_key(raw_required)
                            ):
                                producer_id = owner_id
                                covering_path = declared_path
                                break
                        if producer_id:
                            break
                    if producer_id:
                        canonical = raw_required
                        node["dependencies"] = list(dict.fromkeys([*node.get("dependencies", []), producer_id]))
                        node.setdefault("metadata", {}).setdefault(
                            "planned_dependencies", []
                        ).append({"path": raw_required, "producer": producer_id})
                        self.trace_recorder.record(
                            "governor.patch.planned_input_admitted",
                            {
                                "node_id": str(node["node_id"]),
                                "input": raw_required,
                                "producer": producer_id,
                                "producer_declared_path": covering_path,
                                "reason_code": "PLANNED_DEPENDENCY_IN_PATCH_PROJECTION",
                            },
                        )
                    else:
                        input_contract_errors.append(
                            "graph patch references unknown input artifact: "
                            f"{raw_required} ({node['node_id']}) [no producer in patch projection]"
                        )
                        continue
                else:
                    competing = []
                    for key in sorted(alias_candidates):
                        record = self.task_graph.artifacts.get(key)
                        if record is None:
                            competing.append(f"{key}: unregistered")
                            continue
                        competing.append(
                            f"{key}: node={record.node_id}, "
                            f"task_version={record.metadata.get('task_version')}, "
                            f"retry_count={record.metadata.get('retry_count')}"
                        )
                    input_contract_errors.append(
                        "graph patch input artifact alias is ambiguous: "
                        f"{raw_required} -> {sorted(alias_candidates)} ({node['node_id']})"
                        + "; competing versions: " + "; ".join(competing)
                    )
                    continue
                if canonical not in resolved_inputs:
                    resolved_inputs.append(canonical)
            node["input_artifacts"] = resolved_inputs
            node["metadata"]["input_artifacts"] = list(resolved_inputs)
            current[node["node_id"]]["input_artifacts"] = list(resolved_inputs)

        if input_contract_errors:
            raise ValueError("; ".join(input_contract_errors))

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
                if node.get("input_artifacts") or node.get("handoff_contract"):
                    communication = {
                        "source": dep,
                        "target": node["node_id"],
                        "edge_type": "communicates_to",
                        "metadata": {
                            "created_by": "global_governor",
                            "reason": "declared_artifact_handoff",
                            "input_artifacts": list(node.get("input_artifacts", [])),
                            "handoff_contract": dict(node.get("handoff_contract") or {}),
                        },
                    }
                    proposed_edges[(dep, node["node_id"], "communicates_to")] = communication
                    proposed_communication_links.append({
                        "source": dep,
                        "target": node["node_id"],
                        "edge_type": "communicates_to",
                    })
            superseded = str(node.get("metadata", {}).get("supersedes_node_id") or "").strip()
            if superseded:
                proposed_edges[(superseded, node["node_id"], "supersedes")] = {
                    "source": superseded,
                    "target": node["node_id"],
                    "edge_type": "supersedes",
                    "metadata": {"created_by": "global_governor", "reason": "recovery_takeover"},
                }

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
            "bottleneck_node": bottleneck["node_id"] if bottleneck_is_wide and bottleneck is not None else None,
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
                "or remove PARALLELIZABLE and justify the true dependency. "
                "修复→验证的串行链本身合法：当所有新增节点真实依赖同一个新上游"
                "修复节点时，去掉 PARALLELIZABLE 声明即可通过本门禁，无需人为拆并行。"
            )

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
            proposed_edges[(source, target, edge_type)] = item
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

        # A takeover is exclusive: retaining the old worker as ready would
        # allow both workers to overwrite the same delivery namespace.
        # Exception — a TERMINAL-failed predecessor keeps its failed status:
        # a failed node can never return to ready, so the exclusivity concern
        # does not apply, and superseding it would launder the failure out of
        # the lifecycle record.  Replacement nodes rebuild the track while
        # the terminal markers stay visible (doc 14 §12:50: 保留失败终态).
        for addition in additions:
            target_id = addition.get("metadata", {}).get("supersedes_node_id")
            if not target_id:
                continue
            target_node = self.task_graph.get_node(str(target_id))
            if target_node is not None and target_node.metadata.get("_terminal_failure"):
                self.trace_recorder.record(
                    "governor.patch.terminal_predecessor_kept_failed",
                    {
                        "node_id": str(target_id),
                        "successor": str(
                            addition.get("node_id") or addition.get("task_id") or ""
                        ),
                        "reason": "terminal failure markers must survive takeover",
                    },
                )
                continue
            target_update = next((item for item in patch.update_nodes
                                  if str(item.get("node_id") or item.get("task_id")) == target_id), None)
            if target_update is None:
                patch.update_nodes.append({"node_id": target_id, "status": "superseded"})
            else:
                target_update["status"] = "superseded"

        # Terminal-failed nodes are immutable in their lifecycle status: no
        # patch may clear, roll back, or disguise the failure through a
        # status change.  Non-status field updates (metadata annotations,
        # dependency rewiring around them) remain allowed.
        for update_entry in patch.update_nodes:
            if not isinstance(update_entry, dict):
                continue
            entry_id = str(
                update_entry.get("node_id") or update_entry.get("task_id") or ""
            )
            entry_node = self.task_graph.get_node(entry_id) if entry_id else None
            if entry_node is None or not entry_node.metadata.get("_terminal_failure"):
                continue
            requested_status = str(update_entry.get("status") or "")
            if requested_status and requested_status != entry_node.status:
                self.trace_recorder.record(
                    "governor.patch.terminal_status_change_dropped",
                    {
                        "node_id": entry_id,
                        "requested_status": requested_status,
                        "reason": "terminal failure status is immutable",
                    },
                )
                update_entry.pop("status", None)

        for raw in patch.update_nodes:
            if not isinstance(raw, dict):
                continue
            if "field_name" in raw or "field" in raw or "new_value" in raw:
                raise ValueError(
                    'update_nodes must use direct fields, for example '
                    '{"node_id":"consumer","dependencies":["replacement"]}; '
                    'field_name/field/new_value wrappers are not supported'
                )
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
            if "owned_paths" in raw or "output_artifacts" in raw:
                updated_owned = contract_list(
                    raw.get("owned_paths", current[node_id].get("owned_paths", [])),
                    current[node_id].get("metadata", {}),
                    "owned_paths",
                )
                updated_outputs = contract_list(
                    raw.get("output_artifacts", current[node_id].get("output_artifacts", [])),
                    current[node_id].get("metadata", {}),
                    "output_artifacts",
                )
                updated_owned = [
                    declared_relative(path, root=identity_root) + ("/" if str(path).endswith(("/", "\\")) else "")
                    for path in updated_owned
                ]
                raw["owned_paths"] = updated_owned
                current[node_id]["owned_paths"] = updated_owned
                raw["output_artifacts"] = updated_outputs
                current[node_id]["output_artifacts"] = updated_outputs
                for other_id, other_item in current.items():
                    if current[node_id].get("status") not in active_owner_statuses:
                        continue
                    if other_id == node_id or other_item.get("status") not in active_owner_statuses:
                        continue
                    if output_contract_conflicts(
                        updated_owned,
                        updated_outputs,
                        list(other_item.get("owned_paths", [])),
                        list(other_item.get("output_artifacts", [])),
                    ):
                        raise ValueError(
                            "graph patch update has overlapping output ownership: "
                            f"{node_id} conflicts with {other_id}"
                        )

        # Validate handoffs for existing consumers as well as new nodes. A
        # replacement producer must not leave consumers attached to the old
        # owner or silently drop their data dependency.
        for consumer_id, item in current.items():
            if item.get("status") in {"completed", "failed", "cancelled", "superseded"}:
                continue
            updates = next((raw for raw in patch.update_nodes if remap(raw.get("node_id") or raw.get("task_id")) == consumer_id), {})
            inputs = updates.get("input_artifacts", item.get("input_artifacts", []))
            for artifact_id in inputs:
                input_producer_id = artifact_producers.get(str(artifact_id))
                if input_producer_id and input_producer_id != consumer_id and (input_producer_id, consumer_id, "depends_on") not in proposed_edges:
                    # Fix AR: the message used to say only "update_nodes must set
                    # dependencies directly".  Live r13b hit this during checkpoint
                    # restore on a consumer whose dependencies DID list the producer,
                    # so the instruction was unactionable and the run became
                    # unrestorable.  State what was actually observed.
                    declared_deps = list(current.get(consumer_id, {}).get("dependencies", []) or [])
                    raise ValueError(
                        f"graph patch artifact producer is not a declared dependency: "
                        f"{artifact_id} is produced by {input_producer_id}, consumed by {consumer_id}; "
                        f"update_nodes must set dependencies directly "
                        f"[observed: consumer_status={current.get(consumer_id, {}).get('status')!r}, "
                        f"producer_status={producer_status(input_producer_id)!r}, "
                        f"producer_in_declared_dependencies="
                        f"{input_producer_id in declared_deps}, "
                        f"declared_dependencies={declared_deps}, "
                        f"depends_on_edges_into_consumer="
                        f"{sorted(k[0] for k in proposed_edges if k[1] == consumer_id and k[2] == 'depends_on')}, "
                        f"patch_updates={[{k: v for k, v in x.items() if k in ('node_id', 'task_id', 'status', 'dependencies')} for x in patch.update_nodes if isinstance(x, dict)]}, "
                        f"patch_adds={[remap(x.get('node_id') or x.get('task_id')) for x in (patch.add_nodes or []) if isinstance(x, dict)]}, "
                        f"patch_cancels={list(patch.cancel_nodes or [])}]"
                    )

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
            cancelled_node = self.task_graph.get_node(node_id)
            if cancelled_node is None:
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
        from AstralNexus.core.write_authority import committed_transfers, validate_current_holders, validate_staged_transfers
        grants = list(committed_transfers(self.task_graph.nodes))
        if self.ownership_transfers_provider is not None:
            grants.extend(self.ownership_transfers_provider())
        validate_current_holders(current, grants)
        validate_staged_transfers(current, [*additions, *patch.update_nodes])
        GraphAdmissionService.validate_projection(self, current, additions)
        if _validate_only:
            return {"added": [node["node_id"] for node in additions], "validated_only": True}
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
        self.contract_clause_gaps = candidate_clause_gaps
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
            # F11: the applied patch carries the contract clauses it left
            # unowned, so the next planning round and the audit trail read the
            # same fact the planner was told.
            "contract_clause_gaps": [dict(gap) for gap in candidate_clause_gaps],
            # b33 S4 (CONTRACTION_PLAN §8quater:111): the one authoritative
            # carrier now also answers the counts the audit asks for, so they are
            # never recomputed by a second path.  One scan event per scan
            # (events_recorded), the emitted obligation count, the surviving
            # clause sources, and whether the unchanged cap was reached.
            "contract_clause_gap_metrics": {
                "emitted": len(candidate_clause_gaps),
                "events_recorded": 1 if candidate_clause_gaps else 0,
                "sources": sorted(
                    {str(gap.get("source") or "") for gap in candidate_clause_gaps}
                ),
                "capped": len(candidate_clause_gaps) >= self.CLAUSE_GAP_LIMIT,
            },
            # Fix S1: every declared/criterion path this patch relocated, read
            # back from the admitted nodes themselves.  The existing
            # GRAPH_PATCH_APPLIED payload carries it; no new event type.
            "ownership_normalizations": [
                dict(item)
                for node in additions
                for item in (
                    (node.get("metadata") or {}).get("ownership_normalizations") or []
                )
            ],
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
                        "owned_paths": list(node.owned_paths or node.metadata.get("owned_paths", [])),
                        "input_artifacts": list(node.input_artifacts or node.metadata.get("input_artifacts", [])),
                        "output_artifacts": list(node.output_artifacts or node.metadata.get("output_artifacts", [])),
                        "handoff_contract": dict(node.handoff_contract or node.metadata.get("handoff_contract", {})),
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
                "owned_paths": list(node.owned_paths),
                "input_artifacts": list(node.input_artifacts),
                "output_artifacts": list(node.output_artifacts),
                "delivery_output_ids": node.metadata.get("delivery_output_ids", []),
                "delivery_criterion_ids": node.metadata.get("delivery_criterion_ids", []),
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
            "patch": {
                **TaskGraphPatch().as_dict(),
                "add_nodes": [{"node_id": "new-id", "goal": "string",
                               "dependencies": ["existing-or-new-id"],
                               "owned_paths": ["deliverables/docs/change.md"],
                               "expected_outputs": ["deliverables/docs/change.md"]}],
                "update_nodes": [{"node_id": "existing-id", "goal": "updated goal"}],
                "add_edges": [{"source": "producer-id", "target": "consumer-id", "edge_type": "depends_on"}],
                "remove_edges": [{"source": "producer-id", "target": "consumer-id", "edge_type": "depends_on"}],
            },
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
                    "示例 ID 只是格式，禁止原样使用；没有操作的数组返回空数组。"
                    "边必须使用 source、target、edge_type 字段，方向为生产者到消费者。"
                    "更新节点直接填写 node_id 和要改的字段，不使用 field_name/new_value 包装。"
                    "保留现有 owned_paths、产物 ID 及交付覆盖；新增写入不得与既有负责范围重叠。"
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
        from .patch_fields import model_patch_fields
        patch_data = model_patch_fields(patch_data)
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
        rejection_reason: str = "",
        worker_report: Dict[str, Any] | None = None,
        cap_exemption_recipe: str = "",
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
                "owned_paths": list(item.owned_paths),
                "input_artifacts": list(item.input_artifacts),
                "output_artifacts": list(item.output_artifacts),
                "handoff_contract": dict(item.handoff_contract),
            }
            for item in self.task_graph.nodes.values()
        ]
        # The worker's own report (summary, root-cause statement, proposed
        # next_actions) is first-class routing input.  Live audits showed
        # replans ignoring an explicit "remove the SP-007 skip in init_db.py"
        # escalation and instead iterating test-side workarounds for six
        # generations because the recovery prompt never carried the report.
        report = worker_report or {}
        from AstralNexus.runtime.development_playbook import recovery_procedures
        procedures = recovery_procedures([
            observation
            for dimension in dimensions if isinstance(dimension, dict)
            for observation in (dimension.get("observations") or [])
        ] or report.get("verification_observations") or
            (node.metadata.get("_verification_receipt") or {}).get("observations"),
            owner_node_id=node.node_id,
            nodes=self.task_graph.nodes.values())
        failed_node_report: Dict[str, Any] = {
            "repair_procedures": procedures,
            "summary": str(report.get("summary") or "")[:800],
            "execution_outcome": str(report.get("execution_outcome") or ""),
            "next_actions": list(report.get("next_actions") or [])[:8],
        }
        # These were not value-rejected: the Worker protocol supplies no
        # economic scores. The independent rejection now supplies the context
        # in which Governor must decide required repair vs optional expansion.
        failed_node_report["repair_candidates_pending_review"] = list(
            node.metadata.get("repair_candidates_pending_review") or []
        )[:8]
        failed_node_report["runtime_accounting"] = dict(node.metadata.get("_attempt_accounting") or {})
        failed_node_report["repair_relationships"] = {
            "recovery_of": "failed/repair_pending/blocked task being recovered; never the completed author",
            "write_permission": "declare root-cause owned_paths; grant is validated at graph commit",
            "evidence_source": "original execution and version; not a recovery target",
        }
        if report.get("last_failure"):
            failed_node_report["last_failure"] = report["last_failure"]
        if report.get("historical_execution_failure"):
            failed_node_report["historical_execution_failure"] = report["historical_execution_failure"]
        if report.get("verification_observations"):
            failed_node_report["verification_observations"] = report["verification_observations"]
        # Cross-generation recovery state: inherited original receipts and
        # the lineage's already-tried attempts are first-class routing input
        # (user directive 2026-09-11: 按原始任务谱系/根因/失败回执路由).
        if report.get("next_actions_inherited_from"):
            failed_node_report["next_actions_inherited_from"] = str(
                report.get("next_actions_inherited_from")
            )
        if report.get("lineage_root"):
            failed_node_report["lineage_root"] = str(report.get("lineage_root"))
        if report.get("lineage_already_tried"):
            failed_node_report["lineage_already_tried"] = list(
                report.get("lineage_already_tried") or []
            )[-6:]
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
                    "owned_paths": list(node.owned_paths),
                    "input_artifacts": list(node.input_artifacts),
                    "output_artifacts": list(node.output_artifacts),
                    "expected_outputs": node.metadata.get("expected_outputs", []),
                    "delivery_output_ids": node.metadata.get("delivery_output_ids", []),
                    "delivery_criterion_ids": node.metadata.get("delivery_criterion_ids", []),
                },
                "failed_node_report": failed_node_report,
                "repair_procedure_policy": (
                    "repair_procedures 是按真实验收原因选择的预定路线；优先按 route 分配责任，"
                    "修复实现/采集器后运行 recheck_command。保留对应 criterion 于修复节点或依赖它的复验节点。"
                    "产物类路线（route=produce_missing_artifact，recheck_command 为空）没有命令行可跑："
                    "必须先由 owner 真实生成 missing_paths 列出的文件，再让该 criterion_id 的确定性检查重新执行；"
                    "不得重跑失败命令、不得改写该验收条款、也不得要求重新生成已存在且有效的证据。"
                    "inactive_owners 里的节点已 superseded/cancelled/failed，不得再把工作派回那条链。"
                    "仅调查、装配报告或更换节点名不消除该缺口；前提不成立必须引用反证。"
                ),
                "verification": {
                    "verdict": verdict,
                    "overall_score": overall_score,
                    "dimensions": dimensions,
                },
                "previous_patch_rejection": str(rejection_reason)[:1000],
                "lineage_cap_exemption_recipe": str(cap_exemption_recipe)[:4000],
                "task_graph": project_planning_graph(graph, focus_ids=(node_id,)),
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
                        "historical_execution_failure 是已被成功重执行取代的旧异常，不是当前失败；"
                        "不得以旧产物缺失要求重复生成已经落盘的文件。当前缺口以本次 verification 与实测为准。"
                        "只输出 TaskGraphPatch JSON，不执行任务，不声称修复完成。"
                        "已有节点只能通过 update_nodes 更新；新工作只能放入 add_nodes。"
                        "所有依赖必须引用当前图节点或本 patch 新节点，不得制造环。"
                        "若原节点可通过新方法重试，将其从 repair_pending 更新为 ready；"
                        "禁止把 repair_pending 直接更新为 completed；completed 只能由 Verifier 在真实重执行后签发。"
                        "若需要替代方案，新增具备明确能力、验收条件和依赖的节点。"
                        "新增节点若接管原文件或产物，必须填写 recovery_of=failed_node.node_id，"
                        "并保留 owned_paths、output_artifacts、交付条款与验收责任。"
                        "只改节点 ID 或取消原节点不能代替显式 recovery_of。"
                        "消费者的 dependencies 必须改连新生产者，input_artifacts 保留契约 ID。"
                        "如果只需在原负责范围补充证据，优先更新原节点 goal、acceptance_criteria 和 status=ready，"
                        "不要新增重复负责节点。修复路径错误时同时更正 owned_paths 与 expected_outputs。"
                        "不得因重试次数机械通过质量门，也不得删除已有约束或证据。"
                        "验证恢复通常不需要新增通信边；add_edges 默认必须为空。"
                        "如果确实需要通信边，每项必须同时提供非空字符串 source、target、edge_type；"
                        "禁止输出 null、None、空字符串或缺字段的边对象。"
                        "failed_node_report 是失败 Worker 的根因声明与建议动作，必须作为一级路由输入。"
                        "repair_candidates_pending_review 不是已批准任务。逐项对照本次失败验收条款："
                        "属于原目标内必要修复的，映射到原验收条款并纳入最小修复/复验方案，"
                        "不要因缺少收益评分拒绝；无关扩展不纳入，解释舍弃原因。"
                        "优先在原负责节点修复；若更换所有者须合法移交路径，禁止产生依赖失败父节点的死锁。"
                        "当根因指向其他节点拥有的文件时：若该所有者状态为 completed/superseded/failed/cancelled（非活跃），"
                        "必须新增持有相应 owned_paths 的修复节点（无需 recovery_of，非活跃所有者不构成所有权冲突），"
                        "并把它设为失败节点或其接替者的依赖；禁止要求失败节点在自己的职责内绕过上游缺陷"
                        "（例如注入数据、改写证据、重复同一方案）。"
                        "若同一根因与同一方案已在谱系中重复出现，必须改变方案而不是再派生同形节点。"
                        "反证处理（fix 15，2026-09-13 实盘审计）：当 failed_node_report 或谱系回执中已核实的文件事实"
                        "与 verdict/原 goal 的字面前提矛盾（如 verdict 声称存在的字段实际缺失、同义字段以其他名称存在"
                        "且持有有效值、前提被文件内容反证），禁止再生成要求『证明已被反证的字面前提』的恢复目标；"
                        "必须以实际文件事实为准重构 goal：或将证据规范化（要求回执显式包含 HTTP 状态字段并关联真实"
                        "请求记录），或修正报告引用与字段表述。字段缺失不等于字段为 null，goal 措辞必须区分二者，"
                        "只使用文件中实际存在的字段名。连续派生仅重复只读验证同一文件、不解决前提争议的节点属于同形节点，禁止。"
                        "当 next_actions_inherited_from 出现时，说明本代 Worker 未提交结构化回执，"
                        "next_actions 继承自谱系祖先的原始失败回执，其根因声明与本代回执具有同等路由效力。"
                        "lineage_already_tried 列出该谱系已尝试过的恢复方案，禁止再派生与其中任何一项同形的节点。"
                        "新增或更新节点的 acceptance_criteria 优先使用可判定叶词法（P6-A）："
                        "file:<路径> exists|non-empty、file_written:<路径>、tests_passed:<确切命令>、"
                        + EXECUTION_CRITERION_GUIDANCE +
                        "report_not_stale:<新产物路径> differs_from:<旧产物或归档路径>、sha_matches:<路径> sha256:<期望值>；"
                        "语义条件仅少量自然语言补充。接管被作废产物的节点必须带 report_not_stale 判据。"
                        "tests_passed 判据的命令必须让 stdout 打印可识别测试摘要（"
                        + SUMMARY_EVIDENCE_FORMS +
                        "）；只靠 exit code、stdout 为空的检查不构成测试证据。"
                        "修复/核验类节点必须限定为消费既有索引、脚本输出与已记录收据的一次性确定性核验（P6-E）；"
                        "禁止派生全网格重派生节点或反复重跑已记录比对的同形节点。"
                        "lineage_cap_exemption_recipe 非空时是谱系深度上限的豁免配方，必须遵守："
                        "新增的修复/取证节点必须在 owned_paths（或 expected_outputs）中逐字声明配方列出的 "
                        "root_cause_files 完整相对路径，仅在 goal 文本中描述不构成声明，补丁会被谱系 cap 拦截；"
                        "配方中的所有者均为非活跃节点（completed/superseded/failed），声明这些路径不构成所有权冲突。"
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        parsed = _parse_governor_json(response)
        if not isinstance(parsed, dict):
            raise ValueError("verification recovery planner returned invalid JSON")
        from .patch_fields import model_patch_fields
        parsed = model_patch_fields(parsed)
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
            if any(not isinstance(raw_edge.get(key), str) for key in ("source", "target", "edge_type")):
                malformed_edge_count += 1
                continue
            source = raw_edge["source"].strip()
            target = raw_edge["target"].strip()
            edge_type = raw_edge["edge_type"].strip()
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
        deferred: List[Dict[str, Any]] = []
        parent = self.task_graph.nodes[parent_node_id]
        # Deterministic pre-checks mirror apply_task_graph_patch invariants so
        # one bad proposal cannot blow up the whole admission patch:
        # - dependencies must reference existing graph nodes (unknown ids such
        #   as "N-002-R5-fix" are dropped, falling back to the parent);
        # - owned_paths must not overlap an ACTIVE owner (completed/failed
        #   owners are inactive, so upstream repair proposals against them
        #   remain admissible — the authoritative gate still re-validates).
        active_statuses = {
            "admitted",
            "waiting_dependencies",
            "ready",
            "running",
            "result_submitted",
            "verifying",
            "repair_pending",
            "blocked",
        }

        admissible_roles = {
            "coder",
            "tester",
            "synthesizer",
            "architect",
            "governed_worker",
        }
        for raw in proposals:
            if not isinstance(raw, dict):
                rejected.append({"goal": "", "reason": "proposal_not_object"})
                continue
            if raw.get("metadata") is not None and not isinstance(raw["metadata"], dict):
                rejected.append({"goal": str(raw.get("goal") or ""), "reason": "metadata_not_object"})
                continue
            # Workers phrase discovered work as goal or target interchangeably
            # (the runtime envelope parser emits both); accepting only "goal"
            # previously rejected every envelope-shaped proposal as
            # "missing_goal" before value admission ever saw it.
            goal = str(
                raw.get("goal")
                or raw.get("target")
                or raw.get("description")
                or raw.get("summary")
                or ""
            ).strip()
            if not goal:
                rejected.append({"goal": "", "reason": "missing_goal"})
                continue
            # next_actions' public schema does not ask Workers to invent value
            # scores. Before acceptance, route these suggestions to verification
            # recovery instead of deterministically rejecting defaults (~0.45).
            if parent.status != "completed" and not any(
                key in raw for key in ("information_gain", "risk_reduction", "parallel_benefit")
            ):
                candidate: Dict[str, Any] = {"goal": goal[:4000]}
                for key in ("role", "reason", "owned_paths", "dependencies", "acceptance_criteria"):
                    if key in raw:
                        value = raw[key]
                        candidate[key] = value[:16] if isinstance(value, list) else str(value)[:2000]
                if candidate not in deferred:
                    deferred.append(candidate)
                continue
            try:
                proposed_paths = [declared_relative(item, root=getattr(self.trace_recorder, "run_dir", None))
                                  for item in strings_from(raw.get("owned_paths") or [])]
            except PathIdentityError as error:
                rejected.append({"goal": goal, "reason": str(error)})
                continue
            conflict_owner = ""
            if proposed_paths:
                for existing in self.task_graph.nodes.values():
                    if existing.status not in active_statuses:
                        continue
                    existing_paths = {identity_key(p) for p in existing.owned_paths if str(p).strip()}
                    if any(
                        paths_overlap(a, b)
                        for a in proposed_paths
                        for b in existing_paths
                    ):
                        conflict_owner = existing.node_id
                        break
            if conflict_owner:
                rejected.append({
                    "goal": goal,
                    "reason": (
                        "ownership_conflict_active:"
                        f"{conflict_owner}"
                    ),
                })
                continue
            known_deps = [
                str(item).strip()
                for item in (raw.get("dependencies") or [])
                if str(item).strip() and str(item).strip() in self.task_graph.nodes
            ]
            capabilities = strings_from(
                raw.get("required_capabilities") or raw.get("capabilities") or []
            )
            try:
                proposal = TaskProposal(
                    goal=goal,
                    required_capabilities=capabilities,
                    expected_information_gain=float(raw.get("information_gain", 0.5) or 0.5),
                    risk_reduction=float(raw.get("risk_reduction", 0.3) or 0.3),
                    parallel_benefit=float(raw.get("parallel_benefit", 0.4) or 0.4),
                    estimated_cost=max(1, int(raw.get("estimated_cost", 1000) or 1000)),
                    dependencies=known_deps or [parent_node_id],
                    parent_task_id=parent_node_id,
                )
            except (ValueError, TypeError, OverflowError) as error:
                rejected.append({"goal": goal, "reason": f"invalid_proposal_fields: {error}"})
                continue
            decision = self.evaluate_task_proposal(proposal)
            if not decision.admitted:
                rejected.append({"goal": goal, "reason": decision.reasoning})
                continue
            role = str(raw.get("role") or "").strip()
            node_dict: Dict[str, Any] = {
                "goal": goal,
                "role": role if role in admissible_roles else "governed_worker",
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
            # Carry the declared write contract so an upstream repair proposal
            # gains authority over the root-cause files; apply_task_graph_patch
            # re-validates ownership, identity, cycles, and duplicates.
            if proposed_paths:
                node_dict["owned_paths"] = proposed_paths
            # 起 A (doc b26 C1, Worker-discovery entry point): ``strings_from``
            # used to flatten every declaration to ``str(item)``.  A structured
            # conditional declaration (``required: False`` / ``source:
            # "optional"``) then became its own repr, the ownership gate read a
            # bogus path out of that repr and refused the whole proposal
            # ("declared output is outside owned_paths: … {'output_id': …}"),
            # while a bare prose sentence still survived only by accident.
            # Structure is preserved here; the commit-time gate
            # (``declared_output_items``) re-normalizes prose sentences.
            raw_outputs = raw.get("expected_outputs") or []
            if isinstance(raw_outputs, str):
                raw_outputs = [raw_outputs]
            expected_outputs: List[Any] = []
            for item in (raw_outputs if isinstance(raw_outputs, list) else []):
                if isinstance(item, dict):
                    expected_outputs.append(dict(item))
                elif str(item).strip():
                    normalized = normalize_declared_output(item)
                    expected_outputs.append(
                        normalized
                        if isinstance(normalized, dict)
                        else str(item).strip().replace("\\", "/")
                    )
            if expected_outputs:
                node_dict["expected_outputs"] = expected_outputs
            for field in ("input_artifacts", "output_artifacts", "handoff_contract", "delivery_output_ids", "delivery_criterion_ids"):
                if field in raw:
                    node_dict[field] = raw[field]
            preview_patch = TaskGraphPatch(add_nodes=[*patch.add_nodes, node_dict],
                                       reason_codes=list(patch.reason_codes), reasoning=patch.reasoning)
            try:
                GraphAdmissionService().preview(self, preview_patch, parent_task_id=parent_node_id)
            except (ValueError, PathIdentityError) as error:
                rejected.append({"goal": goal, "reason": str(error)})
                continue
            patch.add_nodes.append(node_dict)
        applied = self.apply_task_graph_patch(patch, parent_task_id=parent_node_id)
        applied["rejected_proposals"] = rejected
        # This records proposals only. It grants neither write authority nor
        # a pass, and apply_task_graph_patch remains the sole admission boundary.
        parent.metadata["repair_candidates_pending_review"] = deferred[:8]
        applied["deferred_proposals"] = deferred[:8]
        self.trace_recorder.record(
            "governor.worker_subtasks.admitted",
            {
                "parent_node_id": parent_node_id,
                "proposal_count": len(proposals),
                "admitted_count": len(applied.get("added", [])),
                "rejected_count": len(rejected),
                "deferred_count": len(deferred),
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
        from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
        from AstralNexus.tools.registry import registry, discover_builtin_tools
        discover_builtin_tools()
        known_tools = set(registry.get_all_tool_names())
        known_resources = set(self.resource_ids_provider()) if self.resource_ids_provider is not None else set()
        self.trace_recorder.record("governor.recovery_catalog", {"tools": sorted(known_tools), "resources": sorted(known_resources)})
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
        # Repeat-aware enforcement (live r11 resume #10, user directive
        # 2026-09-12): N-002-R8-RECOVERY died three identical deaths — this
        # planner kept accepting a bare ``retry`` for repeat counts 2 and 3
        # of the same failure fingerprint because the system prompt only
        # ASKS for information gain while nothing in validation consumed
        # the repetition evidence already present in the fault payload.
        # From the second identical failure onward an unchanged-shape
        # re-dispatch is not recovery: it is rejected with a teaching
        # reason, the planner gets one correction round, and a persistently
        # identical plan escalates to invalid_proposal → graph-level replan
        # instead of yet another clone attempt.
        raw_payload = fault.get("payload")
        payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        repeat_count = int(payload.get("repeat_count") or 0)
        fingerprint = str(payload.get("failure_fingerprint") or "")
        selection = payload.get("worker_selection")
        selected: Dict[str, Any] = {}
        if isinstance(selection, dict):
            candidate = selection.get("selected")
            if isinstance(candidate, dict):
                selected = candidate
        current_worker = str(
            payload.get("approved_worker_type") or selected.get("worker_type") or ""
        )
        current_tier = str(selected.get("resource_tier") or "")
        repeat_evidence = {
            "failure_fingerprint": fingerprint,
            "repeat_count": repeat_count,
            "current_worker_type": current_worker,
            "current_resource_tier": current_tier,
            "identical_redispatch_is_invalid": repeat_count >= 2,
        }
        system_content = (
            "你是天枢 AstralNexus Global Governor 的故障恢复决策器。"
            "只输出 JSON，不执行恢复。根据故障、影响范围和当前任务图选择最小影响动作。"
            "禁止固定三次重试规则；必须依据错误指纹、已有尝试和信息增益判断。"
            "不得引用不存在的任务节点，不得将失败直接标记为完成。"
            "先读取last_failure和worker_selection诊断；已失败的相同Worker/资源分配不算恢复。"
            "能力缺口应选择满足能力边界的已注册Worker，或重规划拆分不相容职责。"
            "输出 {strategy, reasoning, actions}，actions 仅可使用给定动作类型。"
            "当 repeat_evidence.repeat_count≥2 时，retry 与未改变 worker_type/resource_tier 的 "
            "reassign 均属无效恢复，将被拒绝；必须更换 worker_type、更换 resource_tier、"
            "switch_tool 或 replan 改变节点契约形状。"
        )
        user_payload = {
            "fault": fault,
            "impact": impact,
            "repeat_evidence": repeat_evidence,
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
            "registered_tools": sorted(known_tools),
            "registered_resources": sorted(known_resources),
            "worker_catalogue": [
                {"worker_type": profile.worker_type,
                 "capabilities": profile.capabilities, "tools": profile.tools}
                for profile in HETEROGENEOUS_WORKERS.values()
            ],
        }
        violation = ""
        plan_actions: List[Dict[str, Any]] = []
        parsed_strategy = ""
        parsed_reasoning = ""
        for correction_round in range(2):
            messages: List[Dict[str, str]] = [
                {"role": "system", "content": system_content},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ]
            if violation:
                messages.append({
                    "role": "user",
                    "content": (
                        "上一轮恢复提案被拒绝：" + violation +
                        " 请针对该拒绝原因重新决策，输出完整的 {strategy, reasoning, actions}。"
                    ),
                })
            response = self.llm_client.chat(messages)
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
                    if worker_type and worker_type not in HETEROGENEOUS_WORKERS:
                        raise ValueError(f"Governor selected unregistered Worker: {worker_type}")
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
                if action_type == "switch_tool" and (
                        target not in known_tools or str(details.get("alternative_tool") or "") not in known_tools):
                    raise ValueError("switch_tool must name registered source and alternative tools")
                if action_type == "switch_resource" and (
                        target not in known_resources or str(details.get("alternative_resource") or "") not in known_resources):
                    raise ValueError("switch_resource must name registered source and alternative resources")
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
            violation = self._identical_shape_recovery_violation(
                actions,
                repeat_count=repeat_count,
                fingerprint=fingerprint,
                current_worker=current_worker,
                current_tier=current_tier,
            )
            if not violation:
                plan_actions = actions
                parsed_strategy = str(parsed.get("strategy") or "governed_recovery")
                parsed_reasoning = str(parsed.get("reasoning") or "")
                break
            self.trace_recorder.record(
                "governor.fault_recovery.identical_shape_rejected",
                {
                    "fault_id": fault.get("fault_id"),
                    "round": correction_round + 1,
                    "repeat_count": repeat_count,
                    "fingerprint": fingerprint,
                    "violation": violation[:600],
                    "actions": [item["type"] for item in actions],
                },
            )
        if not plan_actions:
            raise ValueError(
                violation or "Governor fault recovery returned no executable actions"
            )
        plan = {
            "strategy": parsed_strategy,
            "reasoning": parsed_reasoning,
            "actions": plan_actions,
        }
        self.trace_recorder.record(
            "governor.fault_recovery.planned",
            {
                "fault_id": fault.get("fault_id"),
                "fault_type": fault.get("fault_type"),
                "strategy": plan["strategy"],
                "action_count": len(plan_actions),
                "actions": [item["type"] for item in plan_actions],
                "repeat_count": repeat_count,
            },
        )
        return plan

    def _identical_shape_recovery_violation(
        self,
        actions: List[Dict[str, Any]],
        *,
        repeat_count: int,
        fingerprint: str,
        current_worker: str,
        current_tier: str,
    ) -> str:
        """Reject unchanged-shape re-dispatch once a fingerprint repeated.

        Returns the teaching violation text, or an empty string when the
        plan carries real information gain.  Only ``retry`` and
        shape-preserving ``reassign`` are policed: ``replan``,
        ``switch_tool``, ``switch_resource`` and ``restore_facts`` always
        change the execution shape, and a ``reassign`` that names a
        DIFFERENT registered worker_type or resource_tier is exactly the
        strategy change the repetition demands.
        """
        if repeat_count < 2 or not fingerprint:
            return ""
        for action in actions:
            if action.get("type") == "retry":
                return (
                    f"retry 对 {action.get('target')} 不构成有效恢复：失败指纹 "
                    f"{fingerprint!r} 已重复 {repeat_count} 次，同型再派发会以相同方式死亡。"
                    "必须改变执行形状：reassign 且指定与 "
                    f"{current_worker or '(当前)'} 不同的 worker_type 或与 "
                    f"{current_tier or '(当前)'} 不同的 resource_tier，或 switch_tool，"
                    "或 replan 改变节点契约。"
                )
            if action.get("type") == "reassign":
                details = action.get("details") or {}
                new_worker = str(details.get("worker_type") or "")
                new_tier = str(details.get("resource_tier") or "")
                worker_changed = bool(new_worker) and new_worker != current_worker
                tier_changed = bool(new_tier) and new_tier != current_tier
                if not (worker_changed or tier_changed):
                    return (
                        f"reassign 对 {action.get('target')} 保持失败执行形状"
                        f"（worker_type={new_worker or current_worker or '未变'}, "
                        f"resource_tier={new_tier or current_tier or '未变'}），"
                        f"而同一失败指纹已重复 {repeat_count} 次。"
                        "选择不同的已注册 worker_type、不同的 resource_tier，"
                        "或改用 replan/switch_tool。"
                    )
        return ""

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
不得发明用户未要求的输出通道限制：unittest 的成功摘要正常写到 stderr，测试通过应检查真实执行摘要而不是强制 stdout 含 OK。
普通 print 命令末尾换行是终端格式，不是额外业务内容。运行目录与发布工作区是不同根：运行时使用声明产物路径，发布后用户命令按发布位置执行。
执行收据是框架账本对象，不得把 execution_receipt:xxx 或任意逻辑收据名声明成必须落盘的业务文件。
summary_required 不是默认收尾阶段。仅当用户明确需要额外的综合报告/汇总交付物时才设为 true；只交付文件或应用时设为 false，正常回复中的完成说明不需要新增 Synthesizer 节点。
不得追加用户未要求的 synthesis_manifest.json，也不得在业务节点和全局验证已通过且前提未变时，派发汇总节点重新执行同一组功能检查。发布器自身仍会生成可核验的发布清单。
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

证据边界（Fix J，doc 18 attempt8）：验收判据只能针对任务环境内可复现的交付物与运行输出。
不得生成要求验证外部系统、真实设备、生产环境等本环境不可达对象的判据；
不得把"标注未验证/说明限制"这一行为本身设为验收判据（attempt7 死锁根因：
该类判据在零 unknown 闸门下永不可满足）。环境限制与外部依赖只写入
assumptions 或 uncertainties，不进入 global_acceptance_criteria。

硬约束只来自用户明确要求，不得另造“必须写在当前工作目录根”“禁止任何命名空间目录”
等位置限制，也不得凭空要求证明解释器、标准库和测试工具的全部文件 I/O 都局限在
交付目录。交付物位置由 required_outputs.path 的结构化声明唯一决定；运行时的
源码分区和框架验收元数据不是额外业务功能。用户若明确要求平铺根目录，必须在路径
声明与约束中保持一致，不能同时声明 app/main.py 又要求 main.py 位于包根。
测试执行证据区分 stdout 与 stderr：unittest 的真实结果通常在 stderr，不得要求
stdout 必须含 OK 或用零测试的 OK 证明非零测试覆盖。

文件、软件、数据类必需交付物必须在 path 写出任务点名的确切相对路径：交付门按这些
声明路径判定产物覆盖，并检查 required=true 的明确文件或目录是否真实存在。
纯文本任务可以不声明文件；明确必需文件不能以零产出通过。可选文件缺失不阻断。
路径覆盖只决定是否可以准备最终交付；GlobalGovernor.verify_delivery 对待发布快照、完整冻结要求与独立运行证据做最终验收，未知判定不能授权发布。

任务：
{task_content}
环境摘要：{environment_summary}
权限限制：{permission_constraints}
既有契约：{json.dumps(previous_contract, ensure_ascii=False) if previous_contract else "null"}
JSON 字段：
{{
  "delivery_mode": "model_decided",
  "delivery_strategy": "artifact_first|text_first|hybrid|model_decided",
  "summary_required": false,
  "execution_evidence_policy": "plan_only|runtime_required|mixed",
  "external_evidence_policy": "required_if_explicit|optional_when_unavailable",
  "required_outputs": [{{"output_id":"O-01","name":"由任务语义决定的交付物","type":"model_decided|text|file|software|data|other","required":true,"source":"explicit|inferred|optional","path":"file/software/data 类必需交付物：任务点名的确切相对路径（相对交付根），例如 runs/defects.json；文本类交付物没有固定文件时留空字符串","acceptance_criteria":[]}}],
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
            contract_response = client.chat([
                {"role": "system", "content": "你是通用任务契约分析器，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ], response_format="json")
            self.trace_recorder.record("governor.delivery_contract.response", {
                "content": contract_response,
                "request_stats": getattr(client, "last_request_stats", {}),
            })
            contract = _parse_governor_json(contract_response)
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
            raise ContextGovernanceUnavailable("GlobalGovernor delivery contract response was not a JSON object")
        def strings(key: str) -> List[str]:
            value = contract.get(key, [])
            return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        def flag(key: str, default: bool) -> bool:
            value = contract.get(key, default)
            return value.strip().lower() not in {"false", "no", "optional", "conditional", "0", ""} if isinstance(value, str) else bool(value)
        outputs = []
        for index, item in enumerate(contract.get("required_outputs", []), 1):
            if isinstance(item, dict):
                output_id = str(item.get("output_id") or f"O-{index:02d}")
                criteria = strings_from(item.get("acceptance_criteria", []))
                # 起 A: ``bool("false")`` is True — decide the flag from its
                # declared value instead of from truthiness.
                required_flag = item.get("required", True)
                if isinstance(required_flag, str):
                    required_flag = required_flag.strip().lower() not in {
                        "false", "no", "optional", "conditional", "0",
                    }
                else:
                    required_flag = bool(required_flag)
                entry = {
                    "output_id": output_id,
                    "name": str(item.get("name") or "required deliverable"),
                    "type": str(item.get("type") or "other"),
                    "required": required_flag,
                    "source": str(item.get("source") or "explicit"),
                    "acceptance_criteria": criteria,
                    "acceptance_criterion_ids": [
                        criterion_id(output_id, criterion_index)
                        for criterion_index in range(1, len(criteria) + 1)
                    ],
                }
                # 起 A: ``path`` and ``condition`` are part of the declaration.
                # Dropping them here made downstream consumers re-derive the
                # path from the output id and lose conditionality entirely.
                declared_path = str(item.get("path") or item.get("file") or item.get("artifact_path") or item.get("location") or "").strip()
                if declared_path:
                    entry["path"] = normalize_worker_output_path(declared_path)
                if item.get("condition") not in (None, "", {}, []):
                    entry["condition"] = item.get("condition")
                outputs.append(entry)
            elif str(item).strip():
                outputs.append({
                    "output_id": f"O-{index:02d}", "name": str(item).strip(),
                    "type": "other", "required": True, "source": "explicit",
                    "acceptance_criteria": [],
                    "acceptance_criterion_ids": [],
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
            summary_required=flag("summary_required", False),
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
            needs_clarification=flag("needs_clarification", False),
            needs_citations=flag("needs_citations", False),
            needs_artifact_refs=flag("needs_artifact_refs", False),
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
        external_policy = str(contract.get("external_evidence_policy") or "required_if_explicit").strip().lower()
        if external_policy not in {"required_if_explicit", "optional_when_unavailable"}:
            raise ContextGovernanceUnavailable(f"invalid external_evidence_policy: {external_policy}")
        delivery_contract.external_evidence_policy = external_policy
        self.delivery_contract["external_evidence_policy"] = external_policy
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
        cancel_check: Optional[Callable[[], bool]] = None,
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
        # F11/F14 (live b31): two facts the planner must not invent.  (1) A frozen
        # clause that names a concrete file no node owns is a gap that must be
        # given an owner or downgraded to an interface constraint — never
        # "satisfied" by an empty shim created by a later repair round.  (2) Cited
        # paths are refreshed from disk and recent evidence, because the final b31
        # replan asserted that the delivered entry file "does not exist on disk"
        # while it had existed and been verified for seventeen minutes, and the
        # shim-only patch that followed consumed the last recovery-budget event.
        premise_paths = self._concrete_file_tokens(goal)
        premises = self._refresh_path_premises(premise_paths)
        if premises:
            goal_context += (
                "\n磁盘与近期证据的现况（以此为准；source=unverified 的路径不得断言“不存在”）：\n"
                + json.dumps(premises, ensure_ascii=False)
            )
        if self.contract_clause_gaps:
            goal_context += (
                "\n点名了具体文件但没有任何节点 owned_paths 覆盖的结构化声明或冻结契约条款（必须在本 "
                "Patch 中为其指定所有者，或把该条款降级为接口约束并说明理由）：\n"
                + json.dumps(self.contract_clause_gaps, ensure_ascii=False)
            )
        from AstralNexus.runtime.development_playbook import fullstack_playbook
        development_route = fullstack_playbook(goal)
        prompt = f"""你是 Governor 控制平面的全局任务图规划器，只输出 JSON。
{development_route}
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

执行宿主环境：{platform.system()}，Python {platform.python_version()}。
规划启动入口和验证命令时必须适配此宿主。若交付要求包含其他平台脚本，保留该交付要求，
同时给负责节点分配宿主可执行的启动/验证入口及其 owned_paths；不得假设 Bash 等额外环境已经存在。

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
13. 每个实现节点必须声明 owned_paths（它唯一可以写入的目录或文件命名空间）、
    input_artifacts、output_artifacts 和 handoff_contract。不同节点的 owned_paths/output_artifacts
    不得重叠；后端 API、前端、数据库、测试和文档必须按真实写入边界拆开。若是恢复节点，
    必须填写 recovery_of=失败节点ID，明确接管关系；不得用新节点重复占用同一产物路径。
    owned_paths 可以是精确文件路径或独占叶级目录。用户明确指定文件路径时必须原样保留，
    可让两个节点分别拥有 deliverables/result.json 与 deliverables/check.md，不要改写用户路径。
    禁止多个节点声明共享聚合目录，例如不得让
    “端到端测试”和“安全验证”同时拥有 deliverables/runs/；应分别使用
    deliverables/runs/e2e/ 与 deliverables/runs/security/。同理，拥有 deliverables/app/
    的节点不能再与拥有 deliverables/app/backend/ 的节点并存。output_artifacts 使用全图唯一的
    稳定 Artifact ID，不要把父目录同时写入 owned_paths 与 output_artifacts。
    同一个可变数据库/共享状态只能有一个生产与初始化负责人。后端、种子脚本和数据库初始化
    若无法独立拥有写入路径，应合为一个可运行组件实现节点；独立测试在自己的目录复制测试数据库。
    使用方把已有产物放入 input_artifacts 并依赖生产者，不能再次列入 output_artifacts。
    同一个可运行组件或源码命名空间只能有一个集成所有者。仅负责需求、架构、Schema、API
    契约或设计说明的节点只能拥有独立 spec/docs 路径，不能同时占用该组件源码目录。
    若两个节点使用同一语言、运行时和组件目录且无法给出互不包含的叶级源码边界，应合并为
    一个实现节点；不得生成两个“后端实现”、两个“前端实现”或其他重复组件所有者。
14. 每个新增节点必须用 delivery_output_ids 声明它负责交付契约中的哪些 output_id。
    首次执行图的所有节点合计必须覆盖全部 required_outputs；同一 output_id 可由实现、测试、
    文档等多个节点共同覆盖，但不得虚构契约中不存在的 ID。重规划只能修正被拒绝字段，
    不得删除原 Patch 已覆盖的 output_id 或工作单元。
15. 若 required_outputs 含 acceptance_criterion_ids，每个节点还必须用
    delivery_criterion_ids 精确声明其负责的验收条款。首次执行图必须覆盖每个顶层 output_id，
    默认必须覆盖全部必需验收条款，不得自行认定某条款属于可延后细节。每条款必须分配给
    能完成它的节点；不得只填写顶层 output_id 或把条款挂给无关节点来掩盖缺失的实现、测试、运行或文档边界。
16. acceptance_criteria 必须优先写成代码可直接判定的判据叶（每节点不超过 12 条，只列真正的门槛条件）。
    可用判据叶（P6，词法固定、大小写不敏感）：
    "file:<相对路径> exists"、"file:<相对路径> non-empty"、"file_written:<相对路径>"（存在且可读回）；
    "tests_passed:<一条确切测试命令>"——锚定 worker 本次真实执行记录与输出中的测试摘要，父侧不重跑命令。
    {EXECUTION_CRITERION_GUIDANCE}
    该摘要必须是验证器可识别的形状，否则该条款**不可能满足**（会强制 inconclusive 并转入重规划）：
    {SUMMARY_EVIDENCE_FORMS}。只写 exit code、stdout 为空的检查命令（例如只调用 sys.exit(0/1) 的
    python -c）不构成测试证据，必须让脚本成功时打印其中之一。
    "report_not_stale:<新产物路径> differs_from:<旧版或归档路径>"——防止把被作废的旧产物字节原样重交；
    "sha_matches:<路径> sha256:<期望值>"——仅当期望值有可靠出处时使用。
    路径相对交付根（run 目录）。代码判不了的语义条件（一致性、质量、覆盖度）仍用自然语言列出，
    交给残差判官一次性判定，但条数不得超过判据叶。禁止用"文档清晰""结果正确"之类无法核查的
    空话凑数；判不了的项如实留白好过伪造可判性。
17. 禁止设立"对全部交付物做全网格交叉验证"的常设法证/审计节点（P6-E，r14 实证：此类节点
    四代返工共烧 12+ 小时）。一致性核验的唯一合法形态：一个确定性脚本作为交付物**跑一次**，
    输出机器可读结果 + 摘要；只有脚本判出的红灯才允许升级为针对性调查节点，且该节点的 goal
    必须限定"消费脚本输出定位具体红灯"，禁止全量重派生或重复执行脚本已覆盖的比对。
    验收判定优先引用 worker 本次执行已产生的真实记录（收据、日志、junit 输出），不重复执行。
18. 冻结契约/需求正文里点名的每个具体文件名（例如 *.py、*.md、*.sqlite、*.json）必须由某个节点的
    owned_paths 覆盖。若某个点名文件无人拥有，必须在本 Patch 中给它指定所有者；确属无法交付的，必须
    把该条款降级为接口约束（端口、端点、字段、枚举、选择器）并在 derivation 中说明。禁止为了满足
    散文条款而新建空壳入口文件或空壳节点，不得让点名文件成为无人拥有的孤儿。此类条款会以
    governor.contract.clause_unanchored 记录，禁止忽略。**普通文档（正文提及、同名后缀、本次运行写出
    的 md/rst/txt）只是提示，不产生交付义务**；只有结构化声明（expected_outputs、required_outputs、
    handoff_contract）点名的路径才必须由某个节点的 owned_paths 覆盖。
19. 规划前必须核对下方"磁盘与近期证据的现况"。任何"某文件不存在"的结论只能来自当次磁盘核对；
    source=unverified 的路径不得断言不存在，也不得据此新建替代节点。恢复与重规划必须基于当前
    真实状态，不得沿用历史叙述中已经过期的判断。

输出 schema：
{{"candidate_mode":"merge|replace","add_nodes":[{{"node_id":"patch-local-id-1","goal":"...","task_type":"analysis|implementation|verification|other",
"required_capabilities":["..."],"dependencies":["已有节点ID"],"acceptance_criteria":["file_written:deliverables/x.json","tests_passed:pytest tests/ -q","report_not_stale:... differs_from:...","自然语言语义条件(少量)"],
"source_requirements":["用户原文或交付契约中的直接依据"],"derivation":"公开的任务拆分说明",
"delivery_output_ids":["O-01"],
"delivery_criterion_ids":["O-01.C01"],
"expected_outputs":[],"owned_paths":[],"input_artifacts":[],"output_artifacts":[],
"handoff_contract":{{"schema":"...","required_fields":[],"acceptance":[]}},"recovery_of":"",
"priority":50,"criticality":"low|medium|high","execution_target":"local|edge|cloud",
"budget":{{"max_tokens":0,"max_seconds":0,"max_tool_calls":0}},
"background_candidates":[{{"goal":"可选的独立后台核验目标","role":"仅作能力提示",
"required_capabilities":["..."],"expected_information_gain":0.0,"risk_reduction":0.0,
"parallel_benefit":0.0,"estimated_cost":0}}]}}],
"update_nodes":[{{"node_id":"consumer","dependencies":["replacement_node_id"],"metadata":{{"reason":"dependency_repair"}}}}],
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
                    cancel_check=cancel_check,
                )
            else:
                response = client.chat(messages)
            parsed = _parse_governor_json(response)
            if isinstance(parsed, dict):
                from .patch_fields import model_patch_fields
                parsed = model_patch_fields(parsed)
        except InterruptionRequested:
            raise
        except Exception as exc:
            self.trace_recorder.record(EventType.GRAPH_PATCH_FAILED, {"error": str(exc)[:500]})
            return TaskGraphPatch(reason_codes=["PLANNING_FAILED"], reasoning=str(exc)[:500])
        if not isinstance(parsed, dict):
            return TaskGraphPatch(reason_codes=["INVALID_PLAN"], reasoning="planner output was not an object")
        nodes = []
        from AstralNexus.runtime.task_execution_budget import ensure_task_budget
        for item in parsed.get("add_nodes", []):
            if not isinstance(item, dict) or not str(item.get("goal", "")).strip():
                continue
            nodes.append({
                # Preserve the planner's patch-local ID so dependencies and
                # add_edges can be remapped to the committed dynamic ID.
                "node_id": str(item.get("node_id") or item.get("task_id") or "").strip(),
                "goal": str(item["goal"]).strip(),
                "role": str(item.get("role") or "governed_worker").strip(),
                "task_type": str(item.get("task_type") or "other"),
                "required_capabilities": [str(x) for x in item.get("required_capabilities", []) if str(x).strip()],
                "dependencies": [str(x) for x in item.get("dependencies", []) if str(x).strip()],
                "acceptance_criteria": strings_from(item.get("acceptance_criteria", [])),
                "source_requirements": strings_from(item.get("source_requirements", [])),
                "derivation": str(item.get("derivation") or "").strip(),
                "delivery_output_ids": strings_from(item.get("delivery_output_ids", [])),
                "delivery_criterion_ids": strings_from(item.get("delivery_criterion_ids", [])),
                "expected_outputs": item.get("expected_outputs", []) if isinstance(item.get("expected_outputs", []), list) else [],
                "owned_paths": item.get("owned_paths", []) if isinstance(item.get("owned_paths", []), list) else [],
                "input_artifacts": item.get("input_artifacts", []) if isinstance(item.get("input_artifacts", []), list) else [],
                "output_artifacts": item.get("output_artifacts", []) if isinstance(item.get("output_artifacts", []), list) else [],
                "handoff_contract": item.get("handoff_contract", {}) if isinstance(item.get("handoff_contract", {}), dict) else {},
                "recovery_of": str(item.get("recovery_of") or item.get("supersedes") or "").strip(),
                "priority": max(0, min(100, int(item.get("priority", 50)))),
                "criticality": str(item.get("criticality") or "medium"),
                "execution_target": str(item.get("execution_target") or "cloud"),
            # P6-D: fill the default table at parse time so the proposed
            # patch and its trace carry a real budget; apply_task_graph_patch
            # re-normalizes at commit time for every other patch source.
            # F4 (doc 51 §22): a proposed recovery successor is a framework-issued
            # repair round even at parse time; apply_task_graph_patch re-normalizes
            # at commit time for patches that omit recovery_of here (it is often
            # auto-attached by the orchestrator, see orchestrator.py:2564-2610).
            "budget": ensure_task_budget(
                item.get("budget"),
                task_type=str(item.get("task_type") or ""),
                execution_target=str(item.get("execution_target") or "cloud"),
                repair_round=bool(
                    str(item.get("recovery_of") or item.get("supersedes") or "").strip()
                ),
            )[0],
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
            candidate_mode=str(parsed.get("candidate_mode") or "merge").strip().lower(),
            background_candidates=(
                parsed.get("background_candidates", [])
                if isinstance(parsed.get("background_candidates", []), list) else []
            ),
        )
        self.trace_recorder.record(EventType.GRAPH_PATCH_PROPOSED, {
            "add_node_count": len(patch.add_nodes),
            "reason_codes": patch.reason_codes,
            "planning_explanation": patch.reasoning[:2000],
            # F14: the proposed patch carries the path premises it relied on, so
            # an assertion like "this file does not exist" stays auditable
            # against the disk/evidence state the planner was actually given.
            "premises": [
                {
                    "path": item.get("path"),
                    "exists": item.get("exists"),
                    "source": item.get("source"),
                }
                for item in premises[:24]
            ],
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
        cancel_check: Optional[Callable[[], bool]] = None,
        rejected_patch: Optional[TaskGraphPatch] = None,
    ) -> TaskGraphPatch:
        """Regenerate a rejected graph patch without delegating policy to the executor."""
        corrected_goal = (
            f"{goal}\n\n上一版 TaskGraphPatch 未通过原子图校验。"
            f"\n拒绝原因：{str(rejection_reason)[:800]}"
            "\n请只修正导致拒绝的局部规划，保留原目标、硬约束和交付契约。"
            "返回最小、无环的 TaskGraphPatch；依赖只能引用当前图节点或同一 Patch 新节点。"
            "不得返回空 add_nodes，不得用说明文字替代结构化字段。"
        )
        if rejected_patch is not None:
            corrected_goal += (
                "\n被拒绝的完整 Patch 如下。你可以返回完整修正版，也可以只返回需要替换或"
                "新增的节点；默认 candidate_mode=merge，Governor 按 node_id 合并；只有明确返回"
                "candidate_mode=replace 才整版替换。节点数量不会隐式触发替换。必须保留其中"
                "未被拒绝的工作单元、delivery_output_ids、验收边界和交付覆盖，只修改引发拒绝"
                "的字段。删除候选节点时必须显式放入 cancel_nodes：\n"
                f"{json.dumps(rejected_patch.as_dict(), ensure_ascii=False)[:30000]}"
            )
        if any(marker in str(rejection_reason) for marker in (
            "false_parallelism_single_wide_bottleneck", "claims PARALLELIZABLE",
        )):
            corrected_goal += (
                "\n这是首波并行门禁拒绝：禁止新增一个共享前置节点再让全部工作依赖它。"
                "请把至少两个真实独立的工作单元直接挂到当前父节点（或 dependencies 为空，"
                "由 Governor 绑定当前父节点），使它们同时进入首个 Ready-Wave；"
                "架构、实现、数据准备、环境检查等只有在真实产物依赖存在时才串行。"
                "不得把 N-001、patch-local-id-1 或任何新增节点作为所有其他节点的共同前置。"
            )
        if "overlapping output ownership" in str(rejection_reason):
            corrected_goal += (
                "\n这是输出所有权门禁拒绝。优先以 candidate_mode=merge 返回同 ID 冲突节点的修正版，"
                "保留其他节点。只有确实提供全部节点及完整交付覆盖时才使用 candidate_mode=replace。"
                "必须实际修改错误中点名的两个冲突节点，禁止原样返回相同 owned_paths。"
                "对每一对 owned_paths 做 exact/parent-child 前缀检查，确认零冲突后再输出。"
                "请保留任务语义，只重写冲突节点的 owned_paths 或合并重复组件节点："
                "优先将聚合目录缩小为该节点实际交付的精确文件路径；用户指定路径必须保留。"
                "否则使用互不包含的叶级目录，禁止使用会覆盖兄弟节点的聚合目录。"
                "例如测试节点使用 deliverables/runs/e2e/，安全节点使用 "
                "deliverables/runs/security/；后端使用 deliverables/app/backend/，前端使用 "
                "deliverables/app/frontend/。output_artifacts 必须是互不重复的 Artifact ID。"
                "仅生成契约/设计的节点应写入独立 spec/docs 目录，不得与实现节点共同拥有源码目录。"
                "如果两个节点属于同一可运行组件且不能安全拆成叶级模块，必须合并为一个实现节点。"
            )
        if "declared output is outside owned_paths" in str(rejection_reason):
            corrected_goal += "\nexpected_outputs 中每个物理文件必须属于本节点 owned_paths；必须同步修正两者。上游只读文件放 input_artifacts，不要列为本节点写入产物。"
        if "artifact_id:" in str(rejection_reason):
            corrected_goal += (
                "\n此次冲突包括 output_artifacts 的逻辑产物 ID，单改 owned_paths 无法修复。"
                "为错误中的同一产物指定唯一生产者；使用方从 output_artifacts 删除该 ID，"
                "改为 input_artifacts 并在 dependencies 加入生产者。"
                "如果双方都必须写同一数据库或共享状态，应合并为一个组件实现节点，"
                "保留全部交付条款并取消旧候选节点，同时重接消费者依赖。"
                "不同独立文件才可以使用不同 ID，禁止只重命名 ID 掩盖同一路径的多方写入。"
            )
        if "unknown edge endpoint" in str(rejection_reason):
            corrected_goal += (
                "\n边必须使用 source、target、edge_type 字段，方向为生产者到消费者。"
                "返回 candidate_mode=replace 的完整修正补丁，保留原补丁所有有效节点与变更，"
                "以正确字段替换无效边；不要仅追加新边而遗留旧的无效边。"
            )
        if "artifact producer is not a declared dependency" in str(rejection_reason):
            corrected_goal += (
                "\n必须修改消费者节点的 dependencies 数组，加入拒绝原因中的生产者节点 ID。"
                "input_artifacts 和 evidence_for 只声明数据或证据，不能代替调度依赖。"
                "消费者读取生产者文件必须串行；不得为了并行而省略真实依赖。"
            )
        if any(marker in str(rejection_reason) for marker in (
            "does not cover required delivery", "does not cover enough required delivery",
        )):
            corrected_goal += (
                "\n这是交付覆盖门禁拒绝。必须保留上一版全部有效节点，并新增或修订最小工作单元，"
                "使 delivery_output_ids 与 delivery_criterion_ids 覆盖拒绝原因列出的每个 ID。"
                "不得把缺失条款虚挂到语义无关节点；前端、后端、运行验证、测试、文档等不同验收"
                "边界必须由实际能产生对应产物或证据的节点负责。"
            )
        # Keep the current failure last: a large rejected candidate may carry
        # reasoning about an earlier error which has already been repaired.
        focused_nodes = []
        if rejected_patch is not None:
            focused_nodes = [raw for raw in rejected_patch.add_nodes
                             if str(raw.get("node_id") or "")
                             and re.search(r"(?<![\w-])" + re.escape(str(raw["node_id"])) + r"(?![\w-])",
                                           str(rejection_reason))]
        corrected_goal += (
            "\n当前唯一阻塞诊断（以此为准，候选 reasoning 中的历史错误可能已经修复）：\n"
            + str(rejection_reason)
            + "\n本次点名节点：\n" + json.dumps(focused_nodes, ensure_ascii=False)
            + "\n修复必须实际修改当前点名节点及字段，不能只重复提交先前已修好的其他节点。"
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
            cancel_check=cancel_check,
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
        declared_required = set(required)
        # A Governor patch may omit capabilities for a newly spawned node.
        # Never let the free-form ``role`` field become the routing decision:
        # derive a small, ontology-bound capability set from the goal so a
        # node such as "build an API" cannot silently become a Retrieval
        # worker merely because the patch said ``researcher``. This is
        # capability-ontology inference, not a task-domain template.
        goal_text = str(task.get("goal") or task.get("description") or "").strip().lower()
        task_type = str(task.get("task_type") or "").strip().lower()
        explicit_required = bool(required)
        # An explicit semantic task type is authoritative. Architecture and
        # planning goals routinely mention APIs, databases and services, but
        # those nouns do not turn the task into source-code implementation.
        # Without this boundary an Architect selected by the LLM matcher was
        # rejected and the formal graph became blocked before execution.
        non_implementation_task_types = {
            "architect", "architecture", "design", "planner", "planning",
            "analysis", "review", "reviewer", "verification", "verifier",
            "test", "testing", "tester", "synthesis", "synthesizer",
            "document", "documentation", "docs", "technical_writing",
        }
        goal_implementation = (
            contains_marker(goal_text, IMPLEMENTATION_MARKERS)
            and task_type not in non_implementation_task_types
        )
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
                if task_type in non_implementation_task_types:
                    # Shared design vocabulary must keep its architectural
                    # meaning. The Coding aliases for api/database design add
                    # implementation-only tokens for implementation tasks,
                    # but doing so here would invalidate a genuine Architect.
                    alias_tokens = [
                        token for token in alias_tokens
                        if token not in {
                            "code_generation", "api_integration",
                            "dependency_analysis", "implementation",
                        }
                    ]
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

        catalogue_tokens: set[str] = set()
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
            explicit_architecture_intent = bool(
                declared_required
                & {
                    "system_architecture", "requirement_analysis",
                    "database_modeling", "state_machine_design",
                    "permission_design",
                }
            )
            implementation_required = (
                bool(required & implementation_terms)
                and not synthesis_required
                and task_type not in non_implementation_task_types
                and not (
                    not task_type and explicit_architecture_intent
                )
            )
            implementation_fit = 1.0 if implementation_required and capabilities & implementation_terms else 0.0
            implementation_penalty = 0.20 if implementation_required and not (capabilities & implementation_terms) else 0.0
            architecture_terms = {
                "system_architecture", "requirement_analysis",
                "database_modeling", "state_machine_design",
                "permission_design",
            }
            architecture_task_types = {
                "architect", "architecture", "design", "system_design",
                "requirement_analysis",
            }
            # Database/API design vocabulary is shared by architecture and
            # implementation profiles.  For an explicit implementation node,
            # those capabilities improve Coding's score but must not create a
            # second hard Architect boundary that no registered profile can
            # satisfy.  Planner task_type is the authoritative boundary.
            architecture_required = bool(declared_required & architecture_terms) and (
                task_type in architecture_task_types or not implementation_required
            )
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
                "selected_model": forced_model or model_by_preference.get(str(value("model_preference", "")), DEFAULT_MODEL),
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
        requires_file_output = any(
            not str(path).startswith("artifact:")
            and ("/" in str(path) or "\\" in str(path) or bool(Path(str(path)).suffix))
            for path in task.get("expected_outputs", []) or []
        )
        if requires_file_output:
            # File postconditions require an actual materialization tool.
            # Do not ask the Matcher to choose a reasoning-only profile and
            # then repeatedly repair its inevitable narrative-only result.
            candidates = [
                item for item in candidates
                if set(item.get("tools", [])) & {"file_write", "python_exec", "terminal_exec"}
            ]
        # The LLM matcher is only allowed to choose from profiles that have
        # at least one normalized capability match whenever requirements are
        # present.  This prevents a valid partial match from being discarded
        # because the model selected an unrelated profile from the catalogue.
        matched_candidates = [
            item for item in candidates
            if not required or item.get("capability_match", 0) > 0
        ]
        matcher_candidates = matched_candidates or candidates
        if not candidates:
            return {"selected": None, "candidates": [], "required_capabilities": sorted(required),
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "reason_codes": ["NO_CAPABILITY_CANDIDATE"]}
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
        matcher_transient_failure = False
        approved_worker = str(task.get("approved_worker_type") or "").strip()
        if approved_worker and not any(item["worker_type"] == approved_worker for item in candidates):
            raise ValueError(f"Approved recovery Worker is not executable: {approved_worker}")
        # A recovery decision is already a Governor selection. Reuse it
        # through the same capability validator rather than asking the
        # matcher to silently discard it on the next scheduling wave.
        llm_match: Optional[Dict[str, Any]]
        if approved_worker:
            llm_match = {"worker_type": approved_worker}
        else:
            try:
                llm_match = self._llm_worker_match(task, matcher_candidates or candidates)
            except WorkerMatcherTransientError:
                # Transient matcher fault (malformed output/transport),
                # already retried and recorded inside _llm_worker_match.
                # It is not a capability decision: degrade to the
                # deterministic validator below instead of blocking a
                # healthy node (live r13c 2026-09-29T11:35:29Z N-122
                # cascade).  Hallucinations and explicit null decisions
                # never reach this branch.
                matcher_transient_failure = True
                llm_match = None
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
                    if not approved_worker:
                        selected["llm_match_confidence"] = llm_match["confidence"]
                    selected["reason_codes"] = list(dict.fromkeys(
                        selected.get("reason_codes", []) + (
                            ["GOVERNOR_RECOVERY_ASSIGNMENT"] if approved_worker
                            else ["LLM_PROFILE_MATCH"] + llm_match["reason_codes"]
                        )
                    ))
                    semantic_match = not approved_worker and not bool(llm_selected.get("capability_match", 0))
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
        # validated rematch, not a generic-agent fallback.  A transient
        # matcher failure degrades into the same validated rematch: the
        # fault is not a capability verdict (r13c N-122 cascade).
        if selected is None and (matcher_profile_rejected or matcher_transient_failure):
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
                    + (["MATCHER_FAILURE_REMATCH"] if matcher_transient_failure else [])
                ))
                self.trace_recorder.record(EventType.WORKER_SELECTED, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "selected": selected,
                    "candidate_count": len(candidates),
                    "required_capabilities": sorted(required),
                    "selection_source": (
                        "matcher_failure_rematch"
                        if matcher_transient_failure and not matcher_profile_rejected
                        else "capability_validator_rematch"
                    ),
                })
            elif matcher_transient_failure and not required and candidates:
                # No capability requirements to validate against: degrade to
                # the same deterministic preselection used when LLM matching
                # is disabled.  The zero-match gate below still applies.
                selected = (matched_candidates or candidates)[0]
                selected["reason_codes"] = list(dict.fromkeys(
                    selected.get("reason_codes", [])
                    + ["MATCHER_FAILURE_DETERMINISTIC_PRESELECT"]
                ))
                self.trace_recorder.record(EventType.WORKER_SELECTED, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "selected": selected,
                    "candidate_count": len(candidates),
                    "required_capabilities": sorted(required),
                    "selection_source": "matcher_failure_preselect",
                })

        # Round 13 (live r11 resume #20, blocked 21:48): a correct hybrid
        # repair node declared ["backend_development", "python_execution",
        # "api_testing"] (fix main.py AND rerun the four boundary tests —
        # the shape our own cap-exemption recipe encourages).  That fires
        # implementation_required AND testing_required simultaneously; no
        # catalogue profile carries both, so the matcher pick was boundary-
        # rejected and the validator rematch found zero candidates →
        # NO_CAPABILITY_CANDIDATE → the node was blocked without ever
        # executing (and its never-executed fingerprint then poisoned the
        # lineage-repeat guard against every retry).  task_type is the
        # authoritative boundary (same principle as the architecture
        # boundary above): for an implementation task, testing demotes to
        # the scoring penalties candidates already carry — a Coding worker
        # runs curl through terminal_exec fine.  Symmetric for test tasks.
        if selected is None and implementation_required and testing_required:
            primary_terms = (
                testing_terms
                if task_type in {"test", "testing", "tester"}
                else implementation_terms
            )
            hybrid_candidates = [
                item for item in candidates
                if item.get("capability_match", 0) > 0
                and bool(set(item.get("capabilities", [])) & primary_terms)
            ]
            if hybrid_candidates:
                selected = hybrid_candidates[0]
                selected["reason_codes"] = list(dict.fromkeys(
                    selected.get("reason_codes", [])
                    + ["CAPABILITY_VALIDATOR_REMATCH", "HYBRID_BOUNDARY_RELAXED"]
                ))
                self.trace_recorder.record(EventType.WORKER_SELECTED, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "selected": selected,
                    "candidate_count": len(candidates),
                    "required_capabilities": sorted(required),
                    "selection_source": "hybrid_boundary_relax",
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
            from AstralNexus.core.governor_evidence import validate_summary
            evidence_ok, binding_errors = validate_summary(
                self.task_graph, run_dir,
                task_version if task_version is not None else contract.get("task_version", self.goal_version),
                summary if isinstance(summary, dict) else {},
            )
            results.append({
                "group_id": group_id,
                "status": "pass" if schema_ok and evidence_ok else "fail",
                "checks": {"schema": schema_ok, "claim_evidence_binding": evidence_ok},
                "evidence_binding_errors": binding_errors,
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
        # F-C (P4 Round-1): reconcile a blocking evidence gap against evidence
        # another group already produced before the failure escalates.  The
        # P4R1 chain started here: the G1 gap demanded real-server e2e
        # evidence that run1.log already contained, the stale gap failed the
        # verifier, and the replan patch then crashed the turn.  Reconciliation
        # is fully deterministic (regex + substring + graph lookups, no LLM)
        # and conservative by construction: a gap is dropped only when every
        # path literal it demands is present in a file recorded as observed
        # evidence by a DIFFERENT group, and that file either has no graph
        # artifact mapping or maps to accepted, current-version artifacts with
        # live producers.  Partial matches keep the gap (宁可漏对账，不可错对账).
        reconciled_gaps: List[Dict[str, Any]] = []
        if coverage_gaps and run_dir:
            current_task_version = task_version if task_version is not None else self.goal_version
            run_root = Path(str(run_dir))

            from AstralNexus.runtime.evidence_catalog import EvidenceCatalog
            catalog = EvidenceCatalog(run_root, graph=self.task_graph, task_version=current_task_version)

            def _ref_file_text(ref: Any) -> Optional[str]:
                value = catalog.read_text(ref, max_bytes=262144, errors="ignore")
                return value.lower() if value is not None else None

            observed_claims: List[Dict[str, Any]] = []
            for group_id, summary in reducers.items():
                if not isinstance(summary, dict):
                    continue
                for claim in summary.get("verified_claims") or []:
                    if not isinstance(claim, dict):
                        continue
                    status = str(claim.get("epistemic_status") or "").strip().lower()
                    if status == "observed" and claim.get("evidence_refs"):
                        observed_claims.append({
                            "group_id": str(group_id),
                            "claim": claim.get("claim"),
                            "evidence_refs": [str(ref) for ref in claim.get("evidence_refs") or []],
                        })
            for entry in list(coverage_gaps):
                gap_group = str(entry.get("group_id") or "")
                combined = " ".join(
                    str(entry.get(key) or "") for key in ("gap", "evidence_needed")
                ).replace("\\", "/").lower()
                demanded: Set[str] = set()
                for literal in re.findall(
                    r"(?:deliverables|runs|docs)/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*", combined
                ):
                    demanded.add(str(literal))
                for token in re.findall(r"[a-z0-9_\-./]+\.[a-z0-9]{1,10}", combined):
                    token = token.strip("/\\")
                    if token and "." in token and "://" not in token:
                        demanded.add(token)
                if not demanded:
                    continue
                verifiable: List[Dict[str, Any]] = []
                for candidate in observed_claims:
                    if candidate["group_id"] == gap_group:
                        continue
                    for ref in candidate["evidence_refs"]:
                        content = _ref_file_text(ref)
                        if content is None:
                            continue
                        verifiable.append({
                            "group_id": candidate["group_id"],
                            "claim": candidate["claim"],
                            "evidence_ref": str(ref),
                            "content": content,
                        })
                if not verifiable:
                    continue
                matched_paths = sorted(
                    path for path in demanded
                    if any(path in evidence["content"] for evidence in verifiable)
                )
                if len(matched_paths) != len(demanded):
                    if matched_paths:
                        entry["reconciliation_partial"] = {
                            "demanded_paths": sorted(demanded),
                            "matched_paths": matched_paths,
                        }
                    continue
                matched_refs = sorted({str(evidence["evidence_ref"]) for evidence in verifiable})
                claim_refs: Dict[str, Dict[str, Any]] = {}
                for evidence in verifiable:
                    claim_key = f"{evidence['group_id']}:::{evidence['claim']}"
                    holder = claim_refs.setdefault(
                        claim_key,
                        {"group_id": str(evidence["group_id"]), "claim": evidence["claim"], "evidence_refs": []},
                    )
                    if str(evidence["evidence_ref"]) not in holder["evidence_refs"]:
                        holder["evidence_refs"].append(str(evidence["evidence_ref"]))
                matched_claims = [claim_refs[key] for key in sorted(claim_refs)]
                reconciled_entry = dict(entry)
                reconciled_entry["reason_code"] = "GAP_RECONCILED_EXISTING_EVIDENCE"
                reconciled_entry["reconciliation"] = {
                    "demanded_paths": sorted(demanded),
                    "matched_paths": matched_paths,
                    "matched_refs": matched_refs,
                    "matched_claims": matched_claims,
                }
                reconciled_gaps.append(reconciled_entry)
                coverage_gaps.remove(entry)
                self.trace_recorder.record(
                    "governor.global_verifier.gap_reconciled",
                    {
                        "group_id": gap_group,
                        "gap": entry.get("gap"),
                        "demanded_paths": sorted(demanded),
                        "matched_paths": matched_paths,
                        "matched_refs": matched_refs,
                        "matched_claims": matched_claims,
                        "reason_code": "GAP_RECONCILED_EXISTING_EVIDENCE",
                    },
                )
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
            "reconciled_gaps": reconciled_gaps,
            "deferred_gaps": deferred_gaps,
            "optional_gaps": [gap for gap in deferred_gaps if not gap.get("required_for_delivery", False)],
            "failed_tests": [item["group_id"] for item in failures],
            "recommended_recovery_tasks": [],
            "task_version": task_version or self.goal_version,
            "source_artifacts": list(artifact_refs or []),
        }
        # F-B (P4 Round-1): the trace event is the machine-readable contract
        # for downstream consumers (cli gap fingerprinting, delivery reports).
        # event_version 2 extends the payload so every blocking gap carries its
        # reason code and content, and reconciliation is countable without
        # re-reading the full report dict.  TraceRecorder.record applies
        # setdefault semantics, so an explicit event_version is preserved.
        self.trace_recorder.record(EventType.VERIFIER_COMPLETED, {
            "overall_status": report["overall_status"],
            "group_count": len(reducers),
            "failed_groups": report["failed_tests"],
            "event_version": 2,
            "reason_codes": [
                "EVIDENCE_COVERAGE_GAP" for _ in coverage_gaps
            ],
            "coverage_gap_count": len(coverage_gaps),
            "coverage_gaps": [
                {
                    "group_id": str(item.get("group_id") or ""),
                    "gap": item.get("gap"),
                    "evidence_needed": item.get("evidence_needed"),
                    "required_for_delivery": item.get("required_for_delivery", True),
                }
                for item in coverage_gaps
            ],
            "reconciled_gap_count": len(reconciled_gaps),
        })
        return report




    @staticmethod
    def _delivery_produced_paths(run_dir: Optional[Any]) -> List[str]:
        """Files this run actually produced under the delivery root.

        DeerFlow derives its produced set from a workspace before/after
        snapshot; we already have the same fact as a tree, filtered by the one
        shared visibility rule the publisher uses (``_delivery_evidence_visible``
        -- imported lazily because orchestrator imports this module).
        """
        if run_dir is None:
            return []
        root = Path(str(run_dir))
        deliverables = root / "deliverables"
        if not deliverables.is_dir():
            return []
        from AstralNexus.core.orchestrator import _delivery_evidence_visible

        produced: List[str] = []
        for path in sorted(deliverables.rglob("*")):
            if not path.is_file() or not _delivery_evidence_visible(path):
                continue
            try:
                produced.append(path.relative_to(root).as_posix())
            except ValueError:
                continue
        return produced

    @staticmethod
    def _delivery_presented_paths(contract: Dict[str, Any], run_dir: Optional[Any]) -> List[str]:
        """Paths the work put forward -- our analogue of ``present_files``."""
        presented: List[str] = []
        for item in (contract.get("required_outputs") or []):
            if not isinstance(item, dict):
                continue
            for key in ("path", "file", "location", "artifact_path"):
                value = str(item.get(key) or "").strip()
                if value:
                    presented.append(value.replace("\\", "/"))
                    break
        if not presented and run_dir is not None:
            # No declared path anywhere: the delivery root itself is the
            # declaration.  Documented substitution -- see the docstring.
            presented.append("deliverables")
        deduped: List[str] = []
        for value in presented:
            if value not in deduped:
                deduped.append(value)
        return deduped
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

        This explicit diagnostic does not decide default run delivery. The
        application presentation receipt is the sole default terminal fact.
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
        from AstralNexus.runtime.llm_client import LLMClientError
        try:
            parsed = _parse_governor_json(client.chat([
                {"role": "system", "content": "你是全局上下文治理器，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ]))
        except LLMClientError as exc:
            self.trace_recorder.record(EventType.CONTEXT_GOVERNANCE_FAILED, {"error": str(exc)[:500], "failure_class": "transport_unavailable"})
            raise
        if not isinstance(parsed, dict):
            raise ContextGovernanceUnavailable("context governance response was not a JSON object")
        if not isinstance(parsed.get("actions", []), list) or not isinstance(parsed.get("groups", []), list):
            raise ContextGovernanceUnavailable("context governance actions/groups must be arrays")
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
            raise ContextGovernanceUnavailable(
                "GlobalGovernor context decision omitted packet actions: "
                f"{sorted(missing_actions)}"
            )
        working = {
            node_id for node_id, action in actions.items()
            if action in {"retain", "compress"}
        }
        if not working:
            raise ContextGovernanceUnavailable("GlobalGovernor context decision produced an empty working set")
        groups: List[Dict[str, Any]] = []
        assigned: Set[str] = set()
        for index, group in enumerate(parsed.get("groups", []), 1):
            if not isinstance(group, dict):
                continue
            if not isinstance(group.get("node_ids", []), list):
                raise ContextGovernanceUnavailable("context group node_ids must be an array")
            ids = strings_from(group.get("node_ids", []))
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
            raise ContextGovernanceUnavailable(
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
                raise ContextGovernanceUnavailable(
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

    def evaluate_task_proposal(self, proposal: TaskProposal, *, _record_stats: bool = True) -> AdmissionDecision:
        return GraphAdmissionService().evaluate(self, proposal, lambda: self._evaluate_task_proposal(proposal), record_stats=_record_stats)

    def _evaluate_task_proposal(self, proposal: TaskProposal) -> AdmissionDecision:
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
        reason = GraphAdmissionService.preview_reason(self, proposal.estimated_cost)
        if reason:
            return AdmissionDecision(admitted=False, value_score=0.0,
                                     reasoning="Admission constraint rejected: " + reason,
                                     rejection_reason=reason)

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
        

        decision = AdmissionDecision(
            admitted=admitted,
            value_score=value_score,
            reasoning=reasoning,
            rejection_reason=None if admitted else "value_below_threshold",
        )
        
        
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
        with self.task_graph._mutation_lock, self.budget._lock:
            if not decision.admitted:
                return None
        
            # Generate task ID — prefer caller-specified ID for plan decomposition
            task_id = proposal.preferred_node_id or f"dynamic-{self.total_tasks_admitted + 1:03d}"
        
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
        
            raw = {
                "node_id": node.node_id, "goal": node.goal, "role": node.role,
                "dependencies": list(node.dependencies), "constraints": list(node.constraints),
                "acceptance_criteria": list(node.acceptance_criteria), "metadata": dict(node.metadata),
                "expected_outputs": list(node.outputs),
                "required_capabilities": list(proposal.required_capabilities),
            }
            result = self.apply_task_graph_patch(TaskGraphPatch(add_nodes=[raw]), parent_task_id=proposal.parent_task_id)
            task_id = str(result.get("id_map", {}).get(task_id, task_id))
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
        with self.budget._lock:
            self.budget.update(tokens=tokens_used, time_seconds=time_used, parallel_delta=parallel_delta)

            self.trace_recorder.record(EventType.BUDGET_UPDATED, {
                "tokens_used": tokens_used,
                "time_used": time_used,
                "parallel_delta": parallel_delta,
                "total_tokens_used": self.budget.used_tokens,
                "total_time_used": self.budget.elapsed_wall_seconds(),
                "worker_time_seconds": self.budget.worker_time_seconds,
                "reserved_tokens": self.budget.reserved_tokens,
                "active_parallel": self.budget.active_parallel_tasks,
                "remaining_tokens": self.budget.remaining_tokens(),
                "remaining_time": self.budget.remaining_time(),
            })

    def record_task_start(self, task_id: str, estimated_tokens: int = 0):
        """Record task start: occupy a parallel slot and reserve token budget.

        Called by the orchestrator when a worker begins execution.
        Increments active_parallel_tasks and pre-reserves estimated tokens.
        """
        with self.budget._lock:
            self.budget.reserve(task_id, estimated_tokens)
            try:
                self.trace_recorder.record(EventType.TASK_STARTED, {
                    "task_id": task_id,
                    "estimated_tokens": estimated_tokens,
                    "active_parallel": self.budget.active_parallel_tasks,
                    "remaining_slots": self.budget.available_parallel_slots(),
                })
            except BaseException:
                self.budget.settle(task_id, 0, 0)
                raise

    def record_task_end(
        self,
        task_id: str,
        actual_tokens: int | None = 0,
        actual_time_seconds: float = 0.0,
        tool_calls: int | None = 0,
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
        with self.budget._lock:
            allowed_outcomes = {"succeeded", "failed", "interrupted", "partial"}
            if execution_outcome not in allowed_outcomes:
                raise ValueError(
                    f"Unknown worker execution outcome: {execution_outcome}"
                )
            self.budget.settle(task_id, actual_tokens, actual_time_seconds)
            snapshot = {
                "task_id": task_id,
                "actual_tokens": actual_tokens,
                "token_usage_known": actual_tokens is not None,
                "actual_time_seconds": actual_time_seconds,
                "tool_calls": tool_calls,
                "tool_usage_known": tool_calls is not None,
                "execution_outcome": execution_outcome,
                "stop_reason": stop_reason,
                "total_tokens_used": self.budget.used_tokens,
                "total_time_used": self.budget.elapsed_wall_seconds(),
                "worker_time_seconds": self.budget.worker_time_seconds,
                "reserved_tokens": self.budget.reserved_tokens,
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
        with self.task_graph._mutation_lock, self.budget._lock:
            if self.termination_requested:
                return {"reason_code": "EXPLICIT_TERMINATION", "details": "Termination explicitly requested"}
            if (self.budget.max_tokens > 0 and self.budget.remaining_tokens() <= 0
                    and (self.budget.used_tokens >= self.budget.max_tokens or self.budget.active_parallel_tasks == 0)):
                return {
                    "reason_code": "TOKEN_BUDGET_EXHAUSTED",
                    "details": f"Token capacity unavailable: consumed={self.budget.used_tokens}, reserved={self.budget.reserved_tokens}, limit={self.budget.max_tokens}",
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
            if len(self.task_graph.nodes) >= self.max_total_tasks and self._count_active_tasks() == 0:
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
        with self.task_graph._mutation_lock, self.budget._lock:
            return {
                "total_proposals": self.total_tasks_created,
                "evaluated_proposals": self.total_proposals_evaluated,
                "graph_total_tasks": len(self.task_graph.nodes),
                "admitted": self.total_tasks_admitted,
                "rejected": self.total_tasks_rejected,
                "merged": getattr(self, "total_tasks_merged", 0),
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
