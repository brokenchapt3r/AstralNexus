"""Independent quality verification for governed task execution.

The Verifier Mesh independently verifies task results along multiple dimensions:
- Goal coverage: does the result address the original goal?
- Evidence support: is the result backed by evidence?
- Constraint satisfaction: are all constraints met?
- Execution success: did the tools actually execute?
- Cross-agent consistency: are results consistent across agents?

Verifier reports quality facts and evidence gaps. It never mutates the task
graph or chooses a recovery strategy; GlobalGovernor owns those decisions.
"""
from __future__ import annotations

import time
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder


class VerificationVerdict(str, Enum):
    """Quality judgment, deliberately separate from task lifecycle."""

    APPROVED = "approved"
    REJECTED = "rejected"
    REPAIR_REQUIRED = "repair_required"
    REPLAN_REQUIRED = "replan_required"
    INCONCLUSIVE = "inconclusive"


@dataclass
class VerificationDimension:
    """A single verification dimension result."""
    name: str
    score: float  # 0.0 to 1.0
    passed: bool
    details: str = ""
    evidence_refs: List[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    """Complete verification result for a task node."""
    node_id: str
    verdict: VerificationVerdict
    dimensions: List[VerificationDimension]
    overall_score: float
    timestamp: float = 0.0
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


class Verifier:
    """Unique quality authority.
    
    Responsibilities:
    - Verify task results along multiple dimensions
    - Detect failures and inconsistencies
    - Report evidence gaps for Governor policy decisions
    - Track verification history for audit
    """
    
    def __init__(
        self,
        task_graph: TaskGraph,
        trace_recorder: TraceRecorder,
        *,
        pass_threshold: float = 0.70,
        repair_threshold: float = 0.45,
        max_repair_attempts: int = 2,
    ):
        self.task_graph = task_graph
        self.trace_recorder = trace_recorder
        self.pass_threshold = pass_threshold
        self.repair_threshold = repair_threshold
        self.max_repair_attempts = max_repair_attempts
        
        # Verification history
        self.verification_results: Dict[str, VerificationResult] = {}
        self.total_verifications = 0
        self.total_approved = 0
        self.total_rejected = 0
        self.total_repair_triggered = 0
        self.total_replan_triggered = 0
        
        self.trace_recorder.record("verifier.initialized", {
            "pass_threshold": pass_threshold,
            "repair_threshold": repair_threshold,
        })

    def verify_node(self, node_id: str) -> VerificationResult:
        """Verify a task node's result.
        
        Checks multiple dimensions:
        1. Goal coverage
        2. Evidence support
        3. Constraint satisfaction
        4. Execution success
        5. Cross-agent consistency
        
        Args:
            node_id: The node to verify
            
        Returns:
            VerificationResult with dimension scores and actions
        """
        node = self.task_graph.nodes.get(node_id)
        if not node:
            return VerificationResult(
                node_id=node_id,
                verdict=VerificationVerdict.REJECTED,
                dimensions=[],
                overall_score=0.0,
            )
        
        dimensions = []
        
        # Dimension 1: Goal coverage
        goal_score = self._verify_goal_coverage(node)
        dimensions.append(goal_score)
        
        # Dimension 2: Evidence support
        evidence_score = self._verify_evidence_support(node)
        dimensions.append(evidence_score)
        
        # Dimension 3: Constraint satisfaction
        constraint_score = self._verify_constraint_satisfaction(node)
        dimensions.append(constraint_score)
        
        # Dimension 4: Execution success
        execution_score = self._verify_execution_success(node)
        dimensions.append(execution_score)
        
        # Dimension 5: Cross-agent consistency
        consistency_score = self._verify_cross_agent_consistency(node)
        dimensions.append(consistency_score)
        
        # Calculate overall score
        overall_score = sum(d.score for d in dimensions) / len(dimensions)
        
        # Critical dimensions are independent delivery gates. A high score in
        # evidence or consistency must never hide that the node did not cover
        # its own goal, violated a constraint, or failed to execute.
        goal_dim = next((d for d in dimensions if d.name == "goal_coverage"), None)
        hard_goal_failed = goal_dim is not None and goal_dim.score < self.repair_threshold
        constraint_dim = next((d for d in dimensions if d.name == "constraint_satisfaction"), None)
        hard_constraint_failed = constraint_dim is not None and constraint_dim.score < self.repair_threshold
        execution_dim = next((d for d in dimensions if d.name == "execution_success"), None)
        hard_execution_failed = execution_dim is not None and execution_dim.score < self.repair_threshold
        
        # Determine verdict: any critical-dimension failure overrides average.
        if hard_goal_failed or hard_constraint_failed or hard_execution_failed:
            failed_dim = (
                goal_dim
                if hard_goal_failed
                else constraint_dim
                if hard_constraint_failed
                else execution_dim
            )
            verdict = (
                VerificationVerdict.REPAIR_REQUIRED
                if failed_dim is not None and failed_dim.score >= 0.3
                else VerificationVerdict.REPLAN_REQUIRED
            )
        elif overall_score >= self.pass_threshold:
            verdict = VerificationVerdict.APPROVED
        elif overall_score >= self.repair_threshold:
            verdict = VerificationVerdict.REPAIR_REQUIRED
        else:
            verdict = VerificationVerdict.REPLAN_REQUIRED
        
        result = VerificationResult(
            node_id=node_id,
            verdict=verdict,
            dimensions=dimensions,
            overall_score=overall_score,
        )
        
        self.verification_results[node_id] = result
        self.total_verifications += 1
        
        if verdict == VerificationVerdict.APPROVED:
            self.total_approved += 1
        elif verdict == VerificationVerdict.REPAIR_REQUIRED:
            self.total_repair_triggered += 1
        elif verdict == VerificationVerdict.REPLAN_REQUIRED:
            self.total_replan_triggered += 1
        else:
            self.total_rejected += 1
        
        self.trace_recorder.record("verifier.verified", {
            "node_id": node_id,
            "verdict": verdict.value,
            "overall_score": overall_score,
            "dimensions": {d.name: d.score for d in dimensions},
            "failed_dimensions": [d.name for d in dimensions if not d.passed],
        })
        
        return result
    
    def get_verification_stats(self) -> Dict[str, Any]:
        """Get verification statistics."""
        return {
            "total_verifications": self.total_verifications,
            "approved": self.total_approved,
            "rejected": self.total_rejected,
            "repair_triggered": self.total_repair_triggered,
            "replan_triggered": self.total_replan_triggered,
            "approval_rate": (
                self.total_approved / max(1, self.total_verifications)
            ),
            "repair_rate": (
                self.total_repair_triggered / max(1, self.total_verifications)
            ),
            "replan_rate": (
                self.total_replan_triggered / max(1, self.total_verifications)
            ),
        }
    
    # --- Private verification methods ---
    
    def _verify_goal_coverage(self, node: TaskNode) -> VerificationDimension:
        """Verify that the result covers the original goal."""
        evidence_packet = node.metadata.get("evidence_packet", {})
        summary = (
            evidence_packet.get("summary", "")
            if isinstance(evidence_packet, dict)
            else ""
        )
        
        if not summary:
            return VerificationDimension(
                name="goal_coverage",
                score=0.3,
                passed=False,
                details="No output summary available",
            )
        
        # Simple keyword overlap heuristic
        goal_words = set(node.goal.lower().split())
        summary_words = set(summary.lower().split())
        
        if not goal_words:
            return VerificationDimension(
                name="goal_coverage",
                score=0.5,
                passed=True,
                details="Empty goal",
            )
        
        overlap = len(goal_words & summary_words) / len(goal_words)
        # Chinese goals are commonly written without whitespace, so the
        # whitespace-token overlap above is insufficient for real LLM output.
        # Fall back to character bigrams while keeping the same evidence-based
        # verifier rather than accepting every non-empty response.
        if overlap == 0 and len(node.goal) >= 2 and len(summary) >= 2:
            goal_bigrams = {node.goal[i:i + 2] for i in range(len(node.goal) - 1)}
            summary_bigrams = {summary[i:i + 2] for i in range(len(summary) - 1)}
            overlap = len(goal_bigrams & summary_bigrams) / max(1, len(goal_bigrams))
        score = min(1.0, overlap * 1.5)  # Boost slightly
        
        return VerificationDimension(
            name="goal_coverage",
            score=score,
            passed=score >= self.pass_threshold,
            details=f"Keyword overlap: {overlap:.2f}",
        )
    
    def _verify_evidence_support(self, node: TaskNode) -> VerificationDimension:
        """Verify evidence from the canonical Bridge EvidencePacket."""
        evidence_packet = node.metadata.get("evidence_packet", {})
        claims = (
            evidence_packet.get("claims", [])
            if isinstance(evidence_packet, dict)
            else []
        )
        artifacts = (
            evidence_packet.get("evidence_refs", [])
            if isinstance(evidence_packet, dict)
            else []
        )

        has_evidence = len(claims) > 0 or len(artifacts) > 0

        if has_evidence:
            score = min(1.0, 0.5 + len(claims) * 0.1 + len(artifacts) * 0.15)
        else:
            score = 0.4

        return VerificationDimension(
            name="evidence_support",
            score=score,
            passed=score >= self.pass_threshold,
            details=f"Claims: {len(claims)}, Artifacts: {len(artifacts)}",
        )
    
    def _verify_constraint_satisfaction(self, node: TaskNode) -> VerificationDimension:
        """Verify that all constraints are satisfied."""
        acceptance = node.acceptance_criteria or []
        if not acceptance:
            return VerificationDimension(
                name="constraint_satisfaction",
                score=0.8,
                passed=True,
                details="No explicit constraints",
            )
        
        evidence_packet = node.metadata.get("evidence_packet", {})
        summary = (
            evidence_packet.get("summary", "")
            if isinstance(evidence_packet, dict)
            else ""
        )
        
        satisfied = 0
        for criterion in acceptance:
            # Simple check: see if key words from criterion appear in output
            criterion_words = set(criterion.lower().split())
            summary_words = set(summary.lower().split()) if summary else set()
            if criterion_words & summary_words:
                satisfied += 1
                continue
            
            # Chinese acceptance criteria often have no whitespace, so
            # whitespace-token overlap is insufficient. Fall back to
            # character bigrams.
            if len(criterion) >= 2 and summary and len(summary) >= 2:
                criterion_bigrams = {criterion[i:i + 2] for i in range(len(criterion) - 1)}
                summary_bigrams = {summary[i:i + 2] for i in range(len(summary) - 1)}
                if criterion_bigrams & summary_bigrams:
                    satisfied += 1
        
        score = satisfied / len(acceptance) if acceptance else 1.0
        
        return VerificationDimension(
            name="constraint_satisfaction",
            score=score,
            passed=score >= self.pass_threshold,
            details=f"Satisfied: {satisfied}/{len(acceptance)}",
        )
    
    def _verify_execution_success(self, node: TaskNode) -> VerificationDimension:
        """Verify that the execution was successful."""
        evidence_packet = node.metadata.get("evidence_packet", {})
        summary = str(evidence_packet.get("summary", "")) if isinstance(evidence_packet, dict) else ""
        declared_failures = 0
        for match in re.finditer(r"(?i)\b(?:failed|failures|errors?)\s*[:=]\s*(\d+)", summary):
            declared_failures = max(declared_failures, int(match.group(1)))
        for match in re.finditer(r"(\d+)\s*(?:个|项)?测试失败", summary):
            declared_failures = max(declared_failures, int(match.group(1)))
        for match in re.finditer(r"(\d+)\s*/\s*(\d+)\s*passed", summary, re.IGNORECASE):
            declared_failures = max(
                declared_failures,
                max(0, int(match.group(2)) - int(match.group(1))),
            )
        if declared_failures > 0:
            return VerificationDimension(
                name="execution_success",
                score=0.0,
                passed=False,
                details=f"Execution evidence declares {declared_failures} failed tests",
            )
        execution_outcome = evidence_packet.get("execution_outcome")
        if execution_outcome == "succeeded":
            return VerificationDimension(
                name="execution_success",
                score=1.0,
                passed=True,
                details="Real LLM execution completed successfully",
            )
        elif node.status == "failed":
            return VerificationDimension(
                name="execution_success",
                score=0.0,
                passed=False,
                details="Node execution failed",
            )
        else:
            return VerificationDimension(
                name="execution_success",
                score=0.5,
                passed=False,
                details=f"Node in unexpected status: {node.status}",
            )
    
    def _verify_cross_agent_consistency(self, node: TaskNode) -> VerificationDimension:
        """Verify consistency with upstream agent results."""
        if not node.dependencies:
            return VerificationDimension(
                name="cross_agent_consistency",
                score=0.9,
                passed=True,
                details="No upstream dependencies",
            )
        
        # A dependency becomes usable when its result is submitted, not only
        # after the graph's later verification transition.  Repair and
        # verification nodes intentionally depend on a ``repair_pending`` or
        # ``verifying`` parent, so requiring ``completed`` here creates a
        # false cross-agent failure and recursive repair expansion.
        usable_upstream_statuses = {"completed", "result_submitted", "verifying", "repair_pending"}
        completed_deps = 0
        total_deps = len(node.dependencies)
        
        for dep_id in node.dependencies:
            dep_node = self.task_graph.nodes.get(dep_id)
            if dep_node and dep_node.status in usable_upstream_statuses:
                completed_deps += 1
        
        score = completed_deps / max(1, total_deps)
        
        return VerificationDimension(
            name="cross_agent_consistency",
            score=score,
            passed=score >= self.pass_threshold,
            details=f"Dependencies available for evidence exchange: {completed_deps}/{total_deps}",
        )
    
