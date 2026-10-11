"""doc 51 §9 边界①②③: the orchestrator SEAM for a framework-decided report-only gap.

Three green files pin the two halves of the contract:

* ``tests/test_report_completion_reuse.py`` — the Verifier decides "only the report is
  missing" from a real anchored green execution (m02774 ①), never flips the node verdict
  (②), and a backfilled report does not invalidate the functional receipt (③);
* ``tests/test_report_completion_dispatch.py`` — the Governor builds exactly one narrow
  successor that owns only report paths, and a non-report failure still needs the LLM;
* ``tests/test_artifact_contract_inversion.py`` — report-class paths stay out of the
  re-verification premise set (F1), which is what keeps ③ true.

Still uncovered was the SEAM between them: ``Orchestrator._apply_governed_verification_recovery``
is the live entry point for a failed node, and the deterministic short-circuit
(``core/orchestrator.py:2578-2580``) sits behind several bookkeeping steps.  These tests
drive that entry point with the very ``Verifier`` the orchestrator owns, and assert:

* a real ``VerificationResult`` carrying the Verifier's ``report_gap`` is dispatched
  deterministically — with ``llm_client=None`` the call would raise
  ``A real LLM client is required for verification recovery`` had it reached the planner,
  so returning an applied patch is proof the LLM was bypassed (m02774 ①);
* the failing node stays unfinished (``repair_pending``): what is reused is execution
  evidence, never the acceptance verdict (m02774 ②);
* the anchored functional execution is untouched — its receipt count does not grow and the
  successor is structurally forbidden from running tests — so a green receipt is never paid
  for twice; once the narrow completion lands the report, the SAME receipt re-verifies clean
  with no staleness (m02774 ③);
* the contrast case still routes to the planner: an ordinary failure must not be hijacked
  by the deterministic report path.

Offline only: ``tmp_path``, injected receipts, no LLM, no subprocess, no network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from AstralNexus.core.task_graph import TaskNode
from AstralNexus.core.verifier import VerificationResult, VerificationVerdict
from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files

PRODUCT = "deliverables/app/product.py"
TEST_SCRIPT = "tests/test_product.py"
REPORT = "deliverables/runs/browser/defects.json"
GREEN_CMD = "python -m pytest tests -q"
PASSING_SUMMARY = "===== 12 passed in 0.42s ====="
NODE_ID = "N-501"
SUCCESSOR_ID = "RC-N-501"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _receipt(files: Dict[str, Any], *, receipt_id: str, status: str = "success",
             returncode: Optional[int] = 0, stdout_tail: str = PASSING_SUMMARY) -> Dict[str, Any]:
    """A recorded execution observation, same shape as the store writes."""
    return {
        "tool": "terminal_exec",
        "call_id": "call-report-only-gap-1",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": dict(files),
        "receipt_id": receipt_id,
        "arguments": json.dumps({"command": GREEN_CMD}),
    }


def _anchor_execution(root: Path, node_id: str) -> str:
    """One anchored functional execution, exactly one receipt, no re-runs."""
    _write(root / PRODUCT, "def add(a, b):\n    return a + b\n")
    _write(root / TEST_SCRIPT, "def test_add():\n    assert True\n")
    inventory = snapshot_files(root, [PRODUCT, TEST_SCRIPT])
    store = ExecutionStore(root)
    execution = store.begin(
        node_id,
        {
            "owned_paths": [PRODUCT, TEST_SCRIPT],
            "upstream_artifacts": [],
            "acceptance_criteria": [f"tests_passed:{GREEN_CMD}"],
        },
    )
    store.append(execution, _receipt(inventory, receipt_id=f"rcpt-{node_id}"))
    store.finish(execution, "succeeded", {"summary": "functional suite green"})
    return execution


def _receipt_count(root: Path, execution: str) -> int:
    """How many tool executions this anchored evidence holds (a re-run adds one)."""
    return len(ExecutionStore(root).get(execution)["receipts"])


def _execution_row_count(root: Path) -> int:
    """How many runtime executions the ledger holds (a new Worker adds one)."""
    db = sqlite3.connect(root / ".astralnexus_state" / "executions.sqlite")
    try:
        return int(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0])
    finally:
        db.close()


def _seed_failing_node(orchestrator, execution: str) -> TaskNode:
    """A node whose functional evidence is green and whose report is owed."""
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
            "expected_outputs": [{
                "output_id": "EO-01",
                "name": "structured defect report",
                "type": "file",
                "path": REPORT,
                "required": True,
                "source": "declared",
            }],
        },
    )
    graph.add_node(node)
    graph.add_node(TaskNode(
        node_id="C-200",
        goal="sign off the release",
        role="reviewer",
        status="waiting_dependencies",
        dependencies=[NODE_ID],
    ))
    return node


def _seam_result(orchestrator, node: TaskNode) -> VerificationResult:
    """The VerificationResult the orchestrator would hand to recovery.

    Assembled the same way ``Verifier.verify_node`` assembles it (see
    ``core/verifier.py:227-228``): the propagated ``report_gap`` is the first dimension's.
    """
    # The live orchestrator binds the Verifier to the run workspace; do the same here.
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


def _applied_payload(orchestrator) -> Dict[str, Any]:
    events = [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "orchestrator.verification_recovery.applied"
    ]
    assert events, "the deterministic dispatch must record the applied trace"
    return events[-1]


def test_report_only_gap_is_completed_in_place_at_the_seam_without_the_llm(tmp_path: Path) -> None:
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.verification_identity import is_report_class_path

    with Orchestrator(tmp_path, run_id="report-only-gap-seam") as orchestrator:
        execution = _anchor_execution(tmp_path, NODE_ID)
        node = _seed_failing_node(orchestrator, execution)
        before = _receipt_count(tmp_path, execution)
        before_nodes = len(orchestrator.task_graph.nodes)
        before_executions = _execution_row_count(tmp_path)
        assert before == 1

        result = _seam_result(orchestrator, node)
        gap = result.report_gap
        assert gap is not None, "the framework did not classify this as a report-only gap"
        assert gap["reason"] == "REPORT_ONLY_GAP"
        assert list(gap["missing_reports"]) == [REPORT]
        assert gap["reuse_execution_id"] == execution
        assert is_report_class_path(REPORT)

        # llm_client is None: reaching the planner would raise instead of returning.
        applied = orchestrator._apply_governed_verification_recovery(node, result)

        # 用户 2026-10-06 第⑤条：本执行内补齐——新增节点数 0、新增执行数 0。
        assert applied["accepted"] is True
        assert applied["added"] == []
        assert applied["updated"] == []
        assert applied["adds_nodes"] == 0
        assert applied["adds_executions"] == 0
        assert applied["written"] == [REPORT]
        plan = applied["plan"]
        assert plan["kind"] == "report_local_completion"
        # The completed artifact set is exactly the missing report -- no product or
        # test path may be touched by the completion's own plan.
        assert plan["owned_paths"] == [REPORT]
        assert all(is_report_class_path(path) for path in plan["owned_paths"])
        assert plan["rerun_functional_tests"] is False
        assert plan["executes_tests"] is False
        assert orchestrator.task_graph.get_node(SUCCESSOR_ID) is None
        assert len(orchestrator.task_graph.nodes) == before_nodes
        assert _execution_row_count(tmp_path) == before_executions

        # The report is on disk and cites the anchored execution's own receipt.
        document = json.loads((tmp_path / REPORT).read_text(encoding="utf-8"))
        assert document["reuse_execution_id"] == execution
        assert document["rerun_functional_tests"] is False
        assert document["executed_tests"] is False

        # ② evidence reuse is not a verdict flip: the failed node still blocks delivery.
        failed = orchestrator.task_graph.get_node(NODE_ID)
        assert failed is not None
        assert failed.status == "repair_pending"
        assert failed.status != "completed"
        # ③ the green receipt was not paid for twice.
        assert _receipt_count(tmp_path, execution) == before

        payload = _applied_payload(orchestrator)
        assert payload["recovery_type"] == "report_completion"
        assert payload["reuse_execution_id"] == execution
        assert payload["reuse_execution_evidence"] is True
        assert payload["rerun_functional_tests"] is False
        assert payload["missing_reports"] == [REPORT]
        assert payload["local_completion"] is True
        assert payload["adds_nodes"] == 0
        assert payload["adds_executions"] == 0
        assert payload["gap_cleared"] is True


def test_narrow_completion_lands_the_report_without_re_running_the_tests(tmp_path: Path) -> None:
    """m02774 ③ at the seam: the completion itself lands the report on the SAME receipt."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="report-only-gap-reverify") as orchestrator:
        execution = _anchor_execution(tmp_path, NODE_ID)
        node = _seed_failing_node(orchestrator, execution)
        before = _receipt_count(tmp_path, execution)

        first = _seam_result(orchestrator, node)
        assert first.report_gap is not None
        applied = orchestrator._apply_governed_verification_recovery(node, first)

        # The completion writes ONLY the report (its whole owned_paths), in place.
        assert applied["written"] == [REPORT]
        assert (tmp_path / REPORT).is_file()
        assert orchestrator.task_graph.get_node(SUCCESSOR_ID) is None

        second = _seam_result(orchestrator, node)
        assert second.report_gap is None, (
            "the report exists but the gap persisted: "
            f"{second.report_gap!r}"
        )
        dimension = second.dimensions[0]
        assert dimension.passed is True, f"{dimension.observations!r}"
        functional = [
            observation for observation in dimension.observations
            if observation.get("kind") == "tests_passed"
        ]
        assert functional and functional[0]["holds"] is True
        assert functional[0]["reason"] != "TEST_EXECUTION_STALE", (
            "backfilling the report invalidated the functional receipt: "
            f"{functional[0]!r}"
        )
        # No functional test was executed again to make the report verifiable.
        assert _receipt_count(tmp_path, execution) == before


def test_seam_without_a_report_gap_still_requires_the_llm_planner(tmp_path: Path) -> None:
    """The deterministic route keys on the Verifier's classification, never on a guess."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="report-only-gap-contrast") as orchestrator:
        execution = _anchor_execution(tmp_path, NODE_ID)
        node = _seed_failing_node(orchestrator, execution)
        stripped = _seam_result(orchestrator, node)
        # Same missing report, but the framework classification is absent: this is an
        # ordinary failure and must go to the LLM planner.  No client is configured, so the
        # planner raises and the pre-existing terminal rule applies -- the point here is that
        # the deterministic report path did NOT take over.
        stripped.report_gap = None

        outcome = orchestrator._apply_governed_verification_recovery(node, stripped)

        assert outcome["accepted"] is False and outcome["rejected"] is True
        assert "recovery_type" not in outcome
        assert orchestrator.task_graph.get_node(SUCCESSOR_ID) is None
        rejected = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.verification_recovery.rejected"
        ]
        assert rejected, "the ordinary failure never reached the LLM planner"
        assert any(
            "A real LLM client is required" in str(item.get("error") or "")
            for item in rejected
        ), f"the planner was not the one that refused: {rejected!r}"
        # The pre-existing terminal rule for an unplannable failure is unchanged by this
        # round; only the framework-classified report gap keeps a node alive with a successor.
        assert node.metadata["_terminal_failure"] is True
        assert _receipt_count(tmp_path, execution) == 1