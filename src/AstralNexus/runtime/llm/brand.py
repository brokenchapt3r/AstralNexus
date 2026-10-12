"""dsh-llm's owned branded ids: tool-call correlation and provider request
diagnostics.

Port of ``@deepseek-ai/dsh-llm`` lib/types/brand.js (commit 5badb150). The
``Branded<B>`` primitive and stateless constructor live in
``@deepseek-ai/dsh-brand`` (ported at ``runtime.dshutil.brand``) so every
owner of a cross-boundary id can brand it without depending on dsh-llm.
"""
from __future__ import annotations

from typing import Any

from ..dshutil.brand import brand_string

__all__ = ["LlmAttemptId", "MessageId", "ProviderRequestId",
           "ReasoningEffortId", "ToolCallId"]


def MessageId(id: Any) -> str:  # noqa: N802 — upstream export surface
    """Brand a message identifier."""
    return brand_string(id)


def ToolCallId(id: Any) -> str:  # noqa: N802 — upstream export surface
    """Brand a string as a ToolCallId (provider-issued or synthesized)."""
    return brand_string(id)


def ProviderRequestId(id: Any) -> str:  # noqa: N802
    """Brand a provider-issued request identifier; no validation."""
    return brand_string(id)


def LlmAttemptId(id: Any) -> str:  # noqa: N802
    """Brand one loop-owned streaming attempt identifier."""
    return brand_string(id)


def ReasoningEffortId(id: Any) -> str:  # noqa: N802
    """Brand an adapter-owned reasoning-effort identifier; no validation."""
    return brand_string(id)
