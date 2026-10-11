"""Unified run entry: identity, ownership lease, cancel, fenced terminal writes.

Doc 17 第三批 (DeerFlow ``runtime/runs/manager.py`` RunManager contract port,
commit 2814bd5d498a, MIT — reference copy under
02_运行产物/99_临时文件/deerflow_runtime_reference/). Pure contract/algorithm
extraction: synchronous SQLite conditional updates instead of the async store
retry layer; no LangGraph. CLI, web, test suite and monitor are meant to go
through this single host boundary so "双管理器互相干扰" and "进程已退出、状态
仍 running" become structurally impossible.

Contract (per plan §可移植的契约 1):
- ``RunLease``: run_id, owner_id, generation, expires_at. Admission and
  takeover are conditional updates fenced by owner+generation; losing
  ownership forbids every later terminal/checkpoint write (renew returns
  None, finish/record_checkpoint return False) — not merely a PID check.
- Orphan recovery: an expired lease (plus grace) may be taken over; the
  takeover bumps ``generation`` so the dead owner's late writes are rejected.
- Cancel: lease valid elsewhere -> persist a cancel request the owner
  observes at its next boundary; lease expired -> take over and mark
  cancelled; halted (blocked/paused, no live owner) -> cancel lands directly.
  Terminal runs (completed/failed/cancelled) are never re-acquired.
- Blocked/paused are HALTED, not terminal: blocked runs whose stop code is in
  ``AUTO_RECOVERABLE_STOP_CODES`` may be reacquired by ordinary ``--resume``;
  user pauses and other manual-review codes still require explicit authorization.

Local modifications vs upstream (recorded per plan §复用方式):
- ``paused_by_user`` survives lease expiry and blocks every automatic
  acquisition (monitor auto-restart after a user pause is structurally
  rejected); only an explicit ``user_resume=True`` may reacquire.
- ``last_checkpoint`` gives recovery a SHARED version reference (graph
  revision / phase / iteration) instead of stitching arbitrary latest
  states from two databases; the write is ownership-fenced like terminals.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

ACTIVE_STATUSES = frozenset({"pending", "running"})
#: Never reacquired, never re-cancelled.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
#: Halted awaiting recovery. Split policy (doc 17 第三批 gap③):
#: ``blocked`` halted because the RUN failed — automatic recovery is allowed
#: (crash/failure restart is exactly the successor-takeover path);
#: ``paused`` halted because the USER stopped it — only an explicit
#: ``user_resume=True`` may reacquire, so a monitor auto-passing --resume can
#: never restart a user pause.
HALTED_STATUSES = frozenset({"blocked", "paused"})
FINISHABLE_STATUSES = TERMINAL_STATUSES | HALTED_STATUSES

#: 闭合枚举（doc 17 第三批第二轮审阅修正①）：自动恢复只放行明确的
#: 崩溃/中断类停机码；未知、缺失或任何不在集合内的码默认要求人工核查。
#: 关键词匹配已被证明会漏判真实的无进展熔断文案（用户复核：
#: "same autonomous recovery outcome repeated three times"、
#: "delivery completion made no progress three times"），故弃用。
#: stop_code 是机器码，stop_reason 只是展示文字，两者分开持久化。
#: ``verification_unavailable`` is recoverable: no verdict/evidence failure was
#: recorded, and a later ordinary resume may retry against a recovered provider.
AUTO_RECOVERABLE_STOP_CODES = frozenset({
    "worker_crash",
    "process_crash",
    "host_exception",
    "user_interrupt",
    "host_interrupted",
    "lease_orphaned",
    "verification_unavailable",
})


def requires_user_resume(status: str, stop_code: str) -> bool:
    """paused 永远需要用户；blocked 仅白名单自动恢复，未知默认核查。"""
    if status == "paused":
        return True
    if status != "blocked":
        return False
    return str(stop_code or "").strip() not in AUTO_RECOVERABLE_STOP_CODES


def reconcile_commit_identities(
    graph_seq: int, state_seq: int, reference_seq: int,
) -> str:
    """恢复对账协议（doc 17 第三批 gap③，第二轮修正②：完整检查不变量）。

    写入次序不变量 S_graph >= S_state >= S_ref（联合提交先落，游标次之，
    确认引用最后）。三段全部显式检查——只查 S_ref <= S_graph 会漏掉
    graph=10, state=8, ref=9 这种违反 S_state >= S_ref 的撕裂组合（用户
    复核发现）。

    返回：
    - "clean": 三者一致，干净恢复点（legacy 全 0 也走此分支）；
    - "torn_window_continue": 图已联合提交、游标/确认未追上（崩溃发生在
      两次持久化之间）——任务图存储为权威，按显式协议续跑；
    - "reject_invalid_seq": 负数等非法序号；
    - "reject_state_ahead": 游标宣称图从未联合提交的序号；
    - "reject_reference_ahead_of_state": 确认引用超前于游标；
    后三者 = 库间撕裂或外部改写，拒绝自动恢复，交人工核查。
    """
    if min(graph_seq, state_seq, reference_seq) < 0:
        return "reject_invalid_seq"
    if state_seq > graph_seq:
        return "reject_state_ahead"
    if reference_seq > state_seq:
        return "reject_reference_ahead_of_state"
    if graph_seq > state_seq or state_seq > reference_seq:
        return "torn_window_continue"
    return "clean"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    owner_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    paused_by_user INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    stop_code TEXT,
    last_checkpoint TEXT,
    last_resume_audit TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class RunLease:
    run_id: str
    owner_id: str
    generation: int
    expires_at: datetime


class RunCoordinator:
    """Single admission/pause/resume/cancel boundary for one run directory."""

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        lease_seconds: int = 120,
        grace_seconds: int = 10,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.root = Path(root)
        self.run_id = str(run_id)
        self.lease_seconds = max(1, int(lease_seconds))
        self.grace_seconds = max(0, int(grace_seconds))
        self._now_fn = now_fn
        self.path = self.root / ".astralnexus_state" / "runs.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            for column in ("stop_code", "last_resume_audit"):
                try:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError:
                    pass  # column already present

    # --- internals -----------------------------------------------------

    def _now(self) -> datetime:
        return self._now_fn() if self._now_fn is not None else datetime.now(timezone.utc)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        # Rollback journal (not WAL): the graph store ATTACHes this database
        # into its commit transaction so generation verification and the
        # authoritative graph write share ONE cross-process commit boundary
        # (doc 17 第三批 gap②). SQLite multi-database atomic commit requires
        # rollback journals on both sides; this table is tiny and readers
        # tolerate the brief write locks (timeout=30).
        db.execute("PRAGMA journal_mode=DELETE")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(row)

    def _lease_expired(self, row: Dict[str, Any], now: datetime) -> bool:
        raw = row.get("lease_expires_at")
        if not raw:
            return True  # NULL lease = orphan / pre-ownership: reclaimable
        try:
            deadline = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return now > deadline + timedelta(seconds=self.grace_seconds)

    def _new_expiry(self, now: datetime) -> str:
        return (now + timedelta(seconds=self.lease_seconds)).isoformat()

    # --- public contract ------------------------------------------------

    def current(self) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
        result = self._row_to_dict(row)
        if result is not None:
            from AstralNexus.runtime.stop_causes import stop_category
            category = stop_category(str(result.get("stop_code") or ""))
            result["last_stop_category"] = category
            result["stop_category"] = category if result.get("status") in {"blocked", "paused", "failed"} else "none"
        return result

    def acquire(
        self,
        owner_id: str,
        *,
        user_resume: bool = False,
        authorization_source: str = "",
    ) -> Optional[RunLease]:
        """Conditional admission/takeover; None when another executor holds it.

        Rules: no row -> admit (generation 1); terminal row -> never
        re-acquired; paused_by_user -> only an explicit user_resume may
        reacquire (automatic monitors are structurally rejected); active row
        with an unexpired lease -> single executor, rejected; expired lease
        (plus grace) -> takeover with generation+1 so the presumed-dead
        owner's late writes are fenced.
        """
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_to_dict(
                    db.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
                )
                if row is None:
                    db.execute(
                        "INSERT INTO runs(run_id,status,owner_id,generation,lease_expires_at,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (self.run_id, "running", str(owner_id), 1, self._new_expiry(now),
                         now.isoformat()),
                    )
                    db.execute("COMMIT")
                    return RunLease(self.run_id, str(owner_id), 1,
                                    now + timedelta(seconds=self.lease_seconds))
                status = str(row.get("status") or "")
                if status in TERMINAL_STATUSES:
                    db.execute("COMMIT")
                    return None
                if status not in ACTIVE_STATUSES | HALTED_STATUSES:
                    db.execute("COMMIT")
                    return None  # unknown status: fail closed
                paused = status == "paused" or int(row.get("paused_by_user") or 0)
                if paused and not user_resume:
                    db.execute("COMMIT")
                    return None
                if (
                    status == "blocked"
                    and requires_user_resume(status, str(row.get("stop_code") or ""))
                    and not user_resume
                ):
                    # 熔断/无进展/版本撕裂/未知停机码：人工核查门槛，
                    # 自动恢复规则不得再次接管。
                    db.execute("COMMIT")
                    return None
                # Classified recoverable blocked runs fall through to takeover:
                # finish() cleared owner/lease, so no live executor exists.
                # User pauses and unknown/manual-review codes were rejected above.
                if status in ACTIVE_STATUSES and not self._lease_expired(row, now):
                    db.execute("COMMIT")
                    return None  # another live executor: 双启动只有一个执行者
                generation = int(row.get("generation") or 0) + 1
                # 人工恢复审计：授权来源、原停机码、代数变迁全部落库。
                # stop_code/stop_reason 原样保留——用户恢复只授权再次运行，
                # 不代表验收通过，也不自动清除熔断依据。
                audit_json = None
                if user_resume and (paused or status == "blocked"):
                    audit_json = json.dumps({
                        "authorized_by": str(authorization_source or owner_id),
                        "owner_id": str(owner_id),
                        "source_status": status,
                        "source_paused_by_user": bool(int(row.get("paused_by_user") or 0)),
                        "source_stop_code": str(row.get("stop_code") or ""),
                        "source_stop_reason": str(row.get("stop_reason") or ""),
                        "source_generation": int(row.get("generation") or 0),
                        "new_generation": generation,
                        "resumed_at": now.isoformat(),
                        "semantics": (
                            "user_resume 仅授权再次运行：不代表验收通过，"
                            "不自动清除熔断依据"
                        ),
                    }, ensure_ascii=False, sort_keys=True)
                # The generation fence is the concurrency contract: a racing
                # takeover bumps generation first and this UPDATE matches 0
                # rows (BEGIN IMMEDIATE serializes the read-check-write).
                # Fix V (doc 18, 2026-09-15 r13b): clear cancel_requested with
                # paused_by_user.  should_stop() treats the two flags identically,
                # but only paused_by_user was cleared here, so a run that was ever
                # cancelled was admitted by acquire() and then killed by its own
                # stale flag at the first lease heartbeat (lease_seconds//3).  The
                # stop was recorded as "user_interrupt", which re-armed the flag —
                # a self-perpetuating trap: live r13b generation 2 ran 36 minutes,
                # generation 3+ died after 33 seconds, every time, with nobody
                # touching the keyboard.  A cancel request targets the executor
                # generation that was running when it was made; this takeover is a
                # new generation with a new owner, and "user_interrupt" is in
                # AUTO_RECOVERABLE_STOP_CODES precisely so it may resume.  The
                # stricter paused_by_user still gates admission above.
                cursor = db.execute(
                    "UPDATE runs SET status='running', owner_id=?, generation=?,"
                    " lease_expires_at=?, paused_by_user=0, cancel_requested=0,"
                    " updated_at=?,"
                    " last_resume_audit=COALESCE(?, last_resume_audit)"
                    " WHERE run_id=? AND generation=?",
                    (str(owner_id), generation, self._new_expiry(now), now.isoformat(),
                     audit_json, self.run_id, int(row.get("generation") or 0)),
                )
                if cursor.rowcount != 1:
                    db.execute("ROLLBACK")
                    return None
                db.execute("COMMIT")
                return RunLease(self.run_id, str(owner_id), generation,
                                now + timedelta(seconds=self.lease_seconds))
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def owns(self, lease: RunLease) -> bool:
        """Fail-closed ownership probe for pre-write fences.

        True only for the current owner+generation on an active row with an
        unexpired lease. Used by authoritative writers (task graph / runtime
        state persistence) BEFORE touching durable state, so an executor that
        lost ownership never writes first and asks forgiveness later.
        """
        row = self.current()
        if row is None:
            return False
        if str(row.get("owner_id") or "") != lease.owner_id:
            return False
        if int(row.get("generation") or 0) != lease.generation:
            return False
        if str(row.get("status") or "") not in ACTIVE_STATUSES:
            return False
        expiry_raw = row.get("lease_expires_at")
        if not expiry_raw:
            return False
        try:
            expiry = datetime.fromisoformat(str(expiry_raw))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return self._now() <= expiry

    def fence_token(self, lease: RunLease) -> Dict[str, Any]:
        """Complete fencing token for the joint graph-store commit.

        ``now_iso`` comes from the coordinator clock at call time — writers
        must not supply their own timestamp, or a stale executor could pass
        the expiry check with an old now. The token is only meaningful when
        evaluated at write time (the joint transaction re-verifies it).
        """
        return {
            "runs_db_path": str(self.path),
            "run_id": self.run_id,
            "owner_id": lease.owner_id,
            "generation": lease.generation,
            "now_iso": self._now().isoformat(),
        }

    def renew(self, lease: RunLease) -> Optional[RunLease]:
        """Conditional renewal fenced by owner+generation+active status+expiry.

        None means ownership is LOST: the caller must stop and may not write
        any terminal state (upstream fencing discipline, ported). An expired
        lease cannot be renewed — fail closed at the confirmed deadline, the
        grace window exists only for the taker's safety margin.
        """
        now = self._now()
        expiry = self._new_expiry(now)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE runs SET lease_expires_at=?, updated_at=?"
                " WHERE run_id=? AND owner_id=? AND generation=?"
                " AND status IN ('pending','running')"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (expiry, now.isoformat(), lease.run_id, lease.owner_id,
                 lease.generation, now.isoformat()),
            )
            if cursor.rowcount != 1:
                return None
        return RunLease(lease.run_id, lease.owner_id, lease.generation,
                        now + timedelta(seconds=self.lease_seconds))

    def should_stop(self, lease: RunLease) -> bool:
        """Owner-side poll at loop boundaries: cancel or user pause."""
        row = self.current()
        if row is None:
            return True
        if int(row.get("cancel_requested") or 0) or int(row.get("paused_by_user") or 0):
            return True
        return str(row.get("owner_id") or "") != lease.owner_id or int(row.get("generation") or 0) != lease.generation

    def request_pause(self) -> str:
        """User pause: survives lease expiry and blocks automatic restarts."""
        now = self._now()
        with self._connect() as db:
            row = self._row_to_dict(
                db.execute("SELECT status FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            )
            if row is None:
                return "not_found"
            if str(row.get("status") or "") in TERMINAL_STATUSES:
                return "already_terminal"
            db.execute(
                "UPDATE runs SET paused_by_user=1, status='paused', updated_at=? WHERE run_id=?",
                (now.isoformat(), self.run_id),
            )
        return "paused"

    def request_cancel(self) -> str:
        """requested | taken_over | already_terminal | not_found (upstream semantics)."""
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_to_dict(
                    db.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
                )
                if row is None:
                    db.execute("COMMIT")
                    return "not_found"
                status = str(row.get("status") or "")
                if status in TERMINAL_STATUSES:
                    db.execute("COMMIT")
                    return "already_terminal"
                if status in HALTED_STATUSES:
                    # No live owner to observe a request: the user's cancel
                    # decision lands directly on the halted row.
                    db.execute(
                        "UPDATE runs SET status='cancelled', owner_id=NULL,"
                        " lease_expires_at=NULL, stop_reason='cancel_of_halted_run',"
                        " updated_at=? WHERE run_id=?",
                        (now.isoformat(), self.run_id),
                    )
                    db.execute("COMMIT")
                    return "taken_over"
                if status in ACTIVE_STATUSES and self._lease_expired(row, now):
                    db.execute(
                        "UPDATE runs SET status='cancelled', owner_id=NULL, lease_expires_at=NULL,"
                        " stop_reason='cancel_takeover_of_expired_lease', updated_at=?"
                        " WHERE run_id=? AND generation=?",
                        (now.isoformat(), self.run_id, int(row.get("generation") or 0)),
                    )
                    db.execute("COMMIT")
                    return "taken_over"
                db.execute(
                    "UPDATE runs SET cancel_requested=1, updated_at=? WHERE run_id=?",
                    (now.isoformat(), self.run_id),
                )
                db.execute("COMMIT")
                return "requested"
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def is_cancel_requested(self) -> bool:
        row = self.current()
        return bool(row and int(row.get("cancel_requested") or 0))

    def finish(
        self,
        lease: RunLease,
        status: str,
        *,
        stop_reason: str = "",
        stop_code: str = "",
    ) -> bool:
        """Fenced terminal write: only the current owner+generation with an
        UNEXPIRED lease may write.

        ``stop_code`` is the machine code from the closed enum (drives the
        automatic-recovery policy); ``stop_reason`` is display text only.

        False = ownership lost or lease expired; the caller must NOT retry
        elsewhere (an old manager's late completion write is rejected, not
        queued — 租约失效即禁止写入，含终态).
        """
        if status not in FINISHABLE_STATUSES:
            raise ValueError(f"not a finishable status: {status}")
        now = self._now()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE runs SET status=?, owner_id=NULL, lease_expires_at=NULL,"
                " stop_reason=?, stop_code=?, updated_at=?"
                " WHERE run_id=? AND owner_id=? AND generation=?"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (status, str(stop_reason)[:500], str(stop_code)[:100], now.isoformat(),
                 lease.run_id, lease.owner_id, lease.generation, now.isoformat()),
            )
            return cursor.rowcount == 1

    def record_checkpoint(self, lease: RunLease, checkpoint: Dict[str, Any]) -> bool:
        """Fenced shared-version reference for consistent recovery.

        Recovery reads this instead of stitching arbitrary latest states
        from the task-graph store and the execution store.
        """
        now = self._now()
        payload = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, default=str)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE runs SET last_checkpoint=?, updated_at=?"
                " WHERE run_id=? AND owner_id=? AND generation=?"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (payload, now.isoformat(), lease.run_id, lease.owner_id,
                 lease.generation, now.isoformat()),
            )
            return cursor.rowcount == 1

    def last_resume_audit(self) -> Optional[Dict[str, Any]]:
        """人工恢复审计记录（授权来源/原停机码/代数变迁），无则 None。"""
        row = self.current()
        if not row or not row.get("last_resume_audit"):
            return None
        try:
            parsed = json.loads(str(row["last_resume_audit"]))
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def last_checkpoint(self) -> Optional[Dict[str, Any]]:
        row = self.current()
        if not row or not row.get("last_checkpoint"):
            return None
        try:
            parsed = json.loads(str(row["last_checkpoint"]))
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
