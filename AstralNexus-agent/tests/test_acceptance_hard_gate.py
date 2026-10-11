"""Acceptance hard gate (doc 17 第一批⑥): 退出码0但无测试成功记录不能通过；
伪造 passed 文本不能替代执行；文件变更后旧成功不能复用。Rejections skip the
gate; contracts without tests_passed criteria keep the former behavior."""
from __future__ import annotations

import json

import pytest

from AstralNexus.runtime.execution_store import ExecutionStore


def _receipt(command, *, returncode=0, status="success", stdout_tail="", files=None):
    return {
        "tool": "terminal_exec", "status": status, "returncode": returncode,
        "stdout_tail": stdout_tail, "files": files or {},
        "arguments": json.dumps({"command": command}),
    }


def _succeeded(tmp_path, criteria, receipts):
    store = ExecutionStore(tmp_path)
    execution = store.begin("T-1", {"owned_paths": [], "acceptance_criteria": criteria})
    for receipt in receipts:
        store.append(execution, receipt)
    store.finish(execution, "succeeded", {"owned_files": {}})
    return store, execution


def test_exit_zero_without_test_summary_cannot_be_accepted(tmp_path):
    store, execution = _succeeded(
        tmp_path, ["tests_passed:pytest tests"],
        [_receipt("pytest tests", stdout_tail="done")],
    )
    with pytest.raises(ValueError, match="TEST_SUMMARY_NOT_RECOGNIZED"):
        store.accept(execution, {"approved": True})


def test_forged_claim_without_execution_cannot_be_accepted(tmp_path):
    store, execution = _succeeded(tmp_path, ["tests_passed:pytest tests"], [])
    with pytest.raises(ValueError, match="TEST_EXECUTION_NOT_FOUND"):
        store.accept(execution, {"approved": True})


def test_anchored_passing_execution_is_accepted(tmp_path):
    store, execution = _succeeded(
        tmp_path, ["tests_passed:pytest tests"],
        [_receipt("pytest tests", stdout_tail="===== 3 passed in 0.5s =====")],
    )
    store.accept(execution, {"approved": True})
    assert store.get(execution)["acceptance"]["approved"] is True


def test_stale_success_cannot_be_accepted(tmp_path):
    store, execution = _succeeded(
        tmp_path, ["tests_passed:pytest tests"],
        [
            _receipt("pytest tests", stdout_tail="3 passed",
                     files={"app.py": {"sha256": "aaa", "bytes": 8}}),
            _receipt("touch app.py", files={"app.py": {"sha256": "bbb", "bytes": 9}}),
        ],
    )
    with pytest.raises(ValueError, match="TEST_EXECUTION_STALE"):
        store.accept(execution, {"approved": True})


def test_rejection_decisions_skip_the_gate(tmp_path):
    store, execution = _succeeded(tmp_path, ["tests_passed:pytest tests"], [])
    store.accept(execution, {"approved": False, "verdict": "repair_required"})
    assert store.get(execution)["acceptance"]["approved"] is False


def test_contracts_without_test_criteria_are_unaffected(tmp_path):
    store, execution = _succeeded(tmp_path, ["结论正确", "file:x.md exists"], [])
    store.accept(execution, {"approved": True})
    assert store.get(execution)["acceptance"]["approved"] is True


def test_unrecognized_summary_rejection_teaches_recognized_vocabulary(tmp_path):
    """Fix P5#17 (r17-resume#3 live): two consecutive 0.98-approved verdicts
    (R-UI-STARTUP-STABILITY, R-SEED-CHECK-READONLY) were force-overridden to
    inconclusive because custom check scripts printed proprietary SUMMARY
    formats, and the rejection text never said WHICH vocabularies count — so
    repair workers had to guess.  The rejection message must enumerate the
    recognized summary shapes so the repair context teaches a deterministic
    fix instead of burning recovery cycles toward the no_progress breaker."""
    store, execution = _succeeded(
        tmp_path, ["tests_passed:python checks/custom.py"],
        [_receipt("python checks/custom.py",
                  stdout_tail="SUMMARY rounds=2 ready=2/2 http200=2/2 deterministic_pass=true")],
    )
    with pytest.raises(ValueError) as excinfo:
        store.accept(execution, {"approved": True})
    message = str(excinfo.value)
    assert "TEST_SUMMARY_NOT_RECOGNIZED" in message
    # vocabulary hints propagated from the observation detail
    for hint in ("'<N> passed'", "'OK'", "'PASS'", "'# pass <N>'", "'Tests: ... passed'"):
        assert hint in message
