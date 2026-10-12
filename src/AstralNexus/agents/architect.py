from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Architect(BaseAgent):
    """Architecture Worker that materializes executable design contracts."""

    def __init__(self):
        super().__init__(
            role="architect",
            capabilities=[
                "system_architecture",
                "requirement_analysis",
                "api_design",
                "database_modeling",
                "state_machine_design",
                "permission_design",
            ],
        )
        self.default_tools = [
            "file_read",
            "file_write",
            "search_files",
            "terminal_exec",
            "python_exec",
        ]

    def execute(self, context: AgentContext) -> AgentResponse:
        return self.try_execute_with_runtime(
            context, task_kind="architecture_contract_materialization"
        )
