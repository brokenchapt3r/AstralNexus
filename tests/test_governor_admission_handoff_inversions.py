"""Failures at permit, artifact-dependency and model-shape boundaries."""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import pytest
from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch, ContextGovernanceUnavailable, EventType, strings_from
from AstralNexus.core.governor_budget import GlobalBudget, BudgetReservationRefused, resize_token_limit
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph, TaskNode, Artifact, ArtifactAcceptanceStatus
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.runtime.llm_client import LLMClientError


class Client:
    def __init__(self, response: Any) -> None:
        self.response = response
    def chat(self, *args: Any, **kwargs: Any) -> str:
        if isinstance(self.response, Exception):
            raise self.response
        return json.dumps(self.response)


def governor(root: Path) -> GlobalGovernor:
    return GlobalGovernor(TaskGraph(), TraceRecorder(root))


@pytest.mark.parametrize("refusal", ["capacity", "duplicate", "start_audit"])
def test_resource_released_when_start_permit_fails(tmp_path: Path, monkeypatch: Any, refusal: str) -> None:
    with Orchestrator(tmp_path, run_id="permit-test") as runtime:
        runtime.governor.llm_matching_enabled = False
        monkeypatch.setattr(runtime, "router", None)
        node = runtime.task_graph.add_node(TaskNode("N", "implement Python logic", "coder", status="ready",
            metadata={"implementation_role": "coder", "required_capabilities": ["code_generation"], "task_type": "implementation"}))
        instantiated: list[str] = []
        monkeypatch.setattr(runtime, "_instantiate_governed_worker", lambda *args: instantiated.append("called"))
        if refusal == "capacity":
            monkeypatch.setattr(runtime.governor, "record_task_start", lambda *args, **kwargs: (_ for _ in ()).throw(BudgetReservationRefused("capacity")))
        elif refusal == "duplicate":
            runtime.governor.record_task_start("N")
        else:
            record = runtime.trace_recorder.record
            def audit(kind: Any, payload: Any) -> Any:
                if kind == EventType.TASK_STARTED:
                    raise OSError("audit write unavailable")
                return record(kind, payload)
            monkeypatch.setattr(runtime.trace_recorder, "record", audit)
        result = runtime.execute_task("N")
        assert result is None if refusal == "capacity" else result is False
        assert not instantiated
        assert all(not resource.metadata["active_leases"] for resource in runtime.resource_scheduler.resource_pool.get_all_resources())
        assert runtime.governor.budget.active_parallel_tasks == (1 if refusal == "duplicate" else 0)
        if refusal == "capacity":
            assert node.status == "ready"
        if refusal == "duplicate":
            runtime.governor.record_task_end("N", 0)


def test_wave_width_never_exceeds_budget_permits(tmp_path: Path, monkeypatch: Any) -> None:
    with Orchestrator(tmp_path, run_id="wave", max_parallel=16) as runtime:
        peak = 0
        active = 0
        lock = threading.Lock()
        def execute(node_id: str) -> bool:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.01)
            with lock:
                active -= 1
            return False
        monkeypatch.setattr(runtime, "execute_task", execute)
        monkeypatch.setattr(runtime.adaptive_concurrency, "choose", lambda *args, **kwargs: 16)
        result = runtime._execute_wave([f"N-{i}" for i in range(16)])
        assert len(result) == 16 and peak <= 8


@pytest.mark.parametrize("limit,expected", [(0, 0), (-1, -1), (100, 50)])
def test_budget_resize_preserves_unlimited(limit: int, expected: int) -> None:
    budget = GlobalBudget(max_tokens=resize_token_limit(limit, .5))
    assert budget.max_tokens == expected
    if limit <= 0:
        budget.reserve("positive-estimate", 1000)
        budget.settle("positive-estimate", 0, 0)


def test_planned_input_becomes_dependency_and_consumer_really_readies(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    gov.apply_task_graph_patch(TaskGraphPatch(add_nodes=[
        {"node_id": "C1", "goal": "consume producer data", "input_artifacts": ["deliverables/runs/data.json"]},
        {"node_id": "S1", "goal": "produce data", "owned_paths": ["deliverables/runs/data.json"]},
    ]))
    consumer = gov.task_graph.nodes["C1"]
    assert consumer.dependencies == ["S1"]
    assert any(edge.source == "S1" and edge.target == "C1" and edge.edge_type == "depends_on" for edge in gov.task_graph.edges)
    gov.task_graph.nodes["S1"].status = "completed"  # fixture: producer verdict already passed
    gov.task_graph.add_artifact(Artifact("DATA", "S1", "data", "deliverables/runs/data.json", "data", acceptance_status=ArtifactAcceptanceStatus.ACCEPTED))
    gov.task_graph.promote_waiting_nodes()
    assert consumer.status == "ready"
    assert not gov.task_graph.readiness_blockers(consumer)["missing_input_artifacts"]


@pytest.mark.parametrize("metadata", [True, False])
def test_updates_cannot_poison_delivery_criteria(tmp_path: Path, metadata: bool) -> None:
    gov = governor(tmp_path)
    gov.task_graph.add_node(TaskNode("N", "existing work", "coder", status="blocked"))
    gov.delivery_contract = {"required_outputs": [{"output_id": "O-03", "required": True,
                          "acceptance_criteria": ["evidence must pass"]}]}
    fields: dict[str, Any] = {"delivery_criterion_ids": ["O-03.INVENTED.C99"]}
    update = {"node_id": "N", "metadata": fields} if metadata else {"node_id": "N", **fields}
    with pytest.raises(ValueError, match="unknown to the contract"):
        gov.apply_task_graph_patch(TaskGraphPatch(update_nodes=[update]))
    assert "delivery_criterion_ids" not in gov.task_graph.nodes["N"].metadata


@pytest.mark.parametrize("error", [LLMClientError("read timeout"), {"actions": [], "groups": []}])
def test_context_error_remains_distinct_from_verdict_and_host_crash(tmp_path: Path, error: Any, monkeypatch: Any) -> None:
    from AstralNexus.cli import main as cli
    gov = governor(tmp_path)
    original = error if isinstance(error, LLMClientError) else None
    try:
        gov.plan_context_governance("task", [{"node_id": "N", "goal": "passed evidence"}], Client(error))
    except (LLMClientError, ContextGovernanceUnavailable) as failure:
        if original is not None:
            assert failure is original
        else:
            assert isinstance(failure, ContextGovernanceUnavailable)
        captured = failure
    else:
        pytest.fail("unavailable governance must not approve synthesis")


@pytest.mark.parametrize("priority", ["high", None])
def test_model_shape_priority_mode_and_scalar_cancel_are_normalized(tmp_path: Path, priority: Any) -> None:
    gov = governor(tmp_path)
    patch = gov.plan_task_graph_patch("task", Client({"add_nodes": [{"node_id": "N", "goal": "inspect data", "priority": priority}],
                    "candidate_mode": "Replace", "cancel_nodes": "N-003"}))
    assert patch.candidate_mode == "replace" and patch.cancel_nodes == ["N-003"]
    assert patch.add_nodes[0]["priority"] == 50
    malformed = gov.plan_task_graph_patch("task", Client({"add_nodes": "bad shape"}))
    assert "PLANNING_FAILED" in malformed.reason_codes
    assert strings_from([None, False, 0, "valid", {"name": "not text"}]) == ["valid"]


@pytest.mark.parametrize("status", ["completed", "failed", "running", "verifying"])
def test_new_nodes_cannot_forge_execution_state(tmp_path: Path, status: str) -> None:
    gov = governor(tmp_path)
    with pytest.raises(ValueError, match="cannot claim"):
        gov.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{"node_id": "fake", "goal": "work", "status": status}]))
    assert not gov.task_graph.nodes
    gov.admit_root_goal(node_id="P", goal="real user intent", constraints=[])
    assert gov.task_graph.nodes["U-001"].status == "completed"


def test_update_ownership_is_compared_on_same_namespace(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    gov.task_graph.add_node(TaskNode("A", "first", "coder", owned_paths=["deliverables/app/backend/"]))
    gov.task_graph.add_node(TaskNode("B", "second", "coder"))
    with pytest.raises(ValueError, match="overlapping"):
        gov.apply_task_graph_patch(TaskGraphPatch(update_nodes=[{"node_id": "B", "owned_paths": ["app/backend/"]}]))
    assert not gov.task_graph.nodes["B"].owned_paths


def test_bad_subtask_does_not_abort_valid_sibling(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    gov.task_graph.add_node(TaskNode("P", "finished parent", "coder", status="completed"))
    def proposal(goal: str, path: str) -> dict[str, Any]:
        return {"goal": goal, "owned_paths": [path], "expected_outputs": [path],
                "information_gain": 1, "risk_reduction": 1, "parallel_benefit": 1, "estimated_cost": 1}
    bad = proposal("implement invalid child", "app/bad.py")
    bad["input_artifacts"] = ["artifact:MISSING"]
    result = gov.admit_worker_subtasks(parent_node_id="P", proposals=[bad, proposal("implement valid child", "app/good.py")])
    assert len(result["added"]) == 1 and len(result["rejected_proposals"]) == 1
    assert gov.task_graph.nodes[result["added"][0]].owned_paths == ["deliverables/app/good.py"]


def test_empty_catalogue_and_structured_path_evidence_are_safe(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    assert gov.select_worker_profile({"goal": "implement code", "required_capabilities": ["code_generation"]}, [])["selected"] is None
    parent = tmp_path.parent / "outside-proof.txt"
    parent.write_text("outside", encoding="utf-8")
    assert not gov._clause_path_exists_on_disk("../outside-proof.txt")
    gov.trace_recorder.record("verifier.completed", {"path": "app/routes.py.backup"})
    assert not gov._recent_path_evidence(["app/routes.py"])
    gov.trace_recorder.record("verifier.completed", {"path": "app/routes.py"})
    assert gov._recent_path_evidence(["app/routes.py"])


def test_external_policy_is_explicit_not_keyword_triggered(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    response = {"required_outputs": [], "external_evidence_policy": "required_if_explicit"}
    result = gov.derive_delivery_contract("无外部材料，标注待检索", Client(response))
    assert result["external_evidence_policy"] == "required_if_explicit"
    response["external_evidence_policy"] = "optional_when_unavailable"
    assert gov.derive_delivery_contract("same objective", Client(response))["external_evidence_policy"] == "optional_when_unavailable"


def test_recovery_catalogue_refuses_invented_targets(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    gov.resource_ids_provider = lambda: {"cpu-one", "cpu-two"}
    gov.llm_matching_enabled = True
    gov.llm_client = Client({"actions": [{"type": "switch_resource", "target": "cpu-one",
                          "details": {"alternative_resource": "invented"}}]})
    with pytest.raises(ValueError, match="registered"):
        gov.plan_fault_recovery(fault={}, impact={})


@pytest.mark.parametrize("state", ["ready", "completed", "repair_pending"])
def test_retarget_only_selects_eligible_lineage_tip(tmp_path: Path, state: str, monkeypatch: Any) -> None:
    with Orchestrator(tmp_path, run_id="lineage") as runtime:
        runtime.task_graph.add_node(TaskNode("A", "original failure", "coder", status="failed"))
        runtime.task_graph.add_node(TaskNode("A-R", "latest carrier", "coder", status=state, metadata={"recovery_of": "A"}))
        monkeypatch.setattr(runtime, "_strip_foreign_takeover_claims", lambda *args: None)
        patch = TaskGraphPatch(add_nodes=[{"node_id": "next", "goal": "repair", "recovery_of": "A"}])
        runtime._normalize_recovery_ownership_transfer(patch)
        assert patch.add_nodes[0]["recovery_of"] == ("A-R" if state == "repair_pending" else "A")
        assert runtime.task_graph.nodes["A-R"].status == state


def test_duplicate_does_not_drop_new_dependency_or_acceptance(tmp_path: Path) -> None:
    gov = governor(tmp_path)
    gov.task_graph.add_node(TaskNode("DEP", "prerequisite", "coder", status="completed"))
    gov.task_graph.add_node(TaskNode("OLD", "inspect same data", "analyst"))
    result = gov.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "NEW", "goal": "inspect same data", "role": "analyst", "dependencies": ["DEP"],
        "acceptance_criteria": ["new independent acceptance"],
    }]))
    assert result["added"] == ["NEW"]
    assert gov.task_graph.nodes["NEW"].dependencies == ["DEP"]
    assert gov.task_graph.nodes["NEW"].acceptance_criteria == ["new independent acceptance"]


def test_cancelled_owner_no_longer_anchors_frozen_file_clause(tmp_path: Path) -> None:
    from AstralNexus.core.governor import GoalSpec
    gov = governor(tmp_path)
    gov.goal_spec = GoalSpec("goal", "deliver deliverables/docs/required.md", "deliver")
    gov.task_graph.add_node(TaskNode("OWNER", "deliver required document", "coder", owned_paths=["deliverables/docs/required.md"]))
    result = gov.apply_task_graph_patch(TaskGraphPatch(cancel_nodes=["OWNER"]))
    assert gov.task_graph.nodes["OWNER"].status == "cancelled"
    assert any(item["path"] == "deliverables/docs/required.md" for item in result["contract_clause_gaps"])


@pytest.mark.parametrize("metadata", [None, {"delivery_criterion_ids": None}])
def test_empty_update_metadata_does_not_crash_known_id_gate(tmp_path: Path, metadata: Any) -> None:
    gov = governor(tmp_path)
    gov.task_graph.add_node(TaskNode("N", "existing work", "coder"))
    gov.delivery_contract = {"required_outputs": [{"output_id": "O-03", "acceptance_criteria": ["evidence"]}]}
    gov.apply_task_graph_patch(TaskGraphPatch(update_nodes=[{"node_id": "N", "metadata": metadata}]))
    assert gov.task_graph.nodes["N"].status == "admitted"
