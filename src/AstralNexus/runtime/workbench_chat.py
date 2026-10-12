"""Durable web conversations. Browser connections never own Agent lifetimes.

Each turn runs the existing CLI Governor/Agent in an isolated process. A single
host queue deliberately serializes work: cwd, approvals and tool registries are
process globals, and two conversations may target the same workspace.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from .workbench import WorkbenchStore, stop_process
from .turn_terminal import (
    HOST_EXCEPTION,
    ORPHAN_RECOVERED,
    ORPHAN_RECOVERY_ERROR,
    USER_CANCELLED,
    describe,
    terminal_for_worker_exit,
)
from .delivery_recovery import verify_published

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


class ChatService:
    # Injected by WorkbenchHost: the real plugin host whose gate plugins form
    # the host-wide floor for per-session toolset visibility. None in bare
    # test construction keeps the legacy per-session behavior unchanged.
    plugin_host: Any = None

    def __init__(self, store: WorkbenchStore, command: list[str] | None = None):
        self.store = store
        self.command = command or [sys.executable, '-u', '-m', 'AstralNexus.runtime.workbench_chat_worker']
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chat_turns(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    message TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    created REAL NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
                    run_dir TEXT NOT NULL DEFAULT '', UNIQUE(session_id,request_id));
                CREATE TABLE IF NOT EXISTS chat_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS chat_session_events ON chat_events(session_id,seq);
                CREATE TABLE IF NOT EXISTS chat_approvals(
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, request TEXT NOT NULL,
                    choice TEXT, delivered INTEGER NOT NULL DEFAULT 0);
            ''')
            if 'model_settings' not in {row[1] for row in db.execute('PRAGMA table_info(chat_turns)')}:
                db.execute("ALTER TABLE chat_turns ADD COLUMN model_settings TEXT NOT NULL DEFAULT '{}'")
            if 'plugin_settings' not in {row[1] for row in db.execute('PRAGMA table_info(chat_turns)')}:
                db.execute("ALTER TABLE chat_turns ADD COLUMN plugin_settings TEXT NOT NULL DEFAULT '[]'")
            # The row must be able to say WHY a turn ended, not only that it did.
            # Live 2026-10-07: the host died of page-file exhaustion, the run went
            # ``interrupted``, and the durable turn row still said ``running`` --
            # neither the host API nor the DB could explain that the run was over.
            # Same shape as DeerFlow ``runtime/runs/manager.py``, which carries a
            # named stop reason plus error text on the run record.
            for column, ddl in (
                ('stop_reason', "TEXT NOT NULL DEFAULT ''"),
                ('error', "TEXT NOT NULL DEFAULT ''"),
                ('goal_verdict', "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in {row[1] for row in db.execute('PRAGMA table_info(chat_turns)')}:
                    db.execute(f"ALTER TABLE chat_turns ADD COLUMN {column} {ddl}")
            # A host restart must not silently replay a paid or side-effecting turn.
            for row in db.execute("SELECT id,session_id FROM chat_turns WHERE status IN ('running','queued')").fetchall():
                db.execute(
                    "UPDATE chat_turns SET status='interrupted',stop_reason=?,error=? WHERE id=?",
                    (ORPHAN_RECOVERED, ORPHAN_RECOVERY_ERROR, row['id']),
                )
                db.execute('INSERT INTO chat_events(turn_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                           (row['id'], row['session_id'], 'turn.interrupted',
                            json.dumps({'text': ORPHAN_RECOVERY_ERROR,
                                        'stop_reason': ORPHAN_RECOVERED}, ensure_ascii=False),
                            time.time()))

    def plugins(self, session: str) -> dict[str, Any]:
        from AstralNexus.tools.registry import discover_builtin_tools, registry
        self.store.session(session)
        discover_builtin_tools()
        with self.store.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', ('plugins:' + session,)).fetchone()
        disabled = json.loads(row['value']) if row else []
        items = []
        for name in registry.get_registered_toolset_names():
            gate = self.plugin_host.toolset_gate(name) if self.plugin_host is not None else None
            host_active = gate is None or gate['phase'] == 'active'
            items.append({
                'id': name,
                'enabled': name not in disabled and host_active,
                'session_enabled': name not in disabled,
                'host_gate': gate,
                'tools': registry.get_tool_names_for_toolset(name),
            })
        return {'disabled': disabled, 'items': items}

    def set_plugins(self, session: str, disabled: Any) -> dict[str, Any]:
        known = {item['id'] for item in self.plugins(session)['items']}
        if not isinstance(disabled, list) or any(not isinstance(x, str) or x not in known for x in disabled):
            raise ValueError('unknown tool plugin')
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                       ('plugins:' + session, json.dumps(sorted(set(disabled)))))
        return self.plugins(session)

    def permissions(self, session: str) -> dict[str, str]:
        self.store.session(session)
        with self.store.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', ('permissions:' + session,)).fetchone()
        return {'mode': row['value'] if row else 'ask'}

    def set_permissions(self, session: str, mode: str) -> dict[str, str]:
        self.store.session(session)
        if mode not in {'ask', 'assist', 'full'}:
            raise ValueError('unknown permission mode')
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('permissions:' + session, mode))
        return self.permissions(session)

    def apply_permissions(self, session: str) -> None:
        workspace = Path(self.store.session(session)['workspace']).resolve()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            setting = db.execute('SELECT value FROM settings WHERE key=?', ('permissions:' + session,)).fetchone()
            mode = setting['value'] if setting else 'ask'
            if mode not in {'assist', 'full'}:
                return
            rows = db.execute("SELECT a.*,t.session_id FROM chat_approvals a JOIN chat_turns t ON a.turn_id=t.id WHERE t.session_id=? AND t.status='running' AND a.choice IS NULL", (session,)).fetchall()
            for row in rows:
                req = json.loads(row['request'])
                allowed = mode == 'full'
                if mode == 'assist' and req.get('tool') in {'file_read', 'file_write', 'file_edit', 'file_patch'}:
                    args = req.get('arguments') or {}
                    raw = args.get('path')
                    if isinstance(raw, str) and raw:
                        try:
                            target = (workspace / raw).resolve()
                            allowed = target.is_relative_to(workspace)
                        except (OSError, ValueError):
                            allowed = False
                if allowed:
                    db.execute("UPDATE chat_approvals SET choice='once' WHERE id=? AND choice IS NULL", (row['id'],))
                    db.execute('INSERT INTO chat_events(turn_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                               (row['turn_id'], session, 'approval.automatic', json.dumps({'id': row['id'], 'mode': mode, 'tool': req.get('tool'), 'text': '按会话授权模式自动批准'}), time.time()))

    def models(self, session: str) -> dict[str, Any]:
        from .session_settings import SessionSettings
        return SessionSettings(self.store).models(session)

    def set_models(self, session: str, values: dict[str, Any]) -> dict[str, Any]:
        from .session_settings import SessionSettings
        return SessionSettings(self.store).set_models(session, values)

    def submit(self, session: str, message: str, request_id: str) -> str:
        self.store.session(session)
        disabled_plugins = self.plugins(session)['disabled']
        selected = self.models(session)
        frozen = {'main_model': selected['effective_main'], 'sub_model': selected['effective_sub']}
        if 'qwen3.8-max' in {value.lower() for value in frozen.values()}:
            raise ValueError('qwen3.8-max remains disabled; select another model')
        if not message.strip() or len(message) > 50000 or not request_id or len(request_id) > 100:
            raise ValueError('message requires 1..50000 characters and a request id')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT id,message FROM chat_turns WHERE session_id=? AND request_id=?', (session, request_id)).fetchone()
            if previous:
                if previous['message'] != message:
                    raise ValueError('request id already belongs to a different message')
                return str(previous['id'])
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO chat_turns(id,session_id,request_id,message,status,created,model_settings,plugin_settings) VALUES(?,?,?,?,?,?,?,?)',
                       (ident, session, request_id, message, 'queued', time.time(), json.dumps(frozen), json.dumps(disabled_plugins)))
            return ident

    def snapshot(self, session: str, after: int = 0) -> dict[str, Any]:
        self.store.session(session)
        with self.store.db() as db:
            turns = [dict(row) for row in db.execute('SELECT * FROM (SELECT * FROM chat_turns WHERE session_id=? ORDER BY created DESC LIMIT 100) ORDER BY created', (session,))]
            events = [dict(row) for row in db.execute('SELECT * FROM chat_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 300', (session, max(0, after)))]
            approvals = [dict(row) for row in db.execute("SELECT a.* FROM chat_approvals a JOIN chat_turns t ON a.turn_id=t.id WHERE t.session_id=? AND t.status='running' AND a.choice IS NULL", (session,))]
        for event in events:
            event['payload'] = json.loads(event['payload'])
        for approval in approvals:
            approval['request'] = json.loads(approval['request'])
        return {'turns': turns, 'events': events, 'cursor': events[-1]['seq'] if events else after, 'approvals': approvals}

    def cancel(self, session: str, ident: str) -> None:
        with self.store.db() as db:
            if not db.execute('SELECT 1 FROM chat_turns WHERE id=? AND session_id=?', (ident, session)).fetchone():
                raise ValueError('unknown conversation turn')
            db.execute(
                "UPDATE chat_turns SET cancel=1,"
                " stop_reason=CASE WHEN status='queued' THEN ? ELSE stop_reason END,"
                " status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",
                (USER_CANCELLED, ident),
            )

    def approve(self, session: str, ident: str, choice: str) -> None:
        if choice not in {'once', 'deny'}:
            raise ValueError('unknown approval choice')
        with self.store.db() as db:
            row = db.execute("SELECT a.choice,t.status FROM chat_approvals a JOIN chat_turns t ON a.turn_id=t.id WHERE a.id=? AND t.session_id=?", (ident, session)).fetchone()
            if not row or row['status'] != 'running':
                raise ValueError('approval is no longer active')
            if row['choice'] is not None and row['choice'] != choice:
                raise ValueError('approval already answered')
            db.execute('UPDATE chat_approvals SET choice=? WHERE id=?', (choice, ident))

    def record(self, turn: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
        with self.store.db() as db:
            db.execute('INSERT INTO chat_events(turn_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                       (turn['id'], turn['session_id'], kind, json.dumps(payload, ensure_ascii=False), time.time()))
            if kind == 'assistant.delta':
                db.execute('UPDATE chat_turns SET answer=answer||? WHERE id=?', (str(payload.get('text', '')), turn['id']))
            elif kind == 'assistant.final':
                db.execute('UPDATE chat_turns SET answer=? WHERE id=?', (str(payload.get('text', '')), turn['id']))
            elif kind == 'run.link':
                db.execute('UPDATE chat_turns SET run_dir=? WHERE id=?', (str(payload.get('run_dir', '')), turn['id']))
            elif kind == 'approval.request':
                db.execute('INSERT INTO chat_approvals(id,turn_id,request) VALUES(?,?,?)', (payload['id'], turn['id'], json.dumps(payload)))

    def run_once(self) -> bool:
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM chat_turns WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return False
            turn = dict(row)
            db.execute("UPDATE chat_turns SET status='running' WHERE id=?", (turn['id'],))
            history = []
            for previous in db.execute('SELECT message,answer,status FROM (SELECT * FROM chat_turns WHERE session_id=? AND created<? ORDER BY created DESC LIMIT 30) ORDER BY created', (turn['session_id'], turn['created'])):
                history.extend([{'role': 'user', 'content': previous['message']},
                                {'role': 'assistant', 'content': previous['answer'] + ('' if previous['status'] == 'completed' else '\n[上一轮状态：' + previous['status'] + '，不能视为已完成]')}])
        session = self.store.session(turn['session_id'])
        directory = self.store.home / 'chat' / turn['session_id'] / turn['id']
        directory.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        # Web session policy is the approval authority, not inherited CLI grants.
        for key in ('ASTRALNEXUS_TOOL_APPROVAL', 'ASTRALNEXUS_ALLOW_FILE_WRITE', 'ASTRALNEXUS_ALLOW_TERMINAL'):
            env[key] = 'false'
        env['ASTRALNEXUS_CHAT_HOME'] = str(self.store.home)
        env['ASTRALNEXUS_CHAT_SESSION'] = turn['session_id']
        disabled = json.loads(turn.get('plugin_settings') or '[]')
        if self.plugin_host is not None:
            # Host-wide floor: a gate plugin that is not active removes its
            # toolsets from every session, regardless of per-session settings.
            inactive = sorted(self.plugin_host.gate_toolsets() - self.plugin_host.active_toolsets())
            disabled = sorted(set(disabled) | set(inactive))
        env['ASTRALNEXUS_DISABLED_TOOLSETS'] = json.dumps(disabled)
        env['ASTRALNEXUS_WORKSPACE'] = session['workspace']
        frozen = json.loads(turn['model_settings'])
        if frozen:
            env['ASTRALNEXUS_MODEL'] = frozen['main_model']
            env['ASTRALNEXUS_SUB_MODEL'] = frozen['sub_model']
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
        process: subprocess.Popen[bytes] | None = None
        connection: socket.socket | None = None
        listener: socket.socket | None = None
        terminal = 'failed'
        stop_reason = ''
        turn_error = ''
        goal_verdict: dict[str, Any] = {}
        cancelled_at: float | None = None
        completed_status: str | None = None
        descendants: dict[int, Any] = {}
        messages: queue.Queue[str | None] = queue.Queue()
        self.record(turn, 'turn.started', {'text': '正在交给 Governor 分析请求'})
        if frozen:
            self.record(turn, 'models.selected', {'text': '主模型：' + frozen['main_model'] + ' · 子模型：' + frozen['sub_model']})
        try:
            with (directory / 'worker.stderr.log').open('w', encoding='utf-8') as errors:
                # Loopback TCP JSON-lines channel instead of anonymous pipes:
                # this host's sandbox refuses CreatePipe for spawned children
                # (WinError 5), while 127.0.0.1 sockets are the proven carrier
                # (the web server itself and the node-pty sidecar both use
                # them). The worker reads ASTRALNEXUS_CHAT_CHANNEL and
                # connects back; the protocol is unchanged.
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                env['ASTRALNEXUS_CHAT_CHANNEL'] = '127.0.0.1:%d' % listener.getsockname()[1]
                process = subprocess.Popen(self.command, cwd=session['workspace'], env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=errors)
                proc = process  # non-Optional alias: later closures reset narrowing of `process`
                listener.settimeout(.5)
                deadline = time.monotonic() + 60
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError('chat worker exited before opening its channel (exit %s); see worker.stderr.log' % proc.returncode)
                    if time.monotonic() > deadline:
                        raise RuntimeError('chat worker did not open its channel within 60s')
                    try:
                        connection, _ = listener.accept()
                        break
                    except socket.timeout:
                        continue
                connection.settimeout(None)
                inbound = connection.makefile('r', encoding='utf-8', errors='replace')
                outbound = connection.makefile('w', encoding='utf-8', newline='\n')
                def send(value: dict[str, Any]) -> None:
                    outbound.write(json.dumps(value, ensure_ascii=False) + '\n')
                    outbound.flush()
                def read() -> None:
                    try:
                        for line in inbound:
                            messages.put(line)
                    except (OSError, ValueError):
                        pass
                    finally:
                        messages.put(None)
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                send({'message': turn['message'], 'history': history, 'home': str(directory)})
                eof = False
                while not eof:
                    import psutil  # type: ignore[import-untyped]
                    try:
                        for child in psutil.Process(proc.pid).children(recursive=True):
                            descendants[child.pid] = child
                    except psutil.NoSuchProcess:
                        pass
                    self.apply_permissions(turn['session_id'])
                    with self.store.db() as db:
                        cancel = db.execute('SELECT cancel FROM chat_turns WHERE id=?', (turn['id'],)).fetchone()[0]
                        responses = db.execute('SELECT id,choice FROM chat_approvals WHERE turn_id=? AND choice IS NOT NULL AND delivered=0', (turn['id'],)).fetchall()
                    if (cancel or self.stop.is_set()) and cancelled_at is None:
                        cancelled_at = time.monotonic()
                        send({'type': 'cancel'})
                        self.record(turn, 'turn.cancelling', {'text': '已请求停止，等待运行保存状态'})
                    for response in responses:
                        send({'type': 'approval', 'id': response['id'], 'choice': response['choice']})
                        with self.store.db() as db:
                            db.execute('UPDATE chat_approvals SET delivered=1 WHERE id=?', (response['id'],))
                    if cancelled_at and time.monotonic() - cancelled_at > 15:
                        stop_process(proc)
                    try:
                        line = messages.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        eof = True
                    else:
                        event = json.loads(line)
                        kind, payload = event['kind'], event['payload']
                        if kind == 'delivery.publish':
                            # The worker's own publication outcome -- the closest
                            # thing this row has to DeerFlow's ``goal_verdict``.
                            goal_verdict = dict(payload or {})
                        if kind == 'turn.result':
                            completed_status = payload.get('status')
                        else:
                            self.record(turn, kind, payload)
                code = proc.wait(timeout=5)
                reader.join(timeout=2)
                terminal, stop_reason = terminal_for_worker_exit(
                    returncode=code,
                    completed_status=completed_status or '',
                    cancelled=cancel,
                    cancelled_at=cancelled_at is not None,
                    host_stopping=self.stop.is_set(),
                )
        except Exception as exc:
            stop_reason = HOST_EXCEPTION
            turn_error = f'{type(exc).__name__}: {exc}'[:3000]
            self.record(turn, 'turn.error', {'text': turn_error, 'stop_reason': stop_reason})
        finally:
            if cancelled_at is not None:
                import psutil  # type: ignore[import-untyped]
                for child in descendants.values():
                    try:
                        if child.is_running():
                            child.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(list(descendants.values()), timeout=2)
            if process is not None:
                if process.poll() is None:
                    stop_process(process)
            for closable in (connection, listener):
                if closable is not None:
                    try:
                        closable.close()
                    except OSError:
                        pass
            # A reported publication is a claim; the binding in the workspace is
            # the evidence.  Verify it here so the row carries an independent
            # verdict rather than the worker's word -- the project's "reports do
            # not certify themselves" rule, applied to delivery.
            if goal_verdict.get('delivery_id') and goal_verdict.get('status') in {'published', 'skipped'}:
                try:
                    workspace = str(self.store.session(turn['session_id']).get('workspace') or '')
                except Exception:  # noqa: BLE001 - a missing verdict is not a crash
                    workspace = ''
                if workspace:
                    goal_verdict['binding'] = verify_published(
                        workspace_root=Path(workspace),
                        delivery_id=str(goal_verdict.get('delivery_id') or ''),
                        expect_manifest_sha256=str(goal_verdict.get('manifest_sha256') or ''),
                    )
            with self.store.db() as db:
                db.execute(
                    'UPDATE chat_turns SET status=?,stop_reason=?,error=?,goal_verdict=? WHERE id=?',
                    (terminal, stop_reason, turn_error,
                     json.dumps(goal_verdict, ensure_ascii=False), turn['id']),
                )
            self.record(turn, 'turn.' + terminal, {
                'text': terminal,
                'stop_reason': stop_reason,
                'stop_reason_description': describe(stop_reason) if stop_reason else '',
                'error': turn_error,
                'acceptance': '由任务最终发布门判定；对话结束不等于验收通过',
            })
        return True

    def start(self) -> None:
        if self.thread is None:
            def serve() -> None:
                while not self.stop.is_set():
                    try:
                        if not self.run_once():
                            self.stop.wait(.2)
                    except sqlite3.OperationalError as exc:
                        if 'lock' not in str(exc).lower() and 'busy' not in str(exc).lower():
                            raise
                        print('dsh-workbench: chat loop retried after %s' % exc, file=sys.stderr)
                        self.stop.wait(.05)
            self.thread = threading.Thread(target=serve, name='astralnexus-chat-host', daemon=True)
            self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                raise RuntimeError('chat worker has not stopped; retain host lock')
