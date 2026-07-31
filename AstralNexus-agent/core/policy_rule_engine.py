"""Deterministic policy boundary for Global Governor LLM proposals."""
from __future__ import annotations

from typing import Any

from AstralNexus.core.governor_policy import GovernorPolicy


class PolicyRuleEngine:
    """Enforce deterministic policy on proposals owned by GlobalGovernor.

    This internal boundary has no planning or graph-mutation authority.
    """

    def __init__(self, policy: GovernorPolicy):
        self.policy = policy

    def evaluate_interactive_route(
        self,
        proposal: dict[str, Any],
        *,
        active_run: bool,
    ) -> dict[str, Any]:
        mode = str(proposal.get("mode") or "").strip().lower()
        if mode not in {"direct_chat", "governed_run", "requirement_change"}:
            raise ValueError(
                f"Governor interactive route returned invalid mode: {mode}"
            )

        complexity = self._unit_interval(proposal.get("complexity_score"))
        parallel = self._unit_interval(proposal.get("parallel_benefit"))
        capabilities = self._strings(proposal.get("required_capabilities"))
        estimated_nodes = max(
            1,
            min(
                self.policy.interactive_max_estimated_nodes,
                int(proposal.get("estimated_nodes") or 1),
            ),
        )
        needs_artifacts = bool(proposal.get("needs_artifacts"))
        needs_verification = bool(proposal.get("needs_verification"))
        reason_codes: list[str] = []

        if active_run and mode == "governed_run":
            mode = "requirement_change"
            reason_codes.append("active_run_patch_required")
        elif not active_run and mode == "requirement_change":
            mode = "governed_run"
            reason_codes.append("no_active_run_create_graph")

        multi_capability = len(set(capabilities)) >= 2
        must_govern = (
            complexity >= self.policy.interactive_complexity_threshold
            or estimated_nodes >= self.policy.interactive_min_graph_nodes
            or (
                parallel >= self.policy.interactive_parallel_threshold
                and multi_capability
            )
            or (
                needs_artifacts
                and needs_verification
                and complexity >= self.policy.interactive_artifact_threshold
            )
        )
        if not active_run and mode == "direct_chat" and must_govern:
            mode = "governed_run"
            reason_codes.append("governor_complexity_guard")

        return {
            "mode": mode,
            "complexity_score": complexity,
            "parallel_benefit": parallel,
            "required_capabilities": capabilities,
            "estimated_nodes": estimated_nodes,
            "needs_artifacts": needs_artifacts,
            "needs_verification": needs_verification,
            "reasoning": str(proposal.get("reasoning") or "").strip(),
            "task_contract_summary": str(
                proposal.get("task_contract_summary") or ""
            ).strip(),
            "reason_codes": reason_codes,
        }

    @staticmethod
    def _unit_interval(value: Any) -> float:
        return max(0.0, min(1.0, float(value or 0.0)))

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]
