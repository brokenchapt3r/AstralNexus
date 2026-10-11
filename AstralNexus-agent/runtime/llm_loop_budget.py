"""Finite, shared session limits for synchronous and streamed tool loops."""
from __future__ import annotations
from AstralNexus.runtime.run_context import get_env
import math
import os
import time
from dataclasses import dataclass
from typing import Any


class LLMLoopBudgetExceeded(RuntimeError):
    pass


def _positive(name: str, default: float) -> float:
    value = float(get_env(name, str(default)))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value or default


@dataclass
class LLMLoopBudget:
    max_rounds: int
    max_requests: int
    max_tokens: int
    deadline: float
    requests: int = 0
    tokens: int = 0
    request_tokens: int = 0

    @classmethod
    def from_environment(cls, rounds: int | None, retries: int = 0) -> LLMLoopBudget:
        cap = int(_positive("ASTRALNEXUS_MAX_TOOL_ROUNDS", 60)) if rounds is None else max(0, int(rounds))
        requests = int(_positive("ASTRALNEXUS_MAX_LLM_REQUESTS", (cap + 1) * (max(0, retries) + 1) + 1))
        tokens = int(_positive("ASTRALNEXUS_MAX_SESSION_TOKENS", 1_000_000))
        seconds = _positive("ASTRALNEXUS_NODE_TIMEOUT", 1800)
        if min(requests, tokens) < 1:
            raise ValueError("LLM request and token limits must be at least one")
        return cls(cap, requests, tokens, time.monotonic() + seconds)

    def admit_request(self, tool_round: int) -> None:
        dimension = ("wall_clock" if time.monotonic() >= self.deadline else
                     "requests" if self.requests >= self.max_requests else
                     "tool_rounds" if tool_round > self.max_rounds else
                     "tokens" if self.tokens >= self.max_tokens else "")
        if dimension:
            raise LLMLoopBudgetExceeded(f"LLM session budget exhausted: {dimension}")
        self.requests += 1
        self.request_tokens = 0

    def consume_usage(self, usage: dict[str, Any]) -> None:
        observed = max(0, int(usage.get("total_tokens") or
            (int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))))
        self.tokens += max(0, observed - self.request_tokens)
        self.request_tokens = max(self.request_tokens, observed)
        if self.tokens > self.max_tokens:
            raise LLMLoopBudgetExceeded("LLM session budget exhausted: tokens")
