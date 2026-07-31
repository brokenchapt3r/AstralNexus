"""Runtime metrics derived exclusively from auditable runtime events."""
from __future__ import annotations

from typing import Any

from .event_parser import event_payload, event_type_of
from .models import ACTIVE_AGENT_STATES, TERMINAL_TASK_STATES


class RuntimeMetrics:
    """Accumulate competition-facing efficiency and resilience evidence."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tokens_before_compression = 0
        self.tokens_saved = 0
        self.communication_messages = 0
        self.recovery_events = 0
        self.wave_sizes: list[int] = []
        self.current_wave_id: int | str | None = None
        self.topology_sparsity: float | None = None

    def apply(self, event: dict[str, Any]) -> None:
        event_type = event_type_of(event)
        payload = event_payload(event)
        if event_type in {"llm.response", "llm.usage"}:
            self.prompt_tokens += _number(payload, "prompt_tokens", "input_tokens")
            self.completion_tokens += _number(
                payload, "completion_tokens", "output_tokens"
            )
        if event_type == "context.compressed":
            before = _number(payload, "tokens_before")
            after = _number(payload, "tokens_after")
            if before > after:
                self.tokens_before_compression += before
                self.tokens_saved += before - after
        if event_type in {
            "message.sent", "task.structured_message",
            "agent.communication.link_created", "task.evidence_chain.created",
        }:
            self.communication_messages += 1
        if (
            event_type.startswith("recovery.")
            or event_type.startswith("fault_recovery.")
            or "recovery" in event_type and event_type.endswith((".applied", ".completed"))
        ):
            self.recovery_events += 1
        if event_type in {"router.ready_wave", "orchestrator.ready_wave", "task.ready_wave"}:
            width = _number(payload, "wave_width")
            if not width:
                nodes = payload.get("node_ids") or payload.get("ready_nodes") or []
                width = len(nodes) if isinstance(nodes, list) else 0
            self.wave_sizes.append(width)
            self.current_wave_id = payload.get("wave_id", len(self.wave_sizes))
        if event_type in {"topology.sparsity", "topology.semantic_edges"}:
            value = payload.get("sparsity_ratio", payload.get("sparsity"))
            if isinstance(value, (int, float)):
                self.topology_sparsity = float(value)

    def snapshot(
        self,
        tasks: list[dict[str, Any]],
        agents: list[dict[str, Any]],
        communication_links: int,
    ) -> dict[str, Any]:
        active_agents = sum(
            1 for item in agents if str(item.get("status")) in ACTIVE_AGENT_STATES
        )
        completed = sum(
            1 for item in tasks
            if str(item.get("status")) in TERMINAL_TASK_STATES - {"failed"}
        )
        failed = sum(1 for item in tasks if str(item.get("status")) == "failed")
        resolved = completed + failed
        average_wave = (
            sum(self.wave_sizes) / len(self.wave_sizes) if self.wave_sizes else 0.0
        )
        token_saving = (
            self.tokens_saved / self.tokens_before_compression
            if self.tokens_before_compression else 0.0
        )
        return {
            "agent_count": len(agents),
            "active_agents": active_agents,
            "parallel_tasks": max(
                active_agents, self.wave_sizes[-1] if self.wave_sizes else 0
            ),
            "peak_parallelism": max(self.wave_sizes, default=active_agents),
            "average_wave_width": round(average_wave, 2),
            "wave_count": len(self.wave_sizes),
            "communication_links": communication_links,
            "communication_messages": self.communication_messages,
            "recovery_count": self.recovery_events,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "token_cost": self.prompt_tokens + self.completion_tokens,
            "tokens_saved": self.tokens_saved,
            "token_saving_ratio": round(token_saving, 4),
            "success_rate": round(completed / resolved, 4) if resolved else 0.0,
            "topology_sparsity": self.topology_sparsity,
        }


def _number(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0
