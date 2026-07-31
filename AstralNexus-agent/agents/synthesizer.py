from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class Synthesizer(BaseAgent):
    """Delivery Worker that integrates only verified upstream artifacts."""

    def __init__(self):
        super().__init__(
            role="synthesizer",
            capabilities=[
                "artifact_integration",
                "delivery_packaging",
                "delivery_manifest_generation",
                "cross_artifact_consistency",
                "final_synthesis",
            ],
        )
        self.default_tools = [
            "file_read",
            "file_write",
            "search_files",
            "terminal_exec",
            "python_exec",
            "pytest_run",
        ]

    def execute(self, context: AgentContext) -> AgentResponse:
        return self.try_execute_with_runtime(
            context, task_kind="verified_delivery_package_synthesis"
        )
