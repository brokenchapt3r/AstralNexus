"""Isolated JSON-lines adapter to the existing conversational CLI runtime."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import threading
from typing import Any, Callable
import uuid


class ChatOutput:
    def __init__(self, emit: Callable[[str, dict[str, Any]], None], cancel: threading.Event):
        self.emit = emit
        self.cancel = cancel
        self.failed = False

    def cancel_requested(self) -> bool:
        return self.cancel.is_set()

    def write(self, text: str, *, error: bool = False) -> None:
        self.failed = self.failed or error
        self.emit('turn.error' if error else 'progress', {'text': str(text)[:6000]})

    def write_response(self, text: str) -> None:
        self.emit('assistant.final', {'text': text})

    def event(self, event: dict[str, Any]) -> None:
        kind = str(event.get('event_type', ''))
        if kind == 'llm.text.delta' and not event.get('node_id'):
            self.emit('assistant.delta', {'text': str(event.get('text', ''))})
        elif kind in {'llm.tool_execution.start', 'llm.tool_execution.done', 'llm.tool_batch'}:
            for call in event.get('calls') or []:
                if isinstance(call, dict):
                    arguments = call.get('arguments') or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            arguments = {}
                    visible_args = {key: str(arguments[key])[:4000] for key in ('command', 'path', 'cwd', 'timeout')
                                    if isinstance(arguments, dict) and key in arguments}
                    self.emit('progress', {'type': kind, 'tool_name': str(call.get('name', 'tool')),
                                          'arguments': visible_args,
                                          'call_id': str(call.get('call_id') or call.get('id') or ''),
                                          'path': str(call.get('path') or ''),
                                          'text': str(call.get('summary') or call.get('error') or call.get('stdout_tail') or call.get('stderr_tail') or call.get('name') or '工具执行')[:2000],
                                          'status': str(call.get('status') or 'running'),
                                          'node_id': str(event.get('node_id') or '')})
        elif kind.startswith(('interactive.', 'public.', 'llm.tool_execution.', 'orchestrator.', 'node.', 'verification.')) or kind in {
            'task.received', 'governor.analysis', 'plan.created', 'tool.called', 'tool.result',
            'planning.started', 'planning.summary', 'agent.spawned', 'agent.activity', 'tool.started', 'tool.finished',
            'governor.interactive_route.decided', 'runtime.progress.report', 'llm.retry', 'llm.stream.failed', 'llm.error',
            'artifact.created', 'recovery.triggered', 'task.completed', 'task.failed',
            'llm.stream.started', 'llm.context.compression.started', 'llm.context.compression.completed',
        }:
            # Never forward raw model requests, hidden reasoning, or tool bodies.
            public = {key: str(event[key])[:2000] for key in (
                'text', 'title', 'summary', 'node_id', 'status', 'tool_name', 'name', 'model', 'run_dir', 'path', 'role', 'actor', 'goal',
            ) if event.get(key) is not None}
            self.emit('progress', {'type': kind, **public})


def _execute_turn(request: dict[str, Any], controller: Any, output: ChatOutput,
                  govern: Callable[..., bool], chat: Callable[..., None]) -> str:
    """Shared tested boundary: a start acknowledgment is never terminal success."""
    history = list(request.get('history', []))
    if history and not any(item.get('role') == 'system' for item in history):
        history.insert(0, {'role': 'system', 'content': (
            '你是天枢 AstralNexus 的交互式 Agent。当前真实工作区是：' + str(Path.cwd()) +
            '。历史对话是上下文，不是当前文件或验收事实；需通过真实工具重新确认。'
            '根据请求读取文件、使用工具、修改和验证，复杂任务交给 delegate_task。'
            '只能报告真实执行结果，不得将启动确认或上轮失败当成完成。'
            '临时编辑使用工作区；最终产物写入 outputs/，完成后必须调用 present_files。'
        )})
    initial = len(history)
    from AstralNexus.runtime.presentation import presentation_scope
    with presentation_scope(None):  # A delegated run receives no lead authority.
        consumed = govern(request['message'], history, controller, output=output)
    if not consumed and not output.cancel_requested():
        chat(request['message'], history, output=output)
    linked = None
    # Both Governor routing and a direct conversational delegate can start work.
    while controller.status()['running']:
        state = controller.status()
        if state.get('run_dir') and state['run_dir'] != linked:
            linked = state['run_dir']
            output.emit('run.link', {'run_dir': linked})
        if output.cancel_requested():
            controller.interrupt('web_chat_cancel')
        thread = controller.thread
        if thread:
            thread.join(timeout=.2)
    state = controller.status()
    if state.get('run_dir'):
        output.emit('run.link', {'run_dir': state['run_dir']})
    if output.cancel_requested():
        return 'cancelled'
    if output.failed:
        return 'failed'
    if state.get('last_result') is not None:
        succeeded = state['last_result'] == 0
        text = ('本轮 Agent 执行已结束。' if succeeded else '本轮 Agent 执行未成功完成。') + '\n运行目录：' + str(state.get('run_dir') or '') + '\n完整交付以运行监控中的最终验收和发布结果为准。'
        output.write_response(text)
        return 'completed' if succeeded else 'failed'
    replies = [item for item in history[initial:] if item.get('role') == 'assistant' and item.get('content')]
    if consumed or not replies:
        output.write('未取得对话结果或任务未真正启动。', error=True)
        return 'failed'
    output.write_response(str(replies[-1]['content']))
    return 'completed'


def execute_turn(request: dict[str, Any], controller: Any, output: ChatOutput,
                 govern: Callable[..., bool], chat: Callable[..., None]) -> str:
    """All text turns use the same presentation receipt before terminal result."""
    from AstralNexus.runtime.presentation import Presentation, presentation_scope, output_inventory, delivery_receipt
    from AstralNexus.runtime.execution_store import ExecutionStore
    home = Path(str(request.get("home") or getattr(controller, "home", None) or Path.cwd())).resolve()
    home.mkdir(parents=True, exist_ok=True)
    outputs = home / "outputs"
    outputs.mkdir(exist_ok=True)
    workspace = Path(os.environ.get("ASTRALNEXUS_WORKSPACE") or Path.cwd())
    context = Presentation(home, outputs, workspace=workspace)
    before = output_inventory(outputs)
    with presentation_scope(context):
        status = _execute_turn(request, controller, output, govern, chat)
    if controller.status().get("run_dir"):
        return status  # RunApplicationService owns formal-run receipt/status.
    receipt = delivery_receipt(before, output_inventory(outputs), context.paths)
    ident = str(request.get("turn_id") or request.get("id") or home.name)
    try:
        receipt = ExecutionStore(home).record_delivery(ident, receipt)
        output.emit("run.delivery", receipt)
    except Exception as exc:
        if receipt["produced_paths"]:
            output.write("交付凭据未持久化: " + str(exc), error=True)
            return "failed"
    if status == "completed" and not receipt["satisfied"]:
        output.write("本轮生成了 outputs 文件但未调用 present_files 呈现", error=True)
        return "failed"
    return status


# ---------------------------------------------------------------------------
# publish a completed run's artifacts into the session workspace
# ---------------------------------------------------------------------------

# Framework publication metadata that lives beside the run's real products and
# must never be republished as a deliverable.
_FRAMEWORK_DELIVERABLE_NAMES = frozenset({'current.json', 'current'})
_FRAMEWORK_VERSION_DIRNAME = re.compile(r'^v\d+$')
# Same ignore rules the publisher applies in its own scan
# (runtime/workspace_publish.py: __pycache__, .DS_Store, *.pyc).
_PUBLISH_IGNORED_NAMES = frozenset({'__pycache__', '.DS_Store'})


def _framework_deliverable(name: str) -> bool:
    """True for the framework's own publication metadata inside deliverables/."""
    if name in _FRAMEWORK_DELIVERABLE_NAMES:
        return True
    return bool(_FRAMEWORK_VERSION_DIRNAME.match(name))


def _workspace_publish() -> Any:
    """Import the publisher lazily.

    ``AstralNexus/__init__.py`` is heavy and is only imported after
    ``bootstrap_environment()`` inside ``main()``; importing it at module level
    would run before bootstrap and outside the stdout redirect (which would
    corrupt the legacy stdin/stdout protocol).
    """
    from AstralNexus.runtime import workspace_publish
    return workspace_publish


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _deliverable_manifest(deliverables: Path) -> dict[str, Any]:
    """Explicit manifest of ``<run_dir>/deliverables`` minus framework metadata.

    ``rel`` values are relative to ``deliverables/`` -- the publisher's
    contract (``_load_entries`` resolves ``run_dir/'deliverables'/rel``).
    Sub-directories are scanned with the publisher's own ``build_manifest`` so
    its ignore rules stay authoritative.  The document digest is deliberately
    omitted: ``publish_deliverables`` recomputes it from the files block, so a
    filtered manifest can never carry a stale digest.
    """
    files: dict[str, dict[str, Any]] = {}
    if not deliverables.is_dir():
        return {'files': files}
    build_manifest = _workspace_publish().build_manifest
    for entry in sorted(deliverables.iterdir(), key=lambda item: item.name):
        name = entry.name
        if _framework_deliverable(name) or name in _PUBLISH_IGNORED_NAMES:
            continue
        if entry.is_dir() and not entry.is_symlink():
            nested = build_manifest(entry)
            for rel, meta in dict(nested.get('files') or {}).items():
                files[f'{name}/{rel}'] = meta
            continue
        if not entry.is_file() or entry.is_symlink():
            continue
        if name.endswith('.pyc'):
            continue
        files[name] = {'sha256': _sha256_file(entry), 'bytes': entry.stat().st_size}
    return {'files': files}


def _delivery_id(*, run_dir: Path, session: str, turn: str, run: str) -> str:
    """Deterministic id derived from the run, never from the wall clock.

    Repeating the delivery for an unchanged run therefore reuses the same
    publication area and resolves as ``skipped_identical`` instead of
    re-writing the same bytes.
    """
    identity = [str(run_dir.resolve()), str(run or run_dir.name or "run"), str(session), str(turn)]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return "run-" + digest[:20]



def _failure_reason(exc: BaseException) -> str:
    """Short, content-free reason string (type plus a bounded message)."""
    text = ' '.join(str(exc).split())
    if not text:
        return type(exc).__name__
    return f'{type(exc).__name__}: {text}'[:200]


def _delivery_summary(result: Any) -> dict[str, Any]:
    """Reduce a PublishResult to a JSON-safe summary without file contents."""
    published = tuple(result.published or ())
    skipped = tuple(result.skipped_identical or ())
    conflicts = tuple(result.conflicts or ())
    refused = tuple(result.refused or ())
    rolled_back = bool(result.rolled_back)
    resumed = bool(result.resumed)
    if rolled_back or refused or conflicts:
        status = 'refused'
        reason = 'rolled_back' if rolled_back else ('conflicts' if conflicts else 'manifest_refused')
    elif published or skipped:
        status = 'published'
        reason = 'published' if published else 'skipped_identical'
    else:
        # The publisher returned no publication and no explicit refusal: never
        # report that as a delivery.
        status = 'refused'
        reason = 'nothing_published'
    # Self-consistency of what was published: the tree root IS the old
    # ``deliverables/`` directory, so a delivered document that still writes
    # ``deliverables/...`` paths points at nothing.  Diagnostic only -- it never
    # blocks the publish (see ``workspace_publish.content_base_mismatches``).
    content_base: dict[str, Any] = {'mismatches': []}
    if result.published_root and (published or skipped):
        try:
            content_base = {
                'mismatches': _workspace_publish().content_base_mismatches(
                    Path(str(result.published_root)), published or skipped),
            }
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not mask the delivery
            content_base = {'mismatches': [],
                            'error': f'{type(exc).__name__}: {exc}'[:200]}
    return {
        'status': status,
        'reason': reason,
        'delivery_id': str(result.delivery_id),
        'published_root': str(result.published_root),
        'published': len(published),
        'skipped_identical': len(skipped),
        'conflicts': len(conflicts),
        'refused': len(refused),
        'manifest_sha256': str(result.manifest_sha256),
        'rolled_back': rolled_back,
        'resumed': resumed,
        'journal_path': str(result.journal_path),
        'content_base': content_base,
    }


def deliver_run_to_workspace(
    *,
    run_dir: Path | str,
    workspace_root: Path | str,
    session: str = '',
    turn: str = '',
    run: str = '',
) -> dict[str, Any]:
    """Publish one completed run's deliverables into the bound workspace area.

    Exception-proof boundary: any failure is reported as ``status == 'failed'``
    rather than escaping into the turn.  The returned summary carries counts,
    paths, hashes and a short reason -- never file contents.

    ``status`` is ``skipped`` (nothing to do), ``published`` (the tree is in
    place, possibly entirely ``skipped_identical``), ``refused`` (the publisher
    refused the whole order or rolled back) or ``failed``.
    """
    try:
        workspace = str(workspace_root or '').strip()
        if not workspace:
            return {'status': 'skipped', 'reason': 'no_workspace'}
        run_text = str(run_dir or '').strip()
        if not run_text:
            return {'status': 'skipped', 'reason': 'no_run_dir'}
        run_path = Path(run_text)
        if not run_path.is_dir():
            return {'status': 'skipped', 'reason': 'no_run_dir'}
        pointer = run_path / 'deliverables' / 'current.json'
        if not pointer.is_file():
            if not _deliverable_manifest(run_path / 'deliverables')['files']:
                return {'status': 'skipped', 'reason': 'no_deliverables'}
            return {'status': 'failed', 'reason': 'no_accepted_publication'}
        identity = json.loads(pointer.read_text(encoding='utf-8'))
        version_name = str(identity.get('path') or '')
        if not re.fullmatch(r'v[0-9]+', version_name):
            if not _deliverable_manifest(run_path / 'deliverables')['files']:
                return {'status': 'skipped', 'reason': 'no_deliverables'}
            return {'status': 'failed', 'reason': 'invalid_publication_pointer'}
        source_root = run_path / 'deliverables' / version_name
        from AstralNexus.core.publication_validation import inspect_publication
        verdict = inspect_publication(run_path, {'summary_json': str(source_root / 'delivery_summary.json')})
        if not verdict['complete']:
            return {'status': 'failed', 'reason': 'publication_not_accepted: ' + verdict['reason']}
        accepted = json.loads((source_root / 'delivery_manifest.json').read_text(encoding='utf-8'))
        gate = verdict.get("gate") or {}
        if gate.get("gate_policy_version") == "presented-output-v1":
            output_files = (gate.get("delivery_receipt") or {}).get("output_files", {})
            product_paths = {path if gate.get("publication_prefix") == "outputs" else path.removeprefix("outputs/") for path in output_files}
        else:
            product_paths = {row["path"] for row in accepted["artifacts"]}
        manifest = {'files': {row['path']: {'sha256': row['sha256'], 'bytes': row['size_bytes']}
                              for row in accepted['artifacts'] if row['path'] in product_paths and not _framework_deliverable(row['path'])}}
        if not manifest['files']:
            return {'status': 'skipped', 'reason': 'no_deliverables'}
        session_text = str(session or '')
        turn_text = str(turn or '')
        run_name = str(run or '')
        result = _workspace_publish().publish_deliverables(
            run_dir=run_path,
            workspace_root=Path(workspace),
            session=session_text,
            turn=turn_text,
            run=run_name,
            delivery_id=_delivery_id(
                run_dir=run_path, session=session_text, turn=turn_text, run=run_name
            ),
            manifest=manifest,
            source_root=source_root,
        )
        return _delivery_summary(result)
    except Exception as exc:
        return {'status': 'failed', 'reason': _failure_reason(exc)}


def deliver_completed_turn(
    *,
    request: dict[str, Any],
    controller: Any,
    emit: Callable[[str, dict[str, Any]], None],
    workspace_root: str = '',
) -> dict[str, Any]:
    """Deliver a finished turn's run artifacts and report the real outcome.

    Called only after a ``completed`` turn.  It never changes the turn status
    and never claims a delivery that did not land.

    ``workspace_root`` must be the SESSION workspace captured before the turn
    ran.  Reading ``ASTRALNEXUS_WORKSPACE`` here instead was wrong: the agent
    runtime rewrites that variable to the run dir for the tool write boundary
    (``cli/main.py:1296`` -- correct for tools, see
    ``tools/builtin_tools.py``), so the publisher inherited the redirect and
    published into the run dir.  Live fix4: 43 correct deliverables landed in
    ``<run_dir>/.astralnexus_delivery/<id>/published`` while the session
    workspace stayed empty, so R1 saw no publication at all.
    """
    try:
        run_dir = str(controller.status().get('run_dir') or '')
    except Exception:
        run_dir = ''
    try:
        summary = deliver_run_to_workspace(
            run_dir=run_dir,
            workspace_root=workspace_root or os.environ.get('ASTRALNEXUS_WORKSPACE', ''),
            session=os.environ.get('ASTRALNEXUS_CHAT_SESSION', ''),
            turn=Path(str(request.get('home') or '')).name,
            run=Path(run_dir).name if run_dir else '',
        )
    except Exception as exc:
        summary = {'status': 'failed', 'reason': _failure_reason(exc)}
    emit('progress', {'type': 'delivery.publish', **summary})
    if summary.get('status') in {'refused', 'failed'}:
        emit('progress', {
            'type': 'delivery.publish.refused',
            'reason': str(summary.get('reason') or summary.get('status')),
        })
    return summary


def _open_channel() -> tuple[Any, Any, Any]:
    """Select the JSON-lines transport.

    ``ASTRALNEXUS_CHAT_CHANNEL`` (``host:port``) switches to the loopback TCP
    carrier the web host now always provides — anonymous pipes are refused
    for spawned children under this host's sandbox (CreatePipe WinError 5),
    while 127.0.0.1 sockets are the proven carrier. Without the variable the
    legacy stdin/stdout form is kept for CLI and direct-protocol callers.
    """
    channel = os.environ.get('ASTRALNEXUS_CHAT_CHANNEL', '').strip()
    if not channel:
        return sys.stdin, sys.stdout, None
    import socket
    host, _, port = channel.rpartition(':')
    connection = socket.create_connection((host or '127.0.0.1', int(port)), timeout=30)
    connection.settimeout(None)
    inbound = connection.makefile('r', encoding='utf-8', errors='replace')
    outbound = connection.makefile('w', encoding='utf-8', newline='\n')
    return inbound, outbound, connection


def main() -> None:
    inbound, protocol, connection = _open_channel()
    write_lock = threading.Lock()
    def emit(kind: str, payload: dict[str, Any]) -> None:
        with write_lock:
            protocol.write(json.dumps({'kind': kind, 'payload': payload}, ensure_ascii=False) + '\n')
            protocol.flush()
    request = json.loads(inbound.readline())
    cancel = threading.Event()
    output = ChatOutput(emit, cancel)
    with contextlib.redirect_stdout(sys.stderr):
        from AstralNexus.core.identity import bootstrap_environment
        bootstrap_environment()
        from AstralNexus.cli.main import (
            _InteractiveRunController, _register_interactive_delegate_tool,
            _govern_interactive_turn, _interactive_chat_turn,
        )
        from AstralNexus.tools.approval import set_approval_provider
        controller = _InteractiveRunController(Path(request['home']))
        controller.set_event_callback(output.event)
        _register_interactive_delegate_tool(controller, Path(request['home']))
        answers: dict[str, str] = {}
        condition = threading.Condition()

        def receive() -> None:
            try:
                for line in inbound:
                    value = json.loads(line)
                    if value.get('type') == 'cancel':
                        cancel.set()
                        controller.interrupt('web_chat_cancel')
                    elif value.get('type') == 'approval':
                        with condition:
                            answers[value['id']] = value['choice']
                            condition.notify_all()
            finally:
                cancel.set()
                controller.interrupt('web_host_disconnected')
                with condition:
                    condition.notify_all()

        def approval(tool_name: str, arguments: dict[str, Any], caller: str, basis: str) -> str:
            ident = uuid.uuid4().hex
            emit('approval.request', {'id': ident, 'tool': tool_name, 'arguments': {key: arguments[key] for key in ('path', 'cwd') if key in arguments},
                                     'description': f'{caller} · {basis}',
                                     'command': str(arguments.get('command') or arguments.get('path') or arguments.get('url') or tool_name)[:4000]})
            with condition:
                while ident not in answers and not cancel.is_set():
                    condition.wait(timeout=.2)
                return answers.pop(ident, 'deny')

        set_approval_provider(approval)
        threading.Thread(target=receive, daemon=True).start()
        # Capture the session workspace BEFORE the turn runs.  The agent
        # runtime rewrites ASTRALNEXUS_WORKSPACE to the run dir (cli/main.py
        # :1296) because the tool layer uses it as the write boundary; that is
        # correct for tools and must not be undone.  The publisher, however,
        # must publish into the session workspace, so it gets this captured
        # value rather than re-reading the mutated variable.
        session_workspace = os.environ.get('ASTRALNEXUS_WORKSPACE', '')
        try:
            status = execute_turn(request, controller, output, _govern_interactive_turn, _interactive_chat_turn)
        except Exception as exc:
            output.write(str(exc), error=True)
            controller.interrupt('web_chat_error')
            status = 'failed'
        if status == 'completed':
            # Delivery is reported, never assumed: a refused or failed publish
            # leaves the turn status untouched and says so on the stream.
            publication = deliver_completed_turn(request=request, controller=controller, emit=emit,
                                                 workspace_root=session_workspace)
            if publication.get("status") in {"failed", "refused"}:
                status = "failed"
        emit('turn.result', {'status': status})
    if connection is not None:
        try:
            connection.close()
        except OSError:
            pass
    raise SystemExit(0 if status == 'completed' else 1)


if __name__ == '__main__':
    main()
