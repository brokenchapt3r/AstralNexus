"""doc 18 修复G：后台服务生命周期边界测试。

attempt3/4/5 实证链：worker 经 terminal_exec(background=true) 启动的服务
在节点会话结束后泄漏 → 占住端口 → 后续代 worker 启动失败 → 重复同一动作
→ runtime_no_progress 空转 → 恢复链代际全灭；attempt4 遗留进程甚至毒化
attempt5 环境（PID 46552 占 8080）。修复G 三层：①注册条目打 node_id 标签
（ContextVar 会话作用域）；②节点会话边界（波次 future finally）按标清理；
③run 级硬边界（收尾 finally ×2 + 恢复接管启动前）清全部注册服务。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from AstralNexus.tools import builtin_tools
from AstralNexus.tools.builtin_tools import (
    _CURRENT_NODE_CONTEXT,
    _process_registry_path,
    set_tool_node_context,
    stop_all_registered_services,
    stop_services_for_node,
)

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def _spawn_registry_entry(root: Path, node_id: str) -> tuple[subprocess.Popen, dict]:
    """启动真实休眠进程并按注册表格式落盘（含修复G的 node_id 标签）。"""
    proc = subprocess.Popen(SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    entry = {
        "pid": proc.pid,
        "command": SLEEPER,
        "cwd": str(root),
        "log_path": str(root / "fake.log"),
        "started_at": time.time(),
        "node_id": node_id,
    }
    registry_path = _process_registry_path(root)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else []
    entries.append(entry)
    registry_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return proc, entry


def _registry(root: Path) -> list:
    registry_path = _process_registry_path(root)
    if not registry_path.is_file():
        return []
    return json.loads(registry_path.read_text(encoding="utf-8"))


@pytest.fixture()
def two_node_services(tmp_path):
    proc_a, _ = _spawn_registry_entry(tmp_path, "N-A")
    proc_b, _ = _spawn_registry_entry(tmp_path, "N-B")
    yield tmp_path, proc_a, proc_b
    for proc in (proc_a, proc_b):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_services_for_node_kills_only_tagged(two_node_services):
    """节点级清理只杀本节点标签的服务，他节点服务与注册条目保留。"""
    root, proc_a, proc_b = two_node_services
    stopped = stop_services_for_node("N-A", root)
    assert stopped == 1
    deadline = time.time() + 10
    while proc_a.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    assert proc_a.poll() is not None, "N-A service should be killed"
    assert proc_b.poll() is None, "N-B service must stay alive"
    remaining = _registry(root)
    assert [e["node_id"] for e in remaining] == ["N-B"]


def test_stop_all_registered_services_is_run_boundary(two_node_services):
    """run 级硬边界：全部注册服务停止、注册表清空。"""
    root, proc_a, proc_b = two_node_services
    stopped = stop_all_registered_services(root)
    assert stopped == 2
    deadline = time.time() + 10
    while (proc_a.poll() is None or proc_b.poll() is None) and time.time() < deadline:
        time.sleep(0.2)
    assert proc_a.poll() is not None and proc_b.poll() is not None
    assert _registry(root) == []


def test_empty_node_id_and_missing_registry_are_noops(tmp_path):
    assert stop_services_for_node("", tmp_path) == 0
    assert stop_all_registered_services(tmp_path / "nonexistent") == 0


def test_nested_execution_cwd_registers_at_run_root(tmp_path, monkeypatch):
    import psutil  # type: ignore[import-untyped]  # Runtime dependency has no bundled typing stubs.
    from AstralNexus.tools.context import tool_workspace
    nested = tmp_path / "outputs" / "app" / "backend"
    nested.mkdir(parents=True)
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    with tool_workspace(tmp_path):
        result = json.loads(builtin_tools._run_process(
            {"cwd": str(nested), "background": True}, SLEEPER, "terminal_exec"))
    assert result["status"] == "started", result
    pid = result["pid"]
    try:
        assert _registry(tmp_path)[0]["pid"] == pid
        assert _registry(tmp_path)[0]["cwd"] == str(nested)
        assert not _process_registry_path(nested).exists()
        assert stop_all_registered_services(tmp_path) == 1
        if psutil.pid_exists(pid):
            psutil.Process(pid).wait(timeout=10)
        assert not psutil.pid_exists(pid)
    finally:
        if psutil.pid_exists(pid):
            psutil.Process(pid).kill()


def test_contextvar_tags_session_scope():
    """ContextVar 会话作用域：set 后本线程读到节点身份。"""
    set_tool_node_context("N-77")
    assert _CURRENT_NODE_CONTEXT.get() == "N-77"
    set_tool_node_context("")
    assert _CURRENT_NODE_CONTEXT.get() == ""


def test_orchestrator_stop_node_services_traces(tmp_path, monkeypatch):
    """编排器卫生边界：清理留痕；清理异常不致命（stop_failed 痕迹）。"""
    from AstralNexus.tests.test_attempt3_audit_fixes import _seam_graph

    orch, _patch, _resolved = _seam_graph(tmp_path)
    calls = []
    monkeypatch.setattr(builtin_tools, "stop_services_for_node", lambda node_id, root: calls.append((node_id, root)) or 3)
    traces = []
    monkeypatch.setattr(
        orch.trace_recorder, "record",
        lambda kind, payload=None, **kw: traces.append((kind, payload or {})),
    )
    orch._stop_node_services("N-002")
    assert calls and calls[0][0] == "N-002"
    assert any(
        k == "orchestrator.node_services.stopped" and v["stopped"] == 3
        for k, v in traces
    )

    def _boom(node_id, root):
        raise OSError("registry exploded")

    monkeypatch.setattr(builtin_tools, "stop_services_for_node", _boom)
    orch._stop_node_services("N-002")  # 不得抛出
    assert any(k == "orchestrator.node_services.stop_failed" for k, _ in traces)
