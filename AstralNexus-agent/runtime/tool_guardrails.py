"""Tool-call loop guardrails for a single LLM execution."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from typing import Any, Dict


class ToolCallGuard:
    """Detect unchanged tool-call loops without blocking normal retries."""

    def __init__(self, max_repeats: int | None = None, failure_threshold: int | None = None) -> None:
        self.max_repeats = max_repeats or max(1, int(os.environ.get("ASTRALNEXUS_TOOL_LOOP_REPEATS", "3")))
        self.failure_threshold = failure_threshold or max(
            1, int(os.environ.get("ASTRALNEXUS_TOOL_FAILURE_THRESHOLD", "2"))
        )
        self._calls: Counter[str] = Counter()
        self._failures: Counter[str] = Counter()

    @staticmethod
    def fingerprint(call: Dict[str, Any]) -> str:
        function = call.get("function") or {}
        payload = {
            "name": function.get("name", ""),
            "arguments": function.get("arguments", {}),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]

    def admit(self, call: Dict[str, Any]) -> tuple[bool, str]:
        key = self.fingerprint(call)
        self._calls[key] += 1
        if self._calls[key] > self.max_repeats:
            return False, "repeated_identical_tool_call"
        return True, key

    def observe(self, call: Dict[str, Any], result: Dict[str, Any]) -> None:
        if str(result.get("status", "")).lower() in {"error", "failed", "failure"}:
            self._failures[self.fingerprint(call)] += 1

    def decision(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """Return the next action for a call with a repeated/failing history.

        The guard does not invent a replacement tool.  The tool router or
        Governor remains responsible for selecting one from the registered
        allow-list; this object only emits the bounded recovery decision.
        """
        key = self.fingerprint(call)
        repeats = self._calls.get(key, 0)
        failures = self._failures.get(key, 0)
        if repeats > self.max_repeats:
            action = "halt"
            reason = "repeated_identical_tool_call"
        elif failures >= self.failure_threshold:
            action = "switch_tool_or_replan"
            reason = "repeated_tool_failure"
        else:
            action = "retry_or_continue"
            reason = "within_failure_budget"
        return {
            "action": action,
            "reason": reason,
            "call_fingerprint": key,
            "repeat_count": repeats,
            "failure_count": failures,
        }

    def snapshot(self) -> Dict[str, Any]:
        return {
            "unique_calls": len(self._calls),
            "repeat_counts": dict(self._calls),
            "failure_counts": dict(self._failures),
            "max_repeats": self.max_repeats,
            "failure_threshold": self.failure_threshold,
            "decisions": {
                key: (
                    "halt" if count > self.max_repeats else
                    "switch_tool_or_replan" if self._failures.get(key, 0) >= self.failure_threshold else
                    "retry_or_continue"
                )
                for key, count in self._calls.items()
            },
        }
