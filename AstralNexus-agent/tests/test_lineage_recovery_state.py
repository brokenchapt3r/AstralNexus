"""Regression tests for cross-generation lineage recovery state (doc 14 §12:50).

User directive step 3: recovery state must live at the LINEAGE level — the
original task lineage, the root cause, the relevant file versions, and the
failed-check identity.  Minting a new node ID must not clear the history, and
when the same root cause + same file state + same repair approach reappears,
re-derivation is blocked with an explicit "change of approach" instruction.

The r11 test track re-ran the identical workaround seven times under fresh
generation IDs because every per-node attempt counter reset on supersede.
"""
from __future__ import annotations


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


def _churn(graph, old_id: str, new_id: str) -> None:
    """Fail the old generation and admit its successor (recovery_of chain)."""
    from AstralNexus.core.task_graph import TaskNode

    old = graph.get_node(old_id)
    if old.status == "repair_pending":
        graph.update_node_status(old_id, "failed")
    else:
        old.status = "failed"
    successor = TaskNode(
        node_id=new_id, goal=old.goal, role=old.role, status="repair_pending",
    )
    successor.metadata["recovery_of"] = old_id
    graph.add_node(successor)


def test_same_plan_is_blocked_across_generation_ids(tmp_path, monkeypatch):
    """A fresh node ID does not launder an identical repeat attempt."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="lineage-block") as orchestrator:
        graph = orchestrator.task_graph
        root = TaskNode(
            node_id="N-004", goal="run the e2e parts flow", role="tester",
            status="superseded",
        )
        graph.add_node(root)
        gen1 = TaskNode(
            node_id="N-004-R1", goal="run the e2e parts flow", role="tester",
            status="repair_pending",
        )
        gen1.metadata["recovery_of"] = "N-004"
        graph.add_node(gen1)
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))

        # Details carry no path and no root-cause wording: routing
        # enforcement stays inert and the lineage guard is the only dedup.
        details = "the parts endpoint returned status 500 during the flow"

        # F2 (live b31): 恢复提案里的新增节点必须声明图上还没有的交付物/证据
        # 路径才算进展——"新增了节点"本身不算。本夹具原先只给节点 id/goal，
        # 于是第 1 次提案就不构成推进；这里补一条真实的 pretest 日志归属（跨代
        # 同名，才保持"同一方案形状"），让它确实是新证据，测试本意（第 2 代由
        # 线系守卫拦下）不变。
        def fake_planner(**kwargs):
            parent = str(kwargs["node_id"])
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": f"{parent}-PRETEST",
                    "goal": "inject SP-007 row before the e2e run",
                    "task_type": "testing",
                    "role": "tester",
                    "dependencies": ["N-002-R5"],
                    "owned_paths": ["deliverables/app/tests/pretest_inject.log"],
                }],
                reason_codes=["TEST_RECOVERY"],
            )

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery", fake_planner
        )

        first = orchestrator._apply_governed_verification_recovery(
            gen1, _make_verdict("N-004-R1", details)
        )
        assert first.get("added") == ["N-004-R1-PRETEST"]
        history = graph.get_node("N-004").metadata["_lineage_recovery_history"]
        assert len(history) == 1
        assert history[0]["node_id"] == "N-004-R1"

        # Generation churn: same lineage, new ID, IDENTICAL plan shape.
        _churn(graph, "N-004-R1", "N-004-R2")
        gen2 = graph.get_node("N-004-R2")
        second = orchestrator._apply_governed_verification_recovery(
            gen2, _make_verdict("N-004-R2", details)
        )
        assert second["accepted"] is False and second["rejected"] is True
        assert "需要改变方案" in second["error"]
        assert "change of approach required" in second["error"]
        assert gen2.metadata["_terminal_failure"] is True
        assert gen2.status == "failed"
        # History stays on the root; the blocked attempts do not append.
        history = graph.get_node("N-004").metadata["_lineage_recovery_history"]
        assert len(history) == 1
        blocked = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.lineage_repeat_blocked"
        ]
        assert blocked and blocked[-1]["lineage_root"] == "N-004"
        assert blocked[-1]["previous_nodes"] == ["N-004-R1"]
        assert blocked[-1]["repeat_count"] == 2
        # No same-shaped node was derived for the blocked generation.
        assert "N-004-R2-PRETEST" not in graph.nodes


def test_changed_file_state_unlocks_same_plan_then_blocks_again(tmp_path, monkeypatch):
    """File-state is part of the identity: changed upstream file legitimately
    unlocks a retry; unchanged state blocks the identical plan again."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    seed = backend / "init_db.py"
    seed.write_text("# seed v1\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="lineage-unlock") as orchestrator:
        graph = orchestrator.task_graph
        root = TaskNode(
            node_id="N-004", goal="run the e2e parts flow", role="tester",
            status="superseded",
        )
        graph.add_node(root)
        gen1 = TaskNode(
            node_id="N-004-R1", goal="run the e2e parts flow", role="tester",
            status="repair_pending",
        )
        gen1.metadata["recovery_of"] = "N-004"
        gen1.metadata["evidence_packet"] = {
            "summary": "SP-007 missing",
            "next_actions": [{
                "type": "spawn",
                "target": "Repair the seed script deliverables/app/backend/init_db.py",
                "goal": "Repair the seed script deliverables/app/backend/init_db.py",
                "reason": "seed data incomplete",
                "role": "coder",
            }],
        }
        graph.add_node(gen1)
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))

        counter = {"n": 0}

        def fake_planner(**kwargs):
            counter["n"] += 1
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": f"N-002-R{5 + counter['n']}",
                    "goal": "repair the seed script",
                    "task_type": "implementation",
                    "role": "coder",
                    "dependencies": ["N-002-R5"],
                    "owned_paths": ["deliverables/app/backend/init_db.py"],
                }],
                reason_codes=["UPSTREAM_REPAIR"],
            )

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery", fake_planner
        )
        verdict_details = "parts flow could not exercise SP-007"

        first = orchestrator._apply_governed_verification_recovery(
            gen1, _make_verdict("N-004-R1", verdict_details)
        )
        assert first.get("added") == ["N-002-R6"]
        history = graph.get_node("N-004").metadata["_lineage_recovery_history"]
        assert len(history) == 1
        fingerprint_v1 = history[0]["fingerprint"]

        # The repair node itself failed and the file was since MODIFIED:
        # same plan, different file state → legitimately unlocked.
        graph.get_node("N-002-R6").status = "failed"
        _churn(graph, "N-004-R1", "N-004-R2")
        seed.write_text("# seed v2 — SP-007 restored\n", encoding="utf-8")
        gen2 = graph.get_node("N-004-R2")
        gen2.metadata["evidence_packet"] = dict(gen1.metadata["evidence_packet"])

        second = orchestrator._apply_governed_verification_recovery(
            gen2, _make_verdict("N-004-R2", verdict_details)
        )
        assert second.get("added") == ["N-002-R7"]
        history = graph.get_node("N-004").metadata["_lineage_recovery_history"]
        assert len(history) == 2
        assert history[1]["fingerprint"] != fingerprint_v1

        # File state unchanged now: the identical plan is blocked again.
        graph.get_node("N-002-R7").status = "failed"
        _churn(graph, "N-004-R2", "N-004-R3")
        gen3 = graph.get_node("N-004-R3")
        gen3.metadata["evidence_packet"] = dict(gen1.metadata["evidence_packet"])

        third = orchestrator._apply_governed_verification_recovery(
            gen3, _make_verdict("N-004-R3", verdict_details)
        )
        assert third["accepted"] is False
        assert "需要改变方案" in third["error"]
        assert gen3.metadata["_terminal_failure"] is True
        history = graph.get_node("N-004").metadata["_lineage_recovery_history"]
        assert len(history) == 2
        assert f"N-002-R{5 + counter['n']}" not in graph.nodes
