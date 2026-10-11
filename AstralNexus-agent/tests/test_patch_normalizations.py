"""Regression tests for deterministic patch normalizations + pedagogic
validation messages (doc 14, resume #7 whack-a-mole post-mortem).

Live evidence (r11 resume #7, 2026-09-12 02:20-02:31 UTC): with class-aware
enforcement active, every stalled-round patch correctly proposed the
upstream init_db.py repair (``completed_owned_files: ["init_db.py"]``,
``addressed=true``), yet eight consecutive rounds died on alternating
mechanical validation classes — recovery_of pointing at completed/superseded
nodes, artifact-producer dependency drops, ownership conflicts with the
blocked consumer that should have been REUSED.  The planner oscillated
because each fix broke another rule and no round terminated the loop.
Mechanical slips are now normalized deterministically (audit-trailed), and
the two semantic messages teach the reuse/serial-chain shapes explicitly.
"""
from __future__ import annotations

import pytest


def _make_verdict(node_id: str, details: str):
    from AstralNexus.core.verifier import (
        VerificationDimension,
        VerificationResult,
        VerificationVerdict,
    )
    return VerificationResult(
        node_id=node_id,
        verdict=VerificationVerdict.REPLAN_REQUIRED,
        dimensions=[VerificationDimension(
            name="semantic_goal", score=0.5, passed=False, details=details,
        )],
        overall_score=0.5,
    )


def test_invalid_recovery_targets_stripped_not_fatal(tmp_path, monkeypatch):
    """recovery_of on completed/superseded nodes is dropped, patch survives."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="recovery-targets") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        graph.add_node(TaskNode(
            node_id="N-004-R4", goal="run e2e", role="tester",
            status="superseded",
        ))
        gen = TaskNode(
            node_id="N-004-V8", goal="run e2e", role="tester",
            status="repair_pending",
        )
        gen.metadata["evidence_packet"] = {
            "summary": "SP-007 absent",
            "next_actions": [{
                "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                "reason": "root cause: hardcoded skip",
            }],
        }
        graph.add_node(gen)

        def fake_planner(**kwargs):
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-002-R6",
                    "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                    "task_type": "implementation",
                    "role": "coder",
                    "dependencies": ["N-002-R5"],
                    "owned_paths": ["deliverables/app/backend/init_db.py"],
                    "recovery_of": "N-002-R5",  # completed → invalid target
                }, {
                    "node_id": "N-004-V8-E2E",
                    "goal": "re-run the e2e parts flow after the backend repair",
                    "task_type": "testing",
                    "role": "tester",
                    "dependencies": ["N-002-R6"],
                    "recovery_of": "N-004-R4",  # superseded → invalid target
                }],
                reason_codes=["UPSTREAM_REPAIR"],
            )

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery", fake_planner
        )

        result = orchestrator._apply_governed_verification_recovery(
            gen, _make_verdict("N-004-V8", "SP-007 still missing")
        )
        assert set(result.get("added") or []) == {"N-002-R6", "N-004-V8-E2E"}
        repair = graph.get_node("N-002-R6")
        assert repair is not None
        assert not repair.metadata.get("supersedes_node_id")
        stripped = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.patch.invalid_recovery_targets_stripped"
        ]
        assert stripped
        dropped_text = str(stripped[-1]["dropped"])
        assert "N-002-R5(completed)" in dropped_text
        assert "N-004-R4(superseded)" in dropped_text


def test_artifact_producer_dependency_restored_on_rewire(tmp_path):
    """Rewiring a blocked consumer must not drop the artifact producer."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import Artifact, TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="producer-deps") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
            output_artifacts=["artifact:backend_component"],
        ))
        graph.artifacts["artifact:backend_component"] = Artifact(
            artifact_id="artifact:backend_component",
            node_id="N-002-R5",
            artifact_type="component",
            path="deliverables/app/backend",
            description="backend service component",
        )
        consumer = TaskNode(
            node_id="N-005-R4", goal="boundary validations", role="tester",
            status="blocked",
            owned_paths=["deliverables/runs/security/"],
            input_artifacts=["artifact:backend_component"],
            dependencies=["N-002-R5"],
        )
        graph.add_node(consumer)

        patch = TaskGraphPatch(
            add_nodes=[{
                "node_id": "N-002-R6",
                "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                "task_type": "implementation",
                "role": "coder",
                "dependencies": ["N-002-R5"],
                "owned_paths": ["deliverables/app/backend/init_db.py"],
            }],
            update_nodes=[{
                "node_id": "N-005-R4",
                "dependencies": ["N-002-R6"],  # producer dropped — live r11
            }],
            reason_codes=["UPSTREAM_REPAIR"],
        )
        orchestrator._ensure_artifact_producer_deps(patch)

        entry = patch.update_nodes[0]
        assert entry["dependencies"] == ["N-002-R6", "N-002-R5"]
        restored = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.patch.artifact_producer_deps_restored"
        ]
        assert restored and "N-005-R4+=N-002-R5" in str(restored[-1]["restored"])

        # The normalized patch now survives governor validation.
        applied = orchestrator.governor.apply_task_graph_patch(patch)
        assert "N-002-R6" in applied["added"]
        assert set(graph.get_node("N-005-R4").dependencies) == {"N-002-R6", "N-002-R5"}


def test_ownership_conflict_message_teaches_reuse_of_blocked_owner(tmp_path):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="conflict-hint") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-005-R4", goal="boundary validations", role="tester",
            status="blocked", owned_paths=["deliverables/runs/security/"],
        ))
        patch = TaskGraphPatch(add_nodes=[{
            "node_id": "N-005-R5",
            "goal": "re-run boundary validations",
            "task_type": "testing",
            "role": "tester",
            "owned_paths": ["deliverables/runs/security/boundary_validation.json"],
        }])
        with pytest.raises(ValueError) as excinfo:
            orchestrator.governor.apply_task_graph_patch(patch)
        message = str(excinfo.value)
        assert "overlapping output ownership" in message
        assert "优先复用而非新增" in message
        assert "recovery_of" in message


def _make_rebind_graph(orchestrator, owner_status: str):
    from AstralNexus.core.task_graph import Artifact, TaskNode

    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="N-002-R5", goal="seed the parts database", role="coder",
        status=owner_status,
    ))
    graph.artifacts["artifact:backend_component"] = Artifact(
        artifact_id="artifact:backend_component",
        node_id="N-002-R5",
        artifact_type="contract_bundle",
        path="deliverables/app/backend",
        description="backend service component",
    )
    succ = TaskNode(
        node_id="N-002-R6", goal="repair the backend seed script", role="coder",
        status="result_submitted",
        output_artifacts=["artifact:backend_component"],
    )
    succ.metadata["_pending_artifact_ids"] = ["artifact:phys-1"]
    graph.add_node(succ)
    graph.artifacts["artifact:phys-1"] = Artifact(
        artifact_id="artifact:phys-1",
        node_id="N-002-R6",
        artifact_type="file",
        path="deliverables/app/backend/init_db.py",
        description="repaired seed script",
    )
    return succ


def test_logical_artifact_rebind_from_inactive_owner(tmp_path):
    """Verifier-approved successor rebinds a logical artifact whose owner is
    inactive.  Live r11: N-002-R6 scored 1.0/1.0 then was hard-failed because
    completed N-002-R5 still held artifact:backend_component — an unwinnable
    wall for every lineage successor."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.verifier import VerificationVerdict

    with Orchestrator(tmp_path, run_id="rebind") as orchestrator:
        succ = _make_rebind_graph(orchestrator, "completed")
        orchestrator._finalize_node_evidence(
            succ, approved=True, verification_score=1.0,
            verdict=VerificationVerdict.APPROVED,
        )
        rebound = orchestrator.task_graph.artifacts["artifact:backend_component"]
        assert rebound.node_id == "N-002-R6"
        events = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"] == "artifacts.logical_ownership_rebound"
        ]
        assert events
        assert events[-1]["previous_owner"] == "N-002-R5"
        assert events[-1]["previous_owner_status"] == "completed"


def test_logical_artifact_active_owner_conflict_still_fatal(tmp_path):
    """A concurrent ACTIVE writer conflict remains a hard failure."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.verifier import VerificationVerdict

    with Orchestrator(tmp_path, run_id="rebind-active") as orchestrator:
        succ = _make_rebind_graph(orchestrator, "running")
        with pytest.raises(RuntimeError, match="already owned by"):
            orchestrator._finalize_node_evidence(
                succ, approved=True, verification_score=1.0,
                verdict=VerificationVerdict.APPROVED,
            )


def test_producer_from_node_declaration_and_failed_producer_skip(tmp_path):
    """Producer resolution mirrors the governor: node-declared outputs count
    even when the registry entry carries no node_id (live r11 resume #8:
    artifact:requirements_md).  A terminally FAILED producer is never
    auto-attached — validation rejection must educate the planner to drop
    the reference instead of creating a dead dependency."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import Artifact, TaskNode

    with Orchestrator(tmp_path, run_id="producer-alias") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-001", goal="capture requirements", role="architect",
            status="completed", output_artifacts=["artifact:requirements_md"],
        ))
        graph.artifacts["artifact:requirements_md"] = Artifact(
            artifact_id="artifact:requirements_md", node_id=None,
            artifact_type="document", path="docs/requirements.md",
            description="requirements",
        )
        graph.add_node(TaskNode(
            node_id="N-007-R1", goal="aggregate reports", role="synthesizer",
            status="failed", output_artifacts=["artifact:old_report"],
        ))
        graph.artifacts["artifact:old_report"] = Artifact(
            artifact_id="artifact:old_report", node_id=None,
            artifact_type="document", path="docs/report.md",
            description="old report",
        )
        graph.add_node(TaskNode(
            node_id="N-005-R6", goal="boundary validations", role="tester",
            status="admitted",
        ))
        graph.add_node(TaskNode(
            node_id="N-006-R6", goal="aggregate evidence", role="synthesizer",
            status="admitted",
            input_artifacts=["artifact:requirements_md", "artifact:old_report"],
        ))

        patch = TaskGraphPatch(update_nodes=[
            {"node_id": "N-006-R6", "dependencies": ["N-005-R6"]},
        ])
        orchestrator._ensure_artifact_producer_deps(patch)
        deps = patch.update_nodes[0]["dependencies"]
        assert "N-001" in deps        # declared producer restored
        assert "N-007-R1" not in deps  # failed producer never attached
        assert "N-005-R6" in deps      # original intent preserved


def test_patch_declared_producer_overrides_registry_owner(tmp_path):
    """Live r11 resume #9 precedence regression (N-002-R8 terminal).

    Governor validation resolved artifact:backend_component's producer to
    the patch-declared N-002-R8-RBAC-VERIFY (node declarations override
    the registry), while the dependency restorer kept registry owner
    N-002-R5 — the restored dependency satisfied neither side, and the
    semantically correct verification-node patch was struck out on
    mechanical rejections.  Restoration must mirror the governor's
    declaration-override precedence exactly.
    """
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import Artifact, TaskNode

    with Orchestrator(tmp_path, run_id="producer-precedence") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", output_artifacts=["artifact:backend_component"],
        ))
        graph.artifacts["artifact:backend_component"] = Artifact(
            artifact_id="artifact:backend_component", node_id="N-002-R5",
            artifact_type="contract_bundle", path="deliverables/app/backend",
            description="backend component",
        )
        patch = TaskGraphPatch(add_nodes=[
            {
                "node_id": "N-002-R8-RBAC-VERIFY",
                "goal": "capture live RBAC rejection receipts (403/400)",
                "task_type": "testing",
                "role": "tester",
                "dependencies": [],
                "output_artifacts": ["artifact:backend_component"],
            },
            {
                "node_id": "N-002-R8-HEALTH-VERIFY",
                "goal": "capture the live /health 200 receipt",
                "task_type": "testing",
                "role": "tester",
                "dependencies": [],
                "input_artifacts": ["artifact:backend_component"],
            },
        ])
        orchestrator._ensure_artifact_producer_deps(patch)
        health = patch.add_nodes[1]
        assert health["dependencies"] == ["N-002-R8-RBAC-VERIFY"]
        events = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.patch.artifact_producer_deps_restored"
        ]
        assert events
        assert (
            "N-002-R8-HEALTH-VERIFY+=N-002-R8-RBAC-VERIFY"
            in str(events[-1]["restored"])
        )
