import json
import os
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


def test_session_model_settings_persist_and_turn_selection_is_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_MODEL', 'main-default')
    store = WorkbenchStore(tmp_path / 'home')
    session = store.register_session('one', str(tmp_path))
    other = store.register_session('two', str(tmp_path))
    chat = ChatService(store)
    assert chat.models(session)['effective_sub'] == 'main-default'
    chat.set_models(session, {'main_model': 'main-chosen', 'sub_model': 'sub-chosen'})
    first = chat.submit(session, 'hello', 'one')
    chat.set_models(session, {'main_model': 'main-new', 'sub_model': ''})
    assert chat.submit(session, 'hello', 'one') == first
    second = chat.submit(session, 'next', 'two')
    turns = chat.snapshot(session)['turns']
    assert json.loads(turns[0]['model_settings']) == {'main_model': 'main-chosen', 'sub_model': 'sub-chosen'}
    assert json.loads(turns[1]['model_settings']) == {'main_model': 'main-new', 'sub_model': 'main-new'}
    assert chat.models(other)['main_model'] == ''
    assert ChatService(store).models(session)['effective_sub'] == 'main-new'
    assert second != first


def test_frozen_models_reach_subprocess_without_changing_host_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_MODEL', 'host-main')
    monkeypatch.setenv('ASTRALNEXUS_SUB_MODEL', 'host-sub')
    store = WorkbenchStore(tmp_path / 'home')
    session = store.register_session('test', str(tmp_path))
    script = CHANNEL + '''
json.loads(inbound.readline())
event('assistant.final',{'text':os.environ['ASTRALNEXUS_MODEL']+'|'+os.environ['ASTRALNEXUS_SUB_MODEL']})
event('turn.result',{'status':'completed'})
'''
    chat = ChatService(store, [sys.executable, '-c', script])
    chat.set_models(session, {'main_model': 'chosen-main', 'sub_model': 'chosen-sub'})
    chat.submit(session, 'test', 'one')
    chat.set_models(session, {'main_model': 'later-main'})
    assert chat.run_once()
    turn = chat.snapshot(session)['turns'][0]
    assert turn['status'] == 'completed' and turn['answer'] == 'chosen-main|chosen-sub'
    assert os.environ['ASTRALNEXUS_MODEL'] == 'host-main'
    assert os.environ['ASTRALNEXUS_SUB_MODEL'] == 'host-sub'


def test_model_routes_validate_without_exposing_credentials(tmp_path):
    host = WorkbenchHost(tmp_path / 'host')
    try:
        session = host.store.register_session('test', str(tmp_path))
        client = host.app.test_client()
        url = '/api/host/models/' + session
        assert client.post(url, json={'main_model': 'custom/model:v1', 'sub_model': ''}).status_code == 200
        result = client.get(url).json
        assert result['effective_sub'] == 'custom/model:v1'
        assert 'api_key' not in result and 'base_url' not in result
        for invalid in ['two words', 'bad\nmodel', 'qwen3.8-max']:
            assert client.post(url, json={'main_model': invalid}).status_code == 400
        assert client.get(url).json['main_model'] == 'custom/model:v1'
    finally:
        host.close()


@pytest.mark.parametrize('sub,explicit,expected', [('sub-model', False, 'sub-model'), ('sub-model', True, 'sub-model'), ('', True, 'main-model'), ('', False, None)])
def test_control_and_verifier_keep_main_workers_receive_sub(tmp_path, monkeypatch, sub, explicit, expected):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime import llm_client
    from AstralNexus.runtime.model_selector import get_model_config
    clients = []
    class Client:
        def __init__(self, **kwargs):
            self.model = kwargs['model']
            clients.append(self)
        def chat(self, *args, **kwargs):
            raise AssertionError('no inference expected in wiring test')
    monkeypatch.setattr(llm_client, 'LLMClient', Client)
    monkeypatch.setenv('ASTRALNEXUS_MODEL', 'main-model')
    monkeypatch.setenv('ASTRALNEXUS_SUB_MODEL', sub)
    config = get_model_config()
    config['_model_explicit_override'] = explicit
    with Orchestrator(run_dir=tmp_path / 'run') as orchestrator:
        orchestrator.enable_real_llm_worker_matching(config)
        assert clients[-1].model == 'main-model'
        assert orchestrator.verifier.llm_client is clients[-1]
        assert orchestrator.governor.forced_worker_model == expected
