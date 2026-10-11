"""Cross-project acceptance vectors adapted to AstralNexus's contract.

The scenarios are derived from DeerFlow's MIT-licensed acceptance regression
suite.  They intentionally exercise AstralNexus public observations rather
than importing DeerFlow implementation details.
"""
from __future__ import annotations

import json

import pytest

import AstralNexus.runtime.acceptance_checks as acceptance
from AstralNexus.runtime.acceptance_checks import run_acceptance_checks


def _receipt(
    command: str,
    *,
    returncode: int = 0,
    status: str = "success",
    stdout: str = "3 passed in 0.10s",
    stderr: str = "",
) -> dict:
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "receipt_id": "ported-receipt",
        "arguments": json.dumps({"command": command}),
    }


def _test_observation(command: str, receipt: dict) -> dict:
    return run_acceptance_checks(
        [f"tests_passed:{command}"], receipts=[receipt]
    )[0]


@pytest.mark.parametrize(
    "recorded_command",
    [
        'pytest tests || echo "3 passed"',
        'pytest tests | echo "3 passed"',
        'pytest tests; echo "3 passed"',
        'pytest tests & echo "3 passed"',
        'false && pytest tests',
    ],
)
def test_shell_control_flow_cannot_launder_a_passing_summary(recorded_command):
    """A wrapper's zero exit or forged tail cannot prove the test ran cleanly."""
    observation = _test_observation("pytest tests", _receipt(recorded_command))

    assert observation["holds"] is not True
    assert observation["reason"] == "TEST_COMMAND_MATCH_UNVERIFIED"


@pytest.mark.parametrize(
    ("stdout", "expected_reason"),
    [
        ("0 passed in 0.01s", "TEST_SUMMARY_ZERO_TESTS"),
        ("no tests ran in 0.01s", "TEST_SUMMARY_ZERO_TESTS"),
        ("Ran 0 tests in 0.000s\n\nOK", "TEST_SUMMARY_ZERO_TESTS"),
        ('{"passed": 0, "failed": 0}', "TEST_SUMMARY_ZERO_TESTS"),
        ("collection completed", "TEST_SUMMARY_NOT_RECOGNIZED"),
    ],
)
def test_zero_or_missing_test_execution_never_counts_as_success(stdout, expected_reason):
    observation = _test_observation("pytest tests", _receipt("pytest tests", stdout=stdout))

    assert observation["holds"] is not True
    assert observation["reason"] == expected_reason


@pytest.mark.parametrize(
    "stdout",
    [
        "3 passed in 0.10s",
        "Ran 3 tests in 0.010s\n\nOK",
        '{"passed": 3, "failed": 0}',
    ],
)
def test_supported_test_summaries_remain_valid(stdout):
    observation = _test_observation("pytest tests", _receipt("pytest tests", stdout=stdout))

    assert observation["checked"] is True
    assert observation["holds"] is True


def test_failure_summary_still_wins_when_a_shell_wrapper_masks_the_exit_code():
    observation = _test_observation(
        "pytest tests",
        _receipt("pytest tests || true", stdout="1 failed, 2 passed in 0.10s"),
    )

    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["reason"] == "TESTS_REPORTED_FAILURES"


def test_large_binary_delivery_is_decided_from_size_without_text_decoding(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(acceptance, "FILE_CONTENT_READ_CAP_BYTES", 8)
    payload = b"\x00\xff%PDF" * 16
    (tmp_path / "report.pdf").write_bytes(payload)

    observations = run_acceptance_checks(
        ["file:report.pdf exists", "file:report.pdf non-empty"],
        receipts=[],
        root=tmp_path,
    )

    assert [(item["checked"], item["holds"]) for item in observations] == [
        (True, True),
        (True, True),
    ]
    assert "above read cap" in observations[1]["detail"]


def test_reader_exception_degrades_to_unverified_instead_of_crashing():
    def broken_reader(_root, _relative):
        raise PermissionError("sandbox denied")

    observation = run_acceptance_checks(
        ["file:report.md exists"], receipts=[], reader=broken_reader
    )[0]

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "CHECK_INTERNAL_ERROR"

