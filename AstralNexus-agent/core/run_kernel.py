"""Single-owner application actor; public commands never expose live graph objects."""
from __future__ import annotations
from concurrent.futures import Future
from copy import deepcopy
from contextvars import copy_context
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread, get_ident
import uuid
from typing import Any, Callable
from AstralNexus.core.delivery_ports import freeze
from AstralNexus.runtime.run_context import RunContext, RunSettings


class RunKernel:
    def __init__(self, run_dir: Path, *, engine_factory: Callable[[], Any] | None = None,
                 user_resume: bool = False, settings: RunSettings | None = None,
                 interrupt_event: Event | None = None, interrupt_queue: Queue | None = None, **engine_options: Any):
        self.run_dir = Path(run_dir)
        self.context = RunContext(self.run_dir, settings or RunSettings.capture())
        self._queue: Queue = Queue()
        self._lock = Lock()
        self._closed = False
        self._closing = False
        self._shutdown_error: BaseException | None = None
        self._terminal_error: BaseException | None = None
        self._close_future: Future | None = None
        self._ready: Future = Future()
        self._snapshot = freeze({})
        self._interrupt = interrupt_event if interrupt_event is not None else Event()
        self._interrupt_queue: Queue = interrupt_queue if interrupt_queue is not None else Queue()
        self._coordinator = None
        self._lease_holder = None
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        self._last_execution: dict = {}
        def default_factory():
            from AstralNexus.core.orchestrator import Orchestrator
            from AstralNexus.runtime.run_coordinator import RunCoordinator
            from AstralNexus.runtime.run_lifecycle import _start_lease_heartbeat
            if {"checkpoint_sink", "ownership_probe", "fence_token_provider"} & engine_options.keys():
                raise ValueError("RunKernel owns lease/checkpoint bindings")
            self._coordinator = RunCoordinator(self.run_dir, run_id=self.run_dir.name)
            lease = self._coordinator.acquire("kernel-" + uuid.uuid4().hex, user_resume=user_resume,
                authorization_source="kernel:explicit-user-resume" if user_resume else "")
            if lease is None:
                raise RuntimeError("RunKernel ownership rejected; another owner or manual stop exists")
            holder, self._heartbeat_stop, self._heartbeat_thread, _ = _start_lease_heartbeat(
                self._coordinator, lease, self._interrupt, self._interrupt_queue)
            self._lease_holder = holder
            had_checkpoint = (self.run_dir / "task_graph.sqlite").exists()
            engine = Orchestrator(self.run_dir, **engine_options,
                checkpoint_sink=lambda payload: self._coordinator.record_checkpoint(holder["lease"], payload),
                ownership_probe=lambda: self._coordinator.owns(holder["lease"]),
                fence_token_provider=lambda: self._coordinator.fence_token(holder["lease"]))
            if had_checkpoint and not engine.restore_checkpoint():
                engine.shutdown()
                raise RuntimeError("RunKernel existing checkpoint could not be restored")
            return engine
        self._factory = engine_factory or default_factory
        self._thread = Thread(target=copy_context().run, args=(self._serve,), name="AstralNexus-RunKernel", daemon=True)
        self._thread.start()
        try:
            self._ready.result(timeout=30)
        except BaseException:
            self._interrupt.set()
            self._queue.put(("close", {}, Future()))
            raise

    def _capture(self, engine) -> None:
        from AstralNexus.core.json_safety import json_safe
        snapshot = freeze(json_safe(engine.task_graph.to_dict()))
        with self._lock:
            self._snapshot = snapshot

    def _serve(self) -> None:
        with self.context.scope():
            self._serve_owned()

    def _serve_owned(self) -> None:
        engine = None
        try:
            engine = self._factory()
            if hasattr(engine, "trace_recorder"):
                engine.trace_recorder.record("run.context.initialized", self.context.settings.audit())
            engine.attach_interrupt_control(self._interrupt, self._interrupt_queue)
            previous = engine.checkpoint_sink
            def checkpoint(payload):
                if previous is not None:
                    previous(payload)
                self._capture(engine)
            engine.checkpoint_sink = checkpoint
            self._capture(engine)
            self._ready.set_result(True)
            while True:
                operation, payload, future = self._queue.get()
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    if operation == "close":
                        future.set_result(None)
                        return
                    if hasattr(engine, "_ownership_ok") and not engine._ownership_ok():
                        raise RuntimeError("RunKernel ownership lost")
                    if operation == "legacy_entry":
                        # Private bridge for trusted entry adapters. Construction,
                        # graph access, execution and teardown stay on this owner;
                        # it is never a user/RPC-supplied callable command.
                        result = payload['callback'](engine)
                        manager = getattr(engine, "runtime_state_manager", None)
                        if manager is not None:
                            state = manager.state
                            self._last_execution = {"delivery_status": state.delivery_status, "status": state.run_status,
                                                    "stop_code": state.stop_code, "stop_reason": state.stop_reason}
                    elif operation == "execute":
                        result = engine.run(**payload)
                    elif operation == "complete":
                        from AstralNexus.core.run_application import RunApplicationService
                        result = RunApplicationService(engine).execute(run_dir=self.run_dir, **payload)
                    elif operation == "patch":
                        result = engine.governor.apply_task_graph_patch(payload["patch"])
                        if not engine._persist_graph_checkpoint(stage="kernel_patch"):
                            raise RuntimeError("kernel checkpoint was rejected")
                    elif operation == "checkpoint":
                        result = engine._persist_graph_checkpoint(stage="kernel_command")
                    else:
                        raise ValueError(f"unknown kernel command: {operation}")
                    self._capture(engine)
                    if operation in {"execute", "complete"}:
                        self._last_execution = dict(result)
                        self._terminal_error = None
                    elif operation == "patch":
                        # A changed graph no longer has the previous delivery verdict.
                        self._last_execution = {}
                    future.set_result(freeze(result))
                except BaseException as error:
                    self._terminal_error = error
                    self._last_execution = {}
                    future.set_exception(error)
        except BaseException as error:
            self._terminal_error = error
            if not self._ready.done():
                self._ready.set_exception(error)
        finally:
            if engine is not None:
                try:
                    engine.shutdown()
                except BaseException as error:
                    self._shutdown_error = error
            with self._lock:
                self._closed = True
                self._closing = True
            from AstralNexus.runtime.run_lifecycle import _stop_lease_heartbeat
            _stop_lease_heartbeat(self._heartbeat_stop, self._heartbeat_thread)
            if self._coordinator is not None and self._lease_holder is not None:
                error = self._shutdown_error or self._terminal_error
                status = "blocked" if error is not None else (
                    "completed" if self._last_execution.get("delivery_status") == "passed" else "paused")
                execution = self._last_execution.get("execution") or self._last_execution
                code = "host_exception" if error is not None else (
                    "completed" if status == "completed" else str(execution.get("stop_code") or "user_pause"))
                self._coordinator.finish(self._lease_holder["lease"], status, stop_code=code,
                    stop_reason=(f"{type(error).__name__}: {error}" if error is not None else
                        str(execution.get("stop_reason") or "kernel closed; explicit resume required")))
            while not self._queue.empty():
                _, _, pending = self._queue.get_nowait()
                if not pending.done():
                    pending.set_exception(RuntimeError("run kernel closed before command execution"))

    def _submit(self, operation: str, payload: dict) -> Future:
        if get_ident() == self._thread.ident:
            raise RuntimeError("kernel command cannot synchronously reenter its owner")
        # Detach before admission; later caller mutation cannot change the command.
        detached = deepcopy(payload)
        future: Future = Future()
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("run kernel is closed")
            self._queue.put((operation, detached, future))
        return future

    def _run_entry(self, callback: Callable[[Any], Any]) -> Future:
        return self._submit('legacy_entry', {'callback': callback})

    def execute(self, *, goal: str, constraints: list[str] | None = None) -> Future:
        return self._submit("execute", {"goal": goal, "constraints": list(constraints or [])})

    def run_complete(self, *, task_content: str, constraints: list[str] | None = None, model_config: dict | None = None) -> Future:
        return self._submit("complete", {"task_content": task_content, "constraints": constraints, "model_config": model_config})

    def apply_patch(self, patch: dict) -> Future:
        return self._submit("patch", {"patch": patch})

    def checkpoint(self) -> Future:
        return self._submit("checkpoint", {})

    def snapshot(self):
        """Non-blocking last committed graph, even while a command is running."""
        with self._lock:
            return self._snapshot

    def cancel(self, reason: str = "user_interrupt") -> None:
        self._interrupt_queue.put({"type": "interrupt", "message": reason})
        self._interrupt.set()

    def close(self, timeout: float = 30) -> None:
        if get_ident() == self._thread.ident:
            raise RuntimeError("kernel cannot close itself from an owner callback")
        with self._lock:
            if self._closed:
                if self._shutdown_error is not None:
                    raise RuntimeError("kernel resource cleanup failed") from self._shutdown_error
                return
            if self._close_future is None:
                self._closing = True
                self._close_future = Future()
                self._queue.put(("close", {}, self._close_future))
            future = self._close_future
        self.cancel("kernel_close")
        future.result(timeout=timeout)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("kernel did not shut down before deadline")
        if self._shutdown_error is not None:
            raise RuntimeError("kernel resource cleanup failed") from self._shutdown_error

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
