"""F2 / F3 / F12 offline regressions: evidence-backed progress vs no-progress churn.

Live b31 (autonomous full-stack acceptance, goal-5e5e9199) produced three
framework defects that this file locks down:

- **F2** — ``_apply_governed_verification_recovery`` applied verification
  recovery proposals that only re-admitted the same node on an *unchanged
  premise*.  The old rule needed three consecutive no-op proposals before it
  refused, so the live run paid N-102 ``exec-000006`` three times and R-601
  twice (≈6 minutes per re-run, zero product increment).
- **F3** — a real failed node never reached repair directly: ``recovery.attempted``
  → malformed proposal (``governed_fault_invalid_proposal``) → 33 iterations and
  ~15 minutes of idle blocked consumers before the stalled-replan path fired.
- **F12** — the run-level churn budget was a monotone total, so a *correct*
  structural repair counted as "no progress": the last patch of the run had just
  unblocked three never-executed nodes, became churn event 20/20, and the run
  stood down with zero delivery.

What is covered:

(a) an unchanged premise is refused on the **first** proposal (no 3-strike wait);
(b) a premise that really moved still passes the same shape — the guard must not
    block a necessary re-verification that has grounds (m00547 invariant);
(c) a structural repair is charged to the no-progress budget unless a node
    really reached ``completed`` since the last credit — b33 case1 fix10
    measured 30 recovery events against a budget of 20 with the breaker never
    arming, because "the graph changed" was accepted as progress; a ready
    frontier and a supersede/takeover replacement are not progress, a completed
    node is;
(d) the budget constant is unchanged (20) and the no-progress counter is a
    separate field from the monotone total;
(e) ``--user-resume`` clears the no-progress counter too and audits
    ``prior_no_progress``;
(f) a malformed recovery proposal escalates exactly one bounded replan (F3);
(g) twenty genuinely unproductive events still trip the breaker at exactly 20
    with the original closed failure class, while (h) churn at the same depth
    that carried real structural repairs does not stand the run down;
(i) a pre-F12 ``runtime_state.json`` (no new key) still loads and defaults the
    tally to 0, with ``record_churn`` semantics unchanged;
(j) a supersede/takeover chain — graph mutations with no completed node — is
    charged in full, so the breaker arms on the shape that let fix10 run 30
    recoveries without one (b33 case1 fix10).

No budget, threshold or anchoring requirement is relaxed anywhere in here: the
only source of "progress" credit is a node reaching ``completed``.  That count is
computed from the graph by the run itself; the caller's ``productive`` claim is a
precondition, never the proof.
"""
from __future__ import annotations


def _verdict(node_id: str, details: str):
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


def _readmit_planner_for(node_id: str):
    """F2 shape: re-admit the same node and declare no new evidence."""

    def planner(**_kwargs):
        from AstralNexus.core.governor import TaskGraphPatch

        return TaskGraphPatch(
            update_nodes=[{"node_id": node_id, "status": "ready"}],
            reason_codes=["RETRY_VERIFICATION"],
        )

    return planner


def _refusals(orchestrator):
    return [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "governor.verification_recovery.no_new_evidence"
    ]


def _applied(orchestrator):
    return [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "orchestrator.verification_recovery.applied"
    ]


def test_unchanged_premise_is_refused_on_the_first_proposal(tmp_path, monkeypatch):
    """(a) 前提未变 → 第一次提案即拒（F2）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="f2-first-refusal") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        gen = TaskNode(
            node_id="N-004-R1", goal="run the e2e parts flow", role="tester",
            status="repair_pending",
        )
        graph.add_node(gen)
        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery",
            _readmit_planner_for("N-004-R1"),
        )

        result = orchestrator._apply_governed_verification_recovery(
            gen, _verdict("N-004-R1", "the parts endpoint returned status 500"),
        )

        assert result.get("rejected") is True, result
        assert "change of approach required" in result["error"]
        assert "no new work or evidence" in result["error"]
        refusals = _refusals(orchestrator)
        assert refusals, "the F2 refusal must be recorded"
        assert refusals[0]["occurrence"] == 1
        assert refusals[0]["premise_changed"] is False
        assert refusals[0]["add_node_count"] == 0
        # Nothing was applied: an unchanged premise is never re-run.
        assert _applied(orchestrator) == []


def test_changed_premise_still_passes_the_same_shape(tmp_path, monkeypatch):
    """(b) 前提文件真变 → 同一形状仍放行（守卫不得阻止有依据的必要复验）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.runtime.verification_identity import premise_versions

    premise = "deliverables/app/backend/init_db.py"
    seed = tmp_path / "deliverables" / "app" / "backend" / "init_db.py"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("# seed v1\n", encoding="utf-8")

    with Orchestrator(tmp_path, run_id="f2-premise-change") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="N-002-R5", goal="seed the parts database", role="coder",
            status="completed", owned_paths=["deliverables/app/backend/"],
        ))
        gen = TaskNode(
            node_id="N-004-R1", goal="run the e2e parts flow", role="tester",
            status="repair_pending",
        )
        # Receipt of the previous verdict: the premise versions AS OF that verdict.
        # ``observations`` is the verifier's own list shape (see
        # ``_persist_verification_receipt`` in core/orchestrator.py:7478).
        gen.metadata["_verification_receipt"] = {"observations": [{
            "declared_premises": [premise],
            "premise_versions": premise_versions(tmp_path, [premise]),
        }]}
        graph.add_node(gen)
        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery",
            _readmit_planner_for("N-004-R1"),
        )

        # The file the verdict rested on really moved: the premise is refreshed.
        seed.write_text("# seed v2 — SP-007 restored\n", encoding="utf-8")
        result = orchestrator._apply_governed_verification_recovery(
            gen, _verdict("N-004-R1", "the parts endpoint returned status 500"),
        )

        assert result.get("rejected") is not True, result
        assert _refusals(orchestrator) == []
        assert _applied(orchestrator), "a grounded re-verification must apply"
        recorded = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"] == "orchestrator.recovery_churn.recorded"
            and event["payload"].get("kind") == "verification_recovery_applied"
        ]
        assert recorded and recorded[-1]["productive"] is True
        # b33 case1 fix10：调用方仍宣告 productive，但信用只认**真的后置进展**。
        # 本次恢复只改了图、没有节点到达 completed，故照计无进展——预算仍是
        # 20（未放宽），只是判据由运行自己算、不由调用方宣告。
        assert recorded[-1]["productive_proved"] is False
        assert (
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn == 1
        )


def _stalled_graph(orchestrator, tmp_path):
    """Live b31 shape: terminal-failed track plus a blocked report consumer."""
    from AstralNexus.core.task_graph import TaskNode

    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "init_db.py").write_text("# seed v1\n", encoding="utf-8")

    graph = orchestrator.task_graph
    graph.add_node(TaskNode(
        node_id="N-002-R5", goal="seed the parts database", role="coder",
        status="completed", owned_paths=["deliverables/app/backend/"],
    ))
    terminal = TaskNode(
        node_id="N-005-R3", goal="run the security test track", role="tester",
        status="failed", owned_paths=["deliverables/runs/security/"],
    )
    terminal.metadata["recovery_of"] = "N-005"
    terminal.metadata["_terminal_failure"] = True
    terminal.metadata["_terminal_reason"] = "recovery_exhausted"
    graph.add_node(terminal)
    graph.add_node(TaskNode(
        node_id="N-006-R3", goal="aggregate the test reports",
        role="synthesizer", status="blocked", dependencies=["N-005-R3"],
    ))
    return graph


def _structural_patch():
    """A genuinely structural repair: new producer + replacement re-verification."""
    from AstralNexus.core.governor import TaskGraphPatch

    patch = TaskGraphPatch(
        add_nodes=[{
            "node_id": "N-002-R6",
            "goal": "repair the seed script",
            "task_type": "implementation",
            "role": "coder",
            "dependencies": ["N-002-R5"],
            "owned_paths": ["deliverables/app/backend/init_db.py"],
        }],
        reason_codes=["DEPENDENCY_REPAIR"],
    )
    patch.add_nodes.append({
        "node_id": "N-005-R4",
        "goal": "re-run the security test track",
        "task_type": "verification",
        "role": "tester",
        "dependencies": ["N-002-R6"],
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


def test_structural_replan_credits_progress_only_after_a_completion(
    tmp_path, monkeypatch
):
    """(c) 结构修复本身不再是信用来源（b33 case1 fix10）。

    旧契约把"补丁改了图且留下非空 ready 前沿"当进展。fix10 实跑证伪了它：
    30 次恢复（预算 20）熔断一次未装——正因为 supersede/接管替换同样改图、
    同样留下 ready 前沿。新契约只认**自上次计入进展以来真有节点到达
    completed**：ready 前沿不是进展，完成才是。
    """
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="f12-productive-replan") as orchestrator:
        _stalled_graph(orchestrator, tmp_path)
        orchestrator.governor.llm_client = object()
        orchestrator.governor.llm_matching_enabled = True
        monkeypatch.setattr(
            orchestrator.governor, "plan_task_graph_patch",
            lambda goal, **kwargs: _structural_patch(),
        )

        result = _replan(orchestrator)

        assert result.get("changed") is True, result
        assert result.get("repeat_count") == 0, result
        assert orchestrator._executable_ready_frontier() == ["N-002-R6"]
        state = orchestrator.runtime_state_manager.state
        # 单调账本照记（预算没有放宽）……
        assert state.recovery_churn_total == 1
        # ……但也照计无进展：只有 ready 前沿，没有新完成的节点。
        assert state.recovery_no_progress_churn == 1
        assert orchestrator._recovery_budget_exhausted is False
        recorded = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"] == "orchestrator.recovery_churn.recorded"
        ]
        assert recorded and recorded[-1]["productive"] is True
        assert recorded[-1]["productive_proved"] is False
        assert recorded[-1]["no_progress_total"] == 1

        # 反面：先让一个节点真的到达 completed，同样形态的恢复才被认领。
        _record_real_progress(orchestrator, 1)
        orchestrator._consume_recovery_churn(
            "stalled_replan_changed", productive=True
        )
        assert state.recovery_churn_total == 2
        assert state.recovery_no_progress_churn == 0   # 1 → 1//2（衰减，非清零）
        assert orchestrator._recovery_budget_exhausted is False


def test_repeated_unproductive_replans_still_trip_the_breaker(
    tmp_path, monkeypatch
):
    """(c) 反面：重复无产出重规划仍然触顶熔断。"""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(
        tmp_path, run_id="f12-unproductive-replan", recovery_churn_budget=2,
    ) as orchestrator:
        _stalled_graph(orchestrator, tmp_path)
        orchestrator.governor.llm_client = object()
        orchestrator.governor.llm_matching_enabled = True
        monkeypatch.setattr(
            orchestrator.governor, "plan_task_graph_patch",
            lambda goal, **kwargs: TaskGraphPatch(add_nodes=[], update_nodes=[]),
        )

        for _ in range(3):
            _replan(orchestrator)

        assert (
            orchestrator.runtime_state_manager.state.recovery_no_progress_churn >= 2
        )
        assert orchestrator._recovery_budget_exhausted is True


def test_no_progress_budget_is_separate_from_the_monotone_ledger(tmp_path):
    """(d) 预算常量未放宽（20），无进展计数与单调总数分离，且**只有完成节点**
    才兑现进展信用（b33 case1 fix10）。"""
    from AstralNexus.core.orchestrator import Orchestrator

    with Orchestrator(tmp_path, run_id="f12-budget-defaults") as orchestrator:
        assert orchestrator.recovery_churn_budget == 20
        manager = orchestrator.runtime_state_manager
        assert manager.state.recovery_churn_total == 0
        assert manager.state.recovery_no_progress_churn == 0

        orchestrator._consume_recovery_churn("stalled_replan_unchanged")
        assert manager.state.recovery_churn_total == 1
        assert manager.state.recovery_no_progress_churn == 1

        # 宣告 productive 但没有任何节点完成：照计无进展（旧契约在此清零）。
        orchestrator._consume_recovery_churn(
            "stalled_replan_changed", productive=True
        )
        assert manager.state.recovery_churn_total == 2
        assert manager.state.recovery_no_progress_churn == 2
        assert orchestrator._recovery_budget_exhausted is False

        # 真有节点到达 completed 之后，同样的宣告才被兑现：2 → 2//2 = 1。
        _record_real_progress(orchestrator, 2)
        orchestrator._consume_recovery_churn(
            "stalled_replan_changed", productive=True
        )
        assert manager.state.recovery_churn_total == 3
        assert manager.state.recovery_no_progress_churn == 1
        assert orchestrator._recovery_budget_exhausted is False


def test_invalid_recovery_proposal_escalates_one_bounded_replan(
    tmp_path, monkeypatch
):
    """(f) 畸形恢复提案 → 正好一次有界重规划（F3）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="f3-invalid-proposal") as orchestrator:
        graph = orchestrator.task_graph
        failed = TaskNode(
            node_id="N-005-R3", goal="run the security test track",
            role="tester", status="failed",
        )
        graph.add_node(failed)
        graph.add_node(TaskNode(
            node_id="N-006-R3", goal="aggregate the test reports",
            role="synthesizer", status="blocked", dependencies=["N-005-R3"],
        ))
        calls: list = []
        monkeypatch.setattr(
            orchestrator, "_replan_stalled_graph",
            lambda **kwargs: calls.append(kwargs) or {
                "changed": True,
                "repeat_count": 0,
                "reason": "governor_incremental_replan",
            },
        )

        assert (
            orchestrator._escalate_invalid_recovery_proposal(
                failed, "N-005-R3"
            ) is True
        )
        assert len(calls) == 1
        # _replan_stalled_graph derives the dossier from the *blocked consumers*
        # of the failed node; handing it the failed node itself would describe
        # that node's own (completed) producers instead.
        assert calls[0]["blocked_nodes"] == ["N-006-R3"]
        assert calls[0]["reason"] == "invalid_recovery_proposal"

        # One shot per node: the escalation must not become its own loop.
        assert (
            orchestrator._escalate_invalid_recovery_proposal(
                failed, "N-005-R3"
            ) is False
        )
        assert len(calls) == 1
        replanned = [
            event["payload"] for event in orchestrator.trace_recorder.events
            if event["event_type"]
            == "orchestrator.invalid_recovery_proposal.replanned"
        ]
        assert replanned
        assert replanned[-1]["blocked_nodes"] == ["N-006-R3"]


def test_user_resume_clears_the_no_progress_counter_with_audit(tmp_path):
    """(e) --user-resume 同时清零无进展计数并审计 prior_no_progress。"""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from AstralNexus.cli.main import _resume_task
    from AstralNexus.core.governor import GoalSpec
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    clock_now = datetime(2026, 9, 14, 0, 0, 0, tzinfo=timezone.utc)

    def clock() -> datetime:
        return clock_now

    budget = 2
    run_dir = tmp_path / "run-user"
    coordinator = RunCoordinator(
        run_dir, run_id="churn-resume", lease_seconds=120, grace_seconds=10,
        now_fn=clock,
    )
    lease = coordinator.acquire("owner-A")
    orchestrator = Orchestrator(
        run_dir=run_dir,
        run_id="churn-resume",
        graph_store=SQLiteTaskGraphStore(run_dir / "task_graph.sqlite"),
        ownership_probe=lambda: coordinator.owns(lease),
        fence_token_provider=lambda: coordinator.fence_token(lease),
        recovery_churn_budget=budget,
    )
    orchestrator.task_graph.add_node({
        "node_id": "T-1", "goal": "small service", "role": "coder",
        "status": "completed",
    })
    orchestrator.governor.goal_spec = GoalSpec(
        goal_id="G1", task_goal="small service",
        normalized_goal="small service", hard_constraints=[],
    )
    orchestrator.governor.delivery_contract = {
        "required_outputs": [], "execution_evidence_policy": "runtime_required",
    }
    orchestrator.governor.goal_version = 1
    for _ in range(budget):
        orchestrator._consume_recovery_churn("verification_recovery_applied")
    assert orchestrator._recovery_budget_exhausted is True
    assert orchestrator._persist_graph_checkpoint(stage="wave_churn") is True
    assert coordinator.finish(
        lease, "blocked",
        stop_reason="autonomous recovery churn budget exhausted",
        stop_code="no_progress_breaker",
    ) is True

    captured = {}

    def on_orchestrator(resumed, _run_dir):
        captured["orchestrator"] = resumed
        resumed.run = lambda *a, **kw: {
            "status": "completed", "iterations": 1, "stop_reason": "",
        }

    task_file = tmp_path / "task.txt"
    task_file.write_text("continue the service work", encoding="utf-8")
    args = SimpleNamespace(
        run_dir=str(run_dir), task=str(task_file), output=str(run_dir),
        model="test-model", provider="test", max_parallel=1, config=None,
        user_resume=True,
    )
    assert _resume_task(args, tmp_path / "home", on_orchestrator=on_orchestrator) == 0  # No new outputs: presentation is not required.

    resumed = captured["orchestrator"]
    state = resumed.runtime_state_manager.state
    assert state.recovery_churn_total == 0
    assert state.recovery_no_progress_churn == 0
    assert resumed._recovery_budget_exhausted is False
    audits = [
        event["payload"] for event in resumed.trace_recorder.events
        if event["event_type"] == "run.user_resume.audit"
    ]
    assert audits
    reset = audits[-1]["churn_budget_reset"]
    assert reset["prior_total"] == budget
    assert reset["prior_no_progress"] == budget
    # ``budget`` documents the ceiling the resumed run works under — the default
    # (20), never an override: --user-resume grants a fresh budget, it does not
    # widen one (m00547: 不加预算，只查根因).
    assert resumed.recovery_churn_budget == 20
    assert reset["budget"] == resumed.recovery_churn_budget


# ---------------------------------------------------------------------------
# (g)/(h) reverse proof — the breaker still stops a genuinely unproductive run,
# and it no longer stops a run whose churn carried real structural progress.
# ---------------------------------------------------------------------------

def _churn_only_orchestrator(tmp_path, run_id: str, budget: int = 20, max_iterations: int = 0):
    """A run that skips planning and enters the main loop directly."""
    from pathlib import Path

    from AstralNexus.core.orchestrator import Orchestrator

    run_dir = Path(tmp_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = Orchestrator(
        run_dir, run_id=run_id, max_iterations=max_iterations,
        recovery_churn_budget=budget,
    )
    orchestrator.task_graph.add_node({
        "node_id": "T-1", "goal": "small service", "role": "coder",
        "status": "completed",
    })
    orchestrator.task_graph.add_node({
        "node_id": "T-2", "goal": "small service api", "role": "coder",
        "status": "ready",
    })
    return orchestrator


def _record_real_progress(orchestrator, index: int):
    """b33 case1 fix10: the only credit that counts — a node reaches completed."""
    from AstralNexus.core.task_graph import TaskNode

    orchestrator.task_graph.add_node(TaskNode(
        node_id="W-%03d" % index, goal="delivered increment", role="coder",
        status="completed",
    ))
    orchestrator.runtime_state_manager.credit_evidence_key("accepted-requirement-%s" % index)


def _blocked_events(orchestrator):
    return [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "orchestrator.blocked"
    ]


def test_twenty_genuine_no_progress_events_still_trip_the_breaker(tmp_path):
    """(g) 20 consecutive no-progress events ⇒ stand-down at exactly 20, with
    the original closed failure class (``recovery_churn_budget_exhausted`` /
    ``no_progress_breaker``) and a stop_reason carrying both counters."""
    orchestrator = _churn_only_orchestrator(tmp_path, "churn-twenty")
    assert orchestrator.recovery_churn_budget == 20
    for index in range(19):
        orchestrator._consume_recovery_churn("stalled_replan_unchanged")
        assert orchestrator._recovery_budget_exhausted is False, index
    assert orchestrator.runtime_state_manager.state.recovery_no_progress_churn == 19

    orchestrator._consume_recovery_churn("stalled_replan_unchanged")
    assert orchestrator.runtime_state_manager.state.recovery_no_progress_churn == 20
    assert orchestrator._recovery_budget_exhausted is True

    result = orchestrator.run(goal="implement a small service")
    assert result["status"] == "blocked"
    assert result["stop_code"] == "no_progress_breaker"
    stop_reason = str(result["stop_reason"] or "")
    assert "churn budget exhausted" in stop_reason          # kept verbatim
    assert "no_progress=20" in stop_reason
    assert "total_churn=20" in stop_reason
    assert "budget=20" in stop_reason
    # 信息传递如实：有进展的结构修复不得被算进"无进展"叙述里。
    assert "productive structural repairs are not" in stop_reason

    blocked = _blocked_events(orchestrator)
    assert blocked
    # 首个原因仍是原错误类型，未新增词表、未换类型。
    assert blocked[-1]["reason"] == "recovery_churn_budget_exhausted"
    assert blocked[-1]["no_progress_total"] == 20
    assert blocked[-1]["churn_total"] == 20
    assert blocked[-1]["budget"] == 20
    # 帽在选择任务之前生效：ready 节点从未执行。
    assert orchestrator.task_graph.get_node("T-2").status == "ready"


def test_structural_progress_inside_twenty_events_does_not_stop_the_run(tmp_path):
    """(h) the live F12 shape: churn reaching the old ceiling where some events
    follow a *real completion* ⇒ the stand-down gate stays open.

    Live b31: events 1-19 carried no provable progress and the 20th came with a
    patch that had just unblocked N-401/N-402/R-801 (``changed=true``,
    ``repeat_count=0``) — the monotone counter stood the run down anyway and
    delivery stayed at zero.

    b33 case1 fix10 收紧后，"进展"必须是**真的完成**：这里正是靠一个节点到达
    completed 兑现那张信用（旧契约只靠 changed + ready 前沿；fix10 实测 30 次
    恢复、熔断一次未装，就是被这条旧口径放过去的）。预算仍是 20。

    The gate the main loop consults before standing down is the flag
    ``_recovery_budget_exhausted`` (``core/orchestrator.py:6404``); test (g)
    exercises it end to end with a real ``run()`` that returns
    ``blocked``/``no_progress_breaker``.  Here the same sequences are proven
    never to set that flag.  A full ``run()`` past this point is deliberately
    *not* driven: the seeded graph keeps producing genuinely unproductive events
    of its own, so a stand-down on those would be the correct — and already
    proven — behaviour of (g), not a counter-example."""
    live_shape = _churn_only_orchestrator(tmp_path / "live-shape", "churn-live-shape")
    for _ in range(19):
        live_shape._consume_recovery_churn("stalled_replan_unchanged")
    assert live_shape.runtime_state_manager.state.recovery_no_progress_churn == 19
    # 第 20 个事件：一个真有节点完成之后到来的结构修复。
    _record_real_progress(live_shape, 9001)
    live_shape._consume_recovery_churn("stalled_replan_changed", productive=True)
    state = live_shape.runtime_state_manager.state
    assert state.recovery_churn_total == 20       # 单调账本 = 现场事件数
    assert state.recovery_no_progress_churn == 9  # 19 → 19//2（衰减，非清零）
    # 旧规则在此就是停机点（单调账本 20 >= 预算 20）；新规则下闸门不开。
    assert live_shape.recovery_churn_budget == 20
    assert live_shape._recovery_budget_exhausted is False
    assert state.recovery_no_progress_churn < live_shape.recovery_churn_budget

    # 同样的深度、交错出现：每次"有进展"都配一个真实完成，单调账本超过 20 也不停机。
    interleaved = _churn_only_orchestrator(tmp_path / "mixed", "churn-mixed")
    for index in range(20):
        # changed=false / repeat_count>0: genuinely unproductive event
        interleaved._consume_recovery_churn("stalled_replan_unchanged")
        if index % 3 == 2:
            # 一个节点真的完成 + changed=true：兑现那张进展信用
            _record_real_progress(interleaved, index)
            interleaved._consume_recovery_churn(
                "stalled_replan_changed", productive=True,
            )

    mixed = interleaved.runtime_state_manager.state
    assert mixed.recovery_churn_total > 20        # 单调账本照记全部事件
    assert mixed.recovery_no_progress_churn < 20  # 无进展计数按证据衰减
    assert interleaved._recovery_budget_exhausted is False


def test_takeover_chain_without_a_completion_still_trips_the_breaker(tmp_path):
    """(j) b33 case1 fix10 现场形状：接管链——图一直在变、却没有一个节点完成——
    必须照数计入无进展，到顶即装熔断。

    现场逐字（`runtime_state.json` 终局）：``recovery_churn_total=30``、
    ``recovery_churn_budget=20``、``recovery_no_progress_churn=0``。30 次恢复里
    13 次被判 productive（``verification_recovery_applied`` ×7、
    ``stalled_replan_changed`` ×5 …），每次把无进展计数减半，于是 30/20 一次未装。
    成因：supersede/接管替换（P-115 superseded → P-116 ``recovery_of=P-115``）
    同样改图、同样留下 ready 前沿，被旧口径当成了进展。
    """
    orchestrator = _churn_only_orchestrator(tmp_path, "churn-takeover", budget=20)
    for index in range(20):
        # 接管链的形状：图在变（productive=True），但没有一个节点完成。
        orchestrator._consume_recovery_churn(
            "stalled_replan_changed" if index % 2 else "verification_recovery_applied",
            productive=True,
        )
    state = orchestrator.runtime_state_manager.state
    assert state.recovery_churn_total == 20
    assert state.recovery_no_progress_churn == 20   # 旧契约在此为 0，熔断不装
    assert orchestrator._recovery_budget_exhausted is True
    recorded = [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "orchestrator.recovery_churn.recorded"
    ]
    assert len(recorded) == 20
    assert all(payload["productive"] is True for payload in recorded)
    assert all(payload["productive_proved"] is False for payload in recorded)
    # 判据取自图本身：只有种子节点 T-1 处于 completed，接管链没加过任何一个。
    assert all(payload["completed_nodes"] == 1 for payload in recorded)


def test_legacy_runtime_state_file_without_the_new_key_still_loads(tmp_path):
    """(3) backward compatibility: a ``runtime_state.json`` written before F12
    has no ``recovery_no_progress_churn`` key.  Loading it must not raise and
    must default the tally to 0, while ``record_churn`` semantics stay verbatim
    (monotone, immune to "changed", unrelated to the no-progress tally)."""
    import json

    from AstralNexus.core.runtime_state import RuntimeState, RuntimeStateManager

    run_dir = tmp_path / "churn-legacy"
    run_dir.mkdir(parents=True, exist_ok=True)
    seed = RuntimeState(run_id="churn-legacy")
    legacy = {
        key: value for key, value in vars(seed).items()
        if key != "recovery_no_progress_churn"
    }
    legacy["recovery_churn_total"] = 7
    legacy["repeated_recovery"] = 4
    assert "recovery_no_progress_churn" not in legacy  # really a pre-F12 payload
    (run_dir / "runtime_state.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8",
    )

    manager = RuntimeStateManager(run_dir, "churn-legacy")
    assert manager.state.recovery_churn_total == 7
    assert manager.state.recovery_no_progress_churn == 0

    # record_churn 逐字不变：单调累计 + 无视"有变化" + 不触碰无进展计数。
    assert manager.record_recovery("legacy-fingerprint", changed=True) == 0
    assert manager.state.repeated_recovery == 0
    assert manager.record_churn("verification_recovery_applied") == 8
    assert manager.state.recovery_no_progress_churn == 0
    assert manager.record_churn("stalled_replan_changed") == 9
    # 单调账本单独存在不足以触发熔断：无进展计数仍为 0。
    assert manager.state.recovery_no_progress_churn == 0

    reloaded = RuntimeStateManager(run_dir, "churn-legacy")
    assert reloaded.state.recovery_churn_total == 9
    assert reloaded.state.recovery_no_progress_churn == 0