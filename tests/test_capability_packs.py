import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import threading
import time
import pytest

from AstralNexus.core.skill_runtime import _frontmatter, discover_skills, load_skill, runtime_skill_context
from AstralNexus.tools.skill_manager import _skills_list, _skill_view
from AstralNexus.tools.mcp_client import StdioTransport
from AstralNexus.tools import mcp_bridge
from AstralNexus.tools.context import tool_workspace


def _anonymous_pipes_available() -> bool:
    """The stdio MCP bridge spawns a child server over anonymous pipes. The
    sandbox refuses CreatePipe (WinError 5) and named pipes alike (P0-4/P1
    boundary chain, 台账 §25/§29/§32), so the REAL stdio-bridge tests cannot
    run here. Same probe shape as test_builtin_tools (R14) and
    test_language_service (R18). The url-endpoint form of the same bridge is
    reachable (localhost TCP is allowed) and is tested below — a skip at the
    source is recorded next to a green equivalent-semantics surface, never
    instead of one.
    """
    try:
        import _winapi
    except ImportError:
        return True  # non-Windows: anonymous pipes are not constrained here
    try:
        read_handle, write_handle = _winapi.CreatePipe(None, 0)
    except OSError:
        return False
    _winapi.CloseHandle(read_handle)
    _winapi.CloseHandle(write_handle)
    return True


_mcp_stdio_unavailable = pytest.mark.skipif(
    not _anonymous_pipes_available(),
    reason='stdio MCP bridge cannot spawn in this sandbox (CreatePipe '
           'refused; §32 boundary) — the url transport covers the same '
           'bridge semantics below')


def test_yaml_multiline_description_and_nested_metadata():
    front, body = _frontmatter('---\nname: sample\ndescription: >\n  First line\n  second line\nmetadata:\n  owner: upstream\n---\nBody')
    assert 'First line second line' in front['description']
    assert front['metadata']['owner'] == 'upstream'
    assert body == 'Body'


def test_imported_sources_are_all_discoverable_with_unique_names(tmp_path):
    root = Path(str(__import__('AstralNexus').__file__)).resolve().parent / 'skills/vendor'
    manifest = json.loads((root / 'catalog.lock.json').read_text(encoding='utf-8'))
    installed = {r['name'] for r in manifest['records'] if r['status'] == 'imported_not_execution_validated'}
    assert len(installed) > 500
    discovered = discover_skills(tmp_path)
    actual = {r['name'] for r in discovered}
    assert installed <= actual
    vendor = [r for r in discovered if root in Path(r['path']).parents]
    assert {r['name'] for r in vendor} == installed
    assert all(r.get('origin') for r in vendor)
    assert load_skill('dsh:office-docx', tmp_path)['origin']['repo'] == 'deepseek-ai/deepseek-harness'
    viewed = json.loads(_skill_view({'name': 'dsh:office-docx', 'home': str(tmp_path)}))
    assert 'terminal_exec' in viewed['compatibility']
    assert viewed['origin']['commit']
    assert viewed['content'] == Path(viewed['path']).read_text(encoding='utf-8')


def test_skill_catalog_can_page_past_original_first_fifty(tmp_path):
    first = json.loads(_skills_list({'home': str(tmp_path), 'limit': 10}))
    second = json.loads(_skills_list({'home': str(tmp_path), 'limit': 10, 'offset': 50}))
    assert first['total'] > 500 and second['offset'] == 50
    assert {s['name'] for s in first['skills']}.isdisjoint(s['name'] for s in second['skills'])
    assert second['next_offset'] == 60


def test_task_ranked_catalog_is_bounded_and_full_body_is_not_eager(tmp_path):
    prompt = runtime_skill_context({}, 'biopython sequence analysis', 'researcher', tmp_path)
    assert 'scientific:biopython' in prompt
    assert 'Use skills_list' in prompt
    assert len(prompt) < 10000
    assert 'Activated Skills' not in prompt


SERVER = '''
import json,sys,time
initialized=False
for line in sys.stdin:
 m=json.loads(line)
 if m['method']=='notifications/initialized': initialized=True;continue
 if m['method']=='initialize': result={'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'test','version':'1'}}
 elif not initialized: raise RuntimeError('missing initialized notification')
 elif m['method']=='tools/list': result={'tools':[{'name':'echo','inputSchema':{'type':'object'}}]}
 elif m['method']=='tools/call': result={'content':[{'type':'text','text':m['params']['arguments'].get('text','')}], 'isError':m['params']['name']=='fail'}
 else: time.sleep(3);continue
 print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{'message':'progress'}}),flush=True)
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


@_mcp_stdio_unavailable
def test_real_stdio_bridge_initializes_ignores_notifications_and_keeps_errors(tmp_path, monkeypatch):
    script = tmp_path / 'server.py'
    script.write_text(SERVER, encoding='utf-8')
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': {'test': {'enabled': True, 'command': [sys.executable, '-u', str(script)]}}}))
    monkeypatch.setenv('ASTRALNEXUS_MCP_CONFIG', str(config))
    with tool_workspace(tmp_path):
        try:
            assert json.loads(mcp_bridge._tools({'server': 'test'}))['tools'][0]['name'] == 'echo'
            response = json.loads(mcp_bridge._call({'server': 'test', 'name': 'echo', 'arguments': {'text': 'actual subprocess'}}))
            assert response['result']['content'][0]['text'] == 'actual subprocess'
            assert json.loads(mcp_bridge._call({'server': 'test', 'name': 'fail'}))['status'] == 'error'
        finally:
            assert json.loads(mcp_bridge._close({'server': 'test'}))['closed']


@_mcp_stdio_unavailable
def test_stdio_timeout_does_not_hang_worker(tmp_path):
    script = tmp_path / 'hang.py'
    script.write_text('import sys,time\nfor line in sys.stdin: time.sleep(10)\n')
    transport = StdioTransport([sys.executable, '-u', str(script)], timeout=0.1)
    try:
        with pytest.raises(TimeoutError):
            transport.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    finally:
        transport.close()


class _InProcessMCPHandler(BaseHTTPRequestHandler):
    """In-process MCP JSON-RPC endpoint (localhost TCP is reachable where
    anonymous pipes are refused). Serves plain JSON on /mcp, an SSE envelope
    on /sse, and stalls on /slow so the timeout surface stays testable."""

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler contract)
        length = int(self.headers.get('Content-Length') or 0)
        message = json.loads(self.rfile.read(length) or b'{}')
        method = message.get('method')
        if method == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {},
                      'serverInfo': {'name': 'inproc', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'echo', 'inputSchema': {'type': 'object'}}]}
        elif method == 'tools/call':
            params = message.get('params') or {}
            result = {'content': [{'type': 'text',
                                   'text': (params.get('arguments') or {}).get('text', '')}],
                      'isError': params.get('name') == 'fail'}
        else:
            result = {}
        if self.path.startswith('/slow'):
            time.sleep(2.0)
        payload = json.dumps({'jsonrpc': '2.0', 'id': message.get('id'),
                              'result': result}).encode('utf-8')
        if self.path.startswith('/sse'):
            body = b'data: ' + payload + b'\n\n'
            content_type = 'text/event-stream'
        else:
            body = payload
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _MCPEndpoint:
    def __init__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _InProcessMCPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.port = self.server.server_address[1]
        self.thread.start()

    def url(self, path='/mcp'):
        return 'http://127.0.0.1:%d%s' % (self.port, path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _write_config(tmp_path, monkeypatch, servers):
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': servers}), encoding='utf-8')
    monkeypatch.setenv('ASTRALNEXUS_MCP_CONFIG', str(config))
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')


def test_url_bridge_lists_calls_and_closes_an_in_process_mcp_server(tmp_path, monkeypatch):
    """The same bridge lifecycle the stdio test covers — initialize, tool
    discovery, real call round-trip, isError preservation, close — over the
    url transport that is physically reachable in this sandbox."""
    endpoint = _MCPEndpoint()
    try:
        _write_config(tmp_path, monkeypatch, {
            'urltest': {'enabled': True, 'url': endpoint.url()}})
        with tool_workspace(tmp_path):
            try:
                listing = json.loads(mcp_bridge._servers({}))
                row = next(s for s in listing['servers'] if s['name'] == 'urltest')
                assert row['transport'] == 'http' and row['enabled'] is True
                tools = json.loads(mcp_bridge._tools({'server': 'urltest'}))
                assert tools['tools'][0]['name'] == 'echo'
                ok = json.loads(mcp_bridge._call({'server': 'urltest', 'name': 'echo',
                                                  'arguments': {'text': 'actual in-process call'}}))
                assert ok['status'] == 'success'
                assert ok['result']['content'][0]['text'] == 'actual in-process call'
                failed = json.loads(mcp_bridge._call({'server': 'urltest', 'name': 'fail'}))
                assert failed['status'] == 'error'
            finally:
                assert json.loads(mcp_bridge._close({'server': 'urltest'}))['closed']
    finally:
        endpoint.close()


def test_sse_envelope_transport_is_wired_through_the_same_bridge(tmp_path, monkeypatch):
    endpoint = _MCPEndpoint()
    try:
        _write_config(tmp_path, monkeypatch, {
            'ssetest': {'enabled': True, 'url': endpoint.url('/sse'),
                        'transport': 'sse'}})
        with tool_workspace(tmp_path):
            try:
                listing = json.loads(mcp_bridge._servers({}))
                row = next(s for s in listing['servers'] if s['name'] == 'ssetest')
                assert row['transport'] == 'sse'
                tools = json.loads(mcp_bridge._tools({'server': 'ssetest'}))
                assert tools['tools'][0]['name'] == 'echo'
            finally:
                mcp_bridge._close({'server': 'ssetest'})
    finally:
        endpoint.close()


def test_url_timeout_does_not_hang_worker(tmp_path, monkeypatch):
    """Equivalent of the stdio timeout test: a stalled endpoint must surface
    a tool_error instead of hanging the worker."""
    endpoint = _MCPEndpoint()
    try:
        _write_config(tmp_path, monkeypatch, {
            'slow': {'enabled': True, 'url': endpoint.url('/slow'),
                     'timeout': 0.2}})
        with tool_workspace(tmp_path):
            try:
                started = time.time()
                out = json.loads(mcp_bridge._tools({'server': 'slow'}))
                elapsed = time.time() - started
                assert out['status'] == 'error'
                assert elapsed < 5.0, 'timeout must fire, not hang (%.1fs)' % elapsed
            finally:
                mcp_bridge._close({'server': 'slow'})
    finally:
        endpoint.close()


def test_bridge_config_requires_command_or_url(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {'broken': {'enabled': True}})
    with tool_workspace(tmp_path):
        out = json.loads(mcp_bridge._tools({'server': 'broken'}))
    assert out['status'] == 'error'
    assert 'command argv list or a url endpoint' in out['error']
