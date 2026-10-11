"""Explicit user restore points over captured edits; no run-state rewrites."""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
import uuid
from AstralNexus.runtime.workspace_review import WorkspaceReview, digest, write_version


@dataclass
class CapturedVersion:
    original: bytes | None
    target: bytes | None
    before_sha: str


class UserCheckpoints:
    def __init__(self, review: WorkspaceReview):
        self.review = review
        with review.store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS user_checkpoints(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, label TEXT NOT NULL,
                change_seq INTEGER NOT NULL, history TEXT NOT NULL, created TEXT NOT NULL,
                restored INTEGER NOT NULL DEFAULT 0)""")

    def create(self, history: list[dict[str, str]], label: str = '') -> dict[str, Any]:
        ident = uuid.uuid4().hex
        created = datetime.now(timezone.utc).isoformat()
        with self.review.lock, self.review.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            seq = db.execute('SELECT COALESCE(MAX(rowid),0) FROM workspace_changes WHERE session_id=?', (self.review.session_id,)).fetchone()[0]
            db.execute('INSERT INTO user_checkpoints(id,session_id,label,change_seq,history,created) VALUES(?,?,?,?,?,?)',
                       (ident, self.review.session_id, label, seq, json.dumps(history, ensure_ascii=False), created))
        return {'id': ident, 'label': label, 'created': created, 'coverage': 'captured_tool_changes_only'}

    def list(self) -> list[dict[str, Any]]:
        with self.review.store.db() as db:
            rows = db.execute('SELECT id,label,change_seq,created,restored FROM user_checkpoints WHERE session_id=? ORDER BY rowid DESC', (self.review.session_id,)).fetchall()
        return [dict(row) | {'coverage': 'captured_tool_changes_only'} for row in rows]

    def restore(self, ident: str) -> dict[str, Any]:
        with self.review.lock, self.review.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            checkpoint = db.execute('SELECT * FROM user_checkpoints WHERE id=? AND session_id=?', (ident, self.review.session_id)).fetchone()
            if checkpoint is None: raise ValueError('unknown checkpoint in this session')
            history = json.loads(checkpoint['history'])
            if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
                raise ValueError('checkpoint conversation is invalid')
            if checkpoint['restored']:
                return {'ok': True, 'already_restored': True, 'checkpoint_id': ident, 'history': history, 'label': checkpoint['label'], 'restored_files': [], 'coverage': 'captured_tool_changes_only'}
            rows = db.execute('SELECT rowid,* FROM workspace_changes WHERE session_id=? AND rowid>? AND restored=0 ORDER BY rowid DESC',
                              (self.review.session_id, checkpoint['change_seq'])).fetchall()
            versions: dict[str, CapturedVersion] = {}
            for row in rows:
                if row['after_sha'] is None: raise ValueError('unfinished edit prevents checkpoint restore')
                path_key = str(row['path'])
                if path_key not in versions:
                    candidate = Path(path_key)
                    current = candidate.read_bytes() if candidate.exists() else None
                    if digest(current) != row['after_sha']: raise ValueError('file changed after Agent edit; whole checkpoint restore refused: ' + path_key)
                    versions[path_key] = CapturedVersion(current, row['before_blob'], row['before_sha'])
                else:
                    version = versions[path_key]
                    if version.before_sha != row['after_sha']:
                        raise ValueError('unobserved edit breaks the version chain; restore refused: ' + path_key)
                    version.target = row['before_blob']
                    version.before_sha = row['before_sha']
            applied: list[str] = []
            try:
                for raw, version in versions.items():
                    path = Path(raw)
                    current = path.read_bytes() if path.exists() else None
                    if digest(current) != digest(version.original): raise ValueError('file changed during restore: ' + raw)
                    write_version(path, version.target)
                    applied.append(raw)
                for row in rows:
                    db.execute("UPDATE workspace_changes SET restored=1,review_decision='rejected' WHERE id=?", (row['id'],))
                db.execute('UPDATE user_checkpoints SET restored=1 WHERE id=?', (ident,))
                db.commit()
            except BaseException:
                conflicts = []
                for raw in reversed(applied):
                    version, path = versions[raw], Path(raw)
                    try:
                        current = path.read_bytes() if path.exists() else None
                        if digest(current) != digest(version.target):
                            conflicts.append(raw)
                        else:
                            write_version(path, version.original)
                    except OSError:
                        conflicts.append(raw)
                if conflicts:
                    raise RuntimeError('checkpoint restore incomplete; compensation could not restore: ' + ', '.join(conflicts))
                raise
        return {'ok': True, 'checkpoint_id': ident, 'history': history, 'label': checkpoint['label'],
                'restored_files': list(versions), 'coverage': 'captured_tool_changes_only'}
