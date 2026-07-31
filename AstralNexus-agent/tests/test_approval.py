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


def test_tui_provider_blocks_until_choice():
    from AstralNexus.tui_gateway.entry import Gateway

    gateway = Gateway()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            gateway.approval_provider(
                "terminal_exec", {"command": "echo ok"}, "test", "test"
            )
        ),
    )
    worker.start()
    for _ in range(100):
        with gateway._approval_lock:
            if gateway._approval_choice is None:
                worker.join(0.01)
                continue
            if gateway._approval_choice:
                break
    gateway.respond_approval("session")
    worker.join(1)
    assert result == ["session"]
