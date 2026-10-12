"""Durable host services, independent of browser connections and Agent runs.

DSH-inspired lifecycle contracts, implemented against the existing execution
ledger. A workflow is an explicit command DAG, not another model orchestrator.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Iterator
import uuid

from .execution_store import ExecutionStore


class HostLock:
    """OS-owned lock: process death releases it, PID reuse cannot steal it."""

    def __init__(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        self.file = (home / 'host.lock').open('a+b')
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('A workbench host already owns this home') from None

    def close(self) -> None:
        self.file.close()


def validate_workflow(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate the entire graph before persisting or executing any command."""
    steps = spec.get('steps')
    if not isinstance(steps, list) or not steps or len(steps) > 100:
        raise ValueError('workflow requires 1..100 steps')
    seen: set[str] = set()
    normalized = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('step must be an object')
        ident = step.get('id')
        command = step.get('command')
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError('step ids must be non-empty and unique')
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and '\x00' not in x for x in command):
            raise ValueError('command must be a non-empty argv array')
        dependencies = step.get('depends_on', [])
        if not isinstance(dependencies, list) or not all(isinstance(x, str) and x in seen for x in dependencies):
            raise ValueError('dependencies must refer to earlier steps (acyclic order)')
        timeout = float(step.get('timeout', 120))
        if not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('timeout must be within (0, 3600]')
        normalized.append({'id': ident, 'command': command, 'depends_on': dependencies, 'timeout': timeout})
        seen.add(ident)
    return {'name': str(spec.get('name') or 'Workflow')[:200], 'steps': normalized}


class WorkbenchStore:
    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'workbench.sqlite'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,workspace TEXT,run_dir TEXT);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session_id TEXT,spec TEXT,status TEXT,
                    result TEXT,created REAL,cancel INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,session_id TEXT,spec TEXT,
                    due REAL,interval REAL,enabled INTEGER);
                CREATE TABLE IF NOT EXISTS deliveries(schedule_id TEXT,scheduled_at REAL,job_id TEXT,
                    PRIMARY KEY(schedule_id,scheduled_at));
            ''')

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register_session(self, title: str, workspace: str, run_dir: str = '') -> str:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('workspace must be an existing directory')
        run = Path(run_dir).expanduser().resolve() if run_dir else None
        if run is not None and not run.is_dir():
            raise ValueError('run_dir must be an existing directory')
        ident = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (ident, title.strip() or root.name, str(root), str(run or '')))
        return ident

    def session(self, ident: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('unknown session')
        return dict(row)

    def select(self, ident: str) -> None:
        self.session(ident)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('selected', ident))

    def snapshot(self) -> dict[str, Any]:
        with self.db() as db:
            selected = db.execute("SELECT value FROM settings WHERE key='selected'").fetchone()
            return {'selected': selected[0] if selected else None,
                    'sessions': [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY rowid')],
                    'jobs': [{**dict(r), 'result': json.loads(r['result']) if r['result'] else None}
                             for r in db.execute('SELECT id,session_id,status,result,created FROM jobs ORDER BY created DESC LIMIT 50')],
                    'schedules': [dict(r) for r in db.execute('SELECT id,session_id,due,interval,enabled FROM schedules')]}

    def submit(self, session_id: str, spec: dict[str, Any]) -> str:
        self.session(session_id)
        normalized = validate_workflow(spec)
        ident = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,0)',
                       (ident, session_id, json.dumps(normalized), 'queued', None, time.time()))
        return ident

    def schedule(self, session_id: str, spec: dict[str, Any], due: float, interval: float = 0) -> str:
        self.session(session_id)
        normalized = validate_workflow(spec)
        if not math.isfinite(due) or not math.isfinite(interval) or due <= 0 or interval < 0 or 0 < interval < 60:
            raise ValueError('due must be finite UTC seconds; interval is 0 or >=60 seconds')
        ident = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,1)',
                       (ident, session_id, json.dumps(normalized), due, interval))
        return ident

    def tick(self, now: float | None = None) -> int:
        """Atomically enqueue the latest missed occurrence, never a catch-up storm."""
        now = time.time() if now is None else now
        count = 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT * FROM schedules WHERE enabled=1 AND due<=?', (now,)).fetchall():
                interval = row['interval']
                occurrence = row['due'] + math.floor((now - row['due']) / interval) * interval if interval else row['due']
                ident = uuid.uuid4().hex
                inserted = db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)', (row['id'], occurrence, ident)).rowcount
                if inserted:
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,0)',
                               (ident, row['session_id'], row['spec'], 'queued', None, now))
                    count += 1
                db.execute('UPDATE schedules SET due=?,enabled=? WHERE id=?',
                           (occurrence + interval, int(bool(interval)), row['id']))
        return count

    def cancel(self, ident: str) -> None:
        with self.db() as db:
            db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?", (ident,))

    def cancelled(self, ident: str) -> bool:
        with self.db() as db:
            row = db.execute('SELECT cancel FROM jobs WHERE id=?', (ident,)).fetchone()
        return bool(row and row[0])

    def enable_schedule(self, ident: str, enabled: bool) -> None:
        with self.db() as db:
            if not db.execute('UPDATE schedules SET enabled=? WHERE id=?', (int(enabled), ident)).rowcount:
                raise ValueError('unknown schedule')


def stop_process(process: subprocess.Popen) -> None:
    from AstralNexus.runtime.process_control import stop_process_tree
    stop_process_tree(process)


class WorkflowEngine:
    def __init__(self, store: WorkbenchStore):
        self.store = store
        self.stop = threading.Event()

    def recover_interrupted(self) -> None:
        # A crashed process may already have performed side effects. Do not replay.
        with self.store.db() as db:
            rows = db.execute("SELECT id,result FROM jobs WHERE status='running'").fetchall()
        from .job_ownership import JobOwnership
        from .execution_fence import ExecutionOwnershipLost
        for row in rows:
            evidence = self.store.home / 'jobs' / row['id']
            try:
                with JobOwnership(evidence) as owner:
                    outcome = json.loads(row['result']) if row['result'] else {}
                    outcome['reason'] = 'host_restart_execution_outcome_unknown'
                    if outcome.get('execution_id'):
                        ledger = ExecutionStore(evidence, fence_token_provider=owner.fence)
                        ledger.interrupt_open(outcome['execution_id'])
                    with owner.job_db(self.store) as db:
                        db.execute("UPDATE jobs SET status='interrupted',result=? WHERE id=? AND status='running'",
                                   (json.dumps(outcome), row['id']))
                    owner.finish('blocked', 'host_exception')
            except ExecutionOwnershipLost:
                continue  # A live host's workflow is not an orphan.

    def run_once(self) -> bool:
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return False
            job = dict(row)
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
        from .job_ownership import JobOwnership
        from .execution_fence import ExecutionOwnershipLost
        evidence = self.store.home / 'jobs' / job['id']
        try:
            with JobOwnership(evidence) as owner:
                return self._execute_job(job, owner)
        except ExecutionOwnershipLost:
            return True  # Old host cannot finalize the new owner's job or receipts.

    def _execute_job(self, job: dict[str, Any], owner) -> bool:
        from .execution_fence import ExecutionOwnershipLost, guard_execution_fence
        results: list[dict[str, Any]] = []
        status = 'failed'
        ledger = None
        execution_id = None
        outcome: dict[str, Any]
        try:
            session = self.store.session(job['session_id'])
            workspace = Path(session['workspace'])
            # Isolated ledger, so workflow host never mutates an Agent run's graph.
            evidence = self.store.home / 'jobs' / job['id']
            evidence.mkdir(parents=True, exist_ok=True)
            ledger = ExecutionStore(evidence, fence_token_provider=owner.fence)
            execution_id = ledger.begin(job['id'], {'owned_paths': [], 'acceptance_criteria': [], 'workspace': str(workspace)})
            with owner.job_db(self.store) as db:
                db.execute('UPDATE jobs SET result=? WHERE id=?',
                           (json.dumps({'execution_id': execution_id, 'steps': []}), job['id']))
            status = 'succeeded'
            for step in json.loads(job['spec'])['steps']:
                if self.stop.is_set() or self.store.cancelled(job['id']):
                    status = 'cancelled'
                    break
                log = evidence / f'{len(results)}.log'
                started = time.time()
                reason = ''
                with log.open('wb') as out:
                    # SW_HIDE startupinfo instead of CREATE_NO_WINDOW: the
                    # creation-flag shape dies with 0xC0000142 (DLL init) for
                    # nested spawns under this host's sandbox — the shape the
                    # subprocess-terminal probe round proved out.
                    startup: Any = None
                    if os.name == 'nt':
                        startup = subprocess.STARTUPINFO()
                        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startup.wShowWindow = subprocess.SW_HIDE
                    guard_execution_fence(owner.root, owner.fence)
                    process = subprocess.Popen(step['command'], cwd=workspace, stdout=out, stderr=subprocess.STDOUT,
                        env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'},
                        startupinfo=startup)
                    try:
                        deadline = time.monotonic() + step['timeout']
                        while process.poll() is None:
                            guard_execution_fence(owner.root, owner.fence)
                            if self.stop.is_set() or self.store.cancelled(job['id']):
                                reason = 'cancelled'
                                break
                            if time.monotonic() >= deadline:
                                reason = 'timeout'
                                break
                            self.stop.wait(0.05)
                    finally:
                        if process.poll() is None:
                            stop_process(process)
                result = {'step': step['id'], 'command': step['command'], 'returncode': process.returncode,
                          'reason': reason, 'log': str(log), 'started': started, 'finished': time.time()}
                receipt = ledger.append(execution_id, result)
                results.append({**result, 'receipt_id': receipt['receipt_id']})
                with owner.job_db(self.store) as db:
                    db.execute('UPDATE jobs SET result=? WHERE id=?',
                               (json.dumps({'execution_id': execution_id, 'steps': results}), job['id']))
                if reason or process.returncode != 0:
                    status = 'cancelled' if reason == 'cancelled' else 'failed'
                    break  # downstream steps never run on failed prerequisites
            ledger.finish(execution_id, 'interrupted' if status == 'cancelled' else status, {'steps': results})
            outcome = {'execution_id': execution_id, 'steps': results, 'acceptance': 'not_evaluated'}
        except ExecutionOwnershipLost:
            raise
        except Exception as exc:
            status = 'failed'
            outcome = {'error': str(exc), 'steps': results, 'execution_id': execution_id, 'acceptance': 'not_evaluated'}
            if ledger is not None and execution_id is not None:
                if ledger.get(execution_id)['status'] == 'running':
                    ledger.finish(execution_id, 'failed', outcome)
        with owner.job_db(self.store) as db:
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, json.dumps(outcome), job['id']))
        owner.finish('completed' if status == 'succeeded' else 'blocked' if status == 'cancelled' else 'failed', 'workflow_' + status)
        return True

    def serve(self) -> None:
        # Startup recovery rides the same transient-lock retry as the loop below: a busy
        # database during recovery must not kill the host before it ever serves a job.
        recovered = False
        while not self.stop.is_set():
            try:
                if not recovered:
                    self.recover_interrupted()
                    recovered = True
                self.store.tick()
                if not self.run_once():
                    self.stop.wait(0.25)
            except sqlite3.OperationalError as exc:
                if 'lock' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise
                print('dsh-workbench: engine loop retried after %s' % exc, file=sys.stderr)
                self.stop.wait(0.25)
