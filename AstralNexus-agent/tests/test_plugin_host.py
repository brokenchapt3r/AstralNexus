"""B1 core loop: native plugin host lifecycle, offline and deterministic.

Covers the doc-48 acceptance line: 插件启停/依赖缺失与环检测/配置持久化/
加载失败隔离/重启状态一致；只读清单不隐式启动插件。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import textwrap

import pytest

from AstralNexus.runtime.plugin_host import (
    BUILTIN_MANIFESTS, DependencyResolver, PluginError, PluginHost, PluginManifest)
from AstralNexus.runtime.workbench import WorkbenchStore


def host(tmp_path) -> PluginHost:
    return PluginHost(WorkbenchStore(tmp_path / "host"))


def manifest(ident, **extra):
    raw = {"id": ident, "name": ident.upper(), "version": "1.0.0", "kind": "gate", **extra}
    raw = {key: value for key, value in raw.items() if value is not None}
    return raw


NATIVE_ENTRY = textwrap.dedent("""
    from pathlib import Path
    CALLS = []
    def start(ctx):
        marker = Path(ctx.home) / ('started-' + ctx.plugin.id)
        marker.write_text(json.dumps(ctx.config) if False else str(len(CALLS)), encoding='utf-8')
        CALLS.append(('start', ctx.plugin.id, dict(ctx.config)))
        return {'pid_marker': str(marker)}
    def stop(ctx, handle):
        CALLS.append(('stop', ctx.plugin.id, handle))
        Path(ctx.home, 'stopped-' + ctx.plugin.id).write_text('1', encoding='utf-8')
""").replace("json.dumps(ctx.config) if False else ", "")

FAILING_ENTRY = textwrap.dedent("""
    def start(ctx):
        raise RuntimeError('entry exploded on purpose')
    def stop(ctx, handle):
        pass
""")


@pytest.fixture()
def native_modules(tmp_path):
    """Real importable entry modules on sys.path; cleaned up afterwards."""
    modules = Path(tmp_path / "entry_modules")
    modules.mkdir()
    (modules / "anx_plugin_ok.py").write_text(
        "import json\n" + NATIVE_ENTRY.replace("str(len(CALLS))", "json.dumps(ctx.config)"),
        encoding="utf-8")
    (modules / "anx_plugin_bad.py").write_text(FAILING_ENTRY, encoding="utf-8")
    sys.path.insert(0, str(modules))
    yield modules
    sys.path.remove(str(modules))
    for name in ("anx_plugin_ok", "anx_plugin_bad"):
        sys.modules.pop(name, None)


# ----------------------------------------------------------------- manifests


def test_manifest_validation_fails_loud():
    with pytest.raises(PluginError, match="manifest id"):
        PluginManifest({"id": "Bad ID", "name": "x", "version": "1"})
    with pytest.raises(PluginError, match="native plugins must declare an entry"):
        PluginManifest({"id": "ok", "name": "x", "version": "1", "kind": "native"})
    with pytest.raises(PluginError, match="cannot depend on itself"):
        PluginManifest({"id": "ok", "name": "x", "version": "1", "dependencies": ["ok"]})
    with pytest.raises(PluginError, match="kind must be one of"):
        PluginManifest({"id": "ok", "name": "x", "version": "1", "kind": "magic"})
    with pytest.raises(PluginError, match="only native plugins declare an entry"):
        PluginManifest({"id": "ok", "name": "x", "version": "1", "kind": "gate", "entry": "mod"})
    with pytest.raises(PluginError, match="config_schema is invalid"):
        PluginManifest({"id": "ok", "name": "x", "version": "1",
                        "config_schema": {"type": "object", "$ref": "#/nope"}})


def test_declaration_never_starts_and_inventory_is_read_only(tmp_path):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("alpha"), source="test", desired="enabled")
    plugins.install_manifest(manifest("beta", dependencies=["alpha"]), source="test", desired="enabled")
    # Read-only projection: desired=enabled but nothing reconciled yet.
    for _ in range(2):
        items = {item["id"]: item for item in plugins.inventory()}
        assert items["alpha"]["phase"] == "disposed"
        assert items["beta"]["phase"] == "disposed"
        assert items["alpha"]["desired"] == "enabled"
    assert not plugins.is_active("alpha")
    assert plugins.snapshot()[0]["status"] == "stopped"


# ----------------------------------------------------------------- resolver


def test_dependency_chain_order_and_cascade(tmp_path):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("base"), source="test")
    plugins.install_manifest(manifest("mid", dependencies=["base"]), source="test")
    plugins.install_manifest(manifest("top", dependencies=["mid"]), source="test")
    result = plugins.set_enabled("top", True)
    # Enabling a leaf pulls in its whole dependency chain (loader semantics).
    assert set(result["affected"]) >= {"base", "mid", "top"}
    for ident in ("base", "mid", "top"):
        assert plugins.is_active(ident), ident
    stopped = plugins.set_enabled("base", False)
    # Disabling a parent cascades to dependents and names every affected id.
    assert set(stopped["affected"]) == {"base", "mid", "top"}
    for ident in ("base", "mid", "top"):
        assert not plugins.is_active(ident)
    # Dependents keep their desired state and wait (restart-consistent).
    items = {item["id"]: item for item in plugins.inventory()}
    assert items["mid"]["desired"] == "enabled"
    assert items["mid"]["phase"] == "pending"
    assert "dependency_disabled" in (items["mid"]["last_error"] or "")
    # Re-enabling the parent converges the whole chain back to active.
    plugins.set_enabled("base", True)
    for ident in ("base", "mid", "top"):
        assert plugins.is_active(ident), ident


def test_missing_dependency_and_cycle_are_named_not_swallowed(tmp_path):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("orphan", dependencies=["ghost"]), source="test")
    plugins.set_enabled("orphan", True)
    state = {item["id"]: item for item in plugins.inventory()}["orphan"]
    assert state["phase"] == "failed"
    assert "missing_dependency" in state["last_error"] and "ghost" in state["last_error"]

    plugins.install_manifest(manifest("loop-a", dependencies=["loop-b"]), source="test")
    plugins.install_manifest(manifest("loop-b", dependencies=["loop-a"]), source="test")
    manifests = {m["id"]: PluginManifest(m) for m in
                 [manifest("loop-a", dependencies=["loop-b"]), manifest("loop-b", dependencies=["loop-a"])]}
    with pytest.raises(PluginError, match=r"dependency cycle detected: loop-a -> loop-b -> loop-a"):
        DependencyResolver(manifests).order(["loop-a"])
    # Recovery isolates the broken chain: enabling loop-a fails it honestly
    # without wedging the resolver for healthy plugins.
    plugins.set_enabled("loop-a", True)
    state = {item["id"]: item for item in plugins.inventory()}["loop-a"]
    assert state["phase"] == "failed"
    assert "missing_dependency" in state["last_error"] or "cycle" in state["last_error"]


# --------------------------------------------------------- config validation


def test_config_schema_validation_persistence_and_defaults(tmp_path):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest(
        "configured", config_schema={
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "pattern": "^https?://", "default": "http://127.0.0.1:1"},
                "retries": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
            },
            "additionalProperties": False,
        }), source="test")
    with pytest.raises(PluginError, match=r"config_invalid.*endpoint"):
        plugins.put_config("configured", {"endpoint": "ftp://x", "retries": 9})
    with pytest.raises(PluginError, match=r"config_invalid.*additional property"):
        plugins.put_config("configured", {"surprise": 1})
    result = plugins.put_config("configured", {"endpoint": "https://example.internal"})
    assert result["config"] == {"endpoint": "https://example.internal", "retries": 2}
    assert result["applied"] is False  # not active: nothing to restart
    # Persisted across a fresh host instance on the same store.
    reopened = PluginHost(WorkbenchStore(tmp_path / "host"))
    assert reopened.get_config("configured")["config"]["endpoint"] == "https://example.internal"
    # Invalid stored config blocks start with a concrete reason (isolation).
    with reopened.store.db() as db:
        db.execute("UPDATE plugin_config SET config=? WHERE id='configured'",
                   (json.dumps({"endpoint": "https://ok", "retries": "many"}),))
    reopened.set_enabled("configured", True)
    state = {item["id"]: item for item in reopened.inventory()}["configured"]
    assert state["phase"] == "failed"
    assert "config_invalid" in state["last_error"] and "retries" in state["last_error"]


# ------------------------------------------------------- native lifecycle


def test_native_start_stop_and_failure_isolation(tmp_path, native_modules):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("good", kind="native", entry="anx_plugin_ok",
                                      config_schema={"type": "object",
                                                     "properties": {"mode": {"type": "string", "default": "fast"}}}),
                             source="test")
    plugins.install_manifest(manifest("bad", kind="native", entry="anx_plugin_bad"), source="test")
    plugins.set_enabled("bad", True)
    plugins.set_enabled("good", True)
    items = {item["id"]: item for item in plugins.inventory()}
    # The exploding entry is contained: failed with a concrete reason…
    assert items["bad"]["phase"] == "failed"
    assert "load_failed" in items["bad"]["last_error"] and "entry exploded" in items["bad"]["last_error"]
    # …while the healthy plugin is active with resolved defaults and a handle.
    assert items["good"]["phase"] == "active"
    assert items["good"]["running_version"] == "1.0.0"
    assert (tmp_path / "host" / "started-good").exists()
    import anx_plugin_ok  # type: ignore[import-not-found]  # synthesized plugin module
    assert anx_plugin_ok.CALLS[-1][0] == "start" and anx_plugin_ok.CALLS[-1][2] == {"mode": "fast"}
    # Real stop path: module stop() runs, handle passed back, marker written.
    plugins.set_enabled("good", False)
    assert anx_plugin_ok.CALLS[-1][0] == "stop"
    assert anx_plugin_ok.CALLS[-1][2] == {"pid_marker": str(tmp_path / "host" / "started-good")}
    assert (tmp_path / "host" / "stopped-good").exists()
    assert {item["id"]: item for item in plugins.inventory()}["good"]["phase"] == "disposed"


def test_config_change_restarts_active_native_plugin(tmp_path, native_modules):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("good", kind="native", entry="anx_plugin_ok",
                                      config_schema={"type": "object",
                                                     "properties": {"mode": {"type": "string", "default": "fast"}}}),
                             source="test", desired="enabled")
    plugins.recover()
    assert plugins.is_active("good")
    import anx_plugin_ok  # type: ignore[import-not-found]  # synthesized plugin module
    before = len(anx_plugin_ok.CALLS)
    result = plugins.put_config("good", {"mode": "slow"})
    assert result["applied"] is True and result["phase"] == "active"
    calls = anx_plugin_ok.CALLS[before:]
    assert [call[0] for call in calls] == ["stop", "start"]
    assert calls[-1][2] == {"mode": "slow"}


def test_sidecar_kind_fails_loud_without_runtime(tmp_path):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("pty-bridge", kind="sidecar"), source="test")
    plugins.set_enabled("pty-bridge", True)
    state = {item["id"]: item for item in plugins.inventory()}["pty-bridge"]
    assert state["phase"] == "failed"
    assert "sidecar_runtime_not_available" in state["last_error"]


def test_platform_precondition_is_enforced(tmp_path):
    plugins = host(tmp_path)
    alien = "plan9" if sys.platform != "plan9" else "linux"
    plugins.install_manifest(manifest("alien", platform={"os": [alien]}), source="test")
    plugins.set_enabled("alien", True)
    state = {item["id"]: item for item in plugins.inventory()}["alien"]
    assert state["phase"] == "failed" and "platform_not_supported" in state["last_error"]
    plugins.install_manifest(manifest("local", platform={"os": [sys.platform]}), source="test")
    plugins.set_enabled("local", True)
    assert plugins.is_active("local")


# -------------------------------------------------------- restart recovery


def test_restart_recovery_is_desired_state_consistent(tmp_path, native_modules):
    store = WorkbenchStore(tmp_path / "host")
    first = PluginHost(store)
    first.install_manifest(manifest("good", kind="native", entry="anx_plugin_ok"), source="test")
    first.install_manifest(manifest("off"), source="test")
    first.set_enabled("good", True)
    assert first.is_active("good")
    # Simulated crash: no shutdown(); a new host instance takes over the store.
    second = PluginHost(store)
    items = {item["id"]: item for item in second.inventory()}
    # Nothing survives a process death: the stale 'active' projection is gone.
    assert items["good"]["phase"] == "disposed"
    assert second.recover() == []
    assert second.is_active("good")
    assert not second.is_active("off")  # desired=disabled stays stopped
    # Graceful path: shutdown disposes everything, recover honors desired.
    second.shutdown()
    third = PluginHost(store)
    assert third.recover() == [] and third.is_active("good")
    third.set_enabled("good", False)
    third.shutdown()
    fourth = PluginHost(store)
    fourth.recover()
    assert not fourth.is_active("good")


def test_redeploy_keeps_desired_state_and_flags_version_drift(tmp_path):
    plugins = host(tmp_path)
    plugins.install_manifest(manifest("svc"), source="test", desired="enabled")
    plugins.recover()
    assert plugins.is_active("svc")
    plugins.install_manifest({**manifest("svc"), "version": "2.0.0"}, source="builtin")
    state = {item["id"]: item for item in plugins.inventory()}["svc"]
    assert state["desired"] == "enabled"
    assert state["running_version"] == "1.0.0"  # still the loaded one
    assert "differs from running" in (state["last_error"] or "")


def test_builtin_manifests_are_valid_and_seed_enabled_once(tmp_path):
    store = WorkbenchStore(tmp_path / "host")
    plugins = PluginHost(store)
    plugins.seed_builtins(BUILTIN_MANIFESTS)
    ids = {item["id"] for item in plugins.inventory()}
    assert {"chat", "mcp", "runtime-host"} <= ids
    assert plugins.recover() == []
    for ident in ("chat", "mcp", "runtime-host"):
        assert plugins.is_active(ident)
    assert plugins.active_services() == {"chat"}
    assert plugins.active_toolsets() == {"mcp", "runtime_host"}
    # A user's disable survives re-seeding (never silently flipped back).
    plugins.set_enabled("mcp", False)
    PluginHost(store).seed_builtins(BUILTIN_MANIFESTS)
    reopened = PluginHost(store)
    reopened.seed_builtins(BUILTIN_MANIFESTS)
    assert {item["id"]: item for item in reopened.inventory()}["mcp"]["desired"] == "disabled"
    assert reopened.recover() == []
    assert not reopened.is_active("mcp") and reopened.is_active("chat")
