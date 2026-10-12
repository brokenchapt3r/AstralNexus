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


def test_delivery_tests_failure_blocks_coder(tmp_path, monkeypatch):
    tests_dir = tmp_path / "deliverables" / "app" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_http_contract.py").write_text(
        "def test_missing_permission():\n    assert False, 'permission check missing'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRALNEXUS_CODER_AUTO_TEST", "true")
    monkeypatch.setenv("ASTRALNEXUS_CODER_RUN_MYPY", "false")
    monkeypatch.delenv("ASTRALNEXUS_CODER_TEST_TARGET", raising=False)
    results = BaseAgent._run_coder_validation(
        run_root=tmp_path, artifact_paths=[], rules="",
    )
    assert results[0]["target"] == "deliverables/app/tests"
    assert results[0]["result"]["returncode"] != 0
    assert len(BaseAgent._validation_failures(results)) == 1


def test_required_mypy_unavailable_is_a_failed_gate(tmp_path, monkeypatch):
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("ASTRALNEXUS_CODER_RUN_MYPY", "true")
    monkeypatch.setattr("AstralNexus.agents.base.importlib.util.find_spec", lambda _: None)
    results = BaseAgent._run_coder_validation(
        run_root=tmp_path, artifact_paths=[str(source)], rules="",
    )
    failures = BaseAgent._validation_failures(results)
    assert len(failures) == 1
    assert failures[0]["check"] == "mypy"
    assert failures[0]["result"]["returncode"] == 1


def test_expected_artifact_glob_checks_real_files(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    assert not BaseAgent._expected_artifacts_materialized(tmp_path, ["reports/*.json"])
    (reports / "result.json").write_text("{}", encoding="utf-8")
    assert BaseAgent._expected_artifacts_materialized(tmp_path, ["reports/*.json"])



def test_missing_third_party_mypy_stubs_remain_failed_validation():
    results = [
        {
            "check": "mypy",
            "target": "app.py",
            "result": {
                "status": "failed",
                "returncode": 1,
                "stdout": (
                    'app.py:1: error: Library stubs not installed for "pandas" '
                    '[import-untyped]\nFound 1 error in 1 file\n'
                ),
                "stderr": "",
            },
        }
    ]

    assert BaseAgent._validation_failures(results) == results
    assert results[0]["result"]["failure_class"] == "optional_dependency_unavailable"
    assert BaseAgent._validation_failures(results) == results


def test_real_mypy_type_error_still_triggers_code_repair():
    results = [
        {
            "check": "mypy",
            "target": "app.py",
            "result": {
                "status": "failed",
                "returncode": 1,
                "stdout": (
                    'app.py:8: error: Incompatible return value type '
                    '(got "str", expected "int") [return-value]\n'
                ),
                "stderr": "",
            },
        }
    ]

    assert BaseAgent._validation_failures(results) == results


def test_mypy_checks_later_artifacts_not_only_first_file(tmp_path, monkeypatch):
    first = tmp_path / 'first.py'
    second = tmp_path / 'second.py'
    first.write_text('value: int = 1\n', encoding='utf-8')
    second.write_text('value: int = "wrong"\n', encoding='utf-8')
    monkeypatch.setenv('ASTRALNEXUS_CODER_RUN_MYPY', 'true')
    monkeypatch.setenv('ASTRALNEXUS_CODER_AUTO_TEST', 'false')
    monkeypatch.delenv('ASTRALNEXUS_CODER_MYPY_TARGET', raising=False)
    results = BaseAgent._run_coder_validation(
        run_root=tmp_path, artifact_paths=[str(first), str(second)], rules='',
    )
    failures = BaseAgent._validation_failures(results)
    assert len(failures) == 1
    assert failures[0]['check'] == 'mypy'
    assert 'second.py' in failures[0]['result']['stdout']
    assert failures[0]['result']['returncode'] != 0


def test_runtime_prompt_preserves_requirements_and_upstream_handoff(tmp_path):
    from AstralNexus.agents.coder import Coder
    context = AgentContext(node_id='consumer', goal='implement', metadata={
        'run_dir': str(tmp_path), 'source_requirements': ['no backward transition'],
        'acceptance_criteria': ['reject closed to open'],
        'original_user_goal': 'use server session identity',
        'global_hard_constraints': ['local data only'],
        'upstream_contracts': [{'node_id': 'design', 'goal': 'define API'}],
        'upstream_artifacts': [{'path': 'docs/api.md', 'artifact_id': 'contract'}],
    })
    prompt = Coder()._build_runtime_prompt(context, 'implementation')
    for text in ['no backward transition', 'reject closed to open', 'use server session identity',
                 'local data only', 'define API', 'docs/api.md']:
        assert text in prompt


def test_resumed_output_existence_does_not_require_rewriting(tmp_path):
    import os
    source = tmp_path / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(source, (1, 1))
    assert BaseAgent._expected_artifacts_materialized(tmp_path, ["app.py"])
    # Existing bytes do not exempt a resumed worker from deterministic checks.
    source.write_text("def broken(\n", encoding="utf-8")
    results = BaseAgent._run_coder_validation(run_root=tmp_path, artifact_paths=[str(source)], rules="")
    assert BaseAgent._validation_failures(results)


def test_worker_prompt_does_not_force_regeneration_or_stack_switch(tmp_path):
    from AstralNexus.agents.coder import Coder
    prompt = Coder()._build_runtime_prompt(AgentContext(metadata={"run_dir": str(tmp_path)}), "implementation")
    assert "续跑不要求重写" in prompt
    assert "应立即改用 Python 标准库" not in prompt
    assert "必须对每条路径调用 file_write" not in prompt


def test_worker_prompt_preserves_existing_receipts_and_failure_evidence(tmp_path):
    from AstralNexus.agents.coder import Coder
    prompt = Coder()._build_runtime_prompt(
        AgentContext(metadata={"run_dir": str(tmp_path)}), "implementation",
    )
    assert "属于不可变历史" in prompt
    assert "不得删除、清空或覆盖" in prompt
    assert "新的版本化路径" in prompt


def test_runtime_prompt_injects_previous_failure_anti_loop_block(tmp_path):
    """Live r11 resume #10: three identical no-progress deaths proved a
    re-armed worker with no visibility into its predecessor's failure
    receipt repeats the same dead loop (probe dead port, read old logs).
    The renderer must surface the receipt plus change-of-approach and
    service-ownership directives."""
    from AstralNexus.agents.coder import Coder
    context = AgentContext(node_id='takeover', goal='recover backend', metadata={
        'run_dir': str(tmp_path),
        'previous_attempt_failure': {
            'message': 'runtime_no_progress: repeated action/evidence cycle after replanning feedback',
            'error_type': 'LLMClientError',
            'execution_path': 'partial_artifacts_retained',
        },
    })
    prompt = Coder()._build_runtime_prompt(context, 'implementation')
    assert '上一次尝试失败回执' in prompt
    assert 'runtime_no_progress' in prompt
    assert '反循环强制指令' in prompt
    assert 'background=true' in prompt
    assert '由你负责启动' in prompt


def test_runtime_prompt_omits_rework_block_on_fresh_dispatch(tmp_path):
    from AstralNexus.agents.coder import Coder
    prompt = Coder()._build_runtime_prompt(
        AgentContext(node_id='fresh', goal='implement', metadata={'run_dir': str(tmp_path)}),
        'implementation')
    assert '上一次尝试失败回执' not in prompt
    assert '反循环强制指令' not in prompt


def test_runtime_prompt_rework_block_requires_premise_revalidation(tmp_path):
    """Fix 16 (live r11 resume #26): five SERVICE-LAUNCH generations died
    cycle_length=1 instant kills because the goal premise ('repair service
    launch failure') was falsified by the environment — the service was
    already up and answering. Workers re-probed the healthy endpoint until
    the no-progress guard fired, and exec-recovery successors inherited the
    same falsified premise verbatim until the run blocked on 'same
    autonomous recovery outcome repeated three times'. The rework block must
    route premise-falsified re-dispatches to fresh-evidence submission."""
    from AstralNexus.agents.tester import Tester
    context = AgentContext(
        node_id='svc-launch-recovery',
        goal='Repair service launch failure and produce /health evidence',
        metadata={
            'run_dir': str(tmp_path),
            'previous_attempt_failure': {
                'message': 'runtime_no_progress: repeated action/evidence cycle after replanning feedback',
                'error_type': 'LLMClientError',
                'execution_path': 'partial_artifacts_retained',
            },
        },
    )
    prompt = Tester()._build_runtime_prompt(context, 'verification')
    assert '前提再验证' in prompt
    assert '前提已失效' in prompt
    assert '合法完成路径' in prompt
    # the falsified-premise branch must not cancel the start-it-yourself branch
    assert '由你负责启动' in prompt
    # Fix 21 (live r11 resume #32): boundary testers probing a backend that
    # deterministically answered 500 were killed by the no-progress guard
    # BEFORE documenting the noncompliance; the rework block must offer the
    # document-and-submit exit for deterministic noncompliance too.
    assert '确定性不合规' in prompt
    assert '上游责任人' in prompt


def test_progress_stalled_feedback_offers_noncompliance_documentation_exit():
    """Fix 21: the first no-progress feedback message (injected mid-run,
    before the handoff kill) must explicitly tell the worker that
    deterministic unexpected results are EVIDENCE to write down and submit,
    not a loop to retry — otherwise the guard kills the messenger and the
    real backend defect never reaches upstream repair."""
    from AstralNexus.runtime.llm_client import PROGRESS_STALLED_FEEDBACK
    assert '确定性不合规' in PROGRESS_STALLED_FEEDBACK
    assert '500' in PROGRESS_STALLED_FEEDBACK
    assert '回执' in PROGRESS_STALLED_FEEDBACK
    assert '合法完成路径' in PROGRESS_STALLED_FEEDBACK
    assert '无进展强杀' in PROGRESS_STALLED_FEEDBACK


def test_validation_repair_block_reason_guards_exhausted_budget():
    """P5#13 (r15 N-105 exec-22): an exhausted execution budget must block
    coder validation repair rounds immediately — with tools withdrawn the
    model cannot repair, fingerprints drift, and the unlimited validation
    loop spun 697 rounds / 7.7h in the live run."""
    from AstralNexus.agents.base import validation_repair_block_reason
    from AstralNexus.runtime.task_execution_budget import TaskExecutionBudget

    # No budget at all: repair rounds are unconstrained (0 = unlimited).
    assert validation_repair_block_reason(None) is None

    # Fresh budget: real tool capacity remains.
    fresh = TaskExecutionBudget(max_tool_calls=160, max_seconds=3000)
    assert validation_repair_block_reason(fresh) is None

    # Tool-call budget spent: repairs could never execute a tool.
    spent = TaskExecutionBudget(max_tool_calls=2)
    assert spent.admit_tool_call()[0]
    assert spent.admit_tool_call()[0]
    assert not spent.admit_tool_call()[0]
    assert validation_repair_block_reason(spent) == "max_tool_calls"

    # Wall-clock budget spent (injectable clock; no sleeping).
    ticks = iter([0.0, 4000.0])
    timed = TaskExecutionBudget(
        max_tool_calls=160, max_seconds=3000, clock=lambda: next(ticks)
    )
    assert validation_repair_block_reason(timed) == "max_seconds"

    # A broken budget object must never crash the validation loop.
    class _Broken:
        def exhaustion_reason(self):
            raise RuntimeError("boom")

    assert validation_repair_block_reason(_Broken()) is None
