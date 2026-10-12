"""Version-bound file review and explicit undo; independent of run checkpoints."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import difflib
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator, TYPE_CHECKING
import uuid
if TYPE_CHECKING:
    from AstralNexus.runtime.workbench import WorkbenchStore

_current: ContextVar[WorkspaceReview | None] = ContextVar('workspace_review', default=None)


def current_review() -> WorkspaceReview | None:
    return _current.get()


@contextmanager
def review_scope(review: WorkspaceReview) -> Iterator[None]:
    token = _current.set(review)
    try: yield
    finally: _current.reset(token)


def digest(content: bytes | None) -> str:
    return hashlib.sha256(content).hexdigest() if content is not None else 'absent'


def write_version(path: Path, content: bytes | None) -> None:
    """Replace one reviewed version atomically; caller owns version checks."""
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.astralnexus-restore-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class WorkspaceReview:
    def __init__(self, store: WorkbenchStore, session_id: str):
        self.store, self.session_id = store, session_id
        self.lock = threading.RLock()
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS workspace_changes(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,path TEXT NOT NULL,
                before_blob BLOB,after_blob BLOB,before_sha TEXT NOT NULL,after_sha TEXT,
                tool_status TEXT NOT NULL,restored INTEGER NOT NULL DEFAULT 0)''')
            if 'review_decision' not in {row[1] for row in db.execute('PRAGMA table_info(workspace_changes)')}:
                db.execute("ALTER TABLE workspace_changes ADD COLUMN review_decision TEXT NOT NULL DEFAULT 'pending'")

    def execute(self, name: str, params: dict[str, Any], root: Path | None,
                invoke: Callable[[], dict[str, Any]], owned_paths: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in {'terminal_exec', 'python_exec', 'pytest_run'} and root is not None:
            return self._execute_command(root, owned_paths, invoke)
        if name not in {'file_write', 'file_replace', 'file_edit', 'file_patch'} or root is None or not params.get('path'):
            return invoke()
        path = Path(str(params['path']))
        path = (root / path if not path.is_absolute() else path).resolve()
        try: path.relative_to(root.resolve())
        except ValueError: return invoke()  # Normal executor ownership policy rejects it.
        if path.is_dir(): return invoke()
        with self.lock:
            before = path.read_bytes() if path.exists() else None
            ident = uuid.uuid4().hex
            with self.store.db() as db:
                db.execute('INSERT INTO workspace_changes(id,session_id,path,before_blob,before_sha,tool_status) '
                           'VALUES(?,?,?,?,?,?)', (ident, self.session_id, str(path), before, digest(before), 'pending'))
            try:
                result = invoke()
            except BaseException:
                self._finish(ident, path, 'exception')
                raise
            self._finish(ident, path, str(result.get('status') or 'unknown'))
            return {**result, 'change_id': ident}

    def _inventory(self, root: Path, owned: tuple[str, ...]) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        total = 0
        loaded = 0
        excluded = {'.astralnexus_state', '.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache'}
        audit_names = {'trace.jsonl', 'runtime_events.jsonl', 'agent_lifecycle.jsonl', 'task_graph.sqlite', 'runtime_state.json'}
        for raw in owned or ('.',):
            base = (root / raw).resolve()
            if not base.is_relative_to(root.resolve()): raise ValueError('review path is outside workspace')
            candidates = base.rglob('*') if base.is_dir() else [base]
            for path in candidates:
                if not path.is_file() or any(part in excluded for part in path.relative_to(root).parts): continue
                if not path.resolve().is_relative_to(root.resolve()): continue
                if path.resolve() == self.store.path.resolve() or path.name.startswith(self.store.path.name + '-'): continue
                relative = path.relative_to(root)
                if relative.parts[0] == 'node_io' or (path.parent == root and
                        any(path.name == name or path.name.startswith(name + '-') for name in audit_names)): continue
                name = str(path.resolve())
                if name in files: continue
                total += path.stat().st_size
                if total > 128 * 1024 * 1024 or len(files) >= 5000:
                    raise ValueError('review snapshot exceeds 5000 files or 128 MiB; coverage unavailable')
                with path.open('rb') as stream:
                    content = stream.read(128 * 1024 * 1024 - loaded + 1)
                loaded += len(content)
                if loaded > 128 * 1024 * 1024:
                    raise ValueError('review snapshot exceeds 128 MiB; coverage unavailable')
                files[name] = content
        return files

    def _execute_command(self, root: Path, owned: tuple[str, ...], invoke: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            before = self._inventory(root, owned)
        except (OSError, ValueError) as error:
            result = invoke()
            return {**result, 'review_coverage': 'unavailable', 'review_warning': str(error)}
        failure = None
        try:
            result = invoke()
        except BaseException as error:
            failure = error
            result = {'status': 'exception'}
        try:
            after = self._inventory(root, owned)
        except (OSError, ValueError) as error:
            if failure is not None: raise failure
            return {**result, 'review_coverage': 'unavailable', 'review_warning': str(error)}
        changes = []
        with self.store.db() as db:
            for path in sorted(set(before) | set(after)):
                old, new = before.get(path), after.get(path)
                if old == new: continue
                ident = uuid.uuid4().hex
                db.execute('INSERT INTO workspace_changes(id,session_id,path,before_blob,after_blob,before_sha,after_sha,tool_status) '
                    'VALUES(?,?,?,?,?,?,?,?)', (ident, self.session_id, path, old, new, digest(old), digest(new), str(result.get('status') or 'unknown')))
                changes.append(ident)
        if failure is not None: raise failure
        if result.get('background') is True:
            return {**result, 'change_ids': changes, 'review_coverage': 'partial_background',
                    'review_warning': '后台进程仍可继续修改文件；这里只记录工具返回前观察到的改动，不能作为完整回滚点。'}
        return {**result, 'change_ids': changes, 'review_coverage': 'owned_paths' if owned else 'workspace'}

    def _finish(self, ident: str, path: Path, status: str) -> None:
        after = path.read_bytes() if path.exists() else None
        with self.store.db() as db:
            db.execute('UPDATE workspace_changes SET after_blob=?,after_sha=?,tool_status=? WHERE id=?',
                       (after, digest(after), status, ident))

    def changes(self) -> list[dict[str, Any]]:
        with self.store.db() as db:
            rows = db.execute('SELECT * FROM workspace_changes WHERE session_id=? ORDER BY rowid', (self.session_id,)).fetchall()
        result = []
        for row in rows:
            before, after = row['before_blob'] or b'', row['after_blob'] or b''
            diff = ''.join(difflib.unified_diff(before.decode('utf-8', errors='replace').splitlines(True),
                after.decode('utf-8', errors='replace').splitlines(True), fromfile=row['path'] + ' (before)', tofile=row['path'] + ' (after)'))
            if b'\x00' in before or b'\x00' in after: diff = '[二进制文件：按字节版本审查与恢复]'
            result.append({key: row[key] for key in ('id', 'path', 'before_sha', 'after_sha', 'tool_status', 'restored', 'review_decision')} |
                {'changed': row['after_sha'] is not None and row['before_sha'] != row['after_sha'], 'diff': diff[:100000]})
        return result

    def restore(self, ident: str) -> dict[str, Any]:
        with self.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM workspace_changes WHERE id=? AND session_id=?', (ident, self.session_id)).fetchone()
            if row is None: raise ValueError('unknown change in this session')
            if row['restored']: return {'ok': True, 'already_restored': True}
            if row['after_sha'] is None: raise ValueError('unfinished change cannot be restored automatically')
            path = Path(row['path'])
            current = path.read_bytes() if path.exists() else None
            if digest(current) != row['after_sha']:
                raise ValueError('file changed after Agent edit; restore refused')
            before = row['before_blob']
            write_version(path, before)
            db.execute("UPDATE workspace_changes SET restored=1,review_decision='rejected' WHERE id=?", (ident,))
            return {'ok': True, 'path': str(path), 'restored_sha': digest(before)}

    def accept(self, ident: str) -> dict[str, Any]:
        """User review of exact bytes; does not authorize delivery or alter tests."""
        with self.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM workspace_changes WHERE id=? AND session_id=?', (ident, self.session_id)).fetchone()
            if row is None: raise ValueError('unknown change in this session')
            if row['restored']: raise ValueError('restored edit cannot be accepted as the current version')
            if row['after_sha'] is None: raise ValueError('unfinished change cannot be accepted')
            path = Path(row['path'])
            current = path.read_bytes() if path.exists() else None
            if digest(current) != row['after_sha']: raise ValueError('file changed after Agent edit; review the new version')
            db.execute("UPDATE workspace_changes SET review_decision='accepted' WHERE id=?", (ident,))
            return {'ok': True, 'change_id': ident, 'path': str(path), 'review_decision': 'accepted',
                    'content_sha256': row['after_sha'], 'acceptance_scope': 'user_file_review_only'}
