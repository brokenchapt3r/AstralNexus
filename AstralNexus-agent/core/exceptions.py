"""Unified exception hierarchy for AstralNexus (天枢).

Mirrors the layered error taxonomy described in the 天枢AstralNexus完善方案书 §4.2.2:
  - Base class `AstralNexusError` for all framework exceptions
  - Per-layer subclasses: runtime, core, storage, tool, router, recovery
  - Each exception carries a severity tag (fatal / degraded / recoverable)
    and an optional recovery hint so the orchestrator can decide whether
    to retry, escalate, or abort without inspecting string messages.

Design notes:
  * Tool dispatch is wrapped in a try/except and returns a structured
    ``{"error": ...}`` JSON string rather than propagating raw exceptions.
    This preserves the "never let the model see a Python traceback" posture.
  * Severity levels map to the orchestrator's automatic-recovery
    decisions: ``recoverable`` -> retry or re-route;
    ``degraded`` -> fallback resource or simplified execution;
    ``fatal`` -> abort the affected subgraph and record a fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    """How the orchestrator should react to an error."""

    RECOVERABLE = "recoverable"   # retry / re-route within the same role
    DEGRADED = "degraded"         # fallback resource, simplified execution
    FATAL = "fatal"               # abort subgraph, record fault, escalate


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class AstralNexusError(Exception):
    """Root of every AstralNexus exception.

    Attributes:
        severity:  How the orchestrator should react.
        details:   Arbitrary structured payload (node_id, tool name, …).
        recovery_hint:  Suggested automatic-recovery strategy.
    """

    severity: Severity = Severity.FATAL
    recovery_hint: str = "manual intervention required"

    def __init__(
        self,
        message: str = "",
        *,
        severity: Optional[Severity] = None,
        recovery_hint: Optional[str] = None,
        **details: Any,
    ):
        super().__init__(message)
        if severity is not None:
            self.severity = severity
        if recovery_hint is not None:
            self.recovery_hint = recovery_hint
        self.details: Dict[str, Any] = details

    def to_structured(self) -> Dict[str, Any]:
        """Serialize to a dict suitable for JSON trace events and tool results."""
        return {
            "error": True,
            "error_class": type(self).__name__,
            "message": str(self),
            "severity": self.severity.value,
            "recovery_hint": self.recovery_hint,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Runtime layer  (model calls, provider resolution, context management)
# ---------------------------------------------------------------------------

class RuntimeError_(AstralNexusError):
    """Errors originating in the runtime layer."""

    severity = Severity.DEGRADED
    recovery_hint = "check model/provider configuration and credentials"


class ModelClientError(RuntimeError_):
    """Model provider resolution or API call failure."""

    recovery_hint = "verify API key, base_url, and model name"


class ModelRateLimitError(ModelClientError):
    """Rate-limited by the provider — safe to retry after backoff."""

    severity = Severity.RECOVERABLE
    recovery_hint = "backoff and retry; consider switching to a fallback model"


class ContextOverflowError(RuntimeError_):
    """Prompt exceeds the model's context window."""

    recovery_hint = "compress context or split the task into smaller nodes"


class ContextCompressionError(RuntimeError_):
    """Context compression failed — critical facts may be lost."""

    recovery_hint = "fall back to artifact-based recall instead of inline context"


class InterruptionRequested(RuntimeError_):
    """Cooperative cancellation requested by the interactive controller."""

    severity = Severity.RECOVERABLE
    recovery_hint = "resume the persisted run or apply the queued requirement patch"


# ---------------------------------------------------------------------------
# Core layer  (task graph, state machine, orchestrator, router)
# ---------------------------------------------------------------------------

class CoreError(AstralNexusError):
    """Errors in the core orchestration layer."""

    severity = Severity.DEGRADED


class TaskGraphError(CoreError):
    """Invalid task-graph operation (missing node, cycle, bad transition)."""

    recovery_hint = "inspect the task graph for structural integrity"


class InvalidStateTransition(TaskGraphError):
    """Attempted a state transition that violates the state machine."""

    recovery_hint = "check state_machine.py for valid transitions"


class NodeNotFoundError(TaskGraphError):
    """Referenced node does not exist in the graph."""

    severity = Severity.RECOVERABLE
    recovery_hint = "the node may have been pruned; re-query the graph"


class RouterError(CoreError):
    """Routing / role-selection failure."""

    recovery_hint = "expand the role pool or relax routing constraints"


class NoAgentAvailable(RouterError):
    """No agent could be spawned for the required role."""

    severity = Severity.DEGRADED
    recovery_hint = "return the capability gap to GlobalGovernor for replanning"


class SchemaValidationError(CoreError):
    """Structured message failed schema validation."""

    severity = Severity.RECOVERABLE
    recovery_hint = "spawn a format-repair node instead of rejecting outright"


class RealLLMRequiredError(CoreError):
    """LLM decomposition failed and template fallback is not allowed in production."""

    severity = Severity.FATAL
    recovery_hint = "check LLM API credentials, network connectivity, and model availability"


# ---------------------------------------------------------------------------
# Storage layer  (SQLite, artifacts, trace)
# ---------------------------------------------------------------------------

class StorageError(AstralNexusError):
    """Database or file-system errors in the storage layer."""

    severity = Severity.RECOVERABLE
    recovery_hint = "check disk space and file permissions; retry after brief delay"


class ArtifactStoreError(StorageError):
    """Artifact read/write failure."""


# ---------------------------------------------------------------------------
# Tool layer  (tool dispatch, execution, sandbox)
# ---------------------------------------------------------------------------

class ToolError(AstralNexusError):
    """Errors during tool dispatch or execution."""

    severity = Severity.RECOVERABLE
    recovery_hint = "log the error and let the orchestrator decide on retry"


class ToolNotFoundError(ToolError):
    """Requested tool is not registered or not available."""

    recovery_hint = "check toolsets configuration; the tool may need to be enabled"


class ToolExecutionError(ToolError):
    """Tool handler raised an exception."""

    recovery_hint = "inspect tool arguments and environment; consider retry"


class ToolTimeoutError(ToolError):
    """Tool execution exceeded the allowed time budget."""

    recovery_hint = "increase timeout or split the operation"


# ---------------------------------------------------------------------------
# Conversation layer (conversation loop, dialogue management)
# ---------------------------------------------------------------------------

class ConversationLoopError(AstralNexusError):
    """Errors in the conversation loop (dialogue management, iteration limits)."""

    severity = Severity.DEGRADED
    recovery_hint = "check conversation state and model responses; may need to reset"


class ToolCallError(AstralNexusError):
    """Errors during tool call parsing or execution within the conversation loop."""

    severity = Severity.RECOVERABLE
    recovery_hint = "validate tool arguments and check tool availability"


# ---------------------------------------------------------------------------
# Recovery layer  (fault detection, impact analysis, recovery planning)
# ---------------------------------------------------------------------------

class RecoveryError(AstralNexusError):
    """Errors in the fault-recovery subsystem."""

    severity = Severity.FATAL
    recovery_hint = "recovery itself failed — escalate to human operator"


class ImpactAnalysisError(RecoveryError):
    """Could not determine the blast radius of a fault."""


class RecoveryPlanError(RecoveryError):
    """Could not generate a viable recovery plan."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wrap_exception(exc: BaseException, *, fallback_severity: Severity = Severity.DEGRADED) -> AstralNexusError:
    """Wrap an arbitrary exception into the AstralNexus hierarchy.

    If *exc* is already an ``AstralNexusError``, return it unchanged.
    Otherwise, wrap it in a generic ``AstralNexusError`` with the given
    fallback severity.
    """
    if isinstance(exc, AstralNexusError):
        return exc
    return AstralNexusError(
        f"{type(exc).__name__}: {exc}",
        severity=fallback_severity,
        recovery_hint="inspect the original exception for details",
        original_type=type(exc).__name__,
    )


def format_tool_error(exc: BaseException, tool_name: str = "") -> str:
    """Format a tool exception as a JSON error string.

    Returns a string like ``{"error": "..."}`` so the model never sees a
    raw Python traceback.
    """
    import json

    if isinstance(exc, AstralNexusError):
        return json.dumps(exc.to_structured(), ensure_ascii=False)

    prefix = f"Tool '{tool_name}' failed: " if tool_name else "Tool execution failed: "
    return json.dumps(
        {"error": f"{prefix}{type(exc).__name__}: {exc}"},
        ensure_ascii=False,
    )
