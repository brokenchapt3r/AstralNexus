"""Provider-safe tool schema normalization.

DashScope and other OpenAI-compatible gateways are stricter than the Python
type hints used to author tools.  Keep the schema expressive, but normalize
recursive objects and remove unsupported metadata before serialization.
"""
from __future__ import annotations

from typing import Any


_ALLOWED_KEYS = {
    "type", "description", "properties", "required", "items", "enum",
    "additionalProperties", "minimum", "maximum", "minItems", "maxItems",
    "minLength", "maxLength", "pattern", "format", "default", "examples",
    "anyOf", "oneOf",
}


def sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded JSON-schema-compatible copy of *schema*."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 12:
            return {"type": "string", "description": "nested schema omitted"}
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value[:32]]
        if not isinstance(value, dict):
            return value
        result = {key: clean(item, depth + 1) for key, item in value.items()
                  if key in _ALLOWED_KEYS}
        if result.get("type") == "object":
            properties = result.get("properties")
            result["properties"] = clean(properties if isinstance(properties, dict) else {}, depth + 1)
            if "additionalProperties" not in result:
                result["additionalProperties"] = False
            required = result.get("required")
            if isinstance(required, list):
                result["required"] = [str(item) for item in required if isinstance(item, (str, int))]
        return result

    normalized = clean(schema)
    if normalized.get("type") != "object":
        normalized = {"type": "object", "properties": {"value": normalized}, "required": ["value"]}
    normalized.setdefault("properties", {})
    return normalized

