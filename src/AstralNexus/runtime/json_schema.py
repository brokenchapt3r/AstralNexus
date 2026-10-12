"""Minimal JSON-Schema validator (native port, DSH B1: @deepseek-ai/dsh-json-schema).

Contract scope (ported subset, explicitly recorded):
- type: object/array/string/integer/number/boolean/null (single or list)
- required, properties, additionalProperties(bool), items
- enum, const, minimum, maximum, exclusiveMinimum, exclusiveMaximum
- minLength, maxLength, pattern
- anyOf, oneOf, allOf
Not covered (rejected at schema-load time so no silent pass): $ref, $defs,
definitions, dependencies, if/then/else, not, contains, patternProperties.

Validation errors carry a JSON-pointer style path so configuration rejection
messages name the exact offending field (DSH settings behavior).
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

UNSUPPORTED_KEYWORDS = frozenset({
    "$ref", "$defs", "definitions", "dependencies", "dependentRequired",
    "dependentSchemas", "if", "then", "else", "not", "contains",
    "patternProperties", "prefixItems", "$dynamicRef",
})

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class JsonSchemaValidationError(ValueError):
    """A schema or instance is invalid; ``path`` names the offending field.

    名字里的 ``Json`` 是刻意的，别改回 ``SchemaValidationError``：
    ``core/exceptions.SchemaValidationError`` 是**另一个概念**（结构化消息校验失败，
    ``CoreError`` 子类、带 ``severity``/``recovery_hint``，由错误分类处理消费）。
    两者曾同名而继承链无关 —— 于是 ``except CoreError`` 捕不到这里抛的错、
    ``except ValueError`` 捕不到那边抛的错，且不会报任何错。一名一概念。
    """

    def __init__(self, message: str, path: str = ""):
        self.path = path
        self.message = message
        super().__init__(f"{path or '<root>'}: {message}" if path else message)


def validate_schema(schema: Any, path: str = "<schema>") -> None:
    """Fail loud on unsupported or malformed schema keywords (no silent pass)."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise JsonSchemaValidationError("schema must be an object or boolean", path)
    unknown = UNSUPPORTED_KEYWORDS.intersection(schema)
    if unknown:
        raise JsonSchemaValidationError(
            f"unsupported schema keywords: {sorted(unknown)}", path)
    if "type" in schema:
        declared = schema["type"]
        names = declared if isinstance(declared, list) else [declared]
        for name in names:
            if name not in _TYPE_MAP and name not in ("integer", "number"):
                raise JsonSchemaValidationError(f"unknown type: {name!r}", path)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minLength", "maxLength"):
        if key in schema and not isinstance(schema[key], (int, float)):
            raise JsonSchemaValidationError(f"{key} must be a number", path)
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise JsonSchemaValidationError(f"pattern does not compile: {exc}", path)
    if "required" in schema:
        if not isinstance(schema["required"], list) or any(
                not isinstance(x, str) for x in schema["required"]):
            raise JsonSchemaValidationError("required must be a list of strings", path)
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise JsonSchemaValidationError("enum must be a list", path)
    for key in ("properties",):
        if key in schema:
            if not isinstance(schema[key], dict):
                raise JsonSchemaValidationError(f"{key} must be an object", path)
            for name, sub in schema[key].items():
                validate_schema(sub, f"{path}/{name}")
    if "items" in schema:
        validate_schema(schema["items"], f"{path}/items")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise JsonSchemaValidationError(f"{key} must be a non-empty list", path)
            for index, sub in enumerate(schema[key]):
                validate_schema(sub, f"{path}/{key}/{index}")


def _type_matches(instance: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    expected = _TYPE_MAP.get(name)
    if expected is None:
        return True
    if expected is str:
        return isinstance(instance, str)
    if expected is dict:
        return isinstance(instance, dict)
    if expected is list:
        return isinstance(instance, list)
    return isinstance(instance, expected) and not isinstance(instance, bool)


class _Collector:
    def __init__(self) -> None:
        self.errors: List[str] = []

    def add(self, path: str, message: str) -> None:
        self.errors.append(f"{path or '<root>'}: {message}")


def _validate(instance: Any, schema: Any, path: str, out: _Collector) -> None:
    if isinstance(schema, bool):
        if not schema:
            out.add(path, "value is rejected by the schema")
        return
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_matches(instance, name) for name in names):
            out.add(path, f"expected type {'|'.join(names)}, got {type(instance).__name__}")
            return
    if "const" in schema and instance != schema["const"]:
        out.add(path, f"value must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        out.add(path, f"value must be one of {schema['enum']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            out.add(path, f"value {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            out.add(path, f"value {instance} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            out.add(path, f"value {instance} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            out.add(path, f"value {instance} >= exclusiveMaximum {schema['exclusiveMaximum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            out.add(path, f"string shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            out.add(path, f"string longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            out.add(path, f"string does not match pattern {schema['pattern']!r}")
    if isinstance(instance, dict):
        for required in schema.get("required", []):
            if required not in instance:
                out.add(f"{path}/{required}" if path else required, "required property is missing")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                _validate(value, properties[name], f"{path}/{name}" if path else name, out)
            elif schema.get("additionalProperties") is False:
                out.add(f"{path}/{name}" if path else name, "additional property is not allowed")
    if isinstance(instance, list) and "items" in schema:
        for index, value in enumerate(instance):
            _validate(value, schema["items"], f"{path}/{index}", out)
    if "allOf" in schema:
        for sub in schema["allOf"]:
            _validate(instance, sub, path, out)
    if "anyOf" in schema:
        probe = _Collector()
        matched = False
        for sub in schema["anyOf"]:
            trial = _Collector()
            _validate(instance, sub, path, trial)
            if not trial.errors:
                matched = True
                break
            probe.errors.extend(trial.errors)
        if not matched:
            out.add(path, "value does not match any of the anyOf branches")
    if "oneOf" in schema:
        matches = 0
        for sub in schema["oneOf"]:
            trial = _Collector()
            _validate(instance, sub, path, trial)
            if not trial.errors:
                matches += 1
        if matches != 1:
            out.add(path, f"value must match exactly one oneOf branch (matched {matches})")


def validate(instance: Any, schema: Any) -> None:
    """Validate ``instance`` against ``schema``; raise JsonSchemaValidationError.

    The error message lists every violation with its field path (first 20).
    """
    validate_schema(schema)
    collector = _Collector()
    _validate(instance, schema, "", collector)
    if collector.errors:
        shown = collector.errors[:20]
        more = f" (+{len(collector.errors) - len(shown)} more)" if len(collector.errors) > len(shown) else ""
        raise JsonSchemaValidationError("; ".join(shown) + more)


def is_valid(instance: Any, schema: Any) -> bool:
    try:
        validate(instance, schema)
        return True
    except JsonSchemaValidationError:
        return False


def apply_defaults(instance: Any, schema: Any) -> Any:
    """Return a copy of ``instance`` with schema-declared defaults filled in.

    Only object properties with a ``default`` keyword are filled; existing
    values are never overwritten. Unsupported keywords already rejected by
    validate_schema, so defaults cannot hide behind $ref indirection.
    """
    if isinstance(schema, bool) or not isinstance(schema, dict):
        return instance
    if isinstance(instance, dict) and isinstance(schema.get("properties"), dict):
        result = dict(instance)
        for name, sub in schema["properties"].items():
            if name not in result and isinstance(sub, dict) and "default" in sub:
                result[name] = sub["default"]
            elif name in result:
                result[name] = apply_defaults(result[name], sub)
        return result
    if isinstance(instance, list) and "items" in schema:
        return [apply_defaults(value, schema["items"]) for value in instance]
    return instance
