import json
import pytest
from AstralNexus.tools.context import tool_workspace
from AstralNexus.tools.dsh_adapters import present, runtime_inspect, session_history
from AstralNexus.runtime.workbench import WorkbenchStore
from AstralNexus.runtime.workbench_chat import ChatService


def test_present_hashes_actual_file_without_accepting_it(tmp_path):
    (tmp_path/'out.txt').write_text('actual output')
    with tool_workspace(tmp_path):
        result = json.loads(present({'files':[{'path':'out.txt'}]}))
    assert result['acceptance'] == 'unverified'
    assert result['files'][0]['bytes'] == 13
    assert len(result['files'][0]['sha256']) == 64


@pytest.mark.parametrize('files', [[], [{'path':'missing'}], [{'path':'..'}], [{'path':'.'}], [{}]])
def test_present_rejects_invalid_delivery(tmp_path, files):
    with tool_workspace(tmp_path), pytest.raises(ValueError):
        present({'files':files})


def test_runtime_inspection_reports_disabled_tools(monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_DISABLED_TOOLSETS', '["deliverables"]')
    result = json.loads(runtime_inspect({'query':'present'}))
    assert result['tools'][0]['name'] == 'present'
    assert result['tools'][0]['available'] is False


def test_history_is_bounded_and_session_scoped(tmp_path, monkeypatch):
    store=WorkbenchStore(tmp_path/'host')
    a=store.register_session('a',str(tmp_path));b=store.register_session('b',str(tmp_path))
    chat=ChatService(store)
    one=chat.submit(a,'needle one','a');two=chat.submit(b,'needle private','b')
    with store.db() as db:
        db.execute("UPDATE chat_turns SET status='completed',answer='answer' WHERE id IN (?,?)",(one,two))
    monkeypatch.setenv('ASTRALNEXUS_CHAT_HOME',str(store.home));monkeypatch.setenv('ASTRALNEXUS_CHAT_SESSION',a)
    result=json.loads(session_history({'query':'needle','limit':1,'session_id':b}))
    assert [r['id'] for r in result['turns']] == [one]
    assert result['scope'] == 'current_conversation'
    assert json.loads(session_history({'query':'not found'}))['turns']==[]
