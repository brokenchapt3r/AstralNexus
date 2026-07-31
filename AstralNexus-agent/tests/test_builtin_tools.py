import json
import os
import threading
import time
from pathlib import Path

from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools.builtin_tools import _normalize_terminal_command


def test_builtin_execution_tools_return_structured_results(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    executor = ToolExecutor()

    result = executor.execute_tool("python_exec", {"code": "print(2 + 3)"})
    assert result["status"] == "success"
    assert result["stdout"].strip() == "5"


def test_python_exec_uses_script_file_for_windows_sized_payload(tmp_path):
    executor = ToolExecutor(workspace_root=tmp_path)
    code = "# padding\n" * 5000 + "print('long-payload-ok')\n"

    result = executor.execute_tool("python_exec", {"code": code})

    assert result["status"] == "success"
    assert result["stdout"].strip() == "long-payload-ok"
    assert not list(tmp_path.glob(".astralnexus_python_*.py"))


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


def test_terminal_command_normalizes_provider_json_variants():
    assert _normalize_terminal_command(["python", "--version"]) == ["python", "--version"]
    assert _normalize_terminal_command('["python", "--version"]') == ["python", "--version"]
    assert _normalize_terminal_command(['["python"', '"--version"]']) == ["python", "--version"]


def test_research_and_browser_tools_are_registered():
    executor = ToolExecutor()
    tools = set(executor.list_tools())
    assert {"openalex_search", "crossref_search", "arxiv_search", "github_repo_contents"} <= tools
    assert "browser_fetch" in tools


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
