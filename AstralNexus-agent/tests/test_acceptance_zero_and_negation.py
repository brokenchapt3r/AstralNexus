"""Fix BB (doc 26 §42, 2026-09-18 r13b): DeerFlow 2.0 anti-false-positive rules.

Upstream ``deer-flow@2814bd5d498a subagents/acceptance_checks.py`` refuses to
treat two situations as a pass, and our port did not:

1. **Zero executed tests.** ``re.compile(r"\\d+\\s+passed")`` also matches
   "**0 passed**", so a suite that ran nothing was recorded as HOLDS.  Upstream
   keeps ``_TEST_ZERO_SHAPE_RE`` ("zero is not a pass").  This compounds with
   Fix BA: a zero-test tail could otherwise carry an aborted turn's "usable
   evidence" forward.
2. **Negated targets.** Upstream ``_negation_overlaps`` refuses an anchor when
   the recipe negates the very target the criterion names
   (``pytest tests --ignore tests/x`` vs a criterion about ``tests/x``).

Both are fail-closed: the leaf either becomes a decided FAIL (zero tests) or
falls back to UNVERIFIED (provenance cannot be established) — never a pass.
"""
from __future__ import annotations

import json

from AstralNexus.runtime.acceptance_checks import (
    REASONS,
    run_acceptance_checks,
)

CRITERION = "tests_passed:python -m pytest tests/test_widget.py"


def _receipt(command, *, returncode=0, status="success", stdout_tail=""):
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": {},
        "receipt_id": "rc-" + command[:12].replace(" ", ""),
        "arguments": json.dumps({"command": command}),
    }


def _one(command, stdout_tail, *, criterion=CRITERION):
    return run_acceptance_checks(
        [criterion], receipts=[_receipt(command, stdout_tail=stdout_tail)]
    )[0]


def test_zero_passed_is_a_decided_failure():
    observation = _one(
        "python -m pytest tests/test_widget.py",
        "\ncollected 0 items\n\n0 passed in 0.01s\n",
    )
    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["reason"] == "TEST_SUMMARY_ZERO_TESTS"
    assert observation["reason"] in REASONS


def test_zero_test_phrasings_are_all_vetoed():
    for tail in (
        "[no test files]",
        "[no tests to run]",
        "Ran 0 tests in 0.001s\n\nOK",
        "no tests ran in 0.00s",
    ):
        observation = _one("python -m pytest tests/test_widget.py", tail)
        assert observation["holds"] is False, tail
        assert observation["reason"] == "TEST_SUMMARY_ZERO_TESTS", tail


def test_a_real_pass_still_holds():
    observation = _one(
        "python -m pytest tests/test_widget.py",
        "\n1 passed in 0.42s\n",
    )
    assert observation["checked"] is True
    assert observation["holds"] is True


def test_negated_target_cannot_anchor_a_pass():
    """Scope reduction: the criterion says "the tests directory", the run
    excluded a subpath of it.  Fail-closed, never a pass."""
    observation = _one(
        "python -m pytest tests --ignore tests/test_widget.py",
        "\n9 passed in 1.10s\n",
        criterion="tests_passed:python -m pytest tests",
    )
    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "TEST_COMMAND_MATCH_UNVERIFIED"


def test_structurally_different_command_is_not_an_anchor():
    """A different file argument never matches, negated or not (documented
    pragmatic matcher: structural comparison, no guessing)."""
    observation = _one(
        "python -m pytest tests --ignore tests/test_widget.py",
        "\n9 passed in 1.10s\n",
    )
    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "TEST_EXECUTION_NOT_FOUND"


def test_negation_outside_the_criterion_scope_still_anchors():
    """Negating something the criterion never named is not a scope reduction."""
    observation = _one(
        "python -m pytest tests/test_widget.py --ignore tests/test_slow_network.py",
        "\n1 passed in 0.42s\n",
    )
    assert observation["checked"] is True
    assert observation["holds"] is True


def test_criterion_own_negation_is_not_self_negating():
    criterion = (
        "tests_passed:python -m pytest tests --ignore tests/test_slow_network.py"
    )
    observation = _one(
        "python -m pytest tests --ignore tests/test_slow_network.py",
        "\n9 passed in 1.10s\n",
        criterion=criterion,
    )
    assert observation["holds"] is True