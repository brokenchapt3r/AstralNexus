"""Public data returned by orchestration."""
from __future__ import annotations
from typing import Any, TypedDict

class ExecutionRunResult(TypedDict, total=False):
    """Counters and delivery evidence returned by an orchestration run."""
    status: str
    execution_status: str
    delivery_status: str
    delivery_authorized: bool
    iterations: int
    completed_tasks: int
    failed_tasks: int
    recovery_attempts: int
    interrupt_reason: str
    stop_reason: str
    stop_code: str
    verification_unavailable: dict[str, Any]
    delivery_verification: dict[str, Any]
    blocked_tasks: list[str]
    semantic_spawns: int
    verification_repairs: int
    governed_verification_repairs: int
    verification_replans: int
    terminal_failures: list[str]
    delivery: dict[str, Any]
    elapsed_seconds: float
    final_report: str



from dataclasses import asdict, dataclass
from enum import Enum


class WaveResult(str, Enum):
    RESUME_VERIFICATION = "resume_verification"


@dataclass(frozen=True)
class StandDownVerdict:
    stop_code: str
    stop_reason: str
    scope: str
    lineage_root: str
    state: str
    budget: int
    cost: dict[str, Any]
    budget_dimension: str = "recoveries"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StandDownVerdict:
        return cls(
            stop_code=payload["stop_code"], stop_reason=payload["stop_reason"],
            scope=payload.get("scope", "lineage"), lineage_root=payload["lineage_root"],
            state=payload["state"], budget=payload["budget"], cost=payload["cost"],
            budget_dimension=payload.get("budget_dimension", "recoveries"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
