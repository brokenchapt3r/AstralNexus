"""TUI persistence on the host's existing session identity and SQLite store."""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any
from AstralNexus.runtime.workbench import WorkbenchStore


class SessionStore:
    def __init__(self, home: Path):
        self.host = WorkbenchStore(home)
        with self.host.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tui_session_state(
                    session_id TEXT PRIMARY KEY, history TEXT NOT NULL, usage TEXT NOT NULL,
                    status TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tui_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tui_session_events ON tui_events(session_id,seq);
            ''')

    def save(self, session: Any) -> None:
        with self.host.db() as db:
            db.execute('INSERT INTO sessions(id,title,workspace,run_dir) VALUES(?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET title=excluded.title,run_dir=excluded.run_dir',
                       (session.session_id, session.title, session.workspace, session.run_dir))
            db.execute('INSERT INTO tui_session_state VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(session_id) DO UPDATE SET history=excluded.history,usage=excluded.usage,'
                       'status=excluded.status,updated=excluded.updated',
                       (session.session_id, json.dumps(session.history, ensure_ascii=False),
                        json.dumps(session.usage), 'running' if session.running else 'idle',
                        session.started_at, time.time()))

    def load(self, ident: str) -> dict[str, Any]:
        with self.host.db() as db:
            row = db.execute('SELECT s.*,t.history,t.usage,t.status,t.started,t.updated '
                             'FROM sessions s JOIN tui_session_state t ON s.id=t.session_id WHERE s.id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('session not found')
        result = dict(row)
        result['history'] = json.loads(result['history'])
        result['usage'] = json.loads(result['usage'])
        if not isinstance(result['history'], list) or not isinstance(result['usage'], dict):
            raise ValueError('invalid persistent session data')
        # Loading history is never permission to replay a previously active turn.
        result['interrupted'] = result['status'] == 'running'
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.host.db() as db:
            identifiers = [row[0] for row in db.execute('SELECT session_id FROM tui_session_state ORDER BY updated DESC')]
        return [self.load(ident) for ident in identifiers]

    def append(self, ident: str, kind: str, payload: dict[str, Any]) -> None:
        with self.host.db() as db:
            db.execute('INSERT INTO tui_events(session_id,kind,payload,created) VALUES(?,?,?,?)',
                       (ident, kind, json.dumps(payload, ensure_ascii=False), time.time()))

    def events(self, ident: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        self.load(ident)
        with self.host.db() as db:
            rows = db.execute('SELECT seq,kind,payload,created FROM tui_events WHERE session_id=? AND seq>? '
                              'ORDER BY seq LIMIT ?', (ident, after, min(1000, max(1, limit)))).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    def transcript(self, ident: str) -> list[dict[str, Any]]:
        """Passive history: never replay approvals, mutations, or active tool state."""
        messages = []
        cursor = 0
        while True:
            events = self.events(ident, cursor, 1000)
            if not events:
                break
            for event in events:
                payload, kind = event['payload'], event['kind']
                if kind in {'user.message', 'message.complete'} and payload.get('text'):
                    messages.append({'role': 'user' if kind == 'user.message' else 'assistant', 'text': payload['text']})
                elif kind == 'tool.complete':
                    messages.append({'role': 'tool', 'name': payload.get('name', 'tool'),
                        'text': payload.get('result_text') or payload.get('error') or '调用结束；未保存输出'})
                elif kind == 'runtime.event':
                    messages.append({'role': 'system', 'text': str(payload.get('summary') or payload.get('title') or '')})
            cursor = events[-1]['seq']
        return messages

    def spawn_trees(self, ident: str) -> list[dict[str, Any]]:
        """Restore passive Agent snapshots; never replay a live control event."""
        snapshots: list[dict[str, Any]] = []
        nodes: dict[str, dict[str, Any]] = {}
        cursor = 0
        last_created = 0.0
        while True:
            events = self.events(ident, cursor, 1000)
            if not events:
                break
            for event in events:
                payload, kind = event['payload'], event['kind']
                last_created = event['created']
                if kind == 'spawn_tree.saved':
                    snapshots.append({**payload, 'snapshot_id': str(event['seq'])})
                    nodes.clear()
                elif kind.startswith('subagent.') and payload.get('id'):
                    key = str(payload['id'])
                    node = nodes.setdefault(key, {'id': key, 'goal': '', 'status': 'unknown', 'index': len(nodes), 'tools': []})
                    if kind == 'subagent.start':
                        node.update(goal=payload.get('goal', ''), role=payload.get('role', 'worker'))
                    elif kind == 'subagent.complete':
                        node.update(status=payload.get('status') or 'unknown', summary=payload.get('summary', ''))
                    elif kind == 'subagent.tool':
                        node['tools'].append(payload.get('tool_name', 'tool'))
            cursor = events[-1]['seq']
        if nodes:
            snapshots.append({'snapshot_id': 'events-' + str(cursor), 'session_id': ident,
                              'finished_at': last_created, 'label': '公开事件恢复的历史快照；未确认结束的节点状态为 unknown',
                              'subagents': list(nodes.values())})
        return snapshots[-10:]
