"""Regression tests for cross-generation receipt inheritance + stable lineage
identity (doc 14, pause #2 hardening after the live V2→V3→V4 spiral).

Live evidence (r11, 2026-09-11 15:51-16:27 UTC): three workaround generations
were derived back-to-back AFTER Fix A/B/C were loaded.  Two seams failed under
live variability:

1. Routing blindness — the failed node's own evidence packet carried no
   structured ``next_actions`` (its worker answered in plain text), so the
   enforcement saw only the workaround contract's own vocabulary
   (``sqlite3.exe``/``pretest_*`` tokens owned by the lineage's superseded
   ancestors) and passed the next workaround generation.  The ORIGINAL
   root-cause receipt ("remove the SP-007 skip in init_db.py") lived in an
   ancestor's raw execution outcome, unreachable from the current node.
   → ``_worker_report_for_node`` now inherits the nearest ancestor's
   structured proposals (packet first, then re-parsing the ancestor
   execution outcome content with the fixed envelope extractor).

2. Identity drift — the three recorded lineage fingerprints all differed
   because the no-declaration fallback keyed on volatile verifier DETAILS
   text, which the LLM rephrases every generation.
   → the fallback identity is now generation-stable: contract goal + verdict
   + score bucket + dimension names (or the resolved root-cause map).

Plus a lineage depth cap (6 recorded generations) so a spiral that still
slips through exact-match dedup cannot continue indefinitely, and prompt
pass-through of the inherited receipts and already-tried dossier.
"""
from __future__ import annotations

import pytest


def _make_verdict(node_id: str, details: str, score: float = 0.5):
    from AstralNexus.core.verifier import (
        VerificationDimension,
        VerificationResult,
        VerificationVerdict,
    )
    return VerificationResult(
        node_id=node_id,
        verdict=VerificationVerdict.REPLAN_REQUIRED,
        dimensions=[VerificationDimension(
            name="semantic_goal", score=score, passed=False, details=details,
        )],
        overall_score=score,
    )


ANCESTOR_ACTIONS = [{
    "type": "spawn",
    "target": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
    "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
    "reason": "root cause: hardcoded 'if part_id == SP-007: continue'",
    "role": "coder",
}]


# ---------------------------------------------------------------------------
# Seam 1: inheritance of the lineage's original failure receipts.
# ---------------------------------------------------------------------------

def test_inherits_next_actions_from_ancestor_packet(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="inherit-packet") as orchestrator:
        graph = orchestrator.task_graph
        ancestor = TaskNode(
            node_id="N-004-R4-RECOVERY", goal="run e2e parts flow",
            role="tester", status="superseded",
        )
        ancestor.metadata["evidence_packet"] = {
            "summary": "SP-007 missing", "next_actions": list(ANCESTOR_ACTIONS),
        }
        graph.add_node(ancestor)
        gen = TaskNode(
            node_id="N-004-R4-RECOVERY-V5", goal="inject SP-007 pretest",
            role="coder", status="repair_pending",
        )
        gen.metadata["recovery_of"] = "N-004-R4-RECOVERY"
        gen.metadata["evidence_packet"] = {
            "summary": "tool limitation: cannot write binary sqlite3.exe",
            "next_actions": [],
        }
        graph.add_node(gen)

        report = orchestrator._worker_report_for_node(gen)
        assert report["next_actions"] == ANCESTOR_ACTIONS
        assert report["next_actions_inherited_from"] == "N-004-R4-RECOVERY"
        # The node's own summary is preserved — only the receipts inherit.
        assert "sqlite3.exe" in report["summary"]


def test_inherits_next_actions_by_reparsing_ancestor_execution_outcome(tmp_path):
    """Pre-fix generations persisted empty packets but their RAW execution
    outcome content still carries the fenced envelope — re-parse it."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.runtime.execution_store import ExecutionStore

    with Orchestrator(tmp_path, run_id="inherit-outcome") as orchestrator:
        graph = orchestrator.task_graph
        ancestor = TaskNode(
            node_id="N-004-R4-RECOVERY", goal="run e2e parts flow",
            role="tester", status="superseded",
        )
        ancestor.metadata["evidence_packet"] = {
            "summary": "blocked", "next_actions": [],  # lost to the old parser
        }
        store = ExecutionStore(orchestrator.run_dir)
        execution_id = store.begin("N-004-R4-RECOVERY", {"goal": "run e2e"})
        content = (
            "E2E 执行完成，但 SP-007 用例失败。分析如下。\n\n"
            "```json\n"
            '{"public_reasoning": "seed data incomplete",\n'
            ' "next_actions": [{"type": "spawn",\n'
            '   "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",\n'
            '   "reason": "root cause: hardcoded skip in init_db.py"}]}\n'
            "```\n"
        )
        store.finish(execution_id, "succeeded", {"content": content})
        ancestor.metadata["runtime_execution_id"] = execution_id
        graph.add_node(ancestor)

        gen = TaskNode(
            node_id="N-004-R4-RECOVERY-V5", goal="inject SP-007 pretest",
            role="coder", status="repair_pending",
        )
        gen.metadata["recovery_of"] = "N-004-R4-RECOVERY"
        graph.add_node(gen)

        report = orchestrator._worker_report_for_node(gen)
        assert report["next_actions_inherited_from"] == "N-004-R4-RECOVERY"
        goals = [str(a.get("goal") or "") for a in report["next_actions"]]
        assert any("init_db.py" in goal for goal in goals)


def test_enforcement_routes_on_inherited_root_cause(tmp_path, monkeypatch):
    """End-to-end: with an empty own-packet, the inherited ancestor receipt
    still activates routing enforcement — a workaround patch is rejected and
    the feedback loop derives the upstream repair node."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed with SP-007 skip\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="inherit-enforce") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        root = TaskNode(
            node_id="N-004", goal="run e2e parts flow", role="tester",
            status="superseded",
        )
        graph.add_node(root)
        ancestor = TaskNode(
            node_id="N-004-R4-RECOVERY", goal="run e2e parts flow",
            role="tester", status="superseded",
        )
        ancestor.metadata["recovery_of"] = "N-004"
        ancestor.metadata["evidence_packet"] = {
            "summary": "SP-007 missing", "next_actions": list(ANCESTOR_ACTIONS),
        }
        graph.add_node(ancestor)
        gen = TaskNode(
            node_id="N-004-R4-RECOVERY-V5", goal="inject SP-007 pretest",
            role="coder", status="repair_pending",
        )
        gen.metadata["recovery_of"] = "N-004-R4-RECOVERY"
        gen.metadata["evidence_packet"] = {"summary": "tool limits", "next_actions": []}
        graph.add_node(gen)

        attempts = {"n": 0}

        def fake_planner(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return TaskGraphPatch(
                    add_nodes=[{
                        "node_id": "N-004-R4-RECOVERY-V5-PRETEST",
                        "goal": "copy sqlite3.exe and inject SP-007 again",
                        "task_type": "testing",
                        "role": "tester",
                        "dependencies": ["N-002-R5"],
                    }],
                    reason_codes=["TEST_RECOVERY"],
                )
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-002-R6",
                    "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
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

        result = orchestrator._apply_governed_verification_recovery(
            gen, _make_verdict("N-004-R4-RECOVERY-V5", "pretest injection incomplete")
        )
        assert result.get("added") == ["N-002-R6"]
        routing = [
            event["payload"]["patch_addresses_root_cause"]
            for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.root_cause_routing"
        ]
        assert routing == [False, True]
        repair = graph.get_node("N-002-R6")
        assert repair is not None
        assert "deliverables/app/backend/init_db.py" in repair.owned_paths


# ---------------------------------------------------------------------------
# Seam 2: generation-stable identity + depth cap.
# ---------------------------------------------------------------------------

def test_details_churn_cannot_evade_lineage_block(tmp_path, monkeypatch):
    """The live evasion: rephrased verifier details produced three different
    fingerprints for the same unresolved failure.  Identity must not key on
    volatile details text."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="churn-block") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-004", goal="run the e2e parts flow", role="tester",
            status="superseded",
        ))
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

        # F2 (live b31): 新增节点必须声明图上还没有的证据路径才算进展；
        # 补一条跨代同名的 pretest 日志归属，让第 1 次提案确实是新工作，同时
        # 保持"同一方案形状"（本测试要验的是改述细节不得改变血缘身份，第 2 代
        # 仍由线系守卫拦下）。
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
            gen1,
            _make_verdict("N-004-R1", "the parts endpoint returned status 500 during the flow"),
        )
        assert first.get("added") == ["N-004-R1-PRETEST"]

        # Generation churn with a REPHRASED finding — same failure identity.
        old = graph.get_node("N-004-R1")
        old.status = "failed"
        gen2 = TaskNode(
            node_id="N-004-R2", goal=old.goal, role=old.role,
            status="repair_pending",
        )
        gen2.metadata["recovery_of"] = "N-004-R1"
        graph.add_node(gen2)

        second = orchestrator._apply_governed_verification_recovery(
            gen2,
            _make_verdict("N-004-R2", "creating a work order answered 500 from the backend"),
        )
        assert second["accepted"] is False and second["rejected"] is True
        assert "需要改变方案" in second["error"]
        assert gen2.metadata["_terminal_failure"] is True
        assert "N-004-R2-PRETEST" not in graph.nodes


def test_lineage_depth_cap_blocks_after_six_generations(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="depth-cap") as orchestrator:
        graph = orchestrator.task_graph
        root = TaskNode(
            node_id="N-004", goal="run e2e", role="tester", status="superseded",
        )
        graph.add_node(root)
        gen = TaskNode(
            node_id="N-004-V7", goal="run e2e", role="tester",
            status="repair_pending",
        )
        gen.metadata["recovery_of"] = "N-004"
        graph.add_node(gen)

        def _entries(count: int):
            return [
                {
                    "node_id": f"N-004-V{i}",
                    "fingerprint": f"fp-{i:04d}{'x' * 28}"[:32],
                    "recorded_at": "2026-09-11T16:00:00+00:00",
                    "added": [f"N-004-V{i + 1}"],
                    "updates": 1,
                }
                for i in range(count)
            ]

        root.metadata["_lineage_recovery_history"] = _entries(5)
        # Below the cap a novel fingerprint passes.
        orchestrator._check_lineage_recovery_repeat(gen, "novel-fingerprint")

        root.metadata["_lineage_recovery_history"] = _entries(6)
        with pytest.raises(ValueError) as excinfo:
            orchestrator._check_lineage_recovery_repeat(gen, "novel-fingerprint")
        assert "需要改变方案" in str(excinfo.value)
        assert "cap 6" in str(excinfo.value)
        blocked = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.lineage_depth_blocked"
        ]
        assert blocked and blocked[-1]["lineage_root"] == "N-004"


# ---------------------------------------------------------------------------
# Prompt pass-through.
# ---------------------------------------------------------------------------

def test_prompt_carries_inherited_receipt_and_lineage_dossier(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="prompt-inherit") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-004-V5", goal="inject SP-007 pretest", role="coder",
            status="repair_pending",
        ))
        captured: dict = {}

        class FakeClient:
            def chat(self, messages, **kwargs):
                captured["messages"] = messages
                return '{"add_nodes": [], "update_nodes": [], "reasoning": "noop"}'

        orchestrator.governor.llm_client = FakeClient()
        orchestrator.governor.llm_matching_enabled = True
        orchestrator.governor.plan_verification_recovery(
            node_id="N-004-V5",
            verdict="replan_required",
            overall_score=0.5,
            dimensions=[{"name": "semantic_goal", "score": 0.5, "passed": False,
                         "details": "pretest artifacts incomplete"}],
            worker_report={
                "summary": "tool limitation report",
                "execution_outcome": "succeeded",
                "next_actions": list(ANCESTOR_ACTIONS),
                "next_actions_inherited_from": "N-004-R4-RECOVERY",
                "lineage_root": "N-004",
                "lineage_already_tried": [
                    {"attempt_by": "N-004-V2", "added": ["N-004-V3"]},
                    {"attempt_by": "N-004-V3", "added": ["N-004-V4"]},
                ],
            },
        )

        user_prompt = captured["messages"][-1]["content"]
        assert "next_actions_inherited_from" in user_prompt
        assert "N-004-R4-RECOVERY" in user_prompt
        assert "lineage_already_tried" in user_prompt
        assert "N-004-V3" in user_prompt
        system_prompt = captured["messages"][0]["content"]
        assert "同等路由效力" in system_prompt
        assert "禁止再派生" in system_prompt


# ---------------------------------------------------------------------------
# Merge inheritance + class-aware enforcement (live r11 terminal-blocked
# post-mortem, doc 14 §18:38 UTC): the V5 round proved nearest-ancestor
# inheritance still masked the deep receipt, and ANY-semantics coverage let
# generation #11 slip through with init_db.py resolved but untouched.
# ---------------------------------------------------------------------------

def test_merge_inheritance_keeps_own_and_adds_deep_ancestor_receipts(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="merge-inherit") as orchestrator:
        graph = orchestrator.task_graph
        deep = TaskNode(
            node_id="N-004-R4-RECOVERY", goal="run e2e", role="tester",
            status="superseded",
        )
        deep.metadata["evidence_packet"] = {
            "summary": "SP-007 missing", "next_actions": list(ANCESTOR_ACTIONS),
        }
        graph.add_node(deep)
        mid = TaskNode(
            node_id="N-004-V5", goal="inject pretest", role="coder",
            status="superseded",
        )
        mid.metadata["recovery_of"] = "N-004-R4-RECOVERY"
        mid.metadata["evidence_packet"] = {
            "summary": "binary blocked",
            "next_actions": [{
                "type": "spawn",
                "target": "Resolve sqlite3.exe binary delivery constraint",
                "goal": "Resolve sqlite3.exe binary delivery constraint",
                "reason": "infrastructure limitation",
            }],
        }
        graph.add_node(mid)
        gen = TaskNode(
            node_id="N-004-V7", goal="inject pretest", role="coder",
            status="repair_pending",
        )
        gen.metadata["recovery_of"] = "N-004-V5"
        gen.metadata["evidence_packet"] = {
            "summary": "own report",
            "next_actions": [{
                "type": "spawn",
                "goal": "Republish the missing pretest inject log",
                "reason": "evidence gap",
            }],
        }
        graph.add_node(gen)

        report = orchestrator._worker_report_for_node(gen)
        goals = [str(a.get("goal") or "") for a in report["next_actions"]]
        # Own action first, then BOTH ancestors' receipts (nearest first):
        # the mid generation's workaround vocabulary must not mask the deep
        # init_db.py root-cause declaration.
        assert goals[0] == "Republish the missing pretest inject log"
        assert any("sqlite3.exe" in g for g in goals)
        assert any("init_db.py" in g for g in goals)
        assert report["next_actions_inherited_from"] == (
            "N-004-V5,N-004-R4-RECOVERY"
        )


def test_completed_owner_preferred_over_superseded_lineage_ancestor(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="owner-prefer") as orchestrator:
        graph = orchestrator.task_graph
        # Superseded ancestor registered FIRST (old code returned it).
        graph.add_node(TaskNode(
            node_id="N-002", goal="build backend", role="coder",
            status="superseded", owned_paths=["deliverables/app/backend/"],
        ))
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        gen = TaskNode(
            node_id="N-004-V7", goal="run e2e", role="tester",
            status="repair_pending",
        )
        graph.add_node(gen)

        report = {
            "summary": "",
            "next_actions": [{
                "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                "reason": "root cause: hardcoded skip",
            }],
        }
        resolved = orchestrator._resolve_declared_root_causes(gen, None, report)
        assert resolved, "init_db.py declaration must resolve"
        owners = {owner for owner, _paths in resolved.values()}
        assert owners == {"N-002-R5"}


def test_class_aware_enforcement_rejects_lineage_only_coverage(tmp_path):
    """Covering only superseded-ancestor paths while a COMPLETED-owned root
    cause is declared must be rejected (the V7-REPAIR round hole)."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed\n", encoding="utf-8")
    e2e = tmp_path / "deliverables" / "runs" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "pretest_inject.log").write_text("injected\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="class-enforce") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        ancestor = TaskNode(
            node_id="N-004-V6", goal="inject pretest", role="coder",
            status="superseded",
            owned_paths=["deliverables/runs/e2e/pretest_inject.log"],
        )
        graph.add_node(ancestor)
        gen = TaskNode(
            node_id="N-004-V7", goal="inject pretest", role="coder",
            status="repair_pending",
        )
        graph.add_node(gen)

        report = {
            "summary": "",
            "next_actions": [
                {
                    "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                    "reason": "root cause: hardcoded skip",
                },
                {
                    "goal": "Re-publish deliverables/runs/e2e/pretest_inject.log",
                    "reason": "missing evidence file",
                },
            ],
        }
        resolved = orchestrator._resolve_declared_root_causes(gen, None, report)
        assert "init_db.py" in resolved or any(
            "init_db.py" in token for token in resolved
        )

        workaround = TaskGraphPatch(add_nodes=[{
            "node_id": "N-004-V7-LOG",
            "goal": "re-publish the inject log",
            "role": "coder",
            "owned_paths": ["deliverables/runs/e2e/pretest_inject.log"],
        }])
        with pytest.raises(ValueError) as excinfo:
            orchestrator._enforce_root_cause_routing(gen, workaround, resolved)
        assert "COMPLETED-owned" in str(excinfo.value)

        upstream = TaskGraphPatch(add_nodes=[{
            "node_id": "N-002-R6",
            "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
            "role": "coder",
            "owned_paths": ["deliverables/app/backend/init_db.py"],
        }])
        assert orchestrator._enforce_root_cause_routing(
            gen, upstream, resolved
        ) is True


def test_depth_cap_bypassed_for_upstream_repair_patch(tmp_path, monkeypatch):
    """The cap must not block the upstream repair it itself recommends."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed with skip\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="cap-bypass") as orchestrator:
        graph = orchestrator.task_graph
        root = TaskNode(
            node_id="N-004", goal="run e2e", role="tester", status="superseded",
        )
        root.metadata["_lineage_recovery_history"] = [
            {
                "node_id": f"N-004-V{i}",
                "fingerprint": f"fp-{i:04d}{'x' * 28}"[:32],
                "recorded_at": "2026-09-11T18:00:00+00:00",
                "added": [f"N-004-V{i + 1}"],
                "updates": 1,
            }
            for i in range(6)
        ]
        graph.add_node(root)
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        gen = TaskNode(
            node_id="N-004-V7", goal="run e2e", role="tester",
            status="repair_pending",
        )
        gen.metadata["recovery_of"] = "N-004"
        gen.metadata["evidence_packet"] = {
            "summary": "SP-007 absent from seed output",
            "next_actions": [{
                "type": "spawn",
                "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                "reason": "root cause: hardcoded skip",
                "role": "coder",
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
                }],
                reason_codes=["UPSTREAM_REPAIR"],
            )

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery", fake_planner
        )

        result = orchestrator._apply_governed_verification_recovery(
            gen, _make_verdict("N-004-V7", "SP-007 still missing after seeding")
        )
        assert result.get("added") == ["N-002-R6"]
        assert not [
            event for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.lineage_depth_blocked"
        ]
        history = graph.get_node("N-004").metadata["_lineage_recovery_history"]
        assert len(history) == 7


def test_unregistered_artifact_refs_stripped_not_fatal(tmp_path, monkeypatch):
    """Ghost artifact references are dropped at the normalization boundary
    instead of striking the patch out three times (live V5 terminal)."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="artifact-strip") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed",
        ))
        gen = TaskNode(
            node_id="N-004-V5", goal="inject pretest", role="coder",
            status="repair_pending",
        )
        gen.metadata["evidence_packet"] = {"summary": "blocked", "next_actions": []}
        graph.add_node(gen)

        def fake_planner(**kwargs):
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-004-V5-R1",
                    "goal": "consume the pre-delivered sqlite3 binary",
                    "task_type": "testing",
                    "role": "tester",
                    "dependencies": ["N-002-R5"],
                    # F2: 输入产物不算"产出的证据"；补一条真实归属路径，
                    # 使该提案在"幽灵产物引用被剥离"之外仍是新工作。
                    "owned_paths": ["deliverables/app/tests/pretest_sqlite3.log"],
                    "input_artifacts": [
                        "artifact:sqlite3_exe_v3_45_2_win32_x86",
                        "plain-evidence-ref",
                    ],
                }],
                reason_codes=["INFRA_FIX"],
            )

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery", fake_planner
        )

        result = orchestrator._apply_governed_verification_recovery(
            gen, _make_verdict("N-004-V5", "binary missing")
        )
        assert result.get("added") == ["N-004-V5-R1"]
        derived = graph.get_node("N-004-V5-R1")
        # Both the ghost artifact ref and the bare unregistered ref are
        # stripped; Governor validation would reject either.
        assert derived.input_artifacts == []
        stripped = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.patch.unregistered_artifact_refs_stripped"
        ]
        assert stripped
        dropped_text = str(stripped[-1]["dropped"])
        assert "sqlite3_exe" in dropped_text and "plain-evidence-ref" in dropped_text


def test_redispatch_context_carries_previous_attempt_failure(tmp_path):
    """Seam 3 (live r11 resume #10): re-dispatch feedback blindness.

    N-002-R8-RECOVERY died three identical deaths — every re-armed worker
    received a byte-identical prompt with no trace of the previous
    attempt's no-progress termination, repeated the same curl-to-a-dead-
    port / read-old-logs loop, and burned the rework admission budget to
    terminal.  ``_run_agent_task`` must forward the node's last_failure
    receipt into the execution metadata so the prompt renderer can turn it
    into explicit anti-loop directives.
    """
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    class _CaptureAgent:
        role = "coder"
        selected_model = "test-model"
        worker_profile = {}
        input_contract = ""
        output_contract = ""
        context_policy = {}
        default_tools = ["file_read"]
        captured = None

        def execute(self, context):
            _CaptureAgent.captured = context
            raise RuntimeError("stop-after-capture")

    with Orchestrator(tmp_path, run_id="rework-feedback") as orchestrator:
        node = TaskNode(
            node_id="N-002-R8-RECOVERY",
            goal="Recover backend takeover and capture live service receipts",
            role="coder",
            status="ready",
        )
        node.metadata["last_failure"] = {
            "message": (
                "Real LLM role execution failed: runtime_no_progress: "
                "repeated action/evidence cycle after replanning feedback"
            ),
            "error_type": "LLMClientError",
            "execution_path": "partial_artifacts_retained",
            "attempt": 3,
        }
        orchestrator.task_graph.add_node(node)
        try:
            orchestrator._run_agent_task(_CaptureAgent(), node)
        except Exception:
            pass
        context = _CaptureAgent.captured
        assert context is not None
        previous = context.metadata.get("previous_attempt_failure")
        assert previous is not None
        assert "runtime_no_progress" in previous["message"]
        assert previous["error_type"] == "LLMClientError"
        assert previous["execution_path"] == "partial_artifacts_retained"

        # Fresh dispatch (no last_failure, no failed predecessor) must NOT
        # carry the block.
        fresh = TaskNode(node_id="N-FRESH", goal="implement", role="coder", status="ready")
        orchestrator.task_graph.add_node(fresh)
        _CaptureAgent.captured = None
        try:
            orchestrator._run_agent_task(_CaptureAgent(), fresh)
        except Exception:
            pass
        assert _CaptureAgent.captured is not None
        assert "previous_attempt_failure" not in _CaptureAgent.captured.metadata

        # Fresh takeover generation: no own receipt, but the terminal
        # predecessor's failure must cross the node-ID boundary (the live
        # trap repeated across generations of fresh nodes).  Note: part 1's
        # sentinel raise was caught by _run_agent_task, which legitimately
        # OVERWROTE the predecessor's last_failure with "stop-after-capture"
        # — inheritance must surface the predecessor's CURRENT receipt.
        takeover = TaskNode(
            node_id="N-002-R9",
            goal="Recover N-002-R8-RECOVERY and capture live service receipts",
            role="coder",
            status="ready",
        )
        takeover.metadata["recovery_of"] = "N-002-R8-RECOVERY"
        orchestrator.task_graph.add_node(takeover)
        _CaptureAgent.captured = None
        try:
            orchestrator._run_agent_task(_CaptureAgent(), takeover)
        except Exception:
            pass
        inherited = _CaptureAgent.captured.metadata.get("previous_attempt_failure")
        assert inherited is not None
        assert "stop-after-capture" in inherited["message"]
        assert inherited["source_node_id"] == "N-002-R8-RECOVERY"
