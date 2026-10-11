"""Fix P5#16 (r17-resume blocked 2026-10-02 14:48:58): ownership-routing deadlock.

Live anatomy: R-UPSTREAM-CONTRACT-REPAIR went repair_pending with its declared
root cause ``deliverables/docs/interface.md`` — a file owned by
R-CONTRACT-RT-REPAIR, which sits ``ready`` but is gated by its dependency on
the very node being recovered.  Every takeover proposal died one of two ways:
``_strip_foreign_takeover_claims`` removed the interface.md claim (foreign to
an ACTIVE owner) and root_cause_routing then rejected the patch for ignoring
the declared root cause; or two proposed nodes both claimed the file and the
ownership validator rejected the intra-patch overlap.  Fix 19's takeover
eligibility only covers blocked/repair_pending and zombie admitted/waiting
owners — ``ready`` never qualified, so the transfer pass could never move the
ownership.  Twelve churn events, breaker at repeat_count=3, run blocked.

The fix keeps a root-cause claim when the conflicting owner is a gated-ready
node (``ready`` with a direct dependency stuck in repair_pending/blocked):
the transfer pass then strips the duplicated paths from the gated owner via
explicit ``ownership_transfer_to`` updates, which the governor pre-merges
before add validation (doc 18 fix H).  Healthy ready/running owners keep full
protection.
"""
from __future__ import annotations

from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder

APP_PY = "deliverables/app/backend/app.py"
INTERFACE_MD = "deliverables/docs/interface.md"
INTERFACE_RT = "deliverables/docs/interface_runtime_verification.json"


def _stub(graph, tmp_path):
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    return orchestrator


def _deadlock_graph(graph):
    """r17 terminal shape: repair_pending root-cause node + gated ready owner."""
    graph.add_node(TaskNode(
        node_id="UPSTREAM", goal="repair the contract root cause in app.py",
        role="coder", status="repair_pending", owned_paths=[APP_PY],
    ))
    graph.add_node(TaskNode(
        node_id="RT", goal="runtime contract verification against interface.md",
        role="tester", status="ready", dependencies=["UPSTREAM"],
        owned_paths=[INTERFACE_MD, INTERFACE_RT],
    ))


def _successor(node_id="S-1"):
    return {
        "node_id": node_id,
        "goal": "adjudicate interface.md and repair the backend root cause",
        "role": "coder",
        "recovery_of": "UPSTREAM",
        "owned_paths": [APP_PY, INTERFACE_MD],
    }


def test_gated_ready_predicate_is_narrow(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="STUCK", goal="stuck repair", role="coder",
                            status="repair_pending"))
    graph.add_node(TaskNode(node_id="BLOCKED-DEP", goal="blocked dep", role="coder",
                            status="blocked"))
    graph.add_node(TaskNode(node_id="DONE", goal="finished work", role="coder",
                            status="completed"))
    graph.add_node(TaskNode(node_id="GATED1", goal="gated by stuck", role="tester",
                            status="ready", dependencies=["STUCK"],
                            owned_paths=["deliverables/docs/a.md"]))
    graph.add_node(TaskNode(node_id="GATED2", goal="gated by blocked", role="tester",
                            status="ready", dependencies=["DONE", "BLOCKED-DEP"],
                            owned_paths=["deliverables/docs/b.md"]))
    graph.add_node(TaskNode(node_id="HEALTHY", goal="healthy ready", role="tester",
                            status="ready", dependencies=["DONE"],
                            owned_paths=["deliverables/docs/c.md"]))
    graph.add_node(TaskNode(node_id="LEASED", goal="leased worker", role="coder",
                            status="running", dependencies=["STUCK"],
                            owned_paths=["deliverables/docs/d.md"]))
    graph.add_node(TaskNode(node_id="ADMITTED", goal="admitted node", role="tester",
                            status="admitted", dependencies=["STUCK"],
                            owned_paths=["deliverables/docs/e.md"]))
    stub = _stub(graph, tmp_path)
    assert stub._is_gated_ready_conflict_owner(graph.get_node("GATED1")) is True
    assert stub._is_gated_ready_conflict_owner(graph.get_node("GATED2")) is True
    assert stub._is_gated_ready_conflict_owner(graph.get_node("HEALTHY")) is False
    assert stub._is_gated_ready_conflict_owner(graph.get_node("LEASED")) is False
    assert stub._is_gated_ready_conflict_owner(graph.get_node("ADMITTED")) is False


def test_takeover_claim_on_gated_owner_survives_and_transfers(tmp_path):
    graph = TaskGraph()
    _deadlock_graph(graph)
    stub = _stub(graph, tmp_path)
    normalized = stub._normalize_recovery_ownership_transfer(
        TaskGraphPatch(add_nodes=[_successor()])
    )
    added = normalized.add_nodes[0]
    # The root-cause claim is no longer stripped from the takeover …
    assert INTERFACE_MD in added["owned_paths"]
    # … the gated owner loses exactly the duplicated path through an explicit
    # transfer update, keeps the rest of its contract, and is rewired onto
    # the replacement.
    transfer = next(item for item in normalized.update_nodes if item["node_id"] == "RT")
    assert transfer["owned_paths"] == [INTERFACE_RT]
    assert transfer["metadata"]["ownership_transfer_to"] == "S-1"
    assert transfer["dependencies"] == ["S-1"]


def test_full_apply_breaks_the_r17_deadlock(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="root", goal="deliver a working application",
                            role="planner", status="running"))
    _deadlock_graph(graph)
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    stub = _stub(graph, tmp_path)
    normalized = stub._normalize_recovery_ownership_transfer(
        TaskGraphPatch(add_nodes=[_successor()])
    )
    result = governor.apply_task_graph_patch(normalized)
    assert "S-1" in result["added"]
    assert graph.get_node("UPSTREAM").status == "superseded"
    successor = graph.get_node("S-1")
    assert INTERFACE_MD in successor.owned_paths
    rt = graph.get_node("RT")
    assert rt.status == "ready"
    assert rt.owned_paths == [INTERFACE_RT]
    assert rt.dependencies == ["S-1"]


def test_planner_pre_stripped_owner_update_gets_transfer_marker(tmp_path):
    """Fix P5#16b (live resume#2 attempt 2, 15:34:43): the rejection text
    teaches update_nodes usage, so the planner strips the duplicated path
    from the gated owner itself.  The transfer pass then finds nothing left
    to strip — but without the ``ownership_transfer_to`` marker the governor
    pre-merge (doc 18 fix H) ignores the planner's update and add validation
    still sees the double claim, rejecting an otherwise legal takeover."""
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="root", goal="deliver a working application",
                            role="planner", status="running"))
    _deadlock_graph(graph)
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    stub = _stub(graph, tmp_path)
    patch = TaskGraphPatch(
        add_nodes=[_successor()],
        update_nodes=[{"node_id": "RT", "owned_paths": [INTERFACE_RT]}],
    )
    normalized = stub._normalize_recovery_ownership_transfer(patch)
    rt_update = next(item for item in normalized.update_nodes if item["node_id"] == "RT")
    assert rt_update["metadata"]["ownership_transfer_to"] == "S-1"
    result = governor.apply_task_graph_patch(normalized)
    assert "S-1" in result["added"]
    assert graph.get_node("UPSTREAM").status == "superseded"
    assert graph.get_node("RT").owned_paths == [INTERFACE_RT]
    assert INTERFACE_MD in graph.get_node("S-1").owned_paths


def test_healthy_ready_owner_claim_is_still_stripped(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="UPSTREAM", goal="repair root cause",
                            role="coder", status="repair_pending", owned_paths=[APP_PY]))
    graph.add_node(TaskNode(node_id="DONE", goal="finished work", role="coder",
                            status="completed"))
    graph.add_node(TaskNode(node_id="HEALTHY", goal="healthy docs writer",
                            role="tester", status="ready", dependencies=["DONE"],
                            owned_paths=["deliverables/docs/user-guide.md"]))
    stub = _stub(graph, tmp_path)
    normalized = stub._normalize_recovery_ownership_transfer(TaskGraphPatch(add_nodes=[{
        "node_id": "S-2", "goal": "repair backend", "role": "coder",
        "recovery_of": "UPSTREAM",
        "owned_paths": [APP_PY, "deliverables/docs/user-guide.md"],
    }]))
    added = normalized.add_nodes[0]
    assert "deliverables/docs/user-guide.md" not in added["owned_paths"]
    assert APP_PY in added["owned_paths"]  # target contract stays inheritable
    assert not [u for u in normalized.update_nodes if u.get("node_id") == "HEALTHY"]


def test_running_owner_claim_is_still_stripped_even_with_stuck_dep(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="UPSTREAM", goal="repair root cause",
                            role="coder", status="repair_pending", owned_paths=[APP_PY]))
    graph.add_node(TaskNode(node_id="LEASED", goal="leased worker mid-execution",
                            role="coder", status="running", dependencies=["UPSTREAM"],
                            owned_paths=["deliverables/runs/e2e/log.txt"]))
    stub = _stub(graph, tmp_path)
    normalized = stub._normalize_recovery_ownership_transfer(TaskGraphPatch(add_nodes=[{
        "node_id": "S-3", "goal": "repair backend and take the e2e log",
        "role": "coder", "recovery_of": "UPSTREAM",
        "owned_paths": [APP_PY, "deliverables/runs/e2e/log.txt"],
    }]))
    added = normalized.add_nodes[0]
    assert "deliverables/runs/e2e/log.txt" not in added["owned_paths"]
    # LEASED is a consumer of the recovery target, so the pre-existing
    # consumer-rewire legitimately rebinds it onto the replacement — but no
    # ownership may be stripped from a running (leased) node.
    leased_updates = [u for u in normalized.update_nodes if u.get("node_id") == "LEASED"]
    assert all("owned_paths" not in u for u in leased_updates)
