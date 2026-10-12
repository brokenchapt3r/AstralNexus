"""Bound presentation payloads without destroying tool-result semantics."""
from __future__ import annotations
import json
from typing import Any

_CONTROL_FIELDS = {"status", "success", "is_error", "failure_class", "error_type",
                   "next_action", "exit_code", "returncode", "return_code"}


def limit_result(result: Any, limit: int | None) -> Any:
    if limit is None or not isinstance(result, str) or len(result) <= limit:
        return result
    try:
        original = json.loads(result)
    except (ValueError, TypeError):
        return json.dumps({"status": "error", "failure_class": "tool_result_invalid_json",
                           "error": "Oversized tool result is not valid JSON"})
    if not isinstance(original, dict):
        return json.dumps({"status": "error", "failure_class": "tool_result_invalid_shape",
                           "error": "Oversized tool result must be an object"})

    def trim(value: Any, chars: int) -> Any:
        if isinstance(value, str):
            return value if len(value) <= chars else value[:chars] + "…"
        if isinstance(value, list):
            return [trim(item, chars) for item in value[:32]]
        if isinstance(value, dict):
            return {key: trim(item, chars) for key, item in list(value.items())[:32]}
        return value

    chars = limit // 4
    while True:
        bounded = trim(original, chars)
        for key in _CONTROL_FIELDS:
            if key in original:
                bounded[key] = original[key]
        if "error" in original:
            bounded["error"] = trim(original["error"], chars)
        bounded["truncated"] = True
        bounded["original_chars"] = len(result)
        encoded = json.dumps(bounded, ensure_ascii=False)
        if len(encoded) <= limit:
            return encoded
        if chars == 0:
            break
        chars //= 2
    # Limits smaller than a semantic envelope fail explicitly. Never slice
    # serialized JSON or manufacture successful status from unparseable data.
    return json.dumps({"status": "error", "failure_class": "tool_result_limit_too_small",
                       "error": "Result limit cannot retain the required control fields",
                       "truncated": True})
