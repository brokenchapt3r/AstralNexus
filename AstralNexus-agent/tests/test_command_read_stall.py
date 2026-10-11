import json
import subprocess
import sys

import pytest

from AstralNexus.runtime.tool_guardrails import ToolCallGuard


def call(name, **arguments):
    return {"function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.mark.parametrize("tool", ["python_exec", "terminal_exec"])
def test_read_only_command_cannot_erase_unchanged_reads(tmp_path, tool):
    target = tmp_path / "source.txt"
    target.write_text("unchanged", encoding="utf8")
    guard = ToolCallGuard(workspace_root=tmp_path, read_streak_limit=2)
    read = call("file_read", path="source.txt")
    for i in range(3):
        assert guard.admit(read)[0]
        guard.observe(read, {"status": "success"})
        code = f"print(open('source.txt').read()) # read {i}"
        command = call(tool, **({"code": code} if tool == "python_exec" else {"command": [sys.executable, "-c", code]}))
        assert guard.admit(command)[0]
        # R17 sandbox-compatible capture (same shape as the r17 replay fix):
        # anonymous pipes are refused here (CreatePipe WinError 5), so the
        # read-only command's stdout is collected through file redirection.
        # The guard observation and every assertion are unchanged.
        stdout_path = tmp_path / f"stall_stdout_{i}.txt"
        with open(stdout_path, "wb") as out_f:
            result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                                    stdin=subprocess.DEVNULL, stdout=out_f,
                                    stderr=subprocess.DEVNULL)
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        assert result.returncode == 0
        guard.observe(command, {"status": "success", "returncode": result.returncode, "stdout": stdout_text})
    assert not guard.admit(read)[0]
    assert target.read_text() == "unchanged"
    assert guard.decision(read)["action"] == "halt"


def test_actual_python_mutation_allows_new_file_version(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("old", encoding="utf8")
    guard = ToolCallGuard(workspace_root=tmp_path, read_streak_limit=1)
    read = call("file_read", path="source.txt")
    for _ in range(2):
        assert guard.admit(read)[0]
        guard.observe(read, {"status": "success"})
    assert not guard.admit(read)[0]
    code = "from pathlib import Path; Path('source.txt').write_text('new')"
    command = call("python_exec", code=code)
    assert guard.admit(command)[0]
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path)
    assert result.returncode == 0
    guard.observe(command, {"status": "success", "returncode": 0})
    assert guard.admit(read)[0]


@pytest.mark.parametrize("tool", ["pytest_run", "file_write", "http_get"])
def test_dedicated_action_tools_keep_existing_reset_semantics(tmp_path, tool):
    (tmp_path / "source.txt").write_text("v1", encoding="utf8")
    guard = ToolCallGuard(workspace_root=tmp_path, read_streak_limit=1)
    read = call("file_read", path="source.txt")
    for _ in range(2):
        assert guard.admit(read)[0]
        guard.observe(read, {"status": "success"})
    guard.observe(call(tool), {"status": "success"})
    assert guard.admit(read)[0]


def test_failed_action_and_printed_progress_do_not_reset(tmp_path):
    (tmp_path / "source.txt").write_text("v1", encoding="utf8")
    guard = ToolCallGuard(workspace_root=tmp_path, read_streak_limit=1)
    read = call("file_read", path="source.txt")
    for _ in range(2):
        guard.admit(read)
        guard.observe(read, {"status": "success"})
    guard.observe(call("python_exec", code="print('tests passed; file changed')"), {"status": "success", "stdout": "tests passed; file changed"})
    guard.observe(call("pytest_run"), {"status": "failed", "returncode": 1})
    assert not guard.admit(read)[0]


def test_stalled_reads_do_not_prohibit_test_startup_or_probe_commands(tmp_path):
    (tmp_path / "source.txt").write_text("v1", encoding="utf8")
    guard = ToolCallGuard(workspace_root=tmp_path, read_streak_limit=1)
    read = call("file_read", path="source.txt")
    for _ in range(2):
        guard.admit(read)
        guard.observe(read, {"status": "success"})
    for arguments in (
        {"command": [sys.executable, "-m", "pytest"]},
        {"command": [sys.executable, "server.py"], "background": True},
        {"command": ["curl", "http://127.0.0.1:8000/health"]},
    ):
        assert guard.admit(call("terminal_exec", **arguments))[0]
