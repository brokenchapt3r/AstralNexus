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
