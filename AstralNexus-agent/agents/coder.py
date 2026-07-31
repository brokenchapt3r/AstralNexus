from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Coder(BaseAgent):
    """Coder agent responsible for real LLM-driven implementation work."""

    def __init__(self):
        super().__init__(
            role="coder",
            capabilities=["programming", "code_modification", "debugging", "testing"],
        )
        self.default_tools = [
            "file_read", "file_write", "terminal_exec", "search_files",
            "python_exec", "pytest_run",
        ]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Execute coding work through the real LLM runtime only."""
        return self.try_execute_with_runtime(context, task_kind="code_or_script_execution")
