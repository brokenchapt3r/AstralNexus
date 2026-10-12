"""Storage invariants exercised through public checkpoint and restore APIs."""
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
import pytest
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


def graph(goal: str = "old", full: bool = True) -> TaskGraph:
    result = TaskGraph()
    result.add_node({"node_id": "A", "goal": goal, "role": "coder"})
    if full:
        result.add_node({"node_id": "B", "goal": "consumer", "role": "tester"})
        result.add_edge({"source": "A", "target": "B", "edge_type": "depends"})
        result.add_fact({"fact_id": "F", "text": goal, "source": "test", "confidence": 1, "recorded_at": "2026-10-08"})
        result.add_artifact({"artifact_id": "AR", "node_id": "B", "artifact_type": "report", "path": goal, "description": goal})
        result.add_metric({"metric_id": "M", "node_id": "B", "name": "quality", "value": 1, "unit": "score"})
    return result


def test_snapshot_removal_does_not_resurrect_and_retains_audit(tmp_path: Path) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.save_task_graph("one", graph())
    store.add_message("B", "error", "failure history", run_id="one")
    store.save_task_graph("one", graph(full=False))
    restored = store.load_task_graph("one")
    assert restored is not None
    assert list(restored.nodes.keys()) == ["A"]
    assert not restored.edges and not restored.facts and not restored.artifacts and not restored.metrics
    assert store.messages("one")[0]["content"] == "failure history"
    assert SQLiteTaskGraphStore(store.db_path).load_task_graph("one").to_dict() == restored.to_dict()  # type: ignore[union-attr]


def test_same_ids_are_isolated_in_all_graph_record_types(tmp_path: Path) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.save_task_graph("one", graph("first"))
    store.save_task_graph("two", graph("second"))
    for run, goal in [("one", "first"), ("two", "second")]:
        restored = store.load_task_graph(run)
        assert restored is not None
        assert restored.nodes["A"].goal == goal
        assert restored.facts["F"].text == goal
        assert restored.artifacts["AR"].path == goal
        assert restored.metrics["M"].value == 1
        assert len(store.facts(run)) == 1
        assert len(store.artifacts(run)) == 1
        assert store.counts(run)["nodes"] == 2
    with pytest.raises(ValueError, match="ambiguous"):
        store.get_node("A")
    with pytest.raises(ValueError, match="ambiguous"):
        store.facts()
    store.save_task_graph("two", graph("second", full=False))
    assert len(store.load_task_graph("one").nodes) == 2  # type: ignore[union-attr]


def fence(tmp_path: Path, sequence: int, revision: int = 1) -> dict[str, Any]:
    db = tmp_path / "owners.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, owner_id TEXT, generation INTEGER,status TEXT,lease_expires_at TEXT,updated_at TEXT)")
        conn.execute("INSERT OR IGNORE INTO runs VALUES('owner-run','owner',1,'running','2099-01-01','2026-01-01')")
    return {"runs_db_path": str(db), "run_id": "owner-run", "owner_id": "owner", "generation": 1,
            "now_iso": "2026-10-08", "commit_seq": sequence, "graph_revision": revision,
            "cursor_json": json.dumps({"iteration": sequence})}


@pytest.mark.parametrize("sequence,revision", [(1, 2), (2, 2), (3, 1)])
def test_fence_rejects_late_equal_sequence_or_revision_rollback(tmp_path: Path, sequence: int,
                                                              revision: int) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    assert store.save_task_graph("one", graph("new"), fence=fence(tmp_path, 2, 2))
    assert not store.save_task_graph("one", graph("stale", full=False), fence=fence(tmp_path, sequence, revision))
    assert store.read_commit_fence("one")["commit_seq"] == 2  # type: ignore[index]
    assert store.load_task_graph("one").nodes["A"].goal == "new"  # type: ignore[union-attr]
    assert len(store.load_task_graph("one").nodes) == 2  # type: ignore[union-attr]


def test_metrics_roundtrip_including_run_level_and_guarded_updates(tmp_path: Path) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    original = graph()
    original.add_metric({"metric_id": "TOTAL", "node_id": None, "name": "total", "value": 2, "unit": "count"})
    store.save_task_graph("one", original)
    assert store.load_task_graph("one").metrics["TOTAL"].node_id is None  # type: ignore[union-attr]
    with store._connect() as conn:
        before = conn.total_changes
        for node in original.nodes.values():
            store.add_node({**node.__dict__}, run_id="one", conn=conn)
        for artifact in original.artifacts.values():
            store.add_artifact(artifact, run_id="one", conn=conn)
        assert conn.total_changes == before


def test_load_uses_one_read_snapshot_while_writer_commits(tmp_path: Path, monkeypatch: Any) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    store.save_task_graph("one", graph())
    start = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []
    def write() -> None:
        try:
            assert start.wait(3)
            store.save_task_graph("one", graph("new", full=False))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    writer = threading.Thread(target=write, name="snapshot-writer")
    writer.start()
    connect = store._connect
    def reader_connect(**kwargs: Any) -> sqlite3.Connection:
        conn = connect(**kwargs)
        if threading.current_thread() is threading.main_thread():
            def interleave(sql: str) -> None:
                if sql.startswith("SELECT * FROM edges"):
                    assert conn.in_transaction
                    start.set()
                    assert finished.wait(3)
            conn.set_trace_callback(interleave)
        return conn
    monkeypatch.setattr(store, "_connect", reader_connect)
    restored = store.load_task_graph("one")
    writer.join(3)
    assert not errors and not writer.is_alive()
    assert restored is not None
    assert restored.nodes["A"].goal == "old" and len(restored.edges) == 1
    assert "AR" in restored.artifacts and "F" in restored.facts


def test_fk_orphans_null_edge_duplicates_and_status_timestamp(tmp_path: Path) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.save_task_graph("one", graph(full=False))
    with store._connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO messages(node_id,run_id) VALUES('missing','one')")
    store.add_edge("A", "A", "communication")
    store.add_edge("A", "A", "communication")
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM edges WHERE run_id='one'").fetchone()[0] == 1
    store.update_run_status("one", "completed")
    store.update_run_status("one", "running")
    assert store.list_runs()[0]["ended_at"] is None


def test_corrupt_cursor_and_ambiguous_identity_do_not_fall_back(tmp_path: Path) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.save_task_graph("one", graph(), fence=fence(tmp_path, 1))
    with store._connect() as conn:
        conn.execute("UPDATE graph_commit_fence SET cursor_json='broken'")
    with pytest.raises(json.JSONDecodeError):
        store.read_commit_fence("one")
    with store._connect() as conn:
        conn.execute("UPDATE graph_commit_fence SET cursor_json='{}'")
        conn.execute("INSERT INTO graph_commit_fence VALUES('two',2,1,1,'later','{}')")
    with pytest.raises(ValueError, match="multiple fence"):
        store.latest_commit_fence()
    assert store.latest_commit_fence("two")["generation"] == 2  # type: ignore[index]


def test_audit_scoping_readonly_queries_and_json_corruption(tmp_path: Path) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    for run in ["one", "two"]:
        store.save_task_graph(run, graph(run))
        store.add_message("A", "test", run, run_id=run)
        store.add_fault_event("same", "error", "A", {"run": run}, run_id=run)
        store.add_recovery_plan("same", "repair", "pending", [], [], run_id=run)
        store.add_recovery_evaluation("same", "failed", 0, 0, 0, run_id=run)
        store.record_resource_decision("A", "coder", "local", 0, run, run_id=run)
        store.add_constraint_version({"constraint_id": "C", "text": run, "version": 1}, run_id=run)
        assert store.messages(run)[0]["content"] == run
        assert store.fault_events(run)[0]["payload"] == {"run": run}
        assert len(store.recovery_plans(run)) == len(store.recovery_evaluations(run)) == 1
        assert store.resource_decisions(run)[0]["reason"] == run
        assert store.constraint_versions(run)[0]["text"] == run
    with store._connect() as conn:
        conn.execute("UPDATE artifacts SET metadata='bad-json' WHERE run_id='one'")
    with pytest.raises(json.JSONDecodeError):
        store.artifacts("one")
    assert len(store.artifacts("two")) == 1


def test_legacy_migration_retains_rows_and_is_idempotent(tmp_path: Path) -> None:
    seed = SQLiteTaskGraphStore(tmp_path / "seed.db")
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(seed.db_path) as source, sqlite3.connect(legacy) as target:
        definitions = source.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND (name='runs' OR name LIKE 'legacy_v1_%')").fetchall()
        for name, sql in definitions:
            target.execute(sql.replace("legacy_v1_", ""))
        target.execute("INSERT INTO runs(run_id,status) VALUES('one','running')")
        target.execute("INSERT INTO nodes(node_id,run_id,goal,role,status,dependencies,constraints,inputs,outputs,acceptance,metadata) VALUES('A','one','old','coder','ready','[]','[]','[]','[]','[]','{}')")
        target.execute("INSERT INTO facts VALUES('F','original','test',1,'time','{}')")
        target.execute("INSERT INTO messages(node_id,content) VALUES('missing','orphan history')")
    store = SQLiteTaskGraphStore(legacy)
    assert store.load_task_graph("one").facts["F"].text == "original"  # type: ignore[union-attr]
    with store._connect() as conn:
        assert conn.execute("SELECT content FROM legacy_v1_messages").fetchone()[0] == "orphan history"
        assert not conn.execute("SELECT * FROM messages").fetchall()
        assert not conn.execute("PRAGMA foreign_key_check(nodes)").fetchall()
    assert SQLiteTaskGraphStore(legacy).load_task_graph("one").nodes["A"].goal == "old"  # type: ignore[union-attr]


def test_read_interfaces_never_issue_ddl(tmp_path: Path, monkeypatch: Any) -> None:
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.save_task_graph("one", graph())
    connect = store._connect
    statements: list[str] = []
    def readonly(**kwargs: Any) -> sqlite3.Connection:
        conn = connect(**kwargs)
        conn.execute("PRAGMA query_only=ON")
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(store, "_connect", readonly)
    assert store.resource_decisions("one") == []
    assert store.constraint_versions("one") == []
    assert store.load_task_graph("one") is not None
    assert all(not sql.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE")) for sql in statements)


def test_memory_and_orchestrator_use_explicit_run_binding(tmp_path: Path) -> None:
    from AstralNexus.core.memory_federation import MemoryFederation
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import Fact
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.save_task_graph("one", graph("first"))
    store.save_task_graph("two", graph("second"))
    memory = MemoryFederation(store, run_id="two")
    assert memory.get_all_facts()[0].text == "second"
    memory.add_fact(Fact("EXTRA", "bound", "test", 1, "time"))
    assert {fact.fact_id for fact in store.facts("two")} == {"F", "EXTRA"}
    assert {fact.fact_id for fact in store.facts("one")} == {"F"}
    with Orchestrator(tmp_path / "runtime", run_id="two", graph_store=store) as runtime:
        assert runtime.memory_federation.run_id == "two"
        assert {fact.text for fact in runtime.memory_federation.get_all_facts()} == {"second", "bound"}


def test_unbound_fact_is_not_imported_into_new_run(tmp_path: Path) -> None:
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import Fact
    store = SQLiteTaskGraphStore(tmp_path / "graph.db")
    store.add_fact(Fact("LEGACY", "unattributed", "old", 1, "time"))
    with Orchestrator(tmp_path / "runtime", run_id="new", graph_store=store) as runtime:
        assert not runtime.memory_federation.get_all_facts()
    assert store.facts("")[0].text == "unattributed"


def test_node_persistence_excludes_runtime_diagnostics_and_rejects_unknown_schema(tmp_path):
    from AstralNexus.core.task_graph import TaskNode
    store = SQLiteTaskGraphStore(tmp_path / 'graph.sqlite')
    store.create_run('run', 'task', [])
    node = TaskNode('N', 'task', 'coder', owned_paths=[], metadata={'owned_paths': ['old.py']})
    store.add_node(dict(node.__dict__), run_id='run')
    assert store.get_node('N', run_id='run').owned_paths == []
    with pytest.raises(ValueError, match='unknown persistent node fields'):
        store.add_node(dict(node.__dict__) | {'invented_column': 'not persisted'}, run_id='run')
