"""Fix BA (doc 26 §41, 2026-09-18 r13b): DeerFlow 2.0 status/stop_reason split.

Contract source: ``deer-flow@2814bd5d498a subagents/status_contract.py``
(#3875 Phase 2) — a run that ended abnormally but produced usable output stays
``completed`` and carries the cause on ``stop_reason``; only a run with NO
usable output is ``failed``.  AstralNexus derived the node status from the raw
execution boolean alone (``orchestrator`` turn boundary), so a turn that had
already produced code-verifiable output was recorded as a failure and redone —
live gen31: PATCH-R017/R018 produced the complete passing acceptance packet and
were still converted into repair branches until the run-level churn budget was
gone (20/20) with no delivery.

These tests pin (a) the fail-closed predicate over Layer 2 observations and
(b) the Orchestrator helper that consumes it, including its trace record and
the metadata stop_reason, without constructing an Orchestrator.
"""
from __future__ import annotations

from types import SimpleNamespace

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.runtime.acceptance_checks import (
    TURN_STOP_REASON_ABORTED_WITH_EVIDENCE,
    usable_partial_evidence,
)


def _obs(checked, holds, criterion_id="AC01"):
    return {
        "criterion_id": criterion_id,
        "criterion": "file:deliverables/x.json non-empty",
        "kind": "file_non_empty",
        "checked": checked,
        "holds": holds,
        "reason": "TEST_REASON",
        "detail": "detail",
    }


class _Recorder:
    def __init__(self):
        self.events = []

    def record(self, event_type, payload=None):
        self.events.append((event_type, payload or {}))


def _node(metadata=None):
    return SimpleNamespace(node_id="N-TEST", metadata=dict(metadata or {}))


def _stub(observations):
    return SimpleNamespace(
        verifier=SimpleNamespace(deterministic_observations=lambda node: observations),
        trace_recorder=_Recorder(),
    )


def _boom(node):
    raise RuntimeError("evidence probe exploded")


def test_predicate_accepts_a_holding_decided_criterion():
    assert usable_partial_evidence([_obs(True, True)]) is True
    # Undecidable criteria neither help nor hurt: the semantic layer and the
    # runtime gate still own final acceptance.
    assert usable_partial_evidence([_obs(True, True), _obs(False, None)]) is True


def test_predicate_is_fail_closed():
    assert usable_partial_evidence([]) is False
    assert usable_partial_evidence([_obs(False, None)]) is False
    # One decided failure vetoes the exemption even when another holds.
    assert usable_partial_evidence([_obs(True, True), _obs(True, False)]) is False
    # ``checked`` without a boolean verdict is not evidence either way.
    assert usable_partial_evidence([_obs(True, None)]) is False


def test_helper_records_stop_reason_and_preserves_the_cause():
    stub = _stub([_obs(True, True), _obs(False, None)])
    node = _node({
        "error_type": "transport_interrupt_budget_exhausted",
        "error": "read timeout",
    })
    reason = Orchestrator._aborted_turn_stop_reason(stub, node)
    assert reason is not None
    assert reason["code"] == TURN_STOP_REASON_ABORTED_WITH_EVIDENCE
    assert reason["cause"] == "transport_interrupt_budget_exhausted"
    assert reason["detail"] == "read timeout"
    assert reason["decided_criteria"] == 1
    assert reason["holding_criteria"] == 1
    assert node.metadata["_turn_stop_reason"] == reason
    assert stub.trace_recorder.events[-1][0] == "orchestrator.turn.aborted_but_usable"
    assert stub.trace_recorder.events[-1][1]["reason_codes"] == [
        TURN_STOP_REASON_ABORTED_WITH_EVIDENCE
    ]


def test_helper_returns_none_without_verifiable_output():
    stub = _stub([_obs(False, None), _obs(True, False)])
    node = _node()
    assert Orchestrator._aborted_turn_stop_reason(stub, node) is None
    assert "_turn_stop_reason" not in node.metadata
    assert stub.trace_recorder.events == []


def test_helper_degrades_when_the_evidence_probe_is_unavailable():
    stub = SimpleNamespace(
        verifier=SimpleNamespace(deterministic_observations=_boom),
        trace_recorder=_Recorder(),
    )
    node = _node()
    assert Orchestrator._aborted_turn_stop_reason(stub, node) is None
    assert stub.trace_recorder.events == []


# --- Wiring level (real Orchestrator, real workspace files) -----------------
# The stub tests above pin the predicate; these pin the integration the wiring
# actually depends on: Verifier.workspace_root == Orchestrator.run_dir
# (core/orchestrator.py:404) and the Layer 2 file reader resolving the node's
# declared criterion against the real run workspace.


def _real_orchestrator(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator

    orch = Orchestrator(tmp_path, run_id="ba-turn-stop")
    # `Orchestrator._configure_..._llm` performs this assignment
    # (core/orchestrator.py:404) during real boot; a unit test drives the
    # verifier directly, so it establishes the same wiring explicitly.  Live
    # evidence that it is really wired at runtime: the gen31 trace carries
    # `verifier.deterministic_acceptance` 30 times and `WORKSPACE_UNAVAILABLE`
    # zero times.
    orch.verifier.workspace_root = orch.run_dir
    return orch


def _acceptance_node(*criteria, metadata=None):
    from AstralNexus.core.task_graph import TaskNode

    return TaskNode(
        node_id="R-ACCEPT",
        goal="produce the acceptance packet",
        role="tester",
        status="running",
        acceptance_criteria=list(criteria),
        metadata=dict(metadata or {}),
    )


def test_wiring_reads_a_real_workspace_deliverable(tmp_path):
    """A real file on disk decides the leaf, so the aborted turn keeps its work."""
    with _real_orchestrator(tmp_path) as orch:
        target = tmp_path / "deliverables" / "acceptance.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"unmet_items_final": []}', encoding="utf-8")
        node = _acceptance_node(
            "file:deliverables/acceptance.json non-empty",
            metadata={
                "error_type": "transport_interrupt_budget_exhausted",
                "error": "read timeout",
            },
        )
        orch.task_graph.add_node(node)

        reason = orch._aborted_turn_stop_reason(node)

        assert reason is not None
        assert reason["code"] == TURN_STOP_REASON_ABORTED_WITH_EVIDENCE
        assert reason["cause"] == "transport_interrupt_budget_exhausted"
        assert reason["decided_criteria"] >= 1
        assert reason["holding_criteria"] >= 1
        assert node.metadata["_turn_stop_reason"] == reason


def test_wiring_stays_fail_closed_for_a_missing_deliverable(tmp_path):
    """A declared-but-absent artifact is a decided failure: no exemption."""
    with _real_orchestrator(tmp_path) as orch:
        node = _acceptance_node("file:deliverables/acceptance.json non-empty")
        orch.task_graph.add_node(node)

        assert orch._aborted_turn_stop_reason(node) is None
        assert "_turn_stop_reason" not in node.metadata