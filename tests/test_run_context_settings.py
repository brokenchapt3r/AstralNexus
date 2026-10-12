from __future__ import annotations
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from AstralNexus.runtime.run_context import RunSettings, RunContext, get_env, current_run_context
from AstralNexus.runtime.llm_loop_budget import LLMLoopBudget
from AstralNexus.runtime.model_selector import get_model_config
from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.core.run_kernel import RunKernel
from AstralNexus.core.task_graph import TaskGraph


def test_environment_config_and_secret_file_are_captured_without_process_mutation(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text('model: {default: qwen-plus}\n', encoding="utf-8")
    (tmp_path / ".env").write_text('ASTRALNEXUS_API_KEY=file-secret\nASTRALNEXUS_MAX_TOOL_ROUNDS=7\n', encoding="utf-8")
    monkeypatch.delenv("ASTRALNEXUS_API_KEY", raising=False)
    settings = RunSettings.capture(environment={}, config_path=config)
    assert "ASTRALNEXUS_API_KEY" not in os.environ
    config.write_text('model: {default: qwen-max}\n', encoding="utf-8")
    (tmp_path / ".env").write_text('ASTRALNEXUS_API_KEY=changed\n', encoding="utf-8")
    monkeypatch.setenv("ASTRALNEXUS_MODEL", "changed-process-model")
    with RunContext(tmp_path, settings).scope():
        assert get_model_config()["model"] == "qwen-plus"
        assert get_env("ASTRALNEXUS_API_KEY") == "file-secret"
        assert LLMLoopBudget.from_environment(None).max_rounds == 7
    assert current_run_context() is None
    assert get_env("ASTRALNEXUS_MODEL") == "changed-process-model"
    assert "file-secret" not in json.dumps(settings.audit())
    assert "file-secret" not in repr(settings)


def test_capture_precedence_and_readonly_values(tmp_path):
    config = tmp_path / "config.yaml"
    (tmp_path / ".env").write_text('ASTRALNEXUS_API_KEY=file-secret\n', encoding="utf-8")
    settings = RunSettings.capture(environment={"ASTRALNEXUS_API_KEY": "environment-secret"}, config_path=config)
    assert settings.environment["ASTRALNEXUS_API_KEY"] == "environment-secret"
    with pytest.raises(TypeError): settings.environment["ASTRALNEXUS_API_KEY"] = "mutated"


def test_tool_worker_threads_use_their_captured_context_and_approval_scope(tmp_path, monkeypatch):
    from AstralNexus.tools.approval import is_tool_approved
    executors = []
    for model, approval in (("A", "true"), ("B", "false")):
        settings = RunSettings.capture(environment={"ASTRALNEXUS_MODEL": model, "ASTRALNEXUS_TOOL_APPROVAL": approval},
            config_path=tmp_path / model / "config.yaml")
        with RunContext(tmp_path / model, settings).scope():
            executors.append(ToolExecutor())
    def read_context(*args):
        return {"model": get_model_config()["model"], "approved": is_tool_approved("terminal_exec")}
    monkeypatch.setattr(ToolExecutor, "_execute_tool_inner", read_context)
    monkeypatch.setenv("ASTRALNEXUS_MODEL", "later-global-change")
    monkeypatch.setenv("ASTRALNEXUS_TOOL_APPROVAL", "true")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda executor: executor.execute_tool("test", {}), executors))
    assert results == [{"model": "A", "approved": True}, {"model": "B", "approved": False}]
    assert executors[0].workspace_root == (tmp_path / "A").resolve()
    assert executors[0].approval_identity != executors[1].approval_identity


def test_kernel_command_keeps_its_settings_after_global_changes(tmp_path, monkeypatch):
    settings = RunSettings.capture(environment={"ASTRALNEXUS_MAX_TOOL_ROUNDS": "9"}, config_path=tmp_path / "config.yaml")
    class Engine:
        checkpoint_sink = None
        task_graph = TaskGraph()
        def attach_interrupt_control(self, *args): pass
        def shutdown(self): pass
        def run(self, **kwargs): return {"rounds": LLMLoopBudget.from_environment(None).max_rounds}
    with RunKernel(tmp_path, settings=settings, engine_factory=Engine) as kernel:
        monkeypatch.setenv("ASTRALNEXUS_MAX_TOOL_ROUNDS", "999")
        assert kernel.execute(goal="read settings").result(timeout=3)["rounds"] == 9


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "2.5", "not-an-integer"])
def test_invalid_run_settings_fail_before_acquiring_resources(tmp_path, value):
    with pytest.raises(ValueError):
        RunSettings.capture(environment={"ASTRALNEXUS_MAX_TOOL_ROUNDS": value}, config_path=tmp_path / "config.yaml")
    assert not (tmp_path / "runs.sqlite").exists()


def test_active_run_cannot_switch_configuration_files_implicitly(tmp_path):
    settings = RunSettings.capture(environment={}, config_path=tmp_path / "A.yaml")
    with RunContext(tmp_path, settings).scope():
        with pytest.raises(ValueError, match="frozen"):
            get_model_config(config_path=str(tmp_path / "B.yaml"))


def test_compat_model_client_and_orchestrator_limits_use_frozen_settings(tmp_path, monkeypatch):
    from AstralNexus.runtime.model_client import ModelClient
    from AstralNexus.core.orchestrator import Orchestrator
    settings = RunSettings.capture(environment={'ASTRALNEXUS_MODEL': 'frozen-model',
        'ASTRALNEXUS_PROVIDER': 'custom', 'ASTRALNEXUS_MAX_ACTIVE_TASKS': '17',
        'ASTRALNEXUS_MAX_TOTAL_TASKS': '29'}, config_path=tmp_path / 'config.yaml')
    with RunContext(tmp_path, settings).scope():
        monkeypatch.setenv('ASTRALNEXUS_MODEL', 'changed-model')
        monkeypatch.setenv('ASTRALNEXUS_MAX_ACTIVE_TASKS', '999')
        with Orchestrator(tmp_path / 'run') as runtime:
            assert runtime.governor.max_active_tasks == 17
            assert runtime.governor.max_total_tasks == 29
        assert ModelClient().resolve()['effective_model'] == 'frozen-model'
