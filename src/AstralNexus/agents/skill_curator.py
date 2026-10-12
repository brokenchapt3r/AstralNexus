"""Governor-controlled Skill discovery and lifecycle worker."""
from __future__ import annotations

from AstralNexus.agents.base import (
    BaseAgent,
    AgentContext,
    AgentResponse,
    ExecutionOutcome,
)


class SkillCurator(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(role="skill_curator", capabilities=["skill_discovery", "skill_audit", "skill_lifecycle"])
        self.default_tools = ["find_skills", "skill_list", "skill_audit", "skill_install", "skill_update"]

    def execute(self, context: AgentContext) -> AgentResponse:
        response = self.try_execute_with_runtime(
            context,
            task_kind="skill discovery, audit, or lifecycle management",
        )
        if response is None:
            return AgentResponse(
                outcome=ExecutionOutcome.FAILED,
                content="Skill curator did not produce a runtime result",
            )
        return response
