"""Model-facing MCP discovery/call bridge over explicitly configured servers."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import threading
from typing import Any

from .context import get_tool_workspace
from .mcp_client import (HTTPTransport, MCPClient, SSETransport,
                         StdioTransport, WebSocketTransport)
from .mcp_sessions import MCPSessionManager
from .registry import registry, tool_error, tool_result

_CLIENTS: dict[tuple[str, str], MCPClient] = {}
_LOCK = threading.RLock()


def _configuration() -> dict[str, Any]:
    home = Path(os.environ.get('ASTRALNEXUS_HOME', str(Path.home() / '.astralnexus')))
    path = Path(os.environ.get('ASTRALNEXUS_MCP_CONFIG', str(home / 'mcp_servers.json')))
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    return dict(data.get('mcpServers') or {})


def _transport_kind(config: dict[str, Any]) -> str:
    """Resolve the configured transport: 'stdio' for argv commands, otherwise
    the named network transport for url endpoints."""
    if config.get('command') is not None:
        return 'stdio'
    return str(config.get('transport', 'http')).lower()


def _client(server: str) -> MCPClient:
    root = str((get_tool_workspace() or Path.cwd()).resolve())
    key = (root, server)
    with _LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]
        config = _configuration().get(server)
        if not isinstance(config, dict) or not config.get('enabled', False):
            raise ValueError('MCP server not configured/enabled: ' + server)
        # Credentials may be inherited from the host; never echo environment values.
        missing = [name for name in config.get('requires_env', []) if not os.environ.get(name)]
        if missing:
            raise ValueError('MCP prerequisite environment variables missing: ' + ', '.join(missing))
        sessions = MCPSessionManager()
        session = sessions.open_session(server, owner=root, capabilities={'mcp'})
        timeout = float(config.get('timeout', 60))
        command = config.get('command')
        url = config.get('url')
        if isinstance(command, list) and command and all(isinstance(v, str) for v in command):
            command = [value.replace('${WORKSPACE}', root) for value in command]
            environment = {**os.environ, **{str(k): str(v).replace('${WORKSPACE}', root)
                                          for k, v in (config.get('env') or {}).items()}}
            transport: Any = StdioTransport(command, timeout=timeout, cwd=root, env=environment)
        elif isinstance(url, str) and url:
            # Network endpoint form (R21接线, 台账 §32): the HTTP/SSE/WebSocket
            # transports already existed in mcp_client without a consumer.
            # localhost TCP is reachable in sandboxes that refuse the anonymous
            # pipes stdio spawn requires — 接通已有实现, not a lenient
            # substitute: the same MCPClient/session lifecycle wraps either
            # transport, and the stdio form stays configured as-is.
            endpoint = url.replace('${WORKSPACE}', root)
            headers = {str(k): str(v).replace('${WORKSPACE}', root)
                       for k, v in (config.get('headers') or {}).items()}
            kind = _transport_kind(config)
            if kind == 'sse':
                transport = SSETransport(endpoint, headers=headers, timeout=timeout)
            elif kind in ('websocket', 'ws'):
                transport = WebSocketTransport(endpoint, timeout=timeout)
            elif kind == 'http':
                transport = HTTPTransport(endpoint, headers=headers, timeout=timeout)
            else:
                raise ValueError('unknown MCP transport: ' + kind)
        else:
            raise ValueError('MCP server requires either a command argv list or a url endpoint')
        client = MCPClient(session, transport, sessions)
        try:
            client.initialize({'name': 'AstralNexus', 'version': '1.0'})
        except Exception:
            transport.close()
            raise
        _CLIENTS[key] = client
        return client


def _servers(args: dict[str, Any]) -> str:
    entries = []
    for name, config in _configuration().items():
        config = config if isinstance(config, dict) else {}
        entries.append({'name': name, 'enabled': bool(config.get('enabled')),
            'requires_env': config.get('requires_env', []),
            'transport': _transport_kind(config),
            'source': config.get('source', ''), 'description': config.get('description', '')})
    return tool_result({'status': 'success', 'servers': entries})


def _tools(args: dict[str, Any]) -> str:
    try:
        return tool_result({'status': 'success', **_client(str(args['server'])).list_tools()})
    except Exception as exc:
        return tool_error(str(exc))


def _call(args: dict[str, Any]) -> str:
    try:
        result = _client(str(args['server'])).call_tool(str(args['name']), dict(args.get('arguments') or {}))
        return tool_result({'status': 'error' if result.get('isError') else 'success',
                            'server': args['server'], 'tool': args['name'], 'result': result})
    except Exception as exc:
        return tool_error(str(exc))


def _close(args: dict[str, Any]) -> str:
    root = str((get_tool_workspace() or Path.cwd()).resolve())
    with _LOCK:
        client = _CLIENTS.pop((root, str(args['server'])), None)
        if client:
            client.close()
    return tool_result({'status': 'success', 'closed': client is not None})


def _close_all() -> None:
    with _LOCK:
        for client in list(_CLIENTS.values()):
            try:
                client.close()
            except Exception:
                pass
        _CLIENTS.clear()


atexit.register(_close_all)
registry.register(name='mcp_servers', toolset='mcp', schema={'name': 'mcp_servers', 'description': 'List configured MCP capability servers and prerequisites; no credentials are returned.', 'parameters': {'type': 'object', 'properties': {}}}, handler=_servers)
registry.register(name='mcp_tools', toolset='mcp', schema={'name': 'mcp_tools', 'description': 'Initialize a configured MCP server and list its actual tool schemas.', 'parameters': {'type': 'object', 'properties': {'server': {'type': 'string'}}, 'required': ['server']}}, handler=_tools)
registry.register(name='mcp_call', toolset='mcp', schema={'name': 'mcp_call', 'description': 'Call a previously discovered tool on a configured MCP server. External services may require credentials.', 'parameters': {'type': 'object', 'properties': {'server': {'type': 'string'}, 'name': {'type': 'string'}, 'arguments': {'type': 'object'}}, 'required': ['server', 'name']}}, handler=_call, requires_approval=True)
registry.register(name='mcp_close', toolset='mcp', schema={'name': 'mcp_close', 'description': 'Close this workspace MCP server process and session.', 'parameters': {'type': 'object', 'properties': {'server': {'type': 'string'}}, 'required': ['server']}}, handler=_close)
