from __future__ import annotations

from AstralNexus.agents.base import AgentContext, AgentResponse, BaseAgent


class RecoveryAgent(BaseAgent):
    """Analyze faults and propose evidence-backed recovery options.

    GlobalGovernor remains the only component allowed to select a recovery
    strategy. This Worker supplies diagnosis and options through the normal
    structured result channel.
    """

    def __init__(self):
        super().__init__(
            role="recovery_agent",
            capabilities=["fault_diagnosis", "impact_analysis", "recovery_option_analysis"],
        )
        self.default_tools = [
            "file_read",
            "search_files",
            "terminal_exec",
            "python_exec",
            "pytest_run",
        ]

    def execute(self, context: AgentContext) -> AgentResponse:
        """Produce diagnosis and recovery options through the real LLM runtime."""
        return self.try_execute_with_runtime(context, task_kind="fault_diagnosis_and_recovery_options")
