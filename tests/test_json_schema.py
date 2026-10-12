"""Contract tests for the native JSON-Schema subset validator (dsh-json-schema
port). Failures must name the offending field path; unsupported keywords must
be rejected at schema-load time instead of silently passing instances.
"""
from __future__ import annotations

import pytest

from AstralNexus.runtime.json_schema import (
    JsonSchemaValidationError, apply_defaults, is_valid, validate, validate_schema)


def test_scalar_types_and_ranges():
    validate(3, {"type": "integer"})
    validate("x", {"type": ["string", "null"]})
    validate(None, {"type": ["string", "null"]})
    with pytest.raises(JsonSchemaValidationError, match=r"retries: expected type integer, got bool"):
        validate({"retries": True}, {
            "type": "object", "properties": {"retries": {"type": "integer"}}})
    with pytest.raises(JsonSchemaValidationError, match=r"n: value 9 > maximum 5"):
        validate({"n": 9}, {"type": "object", "properties": {"n": {"type": "number", "maximum": 5}}})
    assert is_valid({"n": 1}, {"type": "object", "properties": {"n": {"minimum": 0, "maximum": 2}}})
    assert not is_valid(1.5, {"type": "integer"})


def test_object_structure_required_and_additional():
    schema = {"type": "object", "required": ["id"], "additionalProperties": False,
              "properties": {"id": {"type": "string", "minLength": 1}}}
    validate({"id": "a"}, schema)
    with pytest.raises(JsonSchemaValidationError, match=r"id: required property is missing"):
        validate({}, schema)
    with pytest.raises(JsonSchemaValidationError, match=r"extra: additional property is not allowed"):
        validate({"id": "a", "extra": 1}, schema)
    with pytest.raises(JsonSchemaValidationError, match=r"id: string shorter than minLength"):
        validate({"id": ""}, schema)


def test_arrays_items_enum_pattern_composites():
    validate({"tags": ["a", "b"]}, {"type": "object", "properties": {
        "tags": {"type": "array", "items": {"type": "string", "pattern": "^[a-z]$"}}}})
    with pytest.raises(JsonSchemaValidationError, match=r"tags/1: string does not match pattern"):
        validate({"tags": ["a", "B"]}, {"type": "object", "properties": {
            "tags": {"type": "array", "items": {"type": "string", "pattern": "^[a-z]$"}}}})
    with pytest.raises(JsonSchemaValidationError, match="one of"):
        validate("c", {"enum": ["a", "b"]})
    validate({"v": 1}, {"oneOf": [{"required": ["v"]}, {"required": ["w"]}]})
    with pytest.raises(JsonSchemaValidationError, match="exactly one oneOf"):
        validate({"v": 1, "w": 2}, {"oneOf": [{"required": ["v"]}, {"required": ["w"]}]})
    validate(1, {"anyOf": [{"type": "string"}, {"type": "integer"}]})
    with pytest.raises(JsonSchemaValidationError, match="anyOf"):
        validate(None, {"anyOf": [{"type": "string"}, {"type": "integer"}]})


def test_unsupported_keywords_are_rejected_not_ignored():
    for keyword in ("$ref", "definitions", "not", "if", "patternProperties"):
        with pytest.raises(JsonSchemaValidationError, match="unsupported schema keywords"):
            validate_schema({"type": "object", keyword: {}})
    with pytest.raises(JsonSchemaValidationError, match="pattern does not compile"):
        validate_schema({"pattern": "([a-z"})
    with pytest.raises(JsonSchemaValidationError, match="required must be a list of strings"):
        validate_schema({"required": "id"})


def test_defaults_fill_missing_without_overwriting():
    schema = {"type": "object", "properties": {
        "mode": {"type": "string", "default": "fast"},
        "nested": {"type": "object", "properties": {"depth": {"type": "integer", "default": 2}}},
        "list": {"type": "array", "items": {"type": "object", "properties": {"ok": {"type": "boolean", "default": True}}}},
    }}
    assert apply_defaults({}, schema) == {"mode": "fast"}
    assert apply_defaults({"mode": "slow"}, schema) == {"mode": "slow"}
    # Defaults fill recursively; a present sub-object still gets its own defaults.
    assert apply_defaults({"nested": {}}, schema) == {"mode": "fast", "nested": {"depth": 2}}
    assert apply_defaults({"list": [{}]}, schema) == {"mode": "fast", "list": [{"ok": True}]}
