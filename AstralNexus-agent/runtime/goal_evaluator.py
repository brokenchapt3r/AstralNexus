"""Opt-in goal evaluation from visible evidence, separate from delivery facts."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass
class GoalState:
    objective: str
    continuations: int = 0
    no_progress: int = 0
    signature: str = ""
    active: bool = True
    last_verdict: dict[str, Any] | None = None

    def decide(self, verdict: dict[str, Any], evidence: dict[str, Any]) -> str:
        self.last_verdict = verdict
        if verdict.get("satisfied") is True and verdict.get("relied_on_assumption") is not True:
            return "satisfied"  # Clear only after delivery and terminal commit.
        if verdict.get("blocker") != "goal_not_met_yet":
            return str(verdict.get("blocker") or "missing_evidence")
        # Model wording and duplicate calls are not new progress.
        facts = {"outputs": {path: version[:2] for path, version in evidence.get("outputs", {}).items()},
                 "tools": sorted({json.dumps(row, sort_keys=True, ensure_ascii=False) for row in evidence.get("tools", [])})}
        signature = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.no_progress = self.no_progress + 1 if signature == self.signature else 0
        self.signature = signature
        if self.no_progress >= 2:
            return "goal_no_progress"
        if self.continuations >= 8:
            return "goal_continuation_limit"
        self.continuations += 1
        return "continue"


def evaluate_goal(client: Any, objective: str, visible: dict[str, Any]) -> dict[str, Any]:
    from AstralNexus.core.governor import _parse_governor_json
    bounded = {"assistant": str(visible.get("assistant") or "")[-2000:],
               "tools": visible.get("tools", [])[:10], "execution_receipt_counts": visible.get("execution_receipt_counts", {}), "outputs": dict(list(visible.get("outputs", {}).items())[:20])}
    result = _parse_governor_json(client.chat([
        {"role": "system", "content": "Independently evaluate the explicitly requested goal ONLY from visible conversation/tool evidence. Do not reread files or run tools. Return JSON with satisfied:boolean, blocker:goal_not_met_yet|missing_evidence|needs_user_input|run_failed|external_wait, reason, evidence_summary, relied_on_assumption:boolean. Missing evidence cannot satisfy a goal; no business claim is proven by a delivery receipt. Use goal_not_met_yet when concrete work remains that the agent can perform (creating frontend, seed data, tests, reports, or executing verification). Reserve missing_evidence for evidence that cannot be obtained through further agent work, or invalid/uninterpretable observations. Incomplete development is not itself missing_evidence. Never mark failed tests as satisfied. Self-authored reports claiming tests passed are not test executions. Assess verification claims from execution tool receipts, not report prose; execution_receipt_counts is a runtime fact, not a model claim."},
        {"role": "user", "content": json.dumps({"objective": objective, "visible_evidence": bounded}, ensure_ascii=False)},
    ], max_tool_rounds=0, response_format="json"))
    if not isinstance(result, dict) or not isinstance(result.get("satisfied"), bool) or not isinstance(result.get("relied_on_assumption"), bool):
        return {"satisfied": False, "blocker": "missing_evidence", "reason": "invalid evaluator output", "relied_on_assumption": False}
    return result
