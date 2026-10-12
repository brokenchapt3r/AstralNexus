"""Workspace-scoped, persistent stdio LSP with Content-Length framing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any

from .workbench import stop_process


class LanguageService:
    def __init__(self, root: Path, command: list[str], timeout: float = 20):
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.lock = threading.RLock()
        self.messages: queue.Queue[Any] = queue.Queue(maxsize=2048)
        self.counter = 0
        self.closed = False
        self.closing = False
        self.process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.reader = threading.Thread(target=self._read, daemon=True, name='astralnexus-lsp-reader')
        self.reader.start()
        try:
            result = self.request('initialize', {'processId': os.getpid(), 'rootUri': self.root.as_uri(),
                'capabilities': {'general': {'positionEncodings': ['utf-16']},
                                 'workspace': {'configuration': True},
                                 'textDocument': {'hover': {'contentFormat': ['markdown', 'plaintext']}}},
                'workspaceFolders': [{'uri': self.root.as_uri(), 'name': self.root.name}]})
            self.capabilities = result.get('capabilities', {})
            if self.capabilities.get('positionEncoding', 'utf-16') != 'utf-16':
                raise ValueError('language server did not negotiate UTF-16')
            self.notify('initialized', {})
        except Exception:
            self.close()
            raise

    def _read(self) -> None:
        try:
            assert self.process.stdout is not None
            while True:
                length = None
                header_bytes = 0
                while True:
                    line = self.process.stdout.readline(8193)
                    if not line:
                        raise EOFError('language server closed stdout')
                    header_bytes += len(line)
                    if header_bytes > 8192:
                        raise ValueError('LSP header too large')
                    if line in {b'\r\n', b'\n'}:
                        break
                    key, _, value = line.partition(b':')
                    if key.lower() == b'content-length':
                        length = int(value.strip())
                if length is None or not 0 <= length <= 8_000_000:
                    raise ValueError('invalid LSP Content-Length')
                data = self.process.stdout.read(length)
                if len(data) != length:
                    raise EOFError('truncated LSP frame')
                self.messages.put_nowait(json.loads(data))
        except Exception as exc:
            try:
                self.messages.put_nowait(exc)
            except queue.Full:
                # A non-consuming client must not leave the server blocked forever.
                self.process.terminate()

    def _send(self, payload: dict[str, Any]) -> None:
        if self.closed or self.process.poll() is not None:
            raise RuntimeError('language service is closed')
        data = json.dumps({'jsonrpc': '2.0', **payload}).encode('utf-8')
        assert self.process.stdin is not None
        self.process.stdin.write(f'Content-Length: {len(data)}\r\n\r\n'.encode('ascii') + data)
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        with self.lock:
            self._send({'method': method, 'params': params})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        with self.lock:
            self.counter += 1
            ident = self.counter
            self._send({'id': ident, 'method': method, 'params': params})
            deadline = time.monotonic() + self.timeout
            try:
                while True:
                    message = self.messages.get(timeout=max(0.001, deadline - time.monotonic()))
                    if isinstance(message, Exception):
                        raise message
                    if 'method' in message:
                        if 'id' in message:
                            if message['method'] == 'workspace/configuration':
                                result: Any = [{} for _ in message.get('params', {}).get('items', [])]
                            elif message['method'] in {'client/registerCapability', 'window/workDoneProgress/create'}:
                                result = None
                            else:
                                self._send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported client operation'}})
                                continue
                            self._send({'id': message['id'], 'result': result})
                        if time.monotonic() >= deadline:
                            raise queue.Empty
                        continue
                    if message.get('id') != ident:
                        raise ValueError('LSP response identity mismatch')
                    if 'error' in message:
                        raise ValueError('LSP error: ' + json.dumps(message['error']))
                    return message.get('result')
            except queue.Empty:
                self.close()
                raise TimeoutError(f'LSP request timed out: {method}') from None

    def query(self, operation: str, path: str, line: int = 0, character: int = 0) -> dict[str, Any]:
        operations = {'hover': 'hoverProvider', 'definition': 'definitionProvider',
                      'references': 'referencesProvider', 'documentSymbol': 'documentSymbolProvider'}
        if operation not in operations:
            raise ValueError('unsupported LSP operation')
        if not self.capabilities.get(operations[operation]):
            raise ValueError(f'language server does not advertise {operation}')
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            raise ValueError('source must be a file in the session workspace')
        if target.stat().st_size > 2_000_000 or line < 0 or character < 0:
            raise ValueError('invalid source size or position')
        text = target.read_text(encoding='utf-8')
        language = {'.ts': 'typescript', '.tsx': 'typescriptreact', '.js': 'javascript', '.jsx': 'javascriptreact',
                    '.py': 'python', '.pyi': 'python'}.get(target.suffix, target.suffix.lstrip('.'))
        uri = target.as_uri()
        with self.lock:
            self.notify('textDocument/didOpen', {'textDocument': {'uri': uri, 'languageId': language, 'version': 1, 'text': text}})
            try:
                params: dict[str, Any] = {'textDocument': {'uri': uri}}
                if operation != 'documentSymbol':
                    params['position'] = {'line': line, 'character': character}
                if operation == 'references':
                    params['context'] = {'includeDeclaration': True}
                result = self.request('textDocument/' + operation, params)
                return {'operation': operation, 'position_encoding': 'utf-16', 'zero_based': True,
                        'path': str(target), 'result': result, 'server_pid': self.process.pid}
            finally:
                if not self.closed:
                    self.notify('textDocument/didClose', {'textDocument': {'uri': uri}})

    def close(self) -> None:
        with self.lock:
            if self.closed or self.closing:
                return
            self.closing = True
            if self.process.poll() is None:
                try:
                    self.timeout = min(self.timeout, 1)
                    self.request('shutdown', {})
                    self.notify('exit', {})
                    self.process.wait(timeout=1)
                except Exception:
                    pass
            self.closed = True
            if self.process.poll() is None:
                stop_process(self.process)
            for handle in (self.process.stdin, self.process.stdout):
                if handle:
                    handle.close()
            if threading.current_thread() is not self.reader:
                self.reader.join(timeout=2)
