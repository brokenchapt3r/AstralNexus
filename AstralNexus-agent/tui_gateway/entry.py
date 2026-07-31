"""JSON-RPC adapter between the vendored Ink TUI and AstralNexus."""
from __future__ import annotations

import json
import contextlib
import io
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_WRITE_LOCK = threading.RLock()


def _write(message: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _result(request_id: Any, value: Any) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": value})


def _error(request_id: Any, message: str, code: int = -32000) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _event(event_type: str, payload: dict[str, Any] | None = None, *, session_id: str | None = None) -> None:
    params: dict[str, Any] = {"type": event_type}
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
        "context_max": 1_000_000,
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

    def info(self) -> dict[str, Any]:
        tools, skills = _runtime_capabilities()
        return {
            "cwd": str(Path.cwd().resolve()),
            "model": _selected_model(),
            "profile_name": "天枢 Runtime",
            "skills": skills,
            "tools": tools,
            "usage": dict(self.usage),
            "version": _product_version(),
        }


def _product_version() -> str:
    from AstralNexus.core.identity import VERSION

    return VERSION


class Gateway:
    def __init__(self) -> None:
        from AstralNexus.cli.main import (
            _InteractiveRunController,
            _register_interactive_delegate_tool,
        )
        from AstralNexus.core.identity import bootstrap_environment

        self.sessions: dict[str, Session] = {}
        self.current_session_id: str | None = None
        self._approval_lock = threading.Condition()
        self._approval_choice: str | None = None
        self.home = bootstrap_environment()
        self.controller = _InteractiveRunController(self.home)
        _register_interactive_delegate_tool(self.controller, self.home)

    def session(self, session_id: str | None) -> Session:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        if self.current_session_id and self.current_session_id in self.sessions:
            return self.sessions[self.current_session_id]
        raise ValueError("session not found")

    def create_session(self) -> Session:
        session = Session(session_id=f"astral-{uuid.uuid4().hex[:12]}")
        self.sessions[session.session_id] = session
        self.current_session_id = session.session_id
        return session

    def approval_provider(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        caller: str,
        basis: str,
    ) -> str:
        command = str(
            arguments.get("command")
            or arguments.get("path")
            or arguments.get("url")
            or json.dumps(arguments, ensure_ascii=False)
        )
        with self._approval_lock:
            self._approval_choice = None
            _event(
                "approval.request",
                {
                    "allow_permanent": True,
                    "choices": ["once", "session", "always", "deny"],
                    "command": command[:4000],
                    "description": f"{tool_name} · {caller} · {basis}",
                },
                session_id=self.current_session_id,
            )
            self._approval_lock.wait_for(lambda: self._approval_choice is not None)
            return self._approval_choice or "deny"

    def respond_approval(self, choice: str) -> None:
        aliases = {
            "allow_once": "once",
            "allow_session": "session",
            "allow_always": "always",
            "reject": "deny",
        }
        with self._approval_lock:
            self._approval_choice = aliases.get(choice, choice)
            self._approval_lock.notify_all()

    def submit(self, session: Session, text: str) -> None:
        if session.running:
            session.queued.put(text)
            return
        session.running = True
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

        def invoke() -> tuple[int, str]:
            stdout = io.StringIO()
            stderr = io.StringIO()
            old_home = os.environ.get("ASTRALNEXUS_HOME")
            os.environ["ASTRALNEXUS_HOME"] = str(self.home)
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    try:
                        code = int(process_command(command, home=self.home) or 0)
                    except SystemExit as exc:
                        code = int(exc.code or 0)
            except Exception as exc:
                code = 1
                stderr.write(str(exc))
            finally:
                if old_home is None:
                    os.environ.pop("ASTRALNEXUS_HOME", None)
                else:
                    os.environ["ASTRALNEXUS_HOME"] = old_home
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
                session_id=self.current_session_id,
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
        from AstralNexus.tools.approval import clear_approval_provider, set_approval_provider

        output = GatewayOutput(session)
        _event("message.start", session_id=session.session_id)
        _event(
            "status.update",
            {"kind": "status", "text": "正在分析请求并选择下一步…"},
            session_id=session.session_id,
        )
        before = len(session.history)
        set_approval_provider(self.approval_provider)
        try:
            self.controller.set_event_callback(output.event)
            consumed = _govern_interactive_turn(
                text,
                session.history,
                self.controller,
                output=output,
            )
            if not consumed:
                _interactive_chat_turn(text, session.history, output=output)
            answer = ""
            if len(session.history) > before and session.history[-1].get("role") == "assistant":
                answer = session.history[-1].get("content", "")
            _event(
                "message.complete",
                {"text": answer, "usage": dict(session.usage)},
                session_id=session.session_id,
            )
        except Exception as exc:
            _event("error", {"message": str(exc)}, session_id=session.session_id)
            _event("message.complete", {"text": "", "usage": dict(session.usage)}, session_id=session.session_id)
        finally:
            clear_approval_provider(self.approval_provider)
            session.running = False
            _event("session.info", session.info(), session_id=session.session_id)
            try:
                queued = session.queued.get_nowait()
            except queue.Empty:
                queued = ""
            if queued:
                self.submit(session, queued)

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "setup.status":
            return {"provider_configured": True}
        if method == "config.get":
            return {
                "config": {
                    "display": {
                        "busy_input_mode": "interrupt",
                        "mouse_tracking": "off",
                        "tui_auto_resume_recent": False,
                        "tui_agents_nudge": True,
                    }
                }
            }
        if method == "commands.catalog":
            from AstralNexus.cli.commands import command_catalog

            catalog = command_catalog()
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
            return {
                "active": 1 if status["running"] else 0,
                "max_concurrent": 1,
                "max_spawn_depth": 1,
                "running": [status] if status["running"] else [],
            }
        if method == "session.create":
            session = self.create_session()
            return {"session_id": session.session_id, "info": session.info()}
        if method == "session.close":
            requested = str(params.get("session_id") or self.current_session_id or "")
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
                        "status": "working" if item.running else "idle",
                        "title": "天枢会话",
                    }
                    for item in self.sessions.values()
                ]
            }
        if method == "session.most_recent":
            return {"session_id": self.current_session_id}
        if method in {"session.resume", "session.activate"}:
            session = self.session(str(params.get("session_id") or ""))
            self.current_session_id = session.session_id
            messages = [
                {"role": item["role"], "text": item.get("content", "")}
                for item in session.history
                if item.get("role") in {"user", "assistant", "system", "tool"}
            ]
            return {
                "session_id": session.session_id,
                "messages": messages,
                "message_count": len(messages),
                "running": session.running,
                "status": "working" if session.running else "idle",
                "started_at": session.started_at,
                "info": session.info(),
            }
        if method in {"session.status", "session.usage"}:
            session = self.session(str(params.get("session_id") or ""))
            return {
                "running": session.running,
                "status": "working" if session.running else "idle",
                "usage": dict(session.usage),
                "info": session.info(),
            }
        if method == "session.title":
            return {"title": str(params.get("title") or "天枢会话"), "pending": False}
        if method == "session.interrupt":
            session = self.session(str(params.get("session_id") or ""))
            session.cancel.set()
            if self.controller.status()["running"]:
                self.controller.interrupt("native_tui_interrupt")
            return {"ok": True}
        if method == "session.steer":
            session = self.session(str(params.get("session_id") or ""))
            session.queued.put(str(params.get("text") or ""))
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
            self.respond_approval(str(params.get("choice") or "deny"))
            return {"ok": True}
        if method in {"terminal.resize", "spawn_tree.save"}:
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
        return {}


class GatewayOutput:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._tool_sequence = 0
        self._last_calls: list[tuple[str, str]] = []

    def cancel_requested(self) -> bool:
        return self.session.cancel.is_set()

    def write_response(self, text: str) -> None:
        if text:
            _event("message.delta", {"text": text}, session_id=self.session.session_id)

    def write_stream(self, text: str) -> None:
        if text:
            _event("message.delta", {"text": text}, session_id=self.session.session_id)

    def write(self, text: str, *, error: bool = False) -> None:
        event_type = "error" if error else "status.update"
        payload = {"message": text} if error else {"kind": "info", "text": text}
        _event(event_type, payload, session_id=self.session.session_id)

    def event(self, event: dict[str, Any]) -> None:
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
            _event(
                "runtime.event",
                {
                    "event_type": event_type,
                    "title": str(event.get("title") or event_type),
                    "summary": str(event.get("summary") or ""),
                    "rationale": str(event.get("rationale") or ""),
                    "details": event.get("details"),
                    "actor": str(event.get("actor") or event.get("role") or ""),
                    "node_id": str(event.get("node_id") or ""),
                    "status": str(event.get("status") or ""),
                    "recorded_at": str(event.get("recorded_at") or ""),
                    "collapsible": event.get("collapsible", True),
                },
                session_id=sid,
            )
        elif event_type == "llm.text.delta":
            # Governed nodes stream structured contracts and patches. Their
            # deltas belong to node output files/dashboard state, not the
            # top-level assistant transcript. Direct-chat deltas have no
            # node_id and remain fully streamed in the TUI.
            if not str(event.get("node_id") or "").strip():
                _event("message.delta", {"text": str(event.get("text") or "")}, session_id=sid)
        elif event_type in {"llm.stream.started", "llm.request"}:
            self.session.usage["calls"] = int(self.session.usage.get("calls", 0)) + 1
            _event(
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
                _event(
                    "status.update",
                    {"kind": "compressing", "text": f"上下文压缩完成 · 第 {count} 次"},
                    session_id=sid,
                )
            _event("session.info", self.session.info(), session_id=sid)
        elif event_type == "llm.usage":
            prompt_tokens = int(event.get("prompt_tokens") or 0)
            completion_tokens = int(event.get("completion_tokens") or 0)
            self.session.usage["input"] = int(self.session.usage.get("input", 0)) + prompt_tokens
            self.session.usage["output"] = int(self.session.usage.get("output", 0)) + completion_tokens
            self.session.usage["total"] = self.session.usage["input"] + self.session.usage["output"]
            self.session.usage["context_used"] = max(self.session.usage["context_used"], prompt_tokens)
            _event("session.info", self.session.info(), session_id=sid)
        elif event_type == "llm.tool_execution.start":
            self._last_calls = []
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
                self._last_calls.append((tool_id, name))
                _event(
                    "tool.start",
                    {"tool_id": tool_id, "name": name, "context": context, "args_text": context},
                    session_id=sid,
                )
        elif event_type == "llm.tool_batch":
            calls = list(event.get("calls") or [])
            for index, (tool_id, name) in enumerate(self._last_calls):
                call = calls[index] if index < len(calls) and isinstance(calls[index], dict) else {}
                error = call.get("error")
                if not error and call.get("status") in {"failed", "denied"}:
                    error = str(call["status"])
                result = call.get("result") or call.get("output") or call.get("summary") or ""
                _event(
                    "tool.complete",
                    {
                        "tool_id": tool_id,
                        "name": name,
                        "error": error,
                        "summary": str(call.get("summary") or "")[:2000],
                        "result_text": str(result)[:12000],
                    },
                    session_id=sid,
                )
            self._last_calls = []
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
            _event(
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
            _event(
                "status.update",
                {"kind": "warn", "text": "网络连接已恢复，继续当前请求"},
                session_id=sid,
            )
        elif event_type == "llm.retry":
            _event(
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
            _event("error", {"message": str(event.get("error") or "模型调用失败")}, session_id=sid)
        elif event_type == "governor.interactive_route.decided":
            _event(
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
            titles = {
                "heartbeat": "阶段性工作汇报",
                "provider_wait": "节点仍在运行",
                "immediate": "节点进展",
                "final": "最终任务总结",
            }
            _event(
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
                    "status": "completed" if kind == "final" else "running",
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
            _event("status.update", {"kind": kind, "text": text}, session_id=sid)
        elif event_type == "governor.graph_patch.proposed":
            # The RuntimeEventBus emits one normalized planning.summary with
            # the public planning explanation. Do not duplicate the raw patch
            # as a second terminal status message.
            return
        elif event_type == "governor.graph_patch.applied":
            added = [str(item) for item in event.get("added") or []]
            _event(
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
            _event(
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
            _event(
                "subagent.start",
                {
                    "id": node_id,
                    "goal": goal,
                    "role": role,
                },
                session_id=sid,
            )
            if not event.get("progress_reported"):
                _event(
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
            _event(
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
                _event(
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
                _event(
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
            _event(
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
        except Exception as exc:
            _error(request.get("id"), str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
