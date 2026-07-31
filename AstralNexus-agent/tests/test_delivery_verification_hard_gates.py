import json
from datetime import datetime, timezone

from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import VerificationVerdict, Verifier
from AstralNexus.runtime.trace import TraceRecorder


class SemanticEvidenceClient:
    def chat(self, messages):
        prompt = messages[-1]["content"]
        assert "deliverables/runs/evidence.txt" in prompt
        return json.dumps([
            {
                "criterion_id": "AC-EVIDENCE-01",
                "status": "pass",
                "explanation": "artifact contains the requested runtime evidence",
                "evidence_refs": ["deliverables/runs/evidence.txt"],
            }
        ])


class MalformedRecoveryClient:
    def chat(self, messages):
        return json.dumps({
            "add_nodes": [{
                "node_id": "repair-node",
                "goal": "implement the missing behavior",
                "task_type": "implementation",
                "dependencies": ["failed-node"],
                "acceptance_criteria": ["real source exists"],
            }],
            "update_nodes": [{"node_id": "failed-node", "status": "repair_pending"}],
            "add_edges": [{"source": None, "target": None, "edge_type": None}],
            "reason_codes": ["VERIFICATION_GAP"],
        })


def test_delivery_verifier_blocks_machine_report_with_failures(tmp_path):
    report_path = tmp_path / "deliverables" / "runs" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps({
            "summary": {"total": 3, "passed": 2, "failed": 1, "errors": 0},
            "tests": [{"name": "core flow", "passed": False}],
        }),
        encoding="utf-8",
    )
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))

    result = governor.verify_delivery(
        {"required_outputs": [], "global_acceptance_criteria": []},
        content="runtime delivery",
        artifact_refs=["deliverables/runs/report.json"],
        run_dir=tmp_path,
    )

    assert result["overall_status"] != "pass"
    assert result["synthesis_authorized"] is False
    assert "AC-EXECUTABLE-TESTS" in result["failed_tests"]


def test_delivery_verifier_can_semantically_bind_completion_evidence(tmp_path):
    evidence_path = tmp_path / "deliverables" / "runs" / "evidence.txt"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text("HTTP 200\ncurl http://127.0.0.1/health\n", encoding="utf-8")
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))

    result = governor.verify_delivery(
        {
            "required_outputs": [],
            "global_acceptance_criteria": [],
            "completion_evidence": ["健康检查与命令调用的真实运行证据"],
        },
        content="runtime delivery",
        artifact_refs=["deliverables/runs/evidence.txt"],
        run_dir=tmp_path,
        client=SemanticEvidenceClient(),
    )

    assert result["overall_status"] == "pass"
    assert result["synthesis_authorized"] is True
    evidence = next(
        item for item in result["criterion_results"]
        if item["criterion_id"] == "AC-EVIDENCE-01"
    )
    assert evidence["evidence_refs"] == ["deliverables/runs/evidence.txt"]


def test_delivery_snapshot_uses_relative_refs_and_prioritizes_evidence(tmp_path):
    orchestrator = Orchestrator(tmp_path)
    source = tmp_path / "deliverables" / "app" / "large.md"
    source.parent.mkdir(parents=True)
    source.write_text("source-body\n" * 20000, encoding="utf-8")
    report = tmp_path / "deliverables" / "runs" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"summary":{"failed":0}}', encoding="utf-8")

    content, refs = orchestrator._delivery_progress_snapshot()

    assert "deliverables/runs/report.json" in refs
    assert all(not ref.startswith(str(tmp_path)) for ref in refs)
    assert content.index("deliverables/runs/report.json") < content.index("source-body")


def test_same_delivery_gap_blocks_before_third_duplicate_patch(tmp_path, monkeypatch):
    orchestrator = Orchestrator(tmp_path)
    root_id = orchestrator.initialize_task_graph("build delivery", [])
    orchestrator.task_graph.get_node(root_id).status = "completed"
    orchestrator.governor.delivery_contract = {
        "required_outputs": [],
        "global_acceptance_criteria": ["must have evidence"],
    }
    report = {
        "overall_status": "partial",
        "synthesis_authorized": False,
        "coverage_gaps": [],
        "required_fixes": [],
        "failed_tests": ["AC-EVIDENCE-01"],
    }
    monkeypatch.setattr(
        orchestrator.governor, "verify_delivery", lambda *args, **kwargs: dict(report)
    )
    planned = []

    def plan(*args, **kwargs):
        node_id = f"repair-{len(planned) + 1}"
        planned.append(node_id)
        return [node_id]

    monkeypatch.setattr(orchestrator, "plan_task_decomposition", plan)

    assert orchestrator._assess_delivery_completion("build delivery")["changed"] is True
    assert orchestrator._assess_delivery_completion("build delivery")["changed"] is True
    third = orchestrator._assess_delivery_completion("build delivery")

    assert third["blocked"] is True
    assert third["repeat_count"] == 3
    assert planned == ["repair-1", "repair-2"]


def test_node_verifier_rejects_declared_failed_tests(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="test-node",
        goal="run end to end tests",
        role="tester",
        status="result_submitted",
        metadata={
            "evidence_packet": {
                "summary": "测试结果: 22/23 passed，1 个测试失败但非阻塞",
                "claims": ["tests executed"],
                "evidence_refs": ["deliverables/runs/report.json"],
                "execution_outcome": "succeeded",
            }
        },
    ))
    verifier = Verifier(graph, TraceRecorder(tmp_path))

    result = verifier.verify_node("test-node")

    assert result.verdict != VerificationVerdict.APPROVED
    execution = next(item for item in result.dimensions if item.name == "execution_success")
    assert execution.score == 0.0


def test_node_verifier_cannot_average_away_missing_goal_coverage(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="implementation-node",
        goal="implement runnable inventory service with persistence",
        role="coder",
        status="result_submitted",
        metadata={
            "evidence_packet": {
                "summary": "unrelated prose about weather forecasting",
                "claims": ["work completed"],
                "evidence_refs": ["deliverables/app/server.py"],
                "execution_outcome": "succeeded",
            }
        },
    ))
    verifier = Verifier(graph, TraceRecorder(tmp_path))

    result = verifier.verify_node("implementation-node")

    goal = next(item for item in result.dimensions if item.name == "goal_coverage")
    assert goal.score < verifier.repair_threshold
    assert result.verdict != VerificationVerdict.APPROVED


def test_delivery_verifier_rejects_stale_untraceable_test_report(tmp_path):
    report_path = tmp_path / "deliverables" / "runs" / "test_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps({
            "generated_at": "2025-01-01T00:00:00+00:00",
            "summary": {"total": 2, "passed": 2, "failed": 0},
            "tests": [
                {"name": "health", "passed": True},
                {"name": "workflow", "passed": True},
            ],
        }),
        encoding="utf-8",
    )
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))

    result = governor.verify_delivery(
        {"required_outputs": [], "global_acceptance_criteria": []},
        content="runtime delivery",
        artifact_refs=["deliverables/runs/test_report.json"],
        run_dir=tmp_path,
    )

    assert result["synthesis_authorized"] is False
    assert "AC-EXECUTION-PROVENANCE" in result["failed_tests"]


def test_delivery_verifier_accepts_current_case_level_test_report(tmp_path):
    report_path = tmp_path / "deliverables" / "runs" / "test_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"total": 2, "passed": 2, "failed": 0},
            "tests": [
                {"name": "health", "passed": True},
                {"name": "workflow", "passed": True},
            ],
        }),
        encoding="utf-8",
    )
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))

    result = governor.verify_delivery(
        {"required_outputs": [], "global_acceptance_criteria": []},
        content="runtime delivery",
        artifact_refs=["deliverables/runs/test_report.json"],
        run_dir=tmp_path,
    )

    assert "AC-EXECUTION-PROVENANCE" not in result["failed_tests"]


def test_verification_recovery_drops_malformed_optional_edge_but_keeps_repair(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="failed-node",
        goal="implement missing behavior",
        role="coder",
        status="repair_pending",
    ))
    governor = GlobalGovernor(
        graph,
        TraceRecorder(tmp_path),
        llm_client=MalformedRecoveryClient(),
    )

    patch = governor.plan_verification_recovery(
        node_id="failed-node",
        verdict="replan_required",
        overall_score=0.2,
        dimensions=[],
    )

    assert patch.add_nodes[0]["node_id"] == "repair-node"
    assert patch.add_edges == []
    assert "MALFORMED_OPTIONAL_EDGE_DROPPED" in patch.reason_codes
