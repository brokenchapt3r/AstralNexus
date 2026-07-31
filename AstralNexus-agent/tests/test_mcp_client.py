from AstralNexus.tools.mcp_client import MCPClient
from AstralNexus.tools.mcp_sessions import MCPSessionManager


class FakeTransport:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        if message["method"] == "tools/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}


class ConnectionScopedTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.initialized = False

    def reconnect(self):
        self.initialized = False

    def send(self, message):
        self.messages.append(message)
        if message["method"] == "initialize":
            self.initialized = True
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}
        if not self.initialized:
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "not initialized"}}
        if message["method"] == "tools/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}


def test_mcp_json_rpc_client_uses_stateful_session():
    manager = MCPSessionManager()
    session = manager.open_session("demo", owner="run-1", capabilities={"mcp"})
    transport = FakeTransport()
    client = MCPClient(session, transport, manager)
    assert client.initialize({"name": "AstralNexus"})["ok"] is True
    assert client.list_tools()["tools"] == []
    assert client.call_tool("demo", {"x": 1})["ok"] is True
    client.close()
    assert session.state == "closed"


def test_mcp_heartbeat_and_reconnect_are_transport_hooks():
    manager = MCPSessionManager()
    session = manager.open_session("demo", owner="run-1", capabilities={"mcp"})
    transport = FakeTransport()
    client = MCPClient(session, transport, manager)
    assert client.heartbeat()["ok"] is True
    client.reconnect()
    assert transport.messages[-1]["method"] == "ping"


def test_reconnect_reinitializes_connection_scoped_protocol():
    manager = MCPSessionManager()
    session = manager.open_session("demo", owner="run-1", capabilities={"mcp"})
    transport = ConnectionScopedTransport()
    client = MCPClient(session, transport, manager)
    client.initialize({"name": "AstralNexus"})
    transport.reconnect()
    client.reconnect()
    assert client.list_tools()["tools"] == []
    assert [m["method"] for m in transport.messages][-2:] == ["initialize", "tools/list"]
