"""Agent lifecycle manager for AstralNexus (天枢).

Manages the full lifecycle of agent instances:
  - Creation and initialization
  - State monitoring and persistence
  - Cleanup and teardown
  - Health checks and recovery

Implements 天枢方案书 §3.3 异构角色层 lifecycle management.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from AstralNexus.core.agent_registry import get_agent_registry
from AstralNexus.core.artifacts import append_jsonl
from AstralNexus.core.exceptions import NoAgentAvailable
from AstralNexus.core.state_machine import TaskStatus, assert_transition, normalize_status

logger = logging.getLogger(__name__)


@dataclass
class AgentInstance:
    """Represents a running agent instance with lifecycle tracking."""

    agent_id: str
    role: str
    node_id: str
    state: TaskStatus = TaskStatus.ADMITTED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    iteration_count: int = 0
    token_usage: Dict[str, int] = field(default_factory=dict)

    def transition(
        self,
        new_state: TaskStatus | str,
        reason: Optional[str] = None,
    ) -> None:
        """Apply the canonical lifecycle protocol to this worker instance."""
        canonical = TaskStatus(normalize_status(new_state))
        old_state = self.state
        assert_transition(old_state.value, canonical.value)
        self.state = canonical

        if canonical == TaskStatus.RUNNING and not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        elif canonical in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SUPERSEDED,
        }:
            self.completed_at = datetime.now(timezone.utc).isoformat()
        else:
            self.completed_at = None

        if reason:
            self.metadata.setdefault("state_transitions", []).append({
                "from": old_state.value,
                "to": canonical.value,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for persistence."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "node_id": self.node_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
            "error_message": self.error_message,
            "iteration_count": self.iteration_count,
            "token_usage": self.token_usage,
        }


class AgentLifecycleManager:
    """Manages agent instances across their full lifecycle."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.agents: Dict[str, AgentInstance] = {}
        self.agent_factory: Optional[Callable] = None
        self._lock = threading.RLock()
        self._state_log_path = run_dir / "agent_lifecycle.jsonl"
        self._state_log_path.parent.mkdir(parents=True, exist_ok=True)

    def set_agent_factory(self, factory: Callable) -> None:
        """Set the factory function for creating agent instances.

        The factory should accept (role: str, node_id: str, **kwargs)
        and return a governed RoleWorker instance.
        """
        self.agent_factory = factory

    def create_agent(
        self,
        role: str,
        node_id: str,
        *,
        agent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentInstance:
        """Create and register a new agent instance.

        Args:
            role: The role name (e.g., "coder", "verifier")
            node_id: The task node this agent is assigned to
            agent_id: Optional custom ID (auto-generated if omitted)
            metadata: Optional metadata dict

        Returns:
            The created AgentInstance

        Raises:
            NoAgentAvailable: If the role is not registered in the agent registry
        """
        registry = get_agent_registry()
        role_def = registry.get_role(role)
        if not role_def:
            raise NoAgentAvailable(f"Role '{role}' not registered in agent registry")

        if agent_id is None:
            agent_id = f"agent-{role}-{node_id}-{int(time.time() * 1000) % 10000}"

        instance = AgentInstance(
            agent_id=agent_id,
            role=role,
            node_id=node_id,
            metadata=metadata or {},
        )

        with self._lock:
            self.agents[agent_id] = instance
            self._log_state_change(instance, "created")

        logger.info("Created agent %s (role=%s, node=%s)", agent_id, role, node_id)
        return instance

    def get_agent(self, agent_id: str) -> Optional[AgentInstance]:
        """Get an agent instance by ID."""
        with self._lock:
            return self.agents.get(agent_id)

    def get_agents_by_role(self, role: str) -> List[AgentInstance]:
        """Get all agent instances with a specific role."""
        with self._lock:
            return [a for a in self.agents.values() if a.role == role]

    def get_agents_by_node(self, node_id: str) -> List[AgentInstance]:
        """Get all agent instances assigned to a specific node."""
        with self._lock:
            return [a for a in self.agents.values() if a.node_id == node_id]

    def get_active_agents(self) -> List[AgentInstance]:
        """Get all non-terminal worker instances."""
        active_states = {
            TaskStatus.ADMITTED,
            TaskStatus.WAITING_DEPENDENCIES,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_SUBMITTED,
            TaskStatus.VERIFYING,
            TaskStatus.REPAIR_PENDING,
            TaskStatus.BLOCKED,
        }
        with self._lock:
            return [a for a in self.agents.values() if a.state in active_states]

    def transition_agent(
        self,
        agent_id: str,
        new_state: TaskStatus | str,
        reason: Optional[str] = None,
    ) -> bool:
        """Transition an agent to a new state.

        Args:
            agent_id: The agent instance ID
            new_state: The canonical target state
            reason: Optional reason for the transition

        Returns:
            True if transition succeeded, False if agent not found
        """
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                logger.warning("Agent %s not found for transition", agent_id)
                return False

            old_state = agent.state
            agent.transition(new_state, reason)
            self._log_state_change(
                agent,
                f"transitioned from {old_state.value} to {agent.state.value}",
            )
            logger.info(
                "Agent %s: %s -> %s (%s)",
                agent_id,
                old_state.value,
                agent.state.value,
                reason or "no reason",
            )
            return True

    def mark_agent_error(
        self,
        agent_id: str,
        error_message: str,
        *,
        recoverable: bool = True,
    ) -> bool:
        """Mark an agent as failed with an error message."""
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                return False

            agent.error_message = error_message
            agent.transition(TaskStatus.FAILED, f"error: {error_message}")
            if recoverable:
                agent.transition(
                    TaskStatus.REPAIR_PENDING,
                    "recoverable execution failure awaiting Governor repair",
                )
            self._log_state_change(agent, f"error (recoverable={recoverable})")
            logger.error("Agent %s error: %s", agent_id, error_message)
            return True

    def update_agent_metrics(
        self,
        agent_id: str,
        *,
        iteration_count: Optional[int] = None,
        token_usage: Optional[Dict[str, int]] = None,
    ) -> bool:
        """Update agent metrics (iterations, token usage)."""
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                return False

            if iteration_count is not None:
                agent.iteration_count = iteration_count
            if token_usage is not None:
                agent.token_usage.update(token_usage)

            return True

    def terminate_agent(self, agent_id: str, reason: Optional[str] = None) -> bool:
        """Cancel an agent while preserving its lifecycle evidence."""
        return self.transition_agent(
            agent_id,
            TaskStatus.CANCELLED,
            reason or "manual termination",
        )

    def mark_background(self, agent_id: str, reason: str = "background execution") -> bool:
        """Record execution mode without inventing a second lifecycle state."""
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                return False
            agent.metadata["execution_mode"] = "background"
            agent.metadata["background_reason"] = reason
            self._log_state_change(agent, "execution mode changed to background")
            return True

    def mark_result_submitted(
        self,
        agent_id: str,
        reason: str = "result submitted for verification",
    ) -> bool:
        """Advance a worker result to the canonical verification boundary."""
        return self.transition_agent(agent_id, TaskStatus.RESULT_SUBMITTED, reason)

    def record_verification_verdict(
        self,
        agent_id: str,
        *,
        approved: bool,
        repairable: bool = True,
        reason: str = "Verifier verdict recorded",
    ) -> bool:
        """Apply a Verifier verdict without making a quality decision here."""
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                return False
            if agent.state == TaskStatus.RESULT_SUBMITTED:
                agent.transition(TaskStatus.VERIFYING, "result entered verification")
            if agent.state != TaskStatus.VERIFYING:
                raise ValueError(
                    f"Worker {agent_id} cannot consume a verification verdict "
                    f"from state {agent.state.value}"
                )
            target = (
                TaskStatus.COMPLETED
                if approved
                else TaskStatus.REPAIR_PENDING
                if repairable
                else TaskStatus.FAILED
            )
            agent.transition(target, reason)
            self._log_state_change(agent, f"Verifier verdict applied: {target.value}")
            return True

    def cancel_agent(self, agent_id: str, reason: str = "cancelled by Governor") -> bool:
        """Cancel a worker without deleting its lifecycle evidence."""
        return self.transition_agent(agent_id, TaskStatus.CANCELLED, reason)

    def cleanup_completed_agents(self, *, keep_hours: int = 24) -> int:
        """Remove completed/failed agents older than keep_hours.

        Returns:
            Number of agents removed
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (keep_hours * 3600)
        removed = 0

        with self._lock:
            to_remove = []
            for agent_id, agent in self.agents.items():
                if agent.state in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.SUPERSEDED,
                }:
                    if agent.completed_at:
                        completed_ts = datetime.fromisoformat(agent.completed_at).timestamp()
                        if completed_ts < cutoff:
                            to_remove.append(agent_id)

            for agent_id in to_remove:
                del self.agents[agent_id]
                removed += 1
                logger.info("Cleaned up old agent %s", agent_id)

        return removed

    def get_lifecycle_summary(self) -> Dict[str, Any]:
        """Get a summary of all agent lifecycles."""
        with self._lock:
            by_state: Dict[str, int] = {}
            by_role: Dict[str, int] = {}
            total_tokens = 0

            for agent in self.agents.values():
                by_state[agent.state.value] = by_state.get(agent.state.value, 0) + 1
                by_role[agent.role] = by_role.get(agent.role, 0) + 1
                # Provider payloads may include non-numeric usage metadata.
                total_tokens += sum(
                    v for v in agent.token_usage.values()
                    if isinstance(v, (int, float))
                )

            return {
                "total_agents": len(self.agents),
                "by_state": by_state,
                "by_role": by_role,
                "total_token_usage": total_tokens,
                "active_count": len(self.get_active_agents()),
            }

    def export_active_snapshot(self) -> List[Dict[str, Any]]:
        """Project active Worker state for the authoritative RuntimeState."""
        return [agent.to_dict() for agent in self.get_active_agents()]

    def restore_snapshot(self, data: List[Dict[str, Any]]) -> None:
        """Rebuild active Worker views from RuntimeState, never a side file."""
        if not isinstance(data, list):
            raise TypeError("agent lifecycle snapshot must be a list")
        with self._lock:
            self.agents.clear()
            for raw in data:
                agent_data = dict(raw)
                agent_data["state"] = TaskStatus(normalize_status(agent_data["state"]))
                agent_id = str(agent_data["agent_id"])
                self.agents[agent_id] = AgentInstance(**agent_data)

    def _log_state_change(self, agent: AgentInstance, event: str) -> None:
        """Log a state change event to the lifecycle log."""
        append_jsonl(self._state_log_path, {
            "event": event,
            "agent_id": agent.agent_id,
            "role": agent.role,
            "node_id": agent.node_id,
            "state": agent.state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def health_check(self, agent_id: str) -> Dict[str, Any]:
        """Perform a health check on an agent.

        Returns:
            Health status dict with 'healthy', 'state', 'uptime_seconds', etc.
        """
        agent = self.get_agent(agent_id)
        if not agent:
            return {"healthy": False, "reason": "agent not found"}

        uptime = 0
        if agent.started_at:
            start_ts = datetime.fromisoformat(agent.started_at).timestamp()
            uptime = time.time() - start_ts

        return {
            "healthy": agent.state in {
                TaskStatus.READY,
                TaskStatus.RUNNING,
                TaskStatus.RESULT_SUBMITTED,
                TaskStatus.VERIFYING,
            },
            "state": agent.state.value,
            "uptime_seconds": round(uptime, 2),
            "iteration_count": agent.iteration_count,
            "token_usage": agent.token_usage,
            "error_message": agent.error_message,
        }
