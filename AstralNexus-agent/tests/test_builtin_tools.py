import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools.builtin_tools import (
    _bind_runtime_executable,
    _normalize_terminal_command,
    _run_foreground_command,
)


def _anonymous_pipes_available() -> bool:
    """The sandbox refuses CreatePipe (WinError 5); PIPE-shaped tests cannot run.

    Same probe shape as the Playwright UI modules (台账 §23.2): skipping at
    the source is preferred over letting a failed spawn poison the run.
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


def _process_enumeration_available() -> bool:
    """tasklist/netstat-class enumeration tools are Access-denied in this sandbox."""
    if sys.platform != "win32":
        return True
    try:
        probe = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq __astral_probe__.exe"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def test_windows_explicit_cmd_preserves_quoted_command_string(tmp_path):
    import pytest
    if sys.platform != 'win32':
        pytest.skip('cmd quoting is Windows-specific')
    command = [
        'cmd.exe', '/c',
        f'"{sys.executable}" -c "print(\'quoted space 中文\')"',
    ]
    result = _run_foreground_command(command, cwd=tmp_path, env=os.environ.copy(), timeout=10)
    assert result['returncode'] == 0, result
    assert 'quoted space 中文' in result['stdout']


@pytest.mark.skipif(not _process_enumeration_available(),
                    reason='sandbox denies tasklist-class enumeration tools')
def test_windows_cmd_filter_quotes_survive_compound_expression(tmp_path):
    import pytest
    if sys.platform != 'win32':
        pytest.skip('cmd quoting is Windows-specific')
    result = _run_foreground_command(
        ['cmd.exe', '/d', '/s', '/c',
         'echo PROBE & tasklist /FI "IMAGENAME eq __astral_missing__.exe" /FO CSV'],
        cwd=tmp_path, env=os.environ.copy(), timeout=10,
    )
    assert result['returncode'] == 0, result
    assert 'PROBE' in result['stdout']


def test_windows_batch_forward_slash_path_runs_without_changing_cwd(tmp_path, monkeypatch):
    import pytest
    if sys.platform != 'win32':
        pytest.skip('cmd path parsing is Windows-specific')
    from AstralNexus.tools.builtin_tools import _run_process
    monkeypatch.setenv('ASTRALNEXUS_WORKSPACE', str(tmp_path))
    folder = tmp_path / 'project scripts'
    folder.mkdir()
    (folder / 'check.bat').write_text('@echo off\necho SCRIPT_OK\necho %CD%\n', encoding='ascii')
    result = json.loads(_run_process({}, ['cmd', '/c', 'project scripts/check.bat'], 'terminal_exec'))
    assert result['returncode'] == 0
    assert 'SCRIPT_OK' in result['stdout']
    assert str(tmp_path) in result['stdout']


def test_failed_literal_pipeline_gets_hint_without_shell_retry(tmp_path, monkeypatch):
    from AstralNexus.tools.builtin_tools import _run_process
    monkeypatch.setenv('ASTRALNEXUS_WORKSPACE', str(tmp_path))
    result = json.loads(_run_process({}, [sys.executable, '-c', 'import sys; sys.exit(2)', '|', 'anything'], 'terminal_exec'))
    assert result['returncode'] == 2
    assert result['status'] == 'failed'
    assert 'without a shell' in result['command_hint']


def test_runtime_and_parent_pid_cannot_be_killed_by_worker(tmp_path, monkeypatch):
    from AstralNexus.tools import builtin_tools
    monkeypatch.setenv('ASTRALNEXUS_WORKSPACE', str(tmp_path))
    def must_not_spawn(*args, **kwargs):
        raise AssertionError('protected process command must not execute')
    monkeypatch.setattr(builtin_tools, '_run_foreground_command', must_not_spawn)
    for command in [
        ['taskkill', '/F', '/PID', str(os.getpid())],
        ['taskkill', '/F', '/PID', str(os.getppid())],
        ['cmd', '/c', f'taskkill /F /PID {os.getpid()}'],
        ['powershell', '-Command', f'Stop-Process -Id {os.getppid()}'],
    ]:
        result = json.loads(builtin_tools._run_process({}, command, 'terminal_exec'))
        assert result['status'] == 'error'
        assert 'runtime_process_kill_blocked' in result['error']


def test_exact_edit_preserves_crlf_and_rejects_stale_or_ambiguous_input(tmp_path, monkeypatch):
    from AstralNexus.tools.builtin_tools import _file_read, _file_write
    monkeypatch.setenv('ASTRALNEXUS_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('ASTRALNEXUS_ALLOW_FILE_WRITE', 'true')
    path = tmp_path / 'main.py'
    path.write_bytes(b'x = 1\r\ny = 1\r\n')
    read = json.loads(_file_read({'path': 'main.py'}))
    assert read['content'] == 'x = 1\r\ny = 1\r\n'
    args = {'path': 'main.py', 'mode': 'replace', 'old_text': 'x = 1',
            'content': 'x = 2', 'expected_sha256': read['sha256']}
    assert json.loads(_file_write(args))['status'] == 'success'
    assert path.read_bytes() == b'x = 2\r\ny = 1\r\n'

    assert json.loads(_file_write(args))['status'] == 'error'
    read = json.loads(_file_read({'path': 'main.py'}))
    args.update(old_text=' = ', content='=', expected_sha256=read['sha256'])
    assert json.loads(_file_write(args))['status'] == 'error'
    assert path.read_bytes() == b'x = 2\r\ny = 1\r\n'


def test_large_command_output_is_saved_without_model_transcription(tmp_path, monkeypatch):
    from AstralNexus.tools.builtin_tools import _run_process
    monkeypatch.setenv('ASTRALNEXUS_WORKSPACE', str(tmp_path))
    result = json.loads(_run_process({}, [sys.executable, '-c', "print('BEGIN' + 'x'*15000 + 'END')"], 'terminal_exec'))
    assert result['status'] == 'success'
    assert result['stdout_truncated'] is True
    assert len(result['stdout']) == 12000
    full = Path(result['stdout_log_path']).read_text(encoding='utf-8')
    assert full.startswith('BEGIN') and full.rstrip().endswith('END')
    assert 'do not transcribe' in result['output_evidence_hint']


def test_builtin_execution_tools_return_structured_results(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    executor = ToolExecutor()

    result = executor.execute_tool("python_exec", {"code": "print(2 + 3)"})
    assert result["status"] == "success"
    assert result["stdout"].strip() == "5"


def test_file_read_exposes_encoding_and_recovers_legacy_log(tmp_path):
    from AstralNexus.tools.registry import registry
    log = tmp_path / "server.log"
    log.write_bytes("启动失败：无法导入模块".encode("gb18030"))
    executor = ToolExecutor(workspace_root=tmp_path)
    result = executor.execute_tool("file_read", {"path": "server.log"})
    assert result["status"] == "error" and "encoding" in result["error"]
    result = executor.execute_tool("file_read", {"path": "server.log", "encoding": "gb18030"})
    assert result["status"] == "success" and result["content"] == "启动失败：无法导入模块"
    assert "encoding" in registry.get_entry("file_read").schema["parameters"]["properties"]


def test_python_exec_uses_script_file_for_windows_sized_payload(tmp_path):
    executor = ToolExecutor(workspace_root=tmp_path)
    code = "# padding\n" * 5000 + "print('long-payload-ok')\n"

    result = executor.execute_tool("python_exec", {"code": code})

    assert result["status"] == "success"
    assert result["stdout"].strip() == "long-payload-ok"
    assert not list(tmp_path.glob(".astralnexus_python_*.py"))


def test_python_exec_overrides_inherited_gbk_console_for_unicode_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    monkeypatch.setenv("PYTHONUTF8", "1")
    executor = ToolExecutor(workspace_root=tmp_path)

    result = executor.execute_tool(
        "python_exec",
        {"code": "import sys; print(sys.stdout.encoding); print('❌')"},
    )

    assert result["status"] == "success", result
    assert result["stdout"].splitlines() == ["utf-8", "❌"]


def test_file_read_is_workspace_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    target = Path(tmp_path) / "evidence.txt"
    target.write_text("verified", encoding="utf-8")
    executor = ToolExecutor()

    result = executor.execute_tool("file_read", {"path": "evidence.txt"})
    assert result["status"] == "success"
    assert result["content"] == "verified"

    denied = executor.execute_tool("file_read", {"path": str(Path(tmp_path).parent / "outside.txt")})
    assert denied["status"] == "error"


def test_file_read_extracts_docx_body_and_table_text(tmp_path):
    path = tmp_path / "disclosure.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '''<w:document xmlns:w="urn:word"><w:body>
          <w:p><w:r><w:t>技术</w:t><w:t>方案</w:t><w:tab/><w:t>输入</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>参数</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>''')
    executor = ToolExecutor(workspace_root=tmp_path)
    result = executor.execute_tool("file_read", {"path": "disclosure.docx"})
    assert result["status"] == "success"
    assert result["content"] == "技术方案\t输入\n参数"
    assert result["extraction"] == "docx_main_body_text_only"
    limited = executor.execute_tool("file_read", {"path": "disclosure.docx", "max_chars": 4})
    assert limited["content"] == "技术方案"
    assert limited["truncated"] is True


def test_invalid_docx_does_not_report_success(tmp_path):
    (tmp_path / "invalid.docx").write_bytes(b"not an office archive")
    result = ToolExecutor(workspace_root=tmp_path).execute_tool("file_read", {"path": "invalid.docx"})
    assert result["status"] == "error"


def test_executor_workspace_root_materializes_relative_files(tmp_path, monkeypatch):
    """Runtime workers must write into their run, not the process workspace."""
    monkeypatch.setenv("ASTRALNEXUS_ALLOW_FILE_WRITE", "true")
    process_workspace = tmp_path / "process-workspace"
    run_workspace = tmp_path / "run-001"
    process_workspace.mkdir()
    run_workspace.mkdir()
    monkeypatch.chdir(process_workspace)

    executor = ToolExecutor(workspace_root=run_workspace)
    result = executor.execute_tool(
        "file_write",
        {"path": "deliverables/app/main.py", "content": "print('ok')\n"},
    )

    assert result["status"] == "success"
    assert (run_workspace / "deliverables/app/main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not (process_workspace / "deliverables/app/main.py").exists()

    denied = executor.execute_tool(
        "file_write", {"path": "../escape.py", "content": "bad"}
    )
    assert denied["status"] == "error"


def test_executor_workspace_root_is_used_by_process_tools(tmp_path):
    executor = ToolExecutor(workspace_root=tmp_path)
    result = executor.execute_tool(
        "python_exec", {"code": "from pathlib import Path; print(Path.cwd())"}
    )
    assert result["status"] == "success"
    assert Path(result["stdout"].strip()).resolve() == tmp_path.resolve()


def test_foreground_timeout_reaps_child_process_tree(tmp_path):
    started = time.monotonic()
    result = _run_foreground_command(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                "time.sleep(30)"
            ),
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=1,
    )

    assert result["timed_out"] is True
    assert time.monotonic() - started < 10


def test_terminal_command_normalizes_provider_json_variants():
    assert _normalize_terminal_command(["python", "--version"]) == ["python", "--version"]
    assert _normalize_terminal_command('["python", "--version"]') == ["python", "--version"]
    assert _normalize_terminal_command(['["python"', '"--version"]']) == ["python", "--version"]


def test_python_entry_points_use_runtime_interpreter():
    assert _bind_runtime_executable(["python", "-m", "pytest"]) == [
        sys.executable, "-m", "pytest"
    ]
    assert _bind_runtime_executable(["pytest", "-q"]) == [
        sys.executable, "-m", "pytest", "-q"
    ]
    assert _bind_runtime_executable(["pip", "--version"]) == [
        sys.executable, "-m", "pip", "--version"
    ]


def test_windows_overescaped_python_snippet_executes_without_shell(tmp_path, monkeypatch):
    if os.name != 'nt':
        return
    monkeypatch.setenv('ASTRALNEXUS_TOOL_APPROVAL', 'true')
    command = r'python -c \"import sys; print(2 + 3)\"'
    assert _normalize_terminal_command(command) == ['python', '-c', 'import sys; print(2 + 3)']
    result = ToolExecutor(workspace_root=tmp_path).execute_tool('terminal_exec', {'command': command})
    assert result['returncode'] == 0, result
    assert result['stdout'].strip() == '5'
    chained = command + ' && echo untouched'
    assert _normalize_terminal_command(chained)[-1] == chained


def test_terminal_exec_blocks_mass_process_kill(tmp_path, monkeypatch):
    """Round 12 (live r11 resume #17, 18:50:25 UTC): a worker's
    `taskkill /F /IM python.exe` mass-killed the orchestrator, the monitor
    and the live service.  Image/pattern-name kills must be blocked at the
    tool layer with teaching text; PID-targeted kills stay allowed."""
    monkeypatch.setenv('ASTRALNEXUS_TOOL_APPROVAL', 'true')
    executor = ToolExecutor(workspace_root=tmp_path)
    for command in (
        ['taskkill', '/F', '/IM', 'python.exe'],
        'taskkill /f /im python.exe',
        ['cmd', '/c', 'taskkill /F /IM python.exe'],
        ['powershell', '-Command', 'Stop-Process -Name python'],
        ['pkill', '-f', 'uvicorn'],
        ['killall', 'python'],
    ):
        result = executor.execute_tool('terminal_exec', {'command': command})
        assert result.get('status') == 'approval_required', command
        assert 'mass_process_kill_blocked' in str(result.get('error', '')), command

    targeted = executor.execute_tool(
        'terminal_exec', {'command': ['taskkill', '/F', '/PID', '999999']}
    )
    assert 'mass_process_kill_blocked' not in str(targeted)


def test_research_and_browser_tools_are_registered():
    executor = ToolExecutor()
    tools = set(executor.list_tools())
    assert {"openalex_search", "crossref_search", "arxiv_search", "github_repo_contents"} <= tools
    assert "browser_fetch" in tools


def test_browser_fetch_renders_a_real_page_with_a_real_browser(tmp_path):
    """browser_fetch must drive a REAL browser, not answer a typed unavailable.

    The probe page rewrites its own DOM after load, so only a real renderer can
    produce the asserted text — an HTTP fetch cannot fake it. When this machine
    truly has no browser provider the handler answers a typed unavailable, and
    that is an honest environment skip instead of a passing render.
    """
    from AstralNexus.tools.builtin_tools import _browser_fetch

    page = tmp_path / 'page.html'
    page.write_text(
        '<!doctype html><html><head><title>real browser render</title></head>'
        '<body><h1 id="h">raw</h1><script>'
        'document.getElementById("h").textContent = '
        '"rendered by a real browser";</script></body></html>',
        encoding='utf-8')
    payload = json.loads(_browser_fetch({'url': page.as_uri()}))
    if payload.get('status') == 'unavailable':
        pytest.skip('no browser provider on this machine: %s'
                    % payload.get('error'))
    assert payload.get('status') == 'success', payload
    assert payload.get('title') == 'real browser render', payload
    assert 'rendered by a real browser' in payload.get('text', ''), payload


def test_tool_batch_parallelizes_safe_calls_and_preserves_order(monkeypatch):
    executor = ToolExecutor()
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_execute(name, params):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"status": "success", "tool": name, "index": params["index"]}

    monkeypatch.setattr(executor, "execute_tool", fake_execute)
    calls = [
        {"function": {"name": "web_search", "arguments": '{"index": 1}'}},
        {"function": {"name": "http_get", "arguments": '{"index": 2}'}},
        {"function": {"name": "terminal_exec", "arguments": '{"index": 3}'}},
    ]
    results = executor.execute_tool_calls(calls)
    assert [item["index"] for item in results] == [1, 2, 3]
    assert peak >= 2


def test_file_write_append_preserves_chunks_and_rejects_duplicate_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_ALLOW_FILE_WRITE', 'true')
    executor = ToolExecutor(workspace_root=tmp_path, owned_paths=['app/'])
    first = executor.execute_tool('file_write', {'path': 'app/main.py', 'content': '# 中文\n'})
    assert first['status'] == 'success'
    call = {'path': 'app/main.py', 'content': 'print(42)\n', 'mode': 'append', 'expected_bytes': first['bytes']}
    assert executor.execute_tool('file_write', call)['status'] == 'success'
    assert executor.execute_tool('file_write', call)['status'] == 'error'
    assert (tmp_path / 'app/main.py').read_text(encoding='utf-8') == '# 中文\nprint(42)\n'
    assert executor.execute_tool('file_write', {'path': 'outside.txt', 'content': 'bad', 'mode': 'append', 'expected_bytes': 0})['status'] == 'error'


@pytest.mark.skipif(not _anonymous_pipes_available(),
                    reason='sandbox refuses the anonymous pipes this test requires')
def test_tool_child_does_not_inherit_busy_host_control_pipe(tmp_path):
    import subprocess
    script = r"""
import os, sys, threading, time, json
from pathlib import Path
from AstralNexus.tools.builtin_tools import _run_foreground_command
threading.Thread(target=sys.stdin.readline, daemon=True).start()
time.sleep(.2)
result = _run_foreground_command([sys.executable, '-c', 'print("child ok", flush=True)'], cwd=Path.cwd(), env=dict(os.environ), timeout=3)
print(json.dumps(result), flush=True)
os._exit(0)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    parent = subprocess.Popen([sys.executable, '-u', '-c', script], cwd=tmp_path, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # communicate() would close stdin and mask the inherited-pipe defect.
        parent.wait(timeout=12)
        assert parent.stdout is not None and parent.stderr is not None
        result = json.loads(parent.stdout.read())
        assert not result['timed_out'], result
        assert result['returncode'] == 0, result
        assert 'child ok' in result['stdout']
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for stream in (parent.stdin, parent.stdout, parent.stderr):
            if stream is not None:
                stream.close()
