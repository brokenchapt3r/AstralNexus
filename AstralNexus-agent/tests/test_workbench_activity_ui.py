"""Synthetic UI events, real Chrome rendering; never executes model requests."""
import _winapi
import os
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from AstralNexus.dashboard.workbench_server import WorkbenchHost


def _anonymous_pipes_available() -> bool:
    """Playwright's asyncio subprocess transport requires anonymous pipes,
    which this host's sandbox refuses (CreatePipe/CreateFile WinError 5).
    Running anyway also corrupts later tests: the failed greenlet stack
    leaves CPython's AST recursion accounting poisoned (SystemError in
    ast.parse), so the boundary is declared up front."""
    try:
        read, write = _winapi.CreatePipe(None, 0)
    except OSError:
        return False
    _winapi.CloseHandle(read)
    _winapi.CloseHandle(write)
    return True


pytestmark = pytest.mark.skipif(
    not _anonymous_pipes_available(),
    reason='sandbox refuses the anonymous pipes Playwright needs')


def test_conversation_progress_tools_results_and_expansion_survive_poll(tmp_path):
    host = WorkbenchHost(tmp_path / 'host')
    session = host.store.register_session('offline presentation', str(tmp_path))
    host.select(session)
    ident = host.chat.submit(session, 'Build the fixture', 'ui')
    turn = {'id': ident, 'session_id': session}
    with host.store.db() as db:
        db.execute("UPDATE chat_turns SET status='running',answer=? WHERE id=?", ('**已启动**\n`fixture`\n<script>bad()</script>', ident))
    def emit(kind, **payload):
        host.chat.record(turn, 'progress', {'type': kind, **payload})
    emit('planning.summary', summary='Implement, test, verify')
    emit('agent.spawned', node_id='N-1', role='Coder', goal='Build service')
    emit('llm.tool_execution.start', node_id='N-1', tool_name='terminal_exec', arguments={'command': 'python -m pytest'})
    emit('llm.tool_batch', node_id='N-1', tool_name='terminal_exec', status='success', text='3 passed')
    emit('agent.activity', node_id='N-1', status='completed', title='Coder 阶段完成', summary='Implementation submitted, not verified yet')
    emit('runtime.progress.report', text='节点：完成 1，运行 1，验证 0，失败 0，已知总数 3')
    server = make_server('127.0.0.1', 0, host.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get('ASTRALNEXUS_TEST_CHROME', 'C:/Program Files/Google/Chrome/Application/chrome.exe'))
            page = browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.locator('#permission-trigger').click()
            page.locator('[data-permission=assist]').click()
            expect(page.locator('#permission-trigger')).to_contain_text('帮我批准')
            assert host.chat.permissions(session)['mode'] == 'assist'
            page.locator('#sidebar-settings').click()
            expect(page.locator('#settings [data-permission]')).to_have_count(0)
            page.reload()
            expect(page.locator('#main-picker')).to_be_visible()
            page.locator('#settings [data-tab=extensions]').click()
            expect(page.locator('#plugin-list .plugin-row').first).to_be_visible()
            page.locator('nav [data-tab=chat]').click()
            page.locator('#permission-trigger').click()
            expect(page.locator('#permission-menu')).to_be_visible()
            expect(page.locator('[data-permission=assist]')).to_have_attribute('aria-checked', 'true')
            page.locator('[data-permission=full]').click()
            expect(page.locator('#permission-menu')).to_be_hidden()
            expect(page.locator('#permission-trigger')).to_contain_text('完全访问')
            assert host.chat.permissions(session)['mode'] == 'full'
            page.locator('#permission-trigger').click()
            page.keyboard.press('Escape')
            expect(page.locator('#permission-menu')).to_be_hidden()
            page.reload()
            expect(page.locator('#permission-trigger')).to_contain_text('完全访问')

            expect(page.locator('#messages .answer strong')).to_have_text('已启动')
            expect(page.locator('#messages .answer script')).to_have_count(0)
            expect(page.locator('#messages .plan-card')).to_contain_text('1/3 节点完成')
            expect(page.locator('#messages .plan-row')).to_contain_text('结果已提交 / 待验收')
            card = page.locator('#messages details').filter(has=page.locator('summary', has_text='python -m pytest'))
            expect(card).to_have_count(1)
            card.locator('summary').click()
            expect(card.locator('pre')).to_be_visible()
            expect(card.locator('pre')).to_contain_text('3 passed')
            emit('runtime.progress.report', text='正在独立复验，未发布')
            expect(page.locator('#messages .runtime-update')).to_have_text('正在独立复验，未发布')
            expect(card.locator('pre')).to_be_visible()
            expect(page.locator('#messages')).to_contain_text('Implementation submitted, not verified yet')
            expect(page.locator('.workflow-scroll')).to_be_hidden()
            page.locator('#toggle-inspector').click()
            page.locator('#activity-filter').select_option('agents')
            expect(page.locator('#timeline .agent-card')).to_contain_text('Implementation submitted')
            expect(page.locator('#timeline .activity-card')).to_have_count(0)
            page.reload()
            expect(page.locator('#messages .plan-card')).to_contain_text('1/3 节点完成')
            # Replay the actual legacy shape: three same-name starts and three
            # results, plus a mirrored public completion. No permanently pending rows.
            projected = page.evaluate("""() => {
                let seq=10000;
                const emit=(type,p={})=>projectActivity({turn_id:'regression',seq:seq++,created:100,payload:{type,node_id:'N-01',...p}});
                for(let i=0;i<3;i++)emit('llm.tool_execution.start',{tool_name:'file_read'});
                emit('tool.started',{summary:'file_read'});
                emit('agent.activity',{title:'工具 file_read 执行完成',summary:'tool_result',status:'running'});
                for(let i=0;i<3;i++)emit('llm.tool_batch',{tool_name:'file_read',status:'success',text:'read '+i});
                emit('tool.finished',{summary:'file_read',status:'completed'});
                const legacy=viewFor('regression').tools.map(t=>({returned:t.returned,response:t.response}));
                // IDs distinguish out-of-order results and deduplicate done/batch.
                for(const id of ['a','b'])emit('llm.tool_execution.start',{tool_name:'python_exec',call_id:id,arguments:{command:id}});
                for(const id of ['b','a'])for(const type of ['llm.tool_execution.done','llm.tool_batch'])emit(type,{tool_name:'python_exec',call_id:id,status:id==='a'?'error':'success',text:'result '+id});
                emit('tool.finished',{summary:'python_exec',status:'completed'});
                return {legacy,identified:viewFor('regression').tools.filter(t=>t.callId).map(t=>({id:t.callId,status:t.status,response:t.response}))};
            }""")
            assert projected['legacy'] == [{'returned': True, 'response': f'read {i}'} for i in range(3)]
            assert projected['identified'] == [
                {'id': 'a', 'status': 'error', 'response': 'result a'},
                {'id': 'b', 'status': 'success', 'response': 'result b'},
            ]
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        host.close()
