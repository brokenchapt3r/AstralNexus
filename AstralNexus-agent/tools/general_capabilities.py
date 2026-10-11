"""Portable capability adapters usable by development, research and data skills."""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .context import get_tool_workspace, resolve_tool_path
from .registry import registry, tool_error, tool_result


def _path(raw: str) -> Path:
    from .context import read_tool_path
    return read_tool_path(raw)


def _glob(args: dict[str, Any]) -> str:
    root = _path(str(args.get('path') or '.'))
    pattern = str(args.get('pattern') or '*')
    limit = max(1, min(500, int(args.get('limit', 100))))
    files = []
    visited = 0
    truncated = False
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '.venv', '__pycache__'}]
        for name in names:
            visited += 1
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
                files.append(relative)
            if len(files) >= limit or visited >= 10000:
                truncated = True
                break
        if truncated:
            break
    return tool_result({'status': 'success', 'files': files, 'truncated': truncated, 'scanned': visited})


def _dependencies(args: dict[str, Any]) -> str:
    modules = args.get('modules') or ['pytest', 'playwright', 'pandas', 'numpy', 'docx', 'pptx', 'openpyxl', 'pypdf']
    found = {}
    for name in modules[:50]:
        try:
            spec = importlib.util.find_spec(str(name))
            found[str(name)] = {'available': spec is not None, 'origin': spec.origin if spec else None}
        except (ImportError, ValueError, ModuleNotFoundError):
            found[str(name)] = {'available': False, 'origin': None}
    return tool_result({'status': 'success', 'python': sys.executable, 'modules': found,
        'executables': {name: shutil.which(name) for name in ['node', 'npm', 'git', 'rg', 'docker', 'uv', 'pwsh']}})


def _sqlite(args: dict[str, Any]) -> str:
    path = _path(str(args['path']))
    query = str(args['query'])
    limit = max(1, min(1000, int(args.get('limit', 100))))
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
            db.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 5
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            cursor = db.execute(query, args.get('parameters') or [])
            rows = cursor.fetchmany(limit + 1)
            result = {'status': 'success', 'columns': [d[0] for d in cursor.description or []],
                      'rows': rows[:limit], 'truncated': len(rows) > limit}
            return tool_result(result)
    except sqlite3.Error as exc:
        return tool_error(str(exc))


def _http(args: dict[str, Any]) -> str:
    url = str(args['url'])
    if not url.startswith(('http://', 'https://')):
        return tool_error('HTTP(S) URL required')
    body = args.get('body')
    headers = dict(args.get('headers') or {})
    data: bytes | None
    if isinstance(body, (dict, list)):
        headers.setdefault('Content-Type', 'application/json')
        data = json.dumps(body).encode('utf-8')
    else:
        data = str(body).encode('utf-8') if body is not None else None
    request = Request(url, data=data, headers=headers, method=str(args.get('method', 'GET')).upper())
    limit = max(1, min(int(args.get('max_bytes', 50000)), 300000))
    try:
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(limit + 1)
            return tool_result({'status': 'success', 'status_code': response.code, 'url': response.url,
                'body': raw[:limit].decode('utf-8', errors='replace'), 'truncated': len(raw) > limit,
                'note': 'HTTP response evidence only; business assertions must be checked separately'})
    except OSError as exc:
        return tool_error(str(exc))


registry.register(name='glob_files', toolset='file', schema={'name': 'glob_files', 'description': 'Find workspace files by glob with bounded traversal, without reading file contents.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'pattern': {'type': 'string'}, 'limit': {'type': 'integer'}}}}, handler=_glob)
registry.register(name='runtime_dependencies', toolset='execution', schema={'name': 'runtime_dependencies', 'description': 'Inspect installed Python libraries and executable locations before running a skill script.', 'parameters': {'type': 'object', 'properties': {'modules': {'type': 'array', 'items': {'type': 'string'}}}}}, handler=_dependencies)
registry.register(name='sqlite_query', toolset='data', schema={'name': 'sqlite_query', 'description': 'Query an existing workspace SQLite database read-only with row and execution bounds.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'query': {'type': 'string'}, 'parameters': {'type': 'array'}, 'limit': {'type': 'integer'}}, 'required': ['path', 'query']}}, handler=_sqlite)
registry.register(name='http_request', toolset='network', schema={'name': 'http_request', 'description': 'Execute an HTTP request and return bounded response evidence including non-2xx status codes. Does not claim business tests passed.', 'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}, 'method': {'type': 'string', 'enum': ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']}, 'headers': {'type': 'object'}, 'body': {}, 'max_bytes': {'type': 'integer'}}, 'required': ['url']}}, handler=_http, requires_approval=True)
