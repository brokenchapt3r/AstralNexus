"""异常名唯一性守护（2026-10-08）。

起因：`core/exceptions.SchemaValidationError(CoreError)` 与
`runtime/json_schema.SchemaValidationError(ValueError)` **同名而继承链无关**。
后果是静默的：`except CoreError` 捕不到后者抛的错，`except ValueError` 捕不到前者，
而且两者都正常导入、正常运行 —— 没有任何报错提示谁捕错了。

本条把不变量钉住：**框架内不允许"同名异常类、不同基类"**。
只比基类集合的差异，避免把"同名同基类"（可接受）也误判。
"""
from __future__ import annotations

import ast
from pathlib import Path

FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]   # .../AstralNexus
SCAN_DIRS = ("core", "runtime", "agents", "tools", "cli", "dashboard")
EXCLUDE = ("skills/vendor", "skills/public", "__pycache__", "/tests/", "/test_")


def _exception_classes() -> dict[str, dict[tuple[str, ...], list[str]]]:
    """name -> {base_tuple: [locations]}"""
    found: dict[str, dict[tuple[str, ...], list[str]]] = {}
    for directory in SCAN_DIRS:
        for path in (FRAMEWORK_ROOT / directory).rglob("*.py"):
            rel = str(path).replace("\\", "/")
            if any(token in rel for token in EXCLUDE):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not (node.name.endswith("Error") or node.name.endswith("Exception")):
                    continue
                bases = tuple(
                    getattr(base, "id", getattr(base, "attr", ""))
                    for base in node.bases
                )
                where = "%s:%d" % (path.relative_to(FRAMEWORK_ROOT), node.lineno)
                found.setdefault(node.name, {}).setdefault(bases, []).append(where)
    return found


def test_no_two_framework_exceptions_share_a_name_with_different_bases():
    offenders = {
        name: variants for name, variants in _exception_classes().items()
        if len(variants) > 1
    }
    assert offenders == {}, (
        "同名异常类却有不同基类（捕获会静默分叉）："
        + "; ".join(
            "%s -> %s" % (name, {bases: locs for bases, locs in variants.items()})
            for name, variants in offenders.items()
        )
    )


def test_the_two_schema_validation_errors_are_distinct_concepts():
    """钉住本次修复：两者名字不同、概念不同、继承链不同。"""
    from AstralNexus.core.exceptions import CoreError
    from AstralNexus.core.exceptions import SchemaValidationError as MessageSchemaError
    from AstralNexus.runtime.json_schema import JsonSchemaValidationError

    assert MessageSchemaError is not JsonSchemaValidationError
    # 消息 schema 错误属于框架错误分类（带 severity / recovery_hint）
    assert issubclass(MessageSchemaError, CoreError)
    assert MessageSchemaError.severity.value == "recoverable"
    assert MessageSchemaError.recovery_hint
    # JSON schema 错误是通用校验错误，**不属于**框架错误分类
    assert issubclass(JsonSchemaValidationError, ValueError)
    assert not issubclass(JsonSchemaValidationError, CoreError)


def test_plugin_host_catches_the_json_schema_error_it_imports():
    """plugin_host 捕获的必须是它导入的那一个，不能是另一个同名类。"""
    from AstralNexus.runtime import plugin_host
    from AstralNexus.runtime.json_schema import JsonSchemaValidationError

    assert plugin_host.JsonSchemaValidationError is JsonSchemaValidationError