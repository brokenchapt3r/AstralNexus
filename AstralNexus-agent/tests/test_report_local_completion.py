"""Recovery-routing integration for report-only completion.

Uses the real Orchestrator/Verifier seam and injected execution receipts. Checks
that completion reuses evidence without new nodes, executions or verdict flips;
unfinished business and invalid evidence remain blocked. Also covers provenance,
idempotency and interrupted/atomic writes. No model, subprocess or network calls.
"""
from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from AstralNexus.core.task_graph import TaskNode
from AstralNexus.core.verifier import VerificationResult, VerificationVerdict
from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files
from AstralNexus.tests.report_support import (
    receipt_count as _receipt_count,
    terminal_receipt,
    write_file as _write,
    execution_row_count as _execution_row_count,
    stored_receipt_id as _stored_receipt_id,
)

PRODUCT = "deliverables/app/product.py"
TEST_SCRIPT = "tests/test_product.py"
BUSINESS_DELIVERABLE = "deliverables/app/backend/init_db.py"
REPORT = "deliverables/runs/browser/defects.json"
GREEN_CMD = "python -m pytest tests -q"
PASSING_SUMMARY = "===== 12 passed in 0.42s ====="
NODE_ID = "N-701"


def _receipt(files: Dict[str, Any], *, status: str = "success",
             returncode: Optional[int] = 0,
             stdout_tail: str = PASSING_SUMMARY) -> Dict[str, Any]:
    return terminal_receipt(files, command=GREEN_CMD, call_id="call-local-completion-1",
                            status=status, returncode=returncode, stdout_tail=stdout_tail)


def _premises(root: Path) -> None:
    _write(root, PRODUCT, "def add(a, b):\n    return a + b\n")
    _write(root, TEST_SCRIPT, "def test_add():\n    assert True\n")


def _anchor_execution(root: Path, *, status: str = "success",
                      returncode: Optional[int] = 0,
                      finish: str = "succeeded",
                      stdout_tail: str = PASSING_SUMMARY) -> str:
    """One anchored execution with exactly one receipt (a re-run would add another)."""
    _premises(root)
    inventory = snapshot_files(root, [PRODUCT, TEST_SCRIPT])
    store = ExecutionStore(root)
    execution = store.begin(
        NODE_ID,
        {
            "owned_paths": [PRODUCT, TEST_SCRIPT],
            "upstream_artifacts": [],
            "acceptance_criteria": [f"tests_passed:{GREEN_CMD}"],
        },
    )
    store.append(execution, _receipt(inventory, status=status, returncode=returncode,
                                     stdout_tail=stdout_tail))
    store.finish(execution, finish, {"summary": "anchored functional evidence"})
    return execution


def _seed_node(orchestrator, execution: str, *, declared: list) -> TaskNode:
    graph = orchestrator.task_graph
    node = TaskNode(
        node_id=NODE_ID,
        goal="巡检并产出结构化缺陷单",
        role="tester",
        status="repair_pending",
        acceptance_criteria=[f"tests_passed:{GREEN_CMD}"],
        owned_paths=[PRODUCT, TEST_SCRIPT],
        metadata={
            "runtime_execution_id": execution,
            "error_type": "MissingDeclaredArtifacts",
            "expected_outputs": declared,
        },
    )
    graph.add_node(node)
    graph.add_node(TaskNode(
        node_id="C-700",
        goal="sign off the release",
        role="reviewer",
        status="waiting_dependencies",
        dependencies=[NODE_ID],
    ))
    return node


def _output(output_id: str, path: str) -> Dict[str, Any]:
    return {
        "output_id": output_id,
        "name": path,
        "type": "file",
        "path": path,
        "required": True,
        "source": "declared",
    }


def _seam_result(orchestrator, node: TaskNode) -> VerificationResult:
    """The VerificationResult the orchestrator would hand to recovery."""
    orchestrator.verifier.workspace_root = orchestrator.run_dir
    # Explicit business audit fixture; this is not the default run policy.
    orchestrator.verifier.business_acceptance_checks = True
    dimension = orchestrator.verifier._verify_deterministic_acceptance(node)
    assert dimension is not None, "the deterministic dimension must exist for this node"
    return VerificationResult(
        node_id=node.node_id,
        verdict=VerificationVerdict.REPAIR_REQUIRED,
        dimensions=[dimension],
        overall_score=0.4,
        report_gap=getattr(dimension, "report_gap", None),
    )


def _report_completion_shapes(orchestrator) -> list:
    """Every "report completion" redo shape this run recorded, with its counts.

    The old shape was "add an ``RC-*`` node and let a new Worker run it"; the S3
    shape must record ``adds_nodes == 0`` and ``adds_executions == 0`` for every
    report-class gap.  Counting the run's own events avoids quoting stale
    snapshots from an earlier batch.
    """
    shapes = []
    for event in orchestrator.trace_recorder.events:
        payload = event.get("payload") or {}
        if payload.get("recovery_type") != "report_completion":
            continue
        shapes.append({
            "event_type": event["event_type"],
            "adds_nodes": int(payload.get("adds_nodes") or 0),
            "adds_executions": int(payload.get("adds_executions") or 0),
        })
    return shapes


def test_missing_report_is_completed_in_the_same_execution_and_not_flipped(tmp_path: Path) -> None:
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="s3-same-execution") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        before_nodes = len(orchestrator.task_graph.nodes)
        before_executions = _execution_row_count(tmp_path)
        before_receipts = _receipt_count(tmp_path, execution)
        assert before_receipts == 1

        result = _seam_result(orchestrator, node)
        gap = result.report_gap
        assert gap is not None, "the framework did not classify this as a report-only gap"
        assert list(gap["missing_reports"]) == [REPORT]
        assert gap["reuse_execution_id"] == execution
        assert gap["rerun_functional_tests"] is False
        assert gap["reuse_execution_evidence"] is True

        applied = orchestrator._apply_governed_verification_recovery(node, result)

        # ① same execution: the report landed without a new node, a new execution
        # record, or a new functional run.
        assert applied["accepted"] is True
        assert applied["written"] == [REPORT]
        assert applied["added"] == [] and applied["updated"] == []
        assert applied["adds_nodes"] == 0 and applied["adds_executions"] == 0
        assert (tmp_path / REPORT).is_file()
        assert len(orchestrator.task_graph.nodes) == before_nodes
        assert _execution_row_count(tmp_path) == before_executions
        assert _receipt_count(tmp_path, execution) == before_receipts
        assert [item.node_id for item in orchestrator.task_graph.nodes] == [NODE_ID, "C-700"]
        control = orchestrator.task_graph.get_node("C-700")
        assert control is not None
        assert control.dependencies == [NODE_ID]

        document = json.loads((tmp_path / REPORT).read_text(encoding="utf-8"))
        assert document["report_kind"] == "framework_local_report_completion"
        assert document["reuse_execution_id"] == execution
        assert document["rerun_functional_tests"] is False
        assert document["executed_tests"] is False

        # ② evidence reuse, never a verdict flip.
        failed = orchestrator.task_graph.get_node(NODE_ID)
        assert failed is not None
        assert failed.status == "repair_pending"
        assert failed.status != "completed"
        assert failed.metadata.get("_terminal_failure") is not True
        assert "ownership_transfer_to" not in failed.metadata

        # ③ the SAME receipt re-verifies clean: the report is not in the
        # functional-expiry fingerprint, so backfilling it cannot go stale.
        reverified = _seam_result(orchestrator, node)
        assert reverified.report_gap is None, f"{reverified.report_gap!r}"
        dimension = reverified.dimensions[0]
        assert dimension.passed is True, f"{dimension.observations!r}"
        functional = [
            observation for observation in dimension.observations
            if observation.get("kind") == "tests_passed"
        ]
        assert functional and functional[0]["holds"] is True
        assert functional[0]["reason"] != "TEST_EXECUTION_STALE", f"{functional[0]!r}"

        # ⑥ shape metric, counted from this run's own event log: the report-class
        # gap produced zero "report_completion redo" shapes that add a node or an
        # execution, and the deleted whole-node dispatch planned nothing at all.
        shapes = _report_completion_shapes(orchestrator)
        assert shapes, "the completion must be on the record"
        assert all(item["adds_nodes"] == 0 for item in shapes), shapes
        assert all(item["adds_executions"] == 0 for item in shapes), shapes
        assert sum(1 for item in shapes
                   if item["event_type"] == "governor.report_completion.planned") == 0
        assert all(not item.node_id.startswith("RC-")
                   for item in orchestrator.task_graph.nodes)


def test_report_gap_alongside_unfinished_business_still_fails(tmp_path: Path) -> None:
    """The counterexample: a missing report AND unfinished business must still fail.

    A green functional execution is not enough -- when a declared non-report
    deliverable is missing too, the framework must NOT classify a report-only gap,
    and the local completion must never paper over the missing business artifact.
    """
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="s3-business-incomplete") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[
            _output("EO-01", REPORT),
            _output("EO-02", BUSINESS_DELIVERABLE),
        ])

        result = _seam_result(orchestrator, node)
        assert result.report_gap is None, (
            "a missing non-report deliverable must not be classified as a report gap: "
            f"{result.report_gap!r}"
        )

        outcome = orchestrator._apply_governed_verification_recovery(node, result)

        # Explicit failure: no local completion, no LLM client, terminal node.
        assert outcome["accepted"] is False
        assert outcome["rejected"] is True
        assert "recovery_type" not in outcome
        assert not (tmp_path / REPORT).exists(), (
            "the report was written even though the business work was not done"
        )
        assert node.metadata["_terminal_failure"] is True
        assert node.status != "completed"
        assert [item.node_id for item in orchestrator.task_graph.nodes] == [NODE_ID, "C-700"]
        assert _execution_row_count(tmp_path) == 1


def test_budget_exhausted_execution_is_not_completed_locally(tmp_path: Path) -> None:
    """A budget-exhausted anchor is never "only a report": it still fails."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="s3-budget-exhausted") as orchestrator:
        execution = _anchor_execution(
            tmp_path,
            status="budget_exhausted",
            returncode=None,
            finish="failed",
            stdout_tail="",
        )
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        result = _seam_result(orchestrator, node)
        assert result.report_gap is None, (
            "a budget-exhausted execution must not become a report-only gap: "
            f"{result.report_gap!r}"
        )

        outcome = orchestrator._apply_governed_verification_recovery(node, result)

        assert outcome["accepted"] is False
        assert outcome["rejected"] is True
        assert "recovery_type" not in outcome
        assert not (tmp_path / REPORT).exists()
        assert node.metadata["_terminal_failure"] is True
        assert node.status == "failed"
        assert node.status != "completed"


def test_repeated_recovery_without_new_facts_is_refused(tmp_path: Path) -> None:
    """§5: no modification and no new fact ⇒ refuse, with an explicit blocking reason."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="s3-repeat-refused") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        result = _seam_result(orchestrator, node)
        assert result.report_gap is not None

        first = orchestrator._apply_governed_verification_recovery(node, result)
        assert first["accepted"] is True and first["written"] == [REPORT]
        landed = (tmp_path / REPORT).read_text(encoding="utf-8")

        # The same verdict handed back with nothing new: the report is already
        # there, so this is a repeated recovery and it must be refused, not redone.
        second = orchestrator._apply_governed_verification_recovery(node, result)

        assert second["accepted"] is False
        assert second["rejected"] is True
        assert "repeated recovery refused" in second["error"]
        assert second["blocking_reason"] == second["error"]
        assert second["adds_nodes"] == 0 and second["adds_executions"] == 0
        assert (tmp_path / REPORT).read_text(encoding="utf-8") == landed
        assert node.status == "repair_pending"
        assert _receipt_count(tmp_path, execution) == 1
        assert _execution_row_count(tmp_path) == 1
        assert len(orchestrator.task_graph.nodes) == 2


# ============================================ P3-9 .. P3-14 (framework self-certification)
# User directive 2026-10-06 (追加要求, priority over the P3 task book): the framework's
# own backfilled report must never become the basis of the framework's own acceptance.
# The completion may deterministically summarise what the ledger already holds (execution
# id, command, exit code, assertion result, receipt id); it may not write a test
# conclusion, a defect disposition or a human judgement the ledger does not hold, and its
# presence may never stand in for the ledger verdict.  Everything below drives the SAME
# live seam as the rest of this file (no LLM, no subprocess, no network).

#: The exact key set ``runtime/report_gap.py`` authors.  A new "conclusion"/"verdict"/
#: "approved_by" field would fail this equality -- which is the point of the pin.
_REPORT_AUTHORED_KEYS = {
    "report_kind",
    "provenance",
    "generator",
    "note",
    "report_path",
    "node_id",
    "source_execution_id",
    "reuse_execution_id",
    "reuse_execution_evidence",
    "rerun_functional_tests",
    "executed_tests",
    "outcome",
    "content_complete",
    "report_requirement",
    "report_sha256",
    "reason",
    "evidence",
}

#: The exact projection of a cited receipt (ledger facts only, no narration).
_RECEIPT_PROJECTION_KEYS = {
    "receipt_id",
    "tool",
    "call_id",
    "status",
    "returncode",
    "command",
    "stdout_tail",
}

#: Vocabulary a framework-authored report must not carry in its own voice.  ``passed``
#: and ``success`` are allowed INSIDE a cited receipt (``stdout_tail``/``status`` are
#: verbatim ledger facts); they are forbidden in every authored field.
_CONCLUSION_WORDS = (
    "通过",
    "合格",
    "达标",
    "验收结论",
    "approved",
    "accepted",
    "verdict",
    "passed",
    "success",
)


def _anchor_execution_without_a_receipt(root: Path) -> str:
    """An anchored execution that recorded NO functional receipt (tests missing)."""
    _premises(root)
    store = ExecutionStore(root)
    execution = store.begin(
        NODE_ID,
        {
            "owned_paths": [PRODUCT, TEST_SCRIPT],
            "upstream_artifacts": [],
            "acceptance_criteria": [f"tests_passed:{GREEN_CMD}"],
        },
    )
    store.finish(execution, "succeeded", {"summary": "no functional receipt recorded"})
    return execution


def _evidence_snapshot(root: Path, execution: str, orchestrator) -> tuple:
    """The evidence a report write must never disturb: ledger, receipts, graph."""
    return (
        _execution_row_count(root),
        _receipt_count(root, execution),
        len(orchestrator.task_graph.nodes),
    )


# ------------------------------------------------------------------ P3-9
def test_a_backfilled_report_never_replaces_the_ledger_verdict(tmp_path: Path) -> None:
    """P3-9: the report may be generated, but acceptance stays with the ledger.

    REVERSAL: an implementation that treats "the report now exists" as the node's
    acceptance would keep ``passed`` True / a completed node after the underlying
    receipt goes stale.  Here the anchored premise moves AFTER the backfill, and the
    framework must refuse the node again on the SAME receipt it just cited.
    """
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p3-9-independent-acceptance") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        applied = orchestrator._apply_governed_verification_recovery(
            node, _seam_result(orchestrator, node)
        )
        assert applied["accepted"] is True and applied["written"] == [REPORT]
        landed = (tmp_path / REPORT).read_text(encoding="utf-8")

        # The independent re-verification still reads the ledger and clears the gap.
        reverified = _seam_result(orchestrator, node)
        assert reverified.dimensions[0].passed is True
        assert reverified.report_gap is None

        # Now the cited evidence expires.  A conclusion copied from the report would
        # still say "done"; the ledger says otherwise.
        _write(tmp_path, PRODUCT, "# changed after the anchored execution\n")
        stale = _seam_result(orchestrator, node)
        assert stale.dimensions[0].passed is False
        assert stale.report_gap is None, (
            "a stale receipt was still classified as a report-only gap"
        )

        outcome = orchestrator._apply_governed_verification_recovery(node, stale)

        assert outcome["accepted"] is False
        assert "local_completion" not in outcome
        assert (tmp_path / REPORT).read_text(encoding="utf-8") == landed, (
            "the existing report was rewritten from stale evidence"
        )
        assert node.status != "completed"
        assert node.metadata.get("_terminal_failure") is True
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == (1, 1, 2)


def test_the_backfilled_report_states_no_conclusion_of_its_own(tmp_path: Path) -> None:
    """P3-9 content check: the written report carries ledger facts, not conclusions."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p3-9-content") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        applied = orchestrator._apply_governed_verification_recovery(
            node, _seam_result(orchestrator, node)
        )
        assert applied["accepted"] is True and applied["written"] == [REPORT]

        record = (tmp_path / REPORT).read_text(encoding="utf-8")
        document = json.loads(record)

        assert set(document) == _REPORT_AUTHORED_KEYS, (
            "the framework-authored report grew a field of its own: "
            f"{sorted(set(document) - _REPORT_AUTHORED_KEYS)}"
        )
        assert set(document["evidence"][0]) == _RECEIPT_PROJECTION_KEYS

        authored = " ".join(
            str(value) for key, value in document.items() if key != "evidence"
        ).lower()
        for word in _CONCLUSION_WORDS:
            assert word.lower() not in authored, (
                f"the framework authored the conclusion word {word!r}: {authored!r}"
            )

        assert document["executed_tests"] is False
        assert document["rerun_functional_tests"] is False
        assert document["reuse_execution_evidence"] is True
        assert document["reuse_execution_id"] == execution
        # The one "passed" in the file is the ledger's own stdout, quoted verbatim.
        assert document["evidence"][0]["stdout_tail"] == PASSING_SUMMARY
        assert document["evidence"][0]["command"] == GREEN_CMD
        assert document["evidence"][0]["receipt_id"] == _stored_receipt_id(
            tmp_path, execution
        )


# ------------------------------------------------------------------ P3-10
@pytest.mark.parametrize("anchor", ["failing_receipt", "no_receipt"])
def test_a_failed_or_absent_test_execution_never_earns_a_report(
    tmp_path: Path, anchor: str
) -> None:
    """P3-10: tests failing or missing must not yield a report, and never ``completed``.

    REVERSAL: an implementation that backfills a report whenever a declared report
    path is missing (without checking that the functional tests really passed) would
    write the report here and, in the "no receipt" case, cite nothing at all.
    """
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id=f"p3-10-{anchor}") as orchestrator:
        if anchor == "failing_receipt":
            execution = _anchor_execution(
                tmp_path,
                status="failed",
                returncode=1,
                finish="failed",
                stdout_tail="1 failed in 0.2s",
            )
        else:
            execution = _anchor_execution_without_a_receipt(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        result = _seam_result(orchestrator, node)

        assert result.report_gap is None, (
            f"{anchor}: an unproven functional execution was classified as a "
            f"report-only gap: {result.report_gap!r}"
        )
        assert result.dimensions[0].passed is False

        outcome = orchestrator._apply_governed_verification_recovery(node, result)

        assert outcome["accepted"] is False
        assert "local_completion" not in outcome
        assert not (tmp_path / REPORT).exists(), (
            f"{anchor}: a report was written without a proven functional execution"
        )
        assert node.status != "completed"
        assert node.metadata.get("_terminal_failure") is True
        expected_receipts = 1 if anchor == "failing_receipt" else 0
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == (
            1, expected_receipts, 2,
        )


# ------------------------------------------------------------------ P3-11
def test_a_stale_functional_receipt_never_earns_a_backfilled_report(
    tmp_path: Path,
) -> None:
    """P3-11: an expired receipt is refused, with the veto named explicitly."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.report_gap import VETO_STALE, decide_report_gap

    with Orchestrator(tmp_path, run_id="p3-11-stale") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        # Precondition: right now this really is a report-only gap.
        assert _seam_result(orchestrator, node).report_gap is not None

        _write(tmp_path, PRODUCT, "# changed after the anchored execution\n")
        stale = _seam_result(orchestrator, node)

        assert stale.report_gap is None
        assert stale.dimensions[0].passed is False

        outcome = orchestrator._apply_governed_verification_recovery(node, stale)

        assert outcome["accepted"] is False
        assert "local_completion" not in outcome
        assert not (tmp_path / REPORT).exists(), (
            "a report was backfilled from a stale functional receipt"
        )
        assert node.status != "completed"
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == (1, 1, 2)

        # The refusal has a name, not just an absence.
        decision = decide_report_gap(
            missing_paths=[REPORT],
            execution_id=execution,
            execution_status="success",
            returncode=0,
            assertions_checked=True,
            stale=True,
        )
        assert decision.veto == VETO_STALE
        assert decision.gap is None


def test_an_identityless_or_failed_receipt_is_never_used_as_the_citation(
    tmp_path: Path,
) -> None:
    """P3-11: without a matching execution identity the completion must refuse."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.report_gap import (
        VETO_NO_EXECUTION,
        VETO_NOT_A_REAL_SUCCESS,
        decide_report_gap,
    )

    with Orchestrator(tmp_path, run_id="p3-11-identity") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        # The node loses the identity of the execution it was verified under.
        node.metadata.pop("runtime_execution_id", None)
        orphaned = _seam_result(orchestrator, node)

        assert orphaned.report_gap is None
        assert orphaned.dimensions[0].passed is False

        outcome = orchestrator._apply_governed_verification_recovery(node, orphaned)

        assert outcome["accepted"] is False
        assert "local_completion" not in outcome
        assert not (tmp_path / REPORT).exists()
        assert node.status != "completed"
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == (1, 1, 2)

        no_identity = decide_report_gap(
            missing_paths=[REPORT],
            execution_id="",
            execution_status="success",
            returncode=0,
            assertions_checked=True,
        )
        assert no_identity.veto == VETO_NO_EXECUTION
        assert no_identity.gap is None

        not_a_success = decide_report_gap(
            missing_paths=[REPORT],
            execution_id=execution,
            execution_status="failed",
            returncode=1,
            assertions_checked=True,
        )
        assert not_a_success.veto == VETO_NOT_A_REAL_SUCCESS
        assert not_a_success.gap is None


# ------------------------------------------------------------------ P3-12
def test_a_present_but_unusable_report_is_never_bypassed_nor_left_unexplained(
    tmp_path: Path,
) -> None:
    """P3-12: a report that exists but does not satisfy the requirement must be handled.

    REVERSAL: both degenerate implementations fail here -- one that sees the file and
    calls the node done (bypass), and one that keeps re-deciding the same gap forever
    (stuck in ``repair_pending`` with no explanation).
    """
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p3-12-unusable-report") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        live_gap = _seam_result(orchestrator, node).report_gap
        assert live_gap is not None, "precondition: the report is missing"

        # A 0-byte report satisfies "the path exists" but not "non-empty".
        report_path = tmp_path / REPORT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("", encoding="utf-8")

        unusable = _seam_result(orchestrator, node)
        assert unusable.report_gap is None, (
            "an unusable (empty) report was still treated as 'only the report is missing'"
        )
        assert unusable.dimensions[0].passed is False

        bypass_probe = orchestrator._apply_governed_verification_recovery(node, unusable)

        assert bypass_probe["accepted"] is False
        assert report_path.read_text(encoding="utf-8") == "", (
            "the empty report was silently accepted (or half-claimed) as the artifact"
        )
        assert node.status != "completed", "an unusable report completed the node"

        # Action layer, same gap: an existing-but-empty target is NOT skipped -- it is
        # rewritten from the ledger, and the rewrite cites the anchored receipt.
        written = orchestrator._complete_report_gap_locally(node, unusable, dict(live_gap))
        assert written["accepted"] is True
        assert written["written"] == [REPORT]
        assert written["skipped_existing"] == []

        landed = report_path.read_text(encoding="utf-8")
        document = json.loads(landed)
        assert document["report_kind"] == "framework_local_report_completion"
        assert document["evidence"][0]["receipt_id"] == _stored_receipt_id(
            tmp_path, execution
        )

        # And the next identical attempt is refused explicitly, on the record, against
        # the recovery budget -- it cannot sit in ``repair_pending`` unexplained.
        churn_before = int(
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn or 0
        )
        repeat = orchestrator._complete_report_gap_locally(node, unusable, dict(live_gap))

        assert repeat["accepted"] is False and repeat["rejected"] is True
        assert "repeated recovery refused" in repeat["error"]
        assert repeat["blocking_reason"] == repeat["error"]
        assert report_path.read_text(encoding="utf-8") == landed
        assert int(
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn or 0
        ) > churn_before
        rejected = [
            event
            for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.report_completion.rejected"
        ]
        assert rejected
        assert rejected[-1]["payload"]["action"] == "terminal"
        assert rejected[-1]["payload"]["reason_codes"] == ["REPORT_COMPLETION_REJECTED"]


# ------------------------------------------------------------------ P3-13
def test_a_blocked_report_write_leaves_the_evidence_and_the_node_untouched(
    tmp_path: Path,
) -> None:
    """P3-13: a failed report write must not consume evidence; the retry must not re-run."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p3-13-blocked-write") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        before = _evidence_snapshot(tmp_path, execution, orchestrator)
        assert before[1] == 1

        # The report's parent directory is occupied by a regular file: the write path
        # cannot create it.
        blocker = tmp_path / "deliverables/runs/browser"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory\n", encoding="utf-8")

        result = _seam_result(orchestrator, node)
        assert result.report_gap is not None

        with pytest.raises(FileExistsError):
            orchestrator._apply_governed_verification_recovery(node, result)

        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        assert node.status == "repair_pending"
        assert node.metadata.get("_terminal_failure") is None
        assert not (tmp_path / REPORT).exists()

        # Clear the obstruction: the retry reuses the SAME receipt -- no functional
        # re-run, no new execution, no new node.
        blocker.unlink()
        retry = orchestrator._apply_governed_verification_recovery(
            node, _seam_result(orchestrator, node)
        )

        assert retry["accepted"] is True and retry["written"] == [REPORT]
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        document = json.loads((tmp_path / REPORT).read_text(encoding="utf-8"))
        assert [item["receipt_id"] for item in document["evidence"]] == [
            _stored_receipt_id(tmp_path, execution)
        ]


def test_an_interrupted_report_write_does_not_mutate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-13: an interruption during the write leaves the ledger exactly as it was."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p3-13-interrupted-write") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        before = _evidence_snapshot(tmp_path, execution, orchestrator)
        target = tmp_path / REPORT

        real_builtin_open = builtins.open
        real_path_open = Path.open

        def _interrupt(file, mode) -> None:
            # P5b：写入改成"同目录临时文件 + os.replace"后，目标路径本身不再被 open。
            # 中断注入点随之移到那份临时文件（与 target 同目录、名字以 `.<target>.` 开头）。
            candidate = Path(file)
            if (
                candidate.parent == target.parent
                and candidate.name.startswith(f".{target.name}.")
                and "w" in str(mode)
            ):
                raise KeyboardInterrupt("simulated interruption during the report write")

        def builtin_open(file, mode="r", *args, **kwargs):
            _interrupt(file, mode)
            return real_builtin_open(file, mode, *args, **kwargs)

        def path_open(self, mode="r", *args, **kwargs):
            _interrupt(self, mode)
            return real_path_open(self, mode, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", builtin_open)
            patch.setattr(Path, "open", path_open)

            result = _seam_result(orchestrator, node)
            assert result.report_gap is not None

            with pytest.raises(KeyboardInterrupt):
                orchestrator._apply_governed_verification_recovery(node, result)

        assert not target.exists(), "a partial report survived the interruption"
        assert not list(
            target.parent.glob(f".{target.name}.*.tmp")
        ), "a temporary report file survived the interruption"
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        assert node.status == "repair_pending"
        assert node.metadata.get("_terminal_failure") is None

        retry = orchestrator._apply_governed_verification_recovery(
            node, _seam_result(orchestrator, node)
        )

        assert retry["accepted"] is True and retry["written"] == [REPORT]
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        document = json.loads((tmp_path / REPORT).read_text(encoding="utf-8"))
        assert [item["receipt_id"] for item in document["evidence"]] == [
            _stored_receipt_id(tmp_path, execution)
        ]


# ------------------------------------------------------------------ P3-14
def test_repeated_local_completion_is_idempotent_and_explains_itself(
    tmp_path: Path,
) -> None:
    """P3-14: consecutive local completions add nothing, overwrite nothing, and say why."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p3-14-idempotent") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])

        result = _seam_result(orchestrator, node)
        first = orchestrator._apply_governed_verification_recovery(node, result)

        assert first["accepted"] is True and first["written"] == [REPORT]
        landed = (tmp_path / REPORT).read_bytes()
        before = _evidence_snapshot(tmp_path, execution, orchestrator)
        status_before = node.status
        assert status_before == "repair_pending"

        churn_before = int(
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn or 0
        )
        for _ in range(2):
            repeat = orchestrator._apply_governed_verification_recovery(node, result)
            assert repeat["accepted"] is False and repeat["rejected"] is True
            assert "repeated recovery refused" in repeat["error"]
            assert repeat["blocking_reason"] == repeat["error"]
            assert repeat["adds_nodes"] == 0 and repeat["adds_executions"] == 0

        assert (tmp_path / REPORT).read_bytes() == landed, "the report was overwritten"
        assert node.status == status_before
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        assert int(
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn or 0
        ) > churn_before

        rejected = [
            event
            for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.report_completion.rejected"
        ]
        assert len(rejected) == 2
        assert all(event["payload"]["action"] == "terminal" for event in rejected)
        assert all(
            event["payload"]["reason_codes"] == ["REPORT_COMPLETION_REJECTED"]
            for event in rejected
        )


# ------------------------------------------------------------------ P5
# 「报告不自证」（用户 2026-10-06）：补出的报告必须留下**来源执行／生成器版本／报告
# 哈希／处理结果**，并且**自己声明**它没有满足报告要求（``content_complete: false``＋
# ``report_requirement: "unresolved"``）；写失败与被拒同样是"一次处理"，必须留下处理
# 结果，但账本、收据、节点状态逐字不变。判定点仍然只有 ``decide_report_gap`` 一处：
# ``report_requirement_satisfiable_from_receipts`` 是纯查询（无副作用、不改状态）。


def _canonical_report_bytes(document: Dict[str, Any]) -> bytes:
    """The documented recomputation: drop ``report_sha256``, re-render canonically."""
    without_hash = {
        key: value for key, value in document.items() if key != "report_sha256"
    }
    return (
        json.dumps(without_hash, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _markdown_body_bytes(document: str) -> bytes:
    """The same recomputation for the markdown rendering: drop the one hash line."""
    body = [
        line for line in document.splitlines()
        if not line.startswith("- report_sha256: ")
    ]
    return ("\n".join(body) + "\n").encode("utf-8")


def test_the_backfilled_report_carries_its_audit_fields_and_declares_incompleteness(
    tmp_path: Path,
) -> None:
    """P5-1: generator / real sha256 / outcome / source execution + unresolved requirement."""
    import hashlib

    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.report_gap import (
        GENERATOR,
        report_requirement_satisfiable_from_receipts,
    )

    with Orchestrator(tmp_path, run_id="p5-1-audit-fields") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        before = _evidence_snapshot(tmp_path, execution, orchestrator)

        applied = orchestrator._apply_governed_verification_recovery(
            node, _seam_result(orchestrator, node)
        )
        assert applied["accepted"] is True and applied["written"] == [REPORT]

        raw = (tmp_path / REPORT).read_bytes()
        document = json.loads(raw.decode("utf-8"))

        # ① 审计字段：来源执行 / 生成器版本 / 处理结果
        assert document["generator"] == GENERATOR
        assert GENERATOR == "astralnexus.report_local_completion/1"
        assert document["outcome"] == "written"
        assert document["source_execution_id"] == execution
        assert document["source_execution_id"] == document["reuse_execution_id"]

        # ② 报告哈希是**真实** SHA-256：去掉该键后重新规范渲染再哈希，可逐字复算
        assert document["report_sha256"] == hashlib.sha256(
            _canonical_report_bytes(document)
        ).hexdigest()
        assert document["report_sha256"] != "0" * 64

        # ③「报告仍不完整」的显式声明（md/json 两条路径同源，见下一项）
        assert document["content_complete"] is False
        assert document["report_requirement"] == "unresolved"
        assert report_requirement_satisfiable_from_receipts(REPORT) is False

        # ④ 不新增节点/执行/收据，节点也不被翻面（与 P3 同口径）
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        assert node.status == "repair_pending"
        assert orchestrator.task_graph.get_node(NODE_ID) is node


def test_the_markdown_completion_record_carries_the_same_audit_fields() -> None:
    """P5-2: the non-JSON rendering path declares and hashes the same audit fields."""
    import hashlib

    from AstralNexus.runtime.report_gap import (
        GENERATOR,
        ReportGap,
        local_completion_document,
        report_requirement_satisfiable_from_receipts,
    )

    report_path = "deliverables/runs/browser/defects.md"
    document = local_completion_document(
        ReportGap(missing_reports=(report_path,), execution_id="exec-ledger-0007"),
        path=report_path,
        node_id=NODE_ID,
        receipts=(_receipt({"deliverables/app/product.py": "sha"}),),
    )
    lines = document.splitlines()
    for expected in (
        "# report completion: deliverables/runs/browser/defects.md",
        "- report_kind: framework_local_report_completion",
        "- provenance: anchored_execution_receipts",
        f"- generator: {GENERATOR}",
        f"- node_id: {NODE_ID}",
        "- source_execution_id: exec-ledger-0007",
        "- reuse_execution_id: exec-ledger-0007",
        "- outcome: written",
        "- content_complete: False",
        "- report_requirement: unresolved",
        "## cited receipts (anchored execution)",
    ):
        assert expected in lines, f"missing {expected!r} in\n{document}"

    digest = hashlib.sha256(_markdown_body_bytes(document)).hexdigest()
    assert f"- report_sha256: {digest}" in lines
    assert report_requirement_satisfiable_from_receipts(report_path) is False
    # 声明的"未满足"不是本函数现编的常量：它来自那处纯查询
    assert "content_complete: True" not in document
    assert "report_requirement: resolved" not in document


def test_a_refused_completion_records_its_generator_execution_and_outcome(
    tmp_path: Path,
) -> None:
    """P5-3: a refusal is a处理结果 too -- existing event, extended payload only."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.report_gap import GENERATOR

    with Orchestrator(tmp_path, run_id="p5-3-refused-outcome") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        result = _seam_result(orchestrator, node)

        assert orchestrator._apply_governed_verification_recovery(
            node, result
        )["accepted"] is True
        repeat = orchestrator._apply_governed_verification_recovery(node, result)
        assert repeat["accepted"] is False and repeat["rejected"] is True

        rejected = [
            event
            for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.report_completion.rejected"
        ]
        assert len(rejected) == 1
        payload = rejected[-1]["payload"]
        assert payload["generator"] == GENERATOR
        assert payload["source_execution_id"] == execution
        assert payload["outcome"] == "refused:repeated_recovery"
        # 既有语义不动：仍是终止判定，仍是同一条事件类型与 reason_codes
        assert payload["action"] == "terminal"
        assert payload["reason_codes"] == ["REPORT_COMPLETION_REJECTED"]


def test_a_failed_report_write_records_its_outcome_without_touching_evidence(
    tmp_path: Path,
) -> None:
    """P5-4: 写入异常也留下处理结果；账本/收据/节点逐字不变（P3-13 语义不变）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.report_gap import GENERATOR

    with Orchestrator(tmp_path, run_id="p5-4-write-failed") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        before = _evidence_snapshot(tmp_path, execution, orchestrator)
        churn_before = int(
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn or 0
        )

        blocker = tmp_path / "deliverables/runs/browser"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory\n", encoding="utf-8")

        with pytest.raises(FileExistsError):
            orchestrator._apply_governed_verification_recovery(
                node, _seam_result(orchestrator, node)
            )

        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before
        assert node.status == "repair_pending"
        assert not (tmp_path / REPORT).exists()

        rejected = [
            event
            for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.report_completion.rejected"
        ]
        assert len(rejected) == 1
        payload = rejected[-1]["payload"]
        assert payload["generator"] == GENERATOR
        assert payload["source_execution_id"] == execution
        assert payload["outcome"] == "refused:write_failed"
        # 异常上抛不是"返回的拒绝判定"：不动 churn 记账（S3 现状不变）
        assert int(
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn or 0
        ) == churn_before

        # 清障后重试复用同一条收据：不重跑功能测试、不新增执行/节点
        blocker.unlink()
        retry = orchestrator._apply_governed_verification_recovery(
            node, _seam_result(orchestrator, node)
        )
        assert retry["accepted"] is True and retry["written"] == [REPORT]
        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before


# ------------------------------------------------------------------ P5b
# 复核条件 4（用户 2026-10-06）："文件存在"≠"完成"。框架补出的补全记录只有**自哈希
# 自洽且引用成立**（可追溯）才算落地；截断/损坏/哈希不匹配的记录视为**未完成**，落回
# pending 走"同目录临时文件 + os.replace"的原子重写；非框架的非空文件维持既有
# "跳过不覆盖"语义，0 字节仍可改写。


def test_a_damaged_completion_record_is_incomplete_and_is_rewritten_atomically(
    tmp_path: Path,
) -> None:
    """P5b-4: 完好记录幂等跳过；截断／哈希不匹配／0 字节 ⇒ 原子重写；非框架非空 ⇒ 不覆盖。"""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="p5b-4-damaged-record") as orchestrator:
        execution = _anchor_execution(tmp_path)
        node = _seed_node(orchestrator, execution, declared=[_output("EO-01", REPORT)])
        result = _seam_result(orchestrator, node)
        gap = dict(result.report_gap or {})
        before = _evidence_snapshot(tmp_path, execution, orchestrator)

        first = orchestrator._complete_report_gap_locally(node, result, dict(gap))
        assert first["accepted"] is True and first["written"] == [REPORT]
        intact = (tmp_path / REPORT).read_bytes()
        assert json.loads(intact.decode("utf-8"))["report_kind"] == (
            "framework_local_report_completion"
        )

        # 完好记录：无新事实的重复请求仍被拒绝（P3-14 语义），字节逐字不变
        repeat = orchestrator._complete_report_gap_locally(node, result, dict(gap))
        assert repeat["accepted"] is False and repeat["rejected"] is True
        assert "repeated recovery refused" in repeat["error"]
        assert (tmp_path / REPORT).read_bytes() == intact

        # (a) 截断/损坏：不再是可解析记录 ⇒ 未完成 ⇒ 原子重写
        (tmp_path / REPORT).write_text(
            '{"report_kind": "framework_local_report_completion",',
            encoding="utf-8",
        )
        repaired = orchestrator._complete_report_gap_locally(node, result, dict(gap))
        assert repaired["accepted"] is True
        assert repaired["written"] == [REPORT], "截断的框架记录必须视为未完成并重写"
        assert repaired["skipped_existing"] == []
        assert (tmp_path / REPORT).read_bytes() == intact
        assert not list(
            (tmp_path / REPORT).parent.glob(f".{REPORT.split('/')[-1]}.*.tmp")
        ), "原子写入留下了临时文件"

        # (b) 哈希不匹配：仍可解析、仍自称框架记录，但自哈希不自洽 ⇒ 未完成 ⇒ 重写
        damaged = json.loads(intact.decode("utf-8"))
        damaged["report_sha256"] = "0" * 64
        (tmp_path / REPORT).write_text(
            json.dumps(damaged, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert (tmp_path / REPORT).read_bytes() != intact
        repaired_hash = orchestrator._complete_report_gap_locally(
            node, result, dict(gap)
        )
        assert repaired_hash["written"] == [REPORT]
        assert (tmp_path / REPORT).read_bytes() == intact

        # (c) 非框架的非空文件：既有"跳过不覆盖"语义不变
        (tmp_path / REPORT).write_text(
            "agent-authored report body\n", encoding="utf-8"
        )
        kept = orchestrator._complete_report_gap_locally(node, result, dict(gap))
        assert kept["accepted"] is False and kept["rejected"] is True
        assert "repeated recovery refused" in kept["error"]
        assert (tmp_path / REPORT).read_text(encoding="utf-8") == (
            "agent-authored report body\n"
        )

        # (d) 0 字节：仍可改写（P3-12 语义不变）
        (tmp_path / REPORT).write_text("", encoding="utf-8")
        rewritten_empty = orchestrator._complete_report_gap_locally(
            node, result, dict(gap)
        )
        assert rewritten_empty["written"] == [REPORT]
        assert (tmp_path / REPORT).read_bytes() == intact

        assert _evidence_snapshot(tmp_path, execution, orchestrator) == before