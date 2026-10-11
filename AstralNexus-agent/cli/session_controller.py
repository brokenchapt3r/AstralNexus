"""Interactive run lifecycle and ordered, reentrant observer events."""
from __future__ import annotations
import argparse
import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING
from AstralNexus.cli.commands import SESSION_COMMANDS
if TYPE_CHECKING:
    from AstralNexus.core.governor import GlobalGovernor, InteractiveRouteDecision


def _run_task(*args: Any, **kwargs: Any) -> int:
    # Resolve the public CLI seam at invocation time for embedders and tests.
    from . import main
    return main._run_task(*args, **kwargs)


def _resume_task(*args: Any, **kwargs: Any) -> int:
    from . import main
    return main._resume_task(*args, **kwargs)


class _InteractiveRunController:
    """Own the one active background run for the interactive TUI.

    The controller never edits a TaskGraph.  It only exposes a cancellation
    token and a queue; the Orchestrator drains that queue at safe boundaries
    and sends requirement changes through Governor.
    """

    def __init__(self, home: Path):
        self.home = home
        self.interrupt_event = threading.Event()
        self.interrupt_queue: queue.Queue = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.last_result: Optional[int] = None
        self.run_dir: Optional[Path] = None
        self.event_callback = None
        from contextvars import ContextVar
        self._bound_event_callback: ContextVar = ContextVar('interactive_run_callback', default=None)
        self._lock = threading.Lock()
        self._route_governor: GlobalGovernor | None = None

    def set_event_callback(self, callback) -> None:
        self.event_callback = callback

    def _current_event_callback(self):
        return self._bound_event_callback.get() or self.event_callback

    def dispatch_command(
        self,
        command_line: str,
        *,
        history: Optional[list[dict]] = None,
    ) -> dict:
        """Handle commands that operate on the active interactive runtime.

        Frontends must delegate here instead of implementing their own
        background-run semantics. Commands unrelated to the active runtime are
        returned as ``handled=False`` for the canonical command registry.
        """
        from AstralNexus.cli.commands import resolve_command, split_command_line

        try:
            parts = split_command_line(command_line)
        except ValueError as exc:
            return {"handled": True, "ok": False, "code": 1, "output": str(exc)}
        if not parts:
            return {"handled": True, "ok": True, "code": 0, "output": ""}

        canonical = resolve_command(parts[0].lstrip("/").lower())
        if canonical is None:
            return {
                "handled": True,
                "ok": False,
                "code": 1,
                "output": f"未知命令: {parts[0]}",
            }
        if canonical not in SESSION_COMMANDS:
            return {"handled": False, "canonical": canonical}
        arguments = parts[1:]
        if canonical == "goal":
            if not arguments:
                return {"handled": True, "ok": False, "code": 1, "output": "用法: /goal <完成条件>"}
            started = self.start_text("/goal " + " ".join(arguments), history=history)
            return {"handled": True, "ok": started, "code": 0 if started else 1, "output": "Goal 已启动" if started else "已有运行，不能同时启动 Goal"}
        if canonical == "quit":
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "exit": True,
                "output": "正在退出天枢。",
            }
        if canonical == "run":
            if not arguments:
                return {"handled": True, "ok": False, "code": 1, "output": "用法: /run <task-file>"}
            started = self.start(arguments[0])
            return {
                "handled": True,
                "ok": started,
                "code": 0 if started else 1,
                "output": (
                    f"已在后台启动正式任务: {arguments[0]}"
                    if started else "任务未启动；已有任务运行或任务参数无效。"
                ),
            }
        if canonical == "resume":
            if not arguments:
                return {
                    "handled": True,
                    "ok": False,
                    "code": 1,
                    "output": "用法: /resume <run-id|run-dir> [task-file]",
                }
            started = self.start_resume(
                arguments[0],
                task=arguments[1] if len(arguments) > 1 else None,
            )
            return {
                "handled": True,
                "ok": started,
                "code": 0 if started else 1,
                "output": (
                    f"已在后台恢复运行: {arguments[0]}"
                    if started else f"无法恢复运行: {arguments[0]}"
                ),
            }
        if canonical == "new":
            goal = " ".join(arguments).strip()
            if not goal:
                return {"handled": True, "ok": False, "code": 1, "output": "用法: /new <任务目标>"}
            started = self.start_text(goal, history=history)
            return {
                "handled": True,
                "ok": started,
                "code": 0 if started else 1,
                "output": "已创建新任务并交给 Governor。" if started else "新任务未启动。",
            }
        if canonical == "interrupt":
            self.interrupt(" ".join(arguments).strip() or "user_interrupt")
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "output": "已请求协作式中断并保存 checkpoint。",
            }
        if canonical == "status":
            status = self.status()
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "status": status,
                "output": (
                    f"后台任务: {'运行中' if status['running'] else '空闲'}\n"
                    f"运行目录: {status.get('run_dir') or '(无)'}\n"
                    f"待处理注入: {status.get('queued_interrupts', 0)}\n"
                    f"最近退出码: {status.get('last_result')}"
                ),
            }
        if canonical == "inject-requirement" and self.status()["running"]:
            if not arguments:
                return {
                    "handled": True,
                    "ok": False,
                    "code": 1,
                    "output": "用法: /inject-requirement [run-id] <change.json>",
                }
            try:
                self.inject_requirement(arguments[-1])
            except Exception as exc:
                return {
                    "handled": True,
                    "ok": False,
                    "code": 1,
                    "output": f"需求变更注入失败: {exc}",
                }
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "output": "需求变更已进入 Governor 队列。",
            }
        return {"handled": False, "canonical": canonical}

    def _emit(self, event_type: str, text: str, **payload) -> None:
        event = {"event_type": event_type, "text": text, **payload}
        callback = self._current_event_callback()
        if callback is not None:
            callback(event)
        else:
            print(text, flush=True)

    def decide_route(self, text: str, history: list[dict]) -> InteractiveRouteDecision:
        """Ask the real-LLM Governor how an ordinary turn should execute."""
        from AstralNexus.core.governor import GlobalGovernor
        from AstralNexus.core.task_graph import TaskGraph
        from AstralNexus.runtime.llm_client import LLMClient
        from AstralNexus.runtime.trace import TraceRecorder

        if self._route_governor is None:
            control_dir = self.home / "interactive_control"
            trace = TraceRecorder(
                control_dir,
                default_node_id="interactive",
                event_callback=self._current_event_callback(),
            )
            self._route_governor = GlobalGovernor(
                TaskGraph(),
                trace,
                llm_client=LLMClient(interactive=False, workspace_root=Path.cwd()),
            )
        else:
            self._route_governor.trace_recorder.event_callback = self._current_event_callback()
        recent = "\n".join(
            f"{item.get('role')}: {str(item.get('content') or '')[:1000]}"
            for item in history[-8:]
            if item.get("role") in {"user", "assistant"}
        )
        return self._route_governor.decide_interactive_route(
            text,
            conversation_context=recent,
            active_run=self.status()["running"],
        )

    def start_text(
        self,
        text: str,
        *,
        history: Optional[list[dict]] = None,
        output: Optional[str] = None,
        route_decision: Optional[dict] = None,
    ) -> bool:
        """Materialize a conversational request as a reproducible task contract."""
        task_dir = self.home / "interactive_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_path = task_dir / f"task-{int(time.time() * 1000)}.txt"
        context_lines = []
        for item in (history or [])[-8:]:
            if item.get("role") in {"user", "assistant"}:
                context_lines.append(
                    f"{item.get('role')}: {str(item.get('content') or '')[:2000]}"
                )
        # The task file is the immutable user intent. Conversation context and
        # routing evidence are audit metadata, not hidden task requirements.
        explicit_goal = text.strip()[6:].strip() if text.strip().startswith("/goal ") else None
        task_path.write_text(explicit_goal or text.strip(), encoding="utf-8")
        metadata = {
            "user_intent": text.strip(),
            "conversation_context": context_lines,
            "route_decision": route_decision or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        task_path.with_suffix(".context.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if explicit_goal:
            return self.start(str(task_path), output=output, goal=explicit_goal)
        return self.start(str(task_path), output=output)

    def _bind(self, orchestrator, run_dir: Path) -> None:
        with self._lock:
            self.run_dir = run_dir
        orchestrator.attach_interrupt_control(self.interrupt_event, self.interrupt_queue)

    def _launch(self, target, args, *, event_type: str, text: str, thread_name: str, **payload) -> bool:
        gate = threading.Event()
        with self._lock:
            busy = self.thread is not None and self.thread.is_alive()
            if not busy:
                # New session channels fence delayed callbacks from an old run.
                self.interrupt_event = threading.Event()
                self.interrupt_queue = queue.Queue()
                self.last_result = None
                callback = self.event_callback
                def invoke() -> None:
                    gate.wait()
                    token = self._bound_event_callback.set(callback)
                    try:
                        target(args)
                    finally:
                        self._bound_event_callback.reset(token)
                from contextvars import copy_context
                self.thread = threading.Thread(target=copy_context().run, args=(invoke,), name=thread_name, daemon=False)
                self.thread.start()
        if busy:
            self._emit("interactive.run.rejected", "已有任务正在运行，请先中断或等待完成。")
            return False
        try:
            self._emit(event_type, text, **payload)
        finally:
            gate.set()
        return True

    def start(self, task: str, *, output: Optional[str] = None, goal: Optional[str] = None) -> bool:
        args = argparse.Namespace(task=task, output=output, config=None, model=None,
                                  provider=None, max_parallel=None, goal=goal)
        return self._launch(self._run, args, event_type="interactive.run.started",
                            text="Governor 已启动动态异构任务图；可继续输入新需求或使用 /status。",
                            thread_name="astralnexus-orchestrator", task=task, output=output)

    def start_resume(self, run_dir: str, *, task: Optional[str] = None, user_resume: bool = True) -> bool:
        candidate = Path(run_dir).expanduser()
        if not candidate.is_dir():
            from AstralNexus.cli.commands import _resolve_run_dir
            resolved = _resolve_run_dir(run_dir, self.home)
            if resolved is None:
                print(f"找不到可恢复运行: {run_dir}")
                return False
            candidate = resolved
        args = argparse.Namespace(run_dir=str(candidate), task=task, config=None, model=None,
                                  provider=None, max_parallel=None, user_resume=user_resume,
                                  authorization_source="interactive:/resume" if user_resume else "")
        return self._launch(self._resume, args, event_type="interactive.run.resumed",
                            text="已在后台恢复天枢 Orchestrator；可继续输入新需求。",
                            thread_name="astralnexus-resume", run_dir=str(candidate))

    def _resume(self, args) -> None:
        try:
            self.last_result = _resume_task(
                args, self.home,
                interrupt_event=self.interrupt_event,
                interrupt_queue=self.interrupt_queue,
                on_orchestrator=self._bind,
                event_callback=self._current_event_callback(),
            )
        finally:
            self._emit(
                "interactive.run.finished",
                f"后台恢复线程结束，退出码: {self.last_result}",
                result=self.last_result,
                run_dir=str(self.run_dir) if self.run_dir else None,
            )

    def _run(self, args) -> None:
        try:
            self.last_result = _run_task(
                args, self.home,
                interrupt_event=self.interrupt_event,
                interrupt_queue=self.interrupt_queue,
                on_orchestrator=self._bind,
                event_callback=self._current_event_callback(),
            )
        finally:
            self._emit(
                "interactive.run.finished",
                f"后台任务线程结束，退出码: {self.last_result}",
                result=self.last_result,
                run_dir=str(self.run_dir) if self.run_dir else None,
            )

    def status(self) -> dict:
        with self._lock:
            thread = self.thread
            return {
                "running": bool(thread and thread.is_alive()),
                "run_dir": str(self.run_dir) if self.run_dir else None,
                "queued_interrupts": self.interrupt_queue.qsize(),
                "last_result": self.last_result,
            }

    def interrupt(self, message: str = "user_interrupt") -> None:
        self.interrupt_queue.put({"type": "interrupt", "message": message})
        self.interrupt_event.set()
        self._emit(
            "interactive.run.interrupt_requested",
            "已请求协作式中断；当前网络请求会在响应或重试边界退出并保存 checkpoint。",
        )

    def inject_requirement_text(self, description: str) -> None:
        self.interrupt_queue.put({
            "type": "requirement_change",
            "description": description,
            "affected_constraints": [],
            "payload": {"source": "interactive_governor"},
        })
        self.interrupt_event.set()
        self._emit(
            "interactive.requirement_change.queued",
            "新需求已进入 Governor 队列，将在安全边界生成 TaskGraph Patch。",
            description=description[:1000],
        )

    def inject_requirement(self, payload_path: str) -> None:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("需求变更文件必须是 JSON 对象")
        self.interrupt_queue.put({
            "type": "requirement_change",
            "description": payload.get("description") or payload.get("change") or json.dumps(payload, ensure_ascii=False),
            "affected_constraints": payload.get("affected_constraints", []),
            "change_kind": payload.get("change_kind"),
            "payload": payload,
        })
        self.interrupt_event.set()
        self._emit(
            "interactive.requirement_change.queued",
            "需求变更已进入队列，将由 Governor 在安全边界生成并应用 TaskGraph Patch。",
        )
