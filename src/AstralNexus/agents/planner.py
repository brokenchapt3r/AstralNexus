from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class PlannerAgent(BaseAgent):
    """Analyze decomposition options for GlobalGovernor.

    This Worker cannot mutate TaskGraph or admit tasks.
    """

    def __init__(self):
        super().__init__(
            "planner",
            ["decomposition_analysis", "dependency_analysis", "planning_evidence"],
        )

    def execute(self, context: AgentContext) -> AgentResponse:
        """Produce decomposition evidence through the real LLM runtime."""
        return self.try_execute_with_runtime(
            context, task_kind="decomposition_and_dependency_options"
        )
