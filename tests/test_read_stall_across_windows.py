# -*- coding: utf-8 -*-
"""Fix AT —— 读取停滞预算必须按"文件内容"计，而不是按"这一次调用"计。

`ToolCallGuard` 的 read-stall 预算写得很清楚，就是为这种活锁准备的：
"a worker that keeps re-reading the same unchanged files without ever writing
or executing is in a livelock"。但它把 streak 记在 **fingerprint** 上，
而 fingerprint 里含调用参数。

Fix AP 给了 file_read 窗口定址（offset / start_line / end_line）之后，
同一个文件的每个窗口都是不同的 fingerprint，streak 被摊薄到几十个键上，
永远到不了阈值 8。

线上实证（r13b，PATCH-R017-FINAL-ACCEPTANCE-REPAIR）：
- gen 26：232 条回执，**自己写的 47.5KB run_acceptance.py 被读了 123 次**，
  零写入、零执行，停滞预算一次都没触发；
- gen 27：20 分钟 43 条回执，19 次 file_read 里 **17 次是同一个文件**，同样零执行。

修法只动"怎么计数"，不动阈值、不动halt语义：
按 (path, 内容 sha) 记一份预算，且**只统计已经返回过的窗口**——
往前翻页读新窗口是真进展，不计数；重读自己已经拿到的内容才计数。
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.runtime.tool_guardrails import ToolCallGuard


def _call(path: str, **kw):
    args = {"path": path, **kw}
    return {"function": {"name": "file_read", "arguments": json.dumps(args)}}


def _ok(offset: int = 0, returned: int = 100):
    return {"status": "success", "offset": offset, "returned_chars": returned}


def _cycle(guard, call, result):
    """真实调用序列：admit -> 执行 -> observe。只 observe 不 admit 会绕过 _read_keys。"""
    guard.admit(call)
    guard.observe(call, result)


@pytest.fixture()
def workspace(tmp_path):
    target = tmp_path / "driver.py"
    target.write_bytes(b"x" * 4000)
    return tmp_path, target


# ---------------------------------------------------------------- 1. 线上形状
def test_repeated_windowed_reads_of_one_unchanged_file_trip_the_budget(workspace):
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    # 先把一个窗口拿到手
    guard.observe(_call("driver.py", offset=0), _ok(0, 100))
    # 再重复重读同一个窗口——这正是线上那 123 次
    for _ in range(12):
        call = _call("driver.py", offset=0)
        guard.observe(call, _ok(0, 100))
    admitted, _ = guard.admit(_call("driver.py", offset=0))
    assert admitted is False


def test_the_old_keying_would_not_have_tripped(workspace):
    """反向对照：把每次读都换一个窗口，旧口径（按调用记）永远攒不满。"""
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for i in range(40):
        call = _call("driver.py", offset=i * 100)
        guard.observe(call, _ok(i * 100, 100))
    # 每个窗口都是新的 => 旧口径 streak 全是 1；新口径也不该拦（这是诚实翻页）
    assert all(v <= 1 for v in guard._read_streaks.values())
    admitted, _ = guard.admit(_call("driver.py", offset=99 * 100))
    assert admitted is True, "老老实实往前翻页不该被拦"


# ---------------------------------------------------------------- 2. 诚实翻页不受罚
def test_forward_paging_through_a_large_file_is_never_penalised(workspace):
    root, target = workspace
    target.write_bytes(b"y" * 2_000_000)
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    offset = 0
    for _ in range(60):                       # 远超阈值 8
        call = _call("driver.py", offset=offset, max_chars=32768)
        guard.observe(call, _ok(offset, 32768))
        offset += 32768
    admitted, _ = guard.admit(_call("driver.py", offset=offset, max_chars=32768))
    assert admitted is True


def test_reading_a_different_file_does_not_share_the_budget(workspace):
    root, _ = workspace
    (root / "other.py").write_bytes(b"z" * 500)
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py", offset=0), _ok(0, 100))
    admitted, _ = guard.admit(_call("other.py", offset=0))
    assert admitted is True


# ---------------------------------------------------------------- 3. 内容变了就是新世界
def test_a_changed_file_starts_a_fresh_budget(workspace):
    root, target = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py", offset=0), _ok(0, 100))
    assert guard.admit(_call("driver.py", offset=0))[0] is False
    target.write_bytes(b"w" * 4000)           # 文件变了
    assert guard.admit(_call("driver.py", offset=0))[0] is True


def test_real_progress_clears_the_stall_budget(workspace):
    """写入/执行是真进展，按既有契约清空停滞预算。"""
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py", offset=0), _ok(0, 100))
    assert guard.admit(_call("driver.py", offset=0))[0] is False
    guard.observe(
        {"function": {"name": "file_write", "arguments": json.dumps({"path": "out.txt", "content": "x"})}},
        {"status": "success"},
    )
    assert guard.admit(_call("driver.py", offset=0))[0] is True


def test_search_files_is_not_progress(workspace):
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py", offset=0), _ok(0, 100))
    guard.observe(
        {"function": {"name": "search_files", "arguments": json.dumps({"query": "x"})}},
        {"status": "success"},
    )
    assert guard.admit(_call("driver.py", offset=0))[0] is False


# ---------------------------------------------------------------- 4. 行号定址与退化输入
def test_line_addressed_rereads_also_accumulate(workspace):
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        call = _call("driver.py", start_line=10, end_line=40)
        guard.observe(call, {"status": "success", "offset": 90, "returned_chars": 300})
    assert guard.admit(_call("driver.py", start_line=10, end_line=40))[0] is False


def test_result_without_window_fields_falls_back_to_whole_file(workspace):
    """旧版 file_read 结果没有 offset/returned_chars，也必须能计数。"""
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py"), {"status": "success"})
    assert guard.admit(_call("driver.py"))[0] is False


def test_failed_reads_do_not_feed_the_stall_budget(workspace):
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py", offset=0), {"status": "error", "error": "boom"})
    assert guard._stall_streaks == {}


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd"])
def test_paths_outside_the_workspace_are_ignored(workspace, path):
    """工作区外不哈希、不计内容预算。用不同窗口，避免被"按调用记"的旧预算先拦。"""
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for i in range(20):
        guard.observe(_call(path, offset=i), _ok(i, 100))
    assert guard._stall_streaks == {}
    assert guard.admit(_call(path, offset=999))[0] is True


def test_no_workspace_root_disables_content_keying(tmp_path):
    guard = ToolCallGuard(read_streak_limit=8)
    for _ in range(20):
        guard.observe(_call("driver.py", offset=0), _ok(0, 100))
    assert guard._stall_streaks == {}


def test_missing_file_is_not_counted(workspace):
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for i in range(20):
        guard.observe(_call("nope.py", offset=i), _ok(i, 100))
    assert guard._stall_streaks == {}
    assert guard.admit(_call("nope.py", offset=999))[0] is True


# ---------------------------------------------------------------- 5. 既有语义未被放宽
def test_identical_call_budget_is_unchanged(workspace):
    """原本按调用记的预算仍然照常工作。"""
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    call = _call("driver.py")
    for _ in range(20):
        guard.observe(call, {"status": "success"})
    assert guard._read_streaks[guard._state_fingerprint(call)] == 20


def test_decision_reports_the_stall(workspace):
    root, _ = workspace
    guard = ToolCallGuard(workspace_root=root, read_streak_limit=8)
    for _ in range(20):
        _cycle(guard, _call("driver.py", offset=0), _ok(0, 100))
    decision = guard.decision(_call("driver.py", offset=0))
    assert decision["action"] == "halt"
    assert decision["content_stall_count"] > 8, decision
