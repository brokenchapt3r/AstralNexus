"""Model-facing adapters to the persistent host, never an in-process daemon."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import urllib.request

from .context import get_tool_workspace
from .registry import registry, tool_error, tool_result


def _request(path: str, body: dict[str, Any] | None = None) -> Any:
    base = os.environ.get('ASTRALNEXUS_WORKBENCH_URL', 'http://127.0.0.1:5073').rstrip('/')
    request = urllib.request.Request(base + '/api/host' + path,
        data=json.dumps(body).encode('utf-8') if body is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _session(snapshot: dict[str, Any]) -> str:
    from AstralNexus.runtime.presentation import current_presentation
    from AstralNexus.runtime.run_context import get_env
    context = current_presentation()
    root = context.workspace if context else (get_tool_workspace() or Path.cwd()).resolve()
    ident = get_env("ASTRALNEXUS_CHAT_SESSION", "")
    if ident:
        match = next((s for s in snapshot["sessions"] if s["id"] == ident), None)
        home = get_env("ASTRALNEXUS_CHAT_HOME", "")
        managed = Path(home).resolve() / "chat" / ident if home else None
        if match and (Path(match["workspace"]).resolve() == root or (managed is not None and root.is_relative_to(managed))):
            return ident
    matches = [s['id'] for s in snapshot['sessions'] if Path(s['workspace']).resolve() == root]
    if not matches:
        raise ValueError('Register this workspace in the persistent client before using host tools')
    return snapshot['selected'] if snapshot.get('selected') in matches else matches[0]


def dispatch(operation: str, args: dict[str, Any]) -> str:
    try:
        snapshot = _request('')
        if operation == 'runtime_plugins':
            return tool_result({'status': 'success', 'plugins': snapshot['plugins']})
        session = _session(snapshot)
        if operation == 'lsp_query':
            result = _request('/lsp', {**args, 'session_id': session})
        elif operation == 'workflow_submit':
            result = _request('/workflows', {'session_id': session, 'workflow': args['workflow']})
        elif operation in {'workflow_status', 'workflow_cancel'}:
            job = _request('/jobs/' + str(args['id']))
            if job['session_id'] != session:
                raise ValueError('job belongs to another workspace session')
            result = job if operation == 'workflow_status' else _request('/jobs/' + str(args['id']) + '/cancel', {})
        elif operation == 'schedule_create':
            result = _request('/schedules', {**args, 'session_id': session})
        elif operation == 'schedule_update':
            if not any(s['id'] == args['id'] and s['session_id'] == session for s in snapshot['schedules']):
                raise ValueError('schedule does not belong to this session')
            result = _request('/schedules/' + str(args['id']), {'enabled': bool(args['enabled'])})
        else:
            result = {'schedules': [s for s in snapshot['schedules'] if s['session_id'] == session]}
        return tool_result({'status': 'success', 'result': result})
    except Exception as exc:
        return tool_error(str(exc))


DEFINITIONS = {
    'runtime_plugins': ('Inspect persistent host capability readiness.', {}, []),
    'lsp_query': ('Query live workspace LSP. Positions are zero-based UTF-16.',
        {'operation': {'type': 'string', 'enum': ['hover', 'definition', 'references', 'documentSymbol']},
         'path': {'type': 'string'}, 'line': {'type': 'integer', 'minimum': 0}, 'character': {'type': 'integer', 'minimum': 0}}, ['operation', 'path']),
    'workflow_submit': ('Submit an explicit argv pipeline to the persistent host. A successful job is not Agent acceptance.',
        {'workflow': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'steps': {'type': 'array', 'items': {
            'type': 'object', 'properties': {'id': {'type': 'string'}, 'command': {'type': 'array', 'items': {'type': 'string'}},
             'depends_on': {'type': 'array', 'items': {'type': 'string'}}, 'timeout': {'type': 'number'}}, 'required': ['id', 'command']}}}, 'required': ['steps']}}, ['workflow']),
    'workflow_status': ('Read persisted workflow status and execution receipts.', {'id': {'type': 'string'}}, ['id']),
    'workflow_cancel': ('Cancel a queued/running workflow and clean up its children.', {'id': {'type': 'string'}}, ['id']),
    'schedule_create': ('Schedule a workflow once or at fixed interval; never resumes paused Agent runs.',
        {'workflow': {'type': 'object'}, 'due': {'type': 'number', 'description': 'UTC Unix seconds'},
         'interval': {'type': 'number', 'description': '0 for one-shot, otherwise >=60 seconds'}}, ['workflow', 'due']),
    'schedule_list': ('List durable schedules for this workspace.', {}, []),
    'schedule_update': ('Enable or disable an existing schedule without deleting its history.',
        {'id': {'type': 'string'}, 'enabled': {'type': 'boolean'}}, ['id', 'enabled']),
}


def _handler(name: str):
    return lambda args: dispatch(name, args)


def _schema(name: str) -> dict[str, Any]:
    description, properties, required = DEFINITIONS[name]
    return {'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': properties, 'required': required}}


registry.register(name='runtime_plugins', toolset="runtime_host", schema=_schema('runtime_plugins'), handler=_handler('runtime_plugins'), requires_approval=False)
registry.register(name='lsp_query', toolset="runtime_host", schema=_schema('lsp_query'), handler=_handler('lsp_query'), requires_approval=False)
registry.register(name='workflow_submit', toolset="runtime_host", schema=_schema('workflow_submit'), handler=_handler('workflow_submit'), requires_approval=True)
registry.register(name='workflow_status', toolset="runtime_host", schema=_schema('workflow_status'), handler=_handler('workflow_status'), requires_approval=False)
registry.register(name='workflow_cancel', toolset="runtime_host", schema=_schema('workflow_cancel'), handler=_handler('workflow_cancel'), requires_approval=True)
registry.register(name='schedule_create', toolset="runtime_host", schema=_schema('schedule_create'), handler=_handler('schedule_create'), requires_approval=True)
registry.register(name='schedule_list', toolset="runtime_host", schema=_schema('schedule_list'), handler=_handler('schedule_list'), requires_approval=False)
registry.register(name='schedule_update', toolset="runtime_host", schema=_schema('schedule_update'), handler=_handler('schedule_update'), requires_approval=True)
