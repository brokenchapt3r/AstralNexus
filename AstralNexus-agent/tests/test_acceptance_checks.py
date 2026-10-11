"""Deterministic acceptance leaves (doc 17 第一批) — the required proof set:
被拒工具调用有回执；退出码0但无测试成功记录不能通过；伪造passed文本不能替代
执行；文件变更后旧成功不能复用。（压缩后引用不串号属第二批冻结集证明。）"""
from __future__ import annotations

import json

from AstralNexus.runtime.acceptance_checks import (
    REASONS,
    parse_criterion,
    render_acceptance_observation,
    run_acceptance_checks,
)


def _receipt(command, *, returncode=0, status="success", stdout_tail="", files=None):
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": files or {},
        "receipt_id": "rc-" + command[:10].replace(" ", ""),
        "arguments": json.dumps({"command": command}),
    }


def _obs(criteria, receipts, **kwargs):
    return run_acceptance_checks(criteria, receipts=receipts, **kwargs)


def test_forged_passed_text_without_execution_never_anchors():
    results = _obs(["tests_passed:pytest tests"], [])
    assert results[0]["checked"] is False
    assert results[0]["holds"] is None
    assert results[0]["reason"] == "TEST_EXECUTION_NOT_FOUND"
    assert render_acceptance_observation(results[0]).startswith("UNVERIFIED")


def test_exit_code_zero_without_test_record_does_not_pass():
    receipts = [_receipt("pytest tests", stdout_tail="done")]
    results = _obs(["tests_passed:pytest tests"], receipts)
    assert results[0]["checked"] is False
    assert results[0]["reason"] == "TEST_SUMMARY_NOT_RECOGNIZED"


def test_anchored_passing_execution_holds():
    receipts = [_receipt("pytest tests", stdout_tail="===== 3 passed in 0.42s =====")]
    results = _obs(["tests_passed:pytest tests"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True
    assert results[0]["receipt_ids"]
    assert render_acceptance_observation(results[0]).startswith("HOLDS")


def test_echo_mention_cannot_anchor():
    receipts = [_receipt('echo "pytest tests"', stdout_tail="pytest tests\n3 passed")]
    results = _obs(["tests_passed:pytest tests"], receipts)
    assert results[0]["reason"] == "TEST_EXECUTION_NOT_FOUND"


def test_failed_execution_is_decidable_fail():
    receipts = [_receipt("pytest tests", returncode=1, status="error", stdout_tail="2 failed")]
    results = _obs(["tests_passed:pytest tests"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TEST_EXECUTION_FAILED"


def test_summary_with_failures_under_zero_exit_fails():
    receipts = [_receipt("pytest tests || true", stdout_tail="1 failed, 2 passed")]
    results = _obs(["tests_passed:pytest tests"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TESTS_REPORTED_FAILURES"


def _list_receipt(command_tokens, *, key="command", returncode=0, status="success", stdout_tail="", files=None):
    """Real r15 shape: terminal_exec stores arguments.command as an argv list."""
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": files or {},
        "receipt_id": "rc-list-" + command_tokens[-1],
        "arguments": json.dumps({key: command_tokens, "timeout": 300}),
    }


def test_list_form_command_anchors_passing_execution():
    """Fix P5#12: argv-list receipts must round-trip against criterion text."""
    receipts = [_list_receipt(
        ["python", "-m", "pytest", "deliverables/app/backend", "-q"],
        stdout_tail="23 passed in 3.00s",
    )]
    results = _obs(["tests_passed:python -m pytest deliverables/app/backend -q"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True
    assert results[0]["receipt_ids"]


def test_argv_key_list_form_anchors_passing_execution():
    receipts = [_list_receipt(
        ["python", "-m", "pytest", "tests", "-q"], key="argv", stdout_tail="5 passed in 1.0s",
    )]
    results = _obs(["tests_passed:python -m pytest tests -q"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True


def test_list_form_failed_execution_is_decidable_fail():
    receipts = [_list_receipt(
        ["python", "-m", "pytest", "tests", "-q"],
        returncode=1, status="error", stdout_tail="2 failed, 21 passed in 1.52s",
    )]
    results = _obs(["tests_passed:python -m pytest tests -q"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TEST_EXECUTION_FAILED"


def test_changed_file_invalidates_earlier_success():
    receipts = [_receipt(
        "pytest tests",
        stdout_tail="3 passed",
        files={"app.py": {"sha256": "aaa", "bytes": 10}},
    )]
    latest = {"app.py": {"sha256": "bbb", "bytes": 12}}
    results = _obs(["tests_passed:pytest tests"], receipts, latest_files=latest)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TEST_EXECUTION_STALE"


def test_file_leaves_are_workspace_scoped(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text("# design", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    results = run_acceptance_checks(
        [
            "file:docs/architecture.md exists",
            "file:missing.md exists",
            "file:empty.md non-empty",
            "file_written:docs/architecture.md",
            "file:../../secret.txt exists",
        ],
        receipts=[],
        root=tmp_path,
    )
    assert results[0]["checked"] is True and results[0]["holds"] is True
    assert results[1]["checked"] is True and results[1]["holds"] is False
    assert results[1]["reason"] == "FILE_NOT_FOUND"
    assert results[2]["reason"] == "FILE_EMPTY" and results[2]["holds"] is False
    assert results[3]["kind"] == "file_written" and results[3]["holds"] is True
    assert results[4]["checked"] is False
    assert results[4]["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"


def test_undecidable_criterion_stays_unverified():
    results = _obs(["状态迁移表与权限矩阵必须完整准确"], [])
    assert results[0]["kind"] == "undecidable"
    assert results[0]["checked"] is False
    assert results[0]["reason"] == "UNDECIDABLE_IN_CODE"


def test_criterion_id_pairs_and_default_naming():
    results = run_acceptance_checks(
        [("O-03.C02", "file:docs/architecture.md exists"), "file:x.md exists"],
        receipts=[],
        execution_id="ex-1",
    )
    assert results[0]["criterion_id"] == "O-03.C02"
    assert results[1]["criterion_id"] == "C02"
    assert results[0]["execution_id"] == "ex-1"


def test_reason_vocabulary_is_closed_uppercase():
    for reason in REASONS:
        assert reason == reason.upper()


def test_parse_criterion_kinds():
    assert parse_criterion("file:a/b.md exists")[0] == "file_exists"
    assert parse_criterion("file:a/b.md non-empty")[0] == "file_non_empty"
    assert parse_criterion("file_written:a/b.md")[0] == "file_written"
    assert parse_criterion("tests_passed:pytest -q")[0] == "tests_passed"
    assert parse_criterion("权限矩阵完整")[0] == "undecidable"


def test_denied_call_receipt_carries_structured_outcome(tmp_path):
    """Proof 1 at the ledger seam: a guard-blocked call still yields a
    receipt, stamped with the structured outcome (source=guard)."""
    from AstralNexus.runtime.execution_ledger import ExecutionLedger
    from AstralNexus.runtime.execution_store import ExecutionStore

    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-1", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    ledger.observe(
        {"id": "c1", "function": {"name": "file_write", "arguments": '{"path": "x.py"}'}},
        {
            "status": "error",
            "error": "file_write path is outside the Worker owned_paths contract",
            "failure_class": "write_scope_violation",
        },
    )
    receipts = store.get(execution)["receipts"]
    assert len(receipts) == 1
    outcome = receipts[0]["outcome"]
    assert outcome["source"] == "guard"
    assert outcome["status"] == "error"
    assert outcome["recoverable_by_model"] is False
    assert outcome["recommended_next_action"] == "summarize"
