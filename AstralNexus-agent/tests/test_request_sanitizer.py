from AstralNexus.runtime.request_sanitizer import prepare_request, sanitize_messages


def test_messages_remove_empty_and_normalize_tool_content():
    messages, tools, stats = prepare_request([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "question"},
        {"role": "tool", "content": None, "tool_call_id": "call-1"},
        {"role": "assistant", "content": None, "tool_calls": []},
    ])
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["content"] == "{}"
    assert stats["estimated_input_tokens"] > 0
    assert tools == []


def test_tool_schema_is_bounded_and_invalid_tools_are_dropped(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_TOOL_SCHEMA_CHAR_BUDGET", "1000")
    messages, tools, stats = prepare_request(
        [{"role": "user", "content": "use a tool"}],
        [
            {"type": "function", "function": {"name": "first", "description": "x" * 600, "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "second", "description": "x" * 600, "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "invalid"}},
        ],
    )
    # The wire request must obey the hard schema budget.  Relevance selection
    # happens before this guard; the sanitizer is the final enforcement point.
    assert len(tools) == 1
    assert stats["dropped_tools"]
    assert stats["tool_budget_exceeded"] is True


def test_empty_user_message_is_rejected():
    try:
        sanitize_messages([{"role": "system", "content": "only system"}])
    except ValueError as exc:
        assert "user message" in str(exc)
    else:
        raise AssertionError("empty user request was accepted")
