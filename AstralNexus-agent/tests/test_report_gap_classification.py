"""Offline contract for ``runtime/report_gap.py`` (m02774 boundary ① / ④).

Pure classification: no workspace, no store, no subprocess.  The end-to-end
inversion test (functional test passes -> report missing -> narrow completion ->
receipts reused without a second execution -> delivery gate) lives separately in
``tests/test_report_completion_reuse.py``.
"""

from __future__ import annotations

from AstralNexus.runtime.report_gap import (
    REPORT_GAP_REASON,
    VETO_ASSERTIONS_UNCHECKED,
    VETO_BUDGET_EXHAUSTED,
    VETO_NON_REPORT_MISSING,
    VETO_NOT_A_REAL_SUCCESS,
    VETO_STALE,
    classify_report_gap,
    decide_report_gap,
)

GREEN_EXECUTION = "8f4da3711904424e80106e326caba933"
REPORT = "deliverables/reports/N-301-summary.md"
PRODUCT = "deliverables/app/product.py"


def test_report_only_gap_is_classified_from_recorded_facts() -> None:
    gap = classify_report_gap(
        missing_paths=[REPORT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=True,
    )
    assert gap is not None, "a green execution missing only its report must be reusable"
    assert gap.reason == REPORT_GAP_REASON
    assert gap.missing_reports == (REPORT,)
    assert gap.execution_id == GREEN_EXECUTION
    assert gap.rerun_functional_tests is False
    assert gap.reuse_execution_evidence is True
    assert gap.as_metadata()["reuse_execution_id"] == GREEN_EXECUTION


def test_budget_exhaustion_keeps_the_primary_reason() -> None:
    """④ N-103: budget_exhausted must never be relabelled as a report gap."""
    decision = decide_report_gap(
        missing_paths=[REPORT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=True,
        budget_exhausted=True,
    )
    assert decision.is_gap is False
    assert decision.veto == VETO_BUDGET_EXHAUSTED


def test_non_report_artifact_missing_vetoes_reuse() -> None:
    """CONTROL: a missing product file is functional work, not a report gap."""
    decision = decide_report_gap(
        missing_paths=[REPORT, PRODUCT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=True,
    )
    assert decision.is_gap is False
    assert decision.veto == VETO_NON_REPORT_MISSING
    assert classify_report_gap(
        missing_paths=[PRODUCT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=True,
    ) is None


def test_stale_evidence_vetoes_reuse() -> None:
    """③ product code / test script change invalidates the functional receipt."""
    decision = decide_report_gap(
        missing_paths=[REPORT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=True,
        stale=True,
    )
    assert decision.is_gap is False
    assert decision.veto == VETO_STALE


def test_worker_claim_without_a_real_success_is_not_evidence() -> None:
    """① a guard-blocked / failed execution must not be papered over."""
    for status, returncode in (("error", None), ("success", 1), ("", None)):
        decision = decide_report_gap(
            missing_paths=[REPORT],
            execution_id=GREEN_EXECUTION,
            execution_status=status,
            returncode=returncode,
            assertions_checked=True,
        )
        assert decision.is_gap is False, (status, returncode)
        assert decision.veto == VETO_NOT_A_REAL_SUCCESS


def test_unchecked_assertions_veto_reuse() -> None:
    decision = decide_report_gap(
        missing_paths=[REPORT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=False,
    )
    assert decision.is_gap is False
    assert decision.veto == VETO_ASSERTIONS_UNCHECKED


def test_additional_failures_veto_reuse() -> None:
    decision = decide_report_gap(
        missing_paths=[REPORT],
        execution_id=GREEN_EXECUTION,
        execution_status="success",
        returncode=0,
        assertions_checked=True,
        additional_failures=["criterion http:POST /api/tickets failed"],
    )
    assert decision.is_gap is False
    assert decision.veto == "additional_failures"