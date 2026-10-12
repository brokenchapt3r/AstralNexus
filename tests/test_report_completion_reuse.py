"""Verifier classification and reuse of an anchored report-only execution.

Covers valid evidence, stale/failed evidence, budget exhaustion, missing business
artifacts and final-delivery blocking. Receipts are injected into the real ledger;
no model, subprocess or network is used. These are implemented regression tests,
not expected failures awaiting a future fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files
from tests.report_support import (
    receipt_count as _receipt_count,
    terminal_receipt,
)
from AstralNexus.runtime.report_gap import (
    REPORT_GAP_REASON,
    classify_report_gap,
    local_completion_plan,
)
from AstralNexus.runtime.trace import TraceRecorder

PRODUCT = "deliverables/app/product.py"
TEST_SCRIPT = "tests/test_product.py"
REPORT = "deliverables/reports/N-501-summary.md"
NON_REPORT = "deliverables/app/CHANGELOG.md"
GREEN_CMD = "python -m pytest tests -q"
PASSING_SUMMARY = "===== 12 passed in 0.42s ====="

REQUIRED_REPORT: Dict[str, Any] = {
    "output_id": "O-01",
    "name": "wrap-up report",
    "type": "file",
    "path": REPORT,
    "required": True,
    "source": "explicit",
}
REQUIRED_NON_REPORT: Dict[str, Any] = {
    "output_id": "O-04",
    "name": "changelog",
    "type": "file",
    "path": NON_REPORT,
    "required": True,
    "source": "explicit",
}


# --------------------------------------------------------------------------- helpers


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _receipt(
    command: str, files: Optional[Dict[str, Any]] = None,
    receipt_id: str = "rcpt-report-gap-1", status: str = "success",
    returncode: Optional[int] = 0, stdout_tail: str = PASSING_SUMMARY,
) -> Dict[str, Any]:
    return terminal_receipt(files or {}, command=command, call_id="call-report-gap-1",
                            receipt_id=receipt_id, status=status, returncode=returncode,
                            stdout_tail=stdout_tail)


def _verifier(root: Path) -> Verifier:
    return Verifier(
        TaskGraph(), TraceRecorder(root), llm_client=None, workspace_root=root
    )


def _seed_functional_premises(root: Path) -> Dict[str, Any]:
    """Create the functional premise files and return the receipt-shaped inventory."""
    _write(root / PRODUCT, "def add(a, b):\n    return a + b\n")
    _write(root / TEST_SCRIPT, "def test_add():\n    assert True\n")
    return snapshot_files(root, [PRODUCT, TEST_SCRIPT])


def _anchor_execution(
    root: Path,
    node_id: str,
    *,
    receipt: Optional[Dict[str, Any]] = None,
    execution_status: str = "succeeded",
) -> str:
    """One anchored functional execution, exactly one receipt, no re-runs."""
    inventory = _seed_functional_premises(root) if receipt is None else None
    store = ExecutionStore(root)
    execution = store.begin(
        node_id,
        {
            "owned_paths": [PRODUCT, TEST_SCRIPT],
            "upstream_artifacts": [],
            "acceptance_criteria": [f"tests_passed:{GREEN_CMD}"],
        },
    )
    store.append(
        execution,
        receipt
        if receipt is not None
        else _receipt(GREEN_CMD, inventory, receipt_id=f"rcpt-{node_id}"),
    )
    store.finish(execution, execution_status, {"summary": "functional suite green"})
    return execution


def _node(execution: str, outputs: List[Dict[str, Any]], node_id: str = "N-501") -> TaskNode:
    return TaskNode(
        node_id=node_id,
        goal="巡检并产出收尾报告",
        role="coder",
        acceptance_criteria=[f"tests_passed:{GREEN_CMD}"],
        metadata={
            "runtime_execution_id": execution,
            "expected_outputs": list(outputs),
        },
    )


def _observation(dimension: Any, criterion_id: str) -> Dict[str, Any]:
    for observation in dimension.observations:
        if observation.get("criterion_id") == criterion_id:
            return observation
    raise AssertionError(
        f"no observation {criterion_id!r} in {dimension.observations!r}"
    )


def _functional_observation(dimension: Any) -> Dict[str, Any]:
    for observation in dimension.observations:
        if observation.get("kind") == "tests_passed":
            return observation
    raise AssertionError(
        f"no functional observation in {dimension.observations!r}"
    )


# =================================== boundary 1+2: the framework decides "only the report"
# m02774 ①: a real anchored successful execution, required assertions checked, no budget
# exhaustion / other failure, and the missing item is clearly a report.  A worker claiming
# "all green" is not sufficient -- the receipt must be a real success.
# m02774 ②: the reused thing is execution evidence, never the acceptance verdict: the node
# must NOT flip to passed while the required artifact is absent, so the delivery gate keeps
# blocking (control test_missing_required_artifact_blocks_final_delivery pins the gate).


def test_missing_report_after_a_green_execution_is_a_report_only_gap(tmp_path: Path) -> None:
    """REVERSAL: a green anchored execution + a missing report is a report-only gap."""
    execution = _anchor_execution(tmp_path, "N-501")
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_REPORT])
    )
    assert dimension is not None
    gap = getattr(dimension, "report_gap", None)
    assert gap is not None, (
        "a missing report next to a real green anchored execution was not classified "
        f"as a report-only gap: observations={dimension.observations!r}"
    )
    assert gap["reason"] == REPORT_GAP_REASON
    assert list(gap["missing_reports"]) == [REPORT]
    assert gap["reuse_execution_id"] == execution
    assert gap["rerun_functional_tests"] is False
    assert gap["reuse_execution_evidence"] is True

    functional = _functional_observation(dimension)
    assert functional["holds"] is True, (
        "the functional evidence was invalidated while deciding the report gap: "
        f"{functional!r}"
    )


def test_report_gap_does_not_flip_the_node_verdict(tmp_path: Path) -> None:
    """REVERSAL (m02774 ②): evidence reuse must not become a verdict flip."""
    execution = _anchor_execution(tmp_path, "N-501")
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_REPORT])
    )
    assert dimension is not None
    assert getattr(dimension, "report_gap", None) is not None
    missing = [
        observation
        for observation in dimension.observations
        if observation["criterion_id"].endswith(f"file:{REPORT} non-empty")
        or observation.get("reason") == "FILE_NOT_FOUND"
    ]
    assert missing, f"the missing report left no trace: {dimension.observations!r}"
    assert all(observation["holds"] is False for observation in missing)
    assert dimension.passed is False, (
        "the node was flipped to passed while its required artifact is still absent: "
        f"observations={dimension.observations!r}"
    )


# ========================= boundary 3: completion reuses evidence instead of re-running
# m02774 ③: a report update must not invalidate functional evidence; the completion must
# not re-run the functional tests.  F1 already keeps report-class paths out of the
# functional premise set (tests/test_artifact_contract_inversion.py), so the receipt that
# was valid before the backfill is still valid after it -- and the executed-test count is
# unchanged.  Removing the report after the backfill must go back to a gap, not to a pass.


def test_completing_the_report_reuses_the_functional_receipt(tmp_path: Path) -> None:
    """REVERSAL: completing the report must not re-run the functional tests."""
    execution = _anchor_execution(tmp_path, "N-501")
    before = _receipt_count(tmp_path, execution)
    assert before == 1

    first = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_REPORT])
    )
    assert first is not None
    assert getattr(first, "report_gap", None) is not None

    _write(tmp_path / REPORT, "# wrap-up report (backfilled by the narrow completion task)\n")

    second = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_REPORT])
    )
    assert second is not None
    assert getattr(second, "report_gap", None) is None, (
        f"the report exists but the gap persisted: {getattr(second, 'report_gap', None)!r}"
    )
    functional = _functional_observation(second)
    assert functional["holds"] is True and functional["reason"] != "TEST_EXECUTION_STALE", (
        "backfilling the report invalidated the functional receipt: "
        f"reason={functional['reason']!r} detail={functional['detail']!r}"
    )
    assert second.passed is True
    assert _receipt_count(tmp_path, execution) == before, (
        "the completion flow executed the functional tests again: "
        f"receipts={ExecutionStore(tmp_path).get(execution)['receipts']!r}"
    )


# ============================================ boundary 4 / controls: no overshoot allowed
# m02774 ④: budget exhaustion keeps the primary failure reason -- never reclassified.
# m02366: a non-report required artifact is a real functional gap, not a report gap.


def test_budget_exhausted_execution_is_not_a_report_only_gap(tmp_path: Path) -> None:
    """CONTROL (m02774 ④): budget exhaustion is never rewritten into a report gap."""
    execution = _anchor_execution(
        tmp_path,
        "N-502",
        receipt=_receipt(
            GREEN_CMD,
            {},
            receipt_id="rcpt-N-502",
            status="budget_exhausted",
            returncode=None,
            stdout_tail="",
        ),
        execution_status="failed",
    )
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_REPORT], node_id="N-502")
    )
    assert dimension is not None
    assert getattr(dimension, "report_gap", None) is None, (
        "a budget-exhausted execution was reclassified as a report-only gap: "
        f"report_gap={getattr(dimension, 'report_gap', None)!r}"
    )
    assert dimension.passed is False


def test_missing_non_report_artifact_is_not_a_report_only_gap(tmp_path: Path) -> None:
    """CONTROL: a missing non-report required artifact stays a functional gap."""
    execution = _anchor_execution(tmp_path, "N-503")
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_NON_REPORT], node_id="N-503")
    )
    assert dimension is not None
    assert getattr(dimension, "report_gap", None) is None, (
        "a missing non-report artifact was excused as a report-only gap: "
        f"report_gap={getattr(dimension, 'report_gap', None)!r}"
    )
    assert dimension.passed is False


def test_present_report_has_no_report_gap(tmp_path: Path) -> None:
    """CONTROL: when the report is present there is nothing to complete."""
    execution = _anchor_execution(tmp_path, "N-504")
    _write(tmp_path / REPORT, "# wrap-up report\n")
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(
        _node(execution, [REQUIRED_REPORT], node_id="N-504")
    )
    assert dimension is not None
    assert getattr(dimension, "report_gap", None) is None
    assert dimension.passed is True


# ================== the narrow successor's shape, decided by the framework, not a worker
# m02774 ①③: the successor may touch exactly the missing reports, never the product code
# or the test script, and it never re-runs the functional tests it inherits evidence from.


def _decision_gap() -> Any:
    gap = classify_report_gap(
        missing_paths=[REPORT],
        execution_id="exec-501-report",
        execution_status="success",
        returncode=0,
        assertions_checked=True,
    )
    assert gap is not None, "a real green execution with one missing report must be a gap"
    return gap


def test_local_completion_plan_is_narrow_and_reuses_the_execution() -> None:
    """REVERSAL: the in-execution completion's shape is framework-authored and narrow."""
    plan = local_completion_plan(_decision_gap(), node_id="N-501")
    assert plan["kind"] == "report_local_completion"
    assert list(plan["owned_paths"]) == [REPORT], (
        f"the completion touches more than the missing report: {plan!r}"
    )
    assert plan["node_id"] == "N-501"
    assert plan["reuse_execution_id"] == "exec-501-report"
    assert plan["reuse_execution_evidence"] is True
    assert plan["rerun_functional_tests"] is False
    assert plan["executes_tests"] is False
    assert plan["adds_nodes"] == 0
    assert plan["adds_executions"] == 0
    assert plan["source"] == REPORT_GAP_REASON


def test_local_completion_plan_never_holds_product_or_test_paths() -> None:
    """REVERSAL (m02774 ③): the completion cannot invalidate the evidence it reuses."""
    from AstralNexus.runtime.verification_identity import is_report_class_path

    plan = local_completion_plan(_decision_gap(), node_id="N-501")
    declared = list(plan["owned_paths"])
    assert PRODUCT not in declared and TEST_SCRIPT not in declared, (
        f"the completion holds functional premises: {declared!r}"
    )
    assert all(is_report_class_path(path) for path in declared)
    # It also adds nothing that could re-run anything.
    assert plan["adds_nodes"] == 0 and plan["adds_executions"] == 0
    assert plan["executes_tests"] is False and plan["rerun_functional_tests"] is False