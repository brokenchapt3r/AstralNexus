from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Tester(BaseAgent):
    """Testing Worker that creates and executes reproducible tests."""

    def __init__(self):
        super().__init__(
            role="tester",
            capabilities=[
                "test_generation",
                "test_execution",
                "unit_testing",
                "integration_testing",
                "end_to_end_testing",
                "api_testing",
            ],
        )
        self.default_tools = [
            "file_read",
            "file_write",
            "search_files",
            "terminal_exec",
            "python_exec",
            "pytest_run",
            "http_get",
        ]

    def execute(self, context: AgentContext) -> AgentResponse:
        return self.try_execute_with_runtime(
            context, task_kind="test_implementation_and_execution"
        )
