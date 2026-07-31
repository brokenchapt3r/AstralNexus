"""View models shared by the live swarm dashboard."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from AstralNexus.core.state_machine import TaskStatus

ACTIVE_TASK_STATES = {
    TaskStatus.ADMITTED.value,
    TaskStatus.WAITING_DEPENDENCIES.value,
    TaskStatus.READY.value,
    TaskStatus.RUNNING.value,
    TaskStatus.RESULT_SUBMITTED.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.REPAIR_PENDING.value,
    TaskStatus.BLOCKED.value,
}
ACTIVE_AGENT_STATES = set(ACTIVE_TASK_STATES)
TERMINAL_TASK_STATES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.SUPERSEDED.value,
}


@dataclass
class AgentViewState:
    """A runtime execution subject, distinct from the task it executes."""

    id: str
    role: str = "unknown"
    capabilities: list[str] = field(default_factory=list)
    status: str = TaskStatus.ADMITTED.value
    current_task: str = ""
    resource_tier: str = "unknown"
    resource_id: str = ""
    model: str = ""
    tools: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    started_at: str = ""
    updated_at: str = ""
    source: str = "trace"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommunicationLink:
    """A sparse, directed communication route between runtime agents."""

    source: str
    target: str
    kind: str = "structured_message"
    reason: str = ""
    source_task: str = ""
    target_task: str = ""
    inferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_task_node(node_id: str, updated_at: str = "") -> dict[str, Any]:
    return {
        "id": node_id,
        "status": TaskStatus.ADMITTED.value,
        "role": "unknown",
        "goal": "",
        "output": "",
        "summary": "",
        "artifacts": [],
        "input": {},
        "input_sources": [],
        "assigned_by": "GlobalGovernor",
        "prompt_source": "Governor Task Contract",
        "plane": "execution",
        "agent_instance": "",
        "current_activity": {},
        "activity_history": [],
        "public_reasoning": "",
        "stage_reports": [],
        "tool_activity": [],
        "recovery_context": {},
        "resource_tier": "unknown",
        "resource_id": "",
        "selected_model": "",
        "updated_at": updated_at,
    }
