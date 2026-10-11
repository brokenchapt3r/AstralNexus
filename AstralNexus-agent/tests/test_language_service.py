import json
from pathlib import Path
import shutil
import sys

import pytest

from AstralNexus.runtime.language_service import LanguageService


def _anonymous_pipes_available() -> bool:
    """LSP is stdio-carried: the server reads/writes its stdin/stdout through
    anonymous pipes. The sandbox refuses CreatePipe (WinError 5) and named
    pipes alike (P0-4/P1 boundary chain, 台账 §25/§29), so the REAL language
    server tests cannot run here. Same probe shape as test_builtin_tools
    (R14) — skipping at the source is preferred over letting a failed spawn
    poison the run; the refusal surface itself is tested below.
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


_lsp_stdio_unavailable = pytest.mark.skipif(
    not _anonymous_pipes_available(),
    reason='stdio LSP transport cannot spawn in this sandbox (CreatePipe '
           'refused; P1-lsp boundary) — real language server tests need pipes')


@_lsp_stdio_unavailable
def test_actual_typescript_lsp_navigation_and_workspace_process_reuse(tmp_path):
    from AstralNexus.dashboard.workbench_server import WorkbenchHost
    package = Path(__file__).resolve().parents[3] / '03_验证脚本/.tooling/lsp/node_modules/typescript-language-server/lib/cli.mjs'
    node = shutil.which('node')
    if not node or not package.is_file():
        pytest.skip('Run npm ci in 03_验证脚本/.tooling/lsp for actual language server test')
    (tmp_path / 'main.ts').write_text('export const answer: number = 42;\nconsole.log(answer);\n', encoding='utf-8')
    host = WorkbenchHost(tmp_path / 'host', [node, str(package), '--stdio'])
    try:
        ident = host.store.register_session('ts', str(tmp_path))
        result = host.query_lsp(ident, {'operation': 'hover', 'path': 'main.ts', 'line': 1, 'character': 14})
        assert 'number' in json.dumps(result['result']), result
        second = host.query_lsp(ident, {'operation': 'definition', 'path': 'main.ts', 'line': 1, 'character': 14})
        assert second['result'][0]['range']['start']['line'] == 0
        assert second['server_pid'] == result['server_pid']
        with pytest.raises(ValueError, match='workspace'):
            host.query_lsp(ident, {'operation': 'hover', 'path': '../outside.ts'})
    finally:
        processes = [s.process for s in host.services.values()]
        host.close()
        assert all(p.poll() is not None for p in processes)


@_lsp_stdio_unavailable
def test_lsp_timeout_terminates_actual_subprocess(tmp_path):
    script = tmp_path / 'hang.py'
    script.write_text('import time;time.sleep(30)', encoding='utf-8')
    with pytest.raises(TimeoutError):
        LanguageService(tmp_path, [sys.executable, str(script)], timeout=.15)


@_lsp_stdio_unavailable
def test_actual_python_provider_selected_for_python_source(tmp_path):
    import importlib.util
    from AstralNexus.dashboard.workbench_server import WorkbenchHost
    if not importlib.util.find_spec('pylsp'):
        pytest.skip('python-lsp-server is not installed')
    (tmp_path / 'source.py').write_text('def twice(value: int) -> int:\n    return value * 2\n\ntwice(5)\n', encoding='utf-8')
    host = WorkbenchHost(tmp_path / 'host')
    try:
        session = host.store.register_session('python', str(tmp_path))
        result = host.query_lsp(session, {'operation': 'definition', 'path': 'source.py', 'line': 3, 'character': 2})
        assert result['result'][0]['range']['start']['line'] == 0
    finally:
        host.close()


@pytest.mark.skipif(_anonymous_pipes_available(),
                    reason='the typed refusal surface is environment-specific; '
                           'with working pipes the real-server tests above cover the chain')
def test_lsp_http_surface_reports_typed_unavailable_when_pipes_refused(tmp_path):
    """The HTTP boundary maps the sandbox's pipe refusal to a typed
    unavailable response (503 + status), never an unhandled 500 and never a
    silent success — 错误回传不等于能力可用, but the surface must stay
    machine-readable (R18, 台账 §29)."""
    from AstralNexus.dashboard.workbench_server import WorkbenchHost
    (tmp_path / 'main.ts').write_text('export const answer: number = 42;\n', encoding='utf-8')
    host = WorkbenchHost(tmp_path / 'host', [sys.executable, '-c', 'pass'])
    try:
        ident = host.store.register_session('lsp-unavailable', str(tmp_path))
        client = host.app.test_client()
        response = client.post('/api/host/lsp', json={
            'session_id': ident, 'operation': 'hover',
            'path': 'main.ts', 'line': 0, 'character': 0})
        assert response.status_code == 503, response.get_data(as_text=True)
        body = response.get_json()
        assert body['status'] == 'unavailable'
        assert body['error']
        assert 'Traceback' not in response.get_data(as_text=True)
        # the refusal must not poison the session: a second query answers
        # the same typed shape instead of a broken-process 500
        second = client.post('/api/host/lsp', json={
            'session_id': ident, 'operation': 'hover',
            'path': 'main.ts', 'line': 0, 'character': 0})
        assert second.status_code == 503 and second.get_json()['status'] == 'unavailable'
    finally:
        host.close()
