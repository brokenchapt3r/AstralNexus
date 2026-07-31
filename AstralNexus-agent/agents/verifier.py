from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class VerificationWorker(BaseAgent):
    """Worker that gathers test evidence for the authoritative core Verifier.

    This role never changes task state and never issues an acceptance verdict.
    """

    def __init__(self):
        super().__init__(
            role="verifier",
            capabilities=["testing", "verification", "quality_assurance"],
        )
        self.default_tools = [
            "file_read", "search_files", "terminal_exec", "python_exec", "pytest_run",
        ]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Collect verification evidence through the real LLM runtime only."""
        return self.try_execute_with_runtime(
            context, task_kind="verification_evidence_collection"
        )
