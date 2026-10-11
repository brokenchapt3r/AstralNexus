import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from AstralNexus.core.governor import GlobalBudget, TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.task_graph import Fact, TaskEdge, TaskGraph, TaskNode


def _graph() -> TaskGraph:
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="T-001",
        goal="root",
        role="planner",
        status="completed",
    ))
    graph.add_node(TaskNode(
        node_id="T-002",
        goal="collect evidence",
        role="researcher",
        status="ready",
        dependencies=["T-001"],
    ))
    graph.add_edge(TaskEdge("T-002", "T-001", "depends_on"))
    graph.add_fact(Fact(
        fact_id="F-001",
        text="unverified observation",
        source="T-001",
        confidence=0.5,
        recorded_at="2026-01-01T00:00:00+00:00",
    ))
    return graph


def test_runtime_state_observes_and_persists_global_state(tmp_path):
    manager = RuntimeStateManager(tmp_path, "run-1")
    graph = _graph()

    state = manager.observe(
        task_graph=graph,
        global_goal="deliver",
        task_version=2,
        iteration=7,
        phase="observing",
        run_status="running",
        track_progress=True,
        topology_override={
            "all_edges": [["T-001", "T-002"]],
            "metrics": {"sparsity": 0.5},
        },
    )

    assert state.global_goal == "deliver"
    assert state.task_version == 2
    assert state.nodes_by_status == {"completed": 1, "ready": 1}
    assert state.ready_nodes == ["T-002"]
    assert state.uncertainty["low_confidence_fact_ids"] == ["F-001"]
    assert state.topology_state["metrics"]["sparsity"] == 0.5
    persisted = json.loads((tmp_path / "runtime_state.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "observing"


def test_runtime_state_detects_repeated_no_progress_and_restores(tmp_path):
    graph = _graph()
    manager = RuntimeStateManager(tmp_path, "run-1")
    manager.observe(
        task_graph=graph,
        global_goal="deliver",
        task_version=1,
        iteration=1,
        phase="observing",
        run_status="running",
        track_progress=True,
    )
    repeated = manager.observe(
        task_graph=graph,
        global_goal="deliver",
        task_version=1,
        iteration=2,
        phase="observing",
        run_status="running",
        track_progress=True,
    )
    assert repeated.repeated_no_progress == 1

    restored = RuntimeStateManager(tmp_path, "run-1")
    assert restored.state.repeated_no_progress == 1
    assert restored.state.progress_fingerprint == repeated.progress_fingerprint


def test_recovery_converges_only_after_same_noop_repeats(tmp_path):
    manager = RuntimeStateManager(tmp_path, "run-1")
    assert manager.record_recovery("same", changed=False) == 1
    assert manager.record_recovery("same", changed=False) == 2
    assert manager.record_recovery("same", changed=False) == 3
    assert manager.record_recovery("different", changed=False) == 1
    assert manager.record_recovery("different", changed=True) == 0


def test_formal_global_budget_is_unbounded_until_configured():
    budget = GlobalBudget()
    budget.used_tokens = 9_000_000
    budget.used_time_seconds = 9_000_000
    assert budget.remaining_tokens() > budget.used_tokens
    assert budget.remaining_time() > budget.used_time_seconds
    assert budget.can_admit_task(1_000_000)


def test_recovery_rejection_is_persisted_and_sent_to_next_plan(tmp_path, monkeypatch):
    with Orchestrator(tmp_path) as orchestrator:
        orchestrator.task_graph.add_node(TaskNode(
            node_id="producer", goal="build backend", role="coder", status="blocked",
            owned_paths=["deliverables/backend/"],
            output_artifacts=["backend"],
        ))
        # Round 12: a UNIQUE blocked-owner conflict now normalizes into an
        # explicit takeover (fix 11), so this test keeps its original intent
        # — rejection feedback persistence — with an AMBIGUOUS conflict:
        # two blocked owners of the same contract.  Normalization refuses to
        # guess; validation still teaches via the rejection text.
        orchestrator.task_graph.add_node(TaskNode(
            node_id="producer-b", goal="stale backend copy", role="coder",
            status="blocked",
            owned_paths=["deliverables/backend/"],
            output_artifacts=["backend"],
        ))
        orchestrator.governor.enable_llm_worker_matching(Mock(spec=["chat"]))
        goals = []

        def plan(goal, **kwargs):
            goals.append(goal)
            replacement = {
                "node_id": "replacement", "goal": "repair backend",
                "owned_paths": ["deliverables/backend/"],
                "output_artifacts": ["backend"],
            }
            if len(goals) > 1:
                replacement["recovery_of"] = "producer"
            return TaskGraphPatch(add_nodes=[replacement])

        monkeypatch.setattr(orchestrator.governor, "plan_task_graph_patch", plan)
        first = orchestrator._replan_stalled_graph(
            goal="deliver", blocked_nodes=["producer"], reason="stalled",
        )
        assert first["changed"] is False
        assert "overlapping output ownership" in first["error"]
        assert orchestrator.task_graph.get_node("replacement") is None
        # The ambiguous stale sibling is cleaned up before the next wave;
        # with a unique target the recovery_of takeover applies cleanly.
        orchestrator.task_graph.update_node_status("producer-b", "cancelled")
        # Simulate restarting the state manager before the next recovery wave.
        orchestrator.runtime_state_manager = RuntimeStateManager(
            tmp_path, orchestrator.runtime_state_manager.state.run_id,
        )
        second = orchestrator._replan_stalled_graph(
            goal="deliver", blocked_nodes=["producer"], reason="stalled",
        )
        assert first["error"] in goals[1]
        assert second["changed"] is True
        assert orchestrator.runtime_state_manager.state.recovery_error == ""


def test_different_validation_errors_do_not_count_as_same_recovery(tmp_path, monkeypatch):
    with Orchestrator(tmp_path) as orchestrator:
        orchestrator.governor.enable_llm_worker_matching(Mock(spec=["chat"]))
        errors = iter(["ownership conflict", "missing dependency", "missing dependency"])

        def reject(*args, **kwargs):
            raise ValueError(next(errors))

        monkeypatch.setattr(orchestrator.governor, "plan_task_graph_patch", reject)
        results = [orchestrator._replan_stalled_graph(
            goal="deliver", blocked_nodes=[], reason="stalled",
        ) for _ in range(3)]
        assert [result["repeat_count"] for result in results] == [1, 1, 2]


def test_missing_handoff_is_reported_without_promoting_consumer(tmp_path):
    with Orchestrator(tmp_path) as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="producer", goal="create SQL", role="coder", status="completed",
            output_artifacts=["artifact:db_init_sql"],
        ))
        graph.add_node(TaskNode(
            node_id="consumer", goal="use SQL", role="coder",
            dependencies=["producer"], input_artifacts=["artifact:db_init_sql"],
        ))
        assert graph.promote_waiting_nodes() == []
        assert orchestrator._check_for_blocked_tasks() == ["consumer"]
        summary = json.loads(orchestrator._build_governor_planning_context()[0])
        consumer = next(node for node in summary if node["node_id"] == "consumer")
        assert consumer["readiness_blockers"] == {
            "unsatisfied_dependencies": [],
            "missing_input_artifacts": ["artifact:db_init_sql"],
        }
        assert graph.get_node("consumer").status == "admitted"


def test_stalled_graph_recovery_applies_governor_patch(tmp_path):
    class FakeClient:
        def chat(self, _messages):
            return json.dumps({
                "add_nodes": [{
                    "node_id": "replacement",
                    "goal": "replace failed evidence collector",
                    "task_type": "implementation",
                        "required_capabilities": ["research"],
                        "dependencies": [],
                        "acceptance_criteria": ["evidence exists"],
                        "owned_paths": ["deliverables/evidence/"],
                        "output_artifacts": ["evidence.json"],
                        "recovery_of": "T-001",
                }],
                "update_nodes": [
                    {"node_id": "T-001", "status": "superseded"},
                    {"node_id": "T-002", "dependencies": ["replacement"]},
                ],
                "cancel_nodes": [],
                "add_edges": [],
                "remove_edges": [],
                "reason_codes": ["CAPABILITY_SEPARATION"],
                "reasoning": "replace failed strategy",
                "planning_status": "OK",
            }, ensure_ascii=False)

    orchestrator = Orchestrator(tmp_path, max_iterations=0)
    failed = TaskNode(
        node_id="T-001",
        goal="collect evidence",
        role="researcher",
        status="failed",
    )
    failed.metadata["_terminal_failure"] = True
    blocked = TaskNode(
        node_id="T-002",
        goal="use evidence",
        role="reviewer",
        status="waiting_dependencies",
        dependencies=["T-001"],
    )
    orchestrator.task_graph.add_node(failed)
    orchestrator.task_graph.add_node(blocked)
    orchestrator.task_graph.add_edge(TaskEdge("T-002", "T-001", "depends_on"))
    orchestrator.governor.enable_llm_worker_matching(FakeClient())

    result = orchestrator._replan_stalled_graph(
        goal="deliver",
        blocked_nodes=["T-002"],
        reason="blocked_by_failed_dependency",
    )

    assert result["changed"] is True
    # Doc 14 §12:50 (user directive): a TERMINAL-failed predecessor keeps its
    # failed status — takeover must not launder the terminal markers into
    # "superseded" (and an explicit patch status change is dropped at the
    # mutation boundary). The replacement node rebuilds the track while the
    # failure record stays visible.
    terminal = orchestrator.task_graph.get_node("T-001")
    assert terminal.status == "failed"
    assert terminal.metadata["_terminal_failure"] is True
    assert orchestrator.task_graph.get_node("T-002").dependencies != ["T-001"]
    replacement_id = orchestrator.task_graph.get_node("T-002").dependencies[0]
    assert orchestrator.task_graph.get_node(replacement_id).goal == (
        "replace failed evidence collector"
    )


def test_terminal_convergence_syncs_snapshot_and_blocks_dependents(tmp_path):
    """User directive 2026-09-12 (live r11 resume #10): terminalization must
    converge run state synchronously — the stale ``ready:1 / failed:7``
    snapshot window after N-002-R8-RECOVERY's node_fatal left monitors and
    resumes reading pre-terminal truth for minutes while dependents sat
    unblocked and the stalled replan waited behind unrelated preambles."""
    orchestrator = Orchestrator(tmp_path, run_id="terminal-converge")
    graph = orchestrator.task_graph
    failed = TaskNode(node_id="A", goal="backend", role="coder", status="failed")
    failed.metadata["_terminal_failure"] = True
    failed.metadata["_terminal_reason"] = "recovery_exhausted"
    dependent = TaskNode(
        node_id="B", goal="boundary tests", role="tester",
        status="admitted", dependencies=["A"],
    )
    graph.add_node(failed)
    graph.add_node(dependent)
    graph.add_edge(TaskEdge("B", "A", "depends_on"))
    # Stale snapshot that no longer reflects graph truth.
    orchestrator.runtime_state_manager.state.nodes_by_status = {"ready": 99}
    orchestrator.runtime_state_manager.state.ready_nodes = ["GHOST"]

    orchestrator._converge_runtime_state_after_terminal("A", "recovery_exhausted")

    state = orchestrator.runtime_state_manager.state
    assert state.nodes_by_status.get("failed") == 1
    assert state.nodes_by_status.get("ready", 0) == 0
    assert state.ready_nodes == []
    assert state.last_decision["action"] == "terminal_state_converged"
    assert state.last_decision["node_id"] == "A"
    assert state.last_decision["blocked_dependents"] == ["B"]
    assert graph.get_node("B").status == "blocked"
    assert graph.get_node("B").metadata["_blocked_by_terminal_dep"] == "A"
    # Durable snapshot on disk matches (monitors and resumes read the file).
    on_disk = json.loads(
        orchestrator.runtime_state_manager.path.read_text(encoding="utf-8")
    )
    assert on_disk["nodes_by_status"].get("failed") == 1
    assert on_disk["ready_nodes"] == []
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "orchestrator.terminal_convergence.converged" in events
    assert "orchestrator.node_blocked_by_terminal" in events


def test_recovery_node_is_not_blocked_by_its_own_terminal_recovery_target(tmp_path):
    """Fix 18 (live r11 resume #29 blocked 2026-09-13 19:43:51): the last
    legitimate repair G-001-REPAIR-ROLLBACK-PROOF-JSON declared
    recovery_of=REPAIR-V4 and kept REPAIR-V4 as its only dependency; V4's
    ``_terminal_failure`` marker then blocked the repair at birth, so the run
    went blocked with a healthy dispatchable repair in the graph and no
    executable work left. A recovery node's terminal-failed predecessor is
    its reason to exist, not an impossible dependency; plain dependents must
    still be blocked by terminal deps (r11 N-006 rule)."""
    orchestrator = Orchestrator(tmp_path, run_id="recovery-self-dep")
    graph = orchestrator.task_graph
    failed = TaskNode(node_id="V4", goal="rollback demo", role="coder", status="failed")
    failed.metadata["_terminal_failure"] = True
    failed.metadata["_terminal_reason"] = "change_of_approach_required"
    repair = TaskNode(
        node_id="REPAIR", goal="repair rollback_proof.json", role="coder",
        status="admitted", dependencies=["V4"],
    )
    repair.metadata["recovery_of"] = "V4"
    plain = TaskNode(
        node_id="PLAIN", goal="boundary tests", role="tester",
        status="admitted", dependencies=["V4"],
    )
    graph.add_node(failed)
    graph.add_node(repair)
    graph.add_node(plain)
    graph.add_edge(TaskEdge("REPAIR", "V4", "depends_on"))
    graph.add_edge(TaskEdge("PLAIN", "V4", "depends_on"))

    blocked = orchestrator._check_for_blocked_tasks()

    assert "REPAIR" not in blocked
    assert graph.get_node("REPAIR").status == "admitted"
    assert "_blocked_by_terminal_dep" not in graph.get_node("REPAIR").metadata
    # Control: a non-recovery dependent is still permanently blocked.
    assert "PLAIN" in blocked
    assert graph.get_node("PLAIN").status == "blocked"
    assert graph.get_node("PLAIN").metadata["_blocked_by_terminal_dep"] == "V4"


def test_takeover_normalization_prunes_dependency_on_own_recovery_target(tmp_path):
    """Fix 18a (same live deadlock): the takeover patch for
    G-001-REPAIR-ROLLBACK-PROOF-JSON kept REPAIR-V4 — its own recovery_of
    target — as its sole dependency. A replacement must not depend on the
    node it recovers: that dependency can never be satisfied, so takeover
    normalization prunes it instead of letting the terminal-dep rule
    stillbirth the repair."""
    from AstralNexus.core.governor import TaskGraphPatch

    orchestrator = Orchestrator(tmp_path, run_id="self-dep-prune")
    graph = orchestrator.task_graph
    failed = TaskNode(node_id="V4", goal="rollback demo", role="coder", status="failed")
    failed.metadata["_terminal_failure"] = True
    graph.add_node(failed)

    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "REPAIR",
        "goal": "repair rollback_proof.json",
        "role": "coder",
        "dependencies": ["V4"],
        "recovery_of": "V4",
        "owned_paths": ["deliverables/runs/validate/rollback_proof.json"],
    }])
    normalized = orchestrator._normalize_recovery_ownership_transfer(patch)

    added = normalized.add_nodes[0]
    assert added["dependencies"] == []
    assert added["recovery_of"] == "V4"
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "governor.patch.recovery_self_dependency_pruned" in events


def test_zombie_admitted_node_with_permanently_dead_deps_is_persisted_blocked(tmp_path):
    """Fix 20 (live r11 resume #31 blocked 2026-09-13 20:24): fix 19
    normalized the report-regeneration takeover into recovery_of=
    G-001-GENERATE-TEST-REPORT, but the patch validator requires a recovery
    target to be failed/repair_pending/blocked — and the zombie was still
    ``admitted`` because its dead deps (superseded with no living successor,
    cancelled) fell outside the terminal-failure-only blocking rule. A node
    whose dependency can never be satisfied again must persist as blocked so
    recovery patches may legally target it; a superseded dep WITH a living
    successor must not block its consumers."""
    orchestrator = Orchestrator(tmp_path, run_id="zombie-blocked")
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="DEAD-SUP", goal="boundary tests", role="tester", status="superseded"))
    graph.add_node(TaskNode(
        node_id="DEAD-CAN", goal="boundary rerun", role="tester", status="cancelled"))
    zombie = TaskNode(
        node_id="REPORT", goal="aggregate report", role="synthesizer",
        status="admitted", dependencies=["DEAD-SUP", "DEAD-CAN"],
    )
    graph.add_node(zombie)
    graph.add_node(TaskNode(node_id="OLD", goal="seed db", role="coder", status="superseded"))
    successor = TaskNode(node_id="NEW", goal="seed db v2", role="coder", status="admitted")
    successor.metadata["recovery_of"] = "OLD"
    graph.add_node(successor)
    alive = TaskNode(
        node_id="CONSUMER", goal="use seed", role="tester",
        status="admitted", dependencies=["OLD"],
    )
    graph.add_node(alive)

    blocked = orchestrator._check_for_blocked_tasks()

    assert "REPORT" in blocked
    assert graph.get_node("REPORT").status == "blocked"
    assert graph.get_node("REPORT").metadata["_blocked_by_terminal_dep"] == "DEAD-SUP"
    # Control: superseded dep with a living successor keeps the consumer alive.
    assert "CONSUMER" not in blocked
    assert graph.get_node("CONSUMER").status == "admitted"
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "orchestrator.node_blocked_by_terminal" in events


def test_blocked_consumer_revives_when_dead_dep_is_replaced(tmp_path):
    """Fix 24 (live r12): I-001 was persisted blocked on failed-terminal
    A-001; the takeover normalization then superseded A-001 and rewired
    I-001 onto the living replacement — but nothing ever moved I-001 out of
    blocked, so the repaired consumer stayed undispatchable and every later
    wave had to burn a redundant recovery generation on it. blocked is
    recoverable: once none of the node's dependencies is dead or missing,
    the marker-bearing node returns to waiting_dependencies and re-enters
    normal readiness promotion."""
    orchestrator = Orchestrator(tmp_path, run_id="blocked-revival")
    graph = orchestrator.task_graph
    dep = TaskNode(node_id="DEP", goal="write the architecture", role="architect", status="failed")
    dep.metadata["_terminal_failure"] = True
    dep.metadata["_terminal_reason"] = "recovery_exhausted"
    graph.add_node(dep)
    node = TaskNode(
        node_id="CONSUMER", goal="implement the backend", role="coder",
        status="admitted", dependencies=["DEP"],
    )
    graph.add_node(node)
    dep2 = TaskNode(node_id="DEP2", goal="seed the database", role="coder", status="failed")
    dep2.metadata["_terminal_failure"] = True
    graph.add_node(dep2)
    control = TaskNode(
        node_id="STILL-BLOCKED", goal="run the tests", role="tester",
        status="admitted", dependencies=["DEP2"],
    )
    graph.add_node(control)

    first = orchestrator._check_for_blocked_tasks()
    assert "CONSUMER" in first
    assert graph.get_node("CONSUMER").status == "blocked"
    assert "STILL-BLOCKED" in first

    # The takeover lands: DEP is superseded by a living replacement and the
    # consumer rewiring moves CONSUMER onto it.
    graph.update_node_status("DEP", "superseded")
    replacement = TaskNode(node_id="DEP-R1", goal="rewrite the architecture", role="architect", status="admitted")
    replacement.metadata["recovery_of"] = "DEP"
    graph.add_node(replacement)
    graph.get_node("CONSUMER").dependencies = ["DEP-R1"]

    second = orchestrator._check_for_blocked_tasks()

    assert "CONSUMER" not in second
    revived = graph.get_node("CONSUMER")
    assert revived.status == "waiting_dependencies"
    assert "_blocked_by_terminal_dep" not in revived.metadata
    # Control: a consumer whose dependency is still terminally failed stays blocked.
    assert "STILL-BLOCKED" in second
    assert graph.get_node("STILL-BLOCKED").status == "blocked"
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "orchestrator.node_unblocked_by_dependency_recovery" in events


# ---------------------------------------------------------------------------
# Round 8 (live r11 run-blocked postmortem, 10:15 UTC 2026-09-12): the
# stalled replanner proposed three identical in-lineage init_db.py repairs
# because (a) the verifier's blocking-gap truth never reached its context —
# the stalled path passed verification_result=None into root-cause
# resolution — and (b) the lineage-cap teaching text names no concrete
# files, so the planner described repairs in goal text while declaring no
# owned_paths; routing enforcement saw nothing exempt and the run blocked
# on "same autonomous recovery outcome repeated three times".
# ---------------------------------------------------------------------------


def _terminal_with_receipt() -> TaskNode:
    node = TaskNode(
        node_id="T-FAIL", goal="backend service", role="coder", status="failed",
        owned_paths=["deliverables/app/tests/"],
    )
    node.metadata["_terminal_failure"] = True
    node.metadata["_terminal_reason"] = (
        "invalid governed verification recovery patch: 需要改变方案 "
        "(change of approach required): lineage cap 6"
    )
    node.metadata["_verification_receipt"] = {
        "verdict": "replan_required",
        "overall_score": 0.8,
        "failed_dimensions": [{
            "name": "goal_coverage",
            "score": 0.0,
            "details": (
                "Blocking gaps: curl http://localhost:5000/health was not "
                "executed — live HTTP receipts are missing; "
                "deliverables/app/backend/init_db.py state already verified"
            ),
        }],
    }
    return node


def test_stalled_replan_prompt_carries_cap_exemption_recipe(tmp_path):
    captured = {}

    class FakeClient:
        def chat(self, messages):
            captured["prompt"] = json.dumps(messages, ensure_ascii=False)
            return json.dumps({
                "add_nodes": [{
                    "node_id": "T-FIX",
                    "goal": "repair init_db.py and capture live HTTP receipts",
                    "task_type": "implementation",
                    "required_capabilities": ["coding"],
                    "dependencies": [],
                    "acceptance_criteria": ["service listens and /health 200"],
                    "owned_paths": ["deliverables/app/backend/init_db.py"],
                    "output_artifacts": ["fix_report.json"],
                    "recovery_of": "T-FAIL",
                }],
                "update_nodes": [
                    {"node_id": "T-USE", "dependencies": ["T-FIX"]},
                ],
                "cancel_nodes": [],
                "add_edges": [],
                "remove_edges": [],
                "reason_codes": ["ROOT_CAUSE_ROUTING"],
                "reasoning": "declare ownership of the completed-owned file",
                "planning_status": "OK",
            }, ensure_ascii=False)

    orchestrator = Orchestrator(tmp_path, max_iterations=0)
    graph = orchestrator.task_graph
    owner = TaskNode(
        node_id="N-OWNER", goal="backend component", role="coder",
        status="completed",
        owned_paths=["deliverables/app/backend/init_db.py"],
    )
    graph.add_node(owner)
    graph.add_node(_terminal_with_receipt())
    consumer = TaskNode(
        node_id="T-USE", goal="boundary tests", role="tester",
        status="admitted", dependencies=["T-FAIL"],
    )
    graph.add_node(consumer)
    orchestrator.governor.enable_llm_worker_matching(FakeClient())

    result = orchestrator._replan_stalled_graph(
        goal="deliver", blocked_nodes=["T-USE"],
        reason="blocked_by_failed_dependency",
    )

    assert result["changed"] is True
    prompt = captured["prompt"]
    # The recipe names the concrete file, its inactive owner and the
    # declaration requirement — the exact knowledge the live planner lacked.
    assert "谱系 cap 豁免配方" in prompt
    assert "init_db.py" in prompt
    assert "N-OWNER" in prompt
    assert "owned_paths" in prompt
    # The verifier's current blocking-gap truth rides in the dossier.
    assert "latest_verification" in prompt
    assert "Blocking gaps" in prompt
    # Round 8 fix 5: stale-goal correction rides alongside the recipe.
    assert "重要纠偏" in prompt
    # Ownership-declaring patch passes routing enforcement and applies.
    assert graph.get_node("T-USE").dependencies == ["T-FIX"]


def test_latest_verification_view_prefers_memory_then_receipt(tmp_path):
    orchestrator = Orchestrator(tmp_path)
    node = _terminal_with_receipt()
    orchestrator.task_graph.add_node(node)

    # Receipt fallback normalizes for detail mining.
    view = orchestrator._latest_verification_view(node)
    assert view is not None
    assert view.verdict == "replan_required"
    assert view.dimensions[0]["details"].startswith("Blocking gaps")

    # Live in-memory result wins over the persisted receipt.
    sentinel = object()
    orchestrator.verifier.verification_results["T-FAIL"] = sentinel
    assert orchestrator._latest_verification_view(node) is sentinel

    # The normalized view feeds root-cause resolution: the completed owner
    # of init_db.py is resolved as upstream root cause.
    owner = TaskNode(
        node_id="N-OWNER", goal="backend component", role="coder",
        status="completed",
        owned_paths=["deliverables/app/backend/init_db.py"],
    )
    orchestrator.task_graph.add_node(owner)
    del orchestrator.verifier.verification_results["T-FAIL"]
    resolved = orchestrator._resolve_declared_root_causes(
        node, orchestrator._latest_verification_view(node), {}
    )
    assert resolved
    assert all(owner_id == "N-OWNER" for owner_id, _paths in resolved.values())


def test_latest_verification_view_falls_back_to_ancestor_receipt(tmp_path):
    """Round 11 (live r11 resume #16): V3 died unverified (exec partial)
    and V2's one-freshness-receipt-away verdict would have been lost with
    it — the recovery_of chain must serve the nearest ancestor's receipt so
    post-restart planning still sees the lineage's freshest real truth."""
    orchestrator = Orchestrator(tmp_path)
    parent = TaskNode(
        node_id="T-PARENT", goal="backend component", role="coder",
        status="superseded",
    )
    parent.metadata["_verification_receipt"] = {
        "verdict": "replan_required",
        "overall_score": 0.8,
        "failed_dimensions": [{
            "name": "goal_coverage", "score": 0.0,
            "details": "403/stdout evidence confirmed; report freshness "
                       "receipt missing",
        }],
    }
    child = TaskNode(
        node_id="T-CHILD", goal="re-capture freshness receipts",
        role="coder", status="failed",
    )
    child.metadata["recovery_of"] = "T-PARENT"
    orchestrator.task_graph.add_node(parent)
    orchestrator.task_graph.add_node(child)

    view = orchestrator._latest_verification_view(child)
    assert view is not None
    assert view.verdict == "replan_required"
    assert "freshness" in view.dimensions[0]["details"]

    orphan = TaskNode(
        node_id="T-ORPHAN", goal="g", role="coder", status="failed",
    )
    orchestrator.task_graph.add_node(orphan)
    assert orchestrator._latest_verification_view(orphan) is None


def test_verification_recovery_persists_receipt(tmp_path):
    """Round 11: EVERY verification-recovery attempt persists the current
    verdict — superseded generations must carry their truth on disk (entry
    persistence; the 3-strike terminal path reuses the same helper)."""
    orchestrator = Orchestrator(tmp_path)
    node = TaskNode(
        node_id="T-VR", goal="backend component", role="coder",
        status="repair_pending",
    )
    orchestrator.task_graph.add_node(node)
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.8,
        dimensions=[
            SimpleNamespace(
                name="goal_coverage", score=0.0, passed=False,
                details="freshness receipt missing", evidence_refs=[],
            ),
            SimpleNamespace(
                name="evidence_support", score=1.0, passed=True,
                details="ok", evidence_refs=[],
            ),
        ],
    )

    def _reject(**_kwargs):
        raise ValueError("需要改变方案")

    orchestrator.governor.plan_verification_recovery = _reject
    result = orchestrator._apply_governed_verification_recovery(
        node, verification_result
    )

    assert result["rejected"] is True
    receipt = node.metadata["_verification_receipt"]
    assert receipt["verdict"] == "replan_required"
    assert receipt["overall_score"] == 0.8
    assert [d["name"] for d in receipt["failed_dimensions"]] == [
        "goal_coverage"
    ]


def test_verification_recovery_takeover_inherits_failed_node_ownership(tmp_path):
    """Round 12 (live r11 resume #18): REPAIR-ROOT sat one file_read away
    from approval, yet the provider returned recovery_of takeovers with
    EMPTY owned_paths three times — depth cap (attempt_count=12)
    terminalized it despite the recipe riding in the prompt.  An explicit
    takeover must inherit the failed node's ownership contract so the cap
    exemption is structural, not narrative."""
    orchestrator = Orchestrator(tmp_path / "inherit", max_iterations=0)
    _capped_self_owned_graph(orchestrator)
    node = orchestrator.task_graph.get_node("T-FAIL")
    orchestrator.task_graph.update_node_status("T-FAIL", "repair_pending")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.8,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: deliverables/app/backend/init_db_stdout.log "
                    "content was not read — no evidence of the required line",
            evidence_refs=[],
        )],
    )
    patch = TaskGraphPatch(
        add_nodes=[{
            "node_id": "T-FAIL-R1",
            "goal": "read the stdout log and capture the missing receipts",
            "task_type": "implementation",
            "required_capabilities": ["coding"],
            "dependencies": [],
            "acceptance_criteria": ["log content receipt on disk"],
            "recovery_of": "T-FAIL",
            "owned_paths": [],
            "output_artifacts": [],
        }],
    )
    orchestrator.governor.plan_verification_recovery = lambda **_kw: patch
    result = orchestrator._apply_governed_verification_recovery(
        node, verification_result
    )

    assert result.get("rejected") is not True, result
    new = orchestrator.task_graph.get_node("T-FAIL-R1")
    assert new is not None, "takeover node was not applied"
    assert "deliverables/app/backend/" in new.owned_paths
    inherited = [
        event for event in orchestrator.trace_recorder.events
        if event["event_type"]
        == "governor.verification_recovery.takeover_ownership_inherited"
    ]
    assert inherited


def test_stalled_normalization_infers_takeover_of_blocked_conflict(tmp_path):
    """Round 12 (live r11 resume #18, blocked 19:21): three stalled
    proposals owned exactly the blocked security node's security_v6 outputs
    with no recovery_of — ownership-conflict validation rejected all three
    and the run blocked.  A UNIQUE conflict with a blocked/repair_pending
    node must normalize into an explicit takeover."""
    orchestrator = Orchestrator(tmp_path / "takeover-norm", max_iterations=0)
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="C-DONE", goal="backend", role="coder", status="completed",
    ))
    graph.add_node(TaskNode(
        node_id="C-FAILED", goal="prior backend", role="coder",
        status="failed",
    ))
    graph.add_node(TaskNode(
        node_id="B-SEC", goal="boundary tests", role="tester",
        status="blocked", dependencies=["C-FAILED"],
        owned_paths=[
            "deliverables/runs/security_v6/unauthorized_assign.json"
        ],
        output_artifacts=["sec_receipts"],
    ))
    graph.add_node(TaskNode(
        node_id="B-AGG", goal="aggregate report", role="tester",
        status="admitted", dependencies=["B-SEC"],
    ))

    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "B-SEC-NEW",
        "goal": "run boundary tests against the verified backend",
        "role": "tester",
        "status": "ready",
        "dependencies": ["C-DONE"],
        "acceptance_criteria": ["live receipts"],
        "owned_paths": [
            "deliverables/runs/security_v6/unauthorized_assign.json"
        ],
        "output_artifacts": [],
    }])
    out = orchestrator._normalize_recovery_ownership_transfer(patch)
    assert out.add_nodes[0]["recovery_of"] == "B-SEC"
    assert out.add_nodes[0]["metadata"]["auto_recovery_of"] is True
    rewired = next(
        (
            item for item in out.update_nodes
            if str(item.get("node_id")) == "B-AGG"
        ),
        None,
    )
    assert rewired is not None and rewired["dependencies"] == ["B-SEC-NEW"]
    events = [
        event for event in orchestrator.trace_recorder.events
        if event["event_type"]
        == "governor.stalled_recovery.normalized_blocked_takeover"
    ]
    assert events

    clean = TaskGraphPatch(add_nodes=[{
        "node_id": "N-NEW",
        "goal": "aggregate",
        "role": "tester",
        "status": "ready",
        "dependencies": ["C-DONE"],
        "acceptance_criteria": ["report"],
        "owned_paths": ["deliverables/runs/report_v9/"],
        "output_artifacts": [],
    }])
    out2 = orchestrator._normalize_recovery_ownership_transfer(clean)
    assert not str(out2.add_nodes[0].get("recovery_of") or "").strip()


def test_verification_recovery_takeover_grants_mined_root_cause_paths(tmp_path):
    """Round 13 (live r11 resume #19, churn diag 20:31-20:37): the failed
    node was a TESTER while the verification-recovery proposal was a CODER
    fixing the missing backend assign endpoint — its file_write to main.py
    died on "outside the Worker owned_paths contract" (inherited/declared
    tester paths locked it out) and the worker flailed into
    runtime_no_progress.  The recipe mined exactly that file (completed =
    inactive owner); the takeover must be GRANTED the mined root-cause
    paths even when the provider declares tester-only ownership."""
    orchestrator = Orchestrator(tmp_path / "grant", max_iterations=0)
    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="N-BACKEND", goal="backend", role="coder",
        status="completed",
        owned_paths=["deliverables/app/backend/main.py"],
    ))
    graph.add_node(TaskNode(
        node_id="T-SEC", goal="boundary tests", role="tester",
        status="repair_pending",
        owned_paths=[
            "deliverables/runs/security_v3/unauthorized_assign.json"
        ],
    ))
    node = graph.get_node("T-SEC")
    verification_result = SimpleNamespace(
        verdict=SimpleNamespace(value="replan_required"),
        overall_score=0.5,
        dimensions=[SimpleNamespace(
            name="goal_coverage", score=0.0, passed=False,
            details="Blocking gaps: assign endpoint is missing in "
                    "deliverables/app/backend/main.py — POST returns 404, "
                    "no evidence of real execution",
            evidence_refs=[],
        )],
    )
    patch = TaskGraphPatch(
        add_nodes=[{
            "node_id": "T-SEC-R1",
            "goal": "add the assign endpoint to the backend and rerun tests",
            "task_type": "implementation",
            "required_capabilities": ["coding"],
            "dependencies": [],
            "acceptance_criteria": ["live curl receipts"],
            "recovery_of": "T-SEC",
            "owned_paths": [
                "deliverables/runs/security_v3/unauthorized_assign.json"
            ],
            "output_artifacts": [],
        }],
    )
    orchestrator.governor.plan_verification_recovery = lambda **_kw: patch
    result = orchestrator._apply_governed_verification_recovery(
        node, verification_result
    )

    assert result.get("rejected") is not True, result
    new = graph.get_node("T-SEC-R1")
    assert new is not None, "takeover was not applied"
    assert "deliverables/app/backend/main.py" in new.owned_paths
    assert (
        "deliverables/runs/security_v3/unauthorized_assign.json"
        in new.owned_paths
    )
    granted = [
        event for event in orchestrator.trace_recorder.events
        if event["event_type"]
        == "governor.verification_recovery.takeover_root_cause_paths_granted"
    ]
    assert granted
    granted_paths = granted[0]["payload"]["granted_paths"]
    assert "deliverables/app/backend/main.py" in granted_paths


def test_never_dispatched_lineage_attempts_do_not_block_repeat(tmp_path):
    """Round 13 (live r11 resume #20, blocked 21:48): two match-blocked
    repair nodes never executed, yet their fingerprint poisoned the lineage
    history and the repeat guard rejected every later proposal of the same
    — never actually tried — approach three times into a run block.
    Provably match-blocked attempts (last_failure.stage ==
    worker_profile_matching) must filter out of the identical-approach
    check."""
    orchestrator = Orchestrator(tmp_path / "repeat-filter", max_iterations=0)
    graph = orchestrator.task_graph
    match_blocked = {
        "error_type": "CapabilityGap",
        "message": "GlobalGovernor selected no executable Worker Profile",
        "stage": "worker_profile_matching",
    }
    graph.add_node(TaskNode(
        node_id="L-KEY", goal="repair", role="coder", status="superseded",
    ))
    graph.add_node(TaskNode(
        node_id="L-ADDED", goal="repair", role="coder",
        status="repair_pending",
    ))
    graph.get_node("L-KEY").metadata["last_failure"] = dict(match_blocked)
    graph.get_node("L-ADDED").metadata["last_failure"] = dict(match_blocked)
    node = TaskNode(
        node_id="L-NOW", goal="repair", role="coder", status="repair_pending",
    )
    graph.add_node(node)
    node.metadata["_lineage_recovery_history"] = [
        {"node_id": "L-KEY", "fingerprint": "fp-same", "added": ["L-ADDED"]},
    ]
    # Both nodes of the attempt were match-blocked: never actually tried.
    orchestrator._check_lineage_recovery_repeat(node, "fp-same")

    # A genuinely executed attempt still blocks the identical approach.
    graph.get_node("L-KEY").metadata["last_failure"] = {
        "error_type": "RuntimeError", "message": "exec failed",
        "stage": "execution",
    }
    with pytest.raises(ValueError, match="already tried this exact approach"):
        orchestrator._check_lineage_recovery_repeat(node, "fp-same")


def test_never_dispatched_attempts_do_not_consume_depth_cap(tmp_path):
    """Round 13: match-blocked generations must not consume the lineage
    depth-cap budget either — the cap counts TRIED approaches."""
    orchestrator = Orchestrator(tmp_path / "depth-filter", max_iterations=0)
    graph = orchestrator.task_graph
    node = TaskNode(
        node_id="D-NOW", goal="repair", role="coder", status="repair_pending",
    )
    graph.add_node(node)
    history = []
    for i in range(7):
        graph.add_node(TaskNode(
            node_id=f"D-GHOST-{i}", goal="repair", role="coder",
            status="superseded",
        ))
        graph.get_node(f"D-GHOST-{i}").metadata["last_failure"] = {
            "error_type": "CapabilityGap",
            "message": "no profile",
            "stage": "worker_profile_matching",
        }
        history.append({
            "node_id": f"D-GHOST-{i}", "fingerprint": f"fp-{i}", "added": [],
        })
    node.metadata["_lineage_recovery_history"] = history
    # All seven generations were match-blocked before dispatch: no cap.
    orchestrator._check_lineage_recovery_repeat(node, "fp-new")

    # Executed generations count: seven real attempts exceed the cap of 6.
    for i in range(7):
        graph.get_node(f"D-GHOST-{i}").metadata["last_failure"] = {
            "stage": "execution",
        }
    with pytest.raises(ValueError, match="cap 6"):
        orchestrator._check_lineage_recovery_repeat(node, "fp-new")


def _capped_self_owned_graph(orchestrator) -> None:
    """Live r11 shape: terminal node owns the backend tree itself and its
    lineage history is at the depth cap (6 entries, all distinct)."""
    graph = orchestrator.task_graph
    owner = TaskNode(
        node_id="N-OWNER", goal="backend component", role="coder",
        status="completed",
        owned_paths=["deliverables/app/backend/main.py"],
    )
    graph.add_node(owner)
    terminal = TaskNode(
        node_id="T-FAIL", goal="backend service", role="coder", status="failed",
        owned_paths=["deliverables/app/backend/"],
    )
    terminal.metadata["_terminal_failure"] = True
    terminal.metadata["_terminal_reason"] = "recovery_exhausted"
    terminal.metadata["_verification_receipt"] = {
        "verdict": "replan_required",
        "overall_score": 0.8,
        "failed_dimensions": [{
            "name": "goal_coverage",
            "score": 0.0,
            "details": (
                "Blocking gaps: deliverables/app/backend/main.py exception "
                "handlers are missing JSONResponse returns — live HTTP "
                "receipts absent"
            ),
        }],
    }
    terminal.metadata["_lineage_recovery_history"] = [
        {"node_id": f"GEN-{i}", "fingerprint": f"deadbeef0{i}", "added": []}
        for i in range(6)
    ]
    graph.add_node(terminal)
    consumer = TaskNode(
        node_id="T-USE", goal="boundary tests", role="tester",
        status="admitted", dependencies=["T-FAIL"],
    )
    graph.add_node(consumer)


def _takeover_client(declare_ownership: bool):
    class FakeClient:
        def chat(self, _messages):
            node = {
                "node_id": "T-TAKEOVER",
                "goal": "repair main.py handlers and capture live HTTP receipts",
                "task_type": "implementation",
                "required_capabilities": ["coding"],
                "dependencies": [],
                "acceptance_criteria": ["service listens and /health 200"],
                "output_artifacts": ["fix_report.json"],
                "recovery_of": "T-FAIL",
            }
            if declare_ownership:
                node["owned_paths"] = ["deliverables/app/backend/main.py"]
            else:
                node["owned_paths"] = ["deliverables/runs/validate/T-TAKEOVER/"]
            return json.dumps({
                "add_nodes": [node],
                "update_nodes": [
                    {"node_id": "T-USE", "dependencies": ["T-TAKEOVER"]},
                ],
                "cancel_nodes": [],
                "add_edges": [],
                "remove_edges": [],
                "reason_codes": ["TAKEOVER"],
                "reasoning": "take over the offending files",
                "planning_status": "OK",
            }, ensure_ascii=False)

    return FakeClient()


def test_self_owned_offender_takeover_exempts_lineage_depth_cap(tmp_path):
    orchestrator = Orchestrator(tmp_path / "exempt", max_iterations=0)
    _capped_self_owned_graph(orchestrator)
    orchestrator.governor.enable_llm_worker_matching(_takeover_client(True))

    result = orchestrator._replan_stalled_graph(
        goal="deliver", blocked_nodes=["T-USE"],
        reason="blocked_by_failed_dependency",
    )

    # Ownership-declaring takeover passes the depth cap despite six
    # generations and zero upstream-resolvable tokens.
    assert result["changed"] is True
    assert orchestrator.task_graph.get_node("T-USE").dependencies == [
        "T-TAKEOVER"
    ]


def test_same_shape_clone_still_blocked_at_lineage_depth_cap(tmp_path):
    orchestrator = Orchestrator(tmp_path / "blocked", max_iterations=0)
    _capped_self_owned_graph(orchestrator)
    orchestrator.governor.enable_llm_worker_matching(_takeover_client(False))

    result = orchestrator._replan_stalled_graph(
        goal="deliver", blocked_nodes=["T-USE"],
        reason="blocked_by_failed_dependency",
    )

    # Without the ownership declaration the cap still rejects — the escape
    # is narrow, not a general cap removal.
    assert result["changed"] is False
    assert "需要改变方案" in result["error"]
    blocked_events = [
        event for event in orchestrator.trace_recorder.events
        if event["event_type"]
        == "governor.verification_recovery.lineage_depth_blocked"
    ]
    assert blocked_events
    assert orchestrator.task_graph.get_node("T-USE").dependencies == ["T-FAIL"]


def test_independent_verification_patch_skips_lineage_depth_cap(tmp_path):
    """Round 10 (live r11 resume #15, blocked in 80s): an independent
    verification node deriving NOTHING from the capped lineage — no
    recovery_of, disjoint receipt paths, rewire of the blocked consumer —
    is exactly the "escalate with genuinely new evidence" shape the cap's
    own rejection text recommends, so the depth cap must not touch it."""

    class FakeClient:
        def chat(self, _messages):
            return json.dumps({
                "add_nodes": [{
                    "node_id": "T-VERIFY",
                    "goal": "capture live RBAC 403 rejection receipts "
                            "against the running service",
                    "task_type": "verification",
                    "required_capabilities": ["testing"],
                    "dependencies": [],
                    "acceptance_criteria": ["curl receipts written to disk"],
                    "owned_paths": ["deliverables/runs/validate/T-VERIFY/"],
                    "output_artifacts": ["rbac_receipts.json"],
                }],
                "update_nodes": [
                    {"node_id": "T-USE", "dependencies": ["T-VERIFY"]},
                ],
                "cancel_nodes": [],
                "add_edges": [],
                "remove_edges": [],
                "reason_codes": ["VERIFICATION_GAP"],
                "reasoning": "capture the missing rejection evidence",
                "planning_status": "OK",
            }, ensure_ascii=False)

    orchestrator = Orchestrator(tmp_path / "independent", max_iterations=0)
    _capped_self_owned_graph(orchestrator)
    orchestrator.governor.enable_llm_worker_matching(FakeClient())

    result = orchestrator._replan_stalled_graph(
        goal="deliver", blocked_nodes=["T-USE"],
        reason="blocked_by_failed_dependency",
    )

    assert result["changed"] is True
    assert orchestrator.task_graph.get_node("T-USE").dependencies == [
        "T-VERIFY"
    ]
    depth_blocked = [
        event for event in orchestrator.trace_recorder.events
        if event["event_type"]
        == "governor.verification_recovery.lineage_depth_blocked"
    ]
    assert depth_blocked == []


def test_evidence_gap_phrases_pass_root_cause_gate(tmp_path):
    """Round 8 fix 5 (resume #13 blocked in 90s): the live receipt said
    "was not executed / no evidence / Blocking gaps" — zero original gate
    words — so weak-text mining starved and the whole recipe/exemption
    chain resolved to {}.  Evidence-gap phrasing must pass the gate."""
    orchestrator = Orchestrator(tmp_path)
    node = TaskNode(
        node_id="T-FAIL", goal="backend service", role="coder", status="failed",
        owned_paths=["deliverables/app/tests/"],
    )
    node.metadata["_verification_receipt"] = {
        "verdict": "replan_required",
        "overall_score": 0.8,
        "failed_dimensions": [{
            "name": "goal_coverage",
            "score": 0.0,
            "details": (
                "Blocking gaps: curl /health was not executed — no evidence "
                "of HTTP 200; deliverables/app/backend/main.py handlers "
                "unverified at runtime"
            ),
        }],
    }
    orchestrator.task_graph.add_node(node)
    owner = TaskNode(
        node_id="N-OWNER", goal="backend component", role="coder",
        status="completed",
        owned_paths=["deliverables/app/backend/main.py"],
    )
    orchestrator.task_graph.add_node(owner)

    view = orchestrator._latest_verification_view(node)
    resolved = orchestrator._resolve_declared_root_causes(node, view, {})
    assert resolved
    assert any(
        paths == ["deliverables/app/backend/main.py"]
        for _owner_id, paths in resolved.values()
    )
    assert all(owner_id == "N-OWNER" for owner_id, _p in resolved.values())


@pytest.mark.parametrize("permanent", [False, True])
def test_locked_json_cache_never_invalidates_authoritative_checkpoint(tmp_path,monkeypatch,permanent):
    from pathlib import Path
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    from AstralNexus.core.runtime_state import read_authoritative_cursor
    coordinator=RunCoordinator(tmp_path,run_id="R")
    lease=coordinator.acquire("writer");assert lease is not None
    with Orchestrator(tmp_path,run_id="R",fence_token_provider=lambda:coordinator.fence_token(lease),ownership_probe=lambda:coordinator.owns(lease)) as engine:
        assert engine._persist_graph_checkpoint(stage="before_cache_lock")
        manager=engine.runtime_state_manager
        attempts=[];original=Path.replace
        def locked(source,target):
            if Path(target)==manager.path:
                attempts.append(source)
                if permanent or len(attempts)==1:
                    raise PermissionError("simulated Windows shared-reader lock")
            return original(source,target)
        monkeypatch.setattr(Path,"replace",locked)
        manager.state.phase="executing"
        assert engine._persist_graph_checkpoint(stage="locked_cache")
        cursor=read_authoritative_cursor(tmp_path,run_id="R")
        assert cursor["source"]=="commit_fence" and cursor["cursor"]["phase"]=="executing"
        assert len(attempts)==(4 if permanent else 2)
        assert bool(manager.last_cache_error)==permanent


def test_locked_legacy_cache_without_authority_remains_failure(tmp_path,monkeypatch):
    from pathlib import Path
    from AstralNexus.core.runtime_state import RuntimeStateManager
    manager=RuntimeStateManager(tmp_path,"legacy")
    def locked(*args,**kwargs):raise PermissionError("no writable cache")
    monkeypatch.setattr(Path,"replace",locked)
    with pytest.raises(PermissionError):manager.persist()
