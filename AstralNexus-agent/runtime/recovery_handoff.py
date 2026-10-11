"""Bounded recovery input; historical diagnoses remain claims, not acceptance."""
from __future__ import annotations

from typing import Any

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.development_playbook import recovery_procedures


def runtime_failure_is_historical(metadata: dict[str, Any]) -> bool:
    """A submitted successful attempt supersedes an older execution error.

    This does not approve acceptance: the current verification rejection remains
    authoritative. Require both runtime accounting and the submitted packet so
    an exception during postconditions is not mistaken for a successful attempt.
    """
    accounting = metadata.get("_attempt_accounting") or {}
    packet = metadata.get("evidence_packet") or {}
    return (isinstance(accounting, dict) and isinstance(packet, dict)
            and accounting.get("execution_outcome") == "succeeded"
            and packet.get("execution_outcome") == "succeeded")


def build_recovery_handoff(graph: TaskGraph, node: TaskNode) -> dict[str, Any]:
    """Find the nearest rejected/failed attempt without copying its full history."""
    current: TaskNode | None = node
    seen: set[str] = set()
    while current is not None and current.node_id not in seen and len(seen) < 16:
        seen.add(current.node_id)
        metadata = current.metadata
        receipt = metadata.get("_verification_receipt") or {}
        failure = metadata.get("last_failure") or {}
        if not isinstance(receipt, dict):
            receipt = {}
        if not isinstance(failure, dict):
            failure = {}
        verdict = str(receipt.get("verdict") or "")
        if failure.get("message") or verdict in {"repair_required", "replan_required", "rejected", "inconclusive"}:
            packet = metadata.get("evidence_packet") or {}
            if not isinstance(packet, dict):
                packet = {}
            actions = packet.get("next_actions") or []
            pending = []
            for action in actions[:6] if isinstance(actions, list) else []:
                if isinstance(action, dict):
                    pending.append({
                        key: str(action[key])[:1600]
                        for key in ("goal", "target", "reason", "role")
                        if action.get(key)
                    })
            refs = packet.get("evidence_refs") or []
            historical = runtime_failure_is_historical(metadata)
            return {
                "source_node_id": current.node_id,
                "source_execution_id": str(metadata.get("runtime_execution_id") or ""),
                "verdict": verdict,
                "runtime_accounting": dict(metadata.get("_attempt_accounting") or {}),
                "failed_dimensions": list(receipt.get("failed_dimensions") or [])[:12],
                "repair_procedures": recovery_procedures(
                    receipt.get("observations"),
                    owner_node_id=current.node_id,
                    nodes=graph.nodes.values(),
                ),
                "failure": "" if historical else str(failure.get("message") or "")[:1600],
                "historical_execution_failure": str(failure.get("message") or "")[:1600] if historical else "",
                "worker_summary_unverified": str(packet.get("summary") or "")[:2000],
                "proposed_actions_unapproved": pending,
                "evidence_refs": [str(ref)[:1000] for ref in refs[:12]] if isinstance(refs, list) else [],
            }
        predecessor = str(metadata.get("recovery_of") or metadata.get("supersedes_node_id") or "")
        current = graph.get_node(predecessor) if predecessor else None
    return {}
