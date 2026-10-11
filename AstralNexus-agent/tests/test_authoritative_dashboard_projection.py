from __future__ import annotations
import sqlite3
from pathlib import Path
import pytest
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskNode
from AstralNexus.runtime.run_coordinator import RunCoordinator
from AstralNexus.runtime.authoritative_projection import read_current_projection
from AstralNexus.dashboard.state import LiveRunState
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


def managed(run):
    coordinator = RunCoordinator(run, run_id=run.name)
    lease = coordinator.acquire("owner")
    assert lease is not None
    engine = Orchestrator(run, run_id="graph", checkpoint_sink=lambda payload: coordinator.record_checkpoint(lease, payload),
        ownership_probe=lambda: coordinator.owns(lease), fence_token_provider=lambda: coordinator.fence_token(lease))
    return engine, coordinator, lease


def test_current_db_status_overrides_reordered_events_and_deleted_nodes(tmp_path):
    engine, coordinator, lease = managed(tmp_path)
    with engine:
        engine.task_graph.add_node(TaskNode(node_id="real", role="coder", goal="current", status="ready"))
        assert engine._persist_graph_checkpoint(stage="current")
        state = LiveRunState(tmp_path)
        for node in ("real", "deleted"):
            state.apply({"event_type": "node.started", "payload": {"node_id": node, "role": "coder", "goal": "historical", "status": "running"}})
            state.apply({"event_type": "node.completed", "payload": {"node_id": node, "role": "coder", "status": "completed"}})
        assert "deleted" in state.nodes
        snapshot = state.snapshot()
        assert snapshot["authority"]["verified"]
        assert [(node["id"], node["status"]) for node in snapshot["nodes"]] == [("real", "ready")]
        assert snapshot["run_status"] == "running"


def test_all_nodes_completed_does_not_finish_delivery_while_owner_is_running(tmp_path):
    engine, coordinator, lease = managed(tmp_path)
    with engine:
        engine.task_graph.add_node(TaskNode(node_id="node", role="coder", goal="executed", status="completed"))
        engine.runtime_state_manager.state.run_status = "completed"
        assert engine._persist_graph_checkpoint(stage="execution_done")
        snapshot = LiveRunState(tmp_path).snapshot()
        assert snapshot["run_status"] == "running"
        assert snapshot["runtime_brief"]["phase"] == "交付验收/发布处理中"
        assert coordinator.finish(lease, "blocked", stop_code="verification_unavailable")
        assert LiveRunState(tmp_path).snapshot()["run_status"] == "blocked"


def test_database_error_is_unknown_not_success_from_old_trace(tmp_path):
    (tmp_path / "task_graph.sqlite").write_bytes(b"corrupt")
    state = LiveRunState(tmp_path)
    state.apply({"event_type": "node.started", "payload": {"node_id": "old", "role": "coder", "status": "running"}})
    state.apply({"event_type": "node.completed", "payload": {"node_id": "old", "role": "coder", "status": "completed"}})
    snapshot = state.snapshot()
    assert snapshot["run_status"] == "unknown"
    assert snapshot["nodes"][0]["status"] == "unknown"
    assert snapshot["authority"]["source"] == "unavailable"


def test_projection_retries_if_checkpoint_changes_between_reads(tmp_path, monkeypatch):
    engine, _, _ = managed(tmp_path)
    with engine:
        engine.task_graph.add_node(TaskNode(node_id="old", role="coder", goal="old"))
        assert engine._persist_graph_checkpoint(stage="initial")
        original = SQLiteTaskGraphStore.load_task_graph
        changed = []
        def race(store, run_id):
            graph = original(store, run_id)
            if not changed:
                changed.append(1)
                engine.task_graph.add_node(TaskNode(node_id="new", role="coder", goal="new"))
                assert engine._persist_graph_checkpoint(stage="advanced")
            return graph
        monkeypatch.setattr(SQLiteTaskGraphStore, "load_task_graph", race)
        projection = read_current_projection(tmp_path)
        assert projection["verified"] and projection["commit_seq"] == 2
        assert {node["node_id"] for node in projection["graph"]["nodes"]} == {"old", "new"}


def test_readonly_observer_does_not_initialize_or_migrate_schema(tmp_path, monkeypatch):
    engine, _, _ = managed(tmp_path)
    with engine:
        assert engine._persist_graph_checkpoint(stage="initial")
        def forbidden(*args): pytest.fail("observer attempted DDL/migration")
        monkeypatch.setattr(SQLiteTaskGraphStore, "_init_db", forbidden)
        assert read_current_projection(tmp_path)["verified"]
        with SQLiteTaskGraphStore(tmp_path / "task_graph.sqlite", read_only=True)._connect() as connection:
            with pytest.raises(sqlite3.OperationalError): connection.execute("CREATE TABLE accidental_write(x)")


def test_multiple_fence_identities_are_not_guessed(tmp_path):
    store = SQLiteTaskGraphStore(tmp_path / "task_graph.sqlite")
    with store._connect() as connection:
        for identity in ("A", "B"):
            connection.execute("INSERT INTO graph_commit_fence(run_id,generation,commit_seq,graph_revision,committed_at,cursor_json) VALUES(?,1,1,1,'now','{}')", (identity,))
    projection = read_current_projection(tmp_path)
    assert projection["source"] == "unavailable" and not projection["verified"]
