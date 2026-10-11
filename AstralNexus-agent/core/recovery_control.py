"""Shared identity and run-wide recovery cost projection."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


def gap_identity(gaps: Iterable[Mapping[str, Any]]) -> str:
    # Prefer frozen requirement identities; group labels and node names are
    # presentation coordinates and cannot reset recovery accounting.
    keys = sorted({str(item.get("criterion_id") or item.get("requirement_id")
                       or item.get("evidence_needed") or item.get("gap") or "")
                   for item in gaps})
    return hashlib.sha256(json.dumps(keys, ensure_ascii=False).encode()).hexdigest()


def recovery_totals(ledger: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        **{key: sum(int(entry.get(key) or 0) for entry in ledger.values())
           for key in ("requests", "tokens", "tool_calls", "recoveries")},
        "satisfied_items": sorted({str(item) for entry in ledger.values()
                                   for item in entry.get("satisfied_items", []) or []}),
    }


def event_cost(event_type: str, payload: Mapping[str, Any]) -> dict[str, int]:
    requests = tokens = tool_calls = 0
    if event_type in {"llm.request", "llm.stream.started"}:
        requests = 1
    elif event_type in {"llm.response", "llm.usage"}:
        values = payload.get("metrics")
        metrics = values if isinstance(values, dict) else payload
        prompt, completion = metrics.get("prompt_tokens"), metrics.get("completion_tokens")
        if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
            tokens = int(prompt) + int(completion)
        elif isinstance(metrics.get("total_tokens"), (int, float)):
            tokens = int(metrics["total_tokens"])
    elif event_type == "llm.tool_execution.done":
        calls = payload.get("calls")
        if isinstance(calls, list):
            tool_calls = len(calls)
        elif payload.get("tool_name"):
            tool_calls = 1
    return {"requests": requests, "tokens": max(0, tokens), "tool_calls": tool_calls}
