"""Tests for core.exceptions module.

Validates the unified exception hierarchy and helper functions.
Implements 完善方案书 §4.2.2 错误处理和恢复机制.
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.core.exceptions import (
    AstralNexusError,
    ArtifactStoreError,
    ContextCompressionError,
    ContextOverflowError,
    ConversationLoopError,
    CoreError,
    ImpactAnalysisError,
    InvalidStateTransition,
    ModelClientError,
    ModelRateLimitError,
    NoAgentAvailable,
    NodeNotFoundError,
    RecoveryError,
    RecoveryPlanError,
    RouterError,
    RuntimeError_,
    SchemaValidationError,
    Severity,
    StorageError,
    ToolCallError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
    format_tool_error,
    wrap_exception,
)


class TestSeverity:
    """Test Severity enum."""

    def test_recoverable_value(self):
        assert Severity.RECOVERABLE.value == "recoverable"

    def test_degraded_value(self):
        assert Severity.DEGRADED.value == "degraded"

    def test_fatal_value(self):
        assert Severity.FATAL.value == "fatal"

    def test_is_string_enum(self):
        """Severity inherits from str for JSON serialization."""
        assert isinstance(Severity.RECOVERABLE, str)


class TestAstralNexusError:
    """Test the base exception class."""

    def test_default_severity_is_fatal(self):
        err = AstralNexusError("test")
        assert err.severity == Severity.FATAL

    def test_default_recovery_hint(self):
        err = AstralNexusError("test")
        assert err.recovery_hint == "manual intervention required"

    def test_custom_severity(self):
        err = AstralNexusError("test", severity=Severity.RECOVERABLE)
        assert err.severity == Severity.RECOVERABLE

    def test_custom_recovery_hint(self):
        err = AstralNexusError("test", recovery_hint="try again")
        assert err.recovery_hint == "try again"

    def test_details_captured(self):
        err = AstralNexusError("test", node_id="T-001", role="coder")
        assert err.details == {"node_id": "T-001", "role": "coder"}

    def test_message_preserved(self):
        err = AstralNexusError("something went wrong")
        assert str(err) == "something went wrong"

    def test_to_structured(self):
        err = AstralNexusError(
            "test error",
            severity=Severity.DEGRADED,
            recovery_hint="retry",
            node_id="T-001",
        )
        result = err.to_structured()
        assert result["error"] is True
        assert result["error_class"] == "AstralNexusError"
        assert result["message"] == "test error"
        assert result["severity"] == "degraded"
        assert result["recovery_hint"] == "retry"
        assert result["details"]["node_id"] == "T-001"

    def test_to_structured_is_json_serializable(self):
        err = AstralNexusError("test", severity=Severity.FATAL, node_id="T-002")
        result = err.to_structured()
        serialized = json.dumps(result)
        assert '"error": true' in serialized


class TestRuntimeErrors:
    """Test runtime layer exceptions."""

    def test_runtime_error_severity(self):
        err = RuntimeError_("test")
        assert err.severity == Severity.DEGRADED

    def test_model_client_error_inherits_runtime(self):
        err = ModelClientError("API failed")
        assert isinstance(err, RuntimeError_)
        assert "API key" in err.recovery_hint or "base_url" in err.recovery_hint

    def test_model_rate_limit_severity(self):
        err = ModelRateLimitError("rate limited")
        assert err.severity == Severity.RECOVERABLE
        assert "backoff" in err.recovery_hint

    def test_context_overflow_inherits_runtime(self):
        err = ContextOverflowError("too long")
        assert isinstance(err, RuntimeError_)
        assert "compress" in err.recovery_hint

    def test_context_compression_error(self):
        err = ContextCompressionError("compression failed")
        assert isinstance(err, RuntimeError_)
        assert "artifact" in err.recovery_hint


class TestCoreErrors:
    """Test core layer exceptions."""

    def test_core_error_severity(self):
        err = CoreError("test")
        assert err.severity == Severity.DEGRADED

    def test_task_graph_error_inherits_core(self):
        err = InvalidStateTransition("bad transition")
        assert isinstance(err, CoreError)
        assert "state_machine" in err.recovery_hint

    def test_invalid_state_transition(self):
        err = InvalidStateTransition("pending -> passed")
        assert isinstance(err, AstralNexusError)

    def test_node_not_found_severity(self):
        err = NodeNotFoundError("T-999")
        assert err.severity == Severity.RECOVERABLE
        assert "pruned" in err.recovery_hint

    def test_router_error_inherits_core(self):
        err = RouterError("no route")
        assert isinstance(err, CoreError)

    def test_no_agent_available_severity(self):
        err = NoAgentAvailable("no coder")
        assert err.severity == Severity.DEGRADED
        assert "GlobalGovernor" in err.recovery_hint

    def test_schema_validation_error(self):
        err = SchemaValidationError("bad schema")
        assert err.severity == Severity.RECOVERABLE
        assert "format-repair" in err.recovery_hint


class TestStorageErrors:
    """Test storage layer exceptions."""

    def test_storage_error_severity(self):
        err = StorageError("disk full")
        assert err.severity == Severity.RECOVERABLE
        assert "disk space" in err.recovery_hint

    def test_artifact_store_error_inherits_storage(self):
        err = ArtifactStoreError("write failed")
        assert isinstance(err, StorageError)


class TestToolErrors:
    """Test tool layer exceptions."""

    def test_tool_error_severity(self):
        err = ToolError("tool failed")
        assert err.severity == Severity.RECOVERABLE

    def test_tool_not_found(self):
        err = ToolNotFoundError("web_search")
        assert isinstance(err, ToolError)
        assert "toolsets" in err.recovery_hint

    def test_tool_execution_error(self):
        err = ToolExecutionError("handler raised")
        assert isinstance(err, ToolError)

    def test_tool_timeout_error(self):
        err = ToolTimeoutError("30s exceeded")
        assert isinstance(err, ToolError)
        assert "timeout" in err.recovery_hint


class TestConversationErrors:
    """Test conversation layer exceptions."""

    def test_conversation_loop_error(self):
        err = ConversationLoopError("dialogue failed")
        assert err.severity == Severity.DEGRADED

    def test_tool_call_error(self):
        err = ToolCallError("bad tool call")
        assert err.severity == Severity.RECOVERABLE


class TestRecoveryErrors:
    """Test recovery layer exceptions."""

    def test_recovery_error_severity(self):
        err = RecoveryError("recovery failed")
        assert err.severity == Severity.FATAL
        assert "human operator" in err.recovery_hint

    def test_impact_analysis_error(self):
        err = ImpactAnalysisError("cannot analyze")
        assert isinstance(err, RecoveryError)

    def test_recovery_plan_error(self):
        err = RecoveryPlanError("no viable plan")
        assert isinstance(err, RecoveryError)


class TestWrapException:
    """Test wrap_exception() helper."""

    def test_astralnexus_error_returned_unchanged(self):
        original = ModelRateLimitError("rate limited")
        result = wrap_exception(original)
        assert result is original

    def test_plain_exception_wrapped(self):
        original = ValueError("bad value")
        result = wrap_exception(original)
        assert isinstance(result, AstralNexusError)
        assert "ValueError" in str(result)
        assert "bad value" in str(result)

    def test_fallback_severity_default(self):
        result = wrap_exception(RuntimeError("oops"))
        assert result.severity == Severity.DEGRADED

    def test_fallback_severity_custom(self):
        result = wrap_exception(RuntimeError("oops"), fallback_severity=Severity.FATAL)
        assert result.severity == Severity.FATAL

    def test_original_type_in_details(self):
        result = wrap_exception(TypeError("wrong type"))
        assert result.details.get("original_type") == "TypeError"


class TestFormatToolError:
    """Test format_tool_error() helper."""

    def test_astralnexus_error_returns_structured_json(self):
        err = ToolNotFoundError("web_search")
        result = format_tool_error(err, "web_search")
        parsed = json.loads(result)
        assert parsed["error"] is True
        assert parsed["error_class"] == "ToolNotFoundError"

    def test_plain_exception_returns_json(self):
        err = RuntimeError("connection lost")
        result = format_tool_error(err, "http_request")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "RuntimeError" in parsed["error"]

    def test_tool_name_in_message(self):
        err = RuntimeError("timeout")
        result = format_tool_error(err, "file_reader")
        parsed = json.loads(result)
        assert "file_reader" in parsed["error"]

    def test_no_tool_name_still_works(self):
        err = RuntimeError("oops")
        result = format_tool_error(err)
        parsed = json.loads(result)
        assert "error" in parsed

    def test_result_is_valid_json(self):
        err = ValueError("bad input")
        result = format_tool_error(err, "parser")
        assert json.loads(result)  # Should not raise


class TestExceptionInheritance:
    """Verify the complete inheritance hierarchy."""

    def test_all_inherit_from_base(self):
        """Every exception class inherits from AstralNexusError."""
        classes = [
            RuntimeError_, ModelClientError, ModelRateLimitError,
            ContextOverflowError, ContextCompressionError,
            CoreError, InvalidStateTransition, NodeNotFoundError,
            RouterError, NoAgentAvailable, SchemaValidationError,
            StorageError, ArtifactStoreError,
            ToolError, ToolNotFoundError, ToolExecutionError, ToolTimeoutError,
            ConversationLoopError, ToolCallError,
            RecoveryError, ImpactAnalysisError, RecoveryPlanError,
        ]
        for cls in classes:
            assert issubclass(cls, AstralNexusError), f"{cls.__name__} not subclass of AstralNexusError"

    def test_isinstance_catch_all(self):
        """Catching AstralNexusError catches all subclasses."""
        errors = [
            ModelRateLimitError("test"),
            NodeNotFoundError("test"),
            ToolTimeoutError("test"),
            RecoveryError("test"),
        ]
        for err in errors:
            assert isinstance(err, AstralNexusError)
