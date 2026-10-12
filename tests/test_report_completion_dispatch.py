"""Direct local-report completion contract.

Uses real Orchestrator and ExecutionStore with injected receipts. Covers exact
write scope, refusal paths, planner fallback and primary failure preservation.
End-to-end recovery routing and verdict preservation live in
``test_report_local_completion.py``; final release checks live in
``test_delivery_verification_hard_gates.py``. No model or network calls.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from AstralNexus.core.task_graph import TaskNode
from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files
from tests.report_support import (
    receipt_count as _receipt_count,
    terminal_receipt,
    write_file as _write,
    execution_row_count as _execution_row_count,
    stored_receipt_id as _stored_receipt_id,
)

REPORT_DIR = "deliverables/runs/browser"
MISSING_REPORT = f"{REPORT_DIR}/defects.json"
SECOND_REPORT = f"{REPORT_DIR}/run-summary.json"
NON_REPORT_ARTIFACT = f"{REPORT_DIR}/search.json"
PRODUCT = "deliverables/app/product.py"
TEST_SCRIPT = "tests/test_product.py"
GREEN_CMD = "python -m pytest tests -q"
PASSING_SUMMARY = "===== 12 passed in 0.42s ====="
NODE_ID = "N-107"


def _receipt(files, *, receipt_id: str, status: str = "success", returncode=0):
    return terminal_receipt(files, command=GREEN_CMD, call_id="call-report-local-completion-1",
                            receipt_id=receipt_id, status=status, returncode=returncode,
                            stdout_tail=PASSING_SUMMARY)


def _anchor_execution(root, node_id: str = NODE_ID) -> str:
    """One anchored functional execution: exactly one receipt, no re-runs."""
    _write(root, PRODUCT, "def add(a, b):\n    return a + b\n")
    _write(root, TEST_SCRIPT, "def test_add():\n    assert True\n")
    inventory = snapshot_files(root, [PRODUCT, TEST_SCRIPT])
    store = ExecutionStore(root)
    execution = store.begin(
        node_id,
        {
            "owned_paths": [PRODUCT, TEST_SCRIPT, f"{REPORT_DIR}/"],
            "upstream_artifacts": [],
            "acceptance_criteria": [f"tests_passed:{GREEN_CMD}"],
        },
    )
    store.append(execution, _receipt(inventory, receipt_id=f"rcpt-{node_id}"))
    store.finish(execution, "succeeded", {"summary": "functional suite green"})
    return execution


def _graph_node_ids(graph) -> list:
    return [item.node_id for item in graph.nodes]


def _seed_failing_node(orchestrator, execution: str):
    """A node whose functional evidence is green and whose reports are owed."""
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id=NODE_ID,
        goal="capture the browser verification reports",
        role="tester",
        status="repair_pending",
        acceptance_criteria=[f"tests_passed:{GREEN_CMD}"],
        owned_paths=[PRODUCT, TEST_SCRIPT, f"{REPORT_DIR}/", NON_REPORT_ARTIFACT],
        output_artifacts=["artifact:browser-run"],
        metadata={
            "runtime_execution_id": execution,
            "error_type": "MissingDeclaredArtifacts",
            "expected_outputs": [
                {"output_id": "EO-01", "name": "structured defect report",
                 "type": "file", "path": MISSING_REPORT, "required": True,
                 "source": "declared"},
                {"output_id": "EO-02", "name": "run summary",
                 "type": "file", "path": SECOND_REPORT, "required": True,
                 "source": "declared"},
            ],
        },
    ))
    graph.add_node(TaskNode(
        node_id="C-200",
        goal="sign off the release",
        role="reviewer",
        status="waiting_dependencies",
        dependencies=[NODE_ID],
    ))
    graph.add_node(TaskNode(
        node_id="C-201",
        goal="already finished downstream node",
        role="reviewer",
        status="completed",
        dependencies=[NODE_ID],
    ))
    return graph


def _framework_gap(orchestrator, node) -> dict:
    """The gap the Verifier itself classifies for this node (no hand-made claim)."""
    orchestrator.verifier.workspace_root = orchestrator.run_dir
    # Explicit business audit fixture; this is not the default run policy.
    orchestrator.verifier.business_acceptance_checks = True
    dimension = orchestrator.verifier._verify_deterministic_acceptance(node)
    assert dimension is not None, "the deterministic dimension must exist"
    gap = getattr(dimension, "report_gap", None)
    assert gap is not None, "the framework did not classify this as a report-only gap"
    return dict(gap)


def _complete(orchestrator, node, verdict, execution=None, gap=None):
    from AstralNexus.core.verifier import VerificationVerdict  # noqa: F401

    if gap is None:
        gap = _framework_gap(orchestrator, node)
    return orchestrator._complete_report_gap_locally(
        node,
        SimpleNamespace(verdict=verdict, overall_score=0.4, report_gap=dict(gap)),
        dict(gap),
    )


def test_local_completion_writes_only_the_missing_report_paths(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.verifier import VerificationVerdict
    from AstralNexus.runtime.verification_identity import is_report_class_path

    with Orchestrator(tmp_path, run_id="report-local-completion") as orchestrator:
        execution = _anchor_execution(tmp_path)
        graph = _seed_failing_node(orchestrator, execution)
        node = graph.get_node(NODE_ID)
        before_nodes = len(graph.nodes)
        before_executions = _execution_row_count(tmp_path)
        script_before = (tmp_path / TEST_SCRIPT).read_text(encoding="utf-8")
        product_before = (tmp_path / PRODUCT).read_text(encoding="utf-8")
        gap = _framework_gap(orchestrator, node)
        assert gap["reason"] == "REPORT_ONLY_GAP"
        assert gap["reuse_execution_id"] == execution
        assert gap["rerun_functional_tests"] is False
        assert gap["reuse_execution_evidence"] is True
        assert sorted(gap["missing_reports"]) == [MISSING_REPORT, SECOND_REPORT]
        assert all(is_report_class_path(path) for path in gap["missing_reports"])

        result = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                           execution=execution, gap=gap)

        assert result["accepted"] is True
        assert result["rejected"] is False
        assert sorted(result["written"]) == [MISSING_REPORT, SECOND_REPORT]
        assert result["skipped_existing"] == []
        assert result["reuse_execution_id"] == execution
        assert result["reuse_execution_evidence"] is True
        assert result["rerun_functional_tests"] is False
        assert result["adds_nodes"] == 0
        assert result["adds_executions"] == 0
        assert result["added"] == []
        assert result["updated"] == []

        plan = result["plan"]
        assert plan["kind"] == "report_local_completion"
        assert sorted(plan["owned_paths"]) == [MISSING_REPORT, SECOND_REPORT]
        assert is_report_class_path(plan["owned_paths"][0])
        assert plan["reuse_execution_id"] == execution
        assert plan["reuse_execution_evidence"] is True
        assert plan["rerun_functional_tests"] is False
        assert plan["executes_tests"] is False
        assert plan["adds_nodes"] == 0
        assert plan["adds_executions"] == 0

        # Exactly the owed reports landed -- not the product file, not the test
        # script, and not the non-report artifact the node also owns.
        assert (tmp_path / MISSING_REPORT).is_file()
        assert (tmp_path / SECOND_REPORT).is_file()
        assert not (tmp_path / NON_REPORT_ARTIFACT).exists()
        assert (tmp_path / TEST_SCRIPT).read_text(encoding="utf-8") == script_before
        assert (tmp_path / PRODUCT).read_text(encoding="utf-8") == product_before

        # The document is a provenance record citing the anchored receipt; it
        # states no business conclusion of its own.
        document = json.loads((tmp_path / MISSING_REPORT).read_text(encoding="utf-8"))
        assert document["report_kind"] == "framework_local_report_completion"
        assert document["provenance"] == "anchored_execution_receipts"
        assert document["report_path"] == MISSING_REPORT
        assert document["node_id"] == NODE_ID
        assert document["reuse_execution_id"] == execution
        assert document["reuse_execution_evidence"] is True
        assert document["rerun_functional_tests"] is False
        assert document["executed_tests"] is False
        assert [item["receipt_id"] for item in document["evidence"]] == [
            _stored_receipt_id(tmp_path, execution)
        ]
        assert document["evidence"][0]["command"] == GREEN_CMD
        assert document["evidence"][0]["status"] == "success"
        assert document["evidence"][0]["returncode"] == 0

        # No new graph node and no new execution record: the whole-node
        # "missing report => new Worker" reaction is gone.
        assert len(graph.nodes) == before_nodes
        assert _execution_row_count(tmp_path) == before_executions
        assert all(not node_id.startswith("RC-") for node_id in _graph_node_ids(graph))

        # The failing node still blocks delivery; nothing was handed over and
        # nothing new appeared for its consumers to wait on.
        failed = graph.get_node(NODE_ID)
        assert failed.status == "repair_pending"
        assert failed.status != "completed"
        assert failed.owned_paths == [
            PRODUCT, TEST_SCRIPT, f"{REPORT_DIR}/", NON_REPORT_ARTIFACT,
        ]
        assert "ownership_transfer_to" not in failed.metadata
        assert [entry["path"] for entry in failed.metadata["expected_outputs"]] == [
            MISSING_REPORT, SECOND_REPORT,
        ]
        assert graph.get_node("C-200").dependencies == [NODE_ID]
        assert graph.get_node("C-201").dependencies == [NODE_ID]

        # The green receipt was not paid for twice, and re-verifying the SAME
        # receipt after the report landed finds no gap and no staleness.
        assert _receipt_count(tmp_path, execution) == 1
        assert result["gap_cleared"] is True
        assert getattr(
            orchestrator.verifier._verify_deterministic_acceptance(node), "passed", False
        ) is True


def test_repeat_completion_is_refused_and_non_report_paths_are_refused(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.verifier import VerificationVerdict

    with Orchestrator(tmp_path, run_id="report-local-completion-brakes") as orchestrator:
        execution = _anchor_execution(tmp_path)
        graph = _seed_failing_node(orchestrator, execution)
        node = graph.get_node(NODE_ID)
        first = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                          execution=execution)
        assert first["accepted"] is True
        assert sorted(first["written"]) == [MISSING_REPORT, SECOND_REPORT]
        landed = (tmp_path / MISSING_REPORT).read_text(encoding="utf-8")

        # 用户第⑤条：无修改、无新事实的重复恢复必须被拒绝，并给出明确阻塞原因。
        second = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                           execution=execution, gap=_framework_gap_placeholder(first))
        assert second["accepted"] is False
        assert second["rejected"] is True
        assert "repeated recovery refused" in second["error"]
        assert second["blocking_reason"] == second["error"]
        assert "written" not in second
        assert second["adds_nodes"] == 0
        assert second["adds_executions"] == 0
        # The report was not rewritten and the node still blocks delivery.
        assert (tmp_path / MISSING_REPORT).read_text(encoding="utf-8") == landed
        assert node.status == "repair_pending"
        assert all(not node_id.startswith("RC-") for node_id in _graph_node_ids(graph))

        # A verification gap must never become an ownership grab on product code.
        hijack = dict(_framework_gap_placeholder(first))
        hijack["missing_reports"] = ["deliverables/app/backend/init_db.py"]
        grabbed = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                            execution=execution, gap=hijack)
        assert grabbed["accepted"] is False
        assert grabbed["rejected"] is True
        assert "may only own report-class artifacts" in grabbed["error"]
        assert not (tmp_path / "deliverables/app/backend/init_db.py").exists()
        assert all(not node_id.startswith("RC-") for node_id in _graph_node_ids(graph))


def _framework_gap_placeholder(result: dict) -> dict:
    """The gap the first completion answered, rebuilt from its own return value."""
    return {
        "reason": "REPORT_ONLY_GAP",
        "missing_reports": list(result["missing_reports"]),
        "reuse_execution_id": result["reuse_execution_id"],
        "reuse_execution_evidence": True,
        "rerun_functional_tests": False,
    }


def test_non_report_failure_still_requires_llm_planner(tmp_path):
    """A plain failure must NOT be hijacked by the deterministic path."""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="report-local-contrast") as orchestrator:
        with pytest.raises(
            ValueError, match="A real LLM client is required for verification recovery"
        ):
            orchestrator.governor.plan_verification_recovery(
                node_id=NODE_ID,
                verdict="repair_required",
                overall_score=0.4,
                dimensions=[],
            )


def test_local_completion_records_the_section9_payload_without_new_nodes(tmp_path):
    """The orchestrator short-circuit emits the §9 payload and calls no LLM."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.verifier import VerificationVerdict

    with Orchestrator(tmp_path, run_id="report-local-payload") as orchestrator:
        execution = _anchor_execution(tmp_path)
        graph = _seed_failing_node(orchestrator, execution)
        node = graph.get_node(NODE_ID)
        before_nodes = len(graph.nodes)
        applied = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                            execution=execution)
        assert applied["accepted"] is True
        payloads = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"] == "orchestrator.verification_recovery.applied"
        ]
        assert payloads, "the deterministic completion must record the applied trace"
        payload = payloads[-1]
        assert payload["node_id"] == NODE_ID
        assert payload["verdict"] == VerificationVerdict.REPAIR_REQUIRED.value
        assert payload["score"] == 0.4
        assert payload["attempt"] == 1
        assert payload["recovery_type"] == "report_completion"
        assert payload["reuse_execution_id"] == execution
        assert payload["failure_code"] == "MissingDeclaredArtifacts"
        assert sorted(payload["missing_reports"]) == [MISSING_REPORT, SECOND_REPORT]
        assert payload["reuse_execution_evidence"] is True
        assert payload["rerun_functional_tests"] is False
        assert payload["local_completion"] is True
        assert payload["adds_nodes"] == 0
        assert payload["adds_executions"] == 0
        assert payload["patch"] == {"added": [], "updated": []}
        # No event type was added for the new shape: the deleted whole-node
        # dispatch neither planned nor executed anything.
        event_types = {event["event_type"] for event in orchestrator.trace_recorder.events}
        assert "governor.report_completion.planned" not in event_types
        assert len(graph.nodes) == before_nodes
        assert graph.get_node(NODE_ID).status == "repair_pending"


def test_uncitable_completion_is_terminal_not_llm_fallback(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.verifier import VerificationVerdict

    with Orchestrator(tmp_path, run_id="report-local-terminal") as orchestrator:
        execution = _anchor_execution(tmp_path)
        graph = _seed_failing_node(orchestrator, execution)
        node = graph.get_node(NODE_ID)
        before_executions = _execution_row_count(tmp_path)

        # No anchored execution id at all.
        no_id = _framework_gap_placeholder({
            "missing_reports": [MISSING_REPORT],
            "reuse_execution_id": "",
        })
        outcome = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                            execution=execution, gap=no_id)
        assert outcome["accepted"] is False
        assert outcome["rejected"] is True
        assert outcome["recovery_type"] == "report_completion"
        assert "no anchored execution id to cite" in outcome["error"]

        # An execution id the ledger does not know.
        unknown = _framework_gap_placeholder({
            "missing_reports": [MISSING_REPORT],
            "reuse_execution_id": "deadbeefdeadbeefdeadbeefdeadbeef",
        })
        second = _complete(orchestrator, node, VerificationVerdict.REPAIR_REQUIRED,
                           execution=execution, gap=unknown)
        assert second["accepted"] is False
        assert second["rejected"] is True
        assert "no anchored execution record to cite" in second["error"]
        assert second["blocking_reason"] == second["error"]

        rejected = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.report_completion.rejected"
        ]
        assert rejected, "the refusal must be recorded on the existing event type"
        assert rejected[-1]["action"] == "terminal"

        # Terminally refused: nothing written, no node, no execution, no flip.
        assert not (tmp_path / MISSING_REPORT).exists()
        assert _execution_row_count(tmp_path) == before_executions
        assert all(not node_id.startswith("RC-") for node_id in _graph_node_ids(graph))
        assert node.status == "repair_pending"
        assert node.status != "completed"


def test_missing_artifact_verdict_preserves_budget_primary_reason(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="budget-primary-reason") as orchestrator:
        response = SimpleNamespace(
            metrics={
                "runtime_status": "succeeded",
                "task_execution_budget": {
                    "max_tool_calls": 12,
                    "tool_calls_used": 12,
                    "max_seconds": 900,
                    "elapsed_seconds": 120,
                    "exhausted_reason": "max_tool_calls",
                },
            },
            content="",
            artifacts=[],
        )
        verdict = orchestrator._apply_missing_artifact_verdict(
            response,
            [MISSING_REPORT],
            workspace_delta={"classification": "capped_zero_output"},
        )
        assert verdict["runtime_status"] == "budget_exhausted"
        assert verdict["primary_failure_reason"] == "max_tool_calls"
        assert verdict["additional_gaps"] == {
            "missing_declared_artifacts": [MISSING_REPORT]
        }
        assert verdict["missing_outputs"] == [MISSING_REPORT]
        assert verdict["workspace_delta_classification"] == "capped_zero_output"
        assert verdict["error_type"] == "MissingDeclaredArtifacts"
        assert response.metrics["primary_failure_reason"] == "max_tool_calls"
        assert response.metrics["missing_outputs"] == [MISSING_REPORT]


def test_missing_artifact_verdict_without_primary_reason_is_unchanged(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="budget-primary-absent") as orchestrator:
        response = SimpleNamespace(
            metrics={"runtime_status": "succeeded"},
            content="",
            artifacts=[],
        )
        verdict = orchestrator._apply_missing_artifact_verdict(
            response, [NON_REPORT_ARTIFACT], workspace_delta=None
        )
        assert verdict["runtime_status"] == "failed"
        assert verdict["execution_path"] == "artifact_postcondition_failed"
        assert verdict["error_type"] == "MissingDeclaredArtifacts"
        assert verdict["missing_outputs"] == [NON_REPORT_ARTIFACT]
        assert "primary_failure_reason" not in verdict
        assert "additional_gaps" not in verdict
        assert response.metrics["runtime_status"] == "failed"