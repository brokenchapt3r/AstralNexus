"""Tests for runtime/subprocess_terminal.py — the terminal plane.

Covers the verbatim LocalTerminalHandle logic through fake terminal and
inspector seams (root identity capture, backpressure pause/resume, exit
outcome mapping and the managed-outcome override, foreground and
activity inspection branches, foreground signalling rules including the
win32 SIGINT byte and the SIGKILL-shell refusal, the terminate ladder
for managed and unmanaged ranges, the Windows identity-fenced shell
stop, and the host-exit sweep), the ShellActivity state machine, both
prepare_shell_activity startup-file texts, the JSON-lines sidecar
protocol through a pure-Python fake sidecar, and the spawn_terminal
wiring including the linux-scope envelope and failure cleanup.

Real ConPTY spawns are refused by this host's sandbox ("Cannot launch
conpty"); the sidecar mechanics are protocol-tested against the fake,
and the real node-pty load plus sidecar syntax are verified separately
in the migration probe evidence.
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from AstralNexus.runtime import subprocess_local as sl
from AstralNexus.runtime import subprocess_terminal as st
from AstralNexus.runtime.compat import ExceptionGroup

IS_WIN32 = sys.platform == "win32"


# ---------------------------------------------------------------- fakes

class FakeDisposable:
    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


class FakeTerminal:
    """The node-pty IPty seam: pid/onData/onExit/write/resize/kill/
    pause/resume with test drivers emit_data/emit_exit."""

    def __init__(self, pid: int = 100) -> None:
        self.pid = pid
        self.written: List[str] = []
        self.resizes: List[Any] = []
        self.kills: List[Any] = []
        self.paused = 0
        self.resumed = 0
        self.exit_on_kill = True
        self._data_callbacks: List[Any] = []
        self._exit_callbacks: List[Any] = []
        self.data_disposables: List[FakeDisposable] = []
        self.exit_disposables: List[FakeDisposable] = []

    def on_data(self, callback: Any) -> FakeDisposable:
        self._data_callbacks.append(callback)
        disposable = FakeDisposable()
        self.data_disposables.append(disposable)
        return disposable

    def on_exit(self, callback: Any) -> FakeDisposable:
        self._exit_callbacks.append(callback)
        disposable = FakeDisposable()
        self.exit_disposables.append(disposable)
        return disposable

    def write(self, data: str) -> None:
        self.written.append(data)

    def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    def kill(self, signal: Optional[str] = None) -> None:
        self.kills.append(signal)
        if self.exit_on_kill:
            self.emit_exit(0, None)

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def emit_data(self, data: Any) -> None:
        for callback in list(self._data_callbacks):
            callback(data)

    def emit_exit(self, exit_code: Optional[int],
                  signal: Optional[int]) -> None:
        for callback in list(self._exit_callbacks):
            callback({"exitCode": exit_code, "signal": signal})


def _row(pid: int, started: Any, state: str = "S", parent: int = 100,
         session: Optional[int] = None) -> Dict[str, Any]:
    return {"pid": pid, "parentPid": parent, "started": started,
            "session": pid if session is None else session,
            "state": state}


class FakeInspector:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None,
                 complete: bool = True, pgid: Optional[int] = None,
                 waiting: bool = False) -> None:
        self.rows: List[Dict[str, Any]] = rows if rows is not None else []
        self.complete = complete
        self.pgid = pgid
        self.waiting = waiting
        self.group_signals: List[Any] = []
        self.process_signals: List[Any] = []
        self.alive_override: Optional[bool] = None

    def snapshot(self) -> Any:
        return sl.PosixProcessSnapshot(self.rows, self.complete)

    def foregroundPgid(self, pid: int) -> Optional[int]:
        return self.pgid

    def isStdinWaiting(self, pgid: int, pid: int) -> bool:
        return self.waiting

    def isAlive(self, identity: Dict[str, Any]) -> bool:
        if self.alive_override is not None:
            return self.alive_override
        observed = self.snapshot()
        return observed.alive(identity)

    def signalGroup(self, pgid: int, name: str) -> None:
        self.group_signals.append((pgid, name))

    def signalProcess(self, identity: Dict[str, Any], name: str) -> None:
        self.process_signals.append((identity["pid"], name))


class FakeActivity:
    def __init__(self, state: str = "unknown", revision: int = 0) -> None:
        self.state = state
        self.revision = revision
        self.invalidated = 0
        self.disposed = 0
        self.argv = ["bash", "-i"]
        self.env: Dict[str, str] = {}
        self.directory = "fake"

    def invalidate(self) -> None:
        self.invalidated += 1

    def inspect(self, pid: int) -> Dict[str, Any]:
        return {"state": self.state, "revision": self.revision}

    def dispose(self) -> None:
        self.disposed += 1


class FakeManagedOwner:
    def __init__(self, tasks: Optional[int] = None,
                 wait_action: Optional[Any] = None) -> None:
        self.signals: List[str] = []
        self.tasks = tasks
        self.wait_action = wait_action if wait_action is not None \
            else (lambda: None)
        self.wait_calls = 0
        self.terminated = 0
        self.cleaned = 0

    def signal(self, name: str) -> None:
        self.signals.append(name)

    def wait_for_exit(self) -> None:
        self.wait_calls += 1
        self.wait_action()

    def inspect_task_count(self) -> Optional[int]:
        return self.tasks

    def terminate_for_host_exit(self) -> None:
        self.terminated += 1

    def cleanup(self) -> None:
        self.cleaned += 1


def make_handle(terminal: Optional[FakeTerminal] = None,
                inspector: Optional[FakeInspector] = None,
                grace_ms: float = 200.0,
                platform: str = "linux",
                managed_owner: Any = None,
                resolve_managed_outcome: Any = None,
                shell_activity: Any = None,
                on_quiescence: Any = None,
                observe_shell_exit: bool = False) -> Any:
    return st.LocalTerminalHandle(
        terminal if terminal is not None else FakeTerminal(),
        inspector if inspector is not None else FakeInspector(
            [_row(100, "1")]),
        grace_ms, platform, managed_owner, resolve_managed_outcome,
        shell_activity, on_quiescence, observe_shell_exit)


# ----------------------------------------------------------- primitives

def test_outcome_future_faces() -> None:
    future = st.OutcomeFuture()
    seen: List[Any] = []
    future.add_done_callback(lambda f: seen.append("done"))
    assert future.settled is False
    future.resolve({"exitCode": 0})
    assert future.settled is True
    assert future.result(timeout=1) == {"exitCode": 0}
    assert seen == ["done"]
    future.resolve({"exitCode": 1})  # latched
    assert future.result(timeout=1) == {"exitCode": 0}
    late: List[Any] = []
    future.add_done_callback(lambda f: late.append("late"))
    assert late == ["late"]  # fires immediately once settled

    rejected = st.OutcomeFuture()
    rejected.reject(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        rejected.result(timeout=1)


def test_race_with_delay_arms() -> None:
    assert st.race_with_delay(lambda: "operation", 5000, "timeout") \
        == "operation"

    def slow() -> str:
        time.sleep(5)
        return "late"

    assert st.race_with_delay(slow, 50, "timeout") == "timeout"

    def failing() -> Any:
        raise ValueError("operation failed")

    with pytest.raises(ValueError, match="operation failed"):
        st.race_with_delay(failing, 5000, "timeout")


def test_signal_name_mapping() -> None:
    assert st.signal_name(None) is None
    assert st.signal_name(0) is None
    assert st.signal_name(15) == "SIGTERM"
    assert st.signal_name(2) == "SIGINT"
    if IS_WIN32:
        # Node's constants.signals carries no SIGKILL on win32 (verbatim)
        assert st.signal_name(9) is None
    else:
        assert st.signal_name(9) == "SIGKILL"
    assert st.signal_name(999999) is None


def test_terminal_output_backpressure_and_drain() -> None:
    output = st.TerminalOutput()
    drains: List[bool] = []
    output.drain_callback = lambda: drains.append(True)
    assert output.write(b"x" * 100) is True  # below the 16 KiB mark
    assert output.write(b"y" * st.TerminalOutput.HIGH_WATER_MARK) is False
    first = output.read(timeout=1)
    assert len(first) == 100 + st.TerminalOutput.HIGH_WATER_MARK
    assert drains == [True]  # draining below the mark fired drain
    output.end()
    assert output.ended is True
    assert output.write(b"z") is False  # writes after end are dropped
    closes: List[bool] = []
    output.close_callback = lambda: closes.append(True)
    output.close()
    assert closes == [True]


# ------------------------------------------------------- shell activity

def test_quote_escaping() -> None:
    assert st.quote("/tmp/plain") == "'/tmp/plain'"
    assert st.quote("it's") == "'it'\\''s'"


def test_shell_activity_state_machine(tmp_path: Path) -> None:
    activity = st.ShellActivity(str(tmp_path), ["bash", "-i"], {})
    # no state file yet: empty record, unknown
    assert activity.inspect(5) == {"state": "unknown", "revision": 0}
    (tmp_path / "state").write_text("5:1:idle\n", encoding="utf-8")
    assert activity.inspect(5) == {"state": "idle", "revision": 1}
    # the same record does not bump the revision
    assert activity.inspect(5) == {"state": "idle", "revision": 1}
    (tmp_path / "state").write_text("5:2:busy\n", encoding="utf-8")
    assert activity.inspect(5) == {"state": "busy", "revision": 2}
    # a foreign pid is unknown
    assert activity.inspect(6) == {"state": "unknown", "revision": 2}
    # invalidation fences the current record
    activity.invalidate()
    assert activity.state == "unknown"
    assert activity.inspect(5) == {"state": "unknown", "revision": 3}
    # a malformed record is unknown
    (tmp_path / "state").write_text("garbage", encoding="utf-8")
    assert activity.inspect(5)["state"] == "unknown"
    activity.dispose()
    assert not tmp_path.exists()


def test_prepare_shell_activity_rejections() -> None:
    base = {"shellActivity": True, "argv": ["/bin/bash", "-i"]}
    assert st.prepare_shell_activity(dict(base), {}, "win32") is None
    assert st.prepare_shell_activity(
        {**base, "shellActivity": False}, {}, "linux") is None
    assert st.prepare_shell_activity(
        {**base, "argv": ["/bin/bash"]}, {}, "linux") is None
    assert st.prepare_shell_activity(
        {**base, "argv": ["/bin/bash", "-l"]}, {}, "linux") is None
    assert st.prepare_shell_activity(
        {**base, "argv": ["/bin/fish", "-i"]}, {}, "linux") is None


def test_prepare_shell_activity_bash(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch
                                     ) -> None:
    monkeypatch.setattr(st.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path))
    env = {"PATH": "/bin"}
    activity = st.prepare_shell_activity(
        {"shellActivity": True, "argv": ["/bin/bash", "-i"]}, env, "linux")
    assert activity is not None
    rc = str(tmp_path / "bashrc")
    assert activity.argv == ["/bin/bash", "--rcfile", rc, "-i"]
    assert activity.env == env
    text = Path(rc).read_text(encoding="utf-8")
    assert text.startswith(
        "[[ ! -r ~/.bashrc ]] || builtin source ~/.bashrc\n")
    assert "BASH_VERSINFO[0] > 4" in text
    assert "__dsh_shell_pid=$BASHPID; __dsh_shell_sequence=0" in text
    assert "__dsh_shell_idle() {" in text
    assert f"builtin trap -p >| '{tmp_path / 'guards'}'" in text
    assert f"[[ ! -s '{tmp_path / 'guards'}' ]] || activity=unknown" in text
    assert ("builtin printf '%s:%s:%s\\n' \"$BASHPID\" "
            "\"$__dsh_shell_sequence\" \"$activity\" "
            f">| '{tmp_path / 'state'}'") in text
    assert 'PROMPT_COMMAND+=(__dsh_shell_idle)' in text
    assert "PROMPT_COMMAND=\"${PROMPT_COMMAND}\"$'\\n'" \
        "\"__dsh_shell_idle\"" in text
    # the PS0 busy marker is quoted with the same escape
    assert "PS0+='" in text
    assert "%s:%s:busy" in text
    if sys.platform != "win32":
        import stat as stat_module
        assert stat_module.S_IMODE(os.stat(rc).st_mode) == 0o600


def test_prepare_shell_activity_zsh(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch
                                    ) -> None:
    monkeypatch.setattr(st.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path))
    activity = st.prepare_shell_activity(
        {"shellActivity": True, "argv": ["/bin/zsh", "-i"]},
        {"ZDOTDIR": "/custom"}, "linux")
    assert activity is not None
    assert activity.argv == ["/bin/zsh", "-i"]  # unchanged launch
    assert activity.env == {"ZDOTDIR": str(tmp_path)}
    text = (tmp_path / ".zshenv").read_text(encoding="utf-8")
    assert text.startswith("ZDOTDIR='/custom'\n")
    assert ("[[ ! -r ${ZDOTDIR:-$HOME}/.zshenv ]] || builtin source "
            "\"${ZDOTDIR:-$HOME}/.zshenv\"") in text
    assert "typeset -g __dsh_shell_pid=$$ __dsh_shell_sequence=0" in text
    assert "(( ZSH_SUBSHELL == 0 && $$ == __dsh_shell_pid )) || return" \
        in text
    assert f">| '{tmp_path / 'state'}'" in text
    assert f"{{ builtin trap; zle -F; }} >| '{tmp_path / 'guards'}'" in text
    assert "if [[ $CONTEXT != start || -n $BUFFER ]]; then " \
        "__dsh_shell_activity busy" in text
    assert "-n ${(k)functions[(I)TRAP*]}" in text
    assert "autoload -Uz add-zle-hook-widget add-zsh-hook" in text
    assert "add-zle-hook-widget line-init __dsh_shell_idle" in text
    assert "add-zle-hook-widget line-finish __dsh_shell_busy" in text
    assert "add-zsh-hook preexec __dsh_shell_busy" in text
    assert "precmd_functions=(${precmd_functions:#__dsh_shell_init})" in text
    assert "precmd_functions+=(__dsh_shell_init)" in text


def test_prepare_shell_activity_zsh_without_zdotdir() -> None:
    activity = st.prepare_shell_activity(
        {"shellActivity": True, "argv": ["/bin/zsh", "-i"]}, {}, "linux")
    assert activity is not None
    try:
        text = Path(activity.directory, ".zshenv").read_text(
            encoding="utf-8")
        assert text.startswith("unset ZDOTDIR\n")
    finally:
        activity.dispose()


def test_prepare_shell_activity_cleanup_on_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "shell"
    directory.mkdir()
    monkeypatch.setattr(st.tempfile, "mkdtemp", lambda prefix: str(directory))
    blocker = directory / "bashrc"
    blocker.write_text("occupied", encoding="utf-8")  # wx create fails
    with pytest.raises(OSError):
        st.prepare_shell_activity(
            {"shellActivity": True, "argv": ["/bin/bash", "-i"]}, {},
            "linux")
    assert not directory.exists()  # the private directory was removed


# ------------------------------------------------- local terminal handle

def test_handle_root_identity_capture() -> None:
    inspector = FakeInspector([_row(100, "1"), _row(101, "2", parent=100)])
    handle = make_handle(inspector=inspector)
    assert handle.root_identity == {"pid": 100, "started": "1"}
    # an unreadable snapshot leaves the identity undefined
    class BrokenInspector(FakeInspector):
        def snapshot(self) -> Any:
            raise OSError("no proc")

    broken = make_handle(inspector=BrokenInspector())
    assert broken.root_identity is None


def test_handle_data_flow_and_backpressure() -> None:
    terminal = FakeTerminal()
    handle = make_handle(terminal=terminal)
    terminal.emit_data("hello")
    assert handle.output.read(timeout=1) == b"hello"
    # str payloads are utf-8 encoded like Buffer.from(data, "utf8")
    terminal.emit_data("ü")
    assert handle.output.read(timeout=1) == "ü".encode("utf-8")
    # above the high-water mark the terminal is paused once
    terminal.emit_data("x" * (st.TerminalOutput.HIGH_WATER_MARK + 1))
    assert handle.output_paused is True
    assert terminal.paused == 1
    terminal.emit_data("more")  # paused writes still buffer
    handle.output.read(timeout=1)  # draining resumes
    assert handle.output_paused is False
    assert terminal.resumed == 1


def test_handle_exit_outcome_mapping() -> None:
    terminal = FakeTerminal()
    handle = make_handle(terminal=terminal)
    assert handle.running is True
    terminal.emit_exit(3, None)
    assert handle.running is False
    outcome = handle.outcome.result(timeout=1)
    assert outcome == {"exitCode": 3, "signal": None}
    assert handle.output.ended is True
    # a second exit event is ignored
    terminal.emit_exit(9, None)
    assert handle.outcome.result(timeout=1) == {"exitCode": 3,
                                                "signal": None}

    terminal2 = FakeTerminal()
    handle2 = make_handle(terminal=terminal2)
    terminal2.emit_exit(1, 15)
    assert handle2.outcome.result(timeout=1) == {"exitCode": None,
                                                 "signal": "SIGTERM"}

    terminal3 = FakeTerminal()
    handle3 = make_handle(terminal=terminal3)
    terminal3.emit_exit(0, 0)  # signal 0 keeps the exit code
    assert handle3.outcome.result(timeout=1) == {"exitCode": 0,
                                                 "signal": None}


def test_handle_managed_outcome_override() -> None:
    terminal = FakeTerminal()
    handle = make_handle(terminal=terminal,
                         resolve_managed_outcome=lambda outcome: {
                             "exitCode": 99, "signal": None})
    terminal.emit_exit(1, None)
    assert handle.outcome.result(timeout=1)["exitCode"] == 99

    terminal2 = FakeTerminal()

    def failing(outcome: Any) -> Any:
        raise RuntimeError("scope failed")

    handle2 = make_handle(terminal=terminal2,
                          resolve_managed_outcome=failing)
    terminal2.emit_exit(1, None)
    with pytest.raises(RuntimeError, match="scope failed"):
        handle2.outcome.result(timeout=1)


def test_handle_observe_shell_exit_sets_range_empty() -> None:
    terminal = FakeTerminal()
    released = threading.Event()
    owner = FakeManagedOwner(wait_action=lambda: released.set())
    handle = make_handle(terminal=terminal, managed_owner=owner,
                         observe_shell_exit=True)
    terminal.emit_exit(0, None)
    assert released.wait(2) is True
    until = time.monotonic() + 2
    while not handle.managed_range_empty and time.monotonic() < until:
        time.sleep(0.01)
    assert handle.managed_range_empty is True


def test_handle_write_resize_after_exit() -> None:
    terminal = FakeTerminal()
    activity = FakeActivity()
    handle = make_handle(terminal=terminal, shell_activity=activity)
    handle.write("ls\n")
    assert terminal.written == ["ls\n"]
    assert activity.invalidated == 1  # input invalidates prompt evidence
    handle.resize(100, 40)
    assert terminal.resizes == [(100, 40)]
    terminal.emit_exit(0, None)
    with pytest.raises(RuntimeError, match="terminal process has exited"):
        handle.write("x")
    with pytest.raises(RuntimeError, match="terminal process has exited"):
        handle.resize(1, 1)


def test_handle_inspect_foreground() -> None:
    inspector = FakeInspector([_row(100, "1")], pgid=None)
    handle = make_handle(inspector=inspector)
    assert handle.inspect_foreground() is None
    inspector.pgid = 77
    inspector.waiting = True
    assert handle.inspect_foreground() == {"processGroupId": 77,
                                           "inputWaiting": True}


def test_handle_inspect_activity_branches() -> None:
    # quiescent handles report idle without inspection
    handle = make_handle()
    handle.quiescent = True
    assert handle.inspect_activity() == {"state": "idle", "revision": 1}
    assert handle.inspect_activity() == {"state": "idle", "revision": 1}

    # busy: a live descendant
    inspector = FakeInspector([_row(100, "1"),
                               _row(101, "2", parent=100)])
    handle2 = make_handle(inspector=inspector)
    assert handle2.inspect_activity() == {"state": "busy", "revision": 1}

    # exited with an emptied managed range is idle
    terminal = FakeTerminal()
    owner = FakeManagedOwner()
    handle3 = make_handle(terminal=terminal, managed_owner=owner)
    handle3.exited = True
    handle3.managed_range_empty = True
    assert handle3.inspect_activity()["state"] == "idle"

    # linux: an exited shell whose root is gone consults the session
    terminal4 = FakeTerminal()
    session_rows = [_row(200, "9", session=100)]
    inspector4 = FakeInspector(session_rows, complete=True)
    handle4 = make_handle(terminal=terminal4, inspector=inspector4,
                          platform="linux")
    handle4.exited = True
    handle4.root_identity = None  # no root: session decides
    assert handle4.inspect_activity()["state"] == "busy"
    inspector4.rows = []
    handle4.tracked_descendants = []
    handle4.activity_key = ""
    assert handle4.inspect_activity()["state"] == "idle"


def test_handle_inspect_activity_verified_root_states() -> None:
    rows = [_row(100, "1")]
    inspector = FakeInspector(rows, complete=True, pgid=100)
    activity = FakeActivity(state="idle", revision=7)
    handle = make_handle(inspector=inspector, shell_activity=activity)
    # foreground == shell pgid: the shell state decides
    assert handle.inspect_activity() == {"state": "idle", "revision": 1}
    # a foreign foreground group is busy
    inspector.pgid = 55
    assert handle.inspect_activity() == {"state": "busy", "revision": 2}
    # an unreadable foreground group is unknown
    inspector.pgid = None
    assert handle.inspect_activity() == {"state": "unknown",
                                         "revision": 3}
    # a started mismatch drops to unknown
    handle.root_identity = {"pid": 100, "started": "other"}
    inspector.pgid = 100
    assert handle.inspect_activity()["state"] == "unknown"


def test_handle_inspect_activity_managed_task_fence() -> None:
    rows = [_row(100, "1")]
    inspector = FakeInspector(rows, complete=True, pgid=100)
    activity = FakeActivity(state="idle", revision=1)
    owner = FakeManagedOwner(tasks=1)
    handle = make_handle(inspector=inspector, managed_owner=owner,
                         shell_activity=activity)
    assert handle.inspect_activity()["state"] == "idle"
    owner.tasks = 2
    assert handle.inspect_activity()["state"] == "busy"
    owner.tasks = None
    assert handle.inspect_activity()["state"] == "unknown"
    owner.tasks = 0
    assert handle.inspect_activity()["state"] == "unknown"


def test_handle_inspect_activity_error_is_unknown() -> None:
    class BrokenInspector(FakeInspector):
        def snapshot(self) -> Any:
            raise OSError("no proc")

    handle = make_handle(inspector=BrokenInspector())
    assert handle.inspect_activity()["state"] == "unknown"


def test_handle_signal_foreground_posix() -> None:
    inspector = FakeInspector([_row(100, "1")], pgid=77)
    activity = FakeActivity()
    handle = make_handle(inspector=inspector, shell_activity=activity,
                         platform="linux")
    assert handle.signal_foreground("SIGTERM") == 77
    assert inspector.group_signals == [(77, "SIGTERM")]
    assert activity.invalidated == 1  # signalling invalidates evidence
    # an unresolvable group refuses
    inspector.pgid = None
    with pytest.raises(RuntimeError,
                       match="cannot resolve foreground process group"):
        handle.signal_foreground("SIGTERM")
    # SIGKILL against the shell's own group refuses
    inspector.pgid = 100
    with pytest.raises(RuntimeError,
                       match="refusing to SIGKILL the terminal shell"):
        handle.signal_foreground("SIGKILL")
    inspector.pgid = 77
    assert handle.signal_foreground("SIGKILL") == 77
    assert inspector.group_signals[-1] == (77, "SIGKILL")


def test_handle_signal_foreground_windows() -> None:
    terminal = FakeTerminal()
    inspector = FakeInspector([_row(100, "1")], pgid=77)
    handle = make_handle(terminal=terminal, inspector=inspector,
                         platform="win32")
    # SIGINT is the verbatim ETX byte through the pty input
    assert handle.signal_foreground("SIGINT") == 77
    assert terminal.written == ["\x03"]
    assert inspector.group_signals == []
    for refused in ("SIGTSTP", "SIGHUP"):
        with pytest.raises(RuntimeError,
                           match="is unsupported on Windows"):
            handle.signal_foreground(refused)
    assert handle.signal_foreground("SIGTERM") == 77
    assert inspector.group_signals == [(77, "SIGTERM")]
    inspector.pgid = 100
    with pytest.raises(RuntimeError,
                       match="refusing to SIGKILL the terminal shell"):
        handle.signal_foreground("SIGKILL")


def test_handle_terminate_unmanaged_ladder() -> None:
    terminal = FakeTerminal()
    inspector = FakeInspector([_row(100, "1")])
    activity = FakeActivity()
    quiesced: List[bool] = []
    handle = make_handle(terminal=terminal, inspector=inspector,
                         shell_activity=activity,
                         on_quiescence=lambda: quiesced.append(True),
                         platform="linux")
    future = handle.terminate()
    assert future.result(timeout=5) is None
    assert handle.quiescent is True
    assert activity.disposed == 1
    assert quiesced == [True]
    assert terminal.kills == ["SIGTERM"]
    assert handle.terminate() is future  # idempotent
    assert handle.inspect_activity()["state"] == "idle"


def test_handle_terminate_failure_clears_cleanup() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False  # the shell ignores every kill
    survivor = _row(101, "2", parent=100)
    inspector = FakeInspector([_row(100, "1"), survivor])
    inspector.alive_override = True  # nothing ever dies
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=60, platform="linux")
    future = handle.terminate()
    with pytest.raises(RuntimeError, match="surviving pids: 101"):
        future.result(timeout=5)
    assert handle.cleanup is None  # retryable
    assert handle.quiescent is False


def test_handle_stop_shell_posix_escalation() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    inspector = FakeInspector([_row(100, "1")])
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=80, platform="linux")

    def escalate() -> None:
        # the shell only dies after the SIGKILL escalation
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if "SIGKILL" in terminal.kills:
                terminal.emit_exit(None, 9)
                return
            time.sleep(0.005)

    threading.Thread(target=escalate, daemon=True).start()
    future = handle.terminate()
    future.result(timeout=5)
    assert terminal.kills == ["SIGTERM", "SIGKILL"]


def test_handle_stop_shell_posix_gives_up() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    inspector = FakeInspector([_row(100, "1")])
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=50, platform="linux")
    with pytest.raises(RuntimeError,
                       match="terminal cleanup failed; surviving pid: 100"):
        handle.terminate().result(timeout=5)


def test_handle_stop_shell_windows_identity_fences() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    inspector = FakeInspector([_row(100, "1")])
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=80, platform="win32")
    identity = {"pid": 100, "started": "1"}

    # the shell dies right after the identity SIGTERM
    def die_after_term() -> None:
        time.sleep(0.02)
        inspector.rows = [_row(100, "1", state="X")]

    threading.Thread(target=die_after_term, daemon=True).start()
    handle.terminate().result(timeout=5)
    assert inspector.process_signals[0] == (100, "SIGTERM")
    assert terminal.kills == []  # the pty kill is never used with identity


def test_handle_stop_shell_windows_no_identity_uses_pty_kill() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = True  # the pty kill settles the exit
    inspector = FakeInspector([])  # no rows: no root identity
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=80, platform="win32")
    handle.root_identity = None
    handle.terminate().result(timeout=5)
    assert terminal.kills == [None]


def test_handle_stop_shell_windows_escalates_to_sigkill() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    inspector = FakeInspector([_row(100, "1")])
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=100, platform="win32")
    signals = inspector.process_signals

    def die_after_kill() -> None:
        until = time.monotonic() + 1
        while time.monotonic() < until:
            if (100, "SIGKILL") in signals:
                inspector.rows = [_row(100, "1", state="X")]
                return
            time.sleep(0.01)

    threading.Thread(target=die_after_kill, daemon=True).start()
    handle.terminate().result(timeout=5)
    assert signals == [(100, "SIGTERM"), (100, "SIGKILL")]


def test_handle_stop_shell_windows_unstoppable_raises() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    inspector = FakeInspector([_row(100, "1")])
    inspector.alive_override = True
    handle = make_handle(terminal=terminal, inspector=inspector,
                         grace_ms=50, platform="win32")
    with pytest.raises(RuntimeError,
                       match="terminal cleanup failed; surviving pid: 100"):
        handle.terminate().result(timeout=5)


def test_handle_settle_exit_if_gone_windows() -> None:
    terminal = FakeTerminal()
    inspector = FakeInspector([])
    handle = make_handle(terminal=terminal, inspector=inspector,
                         platform="win32")
    handle.root_identity = None
    handle.exited = False
    handle.settle_exit_if_gone()
    assert handle.exited is True
    outcome = handle.outcome.result(timeout=1)
    assert outcome == {"exitCode": None, "signal": None}
    assert handle.output.ended is True

    # posix never settles synthetically
    posix = make_handle(platform="linux")
    posix.settle_exit_if_gone()
    assert posix.exited is False


def test_handle_close_managed_range_success() -> None:
    terminal = FakeTerminal()
    released = threading.Event()

    def wait_action() -> None:
        released.set()
        terminal.emit_exit(0, None)

    owner = FakeManagedOwner(wait_action=wait_action)
    handle = make_handle(terminal=terminal, managed_owner=owner,
                         grace_ms=500, platform="linux")
    handle.terminate().result(timeout=5)
    assert owner.signals == ["SIGTERM"]
    assert handle.quiescent is True
    # the owner cleanup runs after the outcome settles
    until = time.monotonic() + 2
    while owner.cleaned == 0 and time.monotonic() < until:
        time.sleep(0.01)
    assert owner.cleaned == 1


def test_handle_close_managed_range_timeout_escalates() -> None:
    terminal = FakeTerminal()
    calls = {"n": 0}

    def wait_action() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(1.0)  # the SIGTERM observation overruns the grace
        terminal.emit_exit(None, 9)

    owner = FakeManagedOwner(wait_action=wait_action)
    handle = make_handle(terminal=terminal, managed_owner=owner,
                         grace_ms=80, platform="linux")
    handle.terminate().result(timeout=5)
    assert owner.signals == ["SIGTERM", "SIGKILL"]


def test_handle_close_managed_range_failure_paths() -> None:
    # a failed observation retried successfully raises the first error
    terminal = FakeTerminal()
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("scope read failed")
        terminal.emit_exit(None, 9)

    owner = FakeManagedOwner(wait_action=flaky)
    handle = make_handle(terminal=terminal, managed_owner=owner,
                         grace_ms=200, platform="linux")
    with pytest.raises(OSError, match="scope read failed"):
        handle.terminate().result(timeout=5)
    assert owner.signals == ["SIGTERM", "SIGKILL"]

    # both observations failing aggregate
    terminal2 = FakeTerminal()

    def always_fail() -> None:
        raise OSError("still failing")

    owner2 = FakeManagedOwner(wait_action=always_fail)
    handle2 = make_handle(terminal=terminal2, managed_owner=owner2,
                          grace_ms=200, platform="linux")
    with pytest.raises(ExceptionGroup,
                       match="terminal managed-range cleanup failed"):
        handle2.terminate().result(timeout=5)


def test_handle_close_managed_range_waits_for_shell_exit() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    owner = FakeManagedOwner(wait_action=lambda: None)  # range stops...
    handle = make_handle(terminal=terminal, managed_owner=owner,
                         grace_ms=60, platform="linux")
    # ...but the shell exit never publishes: the fence holds
    with pytest.raises(RuntimeError,
                       match="terminal cleanup failed; surviving pid: 100"):
        handle.terminate().result(timeout=5)


def test_handle_descendants_tracking_and_union() -> None:
    rows = [_row(100, "1"), _row(101, "2", parent=100),
            _row(102, "3", parent=101), _row(103, "4", parent=100,
                                             state="Z")]
    inspector = FakeInspector(rows)
    handle = make_handle(inspector=inspector, platform="linux")
    members = handle.descendants(inspector.snapshot())
    assert {member["pid"] for member in members} == {101, 102}
    # a started mismatch stops adopting new members but keeps tracking
    handle.root_identity = {"pid": 100, "started": "other"}
    rows.append(_row(104, "5", parent=100))
    members2 = handle.descendants(inspector.snapshot())
    assert {member["pid"] for member in members2} == {101, 102}
    # dedupe across groups by pid:started
    union = handle.union_members([{"pid": 1, "started": "a"}],
                                 [{"pid": 1, "started": "a"},
                                  {"pid": 1, "started": "b"}])
    assert union == [{"pid": 1, "started": "a"}, {"pid": 1, "started": "b"}]


def test_handle_terminate_for_host_exit() -> None:
    terminal = FakeTerminal()
    terminal.exit_on_kill = False
    inspector = FakeInspector([_row(100, "1"),
                               _row(101, "2", parent=100)])
    owner = FakeManagedOwner()
    handle = make_handle(terminal=terminal, inspector=inspector,
                         managed_owner=owner, platform="linux")
    handle.terminate_for_host_exit()
    assert (101, "SIGKILL") in inspector.process_signals
    assert (100, "SIGKILL") in inspector.process_signals
    assert owner.terminated == 1


# --------------------------------------------------------- pty sidecar

FAKE_SIDECAR_SOURCE = '''
import json
import os
import socket
import sys
import threading

locator_path = sys.argv[1]
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
port = server.getsockname()[1]
fd = os.open(locator_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    json.dump({"port": port, "pid": os.getpid()}, handle)
connection, _ = server.accept()
reader = connection.makefile("rb")


def send(message):
    connection.sendall((json.dumps(message) + "\\n").encode("utf-8"))


for raw in reader:
    if not raw.strip():
        continue
    message = json.loads(raw.decode("utf-8"))
    kind = message.get("type")
    if kind == "spawn":
        send({"type": "spawned", "pid": 4242})
        threading.Thread(
            target=lambda: send({"type": "data", "data": "banner"}),
            daemon=True).start()
    elif kind == "write":
        send({"type": "data", "data": "echo:" + message["data"]})
    elif kind == "resize":
        send({"type": "data",
              "data": f"size:{message['cols']}x{message['rows']}"})
    elif kind == "kill":
        send({"type": "exit", "exitCode": 0, "signal": None})
    elif kind == "pause":
        send({"type": "data", "data": "paused"})
    elif kind == "resume":
        send({"type": "data", "data": "resumed"})
    elif kind == "shutdown":
        break
connection.close()
server.close()
'''


@pytest.fixture()
def fake_sidecar(tmp_path: Path) -> Dict[str, Any]:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(FAKE_SIDECAR_SOURCE, encoding="utf-8")
    return {"sidecarCommand": sys.executable, "sidecarScript": str(script),
            "nodePtyPath": "unused"}


def test_sidecar_terminal_protocol(fake_sidecar: Dict[str, Any]) -> None:
    data: List[str] = []
    exits: List[Any] = []
    terminal = st.SidecarTerminal(
        "cmd.exe", ["/c", "ver"],
        {"name": "conpty", "rows": 24, "cols": 80, "cwd": os.getcwd(),
         "env": {"TERM": "conpty"}},
        fake_sidecar)
    try:
        assert terminal.pid == 4242
        terminal.on_data(data.append)
        terminal.write("hello")
        terminal.resize(100, 40)
        terminal.pause()
        terminal.resume()
        until = time.monotonic() + 5
        while len(data) < 4 and time.monotonic() < until:
            time.sleep(0.02)
        assert "echo:hello" in data
        assert "size:100x40" in data
        assert "paused" in data and "resumed" in data
        terminal.on_exit(exits.append)
        terminal.kill()
        until = time.monotonic() + 5
        while not exits and time.monotonic() < until:
            time.sleep(0.02)
        assert exits and exits[0]["exitCode"] == 0
    finally:
        terminal.close()


def test_sidecar_terminal_spawn_failure(tmp_path: Path) -> None:
    script = tmp_path / "failing_sidecar.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('conpty refused\\n')\n"
        "raise SystemExit(3)\n", encoding="utf-8")
    with pytest.raises(RuntimeError,
                       match="exited before publishing its locator"):
        st.SidecarTerminal("cmd.exe", [], {}, {
            "sidecarCommand": sys.executable,
            "sidecarScript": str(script), "nodePtyPath": "unused"})


def test_sidecar_script_syntax() -> None:
    """The shipped sidecar parses as strict-mode JavaScript (checked with
    the host Python's own parser only for existence; the real node
    --check runs in the migration probe evidence)."""
    assert st.SIDECAR_SCRIPT.exists()
    text = st.SIDECAR_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("// Controlled node-pty sidecar")
    assert '"use strict";' in text
    assert "pty.spawn(message.file, message.args, message.options)" in text


# ------------------------------------------------------ spawn_terminal

class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def make_runtime(platform: str, spawn_record: List[Any],
                 inspector: Optional[FakeInspector] = None,
                 terminal: Optional[FakeTerminal] = None,
                 spawn_error: Optional[BaseException] = None
                 ) -> sl.LocalSubprocessRuntime:
    def pty_spawn(file: str, args: Sequence[str],
                  options: Dict[str, Any]) -> FakeTerminal:
        spawn_record.append((file, list(args), options))
        if spawn_error is not None:
            raise spawn_error
        return terminal if terminal is not None else FakeTerminal()

    logger = RecordingLogger()
    return sl.LocalSubprocessRuntime({
        "platform": platform,
        "logger": logger,
        "ptySpawn": pty_spawn,
        "terminalInspector": inspector if inspector is not None
        else FakeInspector([_row(100, "1")]),
    })


def test_spawn_terminal_wiring_win32() -> None:
    record: List[Any] = []
    terminal = FakeTerminal()
    runtime = make_runtime("win32", record, terminal=terminal)
    cwd = os.getcwd()
    spec = {"argv": ["cmd.exe"], "terminalType": "conpty", "rows": 24,
            "cols": 80, "cwd": cwd, "graceMs": 500}
    handle = runtime.spawn_terminal(spec)
    assert handle is not None
    file, args, options = record[0]
    assert file == "cmd.exe" and args == []
    assert options["name"] == "conpty"
    assert options["rows"] == 24 and options["cols"] == 80
    assert options["cwd"] == cwd
    assert options["env"]["TERM"] == "conpty"
    assert handle in runtime.terminals
    # the release thread removes the handle after the outcome settles
    terminal.emit_exit(0, None)
    until = time.monotonic() + 5
    while handle in runtime.terminals and time.monotonic() < until:
        time.sleep(0.02)
    assert handle not in runtime.terminals
    runtime.close()


def test_spawn_terminal_retains_shell_activity_handles() -> None:
    record: List[Any] = []
    terminal = FakeTerminal()
    runtime = make_runtime("win32", record, terminal=terminal)
    spec = {"argv": ["cmd.exe"], "terminalType": "conpty", "rows": 24,
            "cols": 80, "cwd": os.getcwd(), "graceMs": 500,
            "shellActivity": True}
    handle = runtime.spawn_terminal(spec)
    assert handle in runtime.terminals
    terminal.emit_exit(0, None)
    time.sleep(0.3)
    # verbatim release rule: shell-activity terminals stay registered
    assert handle in runtime.terminals
    handle.terminate().result(timeout=5)
    runtime.close()


def test_spawn_terminal_abort_and_empty_argv() -> None:
    record: List[Any] = []
    runtime = make_runtime("win32", record)
    token = sl.CancellationToken()
    token.abort(RuntimeError("user cancel"))
    with pytest.raises(RuntimeError, match="user cancel"):
        runtime.spawn_terminal({"argv": ["cmd.exe"], "signal": token,
                                "terminalType": "conpty", "rows": 24,
                                "cols": 80, "cwd": os.getcwd(),
                                "graceMs": 500})
    with pytest.raises(RuntimeError,
                       match="terminal argv must contain a program"):
        runtime.spawn_terminal({"argv": [""], "terminalType": "conpty",
                                "rows": 24, "cols": 80,
                                "cwd": os.getcwd(), "graceMs": 500})
    assert record == []
    runtime.close()


def test_spawn_terminal_linux_scope_envelope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    record: List[Any] = []
    terminal = FakeTerminal()
    runtime = make_runtime("linux", record, terminal=terminal)
    monkeypatch.setattr(sl, "probe_linux_native",
                        lambda internals=None: True)
    bound: List[Any] = []
    cleaned: List[bool] = []
    def bind_owner(direct: Any) -> FakeManagedOwner:
        bound.append(direct)
        return FakeManagedOwner()

    scope = {
        "command": "systemd-run",
        "args": ["--user", "--scope", "--", "/bin/bash", "-i"],
        "cwd": "/scope-cwd",
        "env": {"A": "1"},
        "bindOwner": bind_owner,
        "resolveOutcome": lambda outcome: outcome,
        "cleanup": lambda: cleaned.append(True),
    }
    monkeypatch.setattr(sl, "prepare_linux_terminal_scope",
                        lambda spec, env, internals=None: scope)
    spec = {"argv": ["/bin/bash", "-i"], "terminalType": "xterm-256color",
            "rows": 24, "cols": 80, "cwd": "/work", "graceMs": 500}
    handle = runtime.spawn_terminal(spec)
    file, args, options = record[0]
    assert file == "systemd-run"
    assert args == ["--user", "--scope", "--", "/bin/bash", "-i"]
    assert options["cwd"] == "/scope-cwd"
    assert options["env"] == {"A": "1"}
    # the direct seam carries the running/settled/signal faces
    assert len(bound) == 1
    assert set(bound[0]) == {"running", "settled", "signal"}
    assert bound[0]["running"]() is True
    assert handle.managed_owner is not None
    terminal.emit_exit(0, None)
    until = time.monotonic() + 5
    while handle in runtime.terminals and time.monotonic() < until:
        time.sleep(0.02)
    assert cleaned == []  # a clean exit does not call the scope cleanup
    runtime.close()


def test_spawn_terminal_scope_failure_cleans_up(
        monkeypatch: pytest.MonkeyPatch) -> None:
    record: List[Any] = []
    runtime = make_runtime("linux", record,
                           spawn_error=RuntimeError("conpty refused"))
    monkeypatch.setattr(sl, "probe_linux_native",
                        lambda internals=None: True)
    cleaned: List[bool] = []
    scope = {
        "command": "systemd-run", "args": [], "cwd": "/scope-cwd",
        "env": {}, "bindOwner": lambda direct: FakeManagedOwner(),
        "resolveOutcome": lambda outcome: outcome,
        "cleanup": lambda: cleaned.append(True),
    }
    monkeypatch.setattr(sl, "prepare_linux_terminal_scope",
                        lambda spec, env, internals=None: scope)
    before = _shell_dirs()
    with pytest.raises(RuntimeError, match="conpty refused"):
        runtime.spawn_terminal({
            "argv": ["/bin/bash", "-i"], "shellActivity": True,
            "terminalType": "xterm", "rows": 24, "cols": 80,
            "cwd": "/work", "graceMs": 500})
    assert cleaned == [True]
    assert _shell_dirs() <= before  # the activity directory was removed
    runtime.close()


def _shell_dirs() -> set:
    import glob as glob_module
    return set(glob_module.glob(
        os.path.join(tempfile.gettempdir(), "dsh-shell-*")))


def test_spawn_terminal_bash_activity_launch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    record: List[Any] = []
    terminal = FakeTerminal()
    runtime = make_runtime("linux", record, terminal=terminal)
    monkeypatch.setattr(sl, "probe_linux_native",
                        lambda internals=None: False)
    spec = {"argv": ["/bin/bash", "-i"], "shellActivity": True,
            "terminalType": "xterm", "rows": 24, "cols": 80,
            "cwd": os.getcwd(), "graceMs": 500}
    handle = runtime.spawn_terminal(spec)
    try:
        file, args, options = record[0]
        assert file == "/bin/bash"
        assert args[0] == "--rcfile" and args[-1] == "-i"
        assert os.path.exists(args[1])  # the generated bashrc
        assert options["env"]["TERM"] == "xterm"
        assert handle.shell_activity is not None
        # writing invalidates the shell evidence
        handle.write("ls\n")
        assert handle.shell_activity.state == "unknown"
    finally:
        handle.terminate().result(timeout=5)
        runtime.close()
