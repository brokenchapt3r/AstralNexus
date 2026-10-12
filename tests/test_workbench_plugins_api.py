"""B1 real-host call path: the web API drives the actual plugin lifecycle.

Offline (no model, no worker threads): WorkbenchHost is constructed directly
and its Flask test client exercises the same routes the browser uses.
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.dashboard.workbench_server import WorkbenchHost


@pytest.fixture()
def host(tmp_path):
    instance = WorkbenchHost(tmp_path / "host")
    # start_services() owns the background threads; the tests only need the
    # plugin reconciliation a real boot performs before serving requests.
    instance.plugins.recover()
    yield instance
    instance.close()


def test_snapshot_plugins_are_live_not_hardcoded(host):
    client = host.app.test_client()
    body = client.get("/api/host").get_json()
    plugins = {item["id"]: item for item in body["plugins"]}
    assert {"chat", "mcp", "runtime-host"} <= set(plugins)
    assert plugins["chat"]["status"] == "active" and plugins["chat"]["enabled"] is True
    assert plugins["mcp"]["toolsets"] == ["mcp"]
    assert plugins["chat"]["service"] == "chat"
    # Legacy host-core service info moved to its own key, still present.
    assert {item["id"] for item in body["services"]} >= {"monitor", "workflow", "lsp"}
    # The model-facing runtime_plugins tool reads this same projection.
    from AstralNexus.tools import workbench_tools
    assert "plugins" in body  # snapshot['plugins'] no longer KeyErrors


def test_catalog_is_read_only_and_toggle_is_real_lifecycle(host):
    client = host.app.test_client()
    session = host.store.register_session("plugins", str(host.home.parent))
    before = client.get("/api/host/plugin-catalog").get_json()["items"]
    again = client.get("/api/host/plugin-catalog").get_json()["items"]
    assert before == again  # listing never mutates state
    assert all(item["phase"] == "active" for item in before
               if item["id"] in {"chat", "mcp", "runtime-host"})

    result = client.post("/api/host/plugin-catalog/chat",
                         json={"enabled": False}).get_json()
    assert "chat" in result["affected"]
    state = {item["id"]: item for item in
             client.get("/api/host/plugin-catalog").get_json()["items"]}
    assert state["chat"]["phase"] == "disposed" and state["chat"]["desired"] == "disabled"

    # Enforcement point: chat submission is rejected with a concrete reason.
    response = client.post(f"/api/host/chat/{session}",
                           json={"message": "hello", "request_id": "r1"})
    assert response.status_code == 400
    assert "对话插件已停用" in response.get_json()["error"]

    client.post("/api/host/plugin-catalog/chat", json={"enabled": True})
    assert host.plugins.is_active("chat")
    response = client.post(f"/api/host/chat/{session}",
                           json={"message": "hello", "request_id": "r1"})
    assert response.status_code == 202


def test_gate_plugin_removes_toolsets_from_every_session(host):
    client = host.app.test_client()
    session = host.store.register_session("gate", str(host.home.parent))
    catalog = client.post("/api/host/plugin-catalog/mcp",
                          json={"enabled": False}).get_json()
    assert catalog["plugin"]["phase"] == "disposed"
    items = {item["id"]: item for item in host.chat.plugins(session)["items"]}
    assert items["mcp"]["enabled"] is False
    assert items["mcp"]["session_enabled"] is True  # session never opted out
    assert items["mcp"]["host_gate"]["phase"] == "disposed"
    # The host-wide floor reaches the spawn environment of the next turn.
    floor = sorted(host.plugins.gate_toolsets() - host.plugins.active_toolsets())
    assert floor == ["mcp"]
    client.post("/api/host/plugin-catalog/mcp", json={"enabled": True})
    assert host.plugins.gate_toolsets() - host.plugins.active_toolsets() == set()


def test_toggle_errors_are_honest_400s(host):
    client = host.app.test_client()
    assert client.post("/api/host/plugin-catalog/chat", json={}).status_code == 400
    response = client.post("/api/host/plugin-catalog/ghost", json={"enabled": True})
    assert response.status_code == 400 and "unknown plugin" in response.get_json()["error"]


def test_config_routes_validate_persist_and_apply(host):
    client = host.app.test_client()
    host.plugins.install_manifest({
        "id": "probe", "name": "Probe", "version": "0.1.0", "kind": "gate",
        "config_schema": {"type": "object", "additionalProperties": False,
                          "properties": {"url": {"type": "string", "pattern": "^https://"},
                                         "retries": {"type": "integer", "minimum": 1, "default": 3}}},
    }, source="test")
    body = client.get("/api/host/plugin-catalog/probe/config").get_json()
    assert body["config"] == {"retries": 3} and body["schema"] is not None
    bad = client.post("/api/host/plugin-catalog/probe/config", json={"config": {"url": "http://x"}})
    assert bad.status_code == 400 and "config_invalid" in bad.get_json()["error"]
    good = client.post("/api/host/plugin-catalog/probe/config",
                       json={"config": {"url": "https://x.internal"}}).get_json()
    assert good["config"] == {"url": "https://x.internal", "retries": 3}
    assert good["applied"] is False  # declared but never started
    # Config persisted in the same workbench SQLite; a fresh host sees it.
    with host.store.db() as db:
        row = db.execute("SELECT config FROM plugin_config WHERE id='probe'").fetchone()
    assert json.loads(row["config"])["url"] == "https://x.internal"


def test_legacy_session_toolset_route_still_works(host):
    client = host.app.test_client()
    session = host.store.register_session("legacy", str(host.home.parent))
    items = client.get(f"/api/host/plugins/{session}").get_json()["items"]
    assert items, "registered toolsets must be listed"
    name = items[0]["id"]
    saved = client.post(f"/api/host/plugins/{session}",
                        json={"disabled": [name]}).get_json()
    assert saved["disabled"] == [name]
    response = client.post(f"/api/host/plugins/{session}", json={"disabled": ["nope"]})
    assert response.status_code == 400 and "unknown tool plugin" in response.get_json()["error"]
