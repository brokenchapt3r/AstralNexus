"""Browser proof for approvals hidden by monitor tabs or another conversation."""
import _winapi
import os
from pathlib import Path
import sys
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from AstralNexus.dashboard.workbench_server import WorkbenchHost


def _anonymous_pipes_available() -> bool:
    """See test_workbench_activity_ui: Playwright's asyncio transport needs
    anonymous pipes the sandbox refuses, and the failed run poisons AST
    recursion accounting for later tests."""
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


def test_pending_approvals_surface_over_monitor_and_other_session(tmp_path):
    host = WorkbenchHost(tmp_path / 'host')
    actual = host.store.register_session('执行中的会话', str(tmp_path))
    other = host.store.register_session('查看中的会话', str(tmp_path))
    host.select(other)
    fixture = CHANNEL + '''
json.loads(inbound.readline())
for ident in ['first','second']:
 event('approval.request',{'id':ident,'tool':'file_write','command':ident+'.md'})
answers=[json.loads(inbound.readline()) for _ in range(2)]
assert answers[0]['id']=='first' and answers[0]['choice']=='once'
assert answers[1]['id']=='second' and answers[1]['choice']=='deny'
event('turn.result',{'status':'failed'})
sys.exit(1)
'''
    host.chat.command = [sys.executable, '-u', '-c', fixture]
    host.chat.submit(actual, 'offline approval fixture', 'test')
    server = make_server('127.0.0.1', 0, host.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host.start_services()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get('ASTRALNEXUS_TEST_CHROME', 'C:/Program Files/Google/Chrome/Application/chrome.exe'))
            page = browser.new_page(viewport={'width': 900, 'height': 650})
            page.add_init_script("localStorage.setItem('astralnexus.workbench.tab.v2','monitor')")
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            expect(page.locator('#monitor')).to_be_visible()
            expect(page.locator('#chat')).to_be_hidden()
            expect(page.locator('#approval-dialog')).to_be_visible()
            expect(page.locator('#approval-items .approval')).to_have_count(2)
            expect(page.locator('#approval-items')).to_contain_text('执行中的会话')
            page.locator('#approval-close').click()
            expect(page.locator('#approval-dialog')).to_be_hidden()
            expect(page.locator('#approval-alert')).to_be_visible()
            assert len(host.chat.snapshot(actual)['approvals']) == 2
            page.locator('#approval-alert').click()
            expect(page.locator('#approval-dialog')).to_be_visible()
            page.reload()
            expect(page.locator('#approval-dialog')).to_be_visible()
            expect(page.locator('#approval-items .approval')).to_have_count(2)
            page.locator('[data-approval-id="first"] [data-choice="once"]').click()
            expect(page.locator('#approval-items .approval')).to_have_count(1)
            page.locator('[data-approval-id="second"] [data-choice="deny"]').click()
            expect(page.locator('#approval-dialog')).to_be_hidden()
            expect(page.locator('#approval-alert')).to_be_hidden()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        host.close()
