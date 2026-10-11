"""Tests for runtime/subprocess_local.py — the local-subprocess seam.

Covers the R12 subset surface: environment/validation plane (verbatim
childEnv tombstones and Node null-byte message shapes), the libuv-order
Windows executable resolution, spawn error normalization, taskkill/tree
signalling, the Linux /proc inspection plane (stdin-blocking syscall
detector), process inspectors, fallback owner liveness, the
bindManagedProcess lifecycle (collector objects, {data} stdin, the
SIGTERM→grace→SIGKILL ladder, abort integration, pipe-drain race),
OutputCollector spill semantics, the systemd scope state machine with fake
seams, launch-file plumbing, containment selection with the verbatim
fallback warnings, and real win32 spawns through both the Job direct form
and the pipe-free fallback form (the sandbox refuses CreatePipe for
probe/test processes, so pipe-stdio paths use injectable fakes — upstream's
own strategy for thin OS bindings).
"""
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from AstralNexus.runtime import subprocess_local as sl
from AstralNexus.runtime import win32_process as wp
from AstralNexus.runtime.subprocess_output import OutputCollector
from AstralNexus.runtime.subprocess_runtime import (
    SUBPROCESS_CONTROL_ENV, SubprocessExecutableNotFoundError)

IS_WIN32 = sys.platform == "win32"
CMD = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                   "System32", "cmd.exe")


# ------------------------------------------------------------ fake child

class FakeStdin:
    def __init__(self) -> None:
        self.written: List[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True


class FakeChild:
    """Popen-shaped fake with in-memory stdio."""

    def __init__(self, exit_code: int = 0, stdout: bytes = b"",
                 stderr: bytes = b"", pid: int = 4321,
                 wait_delay: float = 0.0) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.stdin = FakeStdin()
        self._waited = False
        self._delay = wait_delay

    def poll(self) -> Optional[int]:
        return self.exit_code if self._waited else None

    def wait(self, timeout: Optional[float] = None) -> int:
        if self._delay:
            time.sleep(self._delay)
        self._waited = True
        return self.exit_code


class FakeOwner:
    def __init__(self, block_wait: Optional[threading.Event] = None) -> None:
        self.signals: List[str] = []
        self.wait_calls = 0
        self.host_exit_calls = 0
        self._block = block_wait

    def as_dict(self) -> Dict[str, Any]:
        return {"signal": self.signal, "waitForExit": self.wait,
                "terminateForHostExit": self.host_exit}

    def signal(self, name: str) -> None:
        self.signals.append(name)

    def wait(self) -> None:
        self.wait_calls += 1
        if self._block is not None:
            self._block.wait(10)

    def host_exit(self) -> None:
        self.host_exit_calls += 1


def make_launch(child: FakeChild, owner: Optional[FakeOwner] = None
                ) -> Dict[str, Any]:
    owner = owner or FakeOwner()
    return {"child": child, "stdin": child.stdin, "stdout": child.stdout,
            "stderr": child.stderr, "control": None,
            "directEvent": threading.Event(), "owner": owner.as_dict(),
            "pid": child.pid}


def base_spec(**extra: Any) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "argv": ["prog", "arg"], "cwd": "C:\\work" if IS_WIN32 else "/work",
        "graceMs": 100,
        "stdio": {"stdin": "ignore",
                  "stdout": {"maxBytes": 4096},
                  "stderr": {"maxBytes": 4096}},
    }
    spec.update(extra)
    return spec


# ------------------------------------------------------------ env plane

def test_child_env_posix_merge_keeps_tombstones(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "linux")
    # a non-scrubbed variable name (DSH_* is filtered by the scrubber)
    monkeypatch.setenv("ASTRALNX_TEST_INHERITED", "parent")
    env = sl.child_env({"NEW": "v", "GONE": None})
    assert env["NEW"] == "v"
    assert "GONE" in env and env["GONE"] is None  # tombstone kept here
    assert env["ASTRALNX_TEST_INHERITED"] == "parent"


def test_child_env_windows_case_insensitive_replace(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "win32")
    env = sl.child_env({"Path": "C:\\bin"})
    assert env["Path"] == "C:\\bin"
    assert "PATH" not in env  # the inherited differently-cased key is gone


def test_environment_value_case_semantics(monkeypatch: pytest.MonkeyPatch
                                          ) -> None:
    monkeypatch.setattr(sl.sys, "platform", "win32")
    assert sl.environment_value({"PaTh": "x"}, "PATH") == "x"
    monkeypatch.setattr(sl.sys, "platform", "linux")
    assert sl.environment_value({"PaTh": "x"}, "PATH") is None


def test_windows_environment_value_sorted_keys() -> None:
    # verbatim: sorted keys, exact uppercase comparison
    assert sl.windows_environment_value({"path": "p"}, "PATH") == "p"
    assert sl.windows_environment_value({"PATH": "p"}, "path") is None


def test_control_environment_reserves_marker() -> None:
    with pytest.raises(RuntimeError, match="reserved for subprocess"):
        sl.control_environment({SUBPROCESS_CONTROL_ENV: "pipe"}, None)
    with pytest.raises(RuntimeError, match="reserved"):
        sl.control_environment({SUBPROCESS_CONTROL_ENV.lower(): "x"}, None)
    env = sl.control_environment({}, "pipe")
    assert env[SUBPROCESS_CONTROL_ENV] == "pipe"
    assert SUBPROCESS_CONTROL_ENV not in sl.control_environment({}, None)


def test_null_byte_message_shapes() -> None:
    with pytest.raises(TypeError) as argument_form:
        sl.validate_no_null_byte("file", "a\0b", True)
    assert str(argument_form.value).startswith(
        "The argument 'file' must be a string without null bytes.")
    assert argument_form.value.code == "ERR_INVALID_ARG_VALUE"  # type: ignore[attr-defined]
    with pytest.raises(TypeError) as property_form:
        sl.validate_no_null_byte("options.cwd", "a\0b")
    assert str(property_form.value).startswith(
        "The property 'options.cwd' must be a string without null bytes.")
    sl.validate_no_null_byte("file", "clean", True)  # no raise


def test_validate_subprocess_spec_verbatim_messages() -> None:
    sl.validate_subprocess_spec(base_spec())  # no raise
    ceiling = sl.MAX_TIMER_DELAY_MS
    for bad in (0, -1, float("nan"), float("inf"), ceiling + 1, "5", None):
        with pytest.raises(RuntimeError,
                           match="subprocess graceMs must be a positive "
                                 "finite number"):
            sl.validate_subprocess_spec(base_spec(graceMs=bad))
    token = sl.CancellationToken()
    token.abort(RuntimeError("nope"))
    with pytest.raises(RuntimeError, match="aborted before spawn: nope"):
        sl.validate_subprocess_spec(base_spec(signal=token))
    with pytest.raises(RuntimeError,
                       match="invalid argv: expected a non-empty program"):
        sl.validate_subprocess_spec(base_spec(argv=["", "x"]))
    with pytest.raises(RuntimeError, match="invalid argv"):
        sl.validate_subprocess_spec(base_spec(argv=[]))


def test_target_environment_filters_tombstones_and_stamps(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "linux")
    spec = base_spec(env={"CUSTOM": "1", "GONE": None},
                     stdio={"stdin": "ignore", "control": "pipe"})
    env = sl.target_environment(spec)
    assert env["CUSTOM"] == "1"
    assert "GONE" not in env
    assert env[SUBPROCESS_CONTROL_ENV] == "pipe"


def test_target_environment_null_byte_contexts(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "linux")
    with pytest.raises(TypeError, match=r"The argument 'file'"):
        sl.target_environment(base_spec(argv=["p\0", "x"]))
    with pytest.raises(TypeError, match=r"The argument 'args\[1\]'"):
        sl.target_environment(base_spec(argv=["p", "ok", "b\0d"]))
    with pytest.raises(TypeError, match=r"The property 'options.cwd'"):
        sl.target_environment(base_spec(cwd="/w\0rk"))
    with pytest.raises(TypeError, match=r"The property 'options.env"):
        sl.target_environment(base_spec(env={"K": "v\0"}))


# --------------------------------------------- windows executable resolve

def test_windows_file_name_start_scans_to_last_separator() -> None:
    assert sl.windows_file_name_start("prog") == 0
    assert sl.windows_file_name_start("C:\\dir\\prog.exe") == 7
    assert sl.windows_file_name_start("\\\\srv\\share\\p") == 12
    assert sl.windows_file_name_start("rel/prog") == 4


def test_windows_path_directories_scanner() -> None:
    assert sl.windows_path_directories("a;b;;c") == ["a", "b", "c"]
    # a leading quote runs to its closing quote; quotes are stripped
    assert sl.windows_path_directories('"x;y";z') == ["x;y", "z"]
    assert sl.windows_path_directories("") == []
    assert sl.windows_path_directories('a"b;c') == ['a"b', "c"]


def test_windows_search_path_join_prefix_rules() -> None:
    # empty root borrows the cwd
    assert sl.windows_search_path_join("", "prog.exe", "C:\\work") == \
        "C:\\work\\prog.exe"
    # an absolute drive root keeps no prefix
    assert sl.windows_search_path_join("C:\\bin", "prog.exe", "D:\\work") == \
        "C:\\bin\\prog.exe"
    # a leading slash borrows the cwd drive
    assert sl.windows_search_path_join("\\bin", "prog.exe", "C:\\work") == \
        "C:\\bin\\prog.exe"
    # UNC keeps no prefix
    assert sl.windows_search_path_join("\\\\srv\\share", "p", "C:\\w") == \
        "\\\\srv\\share\\p"
    # a drive-relative root on a different drive keeps no prefix (the
    # verbatim append rule counts ":" as a trailing separator)
    assert sl.windows_search_path_join("D:", "p", "C:\\work") == "D:p"
    # same drive: the root collapses into the cwd
    assert sl.windows_search_path_join("C:sub", "p", "C:\\work") == \
        "C:\\work\\sub\\p"


def test_windows_executable_names_rules() -> None:
    assert sl.windows_executable_names("prog", "prog") == [
        "prog.com", "prog.exe"]
    assert sl.windows_executable_names("C:\\x\\prog.exe", "prog.exe") == [
        "C:\\x\\prog.exe", "C:\\x\\prog.exe.com", "C:\\x\\prog.exe.exe"]
    # a trailing dot needs no separator and counts as no extension
    assert sl.windows_executable_names("prog.", "prog.") == [
        "prog.com", "prog.exe"]


def test_executable_candidate_exists(tmp_path: Path) -> None:
    real = tmp_path / "prog.exe"
    real.write_bytes(b"x")
    assert sl.executable_candidate_exists(str(real)) is True
    assert sl.executable_candidate_exists(str(tmp_path)) is False
    assert sl.executable_candidate_exists(
        str(tmp_path / "missing.exe")) is False


def test_resolve_windows_executable_search_order(tmp_path: Path) -> None:
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"x")
    seen: List[str] = []

    def exists(candidate: str) -> bool:
        seen.append(candidate)
        return sl.executable_candidate_exists(candidate)

    resolved = sl.resolve_windows_executable(
        "tool", "C:\\work", {"PATH": str(tmp_path)}, exists,
        current_env={"PATH": str(tmp_path)})
    assert resolved == str(tool)
    # cwd root first (NoDefault absent from current_env), .com then .exe,
    # then the PATH root
    assert seen[0] == "C:\\work\\tool.com"
    assert seen[1] == "C:\\work\\tool.exe"
    assert any(str(tmp_path) in entry for entry in seen[2:])
    # an explicit path searches only its own directory
    direct = sl.resolve_windows_executable(
        str(tmp_path / "tool"), str(tmp_path), {},
        current_env={})
    assert direct == str(tool)
    assert sl.resolve_windows_executable(
        "definitely-missing-tool", "C:\\work", {},
        current_env={}) is None


def test_resolve_windows_executable_nodefault_policy(tmp_path: Path) -> None:
    seen: List[str] = []

    def exists(candidate: str) -> bool:
        seen.append(candidate)
        return False

    # with the policy variable set and an empty PATH, no roots remain:
    # the cwd root is skipped entirely and nothing is probed
    resolved = sl.resolve_windows_executable(
        "tool", str(tmp_path), {}, exists,
        current_env={"NODEFAULTCURRENTDIRECTORYINEXEPATH": "1"})
    assert resolved is None
    assert seen == []


# ------------------------------------------------------------ error plane

def test_serialize_deserialize_roundtrip() -> None:
    error = FileNotFoundError(2, "No such file")
    error.code = "ENOENT"  # type: ignore[attr-defined]
    error.syscall = "spawn"  # type: ignore[attr-defined]
    error.path = "prog"  # type: ignore[attr-defined]
    record = sl.serialize_runner_error(error)
    assert record["code"] == "ENOENT"
    restored = sl.deserialize_runner_error(record)
    assert restored.code == "ENOENT"  # type: ignore[attr-defined]
    assert restored.syscall == "spawn"  # type: ignore[attr-defined]


def test_as_spawn_error_windows_code_map() -> None:
    record = sl.as_spawn_error(
        wp.Win32Error("CreateProcessW", 2, "not found"), "prog")
    assert record["code"] == "ENOENT"
    assert record["path"] == "prog"
    assert record["message"] == "spawn prog ENOENT"
    eperm = sl.as_spawn_error(wp.Win32Error("CreateProcessW", 5), "prog")
    assert eperm["code"] == "EPERM"
    unknown = sl.as_spawn_error(wp.Win32Error("CreateProcessW", 999), "prog")
    assert unknown["code"] == "UNKNOWN"
    assert unknown["message"] == "spawn UNKNOWN"


def test_as_spawn_error_posix_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "linux")
    record = sl.as_spawn_error(PermissionError(13, "denied"), "prog")
    assert record["code"] == "EACCES"


# --------------------------------------------------------- signalling

def test_taskkill_process_tree_builds_flags(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []

    def fake_run(args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    sl.taskkill_process_tree(99)
    assert calls[0][0] == ["taskkill", "/PID", "99", "/T", "/F"]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    sl.taskkill_process_tree(None)  # guard: no call
    sl.taskkill_process_tree(0)
    assert len(calls) == 1


def test_taskkill_process_tree_swallows_failures(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("denied")

    monkeypatch.setattr(sl.subprocess, "run", boom)
    sl.taskkill_process_tree(99)  # must not raise


def test_signal_tree_win32_always_forces() -> None:
    killed: List[int] = []
    sl.signal_tree("win32", 77, "SIGTERM", None, killed.append)
    sl.signal_tree("win32", 77, "SIGKILL", None, killed.append)
    assert killed == [77, 77]  # taskkill /F for any signal
    no_pid: List[int] = []
    sl.signal_tree("win32", None, "SIGTERM", None, no_pid.append)
    assert no_pid == []


def test_signal_tree_group_first_posix(monkeypatch: pytest.MonkeyPatch
                                       ) -> None:
    kills: List[Tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))
        if pid < 0:
            raise OSError(sl.errno_module.ESRCH, "group gone")

    monkeypatch.setattr(sl.os, "kill", fake_kill)
    sl.signal_tree("linux", 77, "SIGTERM", None, lambda pid: None)
    assert [pid for pid, _ in kills] == [-77, 77]  # group, then direct child


# -------------------------------------------------------- linux /proc

def fake_proc_internals(files: Dict[str, Any],
                        dirs: Optional[Dict[str, List[str]]] = None
                        ) -> Dict[str, Any]:
    return {
        "readFile": lambda path: files[path],
        "readDir": lambda path: (dirs or {}).get(path, []),
        "stat": os.stat,
        "readLink": lambda path: files[path],
        "open": lambda path: None,
        "read": lambda fd, length, offset: files["mem"][:length],
        "close": lambda fd: None,
        "kill": os.kill,
    }


def stat_text(pid: int, ppid: int, pgrp: int, state: str, started: int
              ) -> str:
    """Build a /proc/pid/stat line whose rest[19] is the start time."""
    filler = " ".join(["0"] * 13)
    return (f"{pid} (sh) {state} {ppid} {pgrp} {pgrp} 0 -1 {filler} "
            f"{started} junk")


def test_parse_proc_stat_handles_comm_parentheses() -> None:
    row = sl.parse_proc_stat(
        "101 (weird (name) proc) S 100 101 101 0 -1 "
        + " ".join(["0"] * 13) + " 12345 rest")
    assert row is not None
    assert row["pid"] == 101
    assert row["parentPid"] == 100
    assert row["pgrp"] == 101
    assert row["state"] == "S"
    assert row["started"] == "12345"
    assert sl.parse_proc_stat("garbage") is None
    assert sl.parse_proc_stat("1 (x) S") is None  # too few fields


def test_linux_process_group_has_live_members() -> None:
    files = {
        "/proc/10/stat": stat_text(10, 1, 10, "S", 100),
        "/proc/11/stat": stat_text(11, 1, 10, "Z", 200),
        "/proc/12/stat": stat_text(12, 1, 99, "S", 300),
    }
    api = fake_proc_internals(files, {"/proc": ["10", "11", "12"]})
    assert sl.linux_process_group_has_live_members(10, api) is True
    zombie_only = fake_proc_internals(
        {"/proc/11/stat": files["/proc/11/stat"]}, {"/proc": ["11"]})
    assert sl.linux_process_group_has_live_members(10, zombie_only) is False
    assert sl.linux_process_group_has_live_members(77, api) is None
    broken = fake_proc_internals({}, {})
    broken["readDir"] = lambda path: (_ for _ in ()).throw(OSError())
    assert sl.linux_process_group_has_live_members(10, broken) is None


def test_read_syscall_shapes() -> None:
    files = {"/proc/1/task/1/syscall": "0 0x0 0x1 0x2 0x3 0x4 0x5 0x7f 0x0"}
    info = sl.read_syscall(fake_proc_internals(files), 1, 1)
    assert info is not None and info["number"] == 0
    running = fake_proc_internals({"/proc/1/task/1/syscall": "running"})
    assert sl.read_syscall(running, 1, 1) is None
    short = fake_proc_internals({"/proc/1/task/1/syscall": "0 0x1 0x2"})
    assert sl.read_syscall(short, 1, 1) is None


def test_syscall_waits_on_stdin_read() -> None:
    tables = sl.linux_syscall_tables("x64")
    assert tables is not None
    waiting = {"number": 0, "args": [0, 1, 2, 3, 4, 5]}
    assert sl.syscall_waits_on_stdin({}, 5, 5, waiting, tables) is True
    other_fd = {"number": 0, "args": [1, 1, 2, 3, 4, 5]}
    assert sl.syscall_waits_on_stdin({}, 5, 5, other_fd, tables) is False


def test_syscall_waits_on_stdin_select_and_poll() -> None:
    tables = sl.linux_syscall_tables("x64")
    assert tables is not None
    # select(23): a0 = nfds >= 1, a1 = readfds address; fd_set bit 0 = stdin
    api = fake_proc_internals(
        {"mem": b"\x01\x00\x00\x00\x00\x00\x00\x00"})
    select_syscall = {"number": 23, "args": [4, 0x1000, 0, 0, 0, 0]}
    assert sl.syscall_waits_on_stdin(api, 5, 5, select_syscall,
                                     tables) is True
    # poll(7): a0 = fds address, a1 = nfds; pollfd {fd=0, events&POLLIN}
    api2 = fake_proc_internals(
        {"mem": b"\x00\x00\x00\x00\x01\x00\x00\x00"})
    poll_syscall = {"number": 7, "args": [0x2000, 1, 0xFFFFFFFF, 0, 0, 0]}
    assert sl.syscall_waits_on_stdin(api2, 5, 5, poll_syscall,
                                     tables) is True


def test_syscall_waits_on_stdin_epoll_fdinfo() -> None:
    tables = sl.linux_syscall_tables("x64")
    assert tables is not None
    epoll_syscall = {"number": 232, "args": [3, 0x4000, 1, 0, 0, 0]}
    api = fake_proc_internals({
        "/proc/5/task/5/fdinfo/3":
            "pos:\t0\nflags:\t02000002\ntfd:        0 events:       19\n"})
    assert sl.syscall_waits_on_stdin(api, 5, 5, epoll_syscall,
                                     tables) is True
    api2 = fake_proc_internals({
        "/proc/5/task/5/fdinfo/3": "pos:\t0\ntfd:        4 events: 1\n"})
    assert sl.syscall_waits_on_stdin(api2, 5, 5, epoll_syscall,
                                     tables) is False


def test_syscall_unknown_number_is_not_waiting() -> None:
    tables = sl.linux_syscall_tables("x64")
    assert tables is not None
    unknown = {"number": 999, "args": [0, 0, 0, 0, 0, 0]}
    # verbatim: every table missing the number is NOT waiting (False)
    assert sl.syscall_waits_on_stdin({}, 5, 5, unknown, tables) is False


def test_linux_syscall_tables_ordering() -> None:
    tables = sl.linux_syscall_tables("arm64")
    assert tables is not None
    assert tables[0] is sl.SYSCALLS["arm64"]
    assert tables[1] is sl.SYSCALLS["x64"]
    assert sl.linux_syscall_tables("mips") is None
    # arm64 carries no select number: the x64 table still decides it
    api = fake_proc_internals(
        {"mem": b"\x01\x00\x00\x00\x00\x00\x00\x00"})
    select_syscall = {"number": 23, "args": [4, 0x1000, 0, 0, 0, 0]}
    assert sl.syscall_waits_on_stdin(api, 5, 5, select_syscall,
                                     tables) is True


def test_read_linux_terminal_device_rules() -> None:
    files = {"/proc/5/fd/0": "/dev/tty"}
    api = fake_proc_internals(files)
    assert sl.read_linux_terminal_device(api, 5, 0x1337) == 0x1337
    assert sl.read_linux_terminal_device(api, 5, 0) is None


# ------------------------------------------------------------ inspectors

def _row(pid: int, ppid: int, started: Any, state: str = "S",
         session: Optional[int] = None) -> Dict[str, Any]:
    return {"pid": pid, "parentPid": ppid, "started": started,
            "session": pid if session is None else session,
            "state": state}


def test_process_tree_children_first_root_last() -> None:
    rows = [_row(1, 0, 10), _row(2, 1, 20), _row(3, 2, 30, state="Z"),
            _row(4, 3, 40), _row(5, 1, 50)]
    members = sl.process_tree(rows, 1)
    # children-first DFS, zombie rows join, the root lands last
    assert [member["pid"] for member in members] == [4, 3, 2, 5, 1]
    assert members[-1] == {"pid": 1, "started": 10}
    assert sl.process_tree(rows, 99) == []


def test_posix_snapshot_faces() -> None:
    rows = [_row(1, 0, 10, session=7), _row(2, 1, 20, session=7),
            _row(3, 1, 30, state="Z", session=7)]
    observed = sl.PosixProcessSnapshot(rows, True)
    assert [member["pid"] for member in observed.tree(1)] == [2, 3, 1]
    assert len(observed.session(7)) == 3
    assert observed.session(8) == []
    assert observed.alive({"pid": 2, "started": 20}) is True
    assert observed.alive({"pid": 2, "started": 999}) is False
    assert observed.alive({"pid": 3, "started": 30}) is False  # zombie
    assert observed.alive({"pid": 9, "started": 1}) is False


def _linux_stat_line(pid: int, ppid: int, pgrp: int, state: str,
                     started: int, tty: int = 0, tpgid: int = -1) -> str:
    filler = " ".join(["0"] * 13)
    return (f"{pid} (sh) {state} {ppid} {pgrp} {pgrp} {tty} {tpgid} "
            f"{filler} {started} junk")


def test_linux_inspector_faces() -> None:
    api = fake_proc_internals(
        {"/proc/5/stat": _linux_stat_line(5, 1, 5, "S", 999)},
        {"/proc": ["5"]})
    inspector = sl.LinuxProcessInspector("x64", api)
    observed = inspector.snapshot()
    assert observed.complete is True
    assert [row["pid"] for row in observed.rows] == [5]
    identity = {"pid": 5, "started": "999"}
    assert inspector.isAlive(identity) is True
    assert inspector.isAlive({"pid": 5, "started": "111"}) is False
    zombie_api = fake_proc_internals(
        {"/proc/5/stat": _linux_stat_line(5, 1, 5, "Z", 999)},
        {"/proc": ["5"]})
    assert sl.LinuxProcessInspector("x64", zombie_api).isAlive(
        identity) is False
    # tpgid > 0 is the foreground group; -1/0 are not
    assert inspector.foregroundPgid(5) is None
    tpgid_api = fake_proc_internals(
        {"/proc/5/stat": _linux_stat_line(5, 1, 5, "S", 999, tpgid=77)},
        {"/proc": ["5"]})
    assert sl.LinuxProcessInspector("x64", tpgid_api).foregroundPgid(
        5) == 77
    # an unreadable entry marks the observation incomplete
    partial = fake_proc_internals({}, {"/proc": ["5"]})
    partial["readFile"] = lambda path: (_ for _ in ()).throw(OSError())
    assert sl.LinuxProcessInspector("x64", partial).snapshot(
    ).complete is False
    # an unreadable /proc raises the verbatim message
    broken = fake_proc_internals({}, {})
    broken["readDir"] = lambda path: (_ for _ in ()).throw(OSError())
    with pytest.raises(RuntimeError, match="/proc directory is unreadable"):
        sl.LinuxProcessInspector("x64", broken).snapshot()


def test_linux_inspector_stdin_waiting() -> None:
    # shell 5 (pgrp 5, tty 0x1337); member 7 thread 7 blocked in read(0)
    files = {
        "/proc/5/stat": _linux_stat_line(5, 1, 5, "S", 999, tty=0x1337),
        "/proc/5/fd/0": "/dev/tty",
        "/proc/7/stat": _linux_stat_line(7, 5, 5, "S", 777, tty=0x1337),
        "/proc/7/task/7/syscall": "0 0x0 0x1 0x2 0x3 0x4 0x5 0x7f 0x0",
        "/proc/7/fd/0": "/dev/tty",
        "/proc/7/task/7/fd/0": "/dev/tty",
    }
    dirs = {"/proc": ["5", "7"], "/proc/7/task": ["7"]}
    api = fake_proc_internals(files, dirs)
    inspector = sl.LinuxProcessInspector("x64", api)
    assert inspector.isStdinWaiting(5, 5) is True
    # unknown arch: no tables, never waiting
    unknown = sl.LinuxProcessInspector("riscv", api)
    assert unknown.isStdinWaiting(5, 5) is False


def test_linux_inspector_group_signals() -> None:
    files = {"/proc/5/stat": _linux_stat_line(5, 1, 5, "S", 999)}
    api = fake_proc_internals(files, {"/proc": ["5"]})
    sent: List[Any] = []
    api["kill"] = lambda pid, signal_name: sent.append((pid, signal_name))
    inspector = sl.LinuxProcessInspector("x64", api)
    inspector.signalGroup(5, "SIGTERM")
    assert sent == [(-5, "SIGTERM")]
    inspector.signalProcess({"pid": 5, "started": "999"}, "SIGKILL")
    assert sent[-1] == (5, "SIGKILL")
    # a stale identity is not signalled
    inspector.signalProcess({"pid": 5, "started": "111"}, "SIGKILL")
    assert sent[-1] == (5, "SIGKILL")


def test_mac_inspector_faces() -> None:
    ps_output = ("  10   1 Mon Jan  1 00:00:00 2026\n"
                 "  20  10 Mon Jan  1 00:00:01 2026\n")
    calls: List[Any] = []

    def fake_exec(file: str, args: Sequence[str]) -> str:
        calls.append((file, tuple(args)))
        if list(args)[:2] == ["-o", "tpgid="]:
            return "  20\n"
        return ps_output

    inspector = sl.MacProcessInspector({"exec": fake_exec})
    observed = inspector.snapshot()
    assert observed.complete is True
    assert [row["pid"] for row in observed.rows] == [10, 20]
    assert [row["parentPid"] for row in observed.rows] == [1, 10]
    assert inspector.foregroundPgid(10) == 20
    assert inspector.isStdinWaiting(20, 10) is False
    assert inspector.isAlive(
        {"pid": 20, "started": "Mon Jan  1 00:00:01 2026"}) is True
    assert inspector.isAlive({"pid": 20, "started": "nope"}) is False
    assert ("ps", ("-o", "tpgid=", "-p", "10")) in [
        (os.path.basename(file), args) for file, args in calls]

    def broken_exec(file: str, args: Sequence[str]) -> str:
        return "  10   1 Mon Jan  1 00:00:00 2026\nnot-a-row\n"

    broken = sl.MacProcessInspector({"exec": broken_exec})
    assert broken.snapshot().complete is False

    def zero_tpgid(file: str, args: Sequence[str]) -> str:
        return "  -1\n" if list(args)[:2] == ["-o", "tpgid="] else ps_output

    assert sl.MacProcessInspector(
        {"exec": zero_tpgid}).foregroundPgid(10) is None


def test_windows_inspector_identity_and_liveness() -> None:
    processes: List[Dict[str, Any]] = [
        {"pid": 10, "parentPid": 1}, {"pid": 20, "parentPid": 10}]
    states: Dict[int, Any] = {
        10: {"started": "1:100", "active": True},
        20: {"started": "1:200", "active": True}}
    killed: List[Any] = []
    inspector = sl.WindowsProcessInspector(internals={
        "snapshot": lambda: processes,
        "processState": lambda pid: states.get(pid),
        "taskkill": lambda pid, force: killed.append((pid, force)),
    })
    # the shell pid is the pseudo process group on Windows
    assert inspector.foregroundPgid(10) == 10
    assert inspector.isStdinWaiting(10, 10) is False
    identity = {"pid": 10, "started": "1:100"}
    assert inspector.isAlive(identity) is True
    states[10] = {"started": "1:100", "active": False}
    assert inspector.isAlive(identity) is False
    states[10] = {"started": "2:999", "active": True}
    assert inspector.isAlive(identity) is False
    states[10] = {"started": "1:100", "active": True}
    observed = inspector.snapshot()
    assert observed.session(1) == []
    assert [member["pid"] for member in observed.tree(10)] == [20, 10]
    # an unreadable identity skips the member but keeps descending
    states[20] = None
    processes.append({"pid": 30, "parentPid": 20})
    states[30] = {"started": "1:300", "active": True}
    fresh = inspector.snapshot()
    assert [member["pid"] for member in fresh.tree(10)] == [30, 10]
    # signals route to taskkill; SIGKILL forces
    inspector.signalGroup(10, "SIGTERM")
    assert killed == [(10, False)]
    inspector.signalProcess(identity, "SIGKILL")
    assert killed[-1] == (10, True)
    # an unreadable state is not alive: signalProcess no-ops
    states[10] = None
    inspector.signalProcess(identity, "SIGKILL")
    assert killed[-1] == (10, True)
    assert fresh.alive({"pid": 30, "started": "1:300"}) is True


def test_create_process_inspector_dispatch() -> None:
    api = fake_proc_internals({}, {})
    assert isinstance(sl.create_process_inspector("linux", "x64", api),
                      sl.LinuxProcessInspector)
    mac_api = {"exec": lambda file, args: ""}
    assert isinstance(sl.create_process_inspector("darwin", "arm64",
                                                  mac_api),
                      sl.MacProcessInspector)
    assert isinstance(sl.create_process_inspector("win32", "x64", api),
                      sl.WindowsProcessInspector)
    with pytest.raises(RuntimeError, match="terminal inspection is "
                                           "unsupported on platform plan9"):
        sl.create_process_inspector("plan9", "x64", api)


# ------------------------------------------------------- fallback owner

def test_fallback_owner_win32_liveness() -> None:
    child = FakeChild()
    owner = sl.fallback_owner("win32", 77, child, lambda pid: None,
                              lambda pid: None, threading.Event())
    assert owner["alive"]() is True
    child.wait()  # settles the exit code
    assert owner["alive"]() is False
    owner["signal"]("SIGTERM")  # dead range: latches stopped, no signal


def test_fallback_owner_posix_esrch_and_eperm(
        monkeypatch: pytest.MonkeyPatch) -> None:
    child = FakeChild()

    def esrch_kill(pid: int, sig: int) -> None:
        raise OSError(sl.errno_module.ESRCH, "gone")

    monkeypatch.setattr(sl.os, "kill", esrch_kill)
    owner = sl.fallback_owner("darwin", 77, child, lambda pid: None,
                              lambda pid: None, threading.Event())
    assert owner["alive"]() is False

    def eperm_kill(pid: int, sig: int) -> None:
        raise OSError(sl.errno_module.EPERM, "exists but not ours")

    monkeypatch.setattr(sl.os, "kill", eperm_kill)
    owner2 = sl.fallback_owner("darwin", 77, child, lambda pid: None,
                               lambda pid: None, threading.Event())
    assert owner2["alive"]() is True


def test_fallback_owner_linux_group_empty_fence(
        monkeypatch: pytest.MonkeyPatch) -> None:
    child = FakeChild()
    monkeypatch.setattr(sl.os, "kill", lambda pid, sig: None)
    settled = threading.Event()
    settled.set()
    child.wait()  # the direct process settled
    owner_empty = sl.fallback_owner("linux", 77, child, lambda pid: None,
                                    lambda pid: False, settled)
    assert owner_empty["alive"]() is False
    owner_live = sl.fallback_owner("linux", 77, child, lambda pid: None,
                                   lambda pid: True, settled)
    assert owner_live["alive"]() is True
    signals: List[Tuple[int, int]] = []
    monkeypatch.setattr(sl.os, "kill",
                        lambda pid, sig: signals.append((pid, sig)))
    owner_live["signal"]("SIGTERM")
    assert signals and signals[0][0] == -77  # group-first


def test_fallback_owner_wait_for_exit_polls_until_dead(
        monkeypatch: pytest.MonkeyPatch) -> None:
    child = FakeChild()
    attempts = {"count": 0}

    def flaky_kill(pid: int, sig: int) -> None:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            return None  # alive twice, then gone
        raise OSError(sl.errno_module.ESRCH, "gone")

    monkeypatch.setattr(sl.os, "kill", flaky_kill)
    owner = sl.fallback_owner("darwin", 77, child, lambda pid: None,
                              lambda pid: None, threading.Event())
    owner["waitForExit"]()
    assert attempts["count"] >= 3
    owner["waitForExit"]()  # memoized: returns immediately


# ------------------------------------------------- managed binding

def test_bind_collects_streams_and_result() -> None:
    child = FakeChild(exit_code=3, stdout=b"hello world", stderr=b"oops")
    owner = FakeOwner()
    handle = sl.bind_managed_process(base_spec(), make_launch(child, owner))
    assert handle.wait_done(5) is True
    assert handle.result == {"exitCode": 3, "signal": None}
    collected = handle.finalize_collected()
    assert collected["stdout"]["text"] == "hello world"
    assert collected["stderr"]["text"] == "oops"
    assert handle.read_from("stdout", 6)["text"] == "world"
    tail_read = handle.read_from("stdout", 9999)
    assert tail_read["text"] == "" and tail_read["lossy"] is False
    # collectors are exposed as objects (upstream handle.collected shape)
    assert "stdout" in handle.collected


def test_bind_exposes_pipe_streams_only_in_pipe_mode() -> None:
    child = FakeChild()
    spec = base_spec(stdio={"stdin": "pipe", "stdout": "pipe",
                            "stderr": {"maxBytes": 64}})
    handle = sl.bind_managed_process(spec, make_launch(child))
    assert handle.stdin is child.stdin
    assert handle.stdout is child.stdout
    assert handle.stderr is None  # collect mode hides the raw stream
    handle.wait_done(5)


def test_bind_data_stdin_writes_and_ends_immediately() -> None:
    child = FakeChild()
    spec = base_spec(stdio={"stdin": {"data": b"line\n"},
                            "stdout": {"maxBytes": 64},
                            "stderr": {"maxBytes": 64}})
    handle = sl.bind_managed_process(spec, make_launch(child))
    assert child.stdin.written == [b"line\n"]
    assert child.stdin.closed is True
    assert handle.stdin is None  # {data} mode does not expose the stream
    handle.wait_done(5)


def test_bind_terminate_ladder_escalates_to_sigkill() -> None:
    child = FakeChild(wait_delay=0.4)  # exit arrives after the grace window
    release = threading.Event()
    owner = FakeOwner(block_wait=release)  # range exit not yet observed
    handle = sl.bind_managed_process(base_spec(graceMs=50),
                                     make_launch(child, owner))
    handle.terminate_with_reason(RuntimeError("cancelled"))
    time.sleep(0.2)
    assert owner.signals[0] == "SIGTERM"
    assert "SIGKILL" in owner.signals  # grace expired → escalation
    release.set()
    assert handle.wait_done(5) is True


def test_bind_range_exit_observed_cancels_escalation() -> None:
    child = FakeChild()
    owner = FakeOwner()
    handle = sl.bind_managed_process(base_spec(graceMs=50),
                                     make_launch(child, owner))
    handle.terminate_with_reason(RuntimeError("cancelled"))
    assert handle.wait_for_exit() is True  # observer completes immediately
    time.sleep(0.15)  # past the grace window
    assert owner.signals == ["SIGTERM"]  # escalation was cancelled


def test_bind_abort_signal_terminates() -> None:
    child = FakeChild()
    owner = FakeOwner()
    token = sl.CancellationToken()
    handle = sl.bind_managed_process(base_spec(signal=token),
                                     make_launch(child, owner))
    token.abort(RuntimeError("user cancel"))
    handle.wait_done(5)
    assert owner.signals[0] == "SIGTERM"


def test_bind_collected_spill_holds_full_stream(tmp_path: Path) -> None:
    child = FakeChild(stdout=b"x" * 100)
    spec = base_spec(stdio={"stdin": "ignore",
                            "stdout": {"maxBytes": 10,
                                       "spill": {"maxBytes": 1000}},
                            "stderr": {"maxBytes": 10}})
    handle = sl.bind_managed_process(
        spec, make_launch(child), {"spillDir": str(tmp_path)})
    assert handle.wait_done(5) is True
    collected = handle.finalize_collected()
    assert collected["stdout"]["truncated"] is True
    spill = collected["stdout"].get("spillPath")
    assert spill is not None and os.path.exists(spill)
    with open(spill, "rb") as stream:
        assert stream.read() == b"x" * 100  # the FULL stream landed in spill


# ------------------------------------------------------- output surface

def test_output_collector_tail_and_spill(tmp_path: Path) -> None:
    reports: List[Tuple[Any, str]] = []
    collector = OutputCollector(10, "stdout", {
        "maxBytes": 1000, "dir": str(tmp_path),
        "onFailure": lambda error, label: reports.append((error, label))})
    collector.push(b"12345")
    assert collector.spill_file is None  # within cap: no spill yet
    collector.push(b"67890abc")  # overflow → spill opens with priors
    final = collector.finalize()
    assert final["truncated"] is True
    assert final["text"] == "4567890abc"  # tail keeps the last 10 bytes
    assert os.path.exists(final["spillPath"])
    assert reports == []
    with open(final["spillPath"], "rb") as stream:
        assert stream.read() == b"1234567890abc"  # complete stream


def test_output_collector_spill_exhausted_discards_file(
        tmp_path: Path) -> None:
    collector = OutputCollector(10, "stdout", {
        "maxBytes": 20, "dir": str(tmp_path), "onFailure": None})
    collector.push(b"x" * 15)  # opens the spill (total 15 <= 20)
    spill_path = collector.spill_file
    assert spill_path is not None and os.path.exists(spill_path)
    collector.push(b"y" * 10)  # total 25 > 20 → discard AND unlink
    final = collector.finalize()
    assert "spillPath" not in final
    assert not os.path.exists(spill_path)
    assert collector.dropped is True


def test_output_collector_spill_failure_degrades(tmp_path: Path) -> None:
    reports: List[Tuple[Any, str]] = []
    blocker = tmp_path / "blocked"
    blocker.write_text("x")  # a FILE where the spill dir should be
    collector = OutputCollector(10, "stdout", {
        "maxBytes": 1000, "dir": str(blocker / "deeper"),
        "onFailure": lambda error, label: reports.append((error, label))})
    collector.push(b"a" * 20)
    assert collector.spill_disabled is True
    assert len(reports) == 1 and reports[0][1] == "stdout"
    collector.push(b"b" * 20)  # in-memory collection keeps working
    final = collector.finalize()
    assert "spillPath" not in final
    assert final["truncated"] is True
    assert len(reports) == 1  # reported once


def test_output_collector_read_from_window_coordinates() -> None:
    collector = OutputCollector(4, "stdout")
    collector.push(b"abc")
    collector.push(b"def")  # total 6, tail keeps b"cdef"
    assert collector.dropped is True
    lossy = collector.read_from(0)
    assert lossy["lossy"] is True and lossy["text"] == "cdef"
    assert lossy["nextOffset"] == 6
    precise = collector.read_from(3)
    assert precise["lossy"] is False and precise["text"] == "def"
    empty = collector.read_from(99)
    assert empty["text"] == "" and empty["lossy"] is False


def test_output_collector_snapshot_raw_tail() -> None:
    collector = OutputCollector(4, "stdout")
    collector.push(b"abcdef")
    snapshot = collector.snapshot()
    assert snapshot["totalBytes"] == 6
    assert snapshot["bytes"] == b"cdef"


def test_seal_idempotent_and_reads_survive(tmp_path: Path) -> None:
    collector = OutputCollector(10, "stdout", {
        "maxBytes": 100, "dir": str(tmp_path), "onFailure": None})
    collector.push(b"x" * 6)
    collector.push(b"y" * 6)  # overflow → spill opens with both chunks
    collector.seal()
    collector.seal()  # must not raise
    read = collector.read_from(0)
    assert read["lossy"] is True  # the head slid out of the tail window
    assert read["text"] == "xxxxyyyyyy"  # tail: last 10 of the 12 bytes


# ------------------------------------------------------- cancellation

def test_cancellation_token_lifecycle() -> None:
    token = sl.CancellationToken()
    fired: List[bool] = []
    token.add_listener(lambda: fired.append(True))
    assert token.aborted is False
    token.abort(RuntimeError("stop"))
    assert token.aborted and fired == [True]
    reason = token.reason
    token.abort(RuntimeError("again"))  # idempotent
    assert token.reason is reason
    late: List[bool] = []
    token.add_listener(lambda: late.append(True))  # fires immediately
    assert late == [True]


# ------------------------------------------------------- linux scope

class FakeSystemctl:
    def __init__(self, results: List[Dict[str, Any]]) -> None:
        self.results = results
        self.calls: List[List[str]] = []

    def __call__(self, command: str, args: List[str]) -> Dict[str, Any]:
        self.calls.append([command, *args])
        return self.results.pop(0) if self.results else {
            "status": 0, "stdout": "LoadState=loaded\nActiveState=inactive\n"
                                    "TasksCurrent=0\n", "stderr": ""}


def _scope_startup(request_path: str = "") -> sl.LinuxScopeStartup:
    return sl.LinuxScopeStartup(
        {"directory": "", "requestPath": request_path,
         "startupErrorPath": ""}, "test")


def _direct(running: bool = False) -> Dict[str, Any]:
    state: Dict[str, Any] = {"signals": [], "is_running": running,
                             "settled": threading.Event()}

    def signal(name: str) -> None:
        state["signals"].append(name)

    state["signal"] = signal
    state["running"] = lambda: bool(state["is_running"])
    return state


def _ok_sync(command: str, args: Sequence[str],
             options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"status": 0, "stdout": "", "stderr": ""}


def _owner(query: Any = None, run_sync: Any = None,
           direct: Optional[Dict[str, Any]] = None,
           startup: Optional[sl.LinuxScopeStartup] = None
           ) -> sl.SystemdScopeOwner:
    return sl.SystemdScopeOwner(
        "dsh-test.scope", startup if startup is not None
        else _scope_startup(),
        direct if direct is not None else _direct(),
        systemctl="systemctl",
        run_sync=run_sync if run_sync is not None else _ok_sync,
        query=query,
        sleep=lambda ms, wake=None: None)


def test_parse_unit_state_validation() -> None:
    owner = _owner()
    state = owner.parse_unit_state(
        "LoadState=loaded\nActiveState=active\nTasksCurrent=3\n")
    assert state == {"loadState": "loaded", "activeState": "active",
                     "tasksCurrent": 3}
    partial = owner.parse_unit_state(
        "\nLoadState=loaded\nActiveState=inactive\nTasksCurrent=[not set]\n")
    assert partial == {"loadState": "loaded", "activeState": "inactive"}
    base = "LoadState=loaded\nActiveState=active\n"
    with pytest.raises(RuntimeError, match="malformed state"):
        owner.parse_unit_state(base + "Bogus\n")
    with pytest.raises(RuntimeError, match="duplicate LoadState"):
        owner.parse_unit_state(base + "LoadState=loaded\nTasksCurrent=1\n")
    with pytest.raises(RuntimeError, match="incomplete state"):
        owner.parse_unit_state("LoadState=loaded\n")
    with pytest.raises(RuntimeError, match="non-numeric TasksCurrent"):
        owner.parse_unit_state(base + "TasksCurrent=x\n")


def test_scope_startup_resolve_outcome(tmp_path: Path) -> None:
    request = tmp_path / "launch-request.json"
    error_path = tmp_path / "startup-error.json"
    files = {"directory": str(tmp_path), "requestPath": str(request),
             "startupErrorPath": str(error_path)}
    startup = sl.LinuxScopeStartup(files, "terminal")
    error_path.write_text(json.dumps(
        {"type": "error",
         "error": {"name": "OSError", "message": "exec failed"}}),
        encoding="utf-8")
    with pytest.raises(RuntimeError, match="exec failed"):
        startup.resolve_outcome({"exitCode": 1, "signal": None})
    error_path.unlink()
    # an unconsumed request without a requested signal is a startup exit
    request.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exited before its bootstrap"):
        startup.resolve_outcome({"exitCode": 1, "signal": None})
    # a requested termination signal excuses the leftover request
    startup.termination_signals.add("SIGTERM")
    outcome = {"exitCode": None, "signal": "SIGTERM"}
    assert startup.resolve_outcome(outcome) is outcome
    request.unlink()
    plain = {"exitCode": 0, "signal": None}
    assert startup.resolve_outcome(plain) is plain


def test_scope_owner_inspect_task_count() -> None:
    def loaded_active(command: str, args: Sequence[str],
                      options: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
        return {"status": 0,
                "stdout": "TasksCurrent=3\nLoadState=loaded\n"
                          "ActiveState=active\n", "stderr": ""}

    assert _owner(run_sync=loaded_active).inspect_task_count() == 3

    def failing(command: str, args: Sequence[str],
                options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"status": 1, "stdout": "", "stderr": ""}

    assert _owner(run_sync=failing).inspect_task_count() is None

    def inactive(command: str, args: Sequence[str],
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"status": 0,
                "stdout": "TasksCurrent=3\nLoadState=loaded\n"
                          "ActiveState=inactive\n", "stderr": ""}

    assert _owner(run_sync=inactive).inspect_task_count() is None


def test_scope_owner_empty_range_releases() -> None:
    query = FakeSystemctl([{"status": 0,
                            "stdout": "LoadState=loaded\nActiveState=active\n"
                                      "TasksCurrent=0\n", "stderr": ""}])
    stopped: List[List[str]] = []

    def run_sync(command: str, args: Sequence[str],
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        stopped.append([command, *args])
        return {"status": 0, "stdout": "", "stderr": ""}

    owner = _owner(query=query, run_sync=run_sync)
    owner.termination_requested = True  # verbatim emptyRange fence
    assert owner.range_active() is False
    assert stopped == [["systemctl", "--user", "stop", "dsh-test.scope"]]
    assert owner.establishment == "established"
    owner.wait_for_exit()


def test_scope_owner_absent_unit_settles() -> None:
    query = FakeSystemctl([{"status": 4, "stdout": "",
                            "stderr": "Unit dsh-x.scope could not be found."}])
    owner = _owner(query=query)
    assert owner.range_active() is False
    # the empty request locator proves bootstrap consumption
    assert owner.establishment == "established"
    owner.wait_for_exit()


def test_scope_owner_unknown_state_raises() -> None:
    query = FakeSystemctl([{"status": 0,
                            "stdout": "LoadState=masked\nActiveState=active\n"
                                      "TasksCurrent=1\n", "stderr": ""}])
    owner = _owner(query=query)
    with pytest.raises(RuntimeError, match="unknown state"):
        owner.range_active()
    owner.stopped = True
    owner.wait_for_exit()


def test_scope_owner_read_failure_raises_verbatim() -> None:
    query = FakeSystemctl([{"status": 1, "stdout": "",
                            "stderr": "Connection refused"}])
    owner = _owner(query=query)
    with pytest.raises(RuntimeError,
                        match="systemctl could not read dsh-test.scope"):
        owner.range_active()
    owner.stopped = True
    owner.wait_for_exit()


def test_scope_owner_kill_failure_reraises() -> None:
    query = FakeSystemctl([{"status": 0,
                            "stdout": "LoadState=loaded\nActiveState=active\n"
                                      "TasksCurrent=2\n", "stderr": ""}])
    owner = _owner(query=query)
    owner.kill_failure = OSError(1, "boom")
    with pytest.raises(OSError):
        owner.range_active()
    owner.stopped = True
    owner.wait_for_exit()


def test_scope_owner_kill_failure_waits_for_direct_settlement() -> None:
    results = [{"status": 0,
                "stdout": "LoadState=loaded\nActiveState=active\n"
                          "TasksCurrent=2\n", "stderr": ""},
               {"status": 0,
                "stdout": "LoadState=loaded\nActiveState=inactive\n",
                "stderr": ""}]
    query = FakeSystemctl(results)
    direct = _direct(running=True)
    direct["settled"].set()  # the direct kill already settled
    owner = _owner(query=query, direct=direct)
    owner.kill_failure = OSError(1, "boom")
    owner.direct_kill_settlement = direct["settled"]
    # settlement waited, then the fresh observation decides
    assert owner.range_active() is False
    owner.wait_for_exit()


def test_scope_owner_signal_direct_fallback_before_establishment() -> None:
    query = FakeSystemctl([{"status": 0,
                            "stdout": "LoadState=loaded\nActiveState=active\n"
                                      "TasksCurrent=2\n", "stderr": ""}])
    calls: List[List[str]] = []

    def run_sync(command: str, args: Sequence[str],
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        calls.append([command, *args])
        return {"status": 0, "stdout": "", "stderr": ""}

    direct = _direct(running=True)
    # an existing request file keeps establishment pending (deterministic)
    startup = sl.LinuxScopeStartup(
        {"directory": "", "requestPath": __file__, "startupErrorPath": ""},
        "test")
    owner = _owner(query=query, run_sync=run_sync, direct=direct,
                   startup=startup)
    owner.signal("SIGTERM")
    assert direct["signals"] == ["SIGTERM"]
    assert "SIGTERM" in startup.termination_signals
    assert owner.termination_requested is True
    assert calls == [["systemctl", "--user", "kill", "--kill-whom=all",
                      "--signal=SIGTERM", "dsh-test.scope"]]
    assert owner.kill_failure is None
    owner.stopped = True
    owner.wait_for_exit()


def test_scope_owner_sigkill_failure_records_kill_failure() -> None:
    def failing(command: str, args: Sequence[str],
                options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"status": 1, "stdout": "", "stderr": "Access denied"}

    direct = _direct(running=True)
    startup = sl.LinuxScopeStartup(
        {"directory": "", "requestPath": __file__, "startupErrorPath": ""},
        "test")
    owner = _owner(run_sync=failing, direct=direct, startup=startup)
    owner.signal("SIGKILL")
    assert direct["signals"] == ["SIGKILL"]  # the pending fallback fired
    assert isinstance(owner.kill_failure, RuntimeError)
    assert "systemctl could not terminate dsh-test.scope: Access denied" \
        == str(owner.kill_failure)
    assert owner.direct_kill_settlement is direct["settled"]
    owner.stopped = True
    owner.wait_for_exit()


def test_scope_owner_sigkill_missing_unit_is_not_a_failure() -> None:
    def missing(command: str, args: Sequence[str],
                options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"status": 1, "stdout": "",
                "stderr": "Unit dsh-test.scope could not be found."}

    owner = _owner(run_sync=missing, direct=_direct(running=False))
    owner.signal("SIGKILL")
    assert owner.kill_failure is None


def test_scope_owner_stopped_signal_noop() -> None:
    owner = _owner()
    owner.stopped = True
    owner.signal("SIGTERM")
    assert owner.termination_requested is False


def test_scope_owner_host_exit_sweep() -> None:
    calls: List[Any] = []

    def run_sync(command: str, args: Sequence[str],
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        calls.append(([command, *args], options))
        return {"status": 0, "stdout": "", "stderr": ""}

    direct = _direct(running=True)
    owner = _owner(run_sync=run_sync, direct=direct)
    owner.terminate_for_host_exit()
    assert direct["signals"] == ["SIGKILL"]
    argv, options = calls[-1]
    assert argv == ["systemctl", "--user", "kill", "--kill-whom=all",
                    "--signal=SIGKILL", "dsh-test.scope"]
    assert options["stdio"] == "ignore"


def test_probe_linux_scope_literal_argv() -> None:
    calls: List[Any] = []

    def run_sync(command: str, args: Sequence[str],
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        calls.append((command, list(args), options))
        return {"status": 0, "stdout": "active\n", "stderr": ""}

    assert sl.probe_linux_scope({"spawnSync": run_sync}) is True
    command, args, options = calls[0]
    assert command == "systemd-run"
    unit_index = next(index for index, arg in enumerate(args)
                      if arg.startswith("--unit="))
    unit = args[unit_index].split("=", 1)[1]
    assert unit.startswith("dsh-subprocess-probe")
    assert args == ["--user", "--scope", "--quiet", "--collect",
                    "--expand-environment=no", f"--unit={unit}", "--",
                    "systemctl", "--user", "show", f"{unit}.scope",
                    "--property=ActiveState", "--value"]
    assert options["stdio"] == "ignore"
    assert options["timeout"] == sl.SYSTEMCTL_TIMEOUT_MS
    # verbatim: ANY non-zero status fails — there is no MISSING_UNIT arm
    assert sl.probe_linux_scope({"spawnSync": lambda *a, **k: {
        "status": 1, "stdout": "",
        "stderr": "Unit dsh-subprocess-probe.scope could not be found."
    }}) is False
    assert sl.probe_linux_scope({"spawnSync": lambda *a, **k: {
        "status": None, "stdout": "", "stderr": "",
        "error": OSError("spawn failed")}}) is False


def test_probe_linux_manager_literal_argv() -> None:
    calls: List[Any] = []

    def run_sync(command: str, args: Sequence[str],
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        calls.append((command, list(args), options))
        return {"status": 0, "stdout": "255\n", "stderr": ""}

    assert sl.probe_linux_manager({"spawnSync": run_sync}) is True
    command, args, options = calls[0]
    assert command == "systemctl"
    assert args == ["--user", "show", "--property=Version", "--value"]
    assert options["stdio"] == "ignore"
    assert sl.probe_linux_manager({"spawnSync": lambda *a, **k: {
        "status": 1, "stdout": "", "stderr": ""}}) is False


def test_probe_linux_bootstrap_seams() -> None:
    assert sl.probe_linux_bootstrap({
        "loadLinuxExecve": lambda: (lambda *args: None),
        "resolveRunnerInvocation": lambda: ["runner"],
        "runnerAvailable": lambda invocation: True}) is True
    assert sl.probe_linux_bootstrap({
        "loadLinuxExecve": lambda: (_ for _ in ()).throw(OSError()),
    }) is False
    # the availability seam gates an otherwise loadable bootstrap
    assert sl.probe_linux_bootstrap({
        "loadLinuxExecve": lambda: (lambda *args: None),
        "resolveRunnerInvocation": lambda: ["runner"],
        "runnerAvailable": lambda invocation: False}) is False


def test_spawn_runner_invocation_shape() -> None:
    invocation = sl.spawn_runner_invocation()
    assert invocation[0] == sys.executable
    assert invocation[1:3] == ["-X", "utf8"]
    assert invocation[3] == "-c"
    assert sl.SUBPROCESS_RUNNER_ENV in invocation[4]
    assert sl.runner_invocation_available(invocation) is True
    assert sl.runner_invocation_available(
        [os.path.join(os.getcwd(), "missing-runner")]) is False


# ------------------------------------------------------- launch files

def test_launch_files_roundtrip() -> None:
    request = {"cwd": "/work", "env": {"A": "1"}}
    files = sl.create_linux_launch_files(request)
    try:
        assert set(files) == {"directory", "requestPath", "startupErrorPath"}
        assert os.path.exists(files["requestPath"])
        assert os.path.basename(files["requestPath"]) == "launch-request.json"
        assert os.path.basename(files["startupErrorPath"]) \
            == "startup-error.json"
        if sys.platform != "win32":
            import stat as stat_module
            mode = stat_module.S_IMODE(
                os.stat(files["requestPath"]).st_mode)
            assert mode == 0o600
        with open(files["requestPath"], "r", encoding="utf-8") as handle:
            assert json.load(handle) == request
        rebuilt = sl.linux_launch_files_from_locator(files["requestPath"])
        assert rebuilt == files
        consumed = sl.consume_linux_launch_request(files["requestPath"])
        assert consumed == request
        assert not os.path.exists(files["requestPath"])  # read-once
    finally:
        sl.cleanup_linux_launch_files(files)
    assert not os.path.exists(files["directory"])


def test_launch_files_locator_rejects_bad_paths() -> None:
    with pytest.raises(RuntimeError, match="invalid Linux launch-request"):
        sl.linux_launch_files_from_locator("relative/launch-request.json")
    with pytest.raises(RuntimeError, match="invalid Linux launch-request"):
        sl.linux_launch_files_from_locator(
            os.path.join(os.path.abspath(os.sep), "tmp", "other.json"))


def test_consume_launch_request_rejects_invalid(tmp_path: Path) -> None:
    path = tmp_path / "launch-request.json"
    path.write_text('{"cwd": 5}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid Linux launch request"):
        sl.consume_linux_launch_request(str(path))
    path.write_text('{"cwd": "/w", "env": {"A": "1"}, "control": "bogus"}',
                    encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid Linux launch request"):
        sl.consume_linux_launch_request(str(path))
    path.write_text('{"cwd": "/w", "env": {"A": 1}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid Linux launch request"):
        sl.consume_linux_launch_request(str(path))
    path.write_text('{"cwd": "/w", "env": {"A": "1"}, "control": "pipe"}',
                    encoding="utf-8")
    assert sl.consume_linux_launch_request(str(path)) == {
        "cwd": "/w", "env": {"A": "1"}, "control": "pipe"}


def test_startup_error_roundtrip(tmp_path: Path) -> None:
    files = {"startupErrorPath": str(tmp_path / "startup-error.json")}
    record = {"type": "error",
              "error": {"name": "OSError", "message": "boom",
                        "code": "EACCES", "syscall": "execve",
                        "path": "/bin/x"}}
    sl.write_linux_startup_error(files, record)
    read = sl.read_linux_startup_error(files["startupErrorPath"])
    assert read == record
    assert sl.read_linux_startup_error(str(tmp_path / "none")) is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"type": "other", "error": {"name": "E", "message": "m"}}',
                   encoding="utf-8")
    with pytest.raises(RuntimeError, match="unknown error result"):
        sl.read_linux_startup_error(str(bad))
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"type": "error", "error": {"name": 5}}',
                       encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid error result"):
        sl.read_linux_startup_error(str(invalid))


# --------------------------------------------------- runtime selection

class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def test_select_containment_windows_job(monkeypatch: pytest.MonkeyPatch
                                        ) -> None:
    monkeypatch.setattr(sl, "probe_windows_job", lambda internals=None: True)
    runtime = sl.LocalSubprocessRuntime({"platform": "win32"})
    assert runtime.select_containment_mode("ordinary") == "windows-job"
    # verbatim: only the ordinary kind consults the Windows Job probe
    assert runtime.select_containment_mode("terminal") == "fallback"


def test_select_containment_fallback_warns_once_verbatim(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl, "probe_windows_job", lambda internals=None: False)
    logger = RecordingLogger()
    runtime = sl.LocalSubprocessRuntime({"platform": "win32",
                                         "logger": logger})
    assert runtime.select_containment_mode("ordinary") == "fallback"
    assert runtime.select_containment_mode("ordinary") == "fallback"
    assert len(logger.warnings) == 1
    assert logger.warnings[0] == (
        "subprocess-local is using weaker process-tree containment because "
        "the Win32 Job runner is unavailable; descendants that escape the "
        "process group or direct-parent tree are not guaranteed to "
        "terminate or delay waitForExit()")


def test_select_containment_terminal_warning_reason() -> None:
    logger = RecordingLogger()
    runtime = sl.LocalSubprocessRuntime({"platform": "win32",
                                         "logger": logger})
    assert runtime.select_containment_mode("terminal") == "fallback"
    assert "Windows ConPTY remains outside Job containment" \
        in logger.warnings[0]


def test_select_containment_linux_deep_probe_cache(
        monkeypatch: pytest.MonkeyPatch) -> None:
    natives: List[bool] = []
    managers: List[bool] = []

    def native(internals: Any = None) -> bool:
        natives.append(True)
        return True

    def manager(internals: Any = None) -> bool:
        managers.append(True)
        return True

    monkeypatch.setattr(sl, "probe_linux_native", native)
    monkeypatch.setattr(sl, "probe_linux_manager", manager)
    runtime = sl.LocalSubprocessRuntime({"platform": "linux"})
    assert runtime.select_containment_mode("ordinary") == "linux-scope"
    assert runtime.select_containment_mode("ordinary") == "linux-scope"
    # after the first deep pass only the cheap manager probe runs
    assert len(natives) == 1 and len(managers) == 1


def test_select_containment_linux_unavailable_warns(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl, "probe_linux_native",
                        lambda internals=None: False)
    logger = RecordingLogger()
    runtime = sl.LocalSubprocessRuntime({"platform": "linux",
                                         "logger": logger})
    assert runtime.select_containment_mode("ordinary") == "fallback"
    assert ("the current user-systemd scope or private bootstrap is "
            "unavailable") in logger.warnings[0]


def test_select_containment_darwin_warns() -> None:
    logger = RecordingLogger()
    runtime = sl.LocalSubprocessRuntime({"platform": "darwin",
                                         "logger": logger})
    assert runtime.select_containment_mode("ordinary") == "fallback"
    # terminal kinds are not rejected; they simply fall back too
    assert runtime.select_containment_mode("terminal") == "fallback"
    assert len(logger.warnings) == 1
    assert "macOS has no supported persistent process-range owner" \
        in logger.warnings[0]


def test_descriptor_shape_modes() -> None:
    shape = sl.descriptor_shape({"stdin": {"data": b"x"},
                                 "stdout": "ignore", "stderr": "inherit"})
    assert shape["stdin"] == {"data": b"x"}
    assert shape["stdout"] == "ignore"
    assert shape["stderr"] == "inherit"
    with pytest.raises(ValueError, match="invalid stdout mode"):
        sl.descriptor_shape({"stdout": "bogus"})
    with pytest.raises(ValueError, match="missing maxBytes"):
        sl.descriptor_shape({"stdout": {"spill": {"maxBytes": 1}}})
    with pytest.raises(ValueError, match="invalid control mode"):
        sl.descriptor_shape({"control": "bogus"})


# ------------------------------------------------------- resolve surface

def test_resolve_executable_relative_rejection() -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "win32"})
    with pytest.raises(RuntimeError, match="is a relative path") as caught:
        runtime.resolve_executable("sub\\tool.exe")
    # the verbatim message quotes the command in JSON form
    assert '"sub\\\\tool.exe"' in str(caught.value)
    with pytest.raises(RuntimeError, match="must be non-empty"):
        runtime.resolve_executable("")


def test_resolve_executable_absolute_missing(tmp_path: Path) -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "win32"})
    missing = str(tmp_path / "tool.exe")
    with pytest.raises(SubprocessExecutableNotFoundError,
                       match="is not an executable file"):
        runtime.resolve_executable(missing)
    # a cancelled token refuses before and after the walk
    token = sl.CancellationToken()
    token.abort(RuntimeError("stop"))
    with pytest.raises(RuntimeError, match="stop"):
        runtime.resolve_executable(missing, None, token)


def test_executable_candidates_cross_product() -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "win32"})
    env: Dict[str, Optional[str]] = {"PATH": "C:\\bin;C:\\tools",
                                     "PATHEXT": ".EXE;.CMD"}
    candidates = runtime.executable_candidates("tool", env)
    assert candidates == [
        os.path.normpath("C:\\bin\\tool.EXE"),
        os.path.normpath("C:\\bin\\tool.CMD"),
        os.path.normpath("C:\\tools\\tool.EXE"),
        os.path.normpath("C:\\tools\\tool.CMD"),
    ]
    # a command carrying an extension skips PATHEXT (verbatim extname rule)
    assert runtime.executable_candidates("tool.bat", env) == [
        os.path.normpath("C:\\bin\\tool.bat"),
        os.path.normpath("C:\\tools\\tool.bat"),
    ]
    # a missing PATHEXT falls back to the verbatim default list
    default_env: Dict[str, Optional[str]] = {"PATH": "C:\\bin"}
    defaults = runtime.executable_candidates("tool", default_env)
    assert defaults[0] == os.path.normpath("C:\\bin\\tool.COM")
    assert defaults[-1] == os.path.normpath("C:\\bin\\tool.CMD")


def test_resolve_executable_real_system_tool() -> None:
    runtime = sl.LocalSubprocessRuntime()
    if IS_WIN32:
        resolved = runtime.resolve_executable("cmd")
        assert resolved.lower().endswith(("cmd.exe", "cmd.com"))
        assert runtime.resolve_executable(CMD) == CMD
    else:
        resolved = runtime.resolve_executable("sh")
        assert os.path.isabs(resolved)
    with pytest.raises(SubprocessExecutableNotFoundError):
        runtime.resolve_executable("definitely-missing-tool-xyz")


def test_terminal_environment_platforms(monkeypatch: pytest.MonkeyPatch
                                        ) -> None:
    monkeypatch.setenv("ComSpec", "C:\\Windows\\system32\\cmd.exe")
    runtime = sl.LocalSubprocessRuntime({"platform": "win32"})
    assert runtime.terminal_environment() == {
        "platform": "windows",
        "defaultShell": "C:\\Windows\\system32\\cmd.exe"}
    monkeypatch.setenv("SHELL", "/bin/zsh")
    posix = sl.LocalSubprocessRuntime({"platform": "linux"})
    assert posix.terminal_environment() == {
        "platform": "posix", "defaultShell": "/bin/zsh"}
    # an aborted token refuses before reading the environment
    token = sl.CancellationToken()
    token.abort(RuntimeError("stop"))
    with pytest.raises(RuntimeError, match="stop"):
        runtime.terminal_environment(token)


def test_spawn_terminal_rejects_empty_argv() -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "win32"})
    with pytest.raises(RuntimeError,
                       match="terminal argv must contain a program"):
        runtime.spawn_terminal({"argv": []})
    closed = sl.LocalSubprocessRuntime({"platform": "win32"})
    closed.close()
    with pytest.raises(RuntimeError, match="closed"):
        closed.spawn_terminal({"argv": ["cmd"]})


# ------------------------------------------- REAL spawn (sandbox-safe)

@pytest.mark.skipif(not IS_WIN32, reason="real win32 spawn paths")
def test_real_windows_job_direct_spawn_exit_code(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The full ordinary-spawn chain on the real platform: containment
    selection → Job direct launch (suspended create + assign + resume) →
    exit-code settlement → host-exit sweep. No pipes involved, so this
    runs inside the sandbox that refuses CreatePipe. The fallback spawn
    is booby-trapped: a silent degradation to the detached group form
    fails the test instead of masking it."""
    def refuse_fallback(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "windows-job launch degraded to the fallback spawn")

    monkeypatch.setattr(sl, "_fallback_launch", refuse_fallback)
    runtime = sl.LocalSubprocessRuntime()
    assert runtime.select_containment_mode("ordinary") == "windows-job"
    spec = {
        "argv": [CMD, "/d", "/s", "/c", "exit 11"],
        "cwd": os.getcwd(), "graceMs": 1000,
        "stdio": {"stdin": "ignore", "stdout": "inherit",
                  "stderr": "inherit"},
    }
    handle = runtime.spawn(spec)
    assert handle.pid is not None and handle.pid > 0
    assert handle.wait_done(30) is True
    assert handle.result == {"exitCode": 11, "signal": None}
    runtime.terminate_for_host_exit()  # idempotent sweep
    runtime.close()


@pytest.mark.skipif(not IS_WIN32, reason="real win32 spawn paths")
def test_real_fallback_spawn_inherit_stdio(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Fallback detached spawn with inherited stdio. The verbatim upstream
    mapping sends every non-inherit out/err mode through a parent pipe, and
    this sandbox refuses CreatePipe — so the real fallback probe uses the
    inherit shape (pipe-stdio paths are fake-seam covered)."""
    monkeypatch.setattr(sl, "probe_windows_job", lambda internals=None: False)
    runtime = sl.LocalSubprocessRuntime()
    spec = {
        "argv": [CMD, "/d", "/s", "/c", "exit 12"],
        "cwd": os.getcwd(), "graceMs": 1000,
        "stdio": {"stdin": "ignore", "stdout": "inherit",
                  "stderr": "inherit"},
    }
    handle = runtime.spawn(spec)
    assert handle.wait_done(30) is True
    assert handle.result is not None
    assert handle.result["exitCode"] == 12
    runtime.close()


@pytest.mark.skipif(not IS_WIN32, reason="real win32 spawn paths")
def test_real_job_terminate_sweeps_range(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """terminate() signals through TerminateJobObject and the wait proves
    the managed range empty before settling."""
    def refuse_fallback(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "windows-job launch degraded to the fallback spawn")

    monkeypatch.setattr(sl, "_fallback_launch", refuse_fallback)
    runtime = sl.LocalSubprocessRuntime()
    spec = {
        "argv": [CMD, "/d", "/s", "/c", "ping -n 30 127.0.0.1 >NUL"],
        "cwd": os.getcwd(), "graceMs": 1000,
        "stdio": {"stdin": "ignore", "stdout": "inherit",
                  "stderr": "inherit"},
    }
    handle = runtime.spawn(spec)
    time.sleep(0.3)  # let the target start
    handle.terminate()
    assert handle.wait_done(30) is True
    runtime.close()


def test_runtime_close_refuses_spawn() -> None:
    runtime = sl.LocalSubprocessRuntime({"platform": "darwin"})
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.spawn(base_spec())
