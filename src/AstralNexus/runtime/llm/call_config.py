"""Conversation call configuration and freeze utilities.

Port of ``@deepseek-ai/dsh-llm`` lib/types/call-config.js (commit 5badb150).
Provider routing, model, reasoning effort, and sampling values are
request-header state that can affect cache reuse; request waterfalls replace
them and the loop logs changed snapshots instead of allowing silent per-call
drift.

Python adaptation (documented): the upstream process-local ``WeakSet`` holds
request objects weakly. Python dicts and MappingProxyType projections are not
weak-referenceable, so the identity set keeps a WeakSet for weak-referenceable
carriers and an id-keyed strong table for the rest (the strong table prevents
id-reuse false positives; loop requests are bounded per turn).
"""
from __future__ import annotations

import weakref
from typing import Any, Dict, Optional

__all__ = ["call_config_equals", "is_agent_loop_request",
           "mark_agent_loop_request"]


class _RequestIdentitySet:
    """Process-local identity set over request objects (WeakSet adaptation)."""

    __slots__ = ("_strong", "_weak")

    def __init__(self) -> None:
        self._weak: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._strong: Dict[int, Any] = {}

    def add(self, request: Any) -> None:
        try:
            self._weak.add(request)
        except TypeError:
            self._strong[id(request)] = request

    def __contains__(self, request: Any) -> bool:
        if request in self._weak:  # WeakSet answers False for unweakrefables
            return True
        return id(request) in self._strong


# Process-local identities of request objects assembled by dsh-agent-loop.
AGENT_LOOP_REQUESTS = _RequestIdentitySet()


def call_config_equals(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Field-wise equality over LlmCallConfig — the comparison a caller runs
    to decide whether a proposed configuration is a real change (worth a
    logged header snapshot) or the held one restated. Every field (including
    the ``stop`` list, element-wise) must match."""
    if a.get("provider") != b.get("provider") \
            or a.get("model") != b.get("model") \
            or a.get("reasoningEffort") != b.get("reasoningEffort") \
            or a.get("temperature") != b.get("temperature") \
            or a.get("maxTokens") != b.get("maxTokens"):
        return False
    a_stop = a.get("stop")
    b_stop = b.get("stop")
    if a_stop is None or b_stop is None:
        return a_stop is b_stop or (a_stop is None and b_stop is None)
    return len(a_stop) == len(b_stop) and all(
        s == b_stop[i] for i, s in enumerate(a_stop))


def mark_agent_loop_request(request: Any) -> Any:
    """Mark one exact request object as assembled by dsh-agent-loop."""
    AGENT_LOOP_REQUESTS.add(request)
    return request


def is_agent_loop_request(request: Any) -> bool:
    """Test whether the exact request object was assembled by dsh-agent-loop."""
    return request in AGENT_LOOP_REQUESTS
