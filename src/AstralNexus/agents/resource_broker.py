from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class ResourceBroker(BaseAgent):
    """Collect resource telemetry and propose feasible scheduling options."""

    def __init__(self):
        super().__init__(
            role="resource_broker",
            capabilities=["resource_telemetry", "model_benchmarking", "cost_analysis"],
        )
        self.default_tools = ["find_skills", "skills_list", "skill_view"]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Produce resource evidence; GlobalGovernor selects the policy."""
        return self.try_execute_with_runtime(
            context, task_kind="resource_telemetry_and_scheduling_options"
        )
