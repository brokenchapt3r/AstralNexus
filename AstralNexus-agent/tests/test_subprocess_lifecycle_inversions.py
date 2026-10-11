"""Counterexamples for subprocess startup, cancellation and settlement."""
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from AstralNexus.runtime import subprocess_local as sl
from AstralNexus.runtime.subprocess_output import OutputCollector


def spec(root: Path) -> dict[str, Any]:
    return {"argv": [sys.executable, "-c", "print('ok')"], "cwd": str(root),
            "graceMs": 20, "stdio": {"stdout": {"maxBytes": 100},
                                      "stderr": {"maxBytes": 100}}}


@pytest.mark.parametrize("descriptor", [{"maxBytes": -1}, {"maxBytes": "many"},
    {"maxBytes": True}, {"maxBytes": 1, "spill": {}},
    {"maxBytes": 1, "spill": {"maxBytes": -1}}])
def test_invalid_collector_rejected_before_launch(tmp_path: Path, monkeypatch: Any,
                                                 descriptor: dict[str, Any]) -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "test"})
    calls: list[Any] = []
    monkeypatch.setattr(sl, "_fallback_launch", lambda *args: calls.append(args))
    request = spec(tmp_path)
    request["stdio"]["stdout"] = descriptor
    with pytest.raises(ValueError):
        runtime.spawn(request)
    assert calls == []


def test_bind_failure_terminates_real_child(tmp_path: Path, monkeypatch: Any) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    killed: list[bool] = []
    def stop() -> None:
        child.kill()
        child.wait(5)
        killed.append(True)
    runtime = sl.LocalSubprocessRuntime({"platform": "test"})
    monkeypatch.setattr(sl, "_fallback_launch", lambda *args: {
        "owner": {"terminateForHostExit": stop}})
    def fail(*args: Any) -> Any:
        raise RuntimeError("bind failed")
    monkeypatch.setattr(sl, "bind_managed_process", fail)
    try:
        with pytest.raises(RuntimeError, match="bind failed"):
            runtime.spawn(spec(tmp_path))
        assert killed == [True] and child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(5)


def test_cancel_race_and_failing_callback_notify_all() -> None:
    for _ in range(50):
        token = sl.CancellationToken()
        gate = threading.Barrier(3)
        fired: list[int] = []
        def broken() -> None:
            raise ValueError("listener failure")
        token.add_listener(broken)
        def register() -> None:
            gate.wait()
            token.add_listener(lambda: fired.append(1))
        def abort() -> None:
            gate.wait()
            token.abort()
        threads = [threading.Thread(target=register), threading.Thread(target=abort)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
        assert fired == [1] and len(token.listener_errors) == 1
        token.abort()
        assert fired == [1]


def test_seal_freezes_tail_and_spill(tmp_path: Path) -> None:
    collector = OutputCollector(3, "stdout", {"maxBytes": 100, "dir": str(tmp_path),
                                             "onFailure": lambda *args: None})
    collector.push(b"before")
    before = collector.finalize()
    snapshot = collector.snapshot()
    collector.push(b"late-output")
    assert collector.finalize() == before
    assert collector.snapshot() == snapshot
    assert Path(before["spillPath"]).read_bytes() == b"before"


def test_drain_timeout_result_is_immutable(tmp_path: Path) -> None:
    release = threading.Event()
    class Stream:
        read_once = False
        def read(self, size: int) -> bytes:
            release.wait(2)
            if self.read_once:
                return b""
            self.read_once = True
            return b"late-output"
    class Child:
        def wait(self) -> int:
            return 0
    handle = sl.bind_managed_process(spec(tmp_path), {
        "child": Child(), "stdout": Stream(), "directEvent": threading.Event(),
        "owner": {"signal": lambda *args: None, "waitForExit": lambda: None}})
    assert handle.wait_done(1)
    assert handle.result and handle.result["drainTimedOut"] is True
    initial = handle.finalize_collected()
    release.set()
    for reader in handle._reader_threads:
        reader.join(2)
    assert handle.finalize_collected() == initial


def test_scope_failed_kill_has_bounded_recheck(tmp_path: Path) -> None:
    files = sl.create_linux_launch_files({"cwd": str(tmp_path), "env": {}})
    event = threading.Event()
    event.set()
    queries: list[bool] = []
    def query(*args: Any) -> dict[str, Any]:
        queries.append(True)
        return {"status": 0, "stdout": "LoadState=loaded\nActiveState=active\nTasksCurrent=2\n"}
    owner = sl.SystemdScopeOwner("unit.scope", sl.LinuxScopeStartup(files, "test"),
        {"running": lambda: True, "settled": event}, query=query)
    owner.kill_failure = RuntimeError("kill failed")
    owner.direct_kill_settlement = event
    try:
        with pytest.raises(RuntimeError, match="kill failed"):
            owner.range_active()
        assert len(queries) == 2
    finally:
        owner.cleanup()


def test_bootstrap_actual_python_argv_and_error_channel(tmp_path: Path) -> None:
    files = sl.create_linux_launch_files({"cwd": str(tmp_path), "env": {}})
    # Use the actual -c argv protocol, with only execve replaced in this child.
    prefix = "import AstralNexus.runtime.subprocess_local as m; "
    prefix += "m.load_linux_execve=lambda: lambda *args: (_ for _ in ()).throw(FileNotFoundError(2, 'missing')); "
    source = prefix + sl.LINUX_BOOTSTRAP_SOURCE
    env = dict(os.environ, DSH_SUBPROCESS_RUNNER=files["requestPath"])
    try:
        result = subprocess.run([sys.executable, "-c", source, "--", "/missing/program", "arg"],
                                env=env, capture_output=True, timeout=10)
        assert result.returncode == 127, result.stderr
        error = sl.read_linux_startup_error(files["startupErrorPath"])
        assert error and error["error"]["path"] == "/missing/program"
        assert not Path(files["requestPath"]).exists()
        with pytest.raises(Exception, match="ENOENT"):
            sl.LinuxScopeStartup(files, "test").resolve_outcome({"exitCode": 127})
    finally:
        sl.cleanup_linux_launch_files(files)


def test_teardown_hanging_owner_bounded_and_unregisters(tmp_path: Path, monkeypatch: Any) -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "test", "teardownTimeoutMs": 30})
    release = threading.Event()
    class Handle:
        done_event = threading.Event()
        def terminate(self) -> None:
            release.wait(2)
        def terminate_for_host_exit(self) -> None:
            release.wait(2)
    runtime.managed.add(Handle())  # type: ignore[arg-type]
    runtime._host_exit_registered = True
    removed: list[Any] = []
    monkeypatch.setattr("atexit.unregister", lambda fn: removed.append(fn))
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            runtime.close()
        assert time.monotonic() - start < 1
        assert len(removed) == 1 and not runtime._host_exit_registered
        assert len(runtime.managed) == 1  # timeout is not proof of cleanup
    finally:
        release.set()


def test_range_observer_shared_by_concurrent_waiters(tmp_path: Path) -> None:
    release = threading.Event()
    calls: list[int] = []
    class Child:
        def wait(self) -> int:
            return 0
    def observe() -> None:
        calls.append(1)
        release.wait(2)
    handle = sl.bind_managed_process(spec(tmp_path), {"child": Child(),
        "directEvent": threading.Event(), "owner": {"waitForExit": observe}})
    observers: list[threading.Thread] = []
    threads = [threading.Thread(target=lambda: observers.append(handle.observe_range_exit()))
               for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    release.set()
    assert len({id(thread) for thread in observers}) == 1
    assert handle.wait_for_exit() and calls == [1]


def test_linux_scope_production_resolver_and_cleanup(tmp_path: Path, monkeypatch: Any) -> None:
    created: list[dict[str, str]] = []
    original = sl.create_linux_launch_files
    def create(request: Any) -> dict[str, str]:
        files = original(request)
        created.append(files)
        return files
    monkeypatch.setattr(sl, "create_linux_launch_files", create)
    class Child:
        pid = 1234
        stdin = None
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        def poll(self) -> int:
            return 127
        def wait(self) -> int:
            return 127
    def spawn(*args: Any, **kwargs: Any) -> Child:
        files = sl.linux_launch_files_from_locator(kwargs["env"]["DSH_SUBPROCESS_RUNNER"])
        sl.consume_linux_launch_request(files["requestPath"])
        sl.write_linux_startup_error(files, {"type": "error", "error":
            sl.as_spawn_error(FileNotFoundError(2, "missing"), "missing-program")})
        return Child()
    runtime = sl.LocalSubprocessRuntime({"platform": "linux", "spawn": spawn,
        "spawnSync": lambda *args: {"status": 0}, "systemctlQuery": lambda *args:
        {"status": 0, "stdout": "LoadState=loaded\nActiveState=inactive\n"}})
    monkeypatch.setattr(runtime, "select_containment_mode", lambda kind: "linux-scope")
    handle = runtime.spawn(spec(tmp_path))
    assert handle.wait_done(1)
    assert handle.error and "ENOENT" in str(handle.error)
    assert handle.result is None
    assert handle.wait_for_exit()
    assert not Path(created[0]["directory"]).exists()
    runtime.close()


def test_windows_collection_uses_visible_fallback(tmp_path: Path, monkeypatch: Any) -> None:
    warnings: list[str] = []
    class Logger:
        def warn(self, message: str) -> None:
            warnings.append(message)
    runtime = sl.LocalSubprocessRuntime({"platform": "win32", "logger": Logger()})
    monkeypatch.setattr(sl, "probe_windows_job", lambda *args: True)
    handle = runtime.spawn(spec(tmp_path))
    assert handle.wait_done(5)
    assert handle.finalize_collected()["stdout"]["text"].strip() == "ok"
    runtime.close()
    assert len(warnings) == 1 and "pipe/data stdio" in warnings[0]


@pytest.mark.parametrize("failure_stage", ["resolve", "spawn", "owner"])
def test_windows_job_failure_closes_owned_resources(tmp_path: Path, monkeypatch: Any,
                                                   failure_stage: str) -> None:
    closed: list[int] = []
    killed: list[int] = []
    def close_handle(handle: int) -> bool:
        closed.append(handle)
        return True
    def terminate_job(handle: int, code: int) -> bool:
        killed.append(handle)
        return True
    api: dict[str, Any] = {"closeHandle": close_handle,
                           "terminateJobObject": terminate_job}
    def std(selector: int) -> int:
        raise RuntimeError("resolve failure")
    api["getStdHandle"] = std
    monkeypatch.setattr(sl, "_open_nul", lambda: 11)
    def spawn(*args: Any) -> dict[str, int]:
        if failure_stage == "spawn":
            raise RuntimeError("spawn failure")
        return {"job": 22, "process": 33, "pid": 44}
    monkeypatch.setattr(sl.win32_process, "spawn_current_token_job_process", spawn)
    def owner(*args: Any) -> Any:
        raise RuntimeError("owner failure")
    monkeypatch.setattr(sl, "WindowsJobOwner", owner)
    request = spec(tmp_path)
    request["stdio"] = {"stdin": "ignore", "stdout":
                        "inherit" if failure_stage == "resolve" else "ignore",
                        "stderr": "ignore"}
    with pytest.raises(RuntimeError, match=failure_stage + " failure"):
        sl.launch_windows_job(request, "prog", {}, {"bindings": api})
    assert closed.count(11) == 1
    if failure_stage == "owner":
        assert set(closed) == {11, 22, 33} and killed == [22]
    else:
        assert closed == [11]
