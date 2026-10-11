from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import Fact
from AstralNexus.core.task_graph import TaskNode
from AstralNexus.runtime.model_selector import get_model_config
from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


def test_sqlite_store_lists_and_updates_runs(tmp_path: Path):
    store = SQLiteTaskGraphStore(tmp_path / "graph.sqlite")
    store.create_run("run-1", "deliver a result", ["keep the API stable"])

    runs = store.list_runs()
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["constraints"] == ["keep the API stable"]

    store.update_run_status("run-1", "completed")
    assert store.list_runs()[0]["status"] == "completed"
    assert store.list_runs()[0]["ended_at"]


def test_sqlite_store_facts_round_trip_and_seed_memory(tmp_path: Path):
    store = SQLiteTaskGraphStore(tmp_path / "graph.sqlite")
    store.create_run("durable-facts", "fact persistence", [])
    store.add_fact(Fact(
        fact_id="fact-1",
        text="SQLite is the durable fact source.",
        source="node-1",
        confidence=0.95,
        recorded_at="2026-07-29T00:00:00+00:00",
        metadata={"kind": "observed"},
    ), run_id="durable-facts")

    with Orchestrator(run_dir=tmp_path / "run", graph_store=store, run_id="durable-facts") as orchestrator:
        facts = orchestrator.memory_federation.get_all_facts()

    assert len(facts) == 1
    assert facts[0].fact_id == "fact-1"
    assert facts[0].metadata == {"kind": "observed"}


def test_explicit_model_config_loads_sibling_env(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  default: qwen3.7-max\n  provider: dashscope\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=fixture-key\n",
        encoding="utf-8",
    )
    for name in (
        "ASTRALNEXUS_API_KEY",
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    config = get_model_config(str(config_path))

    assert config["api_key_env"] == "DASHSCOPE_API_KEY"
    assert config["env_file"] == str(tmp_path / ".env")


def test_tool_pool_can_be_recreated_after_runtime_shutdown():
    first = ToolExecutor._get_shared_pool()
    ToolExecutor.shutdown_global_pool()
    second = ToolExecutor._get_shared_pool()
    try:
        assert first is not second
        assert not ToolExecutor.is_pool_shutdown()
    finally:
        ToolExecutor.shutdown_global_pool()


def test_orchestrator_reads_task_version_from_node_metadata():
    node = TaskNode(
        node_id="node-1",
        goal="produce evidence",
        role="coder",
        metadata={"task_version": "4"},
    )
    assert Orchestrator._node_task_version(node) == 4
    node.metadata["task_version"] = "invalid"
    assert Orchestrator._node_task_version(node) == 1
