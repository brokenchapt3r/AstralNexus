"""Shared provider/tool error classification for retry decisions."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    retryable: bool
    recovery_hint: str


def classify_http(status: int, body: str = "") -> ErrorClassification:
    text = (body or "").lower()
    if status in {401, 403}:
        return ErrorClassification("authentication", False, "check API key and provider access")
    if status == 429:
        return ErrorClassification("rate_limit", True, "exponential backoff or switch resource")
    if status == 400 and any(x in text for x in ("input length", "invalidparameter", "invalid_parameter")):
        return ErrorClassification("context_overflow", False, "compress history and reduce tool schemas")
    if status >= 500:
        return ErrorClassification("provider_server", True, "retry with backoff")
    return ErrorClassification("parameter", False, "inspect request schema and model parameters")


def classify_network(error: BaseException) -> ErrorClassification:
    return ErrorClassification("network", True, "retry with backoff; check TLS/proxy if persistent")

