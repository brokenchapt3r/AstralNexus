"""Message value types, identity, and immutable construction helpers.

Port of ``@deepseek-ai/dsh-llm`` lib/types/message.js (commit 5badb150).

Python adaptation (documented): ``structuredClone`` becomes a recursive
detach that reverses the ``deep_freeze`` projection (MappingProxyType→dict,
tuple→list) so already-frozen messages clone instead of raising.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Dict, List

from ..dshutil.brand import brand_string
from ..dshutil.crypto_util import random_uuid
from ..dshutil.values import deep_freeze

__all__ = ["CONTEXT_SUMMARY_MAX_CHARS", "bound_context_summary",
           "create_assistant_message", "create_developer_message",
           "create_message", "create_system_message",
           "create_tool_result_message", "create_user_message",
           "freeze_message"]

# Bound for a `notice` summary. Producers commit the one-line account to the
# durable log; its inputs — task labels, goal objectives, tool arguments —
# are caller text with no length of their own.
CONTEXT_SUMMARY_MAX_CHARS = 120


def bound_context_summary(summary: str) -> str:
    """Bound one notice summary to CONTEXT_SUMMARY_MAX_CHARS, ellipsized when
    it exceeds the bound."""
    if len(summary) <= CONTEXT_SUMMARY_MAX_CHARS:
        return summary
    return summary[:CONTEXT_SUMMARY_MAX_CHARS - 1] + "\u2026"


def _detach(value: Any) -> Any:
    """structuredClone adaptation: recursive isolated copy that also reverses
    the deep_freeze projection (frozen inputs clone instead of raising)."""
    if isinstance(value, (MappingProxyType, dict)):
        return {name: _detach(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detach(item) for item in value]
    return value


def freeze_message(message: Dict[str, Any]) -> Any:
    """Detach and deep-freeze a message whose identity already exists."""
    return deep_freeze(_detach(message))


def create_message(input: Dict[str, Any]) -> Any:
    """Create one identified message and freeze it before publication."""
    return deep_freeze(_detach({**input, "id": brand_string(random_uuid())}))


def create_developer_message(input: Dict[str, Any]) -> Any:
    """Create an identified, immutable developer message."""
    return create_message({**input, "role": "developer"})


def create_user_message(input: Dict[str, Any]) -> Any:
    """Create one identified user-role message and freeze it before
    publication."""
    return create_message({**input, "role": "user"})


def create_assistant_message(input: Dict[str, Any]) -> Any:
    """Create one identified model-produced assistant message and freeze it
    before publication."""
    return create_message({
        "role": "assistant",
        "content": input["content"],
        "source": {"kind": "model", **input["source"]},
    })


def create_system_message(text: str) -> Any:
    """Create and freeze one identified system-role message holding a
    rendered system prompt; ``''`` records "no system prompt"."""
    content: List[Dict[str, Any]] = [] if len(text) == 0 \
        else [{"type": "text", "text": text}]
    return create_message({
        "role": "system",
        "content": content,
        "source": {"kind": "system-prompt"},
    })


def create_tool_result_message(input: Dict[str, Any]) -> Any:
    """Create and freeze one identified tool-result message."""
    return create_message({
        "role": "tool",
        "source": {"kind": "tool", "callId": input["callId"]},
        "toolCallId": input["callId"],
        "content": input["content"],
        "isError": input["isError"],
    })
