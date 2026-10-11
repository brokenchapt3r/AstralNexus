"""Native DSH contract adaptations; these are not Cordis binary plugins."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from .context import get_tool_workspace, resolve_tool_path
from .registry import registry, tool_result


def present(args: dict[str, Any]) -> str:
    files = args.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise ValueError('files must contain 1..8 regular files')
    root = resolve_tool_path(get_tool_workspace() or Path(os.environ.get('ASTRALNEXUS_WORKSPACE', '.')))
    results = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('each file requires a path')
        raw = root / entry['path']
        path = resolve_tool_path(raw)
        if raw.is_symlink() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError('delivery requires a regular file in the current workspace')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        results.append({'path': str(path.relative_to(root)), 'description': str(entry.get('description', ''))[:1000],
                        'bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
    return tool_result(status='success', files=results, acceptance='unverified',
                       note='File declarations are execution evidence, not final acceptance or publication.')


def runtime_inspect(args: dict[str, Any]) -> str:
    query = str(args.get('query', '')).casefold()
    rows = []
    for name in registry.get_all_tool_names():
        entry = registry.get_entry(name)
        if entry is not None and query in (entry.name + ' ' + entry.toolset).casefold():
            rows.append({'name': name, 'toolset': entry.toolset, 'available': registry.is_tool_available(name),
                         'parameters': entry.schema.get('parameters', {}), 'description': entry.description[:1000]})
    return tool_result(status='success', tools=rows[:100], truncated=len(rows)>100)


def session_history(args: dict[str, Any]) -> str:
    home, session = os.environ.get('ASTRALNEXUS_CHAT_HOME'), os.environ.get('ASTRALNEXUS_CHAT_SESSION')
    if not home or not session:
        raise ValueError('session history requires a web conversation context')
    database = Path(home).resolve() / 'workbench.sqlite'
    limit = max(1, min(int(args.get('limit', 10)), 30))
    query = str(args.get('query', ''))[:500]
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id,message,answer,status,created FROM chat_turns WHERE session_id=? AND status IN ('completed','failed','cancelled','interrupted') AND (instr(message,?)>0 OR instr(answer,?)>0) ORDER BY created DESC LIMIT ?", (session, query, query, limit + 1)).fetchall()
    return tool_result(status='success', turns=[{**dict(row), 'message':row['message'][:2000], 'answer':row['answer'][:4000]} for row in rows[:limit]],
                       truncated=len(rows)>limit, scope='current_conversation', note='History is not current acceptance evidence.')


registry.register(name='present', toolset='deliverables', schema={'name':'present','description':'Declare existing workspace files with descriptions and content digests. Does not approve or publish the delivery.','parameters':{'type':'object','properties':{'files':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'object','properties':{'path':{'type':'string'},'description':{'type':'string'}},'required':['path']}}},'required':['files']}}, handler=present)
registry.register(name='runtime_inspect', toolset='introspection', schema={'name':'runtime_inspect','description':'Inspect registered native tool contracts and availability; no credentials are returned.','parameters':{'type':'object','properties':{'query':{'type':'string'}}}}, handler=runtime_inspect)
registry.register(name='session_history', toolset='session_query', schema={'name':'session_history','description':'Search bounded completed or stopped turns in the current web conversation. Historical output is not current evidence.','parameters':{'type':'object','properties':{'query':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':30}}}}, handler=session_history)
