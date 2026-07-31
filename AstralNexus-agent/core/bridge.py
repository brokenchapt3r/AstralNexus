"""Bridge between AgentResponse and StructuredMessage.

This module provides bidirectional conversion between:
  - AgentResponse (from agents.base): rich execution result from agent work
  - StructuredMessage (from core.message_protocol): low-entropy communication
    protocol for inter-node messaging in the semantic task graph

Design intent (天枢方案书 §5.3 低熵结构化通信协议):
  * AgentResponse → StructuredMessage: compress execution results into
    standardized low-entropy format for task graph propagation
  * StructuredMessage → AgentResponse: reconstruct execution context when
    a downstream node needs to consume upstream results

This bridge is the key enabler of heterogeneous agent collaboration while
maintaining protocol consistency across the system.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from AstralNexus.agents.base import (
    AgentContext,
    AgentResponse,
    ExecutionOutcome,
)
from AstralNexus.core.message_protocol import (
    MessageProtocol,
    StructuredMessage,
)
from AstralNexus.core.state_machine import TaskStatus


def response_to_message(
    response: AgentResponse,
    *,
    node_id: str,
    role: str,
    goal: str,
) -> StructuredMessage:
    """Convert an AgentResponse into a StructuredMessage for the task graph.

    This function implements the "low-entropy" compression strategy by:
      1. Mapping response.outcome → message.status enum
      2. Extracting key claims from response.content (summary + metrics)
      3. Preserving artifacts and next_actions for downstream consumption
      4. Recording budget_used from response.metrics

    Args:
        response: The execution result from an agent
        node_id: The task graph node this response belongs to
        role: The role that produced this response
        goal: The original goal of the task

    Returns:
        A StructuredMessage ready for task graph storage and propagation
    """
    outcome_to_lifecycle = {
        ExecutionOutcome.SUCCEEDED: TaskStatus.RESULT_SUBMITTED,
        ExecutionOutcome.FAILED: TaskStatus.FAILED,
        ExecutionOutcome.PARTIAL: TaskStatus.BLOCKED,
        ExecutionOutcome.INTERRUPTED: TaskStatus.READY,
    }
    msg_status = outcome_to_lifecycle[response.outcome]

    # Build claims from content and metrics
    claims: list[dict[str, Any]] = []

    # Primary claim: the execution summary
    if response.content:
        claims.append({
            "text": response.content[:500],  # Truncate for low-entropy
            "evidence": list(response.artifacts),
            "confidence": _estimate_confidence(response),
        })

    # Secondary claims: key metrics
    for key, value in response.metrics.items():
        if isinstance(value, (int, float)) and key in [
            "test_passed",
            "coverage",
            "lines_changed",
            "files_modified",
        ]:
            claims.append({
                "text": f"{key}: {value}",
                "evidence": [],
                "confidence": 1.0,
            })

    # Build next actions for the task graph
    next_actions = [
        {
            "type": action.get("type", "finish"),
            "target": action.get("target", node_id),
            "reason": action.get("reason", ""),
        }
        for action in response.next_actions
    ]

    # Extract budget from metrics
    budget_used = {
        "prompt_tokens": response.metrics.get("prompt_tokens", 0),
        "completion_tokens": response.metrics.get("completion_tokens", 0),
        "wall_time_s": response.metrics.get(
            "wall_time_s", response.metrics.get("elapsed_seconds", 0.0)
        ),
    }

    # Store goal and timestamp in metadata (StructuredMessage has no dedicated fields)
    now = datetime.now(timezone.utc).isoformat()
    metadata: dict[str, Any] = {
        "goal": goal,
        "timestamp": now,
        "execution_path": response.metrics.get("execution_path", "runtime_llm_direct"),
        "execution_outcome": response.outcome.value,
    }

    return StructuredMessage(
        node_id=node_id,
        role=role,
        status=msg_status,
        summary=(response.content or "")[:120],
        claims=claims,
        artifacts=list(response.artifacts),
        risks=[],
        next_actions=next_actions,
        budget_used=budget_used,
        metadata=metadata,
    )


def validated_response_to_message(
    response: AgentResponse,
    *,
    node_id: str,
    role: str,
    goal: str,
) -> StructuredMessage:
    """Convert and validate at the sole AgentResponse protocol boundary."""
    message = response_to_message(
        response,
        node_id=node_id,
        role=role,
        goal=goal,
    )
    valid, error = MessageProtocol().validate_message(message)
    if not valid:
        raise ValueError(
            f"AgentResponse cannot be represented as StructuredMessage: {error}"
        )
    return message


def message_to_evidence_packet(
    message: StructuredMessage,
    *,
    task_version: int,
) -> dict[str, Any]:
    """Project a validated low-entropy message into the L1 evidence schema."""
    claims = []
    confidences = []
    evidence_refs = list(message.artifacts)
    for item in message.claims:
        if not isinstance(item, dict):
            continue
        confidence = float(item.get("confidence", 0.0) or 0.0)
        confidences.append(confidence)
        item_evidence = [
            str(value)
            for value in (item.get("evidence") or [])
            if str(value).strip()
        ]
        evidence_refs.extend(item_evidence)
        claims.append(
            {
                "claim": str(item.get("text") or "").strip(),
                "evidence_refs": item_evidence,
                "confidence": confidence,
                "epistemic_status": "derived",
            }
        )
    return {
        "node_id": message.node_id,
        "summary": message.summary,
        "claims": claims,
        "confidence": (
            sum(confidences) / len(confidences) if confidences else 0.0
        ),
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "limitations": list(message.risks),
        "open_questions": [],
        "epistemic_status": "derived",
        "task_version": max(1, int(task_version or 1)),
        "execution_outcome": message.metadata.get("execution_outcome"),
        "artifacts": list(message.artifacts),
        "next_actions": list(message.next_actions),
        "budget_used": dict(message.budget_used),
    }


def message_to_context(
    message: StructuredMessage,
    *,
    target_node_id: str,
) -> AgentContext:
    """Convert a StructuredMessage into an AgentContext for downstream execution.

    This function reconstructs execution context by:
      1. Setting node_id to the target node (downstream consumer)
      2. Extracting constraints from claims marked as high-confidence
      3. Providing artifacts as available resources
      4. Populating metadata with upstream execution details

    Args:
        message: The upstream StructuredMessage to consume
        target_node_id: The node ID of the downstream consumer

    Returns:
        An AgentContext ready for agent execution
    """
    # Extract constraints from high-confidence claims
    constraints = [
        f"[from {message.node_id}] {claim['text']}"
        for claim in message.claims
        if isinstance(claim, dict) and claim.get("confidence", 0) >= 0.8
    ]

    # Build metadata with upstream context
    goal = message.metadata.get("goal", "")
    metadata = {
        "upstream_node_id": message.node_id,
        "upstream_role": message.role,
        "upstream_goal": goal,
        "upstream_status": message.status.value,
        "upstream_timestamp": message.metadata.get("timestamp", ""),
        "upstream_summary": message.summary,
    }

    return AgentContext(
        node_id=target_node_id,
        goal=goal,
        constraints=constraints,
        tools=[],  # Tools are determined by the role, not the message
        resources={"artifacts": list(message.artifacts)},
        artifacts=list(message.artifacts),
        metadata=metadata,
    )


def _estimate_confidence(response: AgentResponse) -> float:
    """Estimate the confidence level of an agent response.

    Heuristic:
      - Success status → 0.9 base confidence
      - Partial status → 0.6 base confidence
      - Failure status → 0.3 base confidence
      - Bonus for having artifacts (evidence)
      - Bonus for having detailed metrics

    Args:
        response: The agent response to evaluate

    Returns:
        Confidence score in [0.0, 1.0]
    """
    if response.outcome == ExecutionOutcome.SUCCEEDED:
        base = 0.9
    elif response.outcome == ExecutionOutcome.PARTIAL:
        base = 0.6
    else:
        base = 0.3

    # Bonus for evidence (artifacts)
    artifact_bonus = min(0.1, len(response.artifacts) * 0.02)

    # Bonus for detailed metrics
    metric_bonus = min(0.05, len(response.metrics) * 0.01)

    return min(1.0, base + artifact_bonus + metric_bonus)


def batch_responses_to_messages(
    responses: list[tuple[AgentResponse, str, str, str]],
) -> list[StructuredMessage]:
    """Convert a batch of AgentResponses into StructuredMessages.

    Convenience function for orchestrator use cases where multiple nodes
    complete simultaneously.

    Args:
        responses: List of (response, node_id, role, goal) tuples

    Returns:
        List of StructuredMessages
    """
    return [
        response_to_message(resp, node_id=nid, role=role, goal=goal)
        for resp, nid, role, goal in responses
    ]
