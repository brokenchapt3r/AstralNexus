"""Stream-integrated tool auto-execution and semantic recovery.

Implements #10 gaps: auto tool execution within stream, context cleanup events,
cross-process event push, semantic checkpoint/recovery.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class StreamEventType(str, Enum):
    DELTA = "llm.stream.delta"
    TOOL_CALL_DELTA = "llm.stream.tool_call_delta"
    TOOL_CALL_COMPLETE = "llm.stream.tool_call_complete"
    TOOL_EXECUTION_START = "llm.stream.tool_execution_start"
    TOOL_EXECUTION_DONE = "llm.stream.tool_execution_done"
    TOOL_EXECUTION_ERROR = "llm.stream.tool_execution_error"
    CONTEXT_CLEANUP = "llm.stream.context_cleanup"
    SEMANTIC_CHECKPOINT = "llm.stream.semantic_checkpoint"
    RECONNECTED = "llm.stream.reconnected"
    FAILED = "llm.stream.failed"
    INTERRUPTED = "llm.stream.interrupted"


class ToolExecutionPolicy(str, Enum):
    AUTO = "auto"           # Execute all tools automatically
    CONFIRM_RISKY = "confirm_risky"  # Auto-execute safe tools, confirm risky
    MANUAL = "manual"       # Never auto-execute


@dataclass
class StreamToolCall:
    """A tool call detected during streaming."""
    call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    arguments_raw: str = ""
    complete: bool = False
    executed: bool = False
    execution_result: Optional[Dict[str, Any]] = None
    execution_error: Optional[str] = None
    execution_time: float = 0.0


@dataclass
class SemanticCheckpoint:
    """Recovery point for interrupted streams."""
    timestamp: float
    messages_so_far: int
    text_so_far: str
    pending_tool_calls: List[Dict[str, Any]]
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "messages_so_far": self.messages_so_far,
            "text_so_far": self.text_so_far[:500],  # cap for storage
            "pending_tool_calls": self.pending_tool_calls,
            "reason": self.reason,
        }


class StreamToolExecutor:
    """Auto-execute tool calls detected during LLM streaming.

    Supports tool execution policies, context cleanup events,
    cross-process event broadcasting, and semantic recovery.
    """

    def __init__(
        self,
        tool_registry: Optional[Dict[str, Callable]] = None,
        *,
        policy: ToolExecutionPolicy = ToolExecutionPolicy.CONFIRM_RISKY,
        risky_tools: Optional[set] = None,
        max_concurrent: int = 3,
    ):
        self._registry = tool_registry or {}
        self.policy = policy
        self._risky_tools = risky_tools or {"terminal", "write_file", "delete"}
        self._max_concurrent = max_concurrent
        self._pending_calls: List[StreamToolCall] = []
        self._executed_calls: List[StreamToolCall] = []
        self._event_listeners: List[Callable] = []
        self._checkpoints: List[SemanticCheckpoint] = []
        self._context_cleanup_events: List[Dict[str, Any]] = []

    def register_tool(self, name: str, handler: Callable) -> None:
        """Register a tool handler for auto-execution."""
        self._registry[name] = handler

    def add_event_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Add a cross-process event listener."""
        self._event_listeners.append(listener)

    def on_stream_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process a stream event; auto-execute tool calls when complete."""
        event_type = event.get("event_type", "")

        if event_type == StreamEventType.TOOL_CALL_COMPLETE.value:
            return self._handle_tool_call_complete(event)

        elif event_type == StreamEventType.INTERRUPTED.value:
            return self._handle_interruption(event)

        return None

    def _handle_tool_call_complete(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a completed tool call according to policy."""
        call_id = event.get("call_id", "")
        tool_name = event.get("tool_name", "")
        arguments = event.get("arguments", {})

        tool_call = StreamToolCall(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            arguments_raw=json.dumps(arguments, ensure_ascii=False),
            complete=True,
        )

        # Check policy
        should_execute = self._should_auto_execute(tool_name)
        if not should_execute:
            tool_call.executed = False
            tool_call.execution_error = "pending_confirmation"
            self._pending_calls.append(tool_call)
            self._emit_event({
                "event_type": StreamEventType.TOOL_EXECUTION_START.value,
                "call_id": call_id,
                "tool_name": tool_name,
                "status": "pending_confirmation",
            })
            return None

        return self._execute_tool_call(tool_call)

    def _should_auto_execute(self, tool_name: str) -> bool:
        if self.policy == ToolExecutionPolicy.AUTO:
            return True
        if self.policy == ToolExecutionPolicy.MANUAL:
            return False
        # CONFIRM_RISKY: auto-execute unless tool is risky
        return tool_name not in self._risky_tools

    def _execute_tool_call(self, tool_call: StreamToolCall) -> Dict[str, Any]:
        """Execute a tool call and record the result."""
        start = time.time()
        self._emit_event({
            "event_type": StreamEventType.TOOL_EXECUTION_START.value,
            "call_id": tool_call.call_id,
            "tool_name": tool_call.tool_name,
        })

        handler = self._registry.get(tool_call.tool_name)
        if handler is None:
            tool_call.execution_error = f"unknown_tool: {tool_call.tool_name}"
            self._emit_event({
                "event_type": StreamEventType.TOOL_EXECUTION_ERROR.value,
                "call_id": tool_call.call_id,
                "error": tool_call.execution_error,
            })
            self._executed_calls.append(tool_call)
            return tool_call.execution_error

        try:
            result = handler(tool_call.arguments)
            tool_call.execution_result = result if isinstance(result, dict) else {"output": str(result)}
            tool_call.executed = True
        except Exception as exc:
            tool_call.execution_error = str(exc)
            self._emit_event({
                "event_type": StreamEventType.TOOL_EXECUTION_ERROR.value,
                "call_id": tool_call.call_id,
                "error": str(exc),
            })

        tool_call.execution_time = time.time() - start
        self._executed_calls.append(tool_call)
        self._emit_event({
            "event_type": StreamEventType.TOOL_EXECUTION_DONE.value,
            "call_id": tool_call.call_id,
            "tool_name": tool_call.tool_name,
            "execution_time": tool_call.execution_time,
            "success": tool_call.execution_error is None,
        })
        return tool_call.execution_result

    def confirm_pending(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Confirm and execute a pending tool call."""
        for tc in self._pending_calls:
            if tc.call_id == call_id and not tc.executed:
                self._pending_calls.remove(tc)
                return self._execute_tool_call(tc)
        return None

    def create_checkpoint(
        self,
        messages_count: int,
        current_text: str,
        reason: str = "",
    ) -> SemanticCheckpoint:
        """Create a semantic checkpoint for recovery."""
        cp = SemanticCheckpoint(
            timestamp=time.time(),
            messages_so_far=messages_count,
            text_so_far=current_text,
            pending_tool_calls=[
                {"call_id": tc.call_id, "tool_name": tc.tool_name, "arguments": tc.arguments}
                for tc in self._pending_calls if not tc.executed
            ],
            reason=reason,
        )
        self._checkpoints.append(cp)
        self._emit_event({
            "event_type": StreamEventType.SEMANTIC_CHECKPOINT.value,
            "checkpoint": cp.to_dict(),
        })
        return cp

    def record_context_cleanup(self, reason: str, details: Optional[Dict] = None) -> None:
        """Record a context cleanup event during streaming."""
        event = {
            "event_type": StreamEventType.CONTEXT_CLEANUP.value,
            "timestamp": time.time(),
            "reason": reason,
            "details": details or {},
        }
        self._context_cleanup_events.append(event)
        self._emit_event(event)

    def get_recovery_state(self) -> Dict[str, Any]:
        """Get state for semantic recovery after interruption."""
        last_cp = self._checkpoints[-1] if self._checkpoints else None
        return {
            "has_checkpoint": last_cp is not None,
            "checkpoint": last_cp.to_dict() if last_cp else None,
            "executed_tool_calls": len(self._executed_calls),
            "pending_tool_calls": len(self._pending_calls),
            "context_cleanups": len(self._context_cleanup_events),
        }

    @property
    def executed_calls(self) -> List[StreamToolCall]:
        return list(self._executed_calls)

    @property
    def pending_calls(self) -> List[StreamToolCall]:
        return list(self._pending_calls)

    @property
    def checkpoints(self) -> List[SemanticCheckpoint]:
        return list(self._checkpoints)

    def _handle_interruption(self, event: Dict[str, Any]) -> None:
        """Handle stream interruption: create checkpoint and emit event."""
        reason = event.get("reason", "unknown")
        # Auto-checkpoint any pending tool calls
        pending = [
            {"call_id": tc.call_id, "tool_name": tc.tool_name, "arguments": tc.arguments}
            for tc in self._pending_calls if not tc.executed
        ]
        cp = SemanticCheckpoint(
            timestamp=time.time(),
            messages_so_far=0,
            text_so_far="",
            pending_tool_calls=pending,
            reason=f"interrupted:{reason}",
        )
        self._checkpoints.append(cp)
        self._emit_event({
            "event_type": StreamEventType.INTERRUPTED.value,
            "reason": reason,
            "pending_tool_calls": pending,
        })

    def _emit_event(self, event: Dict[str, Any]) -> None:
        """Broadcast event to all registered listeners."""
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception:
                pass  # Don't let listener errors break the stream
