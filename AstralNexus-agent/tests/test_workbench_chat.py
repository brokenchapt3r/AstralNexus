"""Offline chat lifecycle tests; no inference provider is contacted."""
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from AstralNexus.dashboard.workbench_server import WorkbenchHost
from AstralNexus.runtime.workbench import WorkbenchStore
from AstralNexus.runtime.workbench_chat import ChatService
from AstralNexus.runtime.workbench_chat_worker import ChatOutput, execute_turn


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

FIXTURE = CHANNEL + '''
r=json.loads(inbound.readline())
event('assistant.delta',{'text':'offline reply: '})
event('progress',{'type':'tool.started','tool_name':'file_read','text':'Reading workspace'})
if r['message']=='approve':
 event('approval.request',{'id':'approval-'+r['home'].split('/')[-1], 'tool':'file_write','command':'fixture.txt'})
 answer=json.loads(inbound.readline())
 assert answer['type']=='approval' and answer['choice']=='once'
elif r['message']=='wait':
 answer=json.loads(inbound.readline())
 assert answer['type']=='cancel'
 event('turn.result',{'status':'cancelled'})
 sys.exit(1)
elif r['message']=='crash':
 sys.exit(7)
elif r['message']=='deliver':
 event('delivery.publish',{'status':'published','delivery_id':'d-1','published':3})
event('assistant.final',{'text':'offline reply: '+r['message']+' history='+str(len(r['history']))})
event('turn.result',{'status':'completed'})
'''


def service(tmp_path):
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('chat', str(tmp_path))
    return store, session, ChatService(store, [sys.executable, '-u', '-c', FIXTURE])


def until(predicate):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('condition did not become true')


def test_durable_history_idempotency_reconnect_and_session_isolation(tmp_path):
    store, session, chat = service(tmp_path)
    ident = chat.submit(session, 'hello', 'one')
    assert chat.submit(session, 'hello', 'one') == ident
    with pytest.raises(ValueError):
        chat.submit(session, 'different', 'one')
    assert chat.run_once()
    snap = chat.snapshot(session)
    assert snap['turns'][0]['status'] == 'completed'
    assert snap['turns'][0]['answer'] == 'offline reply: hello history=0'
    assert chat.snapshot(session, snap['cursor'])['events'] == []
    second = store.register_session('other', str(tmp_path))
    assert chat.snapshot(second)['turns'] == []
    with pytest.raises(ValueError):
        chat.cancel(second, ident)
    reopened = ChatService(store, chat.command)
    assert reopened.snapshot(session)['turns'] == snap['turns']
    reopened.submit(session, 'followup', 'two')
    reopened.run_once()
    assert reopened.snapshot(session)['turns'][-1]['answer'].endswith('history=2')


def test_nonzero_exit_never_becomes_success_despite_partial_answer(tmp_path):
    _, session, chat = service(tmp_path)
    chat.submit(session, 'crash', 'one')
    chat.run_once()
    turn = chat.snapshot(session)['turns'][0]
    assert turn['status'] == 'failed' and turn['answer'] == 'offline reply: '


def test_approval_survives_refresh_and_wrong_session_cannot_answer(tmp_path):
    store, session, chat = service(tmp_path)
    chat.submit(session, 'approve', 'one')
    chat.start()
    try:
        approval = until(lambda: chat.snapshot(session)['approvals'])[0]
        assert chat.snapshot(session)['approvals'][0] == approval
        other = store.register_session('other', str(tmp_path))
        with pytest.raises(ValueError):
            chat.approve(other, approval['id'], 'once')
        chat.approve(session, approval['id'], 'once')
        until(lambda: chat.snapshot(session)['turns'][0]['status'] == 'completed')
        assert not chat.snapshot(session)['approvals']
    finally:
        chat.close()


def test_cancel_active_and_queued_turns_without_replay(tmp_path):
    _, session, chat = service(tmp_path)
    ident = chat.submit(session, 'wait', 'one')
    queued = chat.submit(session, 'must not run', 'two')
    chat.start()
    try:
        until(lambda: chat.snapshot(session)['turns'][0]['answer'])
        chat.cancel(session, queued)
        chat.cancel(session, ident)
        until(lambda: all(t['status'] == 'cancelled' for t in chat.snapshot(session)['turns']))
    finally:
        chat.close()


def test_host_restart_marks_unfinished_interrupted_and_never_queues_again(tmp_path):
    store, session, chat = service(tmp_path)
    ident = chat.submit(session, 'hello', 'one')
    with store.db() as db:
        db.execute("UPDATE chat_turns SET status='running' WHERE id=?", (ident,))
    reopened = ChatService(store, chat.command)
    assert reopened.snapshot(session)['turns'][0]['status'] == 'interrupted'
    assert not reopened.run_once()


def test_chat_routes_and_default_composer(tmp_path):
    host = WorkbenchHost(tmp_path / 'host')
    try:
        client = host.app.test_client()
        session = host.store.register_session('chat', str(tmp_path))
        response = client.post('/api/host/chat/' + session, json={'message': 'hello', 'request_id': 'id'})
        assert response.status_code == 202
        assert client.get('/api/host/chat/' + session).json['turns'][0]['message'] == 'hello'
        assert client.post('/api/host/chat/' + session + '/' + response.json['id'] + '/cancel', json={}).status_code == 200
        assert b'id="message"' in client.get('/').data
        assert b'id="timeline"' in client.get('/').data
        assert client.get('/monitor').status_code == 200
    finally:
        host.close()


def test_historical_monitor_replay_does_not_block_chat_host(tmp_path, monkeypatch):
    from AstralNexus.dashboard.live_server import LiveDashboardServer
    release = threading.Event()
    entered = threading.Event()
    def slow_trace(self):
        entered.set()
        release.wait(5)
        return 0
    monkeypatch.setattr(LiveDashboardServer, '_load_trace', slow_trace)
    (tmp_path / 'trace.jsonl').touch()
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('large old run', str(tmp_path), str(tmp_path))
    store.select(session)
    host = WorkbenchHost(store.home)
    try:
        assert not entered.is_set()
        host.start_services()
        assert entered.wait(2)
        client = host.app.test_client()
        assert client.get('/api/host/chat/' + session).status_code == 200
        assert not release.is_set()
    finally:
        release.set()
        host.close()


class Controller:
    def __init__(self):
        self.thread = None
        self.result = None

    def status(self):
        return {'running': bool(self.thread and self.thread.is_alive()), 'run_dir': '/offline/run' if self.thread else None, 'last_result': self.result}

    def interrupt(self, message):
        pass


@pytest.mark.parametrize('result,status', [(0, 'completed'), (1, 'failed')])
def test_start_ack_waits_for_actual_governed_execution(result, status):
    controller = Controller()
    release = threading.Event()
    events = []
    output = ChatOutput(lambda k, p: events.append((k, p)), threading.Event())
    def govern(message, history, ctl, output):
        history.extend([{'role': 'user', 'content': message}, {'role': 'assistant', 'content': 'started'}])
        output.write_response('started')
        def run():
            release.wait(5)
            ctl.result = result
        ctl.thread = threading.Thread(target=run)
        ctl.thread.start()
        return True
    results = []
    thread = threading.Thread(target=lambda: results.append(execute_turn({'message': 'develop'}, controller, output, govern, lambda *a, **k: None)))
    thread.start()
    until(lambda: events)
    assert not results  # startup acknowledgment is not terminal completion
    release.set()
    thread.join(5)
    assert results == [status]
    assert any(k == 'run.link' for k, _ in events)


def test_swallowed_cli_error_and_empty_reply_are_failures():
    for fail in (False, True):
        output = ChatOutput(lambda *a: None, threading.Event())
        def chat(message, history, output):
            if fail:
                output.write('provider unavailable', error=True)
        assert execute_turn({'message': 'hello'}, Controller(), output, lambda *a, **k: False, chat) == 'failed'


def test_public_event_projection_hides_worker_tokens_and_request_bodies():
    events = []
    output = ChatOutput(lambda k, p: events.append((k, p)), threading.Event())
    output.event({'event_type': 'llm.request', 'messages': ['private']})
    output.event({'event_type': 'llm.text.delta', 'node_id': 'N1', 'text': 'structured patch'})
    output.event({'event_type': 'planning.summary', 'summary': 'Three stages', 'reasoning': 'private'})
    output.event({'event_type': 'llm.tool_batch', 'calls': [{'name': 'file_read', 'status': 'success', 'result': 'private content'}]})
    assert len(events) == 2
    assert 'private' not in json.dumps(events)
    assert events[1][1]['tool_name'] == 'file_read'


def test_tool_projection_preserves_identity_arguments_and_bounded_result():
    events = []
    output = ChatOutput(lambda k, p: events.append(p), threading.Event())
    output.event({'event_type': 'llm.tool_execution.start', 'node_id': 'N-1', 'calls': [
        {'name': 'file_read', 'call_id': 'read-a', 'arguments': '{"path":"a.py"}'},
        {'name': 'file_read', 'call_id': 'read-b', 'arguments': {'path': 'b.py'}},
    ]})
    for kind in ('llm.tool_execution.done', 'llm.tool_batch'):
        output.event({'event_type': kind, 'node_id': 'N-1', 'calls': [
            {'name': 'file_read', 'call_id': 'read-b', 'status': 'error', 'error': 'missing file'},
            {'name': 'file_read', 'call_id': 'read-a', 'status': 'success', 'stdout_tail': 'x' * 3000},
        ]})
    assert [event['call_id'] for event in events] == ['read-a', 'read-b', 'read-b', 'read-a', 'read-b', 'read-a']
    assert events[0]['arguments'] == {'path': 'a.py'}
    assert events[2]['text'] == 'missing file'
    assert events[2]['status'] == 'error'
    assert len(events[3]['text']) == 2000


def test_cancel_reaps_descendant_after_cooperative_parent_exit(tmp_path):
    import psutil  # type: ignore[import-untyped]
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('cancel child', str(tmp_path))
    script = CHANNEL + """
json.loads(inbound.readline())
import subprocess
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
event('assistant.delta',{'text':str(child.pid)})
json.loads(inbound.readline())
event('turn.result',{'status':'cancelled'})
"""
    chat = ChatService(store, [sys.executable, '-u', '-c', script])
    child = None
    try:
        ident = chat.submit(session, 'wait', 'one')
        chat.start()
        until(lambda: bool(chat.snapshot(session)['turns'][0]['answer']))
        child = psutil.Process(int(chat.snapshot(session)['turns'][0]['answer']))
        time.sleep(.3)
        chat.cancel(session, ident)
        until(lambda: chat.snapshot(session)['turns'][0]['status'] == 'cancelled')
        assert not child.is_running()
    finally:
        chat.close()
        if child is not None and child.is_running():
            child.kill()


# --------------------------------------------------------------------------
# The durable row must say WHY a turn ended, not only that it did.
# Port of DeerFlow ``runtime/runs/manager.py`` (named stop reason + error text
# written with the status).  Live 2026-10-07: the host died of page-file
# exhaustion, the run went ``interrupted``, and the row still said ``running``,
# so neither the host API nor the DB could explain that the run was over.


def test_failed_turn_row_records_the_stop_reason(tmp_path):
    """A reader of the row alone can say why the turn failed."""
    _, session, chat = service(tmp_path)
    chat.submit(session, 'crash', 'one')
    chat.run_once()
    turn = chat.snapshot(session)['turns'][0]
    assert turn['status'] == 'failed'
    assert turn['stop_reason'] == 'worker_failed'
    # The worker exited by itself: the host loop saw no exception to report.
    assert turn['error'] == ''


def test_cancelled_turn_rows_record_who_stopped_them(tmp_path):
    """Both the running turn and the one still queued carry the reason."""
    _, session, chat = service(tmp_path)
    ident = chat.submit(session, 'wait', 'one')
    queued = chat.submit(session, 'must not run', 'two')
    chat.start()
    try:
        until(lambda: chat.snapshot(session)['turns'][0]['answer'])
        chat.cancel(session, queued)
        chat.cancel(session, ident)
        until(lambda: all(t['status'] == 'cancelled' for t in chat.snapshot(session)['turns']))
    finally:
        chat.close()
    turns = chat.snapshot(session)['turns']
    assert [t['stop_reason'] for t in turns] == ['user_cancelled', 'user_cancelled']


def test_orphan_recovery_row_records_why(tmp_path):
    """``the host came back and found this unfinished`` must be readable from
    the row, not only from an event payload."""
    store, session, chat = service(tmp_path)
    ident = chat.submit(session, 'hello', 'one')
    with store.db() as db:
        db.execute("UPDATE chat_turns SET status='running' WHERE id=?", (ident,))
    reopened = ChatService(store, chat.command)
    turn = reopened.snapshot(session)['turns'][0]
    assert turn['status'] == 'interrupted'
    assert turn['stop_reason'] == 'orphan_recovered'
    assert '重启或崩溃' in turn['error']
    events = [e for e in reopened.snapshot(session)['events'] if e['kind'] == 'turn.interrupted']
    assert events and events[0]['payload']['stop_reason'] == 'orphan_recovered'


def test_completed_turn_row_carries_the_delivery_verdict(tmp_path):
    """The worker's own publication outcome rides on the row (DeerFlow's
    ``goal_verdict``), so the row can answer "was anything published?"."""
    _, session, chat = service(tmp_path)
    chat.submit(session, 'deliver', 'one')
    chat.run_once()
    turn = chat.snapshot(session)['turns'][0]
    assert turn['status'] == 'completed'
    assert turn['stop_reason'] == ''
    assert json.loads(turn['goal_verdict'])['status'] == 'published'
