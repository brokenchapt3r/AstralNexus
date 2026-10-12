"""Fix P5#17/#17b (r17-resume#3 live, 2026-10-03): tests_passed 硬门摘要词表。

实地：R-UI-STARTUP-STABILITY 与其接管节点 R-SEED-CHECK-READONLY 连续两谱系被
``TEST_SUMMARY_NOT_RECOGNIZED`` 强制 inconclusive——自定义 check 脚本 stdout 为
自有格式，拒绝文本却不教可识别词表；恢复合同教出的 JSON 摘要（``"passed": 13,
"failed": 0``）又会被 IGNORECASE ``\\bFAILED\\b`` fail-shape 命中 JSON 键而误判
TESTS_REPORTED_FAILURES，"13 passed, 0 failed" 同理被 "0 failed" 误杀。

#17：拒绝 detail 枚举可识别形状并经硬门 ValueError 传播（教学可确定性修复）。
#17b：零计数失败提及中性化 + key:value 计数摘要成为一等 pass/fail 形状。
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.runtime.acceptance_checks import (
    SUMMARY_EVIDENCE_FORMS,
    _summary_verdict,
    run_acceptance_checks,
)
from AstralNexus.runtime.execution_store import ExecutionStore


def _receipt(command, *, returncode=0, status="success", stdout_tail="",
             stderr_tail="", files=None):
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "files": files or {},
        "receipt_id": "rc-" + command[:10].replace(" ", ""),
        "arguments": json.dumps({"command": command}),
    }


def _obs(criteria, receipts, **kwargs):
    return run_acceptance_checks(criteria, receipts=receipts, **kwargs)


# ---------------------------------------------------------------- #17b shapes

@pytest.mark.parametrize("failure", [
    "FAILED (failures=1)", "FAIL final_check", "# fail 2",
    '"failed": 1',
])
@pytest.mark.parametrize("failure_first", [False, True])
def test_mixed_runner_failure_cannot_be_hidden_by_pytest_pass(failure, failure_first):
    lines = ["21 passed", failure]
    if failure_first:
        lines.reverse()
    receipts = [_receipt("python checks/custom.py", stdout_tail="\n".join(lines))]
    result = _obs(["tests_passed:python checks/custom.py"], receipts)[0]
    assert result["checked"] is True
    assert result["holds"] is False
    assert result["reason"] == "TESTS_REPORTED_FAILURES"


def test_pytest_style_with_zero_failed_tail_is_a_pass():
    """Regression: old digit-first fail shape matched "0 failed" and flipped
    a bona fide "13 passed, 0 failed" summary into TESTS_REPORTED_FAILURES."""
    receipts = [_receipt("python checks/custom.py",
                         stdout_tail="===== 13 passed, 0 failed in 2.10s =====")]
    results = _obs(["tests_passed:python checks/custom.py"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True


def test_json_summary_pass_counts_are_recognized():
    """The shape the 05:15:43 recovery contract teaches: the executed process
    prints its own single-line JSON summary with passed/failed counts."""
    line = json.dumps({
        "command": "python deliverables/app/backend/seed_data.py --check",
        "returncode": 0, "checks": [{"name": "c1", "status": "ok", "observed": 1}],
        "passed": 13, "failed": 0, "generated_at": "2026-10-03T05:30:00Z",
    })
    receipts = [_receipt("python deliverables/app/backend/seed_data.py --check",
                         stdout_tail=f"checking...\n{line}\n")]
    results = _obs(
        ["tests_passed:python deliverables/app/backend/seed_data.py --check"],
        receipts,
    )
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True


def test_bare_key_value_summary_is_recognized():
    receipts = [_receipt("python checks/custom.py",
                         stdout_tail="checks=13 passed=13 failed=0 rc=0")]
    results = _obs(["tests_passed:python checks/custom.py"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True


def test_nonzero_failure_counts_still_decide_fail():
    """#17b neutralizes ONLY zero-count mentions; real failures still fail —
    no acceptance loosening."""
    receipts = [_receipt("python checks/custom.py",
                         stdout_tail='{"passed": 11, "failed": 2}')]
    results = _obs(["tests_passed:python checks/custom.py"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TESTS_REPORTED_FAILURES"


def test_digit_first_nonzero_failure_still_decides_fail():
    receipts = [_receipt("python checks/custom.py", stdout_tail="10 failed")]
    results = _obs(["tests_passed:python checks/custom.py"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TESTS_REPORTED_FAILURES"


def test_non_count_failure_label_is_not_failure_evidence():
    """Fix r29: a check script's empty section banner (``---- failed detail
    ----``) is a label, not failure evidence.  Under the IGNORECASE
    ``\\bFAILED\\b`` shape it flipped a real rc=0 run whose own summary read
    ``{"passed": 36, "failed": 0}`` into TESTS_REPORTED_FAILURES — the exact
    stdout tail of the three R29 anchors (doc 51 §16)."""
    tail = (
        "127.0.0.1 - - [06/Oct/2026 00:02:50] \"GET / HTTP/1.1\" 404 -\r\n"
        "\r\n---- cleanup: 删除冒烟工单 id=9 -> tickets=2 events=2 ----\r\n"
        "---- failed detail ----\r\n"
        "---- notes (not counted) ----\r\n"
        "NOTE | GET_/ (页面根，web/ 由页面层节点负责) | status=404 bytes=76\r\n"
        'SMOKE_SUMMARY {"passed": 36, "failed": 0}\r\n'
    )
    receipts = [_receipt("python server/smoke_check.py --strict", stdout_tail=tail)]
    results = _obs(["tests_passed:python server/smoke_check.py --strict"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True
    assert results[0]["reason"] == ""


def test_real_failure_lines_still_decide_fail_under_that_banner():
    """The label fix must not hide genuine failures printed under it: the
    runner-native upper-case token and every count-bearing shape still decide
    FAIL (no acceptance loosening)."""
    for line in ("FAILED (failures=1)", "1 failed", "# fail 2", '"failed": 3'):
        tail = "---- failed detail ----\n%s\nSMOKE_SUMMARY {\"passed\": 36, \"failed\": 0}" % line
        receipts = [_receipt("python server/smoke_check.py --strict", stdout_tail=tail)]
        results = _obs(["tests_passed:python server/smoke_check.py --strict"], receipts)
        assert results[0]["holds"] is False, line
        assert results[0]["reason"] == "TESTS_REPORTED_FAILURES", line


def test_zero_passed_json_summary_is_not_a_pass():
    """"passed": 0 executes nothing — zero is not a pass (Fix BB doctrine)."""
    receipts = [_receipt("python checks/custom.py",
                         stdout_tail='{"passed": 0, "failed": 0}')]
    results = _obs(["tests_passed:python checks/custom.py"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TEST_SUMMARY_ZERO_TESTS"


# ---------------------------------------------------------------- #17 teaching

def test_unrecognized_summary_detail_teaches_full_vocabulary():
    receipts = [_receipt("python checks/custom.py",
                         stdout_tail="SUMMARY rounds=2 ready=2/2 http200=2/2")]
    results = _obs(["tests_passed:python checks/custom.py"], receipts)
    assert results[0]["reason"] == "TEST_SUMMARY_NOT_RECOGNIZED"
    detail = results[0]["detail"]
    for hint in ("'<N> passed'", "'OK'", "'PASS'", "'# pass <N>'",
                 "'Tests: ... passed'", '"passed": <N>', '"failed": 0'):
        assert hint in detail


def _succeeded(tmp_path, criteria, receipts):
    store = ExecutionStore(tmp_path)
    execution = store.begin("T-1", {"owned_paths": [], "acceptance_criteria": criteria})
    for receipt in receipts:
        store.append(execution, receipt)
    store.finish(execution, "succeeded", {"owned_files": {}})
    return store, execution


def test_hard_gate_accepts_json_summary_execution(tmp_path):
    """End-to-end: the contract-taught JSON stdout passes the hard gate."""
    line = '{"command": "python checks/custom.py", "passed": 7, "failed": 0}'
    store, execution = _succeeded(
        tmp_path, ["tests_passed:python checks/custom.py"],
        [_receipt("python checks/custom.py", stdout_tail=f"done\n{line}")],
    )
    store.accept(execution, {"approved": True})
    assert store.get(execution)["acceptance"]["approved"] is True


def test_taught_vocabulary_is_the_implemented_vocabulary():
    """Every taught form must really be accepted, and be named in the hint.

    ``SUMMARY_EVIDENCE_FORMS`` exists so the requirement is learnable, so a form
    that the table accepts but the hint never names is the defect it prevents;
    a form the hint names but the table rejects is worse -- it teaches a fix
    that does not work.
    """
    samples = [
        ("===== 3 passed in 0.5s =====", "'<N> passed'"),
        ("OK", "'OK'"),
        ("PASS", "'PASS'"),
        ("# pass 4", "'# pass <N>'"),
        ("Tests: 3 passed, 0 failed", "'Tests: ... passed'"),
        ('{"passed": 7, "failed": 0}', "key:value"),
    ]
    for sample, taught in samples:
        assert _summary_verdict(sample) is True, sample
        assert taught in SUMMARY_EVIDENCE_FORMS, taught


def test_summary_requirement_is_taught_where_criteria_are_authored():
    """Live b33case1-rerun1 N-110: an exit-code-only check was authored into a
    criterion the verifier can never accept, and the vocabulary was told to the
    model only AFTER the node failed.  Fix P5#17 gave it to the repair path; the
    authoring path (the Governor's criterion grammar) must render the same
    single-source string.

    The grammar lives inside a very large planner method, so this asserts the
    wiring at source level rather than rendering the whole prompt.
    """
    import ast
    import inspect

    from AstralNexus.core import governor as governor_module

    # Structural, not textual: a mention in the source is not a render.  Parse
    # the module and require the constant to be interpolated at both authoring
    # sites -- the Planner's criterion grammar (an f-string) and the
    # verification-recovery planner's grammar (string concatenation).
    tree = ast.parse(inspect.getsource(governor_module))
    sites = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and any(
                    isinstance(sub, ast.Name) and sub.id == 'SUMMARY_EVIDENCE_FORMS'
                    for sub in ast.walk(part.value)
                ):
                    sites.add('f-string')
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and any(
            isinstance(sub, ast.Name) and sub.id == 'SUMMARY_EVIDENCE_FORMS'
            for sub in (node.left, node.right)
        ):
            sites.add('concatenation')
    assert sites == {'f-string', 'concatenation'}, sites


# ------------------------------------------------- unittest writes to STDERR

UNITTEST_CMD = "python -m unittest discover -s deliverables/tests/api -v"
UNITTEST_OK = ("test_post_success ... ok\r\n\r\n"
               "----------------------------------------------------------------------\r\n"
               "Ran 30 tests in 0.885s\r\n\r\nOK\r\n")


def test_unittest_stderr_summary_is_accepted(tmp_path):
    """Live b33case1-fix3 n-be-01: ``unittest`` reports on stderr.

    The anchored command exited 0 with ``stdout_tail`` EMPTY and
    ``stderr_tail`` ending in ``Ran 30 tests in 0.885s`` / ``OK`` -- and was
    rejected as TEST_SUMMARY_NOT_RECOGNIZED, because the check searched stdout
    only.  The vocabulary names ``'OK' (unittest)``, so the runner was
    acknowledged while its stream was not read.
    """
    store, execution = _succeeded(
        tmp_path, [f"tests_passed:{UNITTEST_CMD}"],
        [_receipt(UNITTEST_CMD, stdout_tail="", stderr_tail=UNITTEST_OK)],
    )
    store.accept(execution, {"approved": True})
    assert store.get(execution)["acceptance"]["approved"] is True


def test_stderr_failure_still_decides_fail(tmp_path):
    """Reading stderr must not turn a reported failure into a pass."""
    store, execution = _succeeded(
        tmp_path, [f"tests_passed:{UNITTEST_CMD}"],
        [_receipt(UNITTEST_CMD, stdout_tail="",
                  stderr_tail="Ran 3 tests in 0.1s\r\n\r\nFAILED (failures=1)\r\n")],
    )
    with pytest.raises(ValueError, match="TESTS_REPORTED_FAILURES"):
        store.accept(execution, {"approved": True})


def test_zero_tests_reported_on_stderr_is_not_a_pass(tmp_path):
    """Fix BB's guard has to cover stderr, or unittest's ``OK`` would pass it."""
    store, execution = _succeeded(
        tmp_path, [f"tests_passed:{UNITTEST_CMD}"],
        [_receipt(UNITTEST_CMD, stdout_tail="",
                  stderr_tail="Ran 0 tests in 0.000s\r\n\r\nOK\r\n")],
    )
    with pytest.raises(ValueError, match="TEST_SUMMARY_ZERO_TESTS"):
        store.accept(execution, {"approved": True})


def test_stderr_failure_cannot_be_hidden_by_stdout_pass(tmp_path):
    """Both streams belong to one execution; a recorded failure vetoes pass."""
    store, execution = _succeeded(
        tmp_path, ["tests_passed:pytest tests"],
        [_receipt("pytest tests", stdout_tail="===== 3 passed in 0.5s =====",
                  stderr_tail="FAILED (failures=9)\r\n")],
    )
    with pytest.raises(ValueError, match="TESTS_REPORTED_FAILURES"):
        store.accept(execution, {"approved": True})
