from __future__ import annotations
import json
import pytest
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.checkpoint_migrations import migrate_checkpoint_graph, CheckpointSchemaError
from AstralNexus.core.orchestrator import Orchestrator


def conflict_graph():
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="old", goal="task", role="coder", status="failed", metadata={
        "_terminal_reason": "graph patch update has overlapping output ownership: old conflicts with replacement",
        "_recovery_patch_rejected": True, "_terminal_failure": True, "last_failure": {"message": "retained history"},
    }))
    return graph


def test_only_legacy_version_can_rearm_the_historical_conflict():
    legacy, current = conflict_graph(), conflict_graph()
    events = []
    assert migrate_checkpoint_graph(legacy, 0, lambda kind, payload: events.append((kind, payload))) == 1
    assert legacy.nodes["old"].status == "ready"
    assert legacy.nodes["old"].metadata["last_failure"] == {"message": "retained history"}
    assert legacy.nodes["old"].metadata["_terminal_reason"].startswith("graph patch")
    assert migrate_checkpoint_graph(current, 1, lambda *args: pytest.fail("current schema ran migration")) == 1
    assert current.nodes["old"].status == "failed" and current.nodes["old"].metadata["_terminal_failure"]
    assert events[-1][0] == "checkpoint.schema.migrated"


@pytest.mark.parametrize("version", [-1, 2, True, "1", None])
def test_unknown_or_malformed_schema_never_mutates_the_graph(version):
    graph = conflict_graph()
    before = graph.to_dict()
    with pytest.raises(CheckpointSchemaError):
        migrate_checkpoint_graph(graph, version, lambda *args: None)
    assert graph.to_dict() == before


def test_migration_transaction_rolls_back_if_any_rule_fails(monkeypatch):
    from AstralNexus.core import checkpoint_migrations as module
    graph = conflict_graph()
    before = graph.to_dict()
    events = []
    def fail(*args): raise RuntimeError("migration aborted")
    monkeypatch.setitem(module.MIGRATIONS, 0, (module._legacy_retired_owner_conflict, fail))
    with pytest.raises(RuntimeError, match="aborted"):
        migrate_checkpoint_graph(graph, 0, lambda *args: events.append(args))
    assert graph.to_dict() == before and events == []


def test_missing_schema_legacy_checkpoint_is_upgraded_and_not_repeated(tmp_path):
    with Orchestrator(tmp_path, run_id="legacy") as original:
        original.task_graph.add_node(conflict_graph().nodes["old"])
        original._save_checkpoint()
    path = tmp_path / "runtime_state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("checkpoint_schema_version")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with Orchestrator(tmp_path, run_id="legacy") as restored:
        assert restored.runtime_state_manager.state.checkpoint_schema_version == 0
        assert restored.restore_checkpoint()
        assert restored.runtime_state_manager.state.checkpoint_schema_version == 1
        assert restored.task_graph.nodes["old"].status == "ready"
    count = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").count('"event_type": "checkpoint.schema.migrated"')
    with Orchestrator(tmp_path, run_id="legacy") as again:
        assert again.restore_checkpoint()
    assert (tmp_path / "trace.jsonl").read_text(encoding="utf-8").count('"event_type": "checkpoint.schema.migrated"') == count


def test_failed_post_restore_commit_is_not_reported_as_success(tmp_path, monkeypatch):
    with Orchestrator(tmp_path, run_id="run") as original:
        original.task_graph.add_node(TaskNode(node_id="node", goal="task", role="coder"))
        original._save_checkpoint()
    with Orchestrator(tmp_path, run_id="run") as restored:
        monkeypatch.setattr(restored, "_persist_graph_checkpoint", lambda **kwargs: False)
        with pytest.raises(RuntimeError, match="post-restore checkpoint was rejected"):
            restored.restore_checkpoint()
