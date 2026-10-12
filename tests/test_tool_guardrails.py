from AstralNexus.runtime.tool_guardrails import ToolCallGuard


def test_tool_guard_blocks_only_identical_repeated_calls():
    guard = ToolCallGuard(max_repeats=2)
    call = {"id": "1", "function": {"name": "search_files", "arguments": {"query": "a"}}}
    assert guard.admit(call)[0] is True
    assert guard.admit(call)[0] is True
    allowed, reason = guard.admit(call)
    assert allowed is False
    assert reason == "repeated_identical_tool_call"

    different = {"id": "2", "function": {"name": "search_files", "arguments": {"query": "b"}}}
    assert guard.admit(different)[0] is True


def test_tool_guard_escalates_repeated_failures_without_inventing_tool():
    guard = ToolCallGuard(max_repeats=4, failure_threshold=2)
    call = {"id": "1", "function": {"name": "web_search", "arguments": {"query": "x"}}}
    guard.admit(call)
    guard.observe(call, {"status": "error"})
    assert guard.decision(call)["action"] == "retry_or_continue"
    guard.admit(call)
    guard.observe(call, {"status": "failed"})
    decision = guard.decision(call)
    assert decision["action"] == "switch_tool_or_replan"
    assert decision["reason"] == "repeated_tool_failure"


def test_tool_guard_normalizes_json_arguments():
    guard = ToolCallGuard(failure_threshold=2)
    for args in ['{"a": 1, "b": 2}', '{"b":2,"a":1}']:
        call = {"function": {"name": "terminal_exec", "arguments": args}}
        guard.admit(call)
        guard.observe(call, {"status": "failed"})
    assert guard.decision(call)["action"] == "switch_tool_or_replan"
    assert guard.snapshot()["unique_calls"] == 1


def test_successful_rereads_do_not_consume_failure_budget(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("old", encoding="utf-8")
    guard = ToolCallGuard(max_repeats=1, workspace_root=tmp_path)
    call = {"function": {"name": "file_read", "arguments": '{"path":"main.py"}'}}
    for _ in range(8):
        assert guard.admit(call)[0]
        guard.observe(call, {"status": "success"})
        assert guard.decision(call)["action"] == "retry_or_continue"
    assert guard.snapshot()["unique_calls"] == 1


def test_failed_read_budget_tracks_content_version(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("old", encoding="utf-8")
    guard = ToolCallGuard(max_repeats=1, workspace_root=tmp_path)
    call = {"function": {"name": "file_read", "arguments": '{"path":"main.py"}'}}
    for _ in range(2):
        guard.admit(call)
        guard.observe(call, {"status": "error"})
    assert not guard.admit(call)[0]
    source.touch()
    assert not guard.admit(call)[0]
    source.write_text("new", encoding="utf-8")
    assert guard.admit(call)[0]
    assert guard.decision(call)["action"] == "retry_or_continue"
