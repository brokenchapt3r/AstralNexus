from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class MemoryCurator(BaseAgent):
    """MemoryCurator agent responsible for real LLM-driven memory curation."""

    def __init__(self):
        super().__init__(
            role="memory_curator",
            capabilities=["knowledge_management", "memory_organization", "conflict_resolution"],
        )
        self.default_tools = ["memory_read", "memory_write", "memory_search", "memory_compress"]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Execute memory curation through the real LLM runtime only."""
        return self.try_execute_with_runtime(context, task_kind="memory_curation_and_recall")
