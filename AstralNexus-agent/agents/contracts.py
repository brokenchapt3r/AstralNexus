"""Agent execution messages and registry port, independent of implementations."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

@dataclass
class AgentContext:
    """Context passed to agents during execution."""
    
    node_id: str = ""
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionOutcome(str, Enum):
    """Result of one Worker execution, distinct from task lifecycle."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"


@dataclass
class AgentResponse:
    """Response from agent execution."""
    
    outcome: ExecutionOutcome = ExecutionOutcome.SUCCEEDED
    content: str = ""
    artifacts: list[str] = field(default_factory=list)
    next_actions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExecutionOutcome):
            self.outcome = ExecutionOutcome(str(self.outcome))


class RegisteredAgent(Protocol):
    role: str
    worker_profile: dict[str, Any]
    default_tools: list[str]
    input_contract: str
    output_contract: str
    model_preference: str
    reasoning_level: str
    capabilities: list[str]
    model_profile: dict[str, Any]
    selected_model: str
    context_policy: dict[str, Any]
    def execute(self, context: AgentContext) -> AgentResponse: ...
