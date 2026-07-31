"""Transport-neutral stateful MCP JSON-RPC client.

The session manager owns identity and authorization; this adapter owns the
MCP initialize/list/call/close protocol.  A real stdio, SSE, HTTP, or
WebSocket transport only needs to implement ``send`` and can then be audited
by the same session IDs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import subprocess
import threading
import time
from urllib.request import Request, urlopen
from typing import Any, Callable, Dict, List, Optional, Protocol

from .mcp_sessions import MCPSession, MCPSessionManager


class MCPTransport(Protocol):
    def send(self, message: Dict[str, Any]) -> Dict[str, Any]: ...

    def reconnect(self) -> None: ...

    def close(self) -> None: ...


class StdioTransport:
    """Line-delimited JSON-RPC transport for a local MCP server process."""

    def __init__(self, command: list[str]):
        self.command = command
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self.reconnect()

    def reconnect(self) -> None:
        with self._lock:
            self.close()
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )

    def send(self, message: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self.reconnect()
            assert self._process is not None and self._process.stdin and self._process.stdout
            self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
            if not line:
                raise ConnectionError("MCP stdio server closed its output")
            return json.loads(line)

    def close(self) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process = None


class HTTPTransport:
    """JSON-RPC HTTP transport with reconnect-compatible lifecycle."""

    def __init__(self, endpoint: str, *, headers: Dict[str, str] | None = None, timeout: float = 60):
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout = timeout

    def send(self, message: Dict[str, Any]) -> Dict[str, Any]:
        request = Request(
            self.endpoint,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **self.headers},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def reconnect(self) -> None:
        """HTTP has no local process to restart; the next request reconnects."""

    def close(self) -> None:
        pass


class SSETransport(HTTPTransport):
    """HTTP transport accepting an MCP SSE response envelope."""

    def send(self, message: Dict[str, Any]) -> Dict[str, Any]:
        request = Request(
            self.endpoint,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream", **self.headers},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            last_data = None
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("data:"):
                    last_data = line[5:].strip()
            if not last_data:
                raise ConnectionError("MCP SSE response contained no data event")
            return json.loads(last_data)


class WebSocketTransport:
    """WebSocket transport for MCP JSON-RPC with auto-reconnect and heartbeat.

    Uses the `websocket-client` library if available, otherwise falls back to
    a minimal implementation using `socket` + HTTP upgrade handshake.
    Supports: auto-reconnect on disconnect, configurable timeout, heartbeat
    keep-alive, message queue for async server notifications.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30,
        reconnect_attempts: int = 3,
        reconnect_delay: float = 1.0,
        heartbeat_interval: float = 0,
    ):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self.heartbeat_interval = heartbeat_interval
        self._ws = None
        self._lock = threading.RLock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._notifications: List[Dict[str, Any]] = []
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._closed = False
        self.reconnect()

    def reconnect(self) -> None:
        with self._lock:
            self._close_ws()
            for attempt in range(self.reconnect_attempts):
                try:
                    self._ws = self._connect()
                    if self.heartbeat_interval > 0:
                        self._start_heartbeat()
                    return
                except Exception:
                    if attempt < self.reconnect_attempts - 1:
                        time.sleep(self.reconnect_delay * (attempt + 1))
                    else:
                        raise ConnectionError(
                            f"WebSocket MCP reconnect failed after {self.reconnect_attempts} attempts"
                        )

    def _connect(self):
        """Establish WebSocket connection using websocket-client or stdlib."""
        try:
            import websocket
            ws = websocket.create_connection(
                self.url,
                header=[f"{k}: {v}" for k, v in self.headers.items()],
                timeout=self.timeout,
            )
            return ws
        except ImportError:
            return self._stdlib_connect()

    def _stdlib_connect(self):
        """Minimal WebSocket client using only stdlib (RFC 6455 subset)."""
        import socket
        import hashlib
        import base64
        import struct
        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"

        sock = socket.create_connection((host, port), timeout=self.timeout)

        key = base64.b64encode(b"AstralNexus-MCP-WsKey").decode()
        request_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in self.headers.items():
            request_lines.append(f"{k}: {v}")
        request_lines.append("")
        request_lines.append("")
        sock.sendall("\r\n".join(request_lines).encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket upgrade failed: connection closed")
            response += chunk

        status_line = response.split(b"\r\n")[0].decode()
        if "101" not in status_line:
            sock.close()
            raise ConnectionError(f"WebSocket upgrade rejected: {status_line}")

        return _StdlibWebSocket(sock)

    def send(self, message: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._ws is None:
                self.reconnect()
            req_id = message.get("id")
            try:
                raw = json.dumps(message, ensure_ascii=False)
                self._ws_send(raw)
                if req_id is None:
                    return {}
                while True:
                    raw_resp = self._ws_recv()
                    resp = json.loads(raw_resp)
                    if resp.get("id") == req_id:
                        return resp
                    else:
                        self._notifications.append(resp)
            except Exception:
                self._ws = None
                raise

    def _ws_send(self, data: str) -> None:
        if hasattr(self._ws, "send"):
            self._ws.send(data)
        else:
            self._ws.send_text(data)

    def _ws_recv(self) -> str:
        if hasattr(self._ws, "recv"):
            result = self._ws.recv()
            if isinstance(result, bytes):
                return result.decode("utf-8")
            return result
        else:
            return self._ws.recv_text()

    def get_notifications(self) -> List[Dict[str, Any]]:
        with self._lock:
            notifications = list(self._notifications)
            self._notifications.clear()
            return notifications

    def _start_heartbeat(self) -> None:
        def _heartbeat_loop():
            while not self._closed and self._ws is not None:
                time.sleep(self.heartbeat_interval)
                try:
                    with self._lock:
                        if self._ws is not None:
                            self._ws_send(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
                except Exception:
                    break
        self._heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                if hasattr(self._ws, "close"):
                    self._ws.close()
            except Exception:
                pass
            self._ws = None

    def close(self) -> None:
        self._closed = True
        with self._lock:
            self._close_ws()


class _StdlibWebSocket:
    """Minimal RFC 6455 frame reader/writer for stdlib-only environments."""

    def __init__(self, sock):
        self._sock = sock

    def send(self, data: str) -> None:
        import struct
        import os
        payload = data.encode("utf-8")
        frame = bytearray()
        frame.append(0x81)
        mask_key = os.urandom(4)
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask_key)
        for i, b in enumerate(payload):
            frame.append(b ^ mask_key[i % 4])
        self._sock.sendall(frame)

    def recv(self) -> str:
        import struct
        header = self._sock.recv(2)
        if len(header) < 2:
            raise ConnectionError("WebSocket closed")
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._sock.recv(8))[0]
        mask_key = self._sock.recv(4) if masked else None
        payload = b""
        while len(payload) < length:
            chunk = self._sock.recv(length - len(payload))
            if not chunk:
                raise ConnectionError("WebSocket closed during read")
            payload += chunk
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise ConnectionError("WebSocket close frame received")
        return payload.decode("utf-8")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


@dataclass
class MCPClient:
    session: MCPSession
    transport: MCPTransport
    sessions: MCPSessionManager
    _request_id: int = 0
    client_info: Dict[str, Any] = field(default_factory=dict)

    def _request(self, method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if self.session.state != "open":
            raise RuntimeError("MCP session is closed")
        self._request_id += 1
        response = self.transport.send({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        })
        if "error" in response:
            raise RuntimeError(f"MCP {method} failed: {response['error']}")
        self.session.touch()
        self.sessions.persist(self.session)
        return response.get("result", response)

    def initialize(self, client_info: Dict[str, Any]) -> Dict[str, Any]:
        self.client_info = dict(client_info)
        return self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": client_info,
        })

    def list_tools(self) -> Dict[str, Any]:
        if not self.session.authorize("tools/list") and "mcp" not in self.session.capabilities:
            raise PermissionError("MCP session is not authorized for tools/list")
        return self._request("tools/list")

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.session.authorize("tools/call") and "mcp" not in self.session.capabilities:
            raise PermissionError("MCP session is not authorized for tools/call")
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        self.sessions.close(self.session.session_id, owner=self.session.owner)

    def heartbeat(self) -> Dict[str, Any]:
        return self._request("ping")

    def reconnect(self) -> None:
        reconnect = getattr(self.transport, "reconnect", None)
        if callable(reconnect):
            reconnect()
        # MCP initialization is connection-scoped.  A transport reconnect
        # creates a new server connection, so restore the protocol session
        # before callers issue tools/list or tools/call.
        if self.client_info and self.session.state == "open":
            self.initialize(self.client_info)
        self.session.touch()
