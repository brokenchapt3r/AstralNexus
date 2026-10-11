"""Workflow host ownership uses the same lease and receipt fence as Agent runs."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Any, Iterator
import uuid
from .run_coordinator import RunCoordinator, RunLease
from .run_lifecycle import _start_lease_heartbeat, _stop_lease_heartbeat
from .execution_fence import ExecutionOwnershipLost, guard_execution_fence


class JobOwnership:
    def __init__(self, root: Path):
        self.root = root
        self.coordinator = RunCoordinator(root, run_id=root.name)
        self.holder: dict[str, RunLease] | None = None
        self.stop: threading.Event | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self):
        lease = self.coordinator.acquire('workflow-' + uuid.uuid4().hex)
        if lease is None: raise ExecutionOwnershipLost('workflow has another owner or a manual stop')
        self.holder, self.stop, self.thread, _ = _start_lease_heartbeat(self.coordinator, lease, threading.Event(), None)
        return self

    def fence(self) -> dict[str, Any] | None:
        if self.holder is None: return None
        return self.coordinator.fence_token(self.holder['lease'])

    @contextmanager
    def job_db(self, store) -> Iterator[Any]:
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            guard_execution_fence(self.root, self.fence, db)
            yield db

    def finish(self, status: str, reason: str) -> None:
        if self.holder is not None:
            self.coordinator.finish(self.holder['lease'], status, stop_code=reason, stop_reason=reason)

    def __exit__(self, kind, value, trace):
        _stop_lease_heartbeat(self.stop, self.thread)
        if self.holder is not None and self.coordinator.owns(self.holder['lease']):
            self.finish('blocked', 'host_exception')
