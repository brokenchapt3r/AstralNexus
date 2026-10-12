"""第三批统一运行入口证明（doc 17）：双启动只有一个执行者；旧管理者迟到写入
被拒；崩溃发生在执行结束/验收写入/图状态更新各边界均可恢复（共享检查点版本）；
用户暂停后监控不能自动重启。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from AstralNexus.runtime.run_coordinator import RunCoordinator


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 14, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _coord(tmp_path, clock, run_id="run-1"):
    return RunCoordinator(
        tmp_path, run_id=run_id, lease_seconds=120, grace_seconds=10, now_fn=clock,
    )


def test_double_start_admits_single_executor(tmp_path):
    clock = Clock()
    first = _coord(tmp_path, clock).acquire("owner-A")
    assert first is not None and first.generation == 1
    # a second process (suite vs monitor vs CLI) cannot seize a live lease
    assert _coord(tmp_path, clock).acquire("owner-B") is None
    row = _coord(tmp_path, clock).current()
    assert row["owner_id"] == "owner-A"
    assert row["status"] == "running"


def test_live_lease_renews_and_blocks_takeover(tmp_path):
    clock = Clock()
    lease = _coord(tmp_path, clock).acquire("owner-A")
    for _ in range(5):
        clock.advance(60)  # inside lease_seconds + grace
        renewed = _coord(tmp_path, clock).renew(lease)
        assert renewed is not None
        lease = renewed
    assert _coord(tmp_path, clock).acquire("owner-B") is None


def test_orphan_takeover_and_late_writes_rejected(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    stale = coord.acquire("owner-A")
    clock.advance(600)  # owner A presumed dead: lease expired beyond grace
    fresh = _coord(tmp_path, clock).acquire("owner-B")
    assert fresh is not None and fresh.generation == stale.generation + 1
    # fenced: the dead owner cannot renew or write any terminal state
    assert coord.renew(stale) is None
    assert coord.finish(stale, "completed") is False
    assert coord.record_checkpoint(stale, {"graph_revision": 999}) is False
    # the new owner writes normally
    assert coord.finish(fresh, "completed", stop_reason="done") is True
    assert coord.current()["status"] == "completed"


def test_terminal_run_is_never_reacquired(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.finish(lease, "completed", stop_reason="delivered") is True
    clock.advance(600)
    assert coord.acquire("owner-B") is None
    assert coord.acquire("owner-B", user_resume=True) is None
    assert coord.request_cancel() == "already_terminal"


def test_blocked_run_allows_automatic_recovery(tmp_path):
    """blocked（明确崩溃/中断停机码）允许自动续跑；未知码除外。"""
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.finish(
        lease, "blocked", stop_reason="process terminated", stop_code="worker_crash",
    ) is True
    clock.advance(600)
    auto = coord.acquire("monitor-auto")  # user_resume=False
    assert auto is not None and auto.generation == lease.generation + 1


def test_blocked_unknown_or_manual_stop_codes_reject_auto_recovery(tmp_path):
    """闭合枚举 + 默认核查（第二轮修正①，用户复现反转）：真实熔断文案
    不含旧关键词也能被停机码分级；未知/缺失码一律要求人工核查。"""
    clock = Clock()
    cases = [
        ("no_progress_breaker", "same autonomous recovery outcome repeated three times"),
        ("unknown_blocked", "delivery completion made no progress three times"),
        ("", "legacy row without stop_code"),
        ("torn_commit_detected", "commit identity reconciliation reject"),
    ]
    for index, (code, text) in enumerate(cases):
        coord = _coord(tmp_path, clock, run_id=f"run-{index}")
        lease = coord.acquire("owner-A")
        assert coord.finish(lease, "blocked", stop_reason=text, stop_code=code) is True
        assert coord.acquire("monitor-auto") is None, (code, text)
        resumed = coord.acquire("operator", user_resume=True)
        assert resumed is not None and resumed.generation == 2, (code, text)


def test_verification_unavailable_can_retry_with_ordinary_resume(tmp_path):
    """No-verdict transport outages are retryable without a user-resume override."""
    clock = Clock()
    coord = _coord(tmp_path, clock, run_id="verification-outage")
    first = coord.acquire("owner-A")
    assert coord.finish(
        first,
        "blocked",
        stop_reason="Verifier could not produce a verdict after bounded retries",
        stop_code="verification_unavailable",
    )

    retry = coord.acquire("monitor-auto")

    assert retry is not None and retry.generation == first.generation + 1
    row = coord.current()
    assert row["status"] == "running"
    assert row["stop_code"] == "verification_unavailable"
    assert "could not produce a verdict" in row["stop_reason"]
    assert row["last_resume_audit"] is None


def test_reconcile_commit_identities_protocol():
    """gap③+第二轮修正②：完整检查 S_graph >= S_state >= S_ref 三段不变量。"""
    from AstralNexus.runtime.run_coordinator import reconcile_commit_identities

    assert reconcile_commit_identities(5, 5, 5) == "clean"
    assert reconcile_commit_identities(0, 0, 0) == "clean"  # legacy 无标识
    assert reconcile_commit_identities(6, 5, 5) == "torn_window_continue"
    assert reconcile_commit_identities(6, 6, 5) == "torn_window_continue"
    assert reconcile_commit_identities(5, 6, 5) == "reject_state_ahead"
    assert reconcile_commit_identities(5, 5, 6) == "reject_reference_ahead_of_state"
    # 用户复核复现：ref=9 违反 S_state >= S_ref，此前被漏判为 torn_window
    assert reconcile_commit_identities(10, 8, 9) == "reject_reference_ahead_of_state"
    # 非法序号
    assert reconcile_commit_identities(-1, 0, 0) == "reject_invalid_seq"
    assert reconcile_commit_identities(0, -5, 0) == "reject_invalid_seq"
    assert reconcile_commit_identities(0, 0, -1) == "reject_invalid_seq"
    # legacy 混合/标记缺失：围栏行丢失但游标或引用宣称提交过 → 拒绝
    assert reconcile_commit_identities(0, 5, 0) == "reject_state_ahead"
    assert reconcile_commit_identities(0, 0, 5) == "reject_reference_ahead_of_state"


def test_expired_lease_cannot_write_terminal(tmp_path):
    """gap④复现修正：租约过期即禁止写入——含终态，且发生在任何接管之前。"""
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    clock.advance(600)  # expired; nobody has taken over yet
    assert coord.owns(lease) is False
    assert coord.renew(lease) is None
    assert coord.finish(lease, "completed") is False
    assert coord.record_checkpoint(lease, {"graph_revision": 9}) is False
    row = coord.current()
    assert row["status"] == "running"
    assert row["owner_id"] == "owner-A"
    # a successor can still take over afterwards
    fresh = coord.acquire("owner-B")
    assert fresh is not None and fresh.generation == lease.generation + 1


def test_owns_probe_semantics(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.owns(lease) is True
    coord.request_pause()
    assert coord.owns(lease) is False  # paused: no longer active
    clock.advance(600)
    fresh = coord.acquire("owner-B", user_resume=True)
    assert coord.owns(fresh) is True
    assert coord.owns(lease) is False


def test_cancel_of_halted_run_lands_directly(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    coord.acquire("owner-A")
    assert coord.request_pause() == "paused"
    assert coord.request_cancel() == "taken_over"
    assert coord.current()["status"] == "cancelled"
    assert coord.acquire("owner-B", user_resume=True) is None


def test_user_pause_blocks_automatic_restart(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.request_pause() == "paused"
    # the running owner is fenced out at its next renewal boundary
    assert coord.renew(lease) is None
    assert coord.should_stop(lease) is True
    # even after the lease fully expires, automatic acquisition stays rejected
    clock.advance(600)
    assert coord.acquire("monitor-auto") is None
    # only an explicit user resume may reacquire
    resumed = coord.acquire("owner-B", user_resume=True)
    assert resumed is not None and resumed.generation == lease.generation + 1


def test_cancel_request_while_owner_alive_is_observed_at_boundary(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.request_cancel() == "requested"
    assert coord.is_cancel_requested() is True
    assert coord.should_stop(lease) is True
    # the owner still holds the lease and writes its own terminal state
    assert coord.renew(lease) is not None
    assert coord.finish(lease, "cancelled", stop_reason="user_cancel") is True


def test_cancel_takes_over_expired_lease(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    coord.acquire("owner-A")
    clock.advance(600)
    assert coord.request_cancel() == "taken_over"
    assert coord.current()["status"] == "cancelled"


def test_crash_boundaries_recover_via_shared_checkpoint(tmp_path):
    """A crash at any boundary (execution end / acceptance write / graph
    update) leaves the shared version reference; the successor reads it
    instead of stitching arbitrary latest states from two databases."""
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.record_checkpoint(lease, {
        "graph_revision": 41, "iteration": 12, "phase": "acceptance_write",
    }) is True
    # crash: no finish, no release
    clock.advance(600)
    successor = coord.acquire("owner-B")
    assert successor is not None
    assert coord.last_checkpoint() == {
        "graph_revision": 41, "iteration": 12, "phase": "acceptance_write",
    }
    assert coord.record_checkpoint(successor, {
        "graph_revision": 42, "iteration": 13, "phase": "graph_update",
    }) is True
    assert coord.last_checkpoint()["graph_revision"] == 42


def test_should_stop_detects_foreign_ownership(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    clock.advance(600)
    coord.acquire("owner-B")
    assert coord.should_stop(lease) is True


def test_finish_rejects_non_terminal_status(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    try:
        coord.finish(lease, "running")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_cli_host_boundary_rejects_double_start(tmp_path):
    """双启动只有一个执行者：CLI 主机边界在任何运行状态写入前拒绝第二执行者。"""
    from AstralNexus.cli.main import _acquire_run_ownership

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    notices: list[dict] = []
    _coord_a, lease_a = _acquire_run_ownership(
        run_dir, user_resume=False, notice_sink=notices.append,
        notice_event="interactive.run.ownership_rejected",
    )
    assert lease_a is not None
    coord_b, lease_b = _acquire_run_ownership(
        run_dir, user_resume=False, notice_sink=notices.append,
        notice_event="interactive.run.ownership_rejected",
    )
    assert coord_b is None and lease_b is None
    assert any("另一存活执行者持有租约" in str(item["text"]) for item in notices)


def test_orchestrator_persistence_boundary_feeds_checkpoint_sink(tmp_path):
    """图状态持久化边界把共享版本引用写入协调器（崩溃恢复锚点）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    collected = []
    orchestrator = Orchestrator(
        run_dir=tmp_path,
        graph_store=SQLiteTaskGraphStore(tmp_path / "task_graph.sqlite"),
        checkpoint_sink=collected.append,
    )
    orchestrator._persist_graph_checkpoint(stage="node_verified", node_id=None)
    assert collected, "persistence boundary must feed the checkpoint sink"
    payload = collected[-1]
    assert payload["phase"] == "graph_checkpoint:node_verified"
    assert isinstance(payload["graph_revision"], int)
    assert payload["node_count"] == 0


def _make_orchestrator(run_dir, **kwargs):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        run_dir=run_dir,
        graph_store=SQLiteTaskGraphStore(run_dir / "task_graph.sqlite"),
        **kwargs,
    )


def test_persistence_fence_skips_authoritative_writes(tmp_path):
    """gap①：失去所有权后不得先写坏任务图/运行状态再求原谅——围栏在
    权威持久化之前，被拒时任务图、运行状态、确认引用一概不写。"""
    sink_calls: list = []
    orchestrator = _make_orchestrator(
        tmp_path,
        checkpoint_sink=lambda payload: sink_calls.append(payload) or True,
        ownership_probe=lambda: False,
    )
    orchestrator._persist_graph_checkpoint(stage="node_verified")
    assert sink_calls == []  # never reached post-confirmation
    assert orchestrator._ownership_lost is True
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "orchestrator.persistence.fenced" in events
    assert "orchestrator.persistence.checkpoint" not in events
    assert not (tmp_path / "runtime_state.json").exists()


def test_persistence_fence_fails_closed_on_probe_error(tmp_path):
    """探针异常=所有权不可证明=fail closed。"""
    def _boom():
        raise RuntimeError("coordinator unreachable")

    orchestrator = _make_orchestrator(tmp_path, ownership_probe=_boom)
    orchestrator._persist_graph_checkpoint(stage="node_verified")
    assert orchestrator._ownership_lost is True
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "orchestrator.persistence.fenced" in events


def test_confirmation_refusal_marks_ownership_lost(tmp_path):
    """写入与确认之间丢失所有权：确认被拒（False）→ 停止后续持久化。"""
    orchestrator = _make_orchestrator(
        tmp_path,
        checkpoint_sink=lambda payload: False,
        ownership_probe=lambda: True,
    )
    orchestrator._persist_graph_checkpoint(stage="node_verified")
    assert orchestrator._ownership_lost is True
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "orchestrator.persistence.ownership_lost_after_write" in events


def test_end_to_end_takeover_fences_old_executor(tmp_path):
    """集成：真实协调器+真实 orchestrator——接管后旧执行者的权威持久化
    被前置围栏拦下（不写任务图/运行状态/引用），新执行者写入并确认，
    共享引用与状态版本一致（gap①②④ 全链路）。"""
    clock = Clock()
    run_dir = tmp_path / "run"
    coord = _coord(run_dir, clock, run_id="run")
    lease_a = coord.acquire("owner-A")
    assert lease_a is not None

    sink_a: list = []
    orch_a = _make_orchestrator(
        run_dir,
        checkpoint_sink=lambda p: (sink_a.append(p), coord.record_checkpoint(lease_a, p))[1],
        ownership_probe=lambda: coord.owns(lease_a),
    )
    orch_a._persist_graph_checkpoint(stage="wave_1")
    assert sink_a and coord.last_checkpoint()["phase"] == "graph_checkpoint:wave_1"

    # A dies; lease expires; B takes over (generation fence)
    clock.advance(600)
    lease_b = coord.acquire("owner-B")
    assert lease_b is not None and lease_b.generation == lease_a.generation + 1

    sink_b: list = []
    orch_b = _make_orchestrator(
        run_dir,
        checkpoint_sink=lambda p: (sink_b.append(p), coord.record_checkpoint(lease_b, p))[1],
        ownership_probe=lambda: coord.owns(lease_b),
    )
    orch_b._persist_graph_checkpoint(stage="wave_1_b")
    assert coord.last_checkpoint()["phase"] == "graph_checkpoint:wave_1_b"

    # the zombie executor wakes up and tries a late authoritative write
    orch_a._persist_graph_checkpoint(stage="late_write")
    events_a = [event["event_type"] for event in orch_a.trace_recorder.events]
    assert "orchestrator.persistence.fenced" in events_a
    assert orch_a._ownership_lost is True
    assert len(sink_a) == 1  # only wave_1 ever confirmed for A
    # the shared reference still belongs to B and matches the persisted state
    reference = coord.last_checkpoint()
    assert reference["phase"] == "graph_checkpoint:wave_1_b"
    assert reference["graph_revision"] == int(
        orch_b.runtime_state_manager.state.graph_revision
    )


def test_save_checkpoint_aborts_entirely_when_fenced(tmp_path):
    """gap①隔离复现修正：围栏失败必须中止整个保存操作——调用方不得在
    _persist_graph_checkpoint 被拒后继续 runtime_state_manager.persist()。"""
    orchestrator = _make_orchestrator(
        tmp_path,
        ownership_probe=lambda: False,
    )
    orchestrator._save_checkpoint()
    assert orchestrator._ownership_lost is True
    assert not (tmp_path / "runtime_state.json").exists()
    events = [event["event_type"] for event in orchestrator.trace_recorder.events]
    assert "checkpoint.save_fenced" in events
    assert "checkpoint.saved" not in events


def test_runtime_state_persist_fence_skips_write(tmp_path):
    """json-only 写入路径（snapshot/record_recovery）也共享围栏。"""
    from AstralNexus.core.runtime_state import RuntimeStateManager

    manager = RuntimeStateManager(tmp_path, "run-1")
    manager.write_fence = lambda: False
    manager.persist()
    assert not (tmp_path / "runtime_state.json").exists()

    def _boom():
        raise RuntimeError("coordinator unreachable")

    manager.write_fence = _boom  # fail closed
    manager.persist()
    assert not (tmp_path / "runtime_state.json").exists()

    manager.write_fence = lambda: True
    manager.persist()
    assert (tmp_path / "runtime_state.json").exists()


def test_toctou_interleave_joint_commit_rejects_stale_writer(tmp_path):
    """gap②隔离复现修正：A 探测成功后暂停 → 租约过期 → B 接管并写入 →
    A 恢复写入。写后确认只能事后发现覆盖；联合提交（所有权验证与图写入
    同一跨进程事务）在权威事务内直接拒绝 A 的过期令牌，覆盖不会发生。"""
    import json as _json

    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    clock = Clock()
    run_dir = tmp_path / "run"
    coord = _coord(run_dir, clock, run_id="run")
    lease_a = coord.acquire("owner-A")
    assert lease_a is not None

    sink_a: list = []
    orch_a = _make_orchestrator(
        run_dir,
        run_id="graph-a",
        checkpoint_sink=lambda p: (sink_a.append(p), coord.record_checkpoint(lease_a, p))[1],
        # 模拟“探测在暂停前已通过”：A 缓存的探针恒真
        ownership_probe=lambda: True,
        fence_token_provider=lambda: coord.fence_token(lease_a),
    )

    # A 暂停（未写入）；租约过期；B 接管并成功联合提交
    clock.advance(600)
    lease_b = coord.acquire("owner-B")
    assert lease_b is not None and lease_b.generation == lease_a.generation + 1
    sink_b: list = []
    orch_b = _make_orchestrator(
        run_dir,
        run_id="graph-b",
        checkpoint_sink=lambda p: (sink_b.append(p), coord.record_checkpoint(lease_b, p))[1],
        ownership_probe=lambda: coord.owns(lease_b),
        fence_token_provider=lambda: coord.fence_token(lease_b),
    )
    assert orch_b._persist_graph_checkpoint(stage="wave_b") is True
    store = SQLiteTaskGraphStore(run_dir / "task_graph.sqlite")
    fence_b = store.read_commit_fence(orch_b.run_id)
    assert fence_b is not None and fence_b["generation"] == 2
    assert fence_b["commit_seq"] == 1
    # 权威游标随联合提交落库（第二轮修正③）
    assert fence_b["cursor"]["fence_generation"] == 2
    assert fence_b["cursor"]["last_commit_seq"] == 1

    # A 醒来迟到写入：联合提交在权威事务内拒绝（generation/租约双重不匹配）
    assert orch_a._persist_graph_checkpoint(stage="late_write") is False
    assert orch_a._ownership_lost is True
    assert sink_a == []  # A 从未走到确认引用
    assert store.read_commit_fence(orch_a.run_id) is None  # A 未落任何围栏行
    events_a = [event["event_type"] for event in orch_a.trace_recorder.events]
    assert "orchestrator.persistence.fenced" in events_a

    # B 的联合提交与运行状态未被 A 触碰
    fence_b_after = store.read_commit_fence(orch_b.run_id)
    assert fence_b_after["generation"] == 2 and fence_b_after["commit_seq"] == 1
    state = _json.loads((run_dir / "runtime_state.json").read_text(encoding="utf-8"))
    assert state["fence_generation"] == 2
    assert state["last_commit_seq"] == 1
    assert coord.last_checkpoint()["commit_seq"] == 1


def test_db_cursor_rebuild_neutralizes_zombie_json_overwrite(tmp_path):
    """第二轮修正③复现：json 文件替换不在联合事务保护内——旧执行者
    探测成功后停顿、待 B 接管写入后再覆盖 json 缓存。恢复以联合提交内
    的游标载荷为准重建，被覆盖的缓存无害（仅展示层）。"""
    import json as _json

    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

    clock = Clock()
    run_dir = tmp_path / "run"
    coord = _coord(run_dir, clock, run_id="run")
    lease_b = coord.acquire("owner-B")
    orch_b = _make_orchestrator(
        run_dir,
        run_id="graph-b",
        ownership_probe=lambda: coord.owns(lease_b),
        fence_token_provider=lambda: coord.fence_token(lease_b),
    )
    orch_b.runtime_state_manager.state.iteration = 12
    assert orch_b._persist_graph_checkpoint(stage="wave_b") is True

    # 僵尸 A：缓存探针恒真（探测后停顿），文件 replace 绕过联合事务
    orch_a = _make_orchestrator(
        run_dir,
        run_id="graph-b",  # 同一 run 的旧执行者
        ownership_probe=lambda: True,
    )
    orch_a.runtime_state_manager.state.iteration = 999  # 伪造的陈旧游标
    orch_a.runtime_state_manager.write_fence = lambda: True
    orch_a.runtime_state_manager.persist()
    cache = _json.loads((run_dir / "runtime_state.json").read_text(encoding="utf-8"))
    assert cache["iteration"] == 999  # 缓存确实被覆盖（不受联合事务保护）

    # 恢复：DB 联合提交载荷重建权威游标，不信任缓存文件
    fence_row = SQLiteTaskGraphStore(
        run_dir / "task_graph.sqlite"
    ).read_commit_fence("graph-b")
    assert fence_row["cursor"]["iteration"] == 12
    orch_r = _make_orchestrator(run_dir, run_id="graph-b")
    assert orch_r.runtime_state_manager.state.iteration == 12  # 初始化即排除污染缓存
    assert orch_r.runtime_state_manager.restore_from_payload(fence_row["cursor"]) is True
    assert orch_r.runtime_state_manager.state.iteration == 12  # 权威游标回来了
    assert orch_r.runtime_state_manager.state.last_commit_seq == 1
    assert orch_r.runtime_state_manager.state.fence_generation == 1


def test_restore_from_payload_rejects_foreign_run_id(tmp_path):
    from AstralNexus.core.runtime_state import RuntimeStateManager

    manager = RuntimeStateManager(tmp_path, "run-1")
    assert manager.restore_from_payload({"run_id": "other-run", "iteration": 7}) is False
    assert manager.state.iteration == 0
    assert manager.restore_from_payload("not-a-dict") is False
    assert manager.restore_from_payload({"run_id": "run-1", "iteration": 7}) is True
    assert manager.state.iteration == 7


def test_user_resume_records_audit_and_preserves_breaker_evidence(tmp_path):
    """人工恢复审计：授权来源、原停机码、代数变迁落库；恢复只授权再次
    运行——熔断依据（stop_code/stop_reason）原样保留，不代表验收通过。"""
    clock = Clock()
    coord = _coord(tmp_path, clock)
    lease = coord.acquire("owner-A")
    assert coord.finish(
        lease, "blocked",
        stop_reason="same autonomous recovery outcome repeated three times",
        stop_code="no_progress_breaker",
    ) is True
    resumed = coord.acquire(
        "operator-B", user_resume=True, authorization_source="suite:--user-resume",
    )
    assert resumed is not None and resumed.generation == 2
    audit = coord.last_resume_audit()
    assert audit is not None
    assert audit["authorized_by"] == "suite:--user-resume"
    assert audit["source_stop_code"] == "no_progress_breaker"
    assert audit["source_stop_reason"] == (
        "same autonomous recovery outcome repeated three times"
    )
    assert audit["source_generation"] == 1
    assert audit["new_generation"] == 2
    row = coord.current()
    assert row["stop_code"] == "no_progress_breaker"  # 熔断依据未被清除
    assert row["stop_reason"] == (
        "same autonomous recovery outcome repeated three times"
    )
    assert row["status"] == "running"


def test_user_resume_from_pause_records_source_status(tmp_path):
    clock = Clock()
    coord = _coord(tmp_path, clock)
    coord.acquire("owner-A")
    assert coord.request_pause() == "paused"
    clock.advance(600)
    resumed = coord.acquire(
        "operator-B", user_resume=True, authorization_source="cli:--user-resume",
    )
    assert resumed is not None
    audit = coord.last_resume_audit()
    assert audit["source_status"] == "paused"
    assert audit["source_paused_by_user"] is True
    assert audit["authorized_by"] == "cli:--user-resume"


def test_read_authoritative_cursor_prefers_commit_fence(tmp_path):
    """展示层权威读取入口：优先联合提交 DB 载荷；被僵尸覆盖的 json 缓存
    不作为来源（"覆盖无害"限定为不影响权威恢复与正确的展示读取）。"""
    import json as _json

    from AstralNexus.core.runtime_state import read_authoritative_cursor

    clock = Clock()
    run_dir = tmp_path / "run"
    coord = _coord(run_dir, clock, run_id="run")
    lease_b = coord.acquire("owner-B")
    orch_b = _make_orchestrator(
        run_dir,
        run_id="graph-b",
        ownership_probe=lambda: coord.owns(lease_b),
        fence_token_provider=lambda: coord.fence_token(lease_b),
    )
    orch_b.runtime_state_manager.state.iteration = 12
    assert orch_b._persist_graph_checkpoint(stage="wave_b") is True

    # 僵尸覆盖 json 缓存
    cache_path = run_dir / "runtime_state.json"
    payload = _json.loads(cache_path.read_text(encoding="utf-8"))
    payload["iteration"] = 999
    cache_path.write_text(_json.dumps(payload), encoding="utf-8")

    view = read_authoritative_cursor(run_dir)
    assert view is not None
    assert view["source"] == "commit_fence"
    assert view["cursor"]["iteration"] == 12
    assert view["commit_seq"] == 1


def test_joint_checkpoint_refreshes_cursor_from_current_graph(tmp_path):
    """The graph and its status projection must describe the same commit."""
    from AstralNexus.core.runtime_state import read_authoritative_cursor

    clock = Clock()
    run_dir = tmp_path / "run"
    coord = _coord(run_dir, clock, run_id="run")
    lease = coord.acquire("owner-A")
    orch = _make_orchestrator(
        run_dir,
        run_id="graph-current",
        ownership_probe=lambda: coord.owns(lease),
        fence_token_provider=lambda: coord.fence_token(lease),
    )
    orch.task_graph.add_node({
        "node_id": "done", "goal": "done", "role": "coder", "status": "completed",
    })
    orch.task_graph.add_node({
        "node_id": "active", "goal": "active", "role": "coder", "status": "running",
    })

    # Simulate the scheduler projection from before the current wave transition.
    state = orch.runtime_state_manager.state
    state.node_count = 1
    state.nodes_by_status = {"ready": 1}
    state.ready_nodes = ["active"]
    state.active_agents = []

    assert orch._persist_graph_checkpoint(stage="worker_result_processed") is True
    view = read_authoritative_cursor(run_dir, run_id="graph-current")
    assert view is not None and view["source"] == "commit_fence"
    cursor = view["cursor"]
    assert cursor["node_count"] == 2
    assert cursor["nodes_by_status"] == {"completed": 1, "running": 1}
    assert cursor["ready_nodes"] == []
    assert cursor["progress_fingerprint"] != ""


def test_read_authoritative_cursor_json_fallback_warns(tmp_path):
    import json as _json

    from AstralNexus.core.runtime_state import read_authoritative_cursor

    run_dir = tmp_path / "legacy"
    run_dir.mkdir()
    (run_dir / "runtime_state.json").write_text(
        _json.dumps({"run_id": "r1", "iteration": 3}), encoding="utf-8",
    )
    view = read_authoritative_cursor(run_dir)
    assert view["source"] == "json_cache"
    assert "迟到写入" in view["warning"]
    assert read_authoritative_cursor(tmp_path / "absent") is None


def _fenced_run_for_cursor_tests(tmp_path):
    """构造一个有权威联合提交游标的 run（iteration=12, commit_seq=1）。"""
    clock = Clock()
    run_dir = tmp_path / "run"
    coord = _coord(run_dir, clock, run_id="run")
    lease_b = coord.acquire("owner-B")
    orch_b = _make_orchestrator(
        run_dir,
        run_id="graph-b",
        ownership_probe=lambda: coord.owns(lease_b),
        fence_token_provider=lambda: coord.fence_token(lease_b),
    )
    orch_b.runtime_state_manager.state.iteration = 12
    assert orch_b._persist_graph_checkpoint(stage="wave_b") is True
    return run_dir


def test_authoritative_cursor_never_lets_cache_decide(tmp_path):
    """第五轮修正（用户独立复现反转）：权威游标定位不依赖缓存——
    缓存缺失、损坏、错误 run_id、虚高序号四类异常都不改变来源选择；
    有效 DB 游标始终被使用，缓存异议只记入 anomalies。"""
    import json as _json

    from AstralNexus.core.runtime_state import read_authoritative_cursor

    run_dir = _fenced_run_for_cursor_tests(tmp_path)
    cache_path = run_dir / "runtime_state.json"

    # 基线：缓存与权威一致；显式 run_id 同样直达 DB
    view = read_authoritative_cursor(run_dir)
    assert view["source"] == "commit_fence" and view["cursor"]["iteration"] == 12
    assert read_authoritative_cursor(run_dir, run_id="graph-b")["source"] == "commit_fence"

    # ① 缓存缺失（用户复现：此前返回 None）
    cache_path.unlink()
    view = read_authoritative_cursor(run_dir)
    assert view["source"] == "commit_fence"
    assert view["cursor"]["iteration"] == 12
    assert "json_cache_missing" in view["anomalies"]

    # ② 缓存损坏
    cache_path.write_text("not-json{{{", encoding="utf-8")
    view = read_authoritative_cursor(run_dir)
    assert view["source"] == "commit_fence" and view["cursor"]["iteration"] == 12
    assert "json_cache_unreadable" in view["anomalies"]

    # ③ 缓存错误 run_id
    cache_path.write_text(
        _json.dumps({"run_id": "other-run", "iteration": 999, "last_commit_seq": 5}),
        encoding="utf-8",
    )
    view = read_authoritative_cursor(run_dir)
    assert view["source"] == "commit_fence" and view["cursor"]["iteration"] == 12
    assert "cache_run_id_mismatch" in view["anomalies"]
    assert "cache_seq_ahead_of_authoritative" in view["anomalies"]  # 5 > 1

    # ④ 缓存虚高序号（用户复现：999 曾劫持来源选择）
    cache_path.write_text(
        _json.dumps({"run_id": "graph-b", "iteration": 999, "last_commit_seq": 999}),
        encoding="utf-8",
    )
    view = read_authoritative_cursor(run_dir)
    assert view["source"] == "commit_fence"
    assert view["cursor"]["iteration"] == 12  # 不是 999
    assert view["commit_seq"] == 1
    assert "cache_seq_ahead_of_authoritative" in view["anomalies"]


def test_authoritative_cursor_db_failure_is_not_no_commit(tmp_path):
    """数据库读取失败不得等同于"没有权威提交"，也不得静默回退缓存。"""
    import json as _json

    from AstralNexus.core.runtime_state import read_authoritative_cursor

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "task_graph.sqlite").write_bytes(b"this is not a sqlite database")
    (run_dir / "runtime_state.json").write_text(
        _json.dumps({"run_id": "r1", "iteration": 3}), encoding="utf-8",
    )
    view = read_authoritative_cursor(run_dir)
    assert view is not None
    assert view["source"] == "db_read_failed"
    assert "不能等同于没有权威提交" in view["warning"]


def test_resume_rebuilds_from_db_cursor_despite_poisoned_cache(tmp_path):
    """第五轮修正（CLI 恢复路径）：json 缓存虚高序号不得否决权威 DB
    游标重建——异议只记 resume.cursor_anomaly 异常。整链走统一入口
    _resume_task（含准入、心跳、围栏终态写入）。"""
    import json as _json
    from types import SimpleNamespace

    from AstralNexus.cli.main import _resume_task

    run_dir = tmp_path / "run"
    clock = Clock()
    coord = _coord(run_dir, clock, run_id="run")
    lease_b = coord.acquire("owner-B")
    orch_b = _make_orchestrator(
        run_dir,
        run_id="graph-b",
        ownership_probe=lambda: coord.owns(lease_b),
        fence_token_provider=lambda: coord.fence_token(lease_b),
    )
    orch_b.task_graph.add_node({
        "node_id": "T-1", "goal": "small service", "role": "coder",
        "status": "completed",
    })
    # 治理契约齐备：恢复路径不得触发交付契约再生成（离线测试无 LLM）。
    from AstralNexus.core.governor import GoalSpec

    orch_b.governor.goal_spec = GoalSpec(
        goal_id="G1", task_goal="small service",
        normalized_goal="small service", hard_constraints=[],
    )
    orch_b.governor.delivery_contract = {
        "required_outputs": [], "execution_evidence_policy": "runtime_required",
    }
    orch_b.governor.goal_version = 1
    orch_b.runtime_state_manager.state.iteration = 12
    assert orch_b._persist_graph_checkpoint(stage="wave_b") is True

    # B 崩溃：租约过期后僵尸覆盖缓存并虚报序号
    clock.advance(600)
    cache_path = run_dir / "runtime_state.json"
    payload = _json.loads(cache_path.read_text(encoding="utf-8"))
    payload["iteration"] = 999
    payload["last_commit_seq"] = 999
    cache_path.write_text(_json.dumps(payload), encoding="utf-8")

    task_file = tmp_path / "task.txt"
    task_file.write_text("continue the service work", encoding="utf-8")
    captured = {}
    anomalies = []

    def on_orch(orchestrator, _run_dir):
        captured["manager"] = orchestrator.runtime_state_manager
        orchestrator.run = lambda *a, **kw: {
            "status": "completed", "iterations": 13, "stop_reason": "",
        }
        original_record = orchestrator.trace_recorder.record

        def record(event_type, payload=None, **kwargs):
            if event_type == "resume.cursor_anomaly":
                anomalies.append(payload)
            return original_record(event_type, payload, **kwargs)

        orchestrator.trace_recorder.record = record

    args = SimpleNamespace(
        run_dir=str(run_dir), task=str(task_file), output=str(run_dir),
        model="test-model", provider="test", max_parallel=1, config=None,
        user_resume=False,
    )
    code = _resume_task(args, tmp_path / "home", on_orchestrator=on_orch)
    assert code == 0  # No new outputs: presentation is not required.
    state = captured["manager"].state
    assert state.iteration == 12  # 权威 DB 游标，不是缓存的 999
    assert state.last_commit_seq >= 1
    assert anomalies and anomalies[0]["anomaly"] == "cache_seq_ahead_of_authoritative"
    assert anomalies[0]["cache_seq"] == 999
    assert coord.current()["status"] == "completed"
    assert coord.current()["stop_code"] == "completed"


def test_host_exception_finishes_blocked_and_stays_resumable(tmp_path, monkeypatch):
    """实验 1a 失败反转（2026-09-14 网络瞬断事故）：宿主异常（网络中断/
    提供方熔断）必须落 blocked+host_exception（自动可恢复类），不得落
    终态 failed——终态永不重新准入，一次瞬断会把 run 永久判死。"""
    from types import SimpleNamespace

    from AstralNexus.cli import main as cli_main
    from AstralNexus.runtime.run_coordinator import RunCoordinator

    run_dir = tmp_path / "run"
    task_file = tmp_path / "task.txt"
    task_file.write_text("implement a small inspection service", encoding="utf-8")

    class FakeTrace:
        def record(self, *args, **kwargs):
            return None

    class FakeGovernor:
        def derive_delivery_contract(self, *args, **kwargs):
            return None

    class FakeOrchestrator:
        def attach_interrupt_control(self, event, queue):
            self.interrupt_event = event

        def shutdown(self):
            return None

        def __init__(self, **kwargs):
            from AstralNexus.core.task_graph import TaskGraph
            self.task_graph = TaskGraph()
            self.checkpoint_sink = kwargs.get('checkpoint_sink')
            self.trace_recorder = FakeTrace()
            self.governor = FakeGovernor()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def enable_real_llm_worker_matching(self, config):
            return None

        def register_staged_inputs(self, staged):
            return None

        def run(self, **kwargs):
            raise RuntimeError("provider circuit open after 3 retries")

    monkeypatch.setattr(cli_main, "Orchestrator", FakeOrchestrator)
    args = SimpleNamespace(
        task=str(task_file), output=str(run_dir), run_dir=str(run_dir),
        model="test-model", provider="test", max_parallel=1, config=None,
    )
    code = cli_main._run_task(args, tmp_path / "home")
    assert code == 1
    coordinator = RunCoordinator(run_dir, run_id=run_dir.name)
    row = coordinator.current()
    assert row["status"] == "blocked"
    assert row["stop_code"] == "host_exception"
    # 崩溃类停机码在自动可恢复白名单内：网络恢复后普通 --resume 即可再接管
    assert coordinator.acquire("monitor-auto") is not None
