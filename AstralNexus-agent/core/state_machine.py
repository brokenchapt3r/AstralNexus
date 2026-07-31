from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """Complete lifecycle states for task graph nodes.
    
    Flow:
        proposed → admitted → waiting_dependencies → ready → running → result_submitted → verifying → completed
                                                             ↘ ready (cooperative interruption)
                                                                                                     or repair_pending
                                                                                                     or failed
                                                                                                     or cancelled
    """
    PROPOSED = "proposed"                        # LLM/governor proposed (not yet in graph)
    ADMITTED = "admitted"                        # Governor approved, node in graph
    WAITING_DEPENDENCIES = "waiting_dependencies"  # Admitted but deps not satisfied
    READY = "ready"                              # Dependencies satisfied, can execute
    RUNNING = "running"                          # Currently executing
    RESULT_SUBMITTED = "result_submitted"        # Execution done, result posted
    VERIFYING = "verifying"                      # Core Verifier checking
    COMPLETED = "completed"                      # Verification passed (terminal success)
    REPAIR_PENDING = "repair_pending"            # Verification failed, needs local repair
    CANCELLED = "cancelled"                      # Explicitly cancelled
    FAILED = "failed"                            # Permanent failure
    SUPERSEDED = "superseded"                    # Replaced by another node
    BLOCKED = "blocked"                          # Resource/dependency blocked


class RunStatus(str, Enum):
    """Lifecycle of one persisted orchestration run."""

    CREATED = "created"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class RuntimePhase(str, Enum):
    """Observable stage of the autonomous control loop."""

    INITIALIZED = "initialized"
    PLANNING = "planning"
    OBSERVING = "observing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


def normalize_runtime_phase(phase: str | RuntimePhase) -> str:
    value = phase.value if isinstance(phase, RuntimePhase) else str(phase)
    canonical = {item.value for item in RuntimePhase}
    if value not in canonical:
        raise ValueError(f"Unknown autonomous runtime phase: {value}")
    return value


def normalize_run_status(status: str | RunStatus) -> str:
    value = status.value if isinstance(status, RunStatus) else str(status)
    canonical = {item.value for item in RunStatus}
    if value not in canonical:
        raise ValueError(f"Unknown orchestration run status: {value}")
    return value


def normalize_status(status: str | TaskStatus) -> str:
    """Validate and return a canonical task lifecycle value.

    Non-lifecycle aliases are deliberately rejected. A caller that supplies a
    verification verdict or execution outcome where a lifecycle state is
    required is a protocol error, not a compatibility case.
    """
    value = status.value if isinstance(status, TaskStatus) else str(status)
    canonical = {item.value for item in TaskStatus}
    if value not in canonical:
        raise ValueError(f"Unknown task lifecycle status: {value}")
    return value


VALID_TRANSITIONS: dict[str, set[str]] = {
    # Initial states
    "proposed": {"admitted", "cancelled"},
    "admitted": {"waiting_dependencies", "ready", "repair_pending", "failed", "cancelled", "superseded"},
    
    # Dependency waiting
    "waiting_dependencies": {"ready", "repair_pending", "cancelled", "superseded"},
    
    # Ready to execute
    "ready": {"running", "blocked", "repair_pending", "cancelled", "superseded"},
    
    # Execution
    # A live interactive interruption returns the node to ready.  It is not a
    # failure and must remain resumable after a checkpoint or graph patch.
    "running": {"result_submitted", "ready", "failed", "blocked", "cancelled", "superseded"},
    "blocked": {"ready", "repair_pending", "cancelled", "superseded"},
    
    # Post-execution verification
    "result_submitted": {"verifying", "repair_pending", "failed", "cancelled", "superseded"},
    "verifying": {"completed", "repair_pending", "failed", "cancelled", "superseded"},
    
    # Repair cycle
    "repair_pending": {"ready", "failed", "cancelled", "superseded"},
    
    # Failure recovery
    "failed": {"repair_pending", "cancelled", "superseded"},
    
    # Terminal states
    "completed": {"superseded"},
    "cancelled": set(),
    "superseded": set(),
}


def can_transition(current: str, target: str) -> bool:
    """Check if a state transition is valid."""
    current = normalize_status(current)
    target = normalize_status(target)
    return target in VALID_TRANSITIONS.get(current, set())


def assert_transition(current: str, target: str) -> None:
    """Assert that a state transition is valid, raising ValueError if not."""
    normalized_current = normalize_status(current)
    normalized_target = normalize_status(target)
    if normalized_current == normalized_target:
        return
    if not can_transition(normalized_current, normalized_target):
        raise ValueError(
            "Invalid task graph state transition: "
            f"{normalized_current} -> {normalized_target}"
        )
