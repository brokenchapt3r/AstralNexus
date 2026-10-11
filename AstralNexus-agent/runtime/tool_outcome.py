"""Unified tool result semantics — single normalization entry (DeerFlow port).

Contract source: deer-flow@2814bd5d498a ``agents/middlewares/tool_result_meta.py``
(MIT, reference copy under 02_运行产物/99_临时文件/deerflow_runtime_reference/).
Pure contract/algorithm extraction per the porting plan
(04_归档/项目文档/2026-10-10_历史修复记录/17_DeerFlow运行时源码映射与移植方案.md): no LangChain/LangGraph
dependency; adapted to AstralNexus executor result dicts.

Local modifications vs upstream (recorded per plan §复用方式):
- ``source`` gains ``"guard"`` for AstralNexus executor/guard pre-classified
  failures (``failure_class`` stamps from ``ToolExecutor._annotate_failure``
  and tool-loop guard blocks) — programmatic judgment before text heuristics.
- Guard-class map encodes the fix-21 deterministic-noncompliance doctrine:
  an ownership/write-scope rejection is NOT model-recoverable by retry; the
  recommended action is to document honestly and let verification route the
  root cause upstream (``summarize``/``stop``), never ``try_alternative``.
- Classification text for a result whose own status is success NEVER includes
  its stdout: a 403/500 inside the output of a successful probe of the
  application under test is measured DATA (expected-permission evidence), not
  a provider authentication failure. Only the tool's own error channel
  (``error``/``stderr``) or a raised exception classifies the call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

Status = Literal["success", "error", "partial_success"]
NextAction = Literal["continue", "rewrite_query", "try_alternative", "summarize", "stop"]
Source = Literal["exception", "tool_return", "guard", "content_analysis"]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Structured signal downstream consumers read instead of parsing text."""

    status: Status
    error_type: Optional[str]
    recoverable_by_model: bool
    recommended_next_action: NextAction
    source: Source

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error_type": self.error_type,
            "recoverable_by_model": self.recoverable_by_model,
            "recommended_next_action": self.recommended_next_action,
            "source": self.source,
        }


_SUCCESS = ToolOutcome("success", None, True, "continue", "tool_return")

# Programmatic pre-classification (highest precedence after a real exception):
# AstralNexus executor/guard stamps. Tuple = (error_type, recoverable, next).
_FAILURE_CLASS_MAP: Dict[str, Tuple[str, bool, NextAction]] = {
    "approval_required": ("permission", False, "stop"),
    "truncated_output": ("internal", True, "rewrite_query"),
    "invalid_tool_arguments": ("internal", True, "rewrite_query"),
    "write_scope_violation": ("permission", False, "summarize"),
    "ownership_conflict": ("permission", False, "summarize"),
    "loop_guard": ("unknown", False, "stop"),
}

# Keyword rules applied to the tool's OWN error channel only. Order matters:
# first hit wins. Numeric HTTP codes use word-boundary matching so durations
# like "took 500ms" never classify as internal errors.
_ERROR_RULES: List[Tuple[List[str], Dict[str, Any]]] = [
    (
        ["401", "403", "unauthorized", "authentication", "invalid api key"],
        {"error_type": "auth", "recoverable_by_model": False, "recommended_next_action": "stop"},
    ),
    (
        ["rate limit", "rate limited", "rate_limit"],
        {"error_type": "rate_limited", "recoverable_by_model": False, "recommended_next_action": "summarize"},
    ),
    (
        ["timeout", "timed out", "connection", "network error", "temporarily unavailable"],
        {"error_type": "transient", "recoverable_by_model": False, "recommended_next_action": "try_alternative"},
    ),
    (
        ["not configured", "not installed", "missing required", "disabled", "no api key"],
        {"error_type": "config", "recoverable_by_model": False, "recommended_next_action": "stop"},
    ),
    (
        ["permission denied", "access denied", "path traversal", "forbidden",
         "outside the worker owned_paths contract", "outside the Worker owned_paths contract"],
        {"error_type": "permission", "recoverable_by_model": True, "recommended_next_action": "try_alternative"},
    ),
    (
        ["no results found", "no content found", "no images found", "no results"],
        {"error_type": "no_results", "recoverable_by_model": True, "recommended_next_action": "rewrite_query"},
    ),
    (
        ["not found", "no such file", "does not exist", "404"],
        {"error_type": "not_found", "recoverable_by_model": True, "recommended_next_action": "rewrite_query"},
    ),
    (
        ["unexpected error", "internal error", "500"],
        {"error_type": "internal", "recoverable_by_model": False, "recommended_next_action": "stop"},
    ),
]

_UNKNOWN_ERROR: Dict[str, Any] = {
    "error_type": "unknown",
    "recoverable_by_model": True,
    "recommended_next_action": "try_alternative",
}

_PARTIAL_MARKERS = (
    "partial results",
    "limited results",
    "truncated",
    "results may be incomplete",
    "no results found",
    "no content found",
)

_NUMERIC_CODES = {"401", "403", "404", "500"}


def _text_hit(text: str, needle: str) -> bool:
    if needle in _NUMERIC_CODES:
        # \b treats "500ms" as one word: a duration never matches the code.
        return re.search(rf"\b{re.escape(needle)}\b", text) is not None
    return needle.lower() in text.lower()


def _classify(text: str) -> Dict[str, Any]:
    for needles, classification in _ERROR_RULES:
        if any(_text_hit(text, needle) for needle in needles):
            return classification
    return dict(_UNKNOWN_ERROR)


def normalize_tool_outcome(
    result: Any,
    *,
    tool_name: str = "",
    exception: Optional[BaseException] = None,
) -> ToolOutcome:
    """Single normalization entry for one tool call result.

    Precedence (programmatic before textual):
    1. a real exception from the call -> classify from the exception, source=exception
    2. executor/guard ``failure_class`` stamp -> mapped, source=guard
    3. the result's own ``status`` field (error/approval_pending) -> classify
       from the result's error channel only, source=tool_return
    4. success with partial markers in the output -> partial_success,
       source=content_analysis
    5. otherwise success, source=tool_return.

    ``tool_name`` is accepted for parity with the upstream contract and future
    per-tool gates; the success-path rule (stdout is data, never a failure
    signal) already makes the remote-content gate unnecessary here.
    """
    del tool_name  # documented parity parameter; see module docstring
    if exception is not None:
        classification = _classify(f"{type(exception).__name__}: {exception}")
        return ToolOutcome(
            status="error",
            error_type=str(classification["error_type"]),
            recoverable_by_model=bool(classification["recoverable_by_model"]),
            recommended_next_action=classification["recommended_next_action"],  # type: ignore[arg-type]
            source="exception",
        )
    if not isinstance(result, dict):
        result = {"output": str(result)}

    failure_class = str(result.get("failure_class") or "").strip()
    if failure_class in _FAILURE_CLASS_MAP:
        error_type, recoverable, next_action = _FAILURE_CLASS_MAP[failure_class]
        return ToolOutcome("error", error_type, recoverable, next_action, "guard")
    if result.get("guard_reason"):
        return ToolOutcome("error", "unknown", False, "stop", "guard")

    status = str(result.get("status") or "").strip().lower()
    if status == "approval_pending":
        return ToolOutcome("error", "permission", False, "stop", "guard")
    if status == "error" or (status in {"", "unknown"} and result.get("error")):
        # Only the tool's own error channel classifies — never stdout of the
        # application under test (a 403 there is measured permission evidence).
        error_text = " ".join(
            str(result.get(key) or "") for key in ("error", "stderr")
        )
        classification = _classify(error_text)
        return ToolOutcome(
            status="error",
            error_type=str(classification["error_type"]),
            recoverable_by_model=bool(classification["recoverable_by_model"]),
            recommended_next_action=classification["recommended_next_action"],  # type: ignore[arg-type]
            source="tool_return",
        )

    output_text = str(result.get("stdout") or result.get("output") or "")
    if output_text and any(marker in output_text.lower() for marker in _PARTIAL_MARKERS):
        classification = _classify(output_text)
        return ToolOutcome(
            status="partial_success",
            error_type=str(classification["error_type"]),
            recoverable_by_model=bool(classification["recoverable_by_model"]),
            recommended_next_action=classification["recommended_next_action"],  # type: ignore[arg-type]
            source="content_analysis",
        )
    return _SUCCESS
