import json
import sys
import pytest
from AstralNexus.dashboard.workbench_server import WorkbenchHost
from AstralNexus.runtime.workbench import WorkbenchStore
from AstralNexus.runtime.workbench_chat import ChatService

CHANNEL = '''import json,os,socket,sys
_ch=os.environ.get('ASTRALNEXUS_CHAT_CHANNEL','')
if _ch:
    _h,_,_p=_ch.rpartition(':')
    _conn=socket.create_connection((_h or '127.0.0.1',int(_p)),timeout=30)
    inbound=_conn.makefile('r',encoding='utf-8')
    protocol=_conn.makefile('w',encoding='utf-8',newline='\\n')
else:
    inbound=sys.stdin
    protocol=sys.stdout
def event(kind,payload):
    protocol.write(json.dumps({'kind':kind,'payload':payload})+'\\n')
    protocol.flush()
'''


def test_modes_isolation_pending_revocation_and_audit(tmp_path):
    host = WorkbenchHost(tmp_path / 'home')
    try:
        session = host.store.register_session('one', str(tmp_path))
        other = host.store.register_session('two', str(tmp_path))
        client = host.app.test_client()
        url = '/api/host/permissions/' + session
        assert client.get(url).json == {'mode': 'ask'}
        turn = {'id': host.chat.submit(session, 'test', 'a'), 'session_id': session}
        with host.store.db() as db:
            db.execute("UPDATE chat_turns SET status='running' WHERE id=?", (turn['id'],))
        def request(ident, tool, path):
            host.chat.record(turn, 'approval.request', {'id': ident, 'tool': tool, 'arguments': {'path': path}})
        request('inside', 'file_write', 'src/a.py')
        request('outside', 'file_write', '../outside.py')
        request('command', 'terminal_exec', '')
        host.chat.apply_permissions(session)
        assert len(host.chat.snapshot(session)['approvals']) == 3
        assert client.post(url, json={'mode': 'assist'}).status_code == 200
        host.chat.apply_permissions(session)
        assert {a['id'] for a in host.chat.snapshot(session)['approvals']} == {'outside', 'command'}
        assert host.chat.permissions(other)['mode'] == 'ask'
        assert client.post(url, json={'mode': 'full'}).status_code == 200
        host.chat.apply_permissions(session)
        assert not host.chat.snapshot(session)['approvals']
        host.chat.apply_permissions(session)
        events = host.chat.snapshot(session)['events']
        assert len([e for e in events if e['kind'] == 'approval.automatic']) == 3
        client.post(url, json={'mode': 'ask'})
        request('later', 'file_write', 'src/b.py')
        host.chat.apply_permissions(session)
        assert [a['id'] for a in host.chat.snapshot(session)['approvals']] == ['later']
        with pytest.raises(ValueError):
            host.chat.set_permissions(session, 'invalid')
        assert host.chat.permissions(session)['mode'] == 'ask'
    finally:
        host.close()


def test_full_access_reaches_waiting_worker_without_browser(tmp_path):
    store = WorkbenchStore(tmp_path / 'home')
    session = store.register_session('one', str(tmp_path))
    script = CHANNEL + """
json.loads(inbound.readline())
event('approval.request',{'id':'req','tool':'terminal_exec','command':'echo fixture'})
r=json.loads(inbound.readline())
assert r == {'type':'approval','id':'req','choice':'once'},r
event('assistant.final',{'text':'approval delivered'})
event('turn.result',{'status':'completed'})
"""
    chat = ChatService(store, [sys.executable, '-c', script])
    chat.set_permissions(session, 'full')
    ident = chat.submit(session, 'test', 'a')
    assert chat.run_once()
    assert chat.snapshot(session)['turns'][0]['status'] == 'completed'
    assert chat.snapshot(session)['turns'][0]['answer'] == 'approval delivered'
    assert ChatService(store).permissions(session)['mode'] == 'full'


def test_plugin_selection_is_frozen_and_dispatch_enforced(tmp_path, monkeypatch):
    from AstralNexus.tools.registry import registry
    store = WorkbenchStore(tmp_path / 'home')
    session = store.register_session('plugins', str(tmp_path))
    chat = ChatService(store)
    chat.set_plugins(session, ['file'])
    ident = chat.submit(session, 'test', 'one')
    chat.set_plugins(session, [])
    with store.db() as db:
        assert json.loads(db.execute('SELECT plugin_settings FROM chat_turns WHERE id=?', (ident,)).fetchone()[0]) == ['file']
    monkeypatch.setenv('ASTRALNEXUS_DISABLED_TOOLSETS', '["file"]')
    assert not registry.is_tool_available('file_read')
    assert not registry.get_definitions({'file_read'})
    assert 'error' in json.loads(registry.dispatch('file_read', {'path': 'not-run.txt'}))
