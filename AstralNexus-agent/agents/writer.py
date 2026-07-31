from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Writer(BaseAgent):
    """Writer agent responsible for real LLM-driven documents and reports."""

    def __init__(self):
        super().__init__(
            role="writer",
            capabilities=["documentation", "report_generation", "visualization"],
        )
        self.default_tools = ["file_read", "file_write"]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Execute writing through the real LLM runtime only."""
        return self.try_execute_with_runtime(context, task_kind="document_and_report_generation")
