from pathlib import Path

from AstralNexus.agents.base import AgentContext, BaseAgent


def test_project_rules_are_loaded_from_run_parent(tmp_path, monkeypatch):
    project = tmp_path / "project"
    run_dir = project / "run"
    run_dir.mkdir(parents=True)
    (project / "AGENTS.md").write_text("run compile and tests", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rules = BaseAgent._load_project_rules(
        AgentContext(metadata={"run_dir": str(run_dir)})
    )

    assert "run compile and tests" in rules
    assert "AGENTS.md" in rules


def test_project_rules_do_not_leak_from_process_cwd(tmp_path, monkeypatch):
    source_checkout = tmp_path / "source"
    run_dir = tmp_path / "deliveries" / "run"
    source_checkout.mkdir()
    run_dir.mkdir(parents=True)
    (source_checkout / "AGENTS.md").write_text("run mypy on framework", encoding="utf-8")
    monkeypatch.chdir(source_checkout)

    rules = BaseAgent._load_project_rules(
        AgentContext(metadata={"run_dir": str(run_dir)})
    )

    assert "run mypy on framework" not in rules


def test_coder_validation_runs_compile_and_pytest(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    tests_dir = run_dir / "tests"
    tests_dir.mkdir(parents=True)
    source = run_dir / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tests_dir / "test_module.py").write_text(
        "from module import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRALNEXUS_CODER_AUTO_TEST", "true")
    monkeypatch.setenv("ASTRALNEXUS_CODER_RUN_MYPY", "false")

    results = BaseAgent._run_coder_validation(
        run_root=run_dir,
        artifact_paths=[str(source)],
        rules="",
    )

    assert {item["check"] for item in results} == {"python_compile", "pytest"}
    assert BaseAgent._validation_failures(results) == []


def test_validation_failure_is_not_treated_as_success():
    failures = BaseAgent._validation_failures(
        [{"check": "pytest", "result": {"status": "error", "returncode": 1}}]
    )
    assert len(failures) == 1
