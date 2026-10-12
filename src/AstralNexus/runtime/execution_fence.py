"""Ownership boundary for managed execution writes and tool admission."""
from __future__ import annotations
from contextlib import contextmanager, closing
from contextvars import ContextVar
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator

FenceProvider = Callable[[], dict[str, Any] | None]
_scope: ContextVar[tuple[Path, FenceProvider | None, str] | None] = ContextVar('execution_fence', default=None)


class ExecutionOwnershipLost(RuntimeError):
    """The execution writer cannot establish its current run lease."""


def current_execution_fence(root: Path) -> FenceProvider | None:
    bound = _scope.get()
    return bound[1] if bound is not None and bound[0] == root.resolve() else None


@contextmanager
def execution_fence_scope(root: Path, provider: FenceProvider | None, execution_id: str = '') -> Iterator[None]:
    token = _scope.set((root.resolve(), provider, execution_id))
    try: yield
    finally: _scope.reset(token)


def current_execution_identity(root: Path) -> str:
    bound = _scope.get()
    return bound[2] if bound is not None and bound[0] == root.resolve() else ''


def guard_active_execution(root: Path, ident: str) -> None:
    if not ident: return
    path = root.resolve() / '.astralnexus_state' / 'executions.sqlite'
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            row = connection.execute('SELECT status FROM executions WHERE id=?', (ident,)).fetchone()
        if row is None or row[0] != 'running':
            raise ExecutionOwnershipLost('execution is no longer active; tool dispatch refused')
    except sqlite3.Error as error:
        raise ExecutionOwnershipLost('execution status unavailable') from error


def guard_execution_fence(root: Path, provider: FenceProvider | None,
                          db: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    owners = root.resolve() / '.astralnexus_state' / 'runs.sqlite'
    try:
        if provider is None:
            if not owners.is_file(): return None  # Explicit legacy, not a managed run.
            with closing(sqlite3.connect(owners.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
                if connection.execute('SELECT 1 FROM runs LIMIT 1').fetchone() is None: return None
            raise ExecutionOwnershipLost('managed execution has no fence provider')
        token = provider()
        if not token or Path(str(token.get('runs_db_path') or '')).resolve() != owners or not owners.is_file():
            raise ExecutionOwnershipLost('execution fence identity is missing or belongs to another run')
        args = (str(token['run_id']), str(token['owner_id']), int(token['generation']), str(token['now_iso']))
        predicate = "run_id=? AND owner_id=? AND generation=? AND status='running' AND lease_expires_at>?"
        if db is not None:
            db.execute('ATTACH DATABASE ? AS execution_owner', (str(owners),))
            # The conditional write obtains the same owner DB lock as takeover,
            # including when the DB uses WAL; no stale receipt transaction can
            # race a generation replacement after this guard.
            cursor = db.execute('UPDATE execution_owner.runs SET updated_at=updated_at WHERE ' + predicate, args)
            valid = cursor.rowcount == 1
        else:
            with closing(sqlite3.connect(owners.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
                valid = connection.execute('SELECT 1 FROM runs WHERE ' + predicate, args).fetchone() is not None
        if not valid: raise ExecutionOwnershipLost('execution ownership lost or lease expired')
        return token
    except ExecutionOwnershipLost:
        raise
    except Exception as error:
        raise ExecutionOwnershipLost('execution fence unavailable: ' + type(error).__name__) from error
