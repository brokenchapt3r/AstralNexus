"""F-C inversion tests (P4 Round-1 fix): GlobalVerifier gap reconciliation.

Round-1 crash chain background: P4 Round 1 completed 8/8 nodes and 34/34
delivery criteria, but the GlobalVerifier failed on the stale G1 gap
(claiming real-server Playwright e2e evidence was missing) even though
``run1.log`` in the SAME run already contained that evidence.  The fail
triggered a replan patch whose input resolution then crashed the whole
turn.  F-C reconciles a blocking evidence gap against evidence another
group already produced BEFORE the failure escalates — deterministic
(regex + substring + graph lookups, no LLM) and conservative
(宁可漏对账，不可错对账: a gap survives unless every demanded path literal
is verifiable).

Fixtures are trimmed from the real round-1 run
(P4_ROUND1_REPORT.md / p4_round1_snapshot) with no runtime dependency on
``02_运行产物``.
"""

from pathlib import Path
import hashlib

from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.task_graph import Artifact, ArtifactAcceptanceStatus, TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder


def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="N-000-plan",
        goal="交付工单管理页面 demo 并完成真实服务端联调",
        role="planner",
        status="completed",
    ))
    for ident in ["N-G-1", "N-103-verify"]:
        graph.add_node(TaskNode(node_id=ident, goal="verified fixture", role="tester", status="completed", metadata={"task_version": 1}))
    fixture = tmp_path / "node_io/N-102-ui-impl_output.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("UI fixture: 22 assertions against a temporary stub", encoding="utf-8")
    graph.add_artifact(Artifact(artifact_id="A-G1", node_id="N-G-1", artifact_type="file", path="node_io/N-102-ui-impl_output.json", description="UI fixture",
        acceptance_status=ArtifactAcceptanceStatus.ACCEPTED, metadata={"task_version": 1, "content_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}))
    recorder = TraceRecorder(tmp_path)
    return graph, GlobalGovernor(graph, recorder), recorder


# --- round-1 G1 gap, trimmed verbatim from the real reducer summary ---------

G1_GAP = (
    "页面与真实服务端（deliverables/app/server.py）的端到端联调尚无证据："
    "N-102 的 22 项 Playwright 断言跑在『契约等价』的临时桩 Flask 服务上"
    "（当时 server.py 尚不存在），N-101 只对 GET / 做了字节/长度与 "
    "data-testid 的静态核对，未在真实服务上跑完整页面流程…"
)

G1_EVIDENCE_NEEDED = (
    "在真实 server.py（TICKETS_DB 指向独立临时库）+ Playwright Chromium 上"
    "复跑页面断言，并留存命令输出：初始 ticket-count 文本与 ticket-list "
    "条目数、提交后 count +1 与新工单出现、重复 external_id 时 "
    "[data-testid=error] 可见非空文本。"
)

G1_COVERAGE_GAP = {
    "gap": G1_GAP,
    "severity": "major",
    "required_for_delivery": True,
    "blocking_stage": "evidence",
    "evidence_needed": G1_EVIDENCE_NEEDED,
}

G1_DERIVED_CLAIM = {
    "claim": "页面 UI 断言在契约等价的桩 Flask 服务上全部通过",
    "evidence_refs": ["node_io/N-102-ui-impl_output.json"],
    "confidence": 0.8,
    "epistemic_status": "derived",
}

# --- round-1 run1.log key lines, trimmed ------------------------------------

RUN1_LOG_REF = "deliverables/runs/verify_logs/run1.log"

RUN1_LOG_LINES = [
    "[verify] start method: python deliverables/app/server.py (port 8099, TICKETS_DB -> temp)",
    "[verify] === Playwright/Chromium assertions for 3.2 (ui_before=2) ===",
    "PASS UI-001 initial ticket-count text == ticket-list entries",
    "PASS UI-002 submit adds ticket and count +1",
    "PASS UI-003 duplicate external_id shows error",
    "PASS UI-004 [data-testid=error] visible with non-empty text",
    "[verify] assertions: passed=31 failed=0 total=31",
    "CASE-OK",
]

G2_OBSERVED_CLAIM = {
    "claim": "页面与真实服务端的端到端联调已在真实 server.py 上复跑并全部通过",
    "evidence_refs": [RUN1_LOG_REF],
    "confidence": 0.9,
    "epistemic_status": "observed",
}


def _write_log(run_dir: Path, lines, graph: TaskGraph) -> Path:
    path = run_dir / "deliverables" / "runs" / "verify_logs" / "run1.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    graph.add_artifact(Artifact(artifact_id="A-N-103-verify-run1", node_id="N-103-verify", artifact_type="file", path=RUN1_LOG_REF, description="verify fixture",
        acceptance_status=ArtifactAcceptanceStatus.ACCEPTED, metadata={"task_version": 1, "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    return path


def _reducer(group_id, claims, gaps):
    return {
        "group_id": group_id,
        "source_nodes": ["N-103-verify" if group_id == "G-2" else f"N-{group_id}"],
        "verified_claims": claims,
        "conflicts": [],
        "coverage_gaps": gaps,
        "constraints": [],
        "recommended_followups": [],
        "source_artifacts": [],
    }


def _reducers(g2_claim=G2_OBSERVED_CLAIM, g2_gaps=()):
    return {
        "G-1": _reducer("G-1", [G1_DERIVED_CLAIM], [G1_COVERAGE_GAP]),
        "G-2": _reducer("G-2", [g2_claim], list(g2_gaps)),
    }


def _contract():
    """Contract that explicitly names the real server path.

    The G1 gap mentions ``deliverables/app/server.py``; only a contract
    path (or an existing file) makes that a legitimate blocking evidence
    obligation instead of an undeclared-path diagnostic.
    """
    return {
        "required_sections": ["验证结论"],
        "acceptance_criteria": [
            {"id": "AC-3.2", "text": "页面断言必须在真实 deliverables/app/server.py 上复跑"},
        ],
        "external_evidence_policy": "required_if_explicit",
        "required_outputs": [],
    }


def _trace_payloads(recorder, event_type):
    return [
        event["payload"]
        for event in recorder.events
        if event.get("event_type") == event_type
    ]


def test_stale_realserver_gap_reconciles_against_observed_run1_log(tmp_path):
    """Taskbook test 1: the round-1 G1 gap reconciles against G2 evidence.

    The gap demanded real-server e2e evidence (deliverables/app/server.py);
    G2 already observed exactly that in run1.log.  After reconciliation:
    overall_status is not "fail" (so the layered synthesis fail->raise
    gate never fires), the gap moved to reconciled_gaps with its reason
    code, and coverage_gaps is empty — which is precisely the cli
    ``_replan_from_global_verifier`` no-replan condition (it extracts
    ``verifier.get("coverage_gaps", [])``; empty list -> return False, no
    replan patch, no crash chain).
    """
    graph, governor, recorder = _governor(tmp_path)
    _write_log(tmp_path, RUN1_LOG_LINES, graph)

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "pass"
    assert report["coverage_gaps"] == []
    assert len(report["reconciled_gaps"]) == 1
    reconciled = report["reconciled_gaps"][0]
    assert reconciled["reason_code"] == "GAP_RECONCILED_EXISTING_EVIDENCE"
    assert reconciled["reconciliation"]["demanded_paths"] == [
        "deliverables/app/server.py", "server.py",
    ]
    assert reconciled["reconciliation"]["matched_paths"] == [
        "deliverables/app/server.py", "server.py",
    ]
    assert reconciled["reconciliation"]["matched_refs"] == [RUN1_LOG_REF]
    events = _trace_payloads(recorder, "governor.global_verifier.gap_reconciled")
    assert len(events) == 1
    assert events[0]["group_id"] == "G-1"
    assert events[0]["reason_code"] == "GAP_RECONCILED_EXISTING_EVIDENCE"


def test_stub_only_evidence_keeps_gap_blocking(tmp_path):
    """Taskbook test 2 (variant a): stub evidence does not reconcile.

    The G2 log comes from the 临时桩 Flask 服务 (contract-equivalent stub)
    and never mentions the real server path, so the demanded literals are
    not matched and the gap must stay blocking.
    """
    graph, governor, recorder = _governor(tmp_path)
    stub_lines = [
        "[verify] start method: 临时桩 Flask 服务（契约等价，端口 8098）",
        "[verify] === Playwright/Chromium assertions for 3.2 (ui_before=2) ===",
        "PASS UI-001 initial ticket-count text == ticket-list entries",
        "[verify] assertions: passed=22 failed=0 total=22",
        "CASE-OK",
    ]
    _write_log(tmp_path, stub_lines, graph)

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "fail"
    assert len(report["coverage_gaps"]) == 1
    assert report["coverage_gaps"][0]["gap"] == G1_GAP
    assert report["reconciled_gaps"] == []


def test_partial_match_keeps_gap_with_diagnostic(tmp_path):
    """Taskbook test 2 (conservative boundary): partial match never drops a gap.

    宁可漏对账，不可错对账: when only some demanded literals are found the
    gap survives, now carrying a reconciliation_partial diagnostic so the
    replan decision sees exactly what was matched.
    """
    graph, governor, recorder = _governor(tmp_path)
    partial_lines = [
        "[verify] start method: python server.py (port 8099, TICKETS_DB -> temp)",
        "[verify] assertions: passed=31 failed=0 total=31",
        "CASE-OK",
    ]
    _write_log(tmp_path, partial_lines, graph)

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "fail"
    assert len(report["coverage_gaps"]) == 1
    diagnostic = report["coverage_gaps"][0].get("reconciliation_partial")
    assert diagnostic is not None
    assert diagnostic["demanded_paths"] == [
        "deliverables/app/server.py", "server.py",
    ]
    assert diagnostic["matched_paths"] == ["server.py"]


def test_old_version_evidence_is_not_verifiable(tmp_path):
    """Taskbook test 2 (variant b): stale-version evidence does not reconcile.

    run1.log is mapped to an accepted artifact, but that artifact declares
    task_version 0 while the current contract is task_version 1 — an old
    version's evidence must not satisfy the current gap.
    """
    graph, governor, recorder = _governor(tmp_path)
    _write_log(tmp_path, RUN1_LOG_LINES, graph)
    graph.add_artifact(Artifact(
        artifact_id="A-N-103-verify-run1",
        node_id="N-103-verify",
        artifact_type="file",
        path=RUN1_LOG_REF,
        description="prior verify log",
        acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
        metadata={"task_version": 0, "retry_count": 0},
    ))

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "fail"
    assert len(report["coverage_gaps"]) == 1
    assert report["reconciled_gaps"] == []


def test_superseded_producer_evidence_is_not_verifiable(tmp_path):
    """Taskbook test 2 (variant c): superseded producer's evidence does not reconcile.

    The artifact is accepted and version-current, but its producer node is
    superseded — the evidence lineage is dead and must not silently close
    a blocking gap.
    """
    graph, governor, recorder = _governor(tmp_path)
    _write_log(tmp_path, RUN1_LOG_LINES, graph)
    graph.add_node(TaskNode(
        node_id="N-103-verify", goal="静态核对", role="tester", status="superseded",
    ))
    graph.add_artifact(Artifact(
        artifact_id="A-N-103-verify-run1",
        node_id="N-103-verify",
        artifact_type="file",
        path=RUN1_LOG_REF,
        description="prior verify log",
        acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
        metadata={"task_version": 1, "retry_count": 0},
    ))

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "fail"
    assert len(report["coverage_gaps"]) == 1
    assert report["reconciled_gaps"] == []


def test_verifier_completed_payload_carries_blocking_gap_detail(tmp_path):
    """Taskbook test 6 (F-B): the global fail event is traceable to its gap.

    The round-1 VERIFIER_COMPLETED event only carried overall_status /
    group_count / failed_groups, so downstream consumers could not tell
    WHICH gap failed or how many gaps were reconciled.  event_version 2
    carries the blocking gap content and per-gap reason codes, and the
    reconciled count, so a failure report can name the gap without
    re-reading the whole report dict.
    """
    graph, governor, recorder = _governor(tmp_path)
    stub_lines = [
        "[verify] start method: 临时桩 Flask 服务（契约等价，端口 8098）",
        "[verify] assertions: passed=22 failed=0 total=22",
    ]
    _write_log(tmp_path, stub_lines, graph)

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "fail"
    completed = _trace_payloads(recorder, "governor.global_verifier.completed")
    assert len(completed) == 1
    payload = completed[0]
    assert payload["overall_status"] == "fail"
    assert payload["event_version"] == 2
    assert payload["reason_codes"] == ["EVIDENCE_COVERAGE_GAP"]
    assert payload["coverage_gap_count"] == 1
    assert payload["reconciled_gap_count"] == 0
    assert len(payload["coverage_gaps"]) == 1
    gap_detail = payload["coverage_gaps"][0]
    assert gap_detail["group_id"] == "G-1"
    assert gap_detail["gap"] == G1_GAP
    assert gap_detail["evidence_needed"] == G1_EVIDENCE_NEEDED
    assert gap_detail["required_for_delivery"] is True


def test_verifier_completed_payload_counts_reconciled_gaps(tmp_path):
    """Taskbook test 6 (companion): reconciled gaps are countable in the event.

    When the gap reconciles, the fail event still records the final
    outcome (pass) with zero blocking gaps and one reconciled gap, so
    delivery reporting can distinguish "reconciled away" from "never
    existed".
    """
    graph, governor, recorder = _governor(tmp_path)
    _write_log(tmp_path, RUN1_LOG_LINES, graph)

    report = governor.verify_evidence_summaries(
        _contract(), _reducers(), task_version=1, run_dir=tmp_path
    )

    assert report["overall_status"] == "pass"
    completed = _trace_payloads(recorder, "governor.global_verifier.completed")
    assert len(completed) == 1
    payload = completed[0]
    assert payload["overall_status"] == "pass"
    assert payload["event_version"] == 2
    assert payload["reason_codes"] == []
    assert payload["coverage_gap_count"] == 0
    assert payload["reconciled_gap_count"] == 1
    assert payload["coverage_gaps"] == []
