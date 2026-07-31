from pathlib import Path

from AstralNexus.core.lifecycle import AgentLifecycleManager
from AstralNexus.core.state_machine import TaskStatus


def test_execution_mode_and_submitted_result_survive_runtime_snapshot(tmp_path: Path):
    manager = AgentLifecycleManager(tmp_path)
    instance = manager.create_agent("coder", "T-001")
    manager.transition_agent(instance.agent_id, TaskStatus.READY, "ready")
    manager.transition_agent(instance.agent_id, TaskStatus.RUNNING, "started")
    manager.mark_background(instance.agent_id)
    manager.mark_result_submitted(instance.agent_id)
    restored = AgentLifecycleManager(tmp_path)
    restored.restore_snapshot(manager.export_active_snapshot())
    loaded = restored.get_agent(instance.agent_id)

    assert loaded is not None
    assert loaded.state == TaskStatus.RESULT_SUBMITTED
    assert loaded.metadata["execution_mode"] == "background"
