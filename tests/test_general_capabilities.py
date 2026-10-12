import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from AstralNexus.tools.context import tool_workspace
from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools.general_capabilities import _glob, _sqlite, _http


def test_new_adapters_are_registered_in_actual_executor(tmp_path):
    executor = ToolExecutor(workspace_root=tmp_path)
    assert {'glob_files', 'runtime_dependencies', 'sqlite_query', 'http_request',
            'mcp_servers', 'mcp_tools', 'mcp_call', 'mcp_close'} <= set(executor.list_tools())
    result = executor.execute_tool('runtime_dependencies', {'modules': ['sqlite3', 'not_a_real_module_123']})
    assert result['status'] == 'success'


def test_sqlite_reads_real_data_without_mutating(tmp_path):
    db = tmp_path / 'demo.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('create table samples(value integer)')
        connection.executemany('insert into samples values (?)', [(1,), (2,)])
    with tool_workspace(tmp_path):
        result = json.loads(_sqlite({'path': 'demo.sqlite', 'query': 'select * from samples', 'limit': 1}))
        assert result['rows'] == [[1]] and result['truncated']
        assert json.loads(_sqlite({'path': 'demo.sqlite', 'query': 'delete from samples'}))['status'] == 'error'
        assert json.loads(_glob({'pattern': '*.sqlite'}))['files'] == ['demo.sqlite']


def test_http_preserves_actual_non_2xx_response_as_evidence():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b'{"error":"actual-conflict"}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = json.loads(_http({'url': f'http://127.0.0.1:{server.server_port}/', 'method': 'POST', 'body': {'value': 1}}))
        assert result['status_code'] == 409
        assert json.loads(result['body'])['error'] == 'actual-conflict'
    finally:
        server.shutdown()
        server.server_close()
