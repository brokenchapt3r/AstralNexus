"""Regression tests for stalled-graph recovery of terminal-failed dependencies.

Doc 14 §12:50 user directive step 4: after the upstream backend repair lands,
the two test tracks must be rebuilt legally — the terminal failure state of
N-005-R3 is PRESERVED, a legitimate replacement re-verification node is
derived, and the report-aggregation consumer's dependencies are rewired so
the delivery gate becomes reachable again.

Covered seams:
1. the stalled-graph planner receives a terminal-dependency dossier (terminal
   reason, the failed worker's root-cause proposals, lineage attempt history)
   and explicit handling rules (markers immutable, upstream repair first,
   consumer rewiring);
2. the resulting patch passes the same root-cause routing enforcement as
   verification recovery, with rejection feedback through recovery_error;
3. identical repeats under fresh node IDs are blocked by the lineage
   fingerprint on this path too.
"""
from __future__ import annotations


def _build_graph(orchestrator, tmp_path):
    """Live r11 shape: completed backend producer, terminal-failed security
    track with a root-cause escalation, blocked report aggregator."""
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "init_db.py").write_text("# seed v1\n", encoding="utf-8")

    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="N-002-R5", goal="seed the parts database", role="coder",
        status="completed", owned_paths=["deliverables/app/backend/"],
    ))
    root5 = TaskNode(
        node_id="N-005", goal="run the security test track", role="tester",
        status="superseded",
    )
    root5.metadata["_lineage_recovery_history"] = [{
        "node_id": "N-005-R2", "fingerprint": "older-attempt",
        "added": ["N-005-R3"], "updates": 0,
    }]
    graph.add_node(root5)
    terminal = TaskNode(
        node_id="N-005-R3", goal="run the security test track", role="tester",
        status="failed", owned_paths=["deliverables/runs/security/"],
    )
    terminal.metadata["recovery_of"] = "N-005"
    terminal.metadata["_terminal_failure"] = True
    terminal.metadata["_terminal_reason"] = "recovery_exhausted"
    terminal.metadata["evidence_packet"] = {
        "summary": "SP-007 flow cannot be exercised",
        "next_actions": [{
            "type": "spawn",
            "target": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
            "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
            "reason": "root cause: hardcoded skip",
            "role": "coder",
        }],
    }
    graph.add_node(terminal)
    graph.add_node(TaskNode(
        node_id="N-006-R3", goal="aggregate the test reports",
        role="synthesizer", status="blocked", dependencies=["N-005-R3"],
    ))
    return graph


def _ready_governor(orchestrator, monkeypatch, patches):
    """Fake the incremental planner; capture every recovery_goal."""
    captured: list[str] = []
    sequence = list(patches)

    def fake_planner(goal, **kwargs):
        captured.append(goal)
        return sequence[min(len(captured), len(sequence)) - 1]

    orchestrator.governor.llm_client = object()
    orchestrator.governor.llm_matching_enabled = True
    monkeypatch.setattr(
        orchestrator.governor, "plan_task_graph_patch", fake_planner
    )
    return captured


def _compliant_patch(replacement_id: str = "N-002-R6", rewire: bool = True):
    from AstralNexus.core.governor import TaskGraphPatch

    patch = TaskGraphPatch(
        add_nodes=[{
            "node_id": replacement_id,
            "goal": "repair the seed script",
            "task_type": "implementation",
            "role": "coder",
            "dependencies": ["N-002-R5"],
            "owned_paths": ["deliverables/app/backend/init_db.py"],
        }],
        reason_codes=["DEPENDENCY_REPAIR"],
    )
    if rewire:
        patch.add_nodes.append({
            "node_id": "N-005-R4",
            "goal": "re-run the security test track",
            "task_type": "verification",
            "role": "tester",
            "dependencies": [replacement_id],
            "recovery_of": "N-005-R3",
            "owned_paths": ["deliverables/runs/security/"],
        })
        patch.update_nodes.append(
            {"node_id": "N-006-R3", "dependencies": ["N-005-R4"]}
        )
    return patch


def _replan(orchestrator):
    return orchestrator._replan_stalled_graph(
        goal="deliver the repair-ticket platform",
        blocked_nodes=["N-006-R3"],
        reason="unsatisfied_dependencies_or_artifacts",
    )


def test_dossier_and_rules_reach_the_planner_and_rewire_consumer(tmp_path, monkeypatch):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="stalled-dossier") as orchestrator:
        graph = _build_graph(orchestrator, tmp_path)
        captured = _ready_governor(orchestrator, monkeypatch, [_compliant_patch()])

        result = _replan(orchestrator)

        assert result["changed"] is True
        assert set(result["patch"]["added"]) == {"N-002-R6", "N-005-R4"}
        # The dossier travels with the recovery goal.
        goal = captured[0]
        assert "终态失败依赖档案" in goal
        assert "recovery_exhausted" in goal
        assert "Remove the SP-007 skip" in goal
        assert "already_tried" in goal and "N-005-R2" in goal
        assert "不得清除、回滚或伪装成功" in goal
        # Consumer rewired to the replacement; aggregation gate reachable.
        assert graph.get_node("N-006-R3").dependencies == ["N-005-R4"]
        # Terminal markers stay untouched.
        terminal = graph.get_node("N-005-R3")
        assert terminal.status == "failed"
        assert terminal.metadata["_terminal_failure"] is True
        assert terminal.metadata["_terminal_reason"] == "recovery_exhausted"
        # The attempt is recorded into the lineage history on the root.
        history = graph.get_node("N-005").metadata["_lineage_recovery_history"]
        assert len(history) == 2
        assert history[-1]["node_id"] == "N-005-R3"


def test_workaround_patch_rejected_with_feedback_then_repaired(tmp_path, monkeypatch):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="stalled-enforce") as orchestrator:
        graph = _build_graph(orchestrator, tmp_path)
        workaround = TaskGraphPatch(
            add_nodes=[{
                "node_id": "N-005-R4-INJECT",
                "goal": "inject the SP-007 row before the security track",
                "task_type": "testing",
                "role": "tester",
                "dependencies": ["N-002-R5"],
            }],
            reason_codes=["TEST_RECOVERY"],
        )
        captured = _ready_governor(
            orchestrator, monkeypatch, [workaround, _compliant_patch()]
        )

        first = _replan(orchestrator)
        assert first["changed"] is False
        assert first["reason"] == "governor_replan_failed"
        assert "ignores the declared upstream root cause" in first["error"]
        assert "N-005-R4-INJECT" not in graph.nodes

        # The rejection is persisted and fed back into the next recovery goal.
        second = _replan(orchestrator)
        assert second["changed"] is True
        assert "上次恢复 Patch 被拒绝" in captured[1]
        assert "ignores the declared upstream root cause" in captured[1]
        assert set(second["patch"]["added"]) == {"N-002-R6", "N-005-R4"}


def test_identical_repeat_under_new_id_is_blocked(tmp_path, monkeypatch):
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="stalled-dedup") as orchestrator:
        graph = _build_graph(orchestrator, tmp_path)
        # No consumer rewiring: the blocked shape persists across calls, so
        # the second identical proposal (fresh node ID) must be caught by the
        # lineage fingerprint instead of being applied again.
        captured = _ready_governor(
            orchestrator,
            monkeypatch,
            [
                _compliant_patch("N-002-R6", rewire=False),
                _compliant_patch("N-002-R7", rewire=False),
            ],
        )

        first = _replan(orchestrator)
        assert first["changed"] is True
        assert first["patch"]["added"] == ["N-002-R6"]

        second = _replan(orchestrator)
        assert second["changed"] is False
        assert second["reason"] == "governor_replan_failed"
        assert "需要改变方案" in second["error"]
        assert "N-002-R7" not in graph.nodes
        history = graph.get_node("N-005").metadata["_lineage_recovery_history"]
        assert len(history) == 2  # pre-existing + first attempt only


def test_zombie_admitted_owner_is_normalized_into_takeover(tmp_path):
    """Fix 19 (live r11 resume #30 blocked 2026-09-13 20:11): after the
    rollback-proof repair completed, G-001-GENERATE-TEST-REPORT sat admitted
    with permanently dead dependencies (boundary-tests superseded with no
    living successor, its recovery cancelled) — it could never become ready,
    yet it held exclusive ownership of deliverables/runs/test_report.json.
    Three autonomous READY-WAVE-5 proposals regenerating that report were
    rejected identically by the overlapping-ownership validator and the run
    blocked on "same autonomous recovery outcome repeated three times".
    A zombie owner must be takeover-eligible so the legal shape (auto
    recovery_of) is reachable; a HEALTHY admitted owner must not."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    orchestrator = Orchestrator(tmp_path, run_id="zombie-takeover")
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="BOUNDARY", goal="run boundary tests", role="tester",
        status="superseded",
    ))
    graph.add_node(TaskNode(
        node_id="BOUNDARY-RECOVERY", goal="rerun boundary tests", role="tester",
        status="cancelled",
    ))
    graph.add_node(TaskNode(
        node_id="REPORT", goal="aggregate the test report", role="synthesizer",
        status="admitted", dependencies=["BOUNDARY", "BOUNDARY-RECOVERY"],
        owned_paths=["deliverables/runs/test_report.json"],
    ))
    graph.add_node(TaskNode(
        node_id="HEALTHY", goal="serve the API", role="coder",
        status="admitted", dependencies=["REPORT"],
        owned_paths=["deliverables/app/backend/main.py"],
    ))

    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "WAVE-5",
        "goal": "re-run boundary tests and regenerate the report",
        "role": "tester",
        "owned_paths": ["deliverables/runs/test_report.json"],
    }])
    normalized = orchestrator._normalize_recovery_ownership_transfer(patch)
    added = normalized.add_nodes[0]
    assert added["recovery_of"] == "REPORT"
    assert added["metadata"]["recovery_of"] == "REPORT"
    assert added["metadata"]["auto_recovery_of"] is True

    # Control: a healthy admitted owner (all deps alive) is never silently
    # taken over — the conflict stays for the validator to teach.
    patch2 = TaskGraphPatch(add_nodes=[{
        "node_id": "WAVE-6",
        "goal": "touch the backend",
        "role": "coder",
        "owned_paths": ["deliverables/app/backend/main.py"],
    }])
    normalized2 = orchestrator._normalize_recovery_ownership_transfer(patch2)
    assert "recovery_of" not in normalized2.add_nodes[0]


def test_repair_of_failed_owner_contract_normalizes_into_takeover(tmp_path):
    """Fix 23a (live r12 initial-graph double ownership): architect A-001
    failed owning the exact file deliverables/docs/architecture.md while
    admitted D-001 carried the SAME exact file (seed double ownership the
    initial admission let through). Repair proposals A-001-R1/R2/R3 without
    recovery_of were rejected identically for 23 planning rounds because the
    active duplicate owner D-001 (healthy deps, not takeover-eligible)
    blocked every attempt and no path taught by the rejection text was taken.
    A proposal re-declaring the output contract of exactly ONE failed node
    must normalize into recovery_of that node; the ownership-transfer pass
    then strips the duplicated file from the active duplicate owner."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    orchestrator = Orchestrator(tmp_path, run_id="failed-owner-takeover")
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="A-001", goal="write the architecture document",
        role="architect", status="failed",
        owned_paths=["deliverables/docs/architecture.md"],
    ))
    graph.add_node(TaskNode(
        node_id="D-001", goal="assemble the documentation bundle",
        role="coder", status="admitted", dependencies=["A-001"],
        owned_paths=[
            "deliverables/docs/architecture.md",
            "deliverables/docs/deployment.md",
        ],
    ))

    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "A-001-R4",
        "goal": "rewrite the architecture document with the full contract",
        "role": "architect",
        "owned_paths": ["deliverables/docs/architecture.md"],
    }])
    normalized = orchestrator._normalize_recovery_ownership_transfer(patch)
    added = normalized.add_nodes[0]
    assert added["recovery_of"] == "A-001"
    assert added["metadata"]["auto_recovery_of"] is True
    # the active duplicate owner is also a consumer of the failed node: the
    # rewire and the ownership strip must MERGE into one update — it loses
    # exactly the duplicated file, keeps the rest, and follows the lineage
    update = next(u for u in normalized.update_nodes if u["node_id"] == "D-001")
    assert update["owned_paths"] == ["deliverables/docs/deployment.md"]
    assert update["dependencies"] == ["A-001-R4"]
    assert update["metadata"]["ownership_transfer_to"] == "A-001-R4"

    # Control: ambiguity (two failed owners of the same contract) must NOT
    # guess a takeover target.
    graph.add_node(TaskNode(
        node_id="A-002", goal="an earlier architecture attempt",
        role="architect", status="failed",
        owned_paths=["deliverables/docs/architecture.md"],
    ))
    patch2 = TaskGraphPatch(add_nodes=[{
        "node_id": "A-001-R5",
        "goal": "rewrite the architecture document",
        "role": "architect",
        "owned_paths": ["deliverables/docs/architecture.md"],
    }])
    normalized2 = orchestrator._normalize_recovery_ownership_transfer(patch2)
    assert "recovery_of" not in normalized2.add_nodes[0]


def test_recovery_fingerprint_invariant_to_rotating_proposed_ids(tmp_path, monkeypatch):
    """Fix 23b (live r12: repeated_no_progress climbed to 23 while
    repeated_recovery stayed at 1): the planner rotated cosmetic proposed
    node IDs (A-001-R1/R2/R3), the rejection text embeds those IDs, so every
    identical structural failure hashed to a DIFFERENT fingerprint and the
    same-outcome breaker never tripped. The fingerprint must be invariant to
    proposed node IDs; the raw error stays verbatim for planner feedback."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator

    def conflicting_patch(tag: str) -> TaskGraphPatch:
        # Two additions claiming the same root-cause-owned path: root-cause
        # routing enforcement is satisfied, and the ownership validator
        # raises "overlapping output ownership" naming BOTH rotating IDs.
        return TaskGraphPatch(
            add_nodes=[
                {
                    "node_id": f"DOC-{tag}-1",
                    "goal": "repair the seed script",
                    "task_type": "implementation",
                    "role": "coder",
                    "owned_paths": ["deliverables/app/backend/init_db.py"],
                },
                {
                    "node_id": f"DOC-{tag}-2",
                    "goal": "aggregate the seed script repairs",
                    "task_type": "implementation",
                    "role": "coder",
                    "owned_paths": ["deliverables/app/backend/init_db.py"],
                },
            ],
            reason_codes=["DEPENDENCY_REPAIR"],
        )

    with Orchestrator(tmp_path, run_id="fingerprint-invariance") as orchestrator:
        _build_graph(orchestrator, tmp_path)
        _ready_governor(orchestrator, monkeypatch, [
            conflicting_patch("R1"), conflicting_patch("R2"), conflicting_patch("R3"),
        ])
        first = _replan(orchestrator)
        second = _replan(orchestrator)
        third = _replan(orchestrator)
    assert first["reason"] == "governor_replan_failed"
    assert "overlapping output ownership" in first["error"]
    # rotating IDs no longer disguise an identical outcome
    assert first["repeat_count"] == 1
    assert second["repeat_count"] == 2
    assert third["repeat_count"] == 3


def test_lineage_prefix_repair_is_reshaped_into_contract_takeover(tmp_path):
    """Fix 25b (live r12 blocked iter 33): architect A-001 failed owning
    architecture_draft.md; the healthy docs-bundle node D-001 owns the final
    architecture.md. The verifier's mis-scoped gap ("architecture.md 不存在")
    drove repair proposals A-001-R1/R2/R3 to claim D-001's file — an
    unsolvable conflict (taking over D-001 would be wrong, giving up leaves
    the blocker forever). Normalization must reshape the proposal into a
    takeover of the failed lineage member (longest ID prefix match) with the
    foreign active-owner claims stripped; the apply-side inheritance walk
    restores the failed node's real contract onto the replacement."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    orchestrator = Orchestrator(tmp_path, run_id="lineage-prefix-takeover")
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="A-001", goal="output the structured design contract",
        role="architect", status="failed",
        owned_paths=["deliverables/docs/architecture_draft.md"],
    ))
    graph.add_node(TaskNode(
        node_id="D-001", goal="assemble the seven required documents",
        role="coder", status="admitted",
        owned_paths=[
            "deliverables/docs/architecture.md",
            "deliverables/docs/deployment.md",
        ],
    ))
    consumer = TaskNode(
        node_id="I-001", goal="implement the backend", role="coder",
        status="blocked", dependencies=["A-001"],
    )
    consumer.metadata["_blocked_by_terminal_dep"] = "A-001"
    graph.add_node(consumer)

    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "A-001-R3",
        "goal": "rewrite the architecture document with the full contract",
        "role": "architect",
        "owned_paths": ["deliverables/docs/architecture.md"],
        "expected_outputs": ["deliverables/docs/architecture.md"],
    }])
    normalized = orchestrator._normalize_recovery_ownership_transfer(patch)
    added = normalized.add_nodes[0]
    assert added["recovery_of"] == "A-001"
    assert added["metadata"]["auto_recovery_of"] is True
    # the foreign claim on D-001's file is stripped; the apply-side
    # inheritance walk unions A-001's draft contract back on
    assert added["owned_paths"] == []
    assert added["expected_outputs"] == []
    # the healthy bundle node is untouched — no strip updates at all
    assert all(u["node_id"] != "D-001" for u in normalized.update_nodes)
    # the blocked consumer is rewired onto the takeover
    rewire = next(u for u in normalized.update_nodes if u["node_id"] == "I-001")
    assert rewire["dependencies"] == ["A-001-R3"]

    # Fix 25c (live r12 resume-2, blocked iter 36): the planner learned to
    # DECLARE recovery_of from the rejection feedback but kept the foreign
    # claim on D-001's file; the validator exempts only the recovery target,
    # so the identical rejection repeated to the breaker. Foreign-claim
    # stripping must apply to declared takeovers too — while target-contract
    # paths are never stripped.
    patch2 = TaskGraphPatch(add_nodes=[{
        "node_id": "A-001-R4",
        "goal": "complete the design contract with state machine and matrix",
        "role": "architect",
        "recovery_of": "A-001",
        "owned_paths": [
            "deliverables/docs/architecture.md",
            "deliverables/docs/architecture_draft.md",
        ],
    }])
    normalized2 = orchestrator._normalize_recovery_ownership_transfer(patch2)
    added2 = normalized2.add_nodes[0]
    assert added2["recovery_of"] == "A-001"
    # the foreign claim is gone; the target's own contract path is kept
    assert added2["owned_paths"] == ["deliverables/docs/architecture_draft.md"]
    assert all(u["node_id"] != "D-001" for u in normalized2.update_nodes)


def test_takeover_is_retargeted_to_latest_living_lineage_member(tmp_path):
    """Fix 27 (live r12 iter 46-60 blocked): proposals aimed at the lineage
    ROOT (recovery_of=A-001) while the living contract carrier was A-001-R2
    (repair_pending, took over via R1). The ownership validator exempts only
    the declared recovery target, so every proposal conflicted with the
    current carrier and rotated to the breaker. Retargeting walks the
    unambiguous successor chain to the latest LIVING member; extinct
    lineages keep the declared failed target."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    orchestrator = Orchestrator(tmp_path, run_id="lineage-retarget")
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="A-001", goal="output the design contract",
        role="architect", status="failed",
    ))
    r1 = TaskNode(
        node_id="A-001-R1", goal="redesign the contract", role="architect",
        status="superseded",
        owned_paths=["deliverables/docs/architecture_draft.md"],
    )
    r1.metadata["recovery_of"] = "A-001"
    graph.add_node(r1)
    r2 = TaskNode(
        node_id="A-001-R2", goal="finalize the draft", role="architect",
        status="repair_pending",
        owned_paths=["deliverables/docs/architecture_draft.md"],
        output_artifacts=["artifact:design_contract"],
    )
    r2.metadata["recovery_of"] = "A-001-R1"
    graph.add_node(r2)
    graph.add_node(TaskNode(
        node_id="D-001", goal="assemble the docs bundle", role="coder",
        status="admitted",
        owned_paths=["deliverables/docs/architecture.md"],
    ))

    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "A-001-R3",
        "goal": "finalize the design contract draft",
        "role": "architect",
        "recovery_of": "A-001",
        "owned_paths": [
            "deliverables/docs/architecture_draft.md",
            "deliverables/docs/architecture.md",
        ],
    }])
    normalized = orchestrator._normalize_recovery_ownership_transfer(patch)
    added = normalized.add_nodes[0]
    assert added["recovery_of"] == "A-001-R2"
    assert added["metadata"]["retargeted_from"] == "A-001"
    # the retargeted target's contract (draft) is kept; the foreign
    # bundle file is stripped by the fix-25c pass
    assert added["owned_paths"] == ["deliverables/docs/architecture_draft.md"]
    assert all(u["node_id"] != "D-001" for u in normalized.update_nodes)

    # Control: extinct lineage (tip superseded, no living successor) keeps
    # the declared failed target.
    graph.update_node_status("A-001-R2", "superseded")
    patch2 = TaskGraphPatch(add_nodes=[{
        "node_id": "A-001-R4",
        "goal": "finalize the design contract draft",
        "role": "architect",
        "recovery_of": "A-001",
        "owned_paths": ["deliverables/docs/architecture_draft.md"],
    }])
    normalized2 = orchestrator._normalize_recovery_ownership_transfer(patch2)
    assert normalized2.add_nodes[0]["recovery_of"] == "A-001"
