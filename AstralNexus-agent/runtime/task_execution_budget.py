"""Cooperative per-task limits for model-driven tool execution.

P6-D (doc 32 §4): every dispatched node must carry an enforceable budget.
The r14 run showed a stalled-replan patch node created with ``budget={}``
run unbounded for 4.9+ hours / 1100+ tool rounds because zero means
"unlimited" here and nothing downstream filled the gap.  ``ensure_task_budget``
is the single normalization point: declared positive limits are always
preserved; missing/zero/non-numeric caps are filled from a role/target
default table calibrated on r14 evidence (natural self-terminations get
~15% headroom; runaway forensics loops get cut with partial output kept via
the P1 ``capped_with_output`` delta).  ``max_seconds`` is deliberately NOT
defaulted: wall-clock deaths on read-heavy missions were the r14 P5#2
pathology, and DeerFlow's guardrail stack (#3875 Phase 2) caps turns/tokens,
not minutes.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

#: Default tool-round caps keyed by (normalized task_type, normalized
#: execution_target).  Calibration evidence (r14, doc 32 §4 P6-D):
#: - implementation/cloud 240: N4R self-terminated at ~r0205 (129 min);
#:   a 200 cap would have cut a healthy natural finish.
#: - synthesis/cloud 300: N10 landed at r0230+ (92.5 min); N9RRR's runaway
#:   (r1100+, no report) is the exact failure this table exists to bound.
#: - edge/local tiers scale down but stay above the r14 edge-tier budget
#:   deaths (6-11.5 min caps on read-heavy missions, P5#2) — defaults only
#:   apply when the planner declared nothing, never shrinking a real
#:   declaration.
_DEFAULT_TOOL_CALL_BUDGETS: dict[tuple[str, str], int] = {
    ("implementation", "cloud"): 240,
    ("implementation", "local"): 200,
    ("implementation", "edge"): 150,
    ("verification", "cloud"): 200,
    ("verification", "local"): 160,
    ("verification", "edge"): 120,
    ("synthesis", "cloud"): 300,
    ("synthesis", "local"): 240,
    ("synthesis", "edge"): 180,
    ("analysis", "cloud"): 200,
    ("analysis", "local"): 160,
    ("analysis", "edge"): 120,
    ("other", "cloud"): 150,
    ("other", "local"): 120,
    ("other", "edge"): 100,
}

#: Fallback for unknown task_type / execution_target combinations.
_FALLBACK_TOOL_CALL_BUDGETS: dict[str, int] = {"cloud": 150, "local": 120, "edge": 100}
_UNIVERSAL_FALLBACK_TOOL_CALLS = 150

#: Task-type synonyms folded onto the canonical table keys.  The planner
#: vocabulary (governor prompt schema) uses "implementation|verification|
#: analysis|other"; routing roles add coder/synthesizer/researcher flavors.
_TASK_TYPE_ALIASES: dict[str, str] = {
    "implementation": "implementation",
    "implementation_task": "implementation",
    "coder": "implementation",
    "developer": "implementation",
    "engineering": "implementation",
    "repair": "implementation",
    "verification": "verification",
    "verifier": "verification",
    "testing": "verification",
    "tester": "verification",
    "audit": "verification",
    "synthesis": "synthesis",
    "synthesizer": "synthesis",
    "assembly": "synthesis",
    "integration": "synthesis",
    "analysis": "analysis",
    "research": "analysis",
    "researcher": "analysis",
    "documentation": "analysis",
    "docs": "analysis",
    "writer": "analysis",
    "other": "other",
}

_TARGET_ALIASES: dict[str, str] = {"cloud": "cloud", "local": "local", "edge": "edge"}


def default_max_tool_calls(task_type: str = "", execution_target: str = "") -> int:
    """Default tool-round cap for a (task_type, execution_target) pair.

    Unknown values degrade to the per-target fallback, then to the universal
    fallback — never to zero, because zero means unlimited downstream.
    """
    canonical_type = _TASK_TYPE_ALIASES.get(str(task_type or "").strip().lower(), "other")
    canonical_target = _TARGET_ALIASES.get(str(execution_target or "").strip().lower(), "cloud")
    pair_default = _DEFAULT_TOOL_CALL_BUDGETS.get((canonical_type, canonical_target))
    if pair_default:
        return pair_default
    return _FALLBACK_TOOL_CALL_BUDGETS.get(canonical_target, _UNIVERSAL_FALLBACK_TOOL_CALLS)


def ensure_task_budget(
    budget: object,
    *,
    task_type: str = "",
    execution_target: str = "",
    repair_round: bool = False,
) -> tuple[dict[str, object], bool]:
    """Normalize a node's declared budget dict; fill an absent tool-call cap.

    Returns ``(budget, defaulted)`` where ``defaulted`` is True when the
    default table supplied ``max_tool_calls``.  Declared positive values are
    preserved verbatim (a planner that asks for a bigger or smaller budget
    gets exactly that — this is a floor against *unbounded*, not a ceiling
    against declared intent).  Missing keys, zero, negatives, and
    non-numeric junk all count as "not declared".  ``max_seconds`` is passed
    through untouched and never defaulted (see module docstring).

    ``repair_round`` marks a budget the *framework* issues when it re-enters a
    failed node or dispatches a ``recovery_of`` successor — never a planner
    declaration.  Such a round must be able to act, so its cap is floored at
    the role default (``max(declared, default)``); its counter starts fresh
    because ``agents/base.py`` builds a new ``TaskExecutionBudget`` per worker
    run.  Live r30 showed the opposite on both counts: the repair round
    re-adopted the spent ``max_tool_calls: 40`` *and* resumed at
    ``tool_calls_used: 40, tool_round: 0`` (trace.jsonl:23736), so it could not
    even write a file and N-103 stayed ``budget_exhausted`` while
    ``tickets.db`` never materialized.  A *planned* attempt keeps its declared
    cap verbatim: an undersized plan must stay visible as
    ``budget_exhausted`` instead of being silently overridden (doc 51 §22), and
    ``defaulted`` stays False whenever the declaration did supply a cap (it was
    floored, not defaulted).
    """
    normalized: dict[str, object] = {}
    if isinstance(budget, dict):
        for key, value in budget.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                normalized[str(key)] = value
    declared_calls = normalized.get("max_tool_calls")
    if isinstance(declared_calls, (int, float)) and declared_calls > 0:
        if repair_round:
            default_calls = default_max_tool_calls(task_type, execution_target)
            if int(declared_calls) < default_calls:
                normalized["max_tool_calls"] = default_calls
        return normalized, False
    normalized["max_tool_calls"] = default_max_tool_calls(task_type, execution_target)
    return normalized, True


class TaskExecutionBudget:
    """Track a worker node's declared tool-call and wall-clock budget.

    A zero or negative limit means that dimension is unlimited. The clock is
    injectable so deadline behavior can be tested without sleeping.
    """

    def __init__(
        self,
        *,
        max_tool_calls: int = 0,
        max_seconds: float = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.max_seconds = max(0.0, float(max_seconds))
        self._clock = clock
        self.started_at = clock()
        self.tool_calls_used = 0
        self.exhausted_reason: Optional[str] = None

    def exhaustion_reason(self) -> Optional[str]:
        if self.exhausted_reason:
            return self.exhausted_reason
        if self.max_seconds and self._clock() - self.started_at >= self.max_seconds:
            self.exhausted_reason = "max_seconds"
        elif self.max_tool_calls and self.tool_calls_used >= self.max_tool_calls:
            self.exhausted_reason = "max_tool_calls"
        return self.exhausted_reason

    def admit_tool_call(self) -> tuple[bool, Optional[str]]:
        """Consume one invocation slot, or return the exhausted dimension."""
        reason = self.exhaustion_reason()
        if reason:
            return False, reason
        self.tool_calls_used += 1
        return True, None

    def snapshot(self) -> dict[str, object]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "tool_calls_used": self.tool_calls_used,
            "max_seconds": self.max_seconds,
            "elapsed_seconds": max(0.0, self._clock() - self.started_at),
            "exhausted_reason": self.exhaustion_reason(),
        }
