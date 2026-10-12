"""Fix P5#14 (r17 live, 2026-10-02): pytest_run receipts are anchorable.

Field anatomy (r17 R-TESTS-REPAIR, criterion
``tests_passed:python -m pytest deliverables/app/tests -q``): the worker ran
pytest only through ``pytest_run`` (receipt arguments ``{"target": ...}`` —
no command key) and a ``python_exec`` subprocess blob.  ``_receipt_command``
extracted nothing from pytest_run and the code blob degraded to ambiguous,
so the observation fell to TEST_COMMAND_MATCH_UNVERIFIED even though a real
rc=4 receipt existed.  The fix reconstructs the tool's fixed argv template
(``python -m pytest {target} -q``) for whitespace-free targets — a lossless,
deterministic mapping — and refuses to guess for mangled targets (whitespace
inside ``target`` is passed as ONE argv token by ``builtin_tools._pytest_run``
and cannot round-trip through normalization).
"""
from __future__ import annotations

import json

from AstralNexus.runtime.acceptance_checks import run_acceptance_checks

CRITERION = "tests_passed:python -m pytest tests/app"


def _pytest_receipt(target, *, returncode=0, status="success", stdout_tail="", files=None):
    return {
        "tool": "pytest_run",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": files or {},
        "receipt_id": "rc-pytest-" + str(target)[:12].replace(" ", ""),
        "arguments": json.dumps({"target": target, "cwd": ".", "timeout": 900}),
    }


def _check(receipts, criterion=CRITERION):
    return run_acceptance_checks([criterion], receipts=receipts)[0]


def test_clean_pytest_run_target_anchors_a_pass():
    observation = _check([
        _pytest_receipt("tests/app", stdout_tail="\n5 passed in 1.10s\n"),
    ])
    assert observation["checked"] is True
    assert observation["holds"] is True
    assert observation["reason"] == ""


def test_clean_pytest_run_target_anchors_a_decided_failure():
    """rc!=0 anchors as TEST_EXECUTION_FAILED (status gate precedes summary);
    a green exit with a fail-shaped summary is TESTS_REPORTED_FAILURES."""
    observation = _check([
        _pytest_receipt(
            "tests/app", returncode=1, status="failed",
            stdout_tail="\n1 failed, 4 passed in 1.10s\n",
        ),
    ])
    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["reason"] == "TEST_EXECUTION_FAILED"

    reported = _check([
        _pytest_receipt(
            "tests/app", returncode=0, status="success",
            stdout_tail="\n1 failed, 4 passed in 1.10s\n",
        ),
    ])
    assert reported["checked"] is True
    assert reported["holds"] is False
    assert reported["reason"] == "TESTS_REPORTED_FAILURES"


def test_clean_pytest_run_zero_tests_is_a_decided_failure():
    observation = _check([
        _pytest_receipt(
            "tests/app", stdout_tail="\ncollected 0 items\n\n0 passed in 0.01s\n",
        ),
    ])
    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["reason"] == "TEST_SUMMARY_ZERO_TESTS"


def test_mangled_target_with_whitespace_is_never_guessed():
    """r17 field replica: the worker stuffed the flag into the target
    (``"tests/app -q"`` becomes ONE argv token, pytest rc=4 "no tests ran").
    The reconstruction refuses whitespace targets, so the receipt cannot
    anchor — honest NOT_FOUND instead of a conflated token match."""
    observation = _check([
        _pytest_receipt(
            "tests/app -q", returncode=4, status="failed",
            stdout_tail="\nno tests ran in 0.00s\n",
        ),
    ])
    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "TEST_EXECUTION_NOT_FOUND"


def test_r17_field_replica_stays_unverified_with_python_exec_blob():
    """The full r17 shape: mangled pytest_run + a python_exec subprocess blob
    with unbalanced quotes.  The blob keeps the observation ambiguous, so the
    result stays TEST_COMMAND_MATCH_UNVERIFIED — no false green, no false
    decided fail beyond what the receipts prove."""
    blob = {
        "tool": "python_exec",
        "status": "failed",
        "returncode": None,
        "stdout_tail": "",
        "files": {},
        "receipt_id": "rc-blob",
        "arguments": json.dumps({"code": "import subprocess\nsubprocess.run(['python', '-m', 'pytest', 'tests/app', '-q'])\nprint('it\\'s done')"}),
    }
    observation = _check([
        _pytest_receipt(
            "tests/app -q", returncode=4, status="failed",
            stdout_tail="\nno tests ran in 0.00s\n",
        ),
        blob,
    ])
    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "TEST_COMMAND_MATCH_UNVERIFIED"


def test_stale_files_after_anchored_pytest_run_fail_closed():
    """Anchored green run whose recorded file digests no longer match the
    latest snapshot is TEST_EXECUTION_STALE, exactly like terminal_exec."""
    anchored = _pytest_receipt(
        "tests/app",
        stdout_tail="\n5 passed in 1.10s\n",
        files={"tests/app/test_x.py": {"sha256": "a" * 64, "bytes": 10}},
    )
    newer = {
        "tool": "file_write",
        "status": "success",
        "returncode": 0,
        "stdout_tail": "",
        "files": {"tests/app/test_x.py": {"sha256": "b" * 64, "bytes": 12}},
        "receipt_id": "rc-write",
        "arguments": json.dumps({"path": "tests/app/test_x.py"}),
    }
    observation = _check([anchored, newer])
    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["reason"] == "TEST_EXECUTION_STALE"
