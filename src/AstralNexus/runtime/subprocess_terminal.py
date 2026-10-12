# -*- coding: utf-8 -*-
"""Terminal plane for the local subprocess runtime.

Verbatim port of the upstream ``@deepseek-ai/dsh-subprocess-local``
terminal surfaces (``lib/types/terminal.js``: delay/raceWithDelay/
signalName/LocalTerminalHandle; ``lib/types/shell-activity.js``:
quote/ShellActivity/prepareShellActivity; and the ``spawnTerminal``
wiring in ``lib/types/index.js``) onto Python threads.

Documented adaptations (semantics preserved, carrier changed):

- The Node ``PassThrough`` output stream becomes :class:`TerminalOutput`,
  a byte buffer with the same backpressure contract (write returns False
  above the 16 KiB high-water mark, consumers draining below it fire the
  drain callback, ``end()`` marks EOF after queued data).
- Promise races become :func:`race_with_delay` over daemon threads; the
  outcome promise becomes :class:`OutcomeFuture` with resolve/reject/
  add_done_callback faces.
- node-pty itself runs in a controlled Node sidecar (Electron-as-Node on
  this host) speaking JSON lines over 127.0.0.1, handed off through a
  private locator file; :class:`SidecarTerminal` implements the IPty seam
  (pid/onData/onExit/write/resize/kill/pause/resume) that
  :class:`LocalTerminalHandle` consumes. Tests inject a fake terminal
  through the ``ptySpawn`` internals seam. On this host the sandbox
  refuses ConPTY creation ("Cannot launch conpty"), so real PTY spawns
  are an environment limit recorded in the migration ledger; the handle
  logic is fully covered through the seam.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from .compat import ExceptionGroup

__all__ = [
    "Disposable", "LocalTerminalHandle", "OutcomeFuture", "ShellActivity",
    "SidecarTerminal", "TerminalOutput", "delay", "prepare_shell_activity",
    "quote", "race_with_delay", "signal_name", "spawn_terminal",
]


# ------------------------------------------------------------- primitives

class Disposable:
    """The node-pty disposable shape: one idempotent dispose()."""

    def __init__(self, action: Callable[[], None]) -> None:
        self._action = action
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._action()


class OutcomeFuture:
    """Promise.withResolvers adaptation: resolve/reject once, blocking
    result(), and done callbacks fired on settlement (then/catch)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Any = None
        self._error: Optional[BaseException] = None
        self._callbacks: List[Callable[["OutcomeFuture"], None]] = []

    @property
    def settled(self) -> bool:
        return self._event.is_set()

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def resolve(self, value: Any = None) -> None:
        if self._event.is_set():
            return
        self._value = value
        self._event.set()
        self._fire()

    def reject(self, error: BaseException) -> None:
        if self._event.is_set():
            return
        self._error = error
        self._event.set()
        self._fire()

    def _fire(self) -> None:
        for callback in list(self._callbacks):
            try:
                callback(self)
            except BaseException:
                pass

    def add_done_callback(
            self, callback: Callable[["OutcomeFuture"], None]) -> None:
        self._callbacks.append(callback)
        if self._event.is_set():
            try:
                callback(self)
            except BaseException:
                pass

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def result(self, timeout: Optional[float] = None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError("outcome did not settle in time")
        if self._error is not None:
            raise self._error
        return self._value


def delay(ms: float, wake: Optional[threading.Event] = None) -> None:
    """The delay(ms, signal) primitive: an interruptible sleep."""
    if wake is not None:
        wake.wait(ms / 1000)
    else:
        time.sleep(ms / 1000)


def race_with_delay(operation: Callable[[], Any], ms: float,
                    timeout_value: Any) -> Any:
    """Promise.race([operation, delay(ms).then(() => timeout)]) on
    threads: the operation runs on a daemon thread; when the timeout arm
    wins, the operation keeps running in the background (its result is
    discarded), matching the aborted-timer semantics."""
    box: Dict[str, Any] = {}
    settled = threading.Event()

    def run() -> None:
        try:
            box["value"] = operation()
        except BaseException as error:  # re-raised on the winning arm
            box["error"] = error
        settled.set()

    threading.Thread(target=run, name="race-operation", daemon=True).start()
    if not settled.wait(ms / 1000):
        return timeout_value
    if "error" in box:
        raise box["error"]
    return box.get("value")


def signal_name(number: Optional[int]) -> Optional[str]:
    """Verbatim signalName: undefined/0 map to null, the platform signal
    table reverse lookup decides, unknown numbers map to null."""
    if number is None or number == 0:
        return None
    import signal as signal_module
    for name in dir(signal_module):
        if not name.startswith("SIG") or name.startswith("SIG_"):
            continue
        try:
            if getattr(signal_module, name) == number:
                return name
        except (TypeError, ValueError):
            continue
    return None


class TerminalOutput:
    """PassThrough adaptation: one byte buffer with the same
    backpressure contract. write() returns False above the high-water
    mark; a consumer read() that drains below the mark fires the drain
    callback; end() marks EOF after the queued data; close() detaches."""

    HIGH_WATER_MARK = 16384

    def __init__(self) -> None:
        self._chunks: List[bytes] = []
        self._size = 0
        self._ended = False
        self._closed = False
        self._lock = threading.Lock()
        self._available = threading.Event()
        self.drain_callback: Optional[Callable[[], None]] = None
        self.close_callback: Optional[Callable[[], None]] = None

    def write(self, chunk: bytes) -> bool:
        with self._lock:
            if self._ended:
                return False
            self._chunks.append(chunk)
            self._size += len(chunk)
            self._available.set()
            return self._size < self.HIGH_WATER_MARK

    def end(self) -> None:
        with self._lock:
            self._ended = True
            self._available.set()

    @property
    def ended(self) -> bool:
        return self._ended

    def read(self, timeout: Optional[float] = None) -> bytes:
        """Consume the buffered prefix (the stream-pipe adaptation);
        draining below the high-water mark fires the drain callback."""
        if not self._available.wait(timeout):
            return b""
        drain = False
        with self._lock:
            data = b"".join(self._chunks)
            self._chunks.clear()
            drain = self._size >= self.HIGH_WATER_MARK
            self._size = 0
            if not (self._ended and data):
                self._available.clear()
        if drain and self.drain_callback is not None:
            self.drain_callback()
        return data

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._available.set()
        if self.close_callback is not None:
            self.close_callback()


# ------------------------------------------------------- shell activity

def quote(value: str) -> str:
    """Verbatim quote: single-quote wrap with the '\\'' escape."""
    return "'" + value.replace("'", "'\\''") + "'"


class ShellActivity:
    """Opt-in startup integration and revision tracking for one ordinary
    interactive shell (verbatim ShellActivity)."""

    def __init__(self, directory: str, argv: List[str],
                 env: Dict[str, str]) -> None:
        self.directory = directory
        self.argv = argv
        self.env = env
        self.revision = 0
        self.observed = ""
        self.invalidated: Optional[str] = None
        self.state = "unknown"

    def invalidate(self) -> None:
        """Invalidate prompt evidence before delivering input or a
        foreground signal."""
        self.invalidated = self.read()
        self.revision += 1
        self.state = "unknown"

    def inspect(self, pid: int) -> Dict[str, Any]:
        """Read the latest top-level shell transition, fenced against
        input since that transition."""
        record = self.read()
        if record != self.observed:
            self.observed = record
            self.revision += 1
        match = re.match(r"^(\d+):(\d+):(idle|busy)\n?$", record)
        if record == self.invalidated or match is None \
                or match.group(1) != str(pid):
            self.state = "unknown"
        else:
            self.state = match.group(3)
        return {"state": self.state, "revision": self.revision}

    def dispose(self) -> None:
        """Remove private startup and status files after quiescence."""
        shutil.rmtree(self.directory, ignore_errors=True)

    def read(self) -> str:
        try:
            with open(os.path.join(self.directory, "state"), "r",
                      encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""


def _write_private(path: str, text: str) -> None:
    """writeFileSync with mode 0600 and flag wx (exclusive create)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def prepare_shell_activity(spec: Dict[str, Any], env: Dict[str, str],
                           platform: str) -> Optional[ShellActivity]:
    """Prepare optional Bash or Zsh integration for a plain, non-login
    interactive launch (verbatim prepareShellActivity, including both
    startup-file texts)."""
    argv = spec.get("argv") or []
    if spec.get("shellActivity") is not True or platform == "win32" \
            or len(argv) != 2 or argv[1] != "-i":
        return None
    shell = os.path.basename(argv[0])
    if shell != "bash" and shell != "zsh":
        return None
    directory = tempfile.mkdtemp(prefix="dsh-shell-")
    state = quote(os.path.join(directory, "state"))
    guards = quote(os.path.join(directory, "guards"))
    try:
        if shell == "bash":
            rc = os.path.join(directory, "bashrc")
            ps0_payload = (
                "$(builtin printf '%s:%s:busy' \"$__dsh_shell_pid\" "
                f"\"$__dsh_shell_sequence\" >| {state})")
            _write_private(rc, "\n".join([
                "[[ ! -r ~/.bashrc ]] || builtin source ~/.bashrc",
                "if (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && "
                "BASH_VERSINFO[1] >= 4) )) && [[ ! $(declare -p "
                "PROMPT_COMMAND PS0 2>/dev/null) =~ declare\\ "
                "-[^[:space:]]*r ]]; then",
                "  __dsh_shell_pid=$BASHPID; __dsh_shell_sequence=0",
                "  __dsh_shell_idle() {",
                "    local result=$?",
                "    if [[ $BASHPID == \"$__dsh_shell_pid\" ]]; then",
                "      (( ++__dsh_shell_sequence ))",
                "      local activity=idle",
                f"      builtin trap -p >| {guards}",
                f"      [[ ! -s {guards} ]] || activity=unknown",
                "      builtin printf '%s:%s:%s\\n' \"$BASHPID\" "
                "\"$__dsh_shell_sequence\" \"$activity\" "
                f">| {state}",
                "    fi",
                "    return \"$result\"",
                "  }",
                "  if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "
                "\"declare -a \"* ]]; then",
                "    PROMPT_COMMAND+=(__dsh_shell_idle)",
                "  else",
                "    PROMPT_COMMAND=\"${PROMPT_COMMAND}\"$'\\n'"
                "\"__dsh_shell_idle\"",
                "  fi",
                f"  PS0+={quote(ps0_payload)}",
                "fi",
                "",
            ]))
            return ShellActivity(directory, [argv[0], "--rcfile", rc, "-i"],
                                 env)
        zdotdir = env.get("ZDOTDIR")
        zdotdir_line = ("unset ZDOTDIR" if zdotdir is None
                        else f"ZDOTDIR={quote(zdotdir)}")
        _write_private(os.path.join(directory, ".zshenv"), "\n".join([
            zdotdir_line,
            "[[ ! -r ${ZDOTDIR:-$HOME}/.zshenv ]] || builtin source "
            "\"${ZDOTDIR:-$HOME}/.zshenv\"",
            "typeset -g __dsh_shell_pid=$$ __dsh_shell_sequence=0",
            "__dsh_shell_activity() {",
            "  (( ZSH_SUBSHELL == 0 && $$ == __dsh_shell_pid )) || return",
            "  (( ++__dsh_shell_sequence ))",
            f"  builtin printf '%s:%s:%s\\n' \"$$\" "
            f"\"$__dsh_shell_sequence\" \"$1\" >| {state}",
            "  return 0",
            "}",
            "__dsh_shell_idle() {",
            f"  {{ builtin trap; zle -F; }} >| {guards}",
            "  if [[ $CONTEXT != start || -n $BUFFER ]]; then "
            "__dsh_shell_activity busy",
            f"  elif [[ -s {guards} || -n "
            "${(k)functions[(I)TRAP*]} ]]; then "
            "__dsh_shell_activity unknown",
            "  else __dsh_shell_activity idle; fi",
            "}",
            "__dsh_shell_busy() { __dsh_shell_activity busy }",
            "__dsh_shell_init() {",
            "  autoload -Uz add-zle-hook-widget add-zsh-hook",
            "  add-zle-hook-widget line-init __dsh_shell_idle",
            "  add-zle-hook-widget line-finish __dsh_shell_busy",
            "  add-zsh-hook preexec __dsh_shell_busy",
            "  precmd_functions=(${precmd_functions:#__dsh_shell_init})",
            "}",
            "typeset -ga precmd_functions",
            "precmd_functions+=(__dsh_shell_init)",
            "",
        ]))
        return ShellActivity(directory, list(argv),
                             {**env, "ZDOTDIR": directory})
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


# ------------------------------------------------- local terminal handle

class LocalTerminalHandle:
    """A local terminal whose native managed range or fallback
    process-session ownership stays below the PTY backend (verbatim
    LocalTerminalHandle; async methods become synchronous blocking
    forms on threads — every handle call still completes with the same
    ordering guarantees)."""

    def __init__(self, terminal: Any, inspector: Any, grace_ms: float,
                 platform: Optional[str] = None,
                 managed_owner: Any = None,
                 resolve_managed_outcome: Optional[Callable[..., Any]] = None,
                 shell_activity: Optional[ShellActivity] = None,
                 on_quiescence: Optional[Callable[[], None]] = None,
                 observe_shell_exit: bool = False) -> None:
        self.terminal = terminal
        self.inspector = inspector
        self.grace_ms = grace_ms
        self.platform = platform if platform is not None else sys.platform
        self.managed_owner = managed_owner
        self.resolve_managed_outcome = resolve_managed_outcome
        self.shell_activity = shell_activity
        self.on_quiescence = on_quiescence
        self.observe_shell_exit = observe_shell_exit
        self.pid: int = terminal.pid
        self.output = TerminalOutput()
        self.outcome = OutcomeFuture()
        self.data_disposable: Optional[Disposable] = None
        self.exit_disposable: Optional[Disposable] = None
        self.cleanup: Optional[OutcomeFuture] = None
        self.managed_owner_cleaned = False
        self.exited = False
        self.output_paused = False
        self.tracked_descendants: List[Dict[str, Any]] = []
        self.activity_revision = 0
        self.activity_key = ""
        self.quiescent = False
        self.managed_range_empty = False
        #: The spawned shell's start identity; scans stop adopting
        #: members once the root pid no longer carries it.
        self.root_identity: Optional[Dict[str, Any]] = None
        try:
            tree = self.inspector.snapshot().tree(self.pid)
            self.root_identity = next(
                (member for member in tree if member["pid"] == self.pid),
                None)
        except BaseException:
            self.root_identity = None

        self._exit_lock = threading.Lock()

        def resume() -> None:
            if not self.output_paused:
                return
            self.output_paused = False
            if not self.exited:
                terminal.resume()

        self.output.drain_callback = resume

        def on_close() -> None:
            self.output.drain_callback = None

        self.output.close_callback = on_close

        def on_data(data: Any) -> None:
            chunk = data.encode("utf-8") if isinstance(data, str) else data
            if not self.output.write(chunk) and self.cleanup is None \
                    and not self.output_paused:
                self.output_paused = True
                terminal.pause()

        def on_exit(event: Dict[str, Any]) -> None:
            with self._exit_lock:
                if self.exited:
                    return
                self.exited = True
            exit_code = event.get("exitCode")
            exit_signal = event.get("signal")
            if self.managed_owner is not None and self.observe_shell_exit:
                def observe_range() -> None:
                    try:
                        self.managed_owner.wait_for_exit()
                        self.managed_range_empty = True
                    except BaseException:
                        pass

                threading.Thread(target=observe_range, daemon=True,
                                 name="terminal-range-exit").start()
            self.output.end()
            outcome: Dict[str, Any] = {
                "exitCode": (exit_code if exit_signal is None
                             or exit_signal == 0 else None),
                "signal": signal_name(exit_signal),
            }
            try:
                if self.resolve_managed_outcome is not None:
                    resolved = self.resolve_managed_outcome(outcome)
                    outcome = resolved if resolved is not None else outcome
                self.outcome.resolve(outcome)
            except BaseException as error:
                self.outcome.reject(error)

        self.data_disposable = terminal.on_data(on_data)
        self.exit_disposable = terminal.on_exit(on_exit)

    @property
    def done(self) -> OutcomeFuture:
        return self.outcome

    @property
    def running(self) -> bool:
        """Whether node-pty has not yet published the top-level exit."""
        return not self.exited

    # -- synchronous handle calls ------------------------------------------

    def write(self, data: str) -> None:
        if self.exited:
            raise RuntimeError("terminal process has exited")
        if self.shell_activity is not None:
            self.shell_activity.invalidate()
        self.terminal.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if self.exited:
            raise RuntimeError("terminal process has exited")
        self.terminal.resize(cols, rows)

    def inspect_foreground(self) -> Optional[Dict[str, Any]]:
        self.descendants(self.inspector.snapshot())
        process_group_id = self.inspector.foregroundPgid(self.pid)
        if process_group_id is None:
            return None
        return {
            "processGroupId": process_group_id,
            "inputWaiting": self.inspector.isStdinWaiting(
                process_group_id, self.pid),
        }

    def inspect_activity(self) -> Dict[str, Any]:
        state = "idle" if self.quiescent else "unknown"
        revision = 0
        if not self.quiescent:
            try:
                if self.shell_activity is not None:
                    shell = self.shell_activity.inspect(self.pid)
                else:
                    shell = {"state": "unknown", "revision": 0}
                revision = shell["revision"]
                observed = self.inspector.snapshot()
                descendants = self.descendants(observed)
                root = next((member
                             for member in observed.tree(self.pid)
                             if member["pid"] == self.pid), None)
                if self.exited and self.managed_range_empty:
                    state = "idle"
                elif len(descendants) > 0:
                    state = "busy"
                elif self.exited and self.managed_owner is None \
                        and self.platform == "linux" \
                        and observed.complete is True and root is None:
                    state = ("busy" if any(observed.alive(member) for member
                                           in observed.session(self.pid))
                             else "idle")
                elif observed.complete is True and root is not None \
                        and self.root_identity is not None \
                        and root["started"] == self.root_identity["started"]:
                    foreground = self.inspector.foregroundPgid(self.pid)
                    if foreground is None:
                        state = "unknown"
                    elif foreground == self.pid:
                        state = shell["state"]
                    else:
                        state = "busy"
                    if state == "idle" and self.managed_owner is not None:
                        inspect_tasks = getattr(
                            self.managed_owner, "inspect_task_count", None)
                        tasks = inspect_tasks() if inspect_tasks else None
                        if tasks is None or tasks < 1:
                            state = "unknown"
                        elif tasks == 1:
                            state = "idle"
                        else:
                            state = "busy"
            except BaseException:
                state = "unknown"
        key = f"{revision}:{state}"
        if key != self.activity_key:
            self.activity_key = key
            self.activity_revision += 1
        return {"state": state, "revision": self.activity_revision}

    def signal_foreground(self, signal: str) -> int:
        if self.shell_activity is not None:
            self.shell_activity.invalidate()
        foreground = self.inspect_foreground()
        if foreground is None:
            raise RuntimeError(
                f"cannot resolve foreground process group for terminal "
                f"{self.pid}")
        if signal == "SIGKILL" and foreground["processGroupId"] == self.pid:
            raise RuntimeError(
                "refusing to SIGKILL the terminal shell; terminate the "
                "terminal session instead")
        if self.platform == "win32":
            if signal == "SIGINT":
                self.terminal.write("\x03")
                return foreground["processGroupId"]
            if signal in ("SIGTSTP", "SIGHUP"):
                raise RuntimeError(
                    f"signal {signal} is unsupported on Windows; only "
                    "SIGINT, SIGTERM, and SIGKILL are available")
        self.inspector.signalGroup(foreground["processGroupId"], signal)
        return foreground["processGroupId"]

    # -- termination ----------------------------------------------------------

    def terminate(self) -> OutcomeFuture:
        if self.cleanup is not None:
            return self.cleanup
        if self.output_paused:
            self.output_paused = False
            self.terminal.resume()
        future = OutcomeFuture()
        self.cleanup = future

        def run() -> None:
            try:
                self.close_once()
                self.quiescent = True
                if self.shell_activity is not None:
                    self.shell_activity.dispose()
                if self.on_quiescence is not None:
                    self.on_quiescence()
                future.resolve(None)
            except BaseException as error:
                if self.cleanup is future:
                    self.cleanup = None
                future.reject(error)

        threading.Thread(target=run, name="terminal-cleanup",
                         daemon=True).start()
        return future

    def terminate_for_host_exit(self) -> None:
        """Force-terminate the observable session synchronously during
        host exit. This does not claim quiescence and does not replace
        terminate()."""
        self.force_stop_descendants()
        self.force_stop_shell()
        self.force_stop_descendants()
        if self.managed_owner is not None:
            self.managed_owner.terminate_for_host_exit()

    def force_stop_shell(self) -> None:
        if self.exited:
            return
        if self.root_identity is not None:
            try:
                self.inspector.signalProcess(self.root_identity, "SIGKILL")
            except BaseException:
                pass
            return
        try:
            self.terminal.kill("SIGKILL")
        except BaseException:
            pass

    def survivors(self, members: List[Dict[str, Any]], observed: Any
                  ) -> List[Dict[str, Any]]:
        return [member for member in members if observed.alive(member)]

    def descendants(self, observed: Any) -> List[Dict[str, Any]]:
        tree = observed.tree(self.pid)
        root = next((member for member in tree
                     if member["pid"] == self.pid), None)
        root_verified = (self.root_identity is not None and root is not None
                         and root["started"] == self.root_identity["started"])
        groups: List[List[Dict[str, Any]]] = [self.tracked_descendants]
        if root_verified:
            groups.append(tree)
            groups.append(observed.session(self.pid))
        union = self.union_members(*groups)
        self.tracked_descendants = self.survivors(
            [member for member in union if member["pid"] != self.pid],
            observed)
        return self.tracked_descendants

    def wait_for_members(self, members: List[Dict[str, Any]]
                         ) -> List[Dict[str, Any]]:
        if len(members) == 0:
            return []
        until = time.monotonic() + self.grace_ms / 1000
        survivors = self.survivors(members, self.inspector.snapshot())
        while len(survivors) > 0 and time.monotonic() < until:
            remaining_ms = (until - time.monotonic()) * 1000
            delay(min(25.0, max(1.0, remaining_ms)))
            survivors = self.survivors(members, self.inspector.snapshot())
        return survivors

    def signal_members(self, members: List[Dict[str, Any]],
                       signal: str) -> None:
        for member in members:
            try:
                self.inspector.signalProcess(member, signal)
            except BaseException:
                pass

    def force_stop_descendants(self) -> None:
        members = self.tracked_descendants
        try:
            members = self.descendants(self.inspector.snapshot())
        except BaseException:
            pass
        self.signal_members(members, "SIGKILL")

    def union_members(self, *groups: Sequence[Dict[str, Any]]
                      ) -> List[Dict[str, Any]]:
        members: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for group in groups:
            for member in group:
                key = f"{member['pid']}:{member['started']}"
                if key in seen:
                    continue
                seen.add(key)
                members.append(member)
        return members

    def stop_descendants(self) -> List[Dict[str, Any]]:
        captured = self.descendants(self.inspector.snapshot())
        self.signal_members(captured, "SIGTERM")
        captured_survivors = self.wait_for_members(captured)
        members = self.union_members(
            captured_survivors, self.descendants(self.inspector.snapshot()))
        self.signal_members(members, "SIGKILL")
        survivors = self.wait_for_members(members)
        observed = self.inspector.snapshot()
        return self.survivors(
            self.union_members(survivors, self.descendants(observed)),
            observed)

    def _race_done_or_delay(self) -> None:
        """Promise.race([done.then(noop), delay(graceMs)]): a rejected
        done propagates like the upstream race."""
        self.outcome.wait(self.grace_ms / 1000)
        if self.outcome.error is not None:
            raise self.outcome.error

    def stop_shell(self) -> None:
        if self.platform == "win32":
            self.stop_shell_windows()
            return
        if not self.exited:
            try:
                self.terminal.kill("SIGTERM")
            except BaseException:
                pass
            self._race_done_or_delay()
        if not self.exited:
            try:
                self.terminal.kill("SIGKILL")
            except BaseException:
                pass
            self._race_done_or_delay()
        if not self.exited:
            raise RuntimeError(
                f"terminal cleanup failed; surviving pid: {self.pid}")

    def stop_shell_windows(self) -> None:
        def shell_gone() -> bool:
            return self.exited or (
                self.root_identity is not None
                and not self.inspector.isAlive(self.root_identity))

        if not shell_gone() and self.root_identity is not None:
            self.inspector.signalProcess(self.root_identity, "SIGTERM")
            self.wait_for_windows_shell_exit()
        if not shell_gone() and self.root_identity is None:
            try:
                self.terminal.kill()
            except BaseException:
                pass
            self._race_done_or_delay()
        if not shell_gone() and self.root_identity is not None:
            self.inspector.signalProcess(self.root_identity, "SIGKILL")
            self.wait_for_windows_shell_exit()
        if not shell_gone():
            raise RuntimeError(
                f"terminal cleanup failed; surviving pid: {self.pid}")

    def wait_for_windows_shell_exit(self) -> None:
        until = time.monotonic() + self.grace_ms / 1000
        while not self.exited and time.monotonic() < until:
            if self.root_identity is not None \
                    and not self.inspector.isAlive(self.root_identity):
                return
            remaining_ms = (until - time.monotonic()) * 1000
            delay(min(25.0, max(1.0, remaining_ms)))

    def close_once(self) -> None:
        if self.managed_owner is not None:
            try:
                self.close_managed_range(self.managed_owner)
                if self.data_disposable is not None:
                    self.data_disposable.dispose()
                if self.exit_disposable is not None:
                    self.exit_disposable.dispose()
            finally:
                owner = self.managed_owner

                def cleanup_owner(_future: Optional[OutcomeFuture] = None
                                  ) -> None:
                    self.cleanup_managed_owner(owner)

                if self.outcome.settled:
                    cleanup_owner()
                else:
                    self.outcome.add_done_callback(cleanup_owner)
            self.close_backend_terminal()
            return
        survivors = self.stop_descendants()
        if len(survivors) > 0:
            raise RuntimeError(
                "terminal cleanup failed; surviving pids: "
                + ", ".join(str(member["pid"]) for member in survivors))
        self.stop_shell()
        survivors = self.stop_descendants()
        if len(survivors) > 0:
            raise RuntimeError(
                "terminal cleanup failed; surviving pids: "
                + ", ".join(str(member["pid"]) for member in survivors))
        self.settle_exit_if_gone()
        if self.data_disposable is not None:
            self.data_disposable.dispose()
        if self.exit_disposable is not None:
            self.exit_disposable.dispose()
        self.close_backend_terminal()

    def close_backend_terminal(self) -> None:
        """Release the terminal backend itself (the node-pty sidecar when
        the sidecar seam is in use). The handle-level stop paths above only
        terminate the shell and its descendants, which left the sidecar
        process and its locator directory alive after terminate()."""
        closer = getattr(self.terminal, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:
            pass

    def cleanup_managed_owner(self, owner: Any) -> None:
        if self.managed_owner_cleaned:
            return
        self.managed_owner_cleaned = True
        cleanup = getattr(owner, "cleanup", None)
        if cleanup is not None:
            cleanup()

    def close_managed_range(self, owner: Any) -> None:
        owner.signal("SIGTERM")

        def observe() -> Dict[str, Any]:
            try:
                owner.wait_for_exit()
                return {"kind": "stopped"}
            except BaseException as error:
                return {"kind": "failed", "error": error}

        first = race_with_delay(observe, self.grace_ms, {"kind": "timeout"})
        if first["kind"] != "stopped":
            owner.signal("SIGKILL")
            if first["kind"] == "failed":
                try:
                    owner.wait_for_exit()
                except BaseException as final_error:
                    raise ExceptionGroup(
                        "terminal managed-range cleanup failed",
                        [first["error"], final_error])
                raise first["error"]
            owner.wait_for_exit()
        if not self.exited:
            self.outcome.wait(self.grace_ms / 1000)
        if not self.exited:
            raise RuntimeError(
                f"terminal cleanup failed; surviving pid: {self.pid}")

    def settle_exit_if_gone(self) -> None:
        if self.platform != "win32":
            return
        if self.exited:
            return
        # stopShellWindows() verified the shell is gone or threw; the
        # identity re-check is a defensive fence for a future caller.
        if self.root_identity is not None \
                and self.inspector.isAlive(self.root_identity):
            return
        self.exited = True
        self.output.end()
        self.outcome.resolve({"exitCode": None, "signal": None})


# ------------------------------------------------------------ pty sidecar

#: Controlled node-pty sidecar script shipped with the runtime.
SIDECAR_SCRIPT = Path(__file__).resolve().parent / "resources" \
    / "node_pty_sidecar.js"

#: Host defaults for the sidecar launch (injectable through internals:
#: sidecarCommand / sidecarScript / nodePtyPath).
DEFAULT_SIDECAR_COMMAND = r"C:\Program Files\DSH Desktop\DSH Desktop.exe"
DEFAULT_NODE_PTY_PATH = (r"C:\Program Files\DSH Desktop\resources"
                         r"\app.asar.unpacked\node_modules\node-pty")


class SidecarTerminal:
    """The node-pty IPty seam over the controlled sidecar: one terminal
    per sidecar process, JSON lines over 127.0.0.1, locator-file port
    handoff (the sandbox refuses anonymous pipes for spawned processes;
    localhost TCP is the proven carrier)."""

    def __init__(self, file: str, args: Sequence[str],
                 options: Dict[str, Any],
                 internals: Optional[Dict[str, Any]] = None) -> None:
        api = internals or {}
        command = api.get("sidecarCommand") or DEFAULT_SIDECAR_COMMAND
        script = api.get("sidecarScript") or str(SIDECAR_SCRIPT)
        pty_path = api.get("nodePtyPath") or DEFAULT_NODE_PTY_PATH
        self._directory = tempfile.mkdtemp(prefix="dsh-pty-sidecar-")
        self._locator_path = os.path.join(self._directory, "sidecar.json")
        self._data_callbacks: List[Callable[[str], None]] = []
        self._exit_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._pending_event = threading.Event()
        self._socket: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        try:
            env = dict(os.environ)
            env["ELECTRON_RUN_AS_NODE"] = "1"
            # SW_HIDE via startupinfo: CREATE_NO_WINDOW fails nested
            # process initialization (0xC0000142) under this host's
            # sandbox; the hidden-window form is the proven carrier.
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
            self._process = subprocess.Popen(
                [command, script, self._locator_path, pty_path],
                env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=startupinfo)
            locator = self._wait_for_locator(20.0)
            self._socket = socket.create_connection(
                ("127.0.0.1", int(locator["port"])), timeout=20.0)
            self._socket.settimeout(None)
            self._reader = threading.Thread(target=self._read_loop,
                                            name="pty-sidecar-reader",
                                            daemon=True)
            self._reader.start()
            self._send({"type": "spawn", "file": file, "args": list(args),
                        "options": options})
            response = self._await_response("spawned", 20.0)
            self.pid = int(response["pid"])
        except BaseException:
            self.close()
            raise

    def _wait_for_locator(self, timeout_s: float) -> Dict[str, Any]:
        until = time.monotonic() + timeout_s
        while time.monotonic() < until:
            if os.path.exists(self._locator_path):
                try:
                    with open(self._locator_path, "r",
                              encoding="utf-8") as handle:
                        return json.loads(handle.read())
                except (OSError, ValueError):
                    pass
            if self._process is not None \
                    and self._process.poll() is not None:
                raise RuntimeError(
                    "node-pty sidecar exited before publishing its locator "
                    f"(exit {self._process.returncode})")
            time.sleep(0.05)
        raise RuntimeError("node-pty sidecar locator timed out")

    def _send(self, message: Dict[str, Any]) -> None:
        sock = self._socket
        if sock is None:
            raise RuntimeError("sidecar connection is closed")
        payload = (json.dumps(message) + "\n").encode("utf-8")
        with self._send_lock:
            sock.sendall(payload)

    def _await_response(self, kind: str, timeout_s: float
                        ) -> Dict[str, Any]:
        until = time.monotonic() + timeout_s
        while True:
            with self._pending_lock:
                message = self._pending.pop(kind, None)
                error = self._pending.pop("error", None)
            if error is not None:
                raise RuntimeError(
                    f"node-pty sidecar reported: {error.get('message')}")
            if message is not None:
                return message
            if time.monotonic() >= until:
                raise RuntimeError(
                    f"node-pty sidecar response '{kind}' timed out")
            self._pending_event.wait(0.05)
            self._pending_event.clear()

    def _read_loop(self) -> None:
        sock = self._socket
        if sock is None:
            return
        buffer = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    self._dispatch(message)
        except OSError:
            pass

    def _dispatch(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "data":
            payload = str(message.get("data", ""))
            for data_callback in list(self._data_callbacks):
                try:
                    data_callback(payload)
                except BaseException:
                    pass
            return
        if kind == "exit":
            event: Dict[str, Any] = {"exitCode": message.get("exitCode"),
                                     "signal": message.get("signal")}
            for exit_callback in list(self._exit_callbacks):
                try:
                    exit_callback(event)
                except BaseException:
                    pass
            return
        with self._pending_lock:
            self._pending[str(kind)] = message
        self._pending_event.set()

    # -- IPty seam -----------------------------------------------------------

    def on_data(self, callback: Callable[[str], None]) -> Disposable:
        self._data_callbacks.append(callback)

        def remove() -> None:
            if callback in self._data_callbacks:
                self._data_callbacks.remove(callback)

        return Disposable(remove)

    def on_exit(self, callback: Callable[[Dict[str, Any]], None]
                ) -> Disposable:
        self._exit_callbacks.append(callback)

        def remove() -> None:
            if callback in self._exit_callbacks:
                self._exit_callbacks.remove(callback)

        return Disposable(remove)

    def write(self, data: str) -> None:
        self._send({"type": "write", "data": data})

    def resize(self, cols: int, rows: int) -> None:
        self._send({"type": "resize", "cols": cols, "rows": rows})

    def kill(self, signal: Optional[str] = None) -> None:
        message: Dict[str, Any] = {"type": "kill"}
        if signal is not None:
            message["signal"] = signal
        self._send(message)

    def pause(self) -> None:
        self._send({"type": "pause"})

    def resume(self) -> None:
        self._send({"type": "resume"})

    def close(self) -> None:
        try:
            if self._socket is not None:
                try:
                    self._send({"type": "shutdown"})
                except (OSError, RuntimeError):
                    pass
                self._socket.close()
        except OSError:
            pass
        self._socket = None
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass
        self._process = None
        shutil.rmtree(self._directory, ignore_errors=True)


def default_pty_spawn(internals: Dict[str, Any]
                      ) -> Callable[[str, Sequence[str], Dict[str, Any]],
                                    SidecarTerminal]:
    """The requireNodePty().spawn boundary: the controlled sidecar."""
    def spawn(file: str, args: Sequence[str], options: Dict[str, Any]
              ) -> SidecarTerminal:
        return SidecarTerminal(file, args, options, internals)

    return spawn


# ------------------------------------------------------- spawnTerminal

def spawn_terminal(runtime: Any, spec: Dict[str, Any]) -> LocalTerminalHandle:
    """Allocate one terminal session (verbatim spawnTerminal wiring: the
    inspector, containment selection, shell activity, the linux-scope
    launch envelope, the direct settlement seam, and the release rule
    retaining shell-activity terminals until explicit termination)."""
    from AstralNexus.runtime.subprocess_local import (
        _signal_number, create_process_inspector,
        prepare_linux_terminal_scope, signal_linux_direct_process,
        target_environment)

    argv = spec.get("argv") or []
    file = argv[0] if len(argv) > 0 else None
    if file is None or file == "":
        raise RuntimeError(
            "subprocess-local: terminal argv must contain a program")
    spec_signal = spec.get("signal")
    if spec_signal is not None:
        spec_signal.throw_if_aborted()
    inspector = runtime.terminal_inspector or create_process_inspector()
    containment_mode = runtime.select_containment_mode("terminal")
    env = target_environment(spec)
    platform = runtime.internals.get("platform") or sys.platform
    activity = prepare_shell_activity(spec, env, platform)
    launch = dict(spec)
    if activity is not None:
        launch["argv"] = activity.argv
        launch["env"] = activity.env
    terminal_type = spec.get("terminalType")
    base_env: Dict[str, str] = dict(
        activity.env if activity is not None else env)
    options: Dict[str, Any] = {
        "name": terminal_type,
        "rows": spec.get("rows"),
        "cols": spec.get("cols"),
        "cwd": spec.get("cwd"),
        "env": {**base_env, "TERM": str(terminal_type)},
    }
    scope: Optional[Dict[str, Any]] = None
    terminal: Any = None
    try:
        if containment_mode == "linux-scope":
            scope_env: Dict[str, str] = {**base_env, "PWD": spec["cwd"],
                                         "TERM": str(terminal_type)}
            scope = prepare_linux_terminal_scope(launch, scope_env)
            options["cwd"] = scope["cwd"]
            options["env"] = scope["env"]
        pty_spawn = runtime.internals.get("ptySpawn") \
            or default_pty_spawn(runtime.internals)
        terminal = pty_spawn(
            scope["command"] if scope is not None else file,
            scope["args"] if scope is not None else list(launch["argv"][1:]),
            options)
    except BaseException:
        if scope is not None:
            scope["cleanup"]()
        if activity is not None:
            activity.dispose()
        raise
    handle_box: Dict[str, LocalTerminalHandle] = {}
    direct_settlement = threading.Event()
    owner = None
    if scope is not None:
        terminal_pid = terminal.pid

        def direct_signal(signal: str) -> bool:
            def send() -> bool:
                os.kill(terminal_pid, _signal_number(signal))
                return True

            return signal_linux_direct_process(terminal_pid, send)

        owner = scope["bindOwner"]({
            "running": lambda: (handle_box["handle"].running
                                if "handle" in handle_box else True),
            "settled": direct_settlement,
            "signal": direct_signal,
        })
    handle = LocalTerminalHandle(
        terminal, inspector, spec.get("graceMs", 5000), platform, owner,
        scope["resolveOutcome"] if scope is not None else None, activity,
        lambda: runtime.terminals.discard(handle_box["handle"]),
        spec.get("shellActivity") is True)
    handle_box["handle"] = handle
    runtime.terminals.add(handle)

    def release(_future: Optional[OutcomeFuture] = None) -> None:
        direct_settlement.set()
        if spec.get("shellActivity") is True:
            return

        def run() -> None:
            try:
                handle.terminate().result()
            except BaseException:
                pass
            runtime.terminals.discard(handle)

        threading.Thread(target=run, name="terminal-release",
                         daemon=True).start()

    handle.outcome.add_done_callback(release)
    return handle
