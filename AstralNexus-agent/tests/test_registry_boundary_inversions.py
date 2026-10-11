"""Counterexamples for result semantics, availability and discovery atomicity."""
import importlib
import json
import threading
from pathlib import Path
from typing import Any
import pytest
from AstralNexus.tools.registry import ToolRegistry
from AstralNexus.tools.executor import ToolExecutor

module = importlib.import_module("AstralNexus.tools.registry")
executor_module = importlib.import_module("AstralNexus.tools.executor")


def register(reg: ToolRegistry, name: str = "probe", **kwargs: Any) -> None:
    reg.register(name, "test", {"name": name, "parameters": {"type": "object"}},
                 kwargs.pop("handler", lambda args: json.dumps({"status": "success"})), **kwargs)


def execute(reg: ToolRegistry, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(executor_module, "registry", reg)
    runtime = ToolExecutor.__new__(ToolExecutor)
    runtime.trace_recorder = None
    return runtime._dispatch_registry_tool("probe", reg.get_entry("probe"), {})


@pytest.mark.parametrize("status", ["error", "failed", "success"])
def test_large_json_remains_parseable_and_preserves_status(monkeypatch: Any, status: str) -> None:
    reg = ToolRegistry()
    register(reg, handler=lambda args: json.dumps({"status": status, "payload": "字" * 10000}),
             max_result_size_chars=256)
    raw = reg.dispatch("probe", {})
    assert len(raw) <= 256
    assert json.loads(raw)["status"] == status
    assert json.loads(raw)["truncated"] is True
    result = execute(reg, monkeypatch)
    assert result["status"] == status and result["truncated"] is True
    if status != "success":
        assert result["failure_class"] == "tool_execution_failed"


def test_malformed_result_never_becomes_success(monkeypatch: Any) -> None:
    reg = ToolRegistry()
    register(reg, handler=lambda args: '{"status":"error",')
    result = execute(reg, monkeypatch)
    assert result["status"] == "error" and "invalid JSON" in result["error"]


def test_small_limit_fails_as_valid_json_and_preserves_false_success() -> None:
    reg = ToolRegistry()
    register(reg, handler=lambda args: json.dumps({"success": False, "error": "bad" * 1000}),
             max_result_size_chars=128)
    result = json.loads(reg.dispatch("probe", {}))
    assert result.get("success") is False
    reg.register("probe", "test", {}, lambda args: json.dumps({"status": "error", "body": "x" * 100}),
                 max_result_size_chars=1, override=True)
    assert json.loads(reg.dispatch("probe", {}))["status"] == "error"


@pytest.mark.parametrize("raw", ["not-json", "{}", '"test"', '[1]', "null"])
def test_invalid_disabled_configuration_is_diagnostic_not_schema_crash(monkeypatch: Any,
                                                                     caplog: Any, raw: str) -> None:
    reg = ToolRegistry()
    register(reg)
    monkeypatch.setenv("ASTRALNEXUS_DISABLED_TOOLSETS", raw)
    monkeypatch.setattr(module, "_disabled_raw", None)
    assert not reg.is_tool_available("probe")
    assert not reg.is_toolset_available("test")
    assert reg.get_tools_schema() == []
    assert reg.get_tools_schema() == []
    assert len([record for record in caplog.records if "Invalid ASTRALNEXUS_DISABLED_TOOLSETS" in record.message]) == 1
    monkeypatch.setenv("ASTRALNEXUS_DISABLED_TOOLSETS", "[]")
    assert reg.is_tool_available("probe")


def test_toolset_uses_current_members_requirements_and_aliases(monkeypatch: Any) -> None:
    reg = ToolRegistry()
    register(reg, requires_env=["ASTRAL_REGISTRY_REQUIRED"], check_fn=lambda: False)
    monkeypatch.delenv("ASTRAL_REGISTRY_REQUIRED", raising=False)
    assert not reg.is_toolset_available("test")
    register(reg, override=True, check_fn=lambda: True)
    assert reg.is_tool_available("probe") and reg.is_toolset_available("test")
    reg.register_toolset_alias("alias", "test")
    assert reg.is_toolset_available("alias")
    assert reg.get_tool_names_for_toolset("alias") == ["probe"]
    monkeypatch.setenv("ASTRALNEXUS_DISABLED_TOOLSETS", '["alias"]')
    assert not reg.is_tool_available("probe") and not reg.is_toolset_available("test")
    monkeypatch.setenv("ASTRALNEXUS_DISABLED_TOOLSETS", "[]")
    register(reg, "other")
    reg.deregister("probe")
    assert reg.is_toolset_available("test")
    reg.deregister("other")
    assert not reg.is_toolset_available("test")
    with pytest.raises(ValueError, match="cycle"):
        reg.register_toolset_alias("test", "alias")


def test_async_handlers_rejected_and_unexpected_coroutines_closed(monkeypatch: Any) -> None:
    reg = ToolRegistry()
    async def handler(args: Any) -> str:
        return json.dumps({"status": "success"})
    with pytest.raises(ValueError, match="Asynchronous"):
        register(reg, handler=handler)
    with pytest.raises(ValueError, match="Asynchronous"):
        register(reg, is_async=True)
    register(reg, handler=lambda args: handler(args))
    assert execute(reg, monkeypatch)["status"] == "error"


def test_probe_single_flight_for_eight_callers() -> None:
    module.invalidate_check_fn_cache()
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(9)
    calls: list[int] = []
    results: list[bool] = []
    def probe() -> bool:
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return True
    def call() -> None:
        barrier.wait()
        results.append(module._check_fn_cached(probe))
    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    assert entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert calls == [1] and results == [True] * 8


def test_cache_invalidation_does_not_publish_old_probe() -> None:
    module.invalidate_check_fn_cache()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    def probe() -> bool:
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return True
        return False
    first = threading.Thread(target=lambda: module._check_fn_cached(probe))
    first.start()
    assert entered.wait(1)
    module.invalidate_check_fn_cache()
    release.set()
    first.join(2)
    assert module._check_fn_cached(probe) is False
    assert calls == [1, 1]


def test_duplicate_and_detached_metadata_cannot_change_policy() -> None:
    reg = ToolRegistry()
    register(reg)
    register(reg, requires_approval=True, handler=lambda args: "replacement")
    entry = reg.get_entry("probe")
    assert entry is not None and not entry.requires_approval
    entry.requires_approval = True
    entry.schema["parameters"]["type"] = "array"
    entry.requires_env.append("MISSING")
    actual = reg.get_entry("probe")
    assert actual is not None and not actual.requires_approval
    assert actual.schema["parameters"]["type"] == "object" and actual.requires_env == []
    register(reg, override=True, requires_approval=True)
    assert reg.get_entry("probe").requires_approval  # type: ignore[union-attr]


def test_discovery_failure_rolls_back_and_custom_path_is_explicitly_rejected(monkeypatch: Any,
                                                                          tmp_path: Path) -> None:
    reg = ToolRegistry()
    monkeypatch.setattr(module, "registry", reg)
    monkeypatch.setattr(module, "_module_registers_tools", lambda path: path.stem == "builtin_tools")
    def fail(name: str) -> None:
        register(reg)
        assert reg.get_entry("probe") is not None  # importing thread sees staged entry
        raise RuntimeError("partial import")
    monkeypatch.setattr(module.importlib, "import_module", fail)
    assert module.discover_builtin_tools() == []
    assert reg.get_all_tool_names() == []
    with pytest.raises(ValueError, match="builtin package"):
        module.discover_builtin_tools(tmp_path)


def test_registration_transaction_is_invisible_and_detects_concurrent_update() -> None:
    reg = ToolRegistry()
    with pytest.raises(RuntimeError, match="changed during discovery"):
        with reg.registration_transaction():
            register(reg, "staged")
            def concurrent() -> None:
                assert "staged" not in reg.get_all_tool_names()
                register(reg, "other")
            thread = threading.Thread(target=concurrent)
            thread.start()
            thread.join(2)
            assert not thread.is_alive()
    assert reg.get_all_tool_names() == ["other"]


def test_detached_entry_does_not_bypass_executor_approval(monkeypatch: Any) -> None:
    from types import SimpleNamespace
    reg = ToolRegistry()
    called: list[bool] = []
    register(reg, requires_approval=True, handler=lambda args: called.append(True))
    entry = reg.get_entry("probe")
    assert entry is not None
    entry.requires_approval = False
    monkeypatch.setattr(executor_module, "registry", reg)
    monkeypatch.setattr(module, "request_approval", lambda *args, **kwargs:
                        SimpleNamespace(status="denied", to_dict=lambda: {"status": "denied"}))
    runtime = ToolExecutor.__new__(ToolExecutor)
    runtime.trace_recorder = None
    assert runtime._dispatch_registry_tool("probe", entry, {})["status"] == "denied"
    assert called == []


def test_successful_transaction_publishes_and_probe_error_is_visible(caplog: Any) -> None:
    reg = ToolRegistry()
    def probe() -> bool:
        raise OSError("local probe unavailable")
    with reg.registration_transaction():
        register(reg, check_fn=probe)
    assert reg.get_all_tool_names() == ["probe"]
    assert not reg.is_toolset_available("test")
    assert any("local probe unavailable" in record.message for record in caplog.records)
