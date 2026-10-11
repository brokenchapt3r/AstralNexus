"""Regression tests for the r11 root-cause routing chain (doc 14 §12:20/§12:50).

Live audit: six generations of test-side workaround nodes were derived while
every worker report — and the verifier's own findings — declared the root
cause inside ``init_db.py``, a file owned by a COMPLETED backend node.  Three
repaired seams are covered here:

1. Envelope extraction: worker JSON envelopes emitted after free-form prose
   (fenced block) are parsed instead of dropped, so ``next_actions`` can
   reach the evidence packet and Governor admission.
2. Routing-shape preservation: goal/role/dependencies/owned_paths survive
   AgentResponse → StructuredMessage → admission; admission accepts
   envelope-shaped proposals (``target``), grants write authority over an
   INACTIVE owner's paths, and deterministically rejects proposals that
   collide with ACTIVE owners.
3. Root-cause routing enforcement: the verification-recovery prompt carries
   the worker report, and patches that ignore a declared upstream root cause
   are rejected with actionable feedback through the existing
   rejection-feedback loop until a repair node owning the root-cause files
   is derived.
"""
from __future__ import annotations

from AstralNexus.agents.base import _extract_response_envelope


def test_satisfied_files_do_not_become_root_causes_of_unrelated_gap(tmp_path):
    from types import SimpleNamespace
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path, run_id='gap-scope') as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(node_id='health', role='tester', goal='health evidence',
                                status='completed', owned_paths=['deliverables/health_resp.json']))
        graph.add_node(TaskNode(node_id='backend', role='coder', goal='backend',
                                status='completed', owned_paths=['deliverables/main.py']))
        failed = TaskNode(node_id='test', role='tester', goal='verify API', status='failed')
        graph.add_node(failed)
        verdict = SimpleNamespace(dimensions=[{'details':
            'Satisfied: deliverables/health_resp.json proves health is working. '
            'Blocking gaps: no evidence of RBAC enforcement in deliverables/main.py'}])
        resolved = orchestrator._resolve_declared_root_causes(failed, verdict, {})
        assert any(owner == 'backend' for owner, paths in resolved.values())
        assert all(owner != 'health' for owner, paths in resolved.values())


def test_evidence_receipts_are_never_declared_upstream_root_causes(tmp_path):
    """Fix 17 (live r11 resume #27 blocked twice within four minutes without
    ever dispatching a worker): the miner declared ``health_resp.json`` — a
    /health response RECEIPT owned by a completed node — an upstream root
    cause. Routing enforcement then deterministically rejected every recovery
    patch that did not claim the receipt, the identical rejection repeated
    three times, and the run blocked. Receipts and run-evidence files are
    regenerated evidence outputs, not defect files: they must never drive the
    mandatory-ownership enforcement or the lineage fingerprint, while real
    code root causes named in the same verdict stay declared."""
    from types import SimpleNamespace
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path, run_id='receipt-scope') as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id='backend-done', role='coder', goal='backend', status='completed',
            owned_paths=['deliverables/health_resp.json',
                         'deliverables/runs/e2e/create_ok.json',
                         'deliverables/main.py'],
        ))
        failed = TaskNode(node_id='svc-launch', role='tester',
                          goal='repair service launch', status='failed')
        graph.add_node(failed)
        verdict = SimpleNamespace(dimensions=[{'details':
            'Blocking gaps: root cause is the missing fresh /health receipt in '
            'health_resp.json and stale deliverables/runs/e2e/create_ok.json; '
            'also a hardcoded skip in deliverables/main.py'}])
        resolved = orchestrator._resolve_declared_root_causes(failed, verdict, {})
        declared_paths = {p for _owner, paths in resolved.values() for p in paths}
        assert not any('health_resp.json' in p for p in declared_paths)
        assert not any('runs/' in p for p in declared_paths)
        assert any(owner == 'backend-done' and 'deliverables/main.py' in paths
                   for owner, paths in resolved.values())


# ---------------------------------------------------------------------------
# Seam 1: envelope extraction from real-world worker output shapes.
# ---------------------------------------------------------------------------

def test_envelope_extracted_from_prose_plus_fenced_block():
    """The live r11 shape: prose report, then a fenced json envelope."""
    content = (
        "E2E 执行完成，但 SP-007 用例失败。分析如下。\n\n"
        "```json\n"
        '{"public_reasoning": "seed data is incomplete",\n'
        ' "next_actions": [{"type": "spawn",\n'
        '   "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",\n'
        '   "role": "coder", "dependencies": ["N-002-R5"],\n'
        '   "owned_paths": ["deliverables/app/backend/init_db.py"],\n'
        '   "reason": "root cause: hardcoded skip in init_db.py"}]}\n'
        "```\n"
    )
    envelope = _extract_response_envelope(content)
    assert envelope is not None
    assert envelope["public_reasoning"] == "seed data is incomplete"
    action = envelope["next_actions"][0]
    assert action["role"] == "coder"
    assert action["owned_paths"] == ["deliverables/app/backend/init_db.py"]


def test_strict_whole_content_shapes_still_parse():
    """Legacy shapes (whole-content JSON, whole-content fence) unchanged."""
    strict = _extract_response_envelope('{"next_actions": [], "content": "ok"}')
    assert strict is not None and strict["content"] == "ok"
    fenced = _extract_response_envelope('```json\n{"next_actions": []}\n```')
    assert fenced is not None and fenced["next_actions"] == []
    # Whole-content JSON without envelope keys stays an envelope (legacy).
    legacy = _extract_response_envelope('{"files": ["a.py"]}')
    assert legacy == {"files": ["a.py"]}


def test_plain_text_and_non_envelope_fences_are_not_misread():
    assert _extract_response_envelope("全部用例通过，报告已写入。") is None
    assert _extract_response_envelope("") is None
    tool_output = 'Done.\n```json\n{"files": ["a.py"]}\n```'
    assert _extract_response_envelope(tool_output) is None


def test_line_initial_embedded_envelope_without_fence():
    content = (
        'Summary text\n{"next_actions": [], "public_reasoning": "ok"} trailing'
    )
    envelope = _extract_response_envelope(content)
    assert envelope is not None and envelope["public_reasoning"] == "ok"


# ---------------------------------------------------------------------------
# Seam 2: routing fields survive the message bridge and admission.
# ---------------------------------------------------------------------------

def test_bridge_preserves_routing_fields():
    from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
    from AstralNexus.core.bridge import response_to_message
    from AstralNexus.core.message_protocol import MessageProtocol

    response = AgentResponse(
        outcome=ExecutionOutcome.SUCCEEDED,
        content="e2e finished with one blocked case",
        next_actions=[{
            "type": "spawn",
            "target": "Remove SP-007 skip",
            "goal": "Remove SP-007 skip",
            "role": "coder",
            "dependencies": ["N-002-R5"],
            "owned_paths": ["deliverables/app/backend/init_db.py"],
            "reason": "root cause: hardcoded skip",
        }],
    )
    message = response_to_message(
        response, node_id="N-004", role="tester", goal="run e2e"
    )
    valid, error = MessageProtocol().validate_message(message)
    assert valid, error
    action = message.next_actions[0]
    assert action["type"] == "spawn"
    assert action["goal"] == "Remove SP-007 skip"
    assert action["role"] == "coder"
    assert action["dependencies"] == ["N-002-R5"]
    assert action["owned_paths"] == ["deliverables/app/backend/init_db.py"]


def test_admission_derives_upstream_repair_with_write_authority(tmp_path):
    """Envelope-shaped proposals become actionable repair nodes.

    Before the fix every proposal was rejected as ``missing_goal`` (the
    envelope carries ``target``), and admitted nodes never received
    ``owned_paths`` — so an upstream repair node could not legally touch the
    root-cause files it was created to fix.
    """
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="admission-upstream") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        graph.add_node(TaskNode(
            node_id="N-004-R2", goal="run e2e parts flow", role="tester",
            status="result_submitted",
        ))
        graph.add_node(TaskNode(
            node_id="N-005-R3", goal="inject missing parts", role="tester",
            status="ready", owned_paths=["deliverables/app/backend/db.sqlite"],
        ))

        rich = {
            "information_gain": 0.9,
            "risk_reduction": 0.8,
            "parallel_benefit": 0.7,
            "estimated_cost": 100,
        }
        applied = orchestrator.governor.admit_worker_subtasks(
            parent_node_id="N-004-R2",
            proposals=[
                {  # upstream repair against a COMPLETED owner: legal
                    "type": "spawn",
                    "target": "Remove the SP-007 skip in init_db.py",
                    "reason": "root cause: hardcoded skip",
                    "role": "coder",
                    "dependencies": ["N-002-R5", "N-002-R5-fix"],
                    "owned_paths": ["deliverables/app/backend/init_db.py"],
                    **rich,
                },
                {  # collides with an ACTIVE owner: deterministic rejection
                    "type": "spawn",
                    "goal": "overwrite the live sqlite database",
                    "reason": "workaround",
                    "owned_paths": ["deliverables/app/backend/db.sqlite"],
                    **rich,
                },
                {"type": "spawn", "reason": "no goal at all"},
            ],
        )

        assert len(applied["added"]) == 1
        repair = graph.get_node(applied["added"][0])
        assert repair is not None
        assert repair.goal == "Remove the SP-007 skip in init_db.py"
        assert repair.role == "coder"
        # The unknown dependency id is filtered; the known one is kept.
        assert repair.dependencies == ["N-002-R5"]
        assert [p.replace("\\", "/") for p in repair.owned_paths] == [
            "deliverables/app/backend/init_db.py"
        ]
        assert repair.metadata["source"] == "worker_discovery"
        assert repair.metadata["proposed_by"] == "N-004-R2"
        # The completed producer stays immutable.
        assert graph.get_node("N-002-R5").status == "completed"

        rejected = {item["reason"] for item in applied["rejected_proposals"]}
        assert "ownership_conflict_active:N-005-R3" in rejected
        assert "missing_goal" in rejected


# ---------------------------------------------------------------------------
# Seam 3: recovery routing reads the worker report and enforces it.
# ---------------------------------------------------------------------------

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


def test_recovery_prompt_carries_worker_report_and_routing_rule(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="prompt-report") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-004-R2", goal="run e2e parts flow", role="tester",
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
            node_id="N-004-R2",
            verdict="replan_required",
            overall_score=0.5,
            dimensions=[{
                "name": "semantic_goal", "score": 0.5, "passed": False,
                "details": "SP-007 absent due to hardcoded skip in init_db.py",
            }],
            worker_report={
                "summary": "blocked by seed data",
                "execution_outcome": "succeeded",
                "next_actions": [{
                    "type": "spawn",
                    "target": "fix init_db.py",
                    "reason": "root cause: hardcoded skip",
                }],
            },
        )

        user_prompt = captured["messages"][-1]["content"]
        assert "failed_node_report" in user_prompt
        assert "fix init_db.py" in user_prompt
        assert "blocked by seed data" in user_prompt
        system_prompt = captured["messages"][0]["content"]
        assert "根因" in system_prompt
        assert "owned_paths" in system_prompt


def test_workaround_patch_rejected_until_upstream_repair_derived(tmp_path, monkeypatch):
    """Full recovery loop: rejection feedback forces the routing change."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="routing-enforce") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        tester = TaskNode(
            node_id="N-004-R4", goal="run e2e parts flow", role="tester",
            status="repair_pending",
        )
        tester.metadata["evidence_packet"] = {
            "summary": "SP-007 missing; e2e parts flow cannot pass",
            "execution_outcome": "succeeded",
            "next_actions": [{
                "type": "spawn",
                "target": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                "goal": "Remove the SP-007 skip in deliverables/app/backend/init_db.py",
                "reason": "root cause: hardcoded 'if part_id == SP-007: continue'",
                "role": "coder",
            }],
        }
        graph.add_node(tester)

        attempts = {"n": 0}

        def fake_planner(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                # Yet another test-side workaround, ignoring the report.
                return TaskGraphPatch(
                    add_nodes=[{
                        "node_id": "N-004-R4-PRETEST",
                        "goal": "inject SP-007 row before the e2e run",
                        "task_type": "testing",
                        "role": "tester",
                        "dependencies": ["N-002-R5"],
                    }],
                    reason_codes=["TEST_RECOVERY"],
                )
            # Attempt 2 must see the routing instruction in the rejection.
            assert "root cause" in kwargs.get("rejection_reason", "")
            assert kwargs["worker_report"]["next_actions"], "report must travel"
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-002-R6",
                    "goal": "remove the SP-007 skip from the seed script",
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
        verdict = _make_verdict(
            "N-004-R4",
            "SP-007 absent from the database due to hardcoded skip in init_db.py",
        )

        applied = orchestrator._apply_governed_verification_recovery(tester, verdict)

        assert attempts["n"] == 2
        assert applied.get("added") == ["N-002-R6"]
        repair = graph.get_node("N-002-R6")
        assert repair is not None and repair.role == "coder"
        assert "deliverables/app/backend/init_db.py" in repair.owned_paths
        # Completed producers stay immutable; the tester is untouched.
        assert graph.get_node("N-002-R5").status == "completed"
        assert tester.status == "repair_pending"

        events = [
            event for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.verification_recovery.rejected"
        ]
        assert len(events) == 1
        assert (
            "ignores the declared upstream root cause"
            in events[0]["payload"]["error"]
        )
        routing = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.root_cause_routing"
        ]
        assert [item["patch_addresses_root_cause"] for item in routing] == [False, True]


def test_bare_filename_root_cause_resolves_through_deliverables(tmp_path, monkeypatch):
    """Weak-signal shape observed at resume: empty next_actions, verifier
    details name a bare file owned by a completed node.

    Fix S1 (b32_path_identity): the file is deliberately **absent from disk**.
    The bare name ``init_db.py`` must resolve through the graph's DECLARED
    paths (``N-002-R5.owned_paths == ["deliverables/app/backend/"]``), never
    through a filesystem same-name search — the run tree does not even contain
    a ``deliverables/`` directory.  That makes the assertion stronger than the
    pre-fix version, which needed the file to exist before it could be found.
    """
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    assert not backend.exists()

    with Orchestrator(tmp_path, run_id="bare-name-resolve") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        tester = TaskNode(
            node_id="N-004-V2", goal="verify parts flow", role="tester",
            status="repair_pending",
        )
        # Persisted evidence packet from the broken-extraction era: empty.
        tester.metadata["evidence_packet"] = {
            "summary": "verification blocked", "next_actions": [],
        }
        graph.add_node(tester)

        attempts = {"n": 0}

        def fake_planner(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return TaskGraphPatch(
                    update_nodes=[{"node_id": "N-004-V2", "status": "ready"}],
                    reason_codes=["REARM"],
                )
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-002-R6",
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
        verdict = _make_verdict(
            "N-004-V2",
            "SP-007 is absent from the database due to hardcoded skip logic "
            "in init_db.py, causing the parts flow test to fail",
        )

        applied = orchestrator._apply_governed_verification_recovery(tester, verdict)

        assert attempts["n"] == 2
        assert applied.get("added") == ["N-002-R6"]
        routing = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.root_cause_routing"
        ]
        assert routing[0]["root_cause_files"] == {"init_db.py": "N-002-R5"}
        assert [item["patch_addresses_root_cause"] for item in routing] == [False, True]


def test_self_owned_backend_files_do_not_demand_upstream_repair(tmp_path, monkeypatch):
    """Live r11 resume #8 regression (N-002-R7 terminal deadlock).

    The failed node owned the whole backend tree; its verified init_db.py
    repair passed all five acceptance criteria, yet one evidence dimension
    (start.bat launch causality, boundary-test coverage belonging to the
    N-005 track) forced replan_required.  Verifier details mentioning the
    backend files — in PRAISE and gap context — were misread as upstream
    root-cause declarations resolved to completed N-002-R5, so every
    (correct) verification-node patch was rejected and three strikes made
    the node terminal while the real repair sat on disk.  Files covered by
    the failed node's OWN contract route through its rework/takeover
    lineage, never through an upstream-repair demand.
    """
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "init_db.py").write_text("# seed with SP-007\n", encoding="utf-8")
    (backend / "main.py").write_text("# service\n", encoding="utf-8")
    (backend / "start.bat").write_text("python -m uvicorn main:app\n", encoding="utf-8")
    (backend / "process_registry.json").write_text("{}\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="self-owned-exempt") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        backend_node = TaskNode(
            node_id="N-002-R7", goal="implement the backend component",
            role="coder", status="repair_pending",
            owned_paths=[
                "deliverables/app/backend/",
                "deliverables/app/backend/init_db.py",
                "deliverables/app/backend/main.py",
                "deliverables/app/backend/start.bat",
            ],
        )
        backend_node.metadata["evidence_packet"] = {
            "summary": "all acceptance criteria satisfied; start.bat launch causality gap",
            "next_actions": [],
        }
        graph.add_node(backend_node)

        calls = {"n": 0}

        def fake_planner(**kwargs):
            calls["n"] += 1
            # The semantically correct recovery: add missing verification.
            return TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-004-R5",
                    "goal": "run the e2e parts flow against the repaired backend "
                            "and capture start.bat launch causality",
                    "task_type": "testing",
                    "role": "tester",
                    "dependencies": ["N-002-R7"],
                    "owned_paths": ["deliverables/runs/e2e/"],
                }],
                reason_codes=["VERIFICATION_GAP"],
            )

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery", fake_planner
        )
        verdict = _make_verdict(
            "N-002-R7",
            "All acceptance criteria are satisfied: init_db.py contains the "
            "INSERT OR REPLACE for SP-007 — confirmed; /health returned 200. "
            "Blocking gaps: no evidence confirms start.bat launched the port "
            "5000 instance; zero-stock rejection coverage missing for "
            "main.py; process_registry.json ambiguous.",
        )

        applied = orchestrator._apply_governed_verification_recovery(
            backend_node, verdict
        )

        # First attempt applies: enforcement is disarmed for self-owned files.
        assert calls["n"] == 1
        assert applied.get("added") == ["N-004-R5"]
        assert graph.get_node("N-004-R5") is not None
        routing = [
            event for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "governor.verification_recovery.root_cause_routing"
        ]
        assert routing == []  # disarmed: no resolved upstream root causes
        rejected = [
            event for event in orchestrator.trace_recorder.events
            if event["event_type"] == "governor.verification_recovery.rejected"
        ]
        assert rejected == []


def test_recovery_prompt_forbids_reissuing_disproved_literal_premise(tmp_path):
    """Fix 15b: the recovery planner must treat verified worker counter-evidence
    (e.g. 'field status_code does not exist; the file has code=403') as
    authoritative and rebuild the goal from actual file facts instead of
    re-deriving read-only nodes that re-check a contradicted premise."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="counter-evidence") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-009-R2", goal="prove receipt shows status_code null",
            role="tester", status="repair_pending",
        ))
        captured: dict = {}

        class FakeClient:
            def chat(self, messages, **kwargs):
                captured["messages"] = messages
                return '{"add_nodes": [], "update_nodes": [], "reasoning": "noop"}'

        orchestrator.governor.llm_client = FakeClient()
        orchestrator.governor.llm_matching_enabled = True
        orchestrator.governor.plan_verification_recovery(
            node_id="N-009-R2",
            verdict="replan_required",
            overall_score=0.5,
            dimensions=[{
                "name": "semantic_goal", "score": 0.5, "passed": False,
                "details": "status_code is null in receipt",
            }],
            worker_report={
                "summary": "field status_code does not exist; file has code=403",
                "execution_outcome": "succeeded",
                "next_actions": [],
            },
        )
        system_prompt = captured["messages"][0]["content"]
        assert "反证处理" in system_prompt
        assert "字段缺失不等于字段为 null" in system_prompt
        assert "禁止" in system_prompt
        # worker counter-evidence still rides in the user payload
        user_prompt = captured["messages"][-1]["content"]
        assert "code=403" in user_prompt
