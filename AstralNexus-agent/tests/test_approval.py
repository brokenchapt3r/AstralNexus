import threading

from AstralNexus.tools.approval import (
    clear_approval_provider,
    is_tool_approved,
    reset_approval_state,
    set_approval_provider,
)
from AstralNexus.tools.registry import ToolRegistry


def test_interactive_once_grant_reaches_handler_and_is_consumed():
    registry = ToolRegistry()
    calls = []

    def handler(args):
        calls.append(is_tool_approved("approval_test"))
        return "ok"

    registry.register(
        name="approval_test",
        toolset="test",
        schema={"description": "test", "parameters": {"type": "object"}},
        handler=handler,
        requires_approval=True,
    )
    set_approval_provider(lambda *args: "once")
    try:
        assert registry.dispatch("approval_test", {}) == "ok"
        assert calls == [True]
        # A second dispatch must ask again; the one-shot grant was consumed.
        set_approval_provider(lambda *args: "deny")
        denied = registry.dispatch("approval_test", {})
        assert '"status": "denied"' in denied
    finally:
        clear_approval_provider()
        reset_approval_state()


def test_tui_provider_blocks_until_choice(monkeypatch, tmp_path):
    from AstralNexus.tui_gateway import entry

    gateway = entry.Gateway(home=tmp_path)
    session = gateway.create_session()
    requested = threading.Event()
    payloads = []
    def emit(kind, payload=None, **kwargs):
        if kind == "approval.request":
            payloads.append(payload)
            requested.set()
    monkeypatch.setattr(entry, "_event", emit)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            gateway.approval_provider(
                "terminal_exec", {"command": "echo ok"}, "test", "test"
            )
        ),
    )
    worker.start()
    assert requested.wait(2)
    assert worker.is_alive() and result == []
    assert gateway.respond_approval("session", request_id=payloads[0]["request_id"], session_id=session.session_id)
    worker.join(1)
    assert result == ["session"]
