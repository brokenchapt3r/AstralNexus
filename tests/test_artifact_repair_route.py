"""F1: artifact-class deterministic failures need a non-command repair route.

Live r31 evidence (b31 autonomous acceptance): N-102 failed
``N-102.AC01/AC02/EO01 [FILE_NOT_FOUND]``, and the recovery handoff carried
``repair_procedures: []`` because the playbook only recognised ``tests_passed:``
criteria.  With no route for the missing file the planner could only propose
reopening the same node.  The anchored criterion is a file question, so the repair
is to produce the artifact — never to replay a command that measures nothing.
"""
from types import SimpleNamespace

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.development_playbook import recovery_procedures
from AstralNexus.runtime.recovery_handoff import build_recovery_handoff

# Shape taken verbatim from the live N-102 verification receipt.
LIVE_AC01 = {
    "criterion_id": "N-102.AC01",
    "criterion": "file_written:evidence/env/runtime_probe.json",
    "kind": "file_written",
    "execution_id": "488a36939e994511be12c0f84a9eb1d8",
    "checked": True,
    "holds": False,
    "reason": "FILE_NOT_FOUND",
    "detail": "evidence/env/runtime_probe.json does not exist in the workspace",
    "receipt_ids": [],
    "artifact_versions": {},
    "checked_by": "acceptance_check",
}
LIVE_EO01 = {
    **LIVE_AC01,
    "criterion_id": "N-102.EO01",
    "criterion": "file:deliverables/runs/env/runtime_probe.json non-empty",
    "kind": "file_non_empty",
}
LIVE_UNDECIDABLE = {
    **LIVE_AC01,
    "criterion_id": "N-102.AC06",
    "criterion": "N-102.AC02（映射并去歧义）：每条 check 都附真实 command/exit_code/stdout",
    "kind": "undecidable",
    "checked": False,
    "holds": None,
    "reason": "UNDECIDABLE_IN_CODE",
}
LIVE_SUPERSEDED_OWNER = SimpleNamespace(
    node_id="N-102", status="superseded", owned_paths=["evidence/env/runtime_probe.json"])
LIVE_CANCELLED_OWNER = SimpleNamespace(
    node_id="N-103", status="cancelled", owned_paths=["evidence/env/"])


def test_artifact_failure_yields_non_command_route_naming_owner_and_path():
    procedures = recovery_procedures(
        [LIVE_AC01], owner_node_id="N-102",
        nodes=[LIVE_SUPERSEDED_OWNER, LIVE_CANCELLED_OWNER])
    assert len(procedures) == 1
    item = procedures[0]
    assert item["route"] == "produce_missing_artifact"
    assert item["criterion_id"] == "N-102.AC01"
    assert item["criterion"] == "file_written:evidence/env/runtime_probe.json"
    assert item["reason"] == "FILE_NOT_FOUND"
    assert item["missing_paths"] == ["evidence/env/runtime_probe.json"]
    # The failing node is the fallback producer; the cancelled chain is named, not used.
    assert item["owner"] == "N-102"
    assert item["owner_status"] == "superseded"
    assert item["inactive_owners"] == ["N-103"]
    assert item["recheck_command"] == ""
    assert item["recheck"] == "deterministic_check:N-102.AC01"
    steps = " ".join(item["steps"])
    assert "真实生成" in steps and "N-102" in item["steps"][1]
    assert "运行原命令" not in steps
    assert item["completion"]


def test_active_owner_outranks_the_failing_node():
    active = SimpleNamespace(node_id="R-901", status="running", owned_paths=[
        "evidence/env/runtime_probe.json"])
    procedures = recovery_procedures(
        [LIVE_AC01], owner_node_id="N-102", nodes=[active, LIVE_SUPERSEDED_OWNER])
    assert procedures[0]["owner"] == "R-901"
    assert procedures[0]["owner_status"] == "running"
    assert procedures[0]["inactive_owners"] == ["N-102"]


def test_file_non_empty_and_file_empty_reasons_also_route_to_production():
    procedures = recovery_procedures(
        [{**LIVE_EO01, "reason": "FILE_EMPTY"}], owner_node_id="N-201")
    assert procedures[0]["route"] == "produce_missing_artifact"
    assert procedures[0]["missing_paths"] == ["deliverables/runs/env/runtime_probe.json"]


def test_tests_passed_failure_keeps_its_existing_command_route():
    procedures = recovery_procedures([{
        "criterion_id": "N-201.AC05",
        "criterion": "tests_passed:python scripts/selfcheck.py",
        "kind": "tests_passed",
        "execution_id": "exec-1",
        "checked": False,
        "holds": None,
        "reason": "TEST_EXECUTION_STALE",
    }])
    assert len(procedures) == 1
    assert procedures[0]["route"] == "rerun_after_change"
    assert procedures[0]["recheck_command"] == "python scripts/selfcheck.py"
    assert "missing_paths" not in procedures[0]


def test_report_gap_is_not_reframed_as_an_artifact_or_implementation_gap():
    procedures = recovery_procedures([{
        "criterion_id": "N-401.AC02",
        "criterion": ("report_not_stale:deliverables/runs/summary.json "
                      "differs_from:deliverables/runs/summary.baseline.json"),
        "kind": "report_not_stale",
        "execution_id": "exec-2",
        "checked": True,
        "holds": False,
        "reason": "STALE_REPORT_BYTE_IDENTICAL",
    }])
    assert procedures == []


def test_report_class_artifact_gap_is_a_production_route_not_a_failure_claim():
    procedures = recovery_procedures([{
        **LIVE_AC01,
        "criterion_id": "R-901.WR02",
        "criterion": "file_written:deliverables/runs/frontend/selftest.json",
        "reason": "FILE_NOT_FOUND",
    }], owner_node_id="R-901")
    assert procedures[0]["route"] == "produce_missing_artifact"
    assert procedures[0]["missing_paths"] == ["deliverables/runs/frontend/selftest.json"]


def test_identical_criterion_text_keeps_distinct_criterion_ids():
    # Live AC02 and EO01 declare the same path with the same execution: both
    # criterion ids must stay reachable, because completion is per criterion id.
    twin = {**LIVE_EO01, "criterion_id": "N-102.AC02"}
    procedures = recovery_procedures([twin, LIVE_EO01], owner_node_id="N-102")
    assert [item["criterion_id"] for item in procedures] == ["N-102.AC02", "N-102.EO01"]


def test_passing_and_undecidable_leaves_produce_nothing():
    passing = {**LIVE_AC01, "checked": True, "holds": True, "reason": ""}
    assert recovery_procedures([passing, LIVE_UNDECIDABLE]) == []


def test_recovery_handoff_carries_the_route_for_the_live_receipt():
    node = TaskNode(node_id="N-102", goal="runtime probe", role="coder",
                    status="superseded", metadata={
        "runtime_execution_id": "488a36939e994511be12c0f84a9eb1d8",
        "_verification_receipt": {
            "verdict": "replan_required",
            "overall_score": 0.0,
            "failed_dimensions": [{"name": "verifier_runtime", "details": "FILE_NOT_FOUND"}],
            "observations": [LIVE_AC01, LIVE_EO01, LIVE_UNDECIDABLE],
        },
    })
    graph = TaskGraph(nodes=[node, TaskNode(
        node_id="N-103", goal="receipt chain", role="coder", status="cancelled",
        owned_paths=["deliverables/runs/env/", "evidence/env/"], metadata={})])
    handoff = build_recovery_handoff(graph, node)
    assert handoff["verdict"] == "replan_required"
    routes = [item["route"] for item in handoff["repair_procedures"]]
    assert routes == ["produce_missing_artifact", "produce_missing_artifact"]
    assert [item["criterion_id"] for item in handoff["repair_procedures"]] == [
        "N-102.AC01", "N-102.EO01"]
    assert handoff["repair_procedures"][0]["owner"] == "N-102"
    assert handoff["repair_procedures"][0]["owner_status"] == "superseded"
    assert handoff["repair_procedures"][0]["inactive_owners"] == ["N-103"]


def test_worker_prompt_does_not_tell_an_artifact_route_to_run_a_command():
    # F1 follow-up: the handoff prompt used to say "use recheck_command to recheck",
    # which is an impossible instruction for produce_missing_artifact (empty command).
    # The prompt must hand the choice to the procedure fields instead.
    node = TaskNode(node_id="N-102", goal="runtime probe", role="coder",
                    status="superseded", metadata={
        "runtime_execution_id": "488a36939e994511be12c0f84a9eb1d8",
        "_verification_receipt": {"verdict": "replan_required", "overall_score": 0.0,
                                  "failed_dimensions": [], "observations": [LIVE_AC01]},
    })
    graph = TaskGraph(nodes=[node])
    handoff = build_recovery_handoff(graph, node)
    assert handoff["repair_procedures"][0]["recheck_command"] == ""
    prompt = Coder()._build_runtime_prompt(
        AgentContext(node_id="N-102", goal="runtime probe",
                     metadata={"recovery_handoff": handoff}), "implementation")
    assert "按 steps 执行并使用 recheck_command 复验" not in prompt
    assert "复验方式以该 procedure 自身给出的字段为准" in prompt
    assert "deterministic_check:N-102.AC01" in prompt  # the route's real recheck