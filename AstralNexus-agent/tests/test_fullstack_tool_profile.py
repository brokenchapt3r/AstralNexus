"""Offline routing through the real registry and real HTTP/SQLite adapters."""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools.registry import discover_builtin_tools, registry
from AstralNexus.tools.tool_router import select_tools
from AstralNexus.tools.toolsets import resolve_toolset


@pytest.mark.parametrize('role', ['coder', 'verifier', 'recovery_agent'])
def test_real_checks_reach_development_roles(role):
    discover_builtin_tools()
    selected = select_tools(role, '全栈项目接口测试与数据库验证', resolve_toolset(role))
    assert {'http_request', 'sqlite_query', 'runtime_dependencies', 'pytest_run'} <= set(selected)
    assert len(selected) <= 12


def test_fullstack_priority_never_expands_explicit_profile():
    discover_builtin_tools()
    assert select_tools('coder', '全栈项目', [], profile_authorized=True) == []
    assert select_tools('coder', '全栈项目', ['file_read'], profile_authorized=True) == ['file_read']
    assert 'http_request' not in select_tools('reviewer', '全栈项目接口测试')
    entry = registry.get_entry('http_request')
    assert entry is not None and entry.requires_approval


def test_browser_check_selects_provider_discovery_not_fetch_substitute():
    discover_builtin_tools()
    selected = select_tools('verifier', '全栈项目浏览器 E2E 验收', resolve_toolset('verifier'))
    assert selected[:3] == ['mcp_servers', 'mcp_tools', 'mcp_call']
    assert 'browser_fetch' not in selected


def test_selected_fullstack_tools_observe_real_conflict_and_database(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_TOOL_APPROVAL', 'true')
    with sqlite3.connect(tmp_path / 'app.sqlite') as db:
        db.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)')
        db.execute("INSERT INTO orders VALUES (1, 'open')")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b'{"error":"duplicate_order"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        executor = ToolExecutor(workspace_root=tmp_path)
        tools = select_tools('verifier', '全栈项目接口测试', resolve_toolset('verifier'))
        assert {'http_request', 'sqlite_query'} <= set(tools)
        response = executor.execute_tool('http_request', {
            'url': f'http://127.0.0.1:{server.server_port}/orders', 'method': 'POST',
        })
        assert response['status_code'] == 409
        assert json.loads(response['body'])['error'] == 'duplicate_order'
        rows = executor.execute_tool('sqlite_query', {
            'path': 'app.sqlite', 'query': 'SELECT id, status FROM orders',
        })
        assert rows['rows'] == [[1, 'open']]
        rejected = executor.execute_tool('sqlite_query', {
            'path': 'app.sqlite', 'query': 'DELETE FROM orders',
        })
        assert rejected['status'] == 'error'
        # A successful transport is still not a business acceptance verdict.
        assert 'business assertions must be checked separately' in response['note']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
