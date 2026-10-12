"""Deterministic observations of JSON, without promoting body claims to HTTP proof."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def observe_json(path: str, content: str, *, truncated: bool = False) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "path": path,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "hash_scope": "decoded_utf8_excerpt" if truncated else "decoded_utf8_content",
        "http_transport_verified": False,
    }
    if truncated:
        observation["parse_status"] = "not_parsed_truncated"
        return observation
    duplicates: list[str] = []

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    try:
        data = json.loads(content, object_pairs_hook=object_pairs)
    except (ValueError, RecursionError):
        observation["parse_status"] = "invalid_json"
        return observation
    if duplicates:
        observation.update(parse_status="ambiguous_duplicate_keys", duplicate_keys=sorted(set(duplicates)))
        return observation
    observation["parse_status"] = "valid_json"
    if not isinstance(data, dict):
        observation["root_type"] = type(data).__name__
        return observation
    observation["root_type"] = "object"
    observation["keys"] = sorted(data)
    fields: dict[str, Any] = {}
    for name in ("status_code", "code", "status"):
        if name not in data:
            fields[name] = {"presence": "missing"}
        else:
            value = data[name]
            fields[name] = {
                "presence": "present",
                "is_null": value is None,
                "value": value if isinstance(value, (str, int, float, bool, type(None))) else "<non-scalar>",
                "http_status_candidate": type(value) is int and 100 <= value <= 599,
            }
    observation["status_fields"] = fields
    return observation
