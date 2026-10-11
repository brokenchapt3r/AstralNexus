from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from pathlib import Path

from AstralNexus.cli import commands


def test_report_uses_current_graph_records(tmp_path):
    from AstralNexus.cli.main import _generate_report
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode, Fact, Artifact
    run_dir = tmp_path / "runs" / "report-run"
    with Orchestrator(run_dir, run_id="report-run") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="worker", goal="report current records", role="coder",
            status="completed", metadata={"error": "historical diagnostic"},
        ))
        graph.add_fact(Fact("fact-1", "verified fact", "worker", 1.0, "2026-09-09"))
        graph.add_artifact(Artifact("artifact-1", "worker", "file", "result.json", "result"))
        orchestrator._save_checkpoint()
    output = tmp_path / "report.md"
    assert _generate_report(SimpleNamespace(run_id="report-run", output=str(output)), tmp_path) == 0
    text = output.read_text(encoding="utf-8")
    assert "verified fact" in text and "result.json" in text
    assert "historical diagnostic" in text


def test_inject_fault_finds_run_and_reports_current_worker(tmp_path, monkeypatch):
    from AstralNexus.cli.main import _inject_fault
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    run_dir = tmp_path / "runs" / "fault-run"
    with Orchestrator(run_dir, run_id="fault-run") as orchestrator:
        orchestrator.task_graph.add_node(TaskNode(
            node_id="worker", goal="inject fault", role="coder", status="ready",
        ))
        orchestrator._save_checkpoint()
    monkeypatch.setattr(Orchestrator, "inject_fault", lambda *args: {
        "fault_id": "fault-test", "recovery_success": False,
    })
    assert _inject_fault(SimpleNamespace(run_id="fault-run", type="node_failure", payload=None), tmp_path) == 0
    assert (run_dir / "fault_injection_fault-test.md").is_file()


def test_interactive_task_keeps_user_intent_separate_from_control_metadata(
    monkeypatch, tmp_path: Path,
) -> None:
    from AstralNexus.cli.main import _InteractiveRunController

    controller = _InteractiveRunController(tmp_path)
    captured = {}

    def fake_start(task, *, output=None):
        captured["task"] = Path(task)
        return True

    monkeypatch.setattr(controller, "start", fake_start)
    request = "http://one.example/\nhttp://two.example/\n搜索中标公告"
    assert controller.start_text(
        request,
        history=[{"role": "assistant", "content": "历史回答"}],
        route_decision={"mode": "governed_run"},
    )

    task_path = captured["task"]
    assert task_path.read_text(encoding="utf-8") == request
    metadata = json.loads(
        task_path.with_suffix(".context.json").read_text(encoding="utf-8")
    )
    assert metadata["user_intent"] == request
    assert metadata["conversation_context"] == ["assistant: 历史回答"]
    assert metadata["route_decision"]["mode"] == "governed_run"


def test_command_catalog_is_derived_from_registry() -> None:
    catalog = commands.command_catalog()
    names = [item[0] for item in catalog["pairs"]]

    assert names == [f"/{item.name}" for item in commands.COMMAND_REGISTRY]
    for item in commands.COMMAND_REGISTRY:
        for alias in item.aliases:
            assert catalog["canon"][f"/{alias}"] == f"/{item.name}"
    assert all(
        isinstance(category.get("pairs"), list)
        for category in catalog["categories"]
    )


def test_command_parser_preserves_quoted_windows_paths() -> None:
    path = r"C:\Users\Finitude\Documents\任务 文件.txt"

    assert commands.split_command_line(f'/run "{path}"') == ["/run", path]


def test_command_registry_has_a_real_handler_for_every_command() -> None:
    assert {item.name for item in commands.COMMAND_REGISTRY} == set(
        commands._command_handlers()
    )


def test_command_names_and_aliases_are_globally_unique() -> None:
    owners = {}
    for item in commands.COMMAND_REGISTRY:
        for name in (item.name, *item.aliases):
            assert name not in owners, f"duplicate command/alias: {name}"
            owners[name] = item.name


def test_command_ownership_is_derived_from_registry() -> None:
    assert commands.ARGPARSE_COMMANDS == {
        item.name for item in commands.COMMAND_REGISTRY if item.argparse_owned
    }
    assert commands.SESSION_COMMANDS == {
        item.name for item in commands.COMMAND_REGISTRY if item.session_owned
    }
    assert {"run", "resume", "new", "status", "interrupt", "quit"} <= commands.SESSION_COMMANDS


def test_noninteractive_interrupt_does_not_report_false_success(capsys) -> None:
    assert commands.process_command_args(["interrupt"]) == 1
    assert "只能在" in capsys.readouterr().out


def test_system_cli_uses_registry_for_non_argparse_commands(monkeypatch, tmp_path) -> None:
    def _capture_callback(parts, home=None):
        calls.append((parts, home))
        return 0

    calls: list[tuple[object, object]] = []
    from AstralNexus.cli import main as cli_main

    monkeypatch.setattr(cli_main, "bootstrap_environment", lambda: tmp_path)
    monkeypatch.setattr(
        cli_main,
        "process_command_args",
        _capture_callback,
    )

    assert cli_main.main(["model", "list"]) == 0
    assert calls == [(["model", "list"], tmp_path)]


def test_version_has_one_runtime_source() -> None:
    from AstralNexus import __version__
    from AstralNexus.core.identity import VERSION

    assert __version__ == VERSION


def test_native_tui_is_the_only_interactive_cli_entry() -> None:
    from AstralNexus.cli import main as cli_main
    from AstralNexus.tui_gateway import entry as gateway_entry

    cli_dir = Path(cli_main.__file__).parent
    assert not (cli_dir / "tui.py").exists()
    source = inspect.getsource(cli_main._interactive_loop)
    assert "prompt_toolkit" not in source
    assert "input(" not in source
    assert "launch_native_tui" in source
    assert "subprocess" not in inspect.getsource(gateway_entry)


def test_gateway_routes_slash_commands_through_canonical_handler() -> None:
    from AstralNexus.tui_gateway import entry as gateway_entry

    source = inspect.getsource(gateway_entry.Gateway._execute_command_handler)
    assert "process_command" in source
    assert "_execute_cli_subprocess" not in source


def test_resume_handler_delegates_to_canonical_resume(monkeypatch, tmp_path) -> None:
    def _capture_callback(args, home):
        calls.append((args, home))
        return 0

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "task_graph.sqlite").write_bytes(b"")
    calls: list[tuple[SimpleNamespace, Path]] = []

    monkeypatch.setattr(commands, "_resolve_run_dir", lambda session_id, home: run_dir)

    from AstralNexus.cli import main as cli_main

    monkeypatch.setattr(
        cli_main,
        "_resume_task",
        _capture_callback,
    )

    assert commands._handle_resume(["run-1"], home=tmp_path) == 0
    assert calls
    args, home = calls[0]
    assert args.run_dir == str(run_dir)
    assert args.max_parallel is None
    assert home == tmp_path
