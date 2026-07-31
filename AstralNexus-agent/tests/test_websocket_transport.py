"""Tests for WebSocket MCP transport."""
import json
import threading
import pytest
from unittest.mock import MagicMock, patch


class TestWebSocketTransport:
    """Test WebSocketTransport without real network."""

    def test_init_stores_config(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        with patch.object(WebSocketTransport, "reconnect"):
            t = WebSocketTransport(
                "ws://localhost:8080/mcp",
                headers={"Authorization": "Bearer test"},
                timeout=15,
                reconnect_attempts=5,
            )
            assert t.url == "ws://localhost:8080/mcp"
            assert t.headers["Authorization"] == "Bearer test"
            assert t.timeout == 15
            assert t.reconnect_attempts == 5

    def test_send_dispatches_by_request_id(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        with patch.object(WebSocketTransport, "reconnect"):
            t = WebSocketTransport("ws://localhost:8080/mcp")
            mock_ws = MagicMock()
            responses = [
                json.dumps({"jsonrpc": "2.0", "method": "notification", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),
            ]
            mock_ws.recv = MagicMock(side_effect=responses)
            t._ws = mock_ws

            result = t.send({"jsonrpc": "2.0", "id": 1, "method": "test"})
            assert result == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
            assert len(t.get_notifications()) == 1

    def test_get_notifications_clears_queue(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        with patch.object(WebSocketTransport, "reconnect"):
            t = WebSocketTransport("ws://localhost:8080/mcp")
            t._notifications = [{"a": 1}, {"b": 2}]
            batch = t.get_notifications()
            assert len(batch) == 2
            assert len(t.get_notifications()) == 0

    def test_send_with_no_id_returns_empty(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        with patch.object(WebSocketTransport, "reconnect"):
            t = WebSocketTransport("ws://localhost:8080/mcp")
            mock_ws = MagicMock()
            t._ws = mock_ws

            result = t.send({"jsonrpc": "2.0", "method": "notify"})
            assert result == {}

    def test_close_sets_closed_flag(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        with patch.object(WebSocketTransport, "reconnect"):
            t = WebSocketTransport("ws://localhost:8080/mcp")
            t._ws = MagicMock()
            t.close()
            assert t._closed is True
            assert t._ws is None

    def test_reconnect_retries_on_failure(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        call_count = 0
        def mock_connect():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return MagicMock()

        with patch.object(
            WebSocketTransport,
            "_connect",
            side_effect=lambda self_inner: mock_connect(),
        ), patch("AstralNexus.tools.mcp_client.time.sleep"):
                t = WebSocketTransport.__new__(WebSocketTransport)
                t.url = "ws://localhost:8080"
                t.headers = {}
                t.timeout = 5
                t.reconnect_attempts = 3
                t.reconnect_delay = 0
                t.heartbeat_interval = 0
                t._ws = None
                t._lock = threading.RLock()
                t._pending = {}
                t._notifications = []
                t._heartbeat_thread = None
                t._closed = False

                def _connect_stub():
                    return mock_connect()
                t._connect = _connect_stub
                t.reconnect()
                assert t._ws is not None

    def test_stdlib_websocket_close(self):
        from AstralNexus.tools.mcp_client import _StdlibWebSocket
        mock_sock = MagicMock()
        ws = _StdlibWebSocket(mock_sock)
        ws.close()
        mock_sock.close.assert_called_once()

    def test_heartbeat_interval_config(self):
        from AstralNexus.tools.mcp_client import WebSocketTransport
        with patch.object(WebSocketTransport, "reconnect"):
            t = WebSocketTransport("ws://localhost:8080/mcp", heartbeat_interval=30)
            assert t.heartbeat_interval == 30

    def test_stdlib_websocket_frame_encoding(self):
        from AstralNexus.tools.mcp_client import _StdlibWebSocket
        import struct
        mock_sock = MagicMock()
        ws = _StdlibWebSocket(mock_sock)

        ws.send("hello")
        sent = mock_sock.sendall.call_args[0][0]
        assert sent[0] == 0x81  # FIN + text opcode
        assert sent[1] & 0x80  # MASK bit set


class TestStdlibWebSocketRecv:
    def test_recv_unmasked_text_frame(self):
        from AstralNexus.tools.mcp_client import _StdlibWebSocket
        import struct
        mock_sock = MagicMock()
        # Header: FIN=1, opcode=1 (text), masked=0, length=5
        header = bytes([0x81, 0x05])
        payload = b"hello"
        mock_sock.recv = MagicMock(side_effect=[header, payload])
        ws = _StdlibWebSocket(mock_sock)
        result = ws.recv()
        assert result == "hello"

    def test_recv_close_frame_raises(self):
        from AstralNexus.tools.mcp_client import _StdlibWebSocket
        mock_sock = MagicMock()
        header = bytes([0x88, 0x00])
        mock_sock.recv = MagicMock(return_value=header)
        ws = _StdlibWebSocket(mock_sock)
        with pytest.raises(ConnectionError, match="close frame"):
            ws.recv()
