"""A versioned public event identity, shared by transport and persisted history."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
import uuid


class UxEvent(TypedDict):
    version: int
    event_id: str
    event_type: str
    session_id: str | None
    run_id: str | None
    node_id: str | None
    execution_id: str | None
    tool_call_id: str | None
    phase: str
    summary: str
    status: str
    actions: list[dict[str, Any]]
    recorded_at: str


def envelope(kind: str, payload: dict[str, Any], session_id: str | None, run_dir: str = '') -> UxEvent:
    node = payload.get('node_id') or (payload.get('id') if kind.startswith('subagent.') else None)
    actions = []
    if session_id and kind in {'message.start', 'run.progress'}:
        actions.append({'id': 'stop', 'label': '停止本轮', 'method': 'session.interrupt', 'params': {'session_id': session_id}})
    if session_id and run_dir:
        actions.append({'id': 'plan', 'label': '查看计划', 'method': 'inspection.get', 'params': {'session_id': session_id, 'kind': 'plan'}})
    return {'version': 1, 'event_id': uuid.uuid4().hex, 'event_type': kind,
        'session_id': session_id, 'run_id': str(payload.get('run_id') or Path(run_dir).name) or None,
        'node_id': str(node) if node else None,
        'execution_id': str(payload['execution_id']) if payload.get('execution_id') else None,
        'tool_call_id': str(payload['call_id']) if payload.get('call_id') else None,
        'phase': str(payload.get('phase') or 'unknown'),
        'summary': str(payload.get('summary') or payload.get('text') or payload.get('title') or payload.get('name') or ''),
        'status': str(payload.get('status') or 'unknown'), 'actions': actions,
        'recorded_at': str(payload.get('recorded_at') or datetime.now(timezone.utc).isoformat())}
