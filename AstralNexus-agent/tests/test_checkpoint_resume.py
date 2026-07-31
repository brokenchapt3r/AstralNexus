from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.message_protocol import StructuredMessage


def test_checkpoint_round_trip_rearms_running_node(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_iterations=1, run_id="resume-test")
    orchestrator.task_graph.add_node({
        "node_id": "T-001",
        "goal": "resume me",
        "role": "coder",
        "status": "running",
        "dependencies": [],
        "constraints": ["preserve API"],
        "metadata": {"kind": "test"},
    })
    orchestrator.iteration_count = 4
    orchestrator._save_checkpoint()

    restored = Orchestrator(tmp_path, max_iterations=1, run_id="resume-test")
    assert restored.restore_checkpoint() is True
    node = restored.task_graph.get_node("T-001")
    assert node is not None
    assert node.status == "ready"
    assert node.metadata["kind"] == "test"
    assert restored.iteration_count == 4


def test_checkpoint_serializes_structured_messages_in_metadata(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_iterations=1, run_id="message-test")
    message = StructuredMessage(
        node_id="T-001",
        role="researcher",
        status="completed",
        summary="evidence collected",
        claims=[{"text": "claim", "confidence": 0.9}],
    )
    orchestrator.task_graph.add_node({
        "node_id": "T-001",
        "goal": "persist structured state",
        "role": "researcher",
        "status": "ready",
        "dependencies": [],
        "metadata": {"last_message": message},
    })

    orchestrator._save_checkpoint()
    persisted_graph = orchestrator.graph_store.load_task_graph("message-test")
    persisted = persisted_graph.get_node("T-001").metadata["last_message"]
    assert persisted["node_id"] == "T-001"
    assert persisted["claims"][0]["confidence"] == 0.9


def test_checkpoint_does_not_duplicate_graph_into_runtime_state(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_iterations=1, run_id="ssot-test")
    orchestrator.task_graph.add_node({
        "node_id": "T-001",
        "goal": "single source",
        "role": "researcher",
        "status": "ready",
    })
    orchestrator._save_checkpoint()

    state = __import__("json").loads(
        (tmp_path / "runtime_state.json").read_text(encoding="utf-8")
    )
    assert "task_graph" not in state
    assert (tmp_path / "task_graph.sqlite").exists()
    assert not (tmp_path / "checkpoint.json").exists()
