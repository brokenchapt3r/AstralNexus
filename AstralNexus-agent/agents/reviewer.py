from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Reviewer(BaseAgent):
    """Reviewer agent responsible for real LLM-driven review."""

    def __init__(self):
        super().__init__(
            role="reviewer",
            capabilities=["risk_assessment", "compatibility_checking", "consistency_verification"],
        )
        self.default_tools = ["file_read", "search_files", "github_repo_contents", "browser_fetch"]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Execute review through the real LLM runtime only."""
        return self.try_execute_with_runtime(context, task_kind="risk_and_consistency_review")
