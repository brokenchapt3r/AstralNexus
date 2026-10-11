"""Deterministic inversions of the system audit's executable boundaries."""
from __future__ import annotations
import ast
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.llm_loop_budget import LLMLoopBudget, LLMLoopBudgetExceeded
from AstralNexus.tools.executor import ToolExecutor


@pytest.mark.parametrize("cache", ["missing", "truncated", "wrong_identity"])
def test_cli_resume_uses_database_identity_before_cache(tmp_path, monkeypatch, cache):
    from AstralNexus.cli import main
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    run = tmp_path / "directory-identity"
    coordinator = RunCoordinator(run, run_id=run.name)
    lease = coordinator.acquire("previous-owner")
    assert lease is not None
    with Orchestrator(run, run_id="graph-identity", fence_token_provider=lambda: coordinator.fence_token(lease),
            checkpoint_sink=lambda payload: coordinator.record_checkpoint(lease, payload)) as runtime:
        runtime.task_graph.add_node(TaskNode(node_id="worker", goal="task", role="coder", status="ready"))
        assert runtime._persist_graph_checkpoint(stage="prepare")
    coordinator.finish(lease, "paused", stop_code="user_pause")
    state = run / "runtime_state.json"
    if cache == "missing":
        state.unlink()
    else:
        state.write_text("{" if cache == "truncated" else '{"run_id":"wrong"}', encoding="utf-8")
    restored = []
    original = Orchestrator.restore_checkpoint
    def stop_after_restore(runtime):
        assert runtime.run_id == "graph-identity"
        assert original(runtime)
        restored.append(runtime.task_graph.nodes["worker"].goal)
        raise RuntimeError("test stops before any real model request")
    monkeypatch.setattr(Orchestrator, "restore_checkpoint", stop_after_restore)
    args = SimpleNamespace(run_dir=str(run), max_parallel=None, user_resume=True, model=None, provider=None, task=None)
    assert main._resume_task(args, tmp_path) == 1
    assert restored == ["task"]


def test_run_creation_failure_cannot_return_partially_initialized_orchestrator(tmp_path, monkeypatch):
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
    def unavailable(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(SQLiteTaskGraphStore, "create_run", unavailable)
    with pytest.raises(RuntimeError, match="persistence initialization failed") as error:
        Orchestrator(tmp_path, run_id="failed-init")
    assert isinstance(error.value.__cause__, OSError)


def test_run_shutdown_leaves_another_tool_pool_and_queued_work_alive(tmp_path):
    first = ToolExecutor(workspace_root=tmp_path / "A")
    second = ToolExecutor(workspace_root=tmp_path / "B")
    a, b = first._get_run_pool(), second._get_run_pool()
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(3)
        return "B completed"
    pending = b.submit(work)
    assert entered.wait(2)
    try:
        with Orchestrator(tmp_path / "A", run_id="A"):
            pass
        with pytest.raises(RuntimeError, match="shutdown"):
            a.submit(lambda: None)
        assert b.submit(lambda: 7).result(timeout=2) == 7
    finally:
        release.set()
        ToolExecutor.shutdown_run_pool(tmp_path / "B")
    assert pending.result() == "B completed"


@pytest.mark.parametrize("choice", ["session", "always", "once"])
def test_approval_cannot_leak_between_scopes(choice, monkeypatch):
    from AstralNexus.tools import approval
    monkeypatch.delenv("ASTRALNEXUS_TOOL_APPROVAL", raising=False)
    approval.reset_approval_state()
    approval.set_approval_provider(lambda *args: choice, scope="A")
    approval.set_approval_provider(lambda *args: "deny", scope="B")
    try:
        with approval.approval_scope("A"):
            assert approval.request_approval("test_tool", {}).status == "approved"
            assert approval.is_tool_approved("test_tool")
        with approval.approval_scope("B"):
            assert not approval.is_tool_approved("test_tool")
            assert approval.request_approval("test_tool", {}).status == "denied"
    finally:
        approval.reset_approval_state()


def test_executor_pins_provider_and_grants_on_parallel_worker_thread(tmp_path, monkeypatch):
    from AstralNexus.tools import approval
    approval.reset_approval_state()
    approval.set_approval_provider(lambda *args: "session")
    first = ToolExecutor(workspace_root=tmp_path / "A")
    approval.set_approval_provider(lambda *args: "deny")
    second = ToolExecutor(workspace_root=tmp_path / "B")
    def request(_executor, *args):
        return {"status": approval.request_approval("test_tool", {}).status}
    monkeypatch.setattr(ToolExecutor, "_execute_tool_inner", request)
    try:
        assert first._get_run_pool().submit(first.execute_tool, "test_tool", {}).result(timeout=2)["status"] == "approved"
        assert second._get_run_pool().submit(second.execute_tool, "test_tool", {}).result(timeout=2)["status"] == "denied"
    finally:
        approval.clear_approval_provider()
        approval.reset_approval_state()
        ToolExecutor.shutdown_run_pool(tmp_path / "A")
        ToolExecutor.shutdown_run_pool(tmp_path / "B")


def test_graph_api_and_checkpoint_use_the_same_lock(tmp_path):
    with Orchestrator(tmp_path, run_id="lock") as runtime:
        assert runtime._execution_lock is runtime.task_graph._mutation_lock


        entered = threading.Event()
        def mutate():
            entered.set()
            runtime.task_graph.add_node(TaskNode(node_id="new", goal="task", role="coder"))
        with runtime._execution_lock:
            thread = threading.Thread(target=mutate)
            thread.start()
            assert entered.wait(2)
            assert "new" not in runtime.task_graph.nodes
        thread.join(2)
        assert not thread.is_alive() and "new" in runtime.task_graph.nodes
        runtime._save_checkpoint()
        assert runtime.restore_checkpoint()
        assert runtime._execution_lock is runtime.task_graph._mutation_lock


def test_explicit_session_identity_selects_its_scoped_provider_in_same_workspace(tmp_path, monkeypatch):
    from AstralNexus.tools import approval
    approval.reset_approval_state()
    approval.set_approval_provider(lambda *args: "session", scope="session-A")
    approval.set_approval_provider(lambda *args: "deny", scope="session-B")
    first = ToolExecutor(workspace_root=tmp_path, approval_identity="session-A")
    with approval.approval_scope("session-A", provider=lambda *args: "always", bind_provider=True):
        second = ToolExecutor(workspace_root=tmp_path, approval_identity="session-B")
    monkeypatch.setattr(ToolExecutor, "_execute_tool_inner", lambda *args:
        {"status": approval.request_approval("test_tool", {}).status})
    try:
        assert first.execute_tool("test_tool", {})["status"] == "approved"
        assert second.execute_tool("test_tool", {})["status"] == "denied"
    finally:
        approval.reset_approval_state()


@pytest.mark.parametrize("dimension", ["requests", "tokens", "tool_rounds", "wall_clock"])
def test_shared_budget_reports_the_exhausted_dimension(dimension):
    budget = LLMLoopBudget(max_rounds=2, max_requests=3, max_tokens=10, deadline=float("inf"))
    if dimension == "requests":
        budget.requests = 3
    elif dimension == "tokens":
        budget.tokens = 10
    elif dimension == "wall_clock":
        budget.deadline = 0
    with pytest.raises(LLMLoopBudgetExceeded, match=dimension):
        budget.admit_request(3 if dimension == "tool_rounds" else 0)


def test_legacy_zero_limits_mean_finite_defaults_and_usage_is_not_double_counted(monkeypatch):
    for key in ("ASTRALNEXUS_MAX_TOOL_ROUNDS", "ASTRALNEXUS_NODE_TIMEOUT", "ASTRALNEXUS_MAX_LLM_REQUESTS"):
        monkeypatch.setenv(key, "0")
    budget = LLMLoopBudget.from_environment(None)
    assert budget.max_rounds == 60 and budget.max_requests > 0
    budget.admit_request(0)
    budget.consume_usage({"total_tokens": 4})
    budget.consume_usage({"total_tokens": 4})
    assert budget.tokens == 4
    budget.admit_request(1)
    budget.consume_usage({"total_tokens": 4})
    assert budget.tokens == 8


@pytest.mark.parametrize("streaming", [False, True])
def test_diverse_tool_calls_cannot_evade_session_request_limit(tmp_path, monkeypatch, streaming):
    from AstralNexus.runtime import llm_client
    from AstralNexus.runtime.provider_request_gate import ProviderRequestGate
    monkeypatch.setattr(llm_client, "provider_request_gate", ProviderRequestGate())
    monkeypatch.setenv("ASTRALNEXUS_MAX_TOOL_ROUNDS", "0")
    monkeypatch.setenv("ASTRALNEXUS_MAX_LLM_REQUESTS", "3")
    monkeypatch.setenv("ASTRALNEXUS_TOOL_PROGRESS_GUARD", "0")
    client = llm_client.LLMClient(model="offline", api_key="test", interactive=False)
    client.max_retries = 0
    monkeypatch.setattr(client, "_maybe_compress_conversation", lambda messages, tools: messages)
    requests = []
    def calls():
        requests.append(1)
        return {"id": f"unique-{len(requests)}", "type": "function", "function": {
            "name": "python_exec", "arguments": json.dumps({"code": f"print({len(requests)})"})}}
    def response(*args, **kwargs):
        return io.BytesIO(json.dumps({"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [calls()]}}]}).encode())
    def stream(*args, **kwargs):
        call = calls()
        yield {"choices": [{"delta": {"tool_calls": [{**call, "index": 0}]}, "finish_reason": "tool_calls"}]}
    monkeypatch.setattr(llm_client, "llm_urlopen", response)
    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr(ToolExecutor, "execute_tool_calls", lambda *args: [{"status": "success", "output": "read"}])
    tools = [{"type": "function", "function": {"name": "python_exec", "parameters": {"type": "object"}}}]
    with pytest.raises(llm_client.LLMClientError, match="budget exhausted: requests"):
        (client.stream_chat if streaming else client.chat)([{"role": "user", "content": "continue"}], tools=tools, workspace_root=tmp_path)
    assert len(requests) == 3


def test_install_dependencies_and_requirements_match():
    root = Path(__file__).resolve().parents[1]
    syntax = ast.parse((root / "setup.py").read_text(encoding="utf-8"))
    setup = next(node for node in ast.walk(syntax) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setup")
    declared = next(ast.literal_eval(item.value) for item in setup.keywords if item.arg == "install_requires")
    requirements = [line.strip() for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
    assert set(declared) == set(requirements)
    assert any(item.startswith("ruamel.yaml") for item in declared)


def test_wheel_resources_explicitly_include_dashboard_and_skill_assets(monkeypatch):
    import runpy
    import setuptools
    root = Path(__file__).resolve().parents[1]
    captured = {}
    monkeypatch.chdir(root)
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: captured.update(kwargs))
    runpy.run_path(str(root / "setup.py"))
    assets = set(captured["package_data"]["AstralNexus"])
    assert "dashboard/workbench.html" in assets
    assert any(path.endswith("/SKILL.md") for path in assets)
    assert "skills/vendor/microsoft/fastapi-router-py/assets/template.py" in assets
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in assets)
