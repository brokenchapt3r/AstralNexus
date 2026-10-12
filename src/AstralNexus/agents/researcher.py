from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Researcher(BaseAgent):
    """Researcher agent responsible for real LLM-driven evidence extraction."""

    def __init__(self):
        super().__init__(
            role="researcher",
            capabilities=["information_retrieval", "document_analysis", "evidence_extraction"],
        )
        self.default_tools = ["web_search", "file_read", "http_get", "openalex_search",
                              "crossref_search", "arxiv_search", "browser_fetch"]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Execute research through the real LLM runtime only."""
        return self.try_execute_with_runtime(context, task_kind="research_and_evidence_extraction")
