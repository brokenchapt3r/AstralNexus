"""Project one attempt's runtime measurements without inventing zero usage."""
from typing import Any


def execution_accounting(metrics: dict[str, Any], outcome: str) -> dict[str, Any]:
    budget = metrics.get("task_execution_budget") or {}
    if not isinstance(budget, dict):
        budget = {}
    usage = metrics.get("token_usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    def number(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

    tokens = number(usage.get("total_tokens", metrics.get("total_tokens")))
    if tokens is None:
        prompt = number(usage.get("prompt_tokens", metrics.get("prompt_tokens")))
        completion = number(usage.get("completion_tokens", metrics.get("completion_tokens")))
        if prompt is not None and completion is not None:
            tokens = prompt + completion
    reason = str(budget.get("exhausted_reason") or "")
    return {
        "actual_tokens": tokens,
        "tool_calls": number(budget.get("tool_calls_used", usage.get("tool_calls"))),
        "execution_outcome": "partial" if reason and outcome == "succeeded" else outcome,
        "stop_reason": reason or str(metrics.get("error") or ""),
    }
