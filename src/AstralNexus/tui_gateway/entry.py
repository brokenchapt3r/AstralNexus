"""JSON-RPC adapter between the vendored Ink TUI and AstralNexus."""
from __future__ import annotations

import json
import contextlib
import io
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from AstralNexus.tui_gateway.session_store import SessionStore

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_WRITE_LOCK = threading.RLock()


def _write(message: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        from AstralNexus.runtime.console_capture import protocol_stdout
        stream = protocol_stdout()
        stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        stream.flush()


def _result(request_id: Any, value: Any) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": value})


def _error(request_id: Any, message: str, code: int = -32000) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _event(event_type: str, payload: dict[str, Any] | None = None, *, session_id: str | None = None) -> None:
    from AstralNexus.tui_gateway.ux_events import envelope
    params: dict[str, Any] = {"type": event_type}
    params['ux'] = (payload or {}).get('ux') or envelope(event_type, payload or {}, session_id)
    if payload is not None:
        params["payload"] = payload
    if session_id:
        params["session_id"] = session_id
    _write({"jsonrpc": "2.0", "method": "event", "params": params})


def _selected_model() -> str:
    try:
        from AstralNexus.runtime.model_selector import get_selected_model

        return get_selected_model() or "qwen-flash"
    except Exception:
        return os.environ.get("ASTRALNEXUS_MODEL", "qwen-flash")


def _provider_status() -> dict[str, Any]:
    from AstralNexus.runtime.run_context import RunSettings
    from AstralNexus.runtime.provider_utils import API_KEY_ENV_VARS
    settings = RunSettings.capture()
    configured = any(settings.environment.get(name, '').strip() for name in API_KEY_ENV_VARS)
    return {'provider_configured': configured, 'credentials_verified': False,
            'message': '' if configured else '尚未配置模型 API key；历史、计划和文件审查仍可使用。'}


def _skin() -> dict[str, Any]:
    return {
        "colors": {
            "ui_primary": "#55d7ff",
            "ui_accent": "#8eeaff",
            "ui_border": "#56738f",
            "ui_text": "#e6f6ff",
            "ui_label": "#55d7ff",
            "ui_ok": "#6ee7a2",
            "ui_warn": "#ffd166",
            "ui_error": "#ff6b81",
            "banner_title": "#55d7ff",
            "banner_accent": "#8eeaff",
            "banner_border": "#56738f",
            "banner_text": "#e6f6ff",
            "banner_dim": "#7f91ad",
            "prompt": "#8eeaff",
        },
        "branding": {
            "agent_name": "AstralNexus",
            "prompt_symbol": "❯",
            "welcome": "直接输入消息，或输入 /help 查看命令。",
            "goodbye": "天枢会话已结束。",
            "help_header": "天枢 / AstralNexus",
        },
        "tool_prefix": "天枢工具",
        "help_header": "天枢 / AstralNexus",
    }


def _usage() -> dict[str, Any]:
    return {
        "calls": 0,
        "compressions": 0,
        # Unknown until a model-context event supplies the actual limit.
        "context_max": 0,
        "context_percent": 0,
        "context_used": 0,
        "input": 0,
        "output": 0,
        "total": 0,
    }


def _runtime_capabilities() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Expose the same registry and progressive Skill catalog used at runtime."""
    try:
        from AstralNexus.core.skill_runtime import discover_skills
        from AstralNexus.tools.registry import discover_builtin_tools, registry

        discover_builtin_tools()
        tools = {
            toolset: registry.get_tool_names_for_toolset(toolset)
            for toolset in registry.get_registered_toolset_names()
        }
        available_tools = set(registry.get_all_tool_names())
        skills: dict[str, list[str]] = {}
        for item in discover_skills(
            available_tools=available_tools,
            toolsets=set(tools),
        ):
            skills.setdefault(str(item.get("category") or "general"), []).append(
                str(item.get("name") or "unknown")
            )
        return tools, skills
    except Exception:
        return {}, {}


@dataclass
class Session:
    session_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=_usage)
    running: bool = False
    started_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)
    queued: queue.Queue[str] = field(default_factory=queue.Queue)
    title: str = "天枢会话"
    workspace: str = field(default_factory=lambda: str(Path.cwd().resolve()))
    run_dir: str = ""
    store: SessionStore | None = field(default=None, repr=False)
    review: Any = field(default=None, repr=False)
    event_sink: Callable[[str, dict[str, Any] | None], None] | None = field(default=None, repr=False)

    def emit(self, kind: str, payload: dict[str, Any] | None = None, *, session_id: str | None = None) -> None:
        from AstralNexus.tui_gateway.ux_events import envelope
        if self.store is not None:
            payload = {**(payload or {}), 'ux': envelope(kind, payload or {}, self.session_id, self.run_dir)}
            if kind != 'session.info':
                self.store.append(self.session_id, kind, payload)
            else:
                self.store.save(self)
        if self.event_sink is not None:
            self.event_sink(kind, payload)
        else:
            _event(kind, payload, session_id=self.session_id)

    def info(self) -> dict[str, Any]:
        tools, skills = _runtime_capabilities()
        model = _selected_model()
        if self.store is not None:
            from AstralNexus.runtime.session_settings import SessionSettings
            model = SessionSettings(self.store.host).models(self.session_id)['effective_main']
        return {
            "credential_warning": _provider_status()["message"],
            "capabilities": {"images": False, "clipboard_images": False, "voice": False, "pets": False},
            "cwd": self.workspace,
            "model": model,
            "profile_name": "天枢 Runtime",
            "skills": skills,
            "tools": tools,
            "usage": dict(self.usage),
            "version": _product_version(),
        }


def _product_version() -> str:
    from AstralNexus.core.identity import VERSION

    return VERSION


class UnsupportedMethodError(ValueError):
    def __init__(self, method: str):
        super().__init__(f"unsupported Gateway method: {method}")


def _tool_batch_identity(event: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(event.get("run_id") or ""), str(event.get("node_id") or ""),
            str(event.get("execution_id") or ""), str(event.get("tool_round") or ""))


class Gateway:
    def __init__(self, *, approval_timeout: float = 120, home: Path | None = None) -> None:
        from AstralNexus.cli.main import (
            _InteractiveRunController,
            _register_interactive_delegate_tool,
        )
        from AstralNexus.core.identity import bootstrap_environment

        self.sessions: dict[str, Session] = {}
        self.current_session_id: str | None = None
        self._approval_lock = threading.Condition()
        self._approvals: dict[str, dict[str, Any]] = {}
        self._approval_timeout = approval_timeout
        self._disconnected = False
        self._controller_session_id: str | None = None
        self.home = Path(home) if home is not None else bootstrap_environment()
        self.store = SessionStore(self.home)
        for row in self.store.list_sessions():
            session = Session(row['id'], history=row['history'], usage=row['usage'], started_at=row['started'],
                title=row['title'], workspace=row['workspace'], run_dir=row['run_dir'], store=self.store)
            self.sessions[session.session_id] = session
            from AstralNexus.runtime.workspace_review import WorkspaceReview
            session.review = WorkspaceReview(self.store.host, session.session_id)
            if row['interrupted']:
                self.store.append(session.session_id, 'runtime.event', {'title': '上次会话已中断',
                    'summary': '历史记录尚无本轮结束结论；已恢复历史，未自动重新执行。请核对运行状态。'})
        self.current_session_id = next(iter(self.sessions), None)
        with self.store.host.db() as db:
            selected = db.execute("SELECT value FROM settings WHERE key='tui:selected'").fetchone()
            if selected and selected[0] in self.sessions:
                self.current_session_id = selected[0]
        self.controller = _InteractiveRunController(self.home)
        _register_interactive_delegate_tool(self.controller, self.home)

    def session(self, session_id: str | None) -> Session:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        if session_id:
            raise ValueError("session not found")
        if self.current_session_id and self.current_session_id in self.sessions:
            return self.sessions[self.current_session_id]
        raise ValueError("session not found")

    def create_session(self) -> Session:
        session = Session(session_id=f"astral-{uuid.uuid4().hex[:12]}", store=self.store)
        self.sessions[session.session_id] = session
        self.current_session_id = session.session_id
        self.store.save(session)
        from AstralNexus.runtime.workspace_review import WorkspaceReview
        session.review = WorkspaceReview(self.store.host, session.session_id)
        self._select_session(session.session_id)
        return session

    def _select_session(self, ident: str) -> None:
        self.current_session_id = ident
        with self.store.host.db() as db:
            db.execute("INSERT INTO settings VALUES('tui:selected',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ident,))

    def approval_provider(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        caller: str,
        basis: str,
        *, session_id: str | None = None,
    ) -> str:
        command = str(
            arguments.get("command")
            or arguments.get("path")
            or arguments.get("url")
            or json.dumps(arguments, ensure_ascii=False)
        )
        with self._approval_lock:
            sid = session_id or self.current_session_id
            if self._disconnected or sid not in self.sessions:
                return "deny"
            session = self.sessions[sid]
            from AstralNexus.tools.context import get_tool_workspace, get_tool_owned_paths
            from AstralNexus.runtime.execution_fence import current_execution_identity
            root = get_tool_workspace() or Path(session.workspace)
            execution_id = current_execution_identity(root)
            owned_paths = list(get_tool_owned_paths())
            request_id = uuid.uuid4().hex
            request = {"session_id": sid, "choice": None, "shown": False, "payload": {
                    "request_id": request_id,
                    "workspace": str(root.resolve()),
                    "execution_id": execution_id or None,
                    "owned_paths": owned_paths,
                    "caller": caller,
                    "allow_permanent": False,
                    "choices": ["once", "session", "deny"],
                    "command": command[:4000],
                    "description": f"{tool_name} · {caller} · {basis}\n工作区：{root.resolve()}\n执行：{execution_id or '未提供执行身份'}\n写入归属：{', '.join(owned_paths) or '无 Worker 路径声明'}",
                }}
            self._approvals[request_id] = request
            self._show_next_approval()
            self._approval_lock.wait_for(lambda: request["choice"] is not None, timeout=self._approval_timeout)
            self._approvals.pop(request_id, None)
            if request["choice"] is None:
                session.emit('approval.expire', {'request_id': request_id})
            self._show_next_approval()
            return str(request["choice"] or "deny")

    def _show_next_approval(self) -> None:
        pending = next((item for item in self._approvals.values() if item["choice"] is None), None)
        if pending is not None and not pending["shown"]:
            pending["shown"] = True
            self.sessions[pending['session_id']].emit('approval.request', pending['payload'])

    def deny_pending_approvals(self, session_id: str | None = None) -> None:
        with self._approval_lock:
            for request_id, item in self._approvals.items():
                if session_id is None or item["session_id"] == session_id:
                    item["choice"] = "deny"
                    session = self.sessions.get(item['session_id'])
                    if session is not None: session.emit('approval.expire', {'request_id': request_id})
            self._approval_lock.notify_all()

    def disconnect(self) -> None:
        with self._approval_lock:
            self._disconnected = True
            self.deny_pending_approvals()
        for session in self.sessions.values():
            session.cancel.set()
        if self.controller.status()['running']:
            self.controller.interrupt('native_tui_disconnect')

    def _session_running(self, session: Session) -> bool:
        return session.running or (self._controller_session_id == session.session_id and bool(self.controller.status()['running']))

    def respond_approval(self, choice: str, *, request_id: str = "", session_id: str = "") -> bool:
        aliases = {
            "allow_once": "once",
            "allow_session": "session",
            "allow_always": "always",
            "reject": "deny",
        }
        with self._approval_lock:
            # Older clients may omit identity only when exactly one request exists.
            if not request_id:
                unresolved = [key for key, item in self._approvals.items() if item["choice"] is None]
                if len(unresolved) != 1:
                    return False
                request_id = unresolved[0]
            item = self._approvals.get(request_id)
            resolved = aliases.get(choice, choice)
            if (item is None or not item["shown"] or item["choice"] is not None
                    or (session_id and item["session_id"] != session_id)
                    or resolved not in {"once", "session", "deny"}):
                return False
            item["choice"] = resolved
            self._approval_lock.notify_all()
            return True

    def submit(self, session: Session, text: str) -> None:
        if session.running:
            session.queued.put(text)
            return
        session.running = True
        self.store.save(session)
        session.cancel.clear()
        threading.Thread(
            target=self._run_turn,
            args=(session, text),
            name=f"AstralNexusTurn-{session.session_id}",
            daemon=True,
        ).start()

    def _dispatch_slash(
        self,
        command: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute one command through the canonical command/control services."""
        from AstralNexus.cli.commands import resolve_command

        raw = str(command or "").strip()
        name = raw.lstrip("/").partition(" ")[0].lower()
        canonical = resolve_command(name)
        if canonical is None:
            return {"code": 1, "output": f"未知命令: /{name}", "ok": False}

        session = self.session(session_id) if session_id else self.session(None)
        if self.controller.status()['running'] and self._controller_session_id != session.session_id:
            return {'ok': False, 'code': 1, 'output': '另一会话拥有正在执行的任务；请在该会话中控制或等待结束。'}
        self._controller_session_id = session.session_id
        output = GatewayOutput(session)
        self.controller.set_event_callback(output.event)
        interactive = self.controller.dispatch_command(
            "/" + raw.lstrip("/"),
            history=session.history,
        )
        if interactive.get("handled"):
            return {
                "code": int(interactive.get("code", 0)),
                "output": str(interactive.get("output") or ""),
                "ok": bool(interactive.get("ok", False)),
                **({"exit": True} if interactive.get("exit") else {}),
            }

        detached = canonical in {"eval", "dashboard"}
        code, text = self._execute_command_handler(
            "/" + str(command).lstrip("/"),
            detached=detached,
        )
        return {
            "code": code,
            "output": text or "(命令执行完成，无文本输出)",
            "ok": code == 0,
        }

    def _execute_command_handler(
        self,
        command: str,
        *,
        detached: bool,
    ) -> tuple[int, str]:
        """Run a command through the one canonical handler, in this process.

        The gateway owns JSON-RPC stdout, so handler output is captured and
        returned as a protocol event instead of starting a second CLI process.
        Long-running commands use a Python worker thread while retaining the
        same command registry and runtime home.
        """
        from AstralNexus.cli.commands import process_command

        owner_session_id = self.current_session_id

        def invoke() -> tuple[int, str]:
            stdout = io.StringIO()
            stderr = io.StringIO()
            from AstralNexus.runtime.console_capture import capture_console
            from AstralNexus.runtime.run_context import RunContext, RunSettings
            context = RunContext(Path.cwd(), RunSettings.capture(environment={**os.environ,
                'ASTRALNEXUS_HOME': str(self.home)}), scope_id='command:' + str(owner_session_id or ''))
            try:
                with context.scope(), capture_console(stdout, stderr):
                    try:
                        code = int(process_command(command, home=self.home) or 0)
                    except SystemExit as exc:
                        code = int(exc.code or 0)
            except Exception as exc:
                code = 1
                stderr.write(str(exc))
            text = "\n".join(
                item.strip() for item in (stdout.getvalue(), stderr.getvalue()) if item.strip()
            )
            return code, text

        if not detached:
            return invoke()

        command_name = command.lstrip("/").split(" ", 1)[0]

        def run_detached() -> None:
            code, output = invoke()
            _event(
                "command.complete",
                {"command": command_name, "code": code, "output": output},
                session_id=owner_session_id,
            )

        thread = threading.Thread(
            target=run_detached,
            name=f"AstralNexusCommand-{command_name}",
            daemon=True,
        )
        thread.start()
        return 0, f"命令已在天枢 Runtime 后台启动: /{command_name}"

    def _run_turn(self, session: Session, text: str) -> None:
        from AstralNexus.cli.main import _govern_interactive_turn, _interactive_chat_turn
        from AstralNexus.tools.approval import approval_scope
        from AstralNexus.runtime.run_context import RunContext, RunSettings
        from AstralNexus.runtime.workspace_review import review_scope
        from functools import partial

        output = GatewayOutput(session)
        before = len(session.history)
        scope = contextlib.ExitStack()
        persistence_ok = True
        try:
            self.store.append(session.session_id, 'user.message', {'text': text})
            session.emit("message.start", session_id=session.session_id)
            session.emit(
                "status.update",
                {"kind": "status", "text": "正在分析请求并选择下一步…"},
                session_id=session.session_id,
            )
            if self.controller.status()['running'] and self._controller_session_id != session.session_id:
                raise ValueError('another session owns the active background task')
            if not self.controller.status()['running']:
                from AstralNexus.runtime.user_checkpoint import UserCheckpoints
                checkpoint = UserCheckpoints(session.review).create(session.history, '执行前：' + text[:80])
                session.emit('runtime.event', {'title': '建立用户恢复点', 'summary': checkpoint['id'], 'status': 'checkpoint_ready'})
            self._controller_session_id = session.session_id
            from AstralNexus.runtime.session_settings import SessionSettings
            selected = SessionSettings(self.store.host).models(session.session_id)
            environment = dict(os.environ, ASTRALNEXUS_MODEL=selected['effective_main'], ASTRALNEXUS_SUB_MODEL=selected['effective_sub'])
            run_context = RunContext(Path(session.workspace), RunSettings.capture(environment=environment), scope_id="tui:" + session.session_id)
            scope.enter_context(run_context.scope())
            scope.enter_context(review_scope(session.review))
            scope.enter_context(approval_scope(run_context.approval_identity,
                provider=partial(self.approval_provider, session_id=session.session_id), bind_provider=True))
            self.controller.set_event_callback(output.event)
            consumed = _govern_interactive_turn(
                text,
                session.history,
                self.controller,
                output=output,
            )
            if not consumed:
                _interactive_chat_turn(text, session.history, output=output)
            if self.controller.status().get('run_dir'):
                session.run_dir = str(self.controller.status()['run_dir'])
            answer = ""
            if len(session.history) > before and session.history[-1].get("role") == "assistant":
                answer = session.history[-1].get("content", "")
            session.emit(
                "message.complete",
                {"text": answer, "usage": dict(session.usage)},
                session_id=session.session_id,
            )
        except Exception as exc:
            failure_events: list[tuple[str, dict[str, Any]]] = [('error', {'message': str(exc)}),
                              ('message.complete', {'text': '', 'usage': dict(session.usage), 'status': 'failed'})]
            for kind, payload in failure_events:
                try:
                    session.emit(kind, payload, session_id=session.session_id)
                except sqlite3.Error:
                    persistence_ok = False
                    _event(kind, {**payload, 'persisted': False}, session_id=session.session_id)
        finally:
            try:
                scope.__exit__(None, None, None)
            finally:
                session.running = False
            try:
                self.store.save(session)
            except sqlite3.Error as storage_error:
                persistence_ok = False
                _event('error', {'message': '会话持久化失败，排队消息未执行：' + str(storage_error), 'persisted': False}, session_id=session.session_id)
            if persistence_ok:
                try:
                    session.emit("session.info", session.info(), session_id=session.session_id)
                except sqlite3.Error as storage_error:
                    persistence_ok = False
                    _event('error', {'message': '会话状态无法保存：' + str(storage_error), 'persisted': False}, session_id=session.session_id)
            if persistence_ok:
                try:
                    queued = session.queued.get_nowait()
                except queue.Empty:
                    queued = ""
                if queued:
                    self.submit(session, queued)

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "setup.status":
            return _provider_status()
        if method == "config.get":
            from AstralNexus.tui_gateway.configuration import PresentationSettings
            display = PresentationSettings(self.store.host).get()
            key = str(params.get('key') or 'full')
            if key == 'mtime':
                with self.store.host.db() as db:
                    row = db.execute("SELECT value FROM settings WHERE key='tui:config_mtime'").fetchone()
                return {'mtime': float(row[0]) if row else 0}
            return {'config': {'display': display, 'capabilities': {'images': False, 'clipboard_images': False, 'voice': False, 'pets': False}}} if key == 'full' else {'value': display.get(key)}
        if method == "config.set":
            from AstralNexus.tui_gateway.configuration import PresentationSettings
            key, value = str(params.get('key') or ''), params.get('value')
            if key == 'model':
                import shlex
                from AstralNexus.runtime.session_settings import SessionSettings
                session = self.session(str(params.get('session_id') or ''))
                words = shlex.split(str(value))
                if not words or '--global' in words: raise ValueError('select a session model; global mutation is not supported')
                if '--provider' in words:
                    from AstralNexus.runtime.model_selector import get_model_profile
                    index = words.index('--provider')
                    if index + 1 >= len(words) or words[index + 1] != get_model_profile(words[0])['provider']:
                        raise ValueError('provider does not match configured model profile')
                    del words[index:index+2]
                if any(word not in {'--session', '--tui-session'} for word in words[1:]): raise ValueError('unknown model options')
                service = SessionSettings(self.store.host)
                selected = service.models(session.session_id)
                service.set_models(session.session_id, {'main_model': words[0], 'sub_model': selected['sub_model']})
                return {'value': words[0], 'info': session.info(), 'warning': '模型设置在下一轮生效' if session.running else ''}
            display = PresentationSettings(self.store.host).set(key, value)
            return {'value': value, 'display': display}
        if method in {'models.get', 'models.set'}:
            from AstralNexus.runtime.session_settings import SessionSettings
            session = self.session(str(params.get('session_id') or ''))
            service = SessionSettings(self.store.host)
            return service.models(session.session_id) if method == 'models.get' else service.set_models(session.session_id, params)
        if method == 'model.options':
            from AstralNexus.runtime.model_selector import AVAILABLE_MODELS, get_model_config
            config = get_model_config()
            models = [key for key, value in AVAILABLE_MODELS.items() if value['provider'] == config['provider']]
            session = self.session(str(params.get('session_id') or ''))
            return {'model': session.info()['model'], 'provider': config['provider'], 'providers': [{
                'slug': config['provider'], 'name': config['provider'], 'authenticated': _provider_status()['provider_configured'],
                'models': models, 'is_current': True}]}
        if method == "commands.catalog":
            from AstralNexus.cli.commands import command_catalog

            catalog = command_catalog()
            catalog["pairs"].append(["/agents", "查看各节点的工具调用与阶段结果"])
            for name, help_text in [('plan', '计划与阻塞'), ('evidence', '来源与验收证据'),
                ('delivery', '正式交付门'), ('changes', '审查文件修改'), ('restore', '停止任务后撤销指定文件修改'), ('accept', '接受已审查的指定文件版本'), ('reject', '拒绝并撤销指定文件修改'), ('checkpoint', '建立当前用户恢复点'), ('checkpoints', '列出可用用户恢复点')]:
                catalog['pairs'].append(['/' + name, help_text])
            _, skills = _runtime_capabilities()
            catalog["skill_count"] = sum(len(items) for items in skills.values())
            return catalog
        if method == "complete.slash":
            text = str(params.get("text") or "").lower()
            pairs = self.dispatch("commands.catalog", {})["pairs"]
            items = [
                {"text": name, "display": name, "meta": description}
                for name, description in pairs
                if name.lower().startswith(text)
            ]
            return {"items": items, "replace_from": 1}
        if method == "complete.path":
            word = str(params.get("word") or "")
            raw = word.strip("\"'")
            path = Path(raw).expanduser()
            parent = path.parent if str(path.parent) not in {"", "."} else Path.cwd()
            prefix = path.name.lower()
            items = []
            if parent.is_dir():
                for candidate in sorted(parent.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:80]:
                    if candidate.name.lower().startswith(prefix):
                        suffix = os.sep if candidate.is_dir() else ""
                        items.append(
                            {
                                "text": str(candidate) + suffix,
                                "display": candidate.name + suffix,
                                "meta": "目录" if candidate.is_dir() else "文件",
                            }
                        )
            return {"items": items}
        if method == "delegation.status":
            status = self.controller.status()
            active = []
            if status['running'] and status.get('run_dir'):
                from AstralNexus.runtime.authoritative_projection import read_current_projection
                projection = read_current_projection(Path(status['run_dir']))
                if projection.get('verified'):
                    active = [{'subagent_id': node['node_id'], 'goal': node.get('goal', ''), 'status': node['status']}
                        for node in projection['graph']['nodes'] if node['status'] == 'running']
            return {'active': active, 'node_control_available': False, 'running': [status] if status['running'] else []}
        if method == "session.create":
            session = self.create_session()
            return {"session_id": session.session_id, "info": session.info()}
        if method == "session.close":
            requested = str(params.get("session_id") or self.current_session_id or "")
            self.deny_pending_approvals(requested)
            if requested in self.sessions:
                self.sessions[requested].cancel.set()
            if requested == self._controller_session_id and self.controller.status()['running']:
                self.controller.interrupt('native_tui_session_close')
            closed = self.sessions.pop(requested, None) is not None
            if self.current_session_id == requested:
                self.current_session_id = next(iter(self.sessions), None)
            return {"closed": closed, "ok": closed}
        if method == "session.active_list":
            return {
                "sessions": [
                    {
                        "id": item.session_id,
                        "current": item.session_id == self.current_session_id,
                        "last_active": time.time(),
                        "message_count": len(item.history),
                        "model": _selected_model(),
                        "started_at": item.started_at,
                        "status": "working" if self._session_running(item) else "idle",
                        "title": item.title,
                    }
                    for item in self.sessions.values()
                ]
            }
        if method == "session.most_recent":
            return {"session_id": self.current_session_id}
        if method in {"session.resume", "session.activate"}:
            ident = str(params.get("session_id") or "")
            if ident and ident not in self.sessions:
                row = self.store.load(ident)
                self.sessions[ident] = Session(ident, history=row['history'], usage=row['usage'], started_at=row['started'],
                    title=row['title'], workspace=row['workspace'], run_dir=row['run_dir'], store=self.store)
                from AstralNexus.runtime.workspace_review import WorkspaceReview
                self.sessions[ident].review = WorkspaceReview(self.store.host, ident)
            session = self.session(ident)
            self._select_session(session.session_id)
            messages = [
                {"role": item["role"], "text": item.get("content", "")}
                for item in session.history
                if item.get("role") in {"user", "assistant", "system", "tool"}
            ]
            messages = self.store.transcript(session.session_id) or messages
            return {
                "session_id": session.session_id,
                "messages": messages,
                "spawn_trees": self.store.spawn_trees(session.session_id),
                "message_count": len(messages),
                "running": self._session_running(session),
                "status": "working" if self._session_running(session) else "idle",
                "started_at": session.started_at,
                "info": session.info(),
            }
        if method in {"session.status", "session.usage"}:
            session = self.session(str(params.get("session_id") or ""))
            return {
                "running": self._session_running(session),
                "status": "working" if self._session_running(session) else "idle",
                "usage": dict(session.usage),
                "info": session.info(),
            }
        if method == "session.title":
            session = self.session(str(params.get("session_id") or ""))
            session.title = str(params.get("title") or "天枢会话")
            self.store.save(session)
            return {"title": session.title, "pending": False}
        if method == "session.interrupt":
            session = self.session(str(params.get("session_id") or ""))
            session.cancel.set()
            self.deny_pending_approvals(session.session_id)
            if self.controller.status()["running"] and self._controller_session_id == session.session_id:
                self.controller.interrupt("native_tui_interrupt")
            return {"ok": True}
        if method == "session.steer":
            session = self.session(str(params.get("session_id") or ""))
            text = str(params.get('text') or '').strip()
            if not text: raise ValueError('steer text is required')
            if self.controller.status()['running']:
                if self._controller_session_id != session.session_id:
                    raise ValueError('another session owns the active background task')
                self.controller.inject_requirement_text(text)
                session.emit('runtime.event', {'title': '用户调整任务方向', 'summary': text, 'status': 'queued'})
                return {'status': 'queued', 'text': text}
            if not session.running:
                raise ValueError('no active turn to steer; submit a new message')
            session.queued.put(text)
            session.cancel.set()
            return {"status": "queued", "text": str(params.get("text") or "")}
        if method == "prompt.submit":
            session = self.session(str(params.get("session_id") or ""))
            self.submit(session, str(params.get("text") or ""))
            return {"ok": True}
        if method == "input.detect_drop":
            text = str(params.get("text") or "")
            candidate = Path(text.strip().strip("\"'"))
            if candidate.exists():
                return {
                    "matched": True,
                    "is_image": candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"},
                    "name": candidate.name,
                    "text": text,
                }
            return {"matched": False}
        if method == "approval.respond":
            return {"ok": self.respond_approval(str(params.get("choice") or "deny"),
                request_id=str(params.get("request_id") or ""),
                session_id=str(params.get("session_id") or ""))}
        if method == "session.events":
            session = self.session(str(params.get("session_id") or ""))
            return {"events": self.store.events(session.session_id, int(params.get("after") or 0))}
        if method == "inspection.get":
            from AstralNexus.tui_gateway.inspection import inspect_session
            return inspect_session(self.session(str(params.get("session_id") or "")), str(params.get("kind") or "plan"))
        if method in {'checkpoints.create', 'checkpoints.list', 'checkpoints.restore'}:
            from AstralNexus.runtime.user_checkpoint import UserCheckpoints
            session = self.session(str(params.get('session_id') or ''))
            checkpoints = UserCheckpoints(session.review)
            if method == 'checkpoints.list': return {'checkpoints': checkpoints.list()}
            if self._session_running(session) or self.controller.status()['running']:
                raise ValueError('stop the active run before creating or restoring user checkpoints')
            if method == 'checkpoints.create':
                return checkpoints.create(session.history, str(params.get('label') or '用户恢复点'))
            ident = str(params.get('checkpoint_id') or '')
            with session.review.lock:
                result = checkpoints.restore(ident)
                key = 'user-restore-fork:' + session.session_id + ':' + ident
                with self.store.host.db() as db:
                    existing = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                if existing:
                    return {**result, 'session_id': existing[0], 'original_session_id': session.session_id}
                fork = Session(session_id='astral-' + uuid.uuid4().hex[:12], history=result['history'],
                               title='恢复：' + result['label'], workspace=session.workspace, store=self.store)
                self.store.save(fork)
                from AstralNexus.runtime.session_settings import SessionSettings
                model_settings = SessionSettings(self.store.host)
                parent_models = model_settings.models(session.session_id)
                model_settings.set_models(fork.session_id, {key: parent_models[key] for key in ('main_model', 'sub_model')})
                from AstralNexus.runtime.workspace_review import WorkspaceReview
                fork.review = WorkspaceReview(self.store.host, fork.session_id)
                with self.store.host.db() as db:
                    db.execute('INSERT INTO settings(key,value) VALUES(?,?)', (key, fork.session_id))
                self.sessions[fork.session_id] = fork
                session.emit('runtime.event', {'title': '用户恢复文件并创建对话分支',
                    'summary': f"checkpoint={ident}; fork={fork.session_id}; 原始会话、收据和运行状态保留。", 'status': 'restored'})
                return {**result, 'session_id': fork.session_id, 'original_session_id': session.session_id}
        if method == "changes.list":
            session = self.session(str(params.get("session_id") or ""))
            return {"changes": session.review.changes()}
        if method in {"changes.restore", "changes.accept"}:
            session = self.session(str(params.get("session_id") or ""))
            if session.running or self.controller.status()['running']:
                raise ValueError('stop the active run before restoring files')
            result = (session.review.accept if method == 'changes.accept' else session.review.restore)(str(params.get('change_id') or ''))
            session.emit('runtime.event', {'title': '用户接受文件版本' if method == 'changes.accept' else '用户撤销文件修改',
                'summary': str(result), 'status': 'accepted' if method == 'changes.accept' else 'restored'})
            return result
        if method == 'spawn_tree.save':
            session = self.session(str(params.get('session_id') or ''))
            if not isinstance(params.get('subagents'), list): raise ValueError('subagents must be an array')
            session.emit('spawn_tree.saved', dict(params))
            return {'ok': True}
        if method == "terminal.resize":
            return {"ok": True}
        if method == "slash.exec":
            return self._dispatch_slash(
                str(params.get("command") or ""),
                session_id=str(params.get("session_id") or "") or None,
            )
        if method == "command.dispatch":
            name = str(params.get("name") or "")
            argument = str(params.get("arg") or "")
            return self._dispatch_slash(
                f"{name} {argument}".strip(),
                session_id=str(params.get("session_id") or "") or None,
            )
        if method == "shell.exec":
            return {"code": 1, "stderr": "请让模型通过受审批的 terminal_exec 工具执行命令。"}
        raise UnsupportedMethodError(method)


class GatewayOutput:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._tool_sequence = 0
        self._calls: dict[tuple[str, str, str, str], list[tuple[str, str, str]]] = {}
        self._event_lock = threading.RLock()

    def cancel_requested(self) -> bool:
        return self.session.cancel.is_set()

    def write_response(self, text: str) -> None:
        if text:
            self.session.emit("message.delta", {"text": text}, session_id=self.session.session_id)

    def write_stream(self, text: str) -> None:
        if text:
            self.session.emit("message.delta", {"text": text}, session_id=self.session.session_id)

    def write(self, text: str, *, error: bool = False) -> None:
        event_type = "error" if error else "status.update"
        payload = {"message": text} if error else {"kind": "info", "text": text}
        self.session.emit(event_type, payload, session_id=self.session.session_id)

    def event(self, event: dict[str, Any]) -> None:
        with self._event_lock:
            self._translate_event(event)

    def _translate_event(self, event: dict[str, Any]) -> None:
        if event.get('trace_owner_valid') is False:
            self.session.emit('runtime.event', {'title': '历史运行诊断', 'status': 'stale',
                'summary': str(event.get('event_type') or ''), 'details': event,
                'rationale': '此事件没有当前运行的写入权限，只保留历史记录。'})
            return
        run_dir = str(event.get("run_dir") or "")
        if run_dir and run_dir != self.session.run_dir:
            self.session.run_dir = str(Path(run_dir).resolve())
            if self.session.store is not None:
                self.session.store.save(self.session)
        event_type = str(event.get("event_type") or "")
        sid = self.session.session_id
        public_events = {
            "task.received",
            "planning.started",
            "planning.summary",
            "agent.spawned",
            "agent.activity",
            "tool.started",
            "tool.finished",
            "artifact.created",
            "recovery.triggered",
            "task.completed",
            "task.failed",
        }
        if event_type in public_events:
            self.session.emit(
                "runtime.event",
                {
                    "event_type": event_type,
                    "title": str(event.get("title") or event_type),
                    "summary": str(event.get("summary") or ""),
                    "rationale": str(event.get("rationale") or ""),
                    "details": event.get("details"),
                    "actor": str(event.get("actor") or event.get("role") or ""),
                    "node_id": str(event.get("node_id") or ""),
                    "run_id": str(event.get('run_id') or ''),
                    "execution_id": str(event.get('execution_id') or ''),
                    "status": str(event.get("status") or ""),
                    "recorded_at": str(event.get("recorded_at") or ""),
                    "collapsible": event.get("collapsible", True),
                },
                session_id=sid,
            )
            if event_type == "agent.activity" and event.get("node_id"):
                self.session.emit("subagent.progress", {"id": str(event["node_id"]),
                    "text": str(event.get("summary") or event.get("title") or "节点正在执行")}, session_id=sid)
        elif event_type == "llm.text.delta":
            # Governed nodes stream structured contracts and patches. Their
            # deltas belong to node output files/dashboard state, not the
            # top-level assistant transcript. Direct-chat deltas have no
            # node_id and remain fully streamed in the TUI.
            if not str(event.get("node_id") or "").strip():
                self.session.emit("message.delta", {"text": str(event.get("text") or "")}, session_id=sid)
        elif event_type in {"llm.stream.started", "llm.request"}:
            self.session.usage["calls"] = int(self.session.usage.get("calls", 0)) + 1
            self.session.emit(
                "status.update",
                {"kind": "status", "text": f"正在连接 {event.get('model') or _selected_model()}…"},
                session_id=sid,
            )
        elif event_type == "llm.context":
            used = int(event.get("estimated_input_tokens") or 0)
            maximum = int(event.get("context_total") or self.session.usage["context_max"])
            self.session.usage["context_used"] = used
            self.session.usage["context_max"] = maximum
            self.session.usage["context_percent"] = round(used * 100 / maximum, 1) if maximum else 0
            if event.get("compressed"):
                count = int(self.session.usage.get("compressions", 0)) + 1
                self.session.usage["compressions"] = count
                self.session.emit(
                    "status.update",
                    {"kind": "compressing", "text": f"上下文压缩完成 · 第 {count} 次"},
                    session_id=sid,
                )
            self.session.emit("session.info", self.session.info(), session_id=sid)
        elif event_type == "llm.usage":
            prompt_tokens = max(0, int(event.get("prompt_tokens") or 0))
            completion_tokens = max(0, int(event.get("completion_tokens") or 0))
            request_id = str(event.get('request_id') or '')
            if request_id:
                requests = self.session.usage.setdefault('request_usage', {})
                previous = requests.get(request_id, {'input': 0, 'output': 0})
                # Streaming usage is a cumulative observation of this request.
                current = {'input': max(previous['input'], prompt_tokens),
                           'output': max(previous['output'], completion_tokens)}
                prompt_tokens, completion_tokens = current['input'] - previous['input'], current['output'] - previous['output']
                requests[request_id] = current
            else:
                self.session.usage['unidentified_usage_events'] = int(self.session.usage.get('unidentified_usage_events', 0)) + 1
            self.session.usage["input"] = int(self.session.usage.get("input", 0)) + prompt_tokens
            self.session.usage["output"] = int(self.session.usage.get("output", 0)) + completion_tokens
            self.session.usage["total"] = self.session.usage["input"] + self.session.usage["output"]
            for field, identity in [('by_model', str(event.get('model') or 'unknown')), ('by_node', str(event.get('node_id') or 'direct-chat'))]:
                groups = self.session.usage.setdefault(field, {})
                group = groups.setdefault(identity, {'input': 0, 'output': 0})
                group['input'] += prompt_tokens
                group['output'] += completion_tokens
            self.session.emit("session.info", self.session.info(), session_id=sid)
        elif event_type == "llm.tool_execution.start":
            key = _tool_batch_identity(event)
            batch: list[tuple[str, str, str]] = []
            self._calls[key] = batch
            for call in event.get("calls") or []:
                self._tool_sequence += 1
                tool_id = f"tool-{self._tool_sequence}"
                name = str(call.get("name") or "tool") if isinstance(call, dict) else str(call)
                args = call.get("arguments") if isinstance(call, dict) else None
                if isinstance(args, str):
                    try:
                        decoded = json.loads(args)
                        args = decoded if isinstance(decoded, dict) else args
                    except json.JSONDecodeError:
                        pass
                context = json.dumps(args, ensure_ascii=False)[:2000] if args else ""
                call_id = str(call.get("call_id") or "") if isinstance(call, dict) else ""
                batch.append((tool_id, name, call_id))
                self.session.emit(
                    "tool.start",
                    {"tool_id": tool_id, "name": name, "context": context, "args_text": context,
                     "node_id": key[1], "execution_id": key[2], "call_id": call_id},
                    session_id=sid,
                )
                if key[1]:
                    self.session.emit("subagent.tool", {"id": key[1], "tool_name": name, "tool_preview": context}, session_id=sid)
        elif event_type == "llm.tool_batch":
            calls = list(event.get("calls") or [])
            key = _tool_batch_identity(event)
            batch = self._calls.pop(key, [])
            for index, raw in enumerate(calls):
                call = raw if isinstance(raw, dict) else {}
                call_id = str(call.get("call_id") or "")
                matched = next((item for item in batch if item[2] == call_id), None) if call_id else (
                    batch[index] if index < len(batch) and not batch[index][2] else None)
                if matched:
                    tool_id, name, _ = matched
                else:
                    self._tool_sequence += 1
                    tool_id, name = f"tool-{self._tool_sequence}", str(call.get("name") or "tool")
                error = call.get("error")
                if not error and call.get("status") not in {None, "success"}:
                    error = str(call["status"])
                result = call.get("result") or call.get("output") or call.get("stdout_tail") or call.get("summary") or ""
                review_warning = str(call.get("review_warning") or "")
                if review_warning:
                    result = str(result)[:12000] + "\n文件审查提示：" + review_warning
                self.session.emit(
                    "tool.complete",
                    {
                        "tool_id": tool_id,
                        "node_id": key[1], "execution_id": key[2], "call_id": call_id,
                        "correlation_unknown": matched is None,
                        "name": name,
                        "error": error,
                        "summary": str(call.get("summary") or "")[:2000],
                        "result_text": str(result) if review_warning else str(result)[:12000],
                        "review_coverage": call.get("review_coverage"),
                        "review_warning": review_warning or None,
                        "change_ids": call.get("change_ids") or [],
                    },
                    session_id=sid,
                )
            succeeded = sum(
                1 for item in calls
                if isinstance(item, dict) and item.get("status") == "success"
            )
            failed = sum(
                1 for item in calls
                if isinstance(item, dict)
                and item.get("status") in {"failed", "error", "denied"}
            )
            names = ", ".join(
                str(item.get("name") or "tool")
                for item in calls[:6]
                if isinstance(item, dict)
            )
            self.session.emit(
                "status.update",
                {
                    "kind": "warn" if failed else "status",
                    "text": (
                        f"节点 {event.get('node_id') or ''} 工具批次完成："
                        f"{names or '无工具'}；成功 {succeeded}，失败 {failed}。"
                    ),
                },
                session_id=sid,
            )
        elif event_type == "llm.stream.reconnected":
            self.session.emit(
                "status.update",
                {"kind": "warn", "text": "网络连接已恢复，继续当前请求"},
                session_id=sid,
            )
        elif event_type == "llm.retry":
            self.session.emit(
                "status.update",
                {
                    "kind": "warn",
                    "text": (
                        f"模型请求暂时失败，正在重试 "
                        f"{int(event.get('attempt') or 1)}/"
                        f"{int(event.get('max_retries') or 1)}："
                        f"{str(event.get('error') or event.get('category') or '')[:300]}"
                    ),
                },
                session_id=sid,
            )
        elif event_type in {"llm.stream.failed", "llm.error"}:
            self.session.emit("error", {"message": str(event.get("error") or "模型调用失败")}, session_id=sid)
        elif event_type == "governor.interactive_route.decided":
            self.session.emit(
                "status.update",
                {
                    "kind": "status",
                    "text": (
                        f"Governor 路由: {event.get('mode')} · "
                        f"复杂度 {float(event.get('complexity_score') or 0):.2f} · "
                        f"并行收益 {float(event.get('parallel_benefit') or 0):.2f} · "
                        f"预计 {int(event.get('estimated_nodes') or 1)} 节点"
                    ),
                },
                session_id=sid,
            )
        elif event_type == "runtime.progress.report":
            kind = str(event.get("kind") or "heartbeat")
            if kind != 'final':
                self.session.emit('run.progress', {'text': str(event.get('text') or ''),
                    'phase': str(event.get('phase') or 'unknown'), 'run_dir': self.session.run_dir}, session_id=sid)
                return
            titles = {
                "heartbeat": "阶段性工作汇报",
                "provider_wait": "节点仍在运行",
                "immediate": "节点进展",
                "final": "最终任务总结",
            }
            self.session.emit(
                "runtime.event",
                {
                    "event_type": "runtime.progress.report",
                    "title": titles.get(kind, "运行进展"),
                    "summary": str(event.get("text") or ""),
                    "rationale": "来自 Runtime 的阶段性执行快照，不包含模型私有思维链。",
                    "details": {
                        "phase": event.get("phase"),
                        "output_dir": event.get("run_dir"),
                    },
                    "actor": "AstralNexus Runtime",
                    "status": str(event.get('status') or 'execution_finished'),
                    "recorded_at": str(event.get("recorded_at") or ""),
                    "collapsible": True,
                },
                session_id=sid,
            )
        elif (
            event_type.startswith(("interactive.run.", "interactive.resume."))
            or event_type.startswith("interactive.requirement_change.")
        ):
            text = str(event.get("text") or event_type)
            kind = "warn" if event_type.endswith(("error", "rejected")) else "status"
            self.session.emit("status.update", {"kind": kind, "text": text}, session_id=sid)
        elif event_type == "governor.graph_patch.proposed":
            # The RuntimeEventBus emits one normalized planning.summary with
            # the public planning explanation. Do not duplicate the raw patch
            # as a second terminal status message.
            return
        elif event_type == "governor.graph_patch.applied":
            added = [str(item) for item in event.get("added") or []]
            self.session.emit(
                "status.update",
                {
                    "kind": "status",
                    "text": (
                        f"任务图已提交：新增 {len(added)} 个节点"
                        + (f"（{', '.join(added)}）" if added else "")
                        + "，正在计算首个 Ready-Wave。"
                    ),
                },
                session_id=sid,
            )
        elif event_type in {"router.ready_wave", "orchestrator.ready_wave"}:
            nodes = [
                str(item)
                for item in (
                    event.get("node_ids")
                    or event.get("ready_nodes")
                    or event.get("nodes")
                    or []
                )
            ]
            wave_id = event.get("wave_id")
            self.session.emit(
                "status.update",
                {
                    "kind": "status",
                    "text": (
                        f"Ready-Wave {wave_id or '?'}：并行派发 {len(nodes)} 个节点"
                        + (f"（{', '.join(nodes)}）" if nodes else "")
                    ),
                },
                session_id=sid,
            )
        elif event_type == "node.started":
            node_id = str(event.get("node_id") or "")
            role = str(event.get("role") or "worker")
            goal = str(event.get("goal") or "")
            self.session.emit(
                "subagent.start",
                {
                    "id": node_id,
                    "goal": goal,
                    "role": role,
                },
                session_id=sid,
            )
            if not event.get("progress_reported"):
                self.session.emit(
                    "status.update",
                    {
                        "kind": "status",
                        "text": f"节点 {node_id} · {role} 已开始：{goal[:500]}",
                    },
                    session_id=sid,
                )
        elif event_type == "node.completed":
            status = str(event.get("status") or "completed")
            node_id = str(event.get("node_id") or "")
            role = str(event.get("role") or "worker")
            summary = str(event.get("summary") or "")
            self.session.emit(
                "subagent.complete",
                {
                    "id": node_id,
                    "status": status,
                    "summary": summary,
                    "role": role,
                },
                session_id=sid,
            )
            if not event.get("progress_reported"):
                self.session.emit(
                    "status.update",
                    {
                        "kind": "warn" if status in {"failure", "failed"} else "status",
                        "text": (
                            f"节点 {node_id} · {role} 已结束，状态 {status}："
                            f"{summary[:700]}"
                        ),
                    },
                    session_id=sid,
                )
        elif event_type == "node.io.output":
            refs = [
                str(event.get("json_path") or ""),
                str(event.get("markdown_path") or ""),
            ]
            refs = [item for item in refs if item]
            if refs:
                self.session.emit(
                    "status.update",
                    {
                        "kind": "status",
                        "text": (
                            f"节点 {event.get('node_id') or ''} 结果已落盘："
                            + "；".join(refs)
                        ),
                    },
                    session_id=sid,
                )
        elif event_type in {
            "recovery.attempted",
            "recovery.planned",
            "recovery.completed",
            "orchestrator.dynamic_spawn.exception",
        }:
            actions = event.get("actions") or []
            detail = (
                ", ".join(str(item) for item in actions)
                or str(event.get("error_type") or event.get("fault_id") or "")
            )
            self.session.emit(
                "status.update",
                {
                    "kind": "warn",
                    "text": (
                        f"故障恢复 · {event_type} · 节点 "
                        f"{event.get('node_id') or event.get('parent') or ''}"
                        + (f"：{detail}" if detail else "")
                    ),
                },
                session_id=sid,
            )


def main() -> int:
    gateway = Gateway()
    _event("gateway.ready", {"skin": _skin()})
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            request: dict[str, Any] = {}
            try:
                request = json.loads(raw)
                request_id = request.get("id")
                value = gateway.dispatch(str(request.get("method") or ""), request.get("params") or {})
                _result(request_id, value)
            except UnsupportedMethodError as exc:
                _error(request.get("id"), str(exc), code=-32601)
            except Exception as exc:
                _error(request.get("id"), str(exc))
    finally:
        gateway.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
