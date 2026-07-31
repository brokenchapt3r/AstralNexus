"""Tests for stream_tool_executor - stream-integrated tool auto-execution."""
import time
import pytest
from AstralNexus.runtime.stream_tool_executor import (
    StreamToolExecutor,
    StreamEventType,
    ToolExecutionPolicy,
    StreamToolCall,
)


def _make_tool_complete_event(call_id: str, tool_name: str, arguments: dict) -> dict:
    return {
        "event_type": StreamEventType.TOOL_CALL_COMPLETE.value,
        "call_id": call_id,
        "tool_name": tool_name,
        "arguments": arguments,
    }


class TestToolExecutionPolicy:
    def test_auto_policy_executes_all(self):
        def mock_handler(args):
            return {"result": "ok"}

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.AUTO)
        executor.register_tool("test_tool", mock_handler)

        event = _make_tool_complete_event("c1", "test_tool", {"x": 1})
        result = executor.on_stream_event(event)

        assert result == {"result": "ok"}
        assert len(executor.executed_calls) == 1
        assert executor.executed_calls[0].executed is True

    def test_manual_policy_never_executes(self):
        def mock_handler(args):
            return {"result": "ok"}

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.MANUAL)
        executor.register_tool("test_tool", mock_handler)

        event = _make_tool_complete_event("c1", "test_tool", {"x": 1})
        result = executor.on_stream_event(event)

        assert result is None
        assert len(executor.pending_calls) == 1
        assert executor.pending_calls[0].execution_error == "pending_confirmation"

    def test_confirm_risky_policy_blocks_risky_tools(self):
        def mock_handler(args):
            return {"result": "ok"}

        executor = StreamToolExecutor(
            policy=ToolExecutionPolicy.CONFIRM_RISKY,
            risky_tools={"terminal"},
        )
        executor.register_tool("terminal", mock_handler)
        executor.register_tool("read_file", mock_handler)

        # Terminal is risky, should be pending
        event1 = _make_tool_complete_event("c1", "terminal", {"cmd": "ls"})
        result1 = executor.on_stream_event(event1)
        assert result1 is None
        assert len(executor.pending_calls) == 1

        # read_file is safe, should auto-execute
        event2 = _make_tool_complete_event("c2", "read_file", {"path": "/tmp/test"})
        result2 = executor.on_stream_event(event2)
        assert result2 == {"result": "ok"}
        assert len(executor.executed_calls) == 1

    def test_confirm_pending_executes_tool(self):
        def mock_handler(args):
            return {"result": "confirmed"}

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.MANUAL)
        executor.register_tool("test_tool", mock_handler)

        event = _make_tool_complete_event("c1", "test_tool", {"x": 1})
        executor.on_stream_event(event)

        assert len(executor.pending_calls) == 1
        result = executor.confirm_pending("c1")

        assert result == {"result": "confirmed"}
        assert len(executor.pending_calls) == 0
        assert len(executor.executed_calls) == 1


class TestToolExecution:
    def test_unknown_tool_records_error(self):
        executor = StreamToolExecutor(policy=ToolExecutionPolicy.AUTO)

        event = _make_tool_complete_event("c1", "nonexistent_tool", {})
        result = executor.on_stream_event(event)

        assert result == "unknown_tool: nonexistent_tool"
        assert len(executor.executed_calls) == 1
        assert "unknown_tool" in executor.executed_calls[0].execution_error

    def test_tool_exception_is_captured(self):
        def failing_handler(args):
            raise ValueError("intentional error")

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.AUTO)
        executor.register_tool("failing_tool", failing_handler)

        event = _make_tool_complete_event("c1", "failing_tool", {})
        executor.on_stream_event(event)

        assert len(executor.executed_calls) == 1
        assert "intentional error" in executor.executed_calls[0].execution_error

    def test_execution_time_is_recorded(self):
        def slow_handler(args):
            time.sleep(0.01)
            return {"ok": True}

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.AUTO)
        executor.register_tool("slow_tool", slow_handler)

        event = _make_tool_complete_event("c1", "slow_tool", {})
        executor.on_stream_event(event)

        assert executor.executed_calls[0].execution_time >= 0.01


class TestEventListeners:
    def test_listeners_receive_events(self):
        received = []

        def listener(event):
            received.append(event)

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.AUTO)
        executor.add_event_listener(listener)

        event = _make_tool_complete_event("c1", "test_tool", {})
        executor.on_stream_event(event)

        # Should receive: TOOL_EXECUTION_START + TOOL_EXECUTION_DONE + unknown_tool error
        assert len(received) >= 2
        event_types = [e["event_type"] for e in received]
        assert StreamEventType.TOOL_EXECUTION_START.value in event_types


class TestSemanticCheckpoints:
    def test_create_checkpoint_records_state(self):
        executor = StreamToolExecutor()

        cp = executor.create_checkpoint(
            messages_count=10,
            current_text="Hello world",
            reason="before_tool_execution",
        )

        assert cp.messages_so_far == 10
        assert cp.text_so_far == "Hello world"
        assert cp.reason == "before_tool_execution"
        assert len(executor.checkpoints) == 1

    def test_checkpoint_includes_pending_tools(self):
        def mock_handler(args):
            return {}

        executor = StreamToolExecutor(policy=ToolExecutionPolicy.MANUAL)
        executor.register_tool("test_tool", mock_handler)

        # Create a pending tool call
        event = _make_tool_complete_event("c1", "test_tool", {"x": 1})
        executor.on_stream_event(event)

        cp = executor.create_checkpoint(5, "text", "test")

        assert len(cp.pending_tool_calls) == 1
        assert cp.pending_tool_calls[0]["call_id"] == "c1"

    def test_get_recovery_state_with_checkpoint(self):
        executor = StreamToolExecutor()
        executor.create_checkpoint(10, "text", "test")

        state = executor.get_recovery_state()

        assert state["has_checkpoint"] is True
        assert state["checkpoint"]["messages_so_far"] == 10
        assert state["executed_tool_calls"] == 0
        assert state["pending_tool_calls"] == 0


class TestContextCleanup:
    def test_record_context_cleanup(self):
        received = []

        def listener(event):
            received.append(event)

        executor = StreamToolExecutor()
        executor.add_event_listener(listener)

        executor.record_context_cleanup(
            reason="token_limit_exceeded",
            details={"tokens_before": 1000, "tokens_after": 500},
        )

        event = received[0]
        assert event["event_type"] == StreamEventType.CONTEXT_CLEANUP.value
        assert event["reason"] == "token_limit_exceeded"
        assert event["details"]["tokens_before"] == 1000


class TestInterruptionHandling:
    def test_interruption_event_creates_checkpoint(self):
        executor = StreamToolExecutor()

        interrupt_event = {
            "event_type": StreamEventType.INTERRUPTED.value,
            "reason": "user_cancel",
        }

        result = executor.on_stream_event(interrupt_event)

        # Should handle interruption gracefully
        assert result is None or isinstance(result, dict)
