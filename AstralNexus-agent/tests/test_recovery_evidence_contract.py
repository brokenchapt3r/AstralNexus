from copy import deepcopy

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskNode
from AstralNexus.runtime.acceptance_checks import render_execution_contract, run_acceptance_checks
from AstralNexus.runtime.recovery_handoff import build_recovery_handoff


def test_worker_receives_summary_requirement_before_first_execution(tmp_path):
    prompt = Coder()._build_runtime_prompt(AgentContext(node_id="ui", goal="probe pages", metadata={
        "run_dir": str(tmp_path), "acceptance_criteria": ["tests_passed:python probe.py"],
    }), "implementation")
    assert "测试执行证据契约（执行前）" in prompt
    assert '"passed": 3, "failed": 0' in prompt
    assert "不得照抄示例" in prompt
    assert render_execution_contract(["file:proof.json"]) == ""
    def check(stdout):
        return run_acceptance_checks(["tests_passed:python probe.py"], receipts=[{
            "tool": "terminal_exec", "arguments": {"command": ["python", "probe.py"]},
            "status": "success", "returncode": 0, "stdout_tail": stdout,
            "receipt_id": "r1", "files": {},
        }])[0]
    assert check('HTTP 200; exit=0')["holds"] is None
    assert check('{"passed": 3, "failed": 0}')["holds"] is True
    assert check('{"passed": 3, "failed": 1}')["holds"] is False


def test_successful_retry_keeps_old_error_as_history_not_current_failure(tmp_path):
    with Orchestrator(tmp_path, run_id="recovery") as orch:
        node = TaskNode(node_id="ui", goal="probe", role="coder", metadata={
            "last_failure": {"message": "MissingDeclaredArtifacts: page.json", "error_type": "MissingDeclaredArtifacts"},
            "_attempt_accounting": {"execution_outcome": "succeeded", "tool_calls": 25},
            "evidence_packet": {"execution_outcome": "succeeded", "summary": "files written"},
            "_verification_receipt": {"verdict": "inconclusive", "failed_dimensions": [
                {"name": "verifier_runtime", "details": "TEST_SUMMARY_NOT_RECOGNIZED"}]},
        })
        orch.task_graph.add_node(node)
        original = deepcopy(node.metadata)
        report = orch._worker_report_for_node(node)
        assert "last_failure" not in report
        assert "MissingDeclaredArtifacts" in report["historical_execution_failure"]["message"]
        handoff = build_recovery_handoff(orch.task_graph, node)
        assert handoff["failure"] == ""
        assert "MissingDeclaredArtifacts" in handoff["historical_execution_failure"]
        assert handoff["failed_dimensions"][0]["details"] == "TEST_SUMMARY_NOT_RECOGNIZED"
        assert node.metadata == original
        # A failed postcondition remains current even if the tool loop succeeded.
        node.metadata["evidence_packet"]["execution_outcome"] = "failed"
        assert "last_failure" in orch._worker_report_for_node(node)
        assert build_recovery_handoff(orch.task_graph, node)["failure"]
        del node.metadata["last_failure"]
        assert build_recovery_handoff(orch.task_graph, node)["verdict"] == "inconclusive"
