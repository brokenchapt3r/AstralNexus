"""Provider-neutral request budgeting and message/tool sanitization.

The runtime deliberately uses a conservative character estimate instead of
requiring a provider-specific tokenizer. Exact prompt-token usage is still
recorded from the provider response when available; this guard prevents
malformed or unexpectedly large OpenAI-compatible requests from reaching a
DashScope gateway unvalidated.
"""
from __future__ import annotations

import json
import os
from typing import Any

from AstralNexus.core.token_utils import estimate_provider_input_tokens
from AstralNexus.tools.schema_sanitizer import sanitize_schema


DEFAULT_INPUT_BUDGET = 900_000
DEFAULT_TOOL_BUDGET = 48_000


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _token_estimate(chars: int) -> int:
    # Keep request admission aligned with ContextCompressor's trigger estimate.
    return estimate_provider_input_tokens(chars)


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize OpenAI messages and remove invalid empty content entries."""
    cleaned: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        item: dict[str, Any] = {"role": role}
        content = _text(message.get("content"))
        # Tool messages must contain text for DashScope's OpenAI adapter.
        if role == "tool" and not content:
            content = "{}"
        if content:
            item["content"] = content
        elif role in {"system", "user"}:
            # An empty system/user message is not meaningful and can trigger
            # provider-side input-length validation failures.
            continue
        if role == "assistant" and message.get("tool_calls"):
            valid_calls = [call for call in message["tool_calls"] if isinstance(call, dict)]
            if valid_calls:
                item["tool_calls"] = valid_calls
        if role == "tool":
            for key in ("tool_call_id", "name"):
                if message.get(key):
                    item[key] = str(message[key])
        cleaned.append(item)
    if not any(item["role"] == "user" for item in cleaned):
        raise ValueError("LLM request must contain at least one non-empty user message")
    return cleaned


def sanitize_tools(tools: list[dict[str, Any]] | None, *, max_chars: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep only valid function schemas within a bounded serialized budget."""
    if not tools:
        return [], {"input_tools": 0, "output_tools": 0, "dropped_tools": []}
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    used = 0
    budget_exceeded = False
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            dropped.append(str(tool.get("function", {}).get("name", "unknown")) if isinstance(tool, dict) else "unknown")
            continue
        function = tool.get("function")
        if not isinstance(function, dict) or not str(function.get("name", "")).strip():
            dropped.append("unnamed")
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            dropped.append(str(function.get("name")))
            continue
        normalized_function = dict(function)
        normalized_function["parameters"] = sanitize_schema(parameters)
        normalized_tool = dict(tool)
        normalized_tool["function"] = normalized_function
        encoded = json.dumps(normalized_tool, ensure_ascii=False, separators=(",", ":"))
        # The input order is already relevance-ranked by tool_router.  Keep
        # that order and stop before the hard schema budget; never send a
        # request that merely records ``tool_budget_exceeded`` after keeping
        # the oversized schema anyway.
        if used + len(encoded) > max_chars:
            dropped.append(str(function.get("name")))
            budget_exceeded = True
            continue
        kept.append(normalized_tool)
        used += len(encoded)
    return kept, {"input_tools": len(tools), "output_tools": len(kept),
                   "dropped_tools": dropped, "tool_chars": used,
                   "tool_budget_exceeded": budget_exceeded}


def prepare_request(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    # Doc 17 第二批: project superseded write bodies out of the REQUEST COPY
    # before sanitization; originals (conversation/receipts/ledger) stay intact.
    from .request_budget import elide_superseded_write_bodies
    projected_messages, budget_stats = elide_superseded_write_bodies(messages)
    cleaned_messages = sanitize_messages(projected_messages)
    input_budget = max(1_000, int(os.environ.get("ASTRALNEXUS_INPUT_TOKEN_BUDGET", DEFAULT_INPUT_BUDGET)))
    tool_budget = max(1_000, int(os.environ.get("ASTRALNEXUS_TOOL_SCHEMA_CHAR_BUDGET", DEFAULT_TOOL_BUDGET)))
    cleaned_tools, tool_stats = sanitize_tools(tools, max_chars=tool_budget)
    message_chars = len(json.dumps(cleaned_messages, ensure_ascii=False, separators=(",", ":")))
    tool_chars = len(json.dumps(cleaned_tools, ensure_ascii=False, separators=(",", ":")))
    estimated_tokens = _token_estimate(message_chars + tool_chars)
    if estimated_tokens > input_budget:
        raise ValueError(
            f"LLM request estimated input {estimated_tokens} tokens exceeds budget {input_budget}; "
            "reduce context or tool schema before sending"
        )
    stats = {"message_count": len(cleaned_messages), "message_chars": message_chars,
             "tool_chars": tool_chars, "estimated_input_tokens": estimated_tokens,
             **tool_stats, **budget_stats}
    return cleaned_messages, cleaned_tools, stats
