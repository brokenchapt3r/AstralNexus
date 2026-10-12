"""Native port of @deepseek-ai/dsh-subprocess-local (commit 5badb150;
lib/index.js 1485 lines, lib/runner.js 436, lib/output.js 239 [separate
module runtime/subprocess_output.py], lib/runner-launch-B2zsQ1Dz.js 1529 —
all read in full) — the local-subprocess implementation of the DeepSeek
Harness subprocess seam.

What ships in this wave (the ordinary-spawn half that runs on this host):
- environment plane: ``child_env`` (scrubbed parent env + explicit
  overrides with win32 case-insensitive merging), ``target_environment``
  (full validation: null-byte rejection of file/args/cwd/env with the
  ERR_INVALID_ARG_VALUE message shape, undefined-tombstone filtering,
  control-marker reservation), ``control_environment``
- spec validation: ``validate_subprocess_spec`` (graceMs positive finite
  and within the int32 timer ceiling, pre-aborted signal rejection,
  non-empty argv[0])
- Windows executable resolution: ``resolve_windows_executable`` with the
  complete libuv search order (explicit directory or the
  NoDefaultCurrentDirectoryInExePath-gated cwd + PATH roots; ``.com``/
  ``.exe`` suffix rules; stat-then-lstat existence checks), PATH parsing
  (quoted and semicolon-delimited forms) and case-insensitive environment
  reads
- spawn error normalization to the bounded Node-shaped record
  (``serialize_runner_error`` / ``as_spawn_error`` with the Windows code
  map 2/3/267→ENOENT, 5→EPERM, 193→EFTYPE, 740→EACCES)
- fallback launch: ``spawn_subprocess`` (detached platform spawn with
  hidden initial windows) + ``fallback_owner`` (taskkill /T tree
  signalling on win32, PGID-then-PID on posix with ESRCH/EPERM liveness
  semantics) + ``bind_managed_process`` (the full stdio/outcome/abort/
  termination lifecycle with OutputCollector binding, the SIGTERM→grace→
  SIGKILL ladder, sealed output reads, and host-exit escalation)
- Windows Job containment, DIRECT form: ``probe_windows_job`` /
  ``WindowsJobOwner`` / ``launch_windows_job`` built on the ported
  dsh-win32-process primitives (suspended create + kill-on-close Job
  assignment + resume; 10 ms poll of process exit and Job emptiness;
  TerminateJobObject signalling)
- process inspectors: ``WindowsProcessInspector`` (real Toolhelp32 +
  GetProcessTimes creation-time identity + taskkill), Linux
  (/proc scans + the complete stdin-blocking syscall detector: read/
  select/pselect/poll/ppoll/epoll_wait with /proc/pid/mem argument
  inspection) and macOS (ps-based) platform tables, ``process_tree``
  children-first with zombie and creation-time fences
- Linux scope surface (contract logic with injectable seams; real systemd
  runs only on Linux hosts): ``SystemdScopeOwner`` state machine, the
  probe trio, ``launch_linux_scope``, launch-file plumbing (0600 wx
  request/result/startup-error files, the execve bootstrap chain with the
  ENOEXEC /bin/sh fallback and PATH walk)
- ``LocalSubprocessRuntime`` service: spawn with containment selection
  (windows-job → fallback with the one-shot latch warning carrying the
  verbatim reason texts), resolveExecutable (X_OK candidates + PATHEXT +
  the relative-path rejection), terminalEnvironment, managed-range
  disposal, and host-exit finalization

Subset boundary (documented, not faked):
- The Node IPC runner protocol (runner.js WindowsJobRunner, the fd 3/4/5/6
  carrier layout, DSH_SUBPROCESS_RUNNER selection, strict message parses)
  is NOT ported: it exists because a Node parent cannot own Job handles
  through child_process. Python owns the Job directly through the ported
  win32 primitives, and kill-on-close semantics are equivalent — the Job's
  last handle closes when the owning process dies, so a crashed host still
  sweeps the managed range. The runner's settle-before-exit discipline
  lives in WindowsJobOwner.wait_for_exit here.
- ``spawn_terminal`` (node-pty backend + LocalTerminalHandle + shell
  activity files) is the next wave (R13): node-pty is a Node native module
  requiring the controlled-sidecar decision; the handle logic ports
  against that seam.
- Sandbox constraint on this host: CreatePipe is refused for probe/test
  processes, so pipe-stdio spawns are covered by injectable-seam unit
  tests (upstream's own strategy for thin OS bindings); the no-pipe paths
  (Job direct containment with inherited/DEVNULL stdio, resolve, env,
  collectors, taskkill) are verified against the real platform.

Python adaptations (documented):
- AbortSignal → ``CancellationToken`` (aborted/reason/listeners).
- Node streams → daemon reader threads pushing into OutputCollector;
  ``done``/``collected`` are Events resolved by the exit-wait thread after
  the readers reach EOF (the outputStreamsClosed ordering: collected output
  is complete before the exit result resolves).
- windowsHide → STARTUPINFO(SW_HIDE) exactly like Node, NOT
  CREATE_NO_WINDOW (which fails with 0xC0000142 under this sandbox and
  allocates no console at all — a different semantic).
- detached → CREATE_NEW_PROCESS_GROUP (win32) / start_new_session (posix).
- uv_get_osfhandle → msvcrt.get_osfhandle (inside win32_process).
- execve bootstrap → os.execve with fcntl FD_CLOEXEC clearing (posix only,
  lazily imported).
"""
from __future__ import annotations

import errno as errno_module
import inspect as _inspect
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, cast

from AstralNexus.runtime import win32_process
from AstralNexus.runtime.compat import ExceptionGroup
from AstralNexus.runtime.dshutil.timeout import MAX_TIMER_DELAY_MS
from AstralNexus.runtime.subprocess_output import (
    OutputCollector, log_spill_failure, prepare_managed_process_binding)
from AstralNexus.runtime.subprocess_runtime import (
    SUBPROCESS_CONTROL_ENV, SUBPROCESS_CONTROL_FD,
    SubprocessExecutableNotFoundError, SubprocessRuntime, scrubbed_parent_env)

__all__ = [
    "CancellationToken", "LinuxProcessInspector", "LinuxScopeStartup",
    "LocalSubprocessRuntime", "MANAGED_PROCESS_KIND",
    "MacProcessInspector", "ManagedProcessHandle", "NODE_SPAWN_DETAIL_CODES",
    "PosixProcessSnapshot", "SUBPROCESS_RUNNER_ENV", "SystemdScopeOwner",
    "WINDOWS_RUNNER_SELECTION", "WINDOWS_SPAWN_ERROR_CODES",
    "WindowsJobOwner", "WindowsProcessInspector", "WindowsProcessObservation",
    "as_spawn_error", "bind_managed_process", "child_env",
    "cleanup_linux_launch_files", "consume_linux_launch_request",
    "control_environment", "create_linux_launch_files",
    "create_process_inspector", "create_windows_process_inspector",
    "default_windows_process_internals", "descriptor_shape",
    "environment_value", "executable_candidate_exists", "fallback_owner",
    "has_exact_keys", "is_record", "is_serialized_runner_error",
    "is_string_record", "linux_launch_files_from_locator",
    "linux_process_group_has_live_members", "linux_syscall_tables",
    "load_linux_execve", "mac_process_table", "node_spawn_error",
    "parse_error_result", "parse_proc_stat", "prepare_linux_terminal_scope",
    "probe_linux_bootstrap", "probe_linux_manager", "probe_linux_native",
    "probe_linux_scope", "probe_windows_job", "process_tree",
    "query_systemctl", "quiescent", "quote", "read_linux_startup_error",
    "resolve_windows_executable", "run_sync_default", "runner_environment",
    "runner_invocation_available", "scope_args", "serialize_runner_error",
    "deserialize_runner_error", "signal_child_group",
    "signal_linux_direct_process", "signal_tree", "sleep_with_abort",
    "spawn_runner_invocation", "spawn_subprocess", "syscall_waits_on_stdin",
    "target_environment", "taskkill_process_tree", "taskkill_tree",
    "validate_subprocess_spec", "windows_environment_value",
    "windows_executable_names", "windows_file_name_start",
    "windows_path_directories", "windows_process_tree",
    "windows_search_path_join", "write_linux_startup_error",
]

#: Managed-range kind reported by every local launch path.
MANAGED_PROCESS_KIND = "local-subprocess"

#: Private variable marking the Node runner entry (protocol vocabulary;
#: the IPC runner itself is not ported — see the module boundary note).
SUBPROCESS_RUNNER_ENV = "DSH_SUBPROCESS_RUNNER"
WINDOWS_RUNNER_SELECTION = "windows-job"

#: Node spawn error detail codes carried with a path.
NODE_SPAWN_DETAIL_CODES: Set[str] = {"EACCES", "ENOENT"}

#: Windows spawn error code map (verbatim upstream).
WINDOWS_SPAWN_ERROR_CODES: Dict[int, str] = {
    2: "ENOENT", 3: "ENOENT", 267: "ENOENT",
    5: "EPERM", 193: "EFTYPE", 740: "EACCES",
}

#: Direct-process exit polling while the managed range stays alive.
TERMINATE_POLL_INTERVAL_MS = 10

# Linux scope constants (verbatim).
SYSTEMCTL_TIMEOUT_MS = 5000
SCOPE_INITIAL_POLL_INTERVAL_MS = 50
MISSING_UNIT = re.compile(
    r"\bunit\b[^\r\n]*(?:could not be found|not found|not loaded)",
    re.IGNORECASE | re.UNICODE)


# ------------------------------------------------------------ cancellation

from .subprocess_cancellation import CancellationToken


# ------------------------------------------------------------ environment

def windows_environment_value(env: Dict[str, Optional[str]], name: str
                              ) -> Optional[str]:
    """Read an uppercase environment name with Windows case-insensitive
    key semantics (verbatim: sorted keys, exact uppercase match)."""
    for key in sorted(env.keys()):
        if key.upper() == name:
            return env[key]
    return None


def environment_value(env: Dict[str, Optional[str]], name: str) -> Optional[str]:
    """Read an environment key using the platform's case semantics."""
    exact = env.get(name)
    if exact is not None or sys.platform != "win32":
        return exact
    return windows_environment_value(env, name.upper())


def child_env(extra: Optional[Dict[str, Optional[str]]] = None
              ) -> Dict[str, Optional[str]]:
    """The scrubbed parent environment plus explicit overrides.

    Verbatim childEnv: on POSIX the layers merge directly; on Windows each
    override first removes every case-insensitively equal inherited key,
    then appends its own entry. A None (undefined) value is KEPT as a
    tombstone here — the undefined filter runs in target_environment.
    """
    env: Dict[str, Optional[str]] = dict(scrubbed_parent_env())
    entries = list(env.items())
    if sys.platform != "win32":
        for key, value in (extra or {}).items():
            entries = [entry for entry in entries if entry[0] != key]
            entries.append((key, value))
        return dict(entries)
    for key, value in (extra or {}).items():
        normalized = key.upper()
        entries = [entry for entry in entries
                   if entry[0].upper() != normalized]
        entries.append((key, value))
    return dict(entries)


def control_environment(env: Dict[str, str],
                        control: Optional[str]) -> Dict[str, str]:
    """Stamp the private marker on a fresh child environment after
    rejecting a caller override."""
    for key, value in env.items():
        if key.upper() == SUBPROCESS_CONTROL_ENV and value is not None:
            raise RuntimeError(
                f"{SUBPROCESS_CONTROL_ENV} is reserved for subprocess "
                f"control-channel setup")
    if control == "pipe":
        env[SUBPROCESS_CONTROL_ENV] = "pipe"
    return env


def throw_null_byte_error(property_name: str, value: str,
                          argument: bool = False) -> None:
    """Raise the ERR_INVALID_ARG_VALUE-shaped null-byte rejection
    (verbatim Node message shape, including the error code attribute)."""
    subject = (f"The argument '{property_name}'" if argument
               else f"The property '{property_name}'")
    error = TypeError(
        f"{subject} must be a string without null bytes. "
        f"Received {_inspect_repr(value)}")
    error.code = "ERR_INVALID_ARG_VALUE"  # type: ignore[attr-defined]
    raise error


def _inspect_repr(value: Any) -> str:
    """util.inspect approximation for the error message shape."""
    if isinstance(value, str):
        escaped = (value.replace("\\", "\\\\").replace("'", "\\'")
                   .replace("\0", "\\0").replace("\n", "\\n"))
        return f"'{escaped}'"
    return repr(value)


def validate_no_null_byte(property_name: str, value: Any,
                          argument: bool = False) -> None:
    """Reject embedded NULs for one string input."""
    if isinstance(value, str) and "\0" in value:
        throw_null_byte_error(property_name, value, argument)


def is_record(value: Any) -> bool:
    return isinstance(value, dict)


def validate_subprocess_spec(spec: Dict[str, Any]) -> None:
    """Validate the synchronous portion of one ordinary spawn request
    (verbatim upstream messages)."""
    grace = spec.get("graceMs")
    if not isinstance(grace, (int, float)) or isinstance(grace, bool):
        raise RuntimeError(
            f"subprocess graceMs must be a positive finite number no "
            f"greater than {MAX_TIMER_DELAY_MS}")
    value = float(grace)
    if value != value or value in (float("inf"), float("-inf")) \
            or value <= 0 or value > MAX_TIMER_DELAY_MS:
        raise RuntimeError(
            f"subprocess graceMs must be a positive finite number no "
            f"greater than {MAX_TIMER_DELAY_MS}")
    signal = spec.get("signal")
    if signal is not None and signal.aborted:
        reason = "aborted"
        try:
            reason = str(signal.reason if signal.reason is not None
                         else reason)
        except Exception:
            pass
        raise RuntimeError(f"aborted before spawn: {reason}")
    argv = spec.get("argv") or []
    program = argv[0] if argv else None
    if not isinstance(program, str) or program == "":
        raise RuntimeError(
            "invalid argv: expected a non-empty program name at argv[0]")


def target_environment(spec: Dict[str, Any]) -> Dict[str, str]:
    """Materialize and synchronously validate the final target environment
    (verbatim targetEnvironment: argv entries as argument subjects, cwd and
    env entries as property subjects, undefined tombstones filtered)."""
    argv = spec.get("argv") or []
    for index, value in enumerate(argv):
        validate_no_null_byte("file" if index == 0 else f"args[{index - 1}]",
                              value, True)
    validate_no_null_byte("options.cwd", spec["cwd"])
    merged = child_env(spec.get("env"))
    env = {key: value for key, value in merged.items() if value is not None}
    for key, value in env.items():
        validate_no_null_byte(f"options.env['{key}']", key)
        validate_no_null_byte(f"options.env['{key}']", value)
    return control_environment(env, (spec.get("stdio") or {}).get("control"))


# --------------------------------------------- windows executable resolve

def windows_file_name_start(command: str) -> int:
    """Start of the file-name portion: scan back to the last path
    separator (verbatim windowsFileNameStart)."""
    start = len(command)
    while start > 0 and command[start - 1] not in "\\/:":
        start -= 1
    return start


def windows_path_directories(path_value: str) -> List[str]:
    """Parse a Windows PATH into directories (verbatim scanner: leading
    separators skipped, a leading quote runs to its closing quote before
    the next separator, wrapping quotes are stripped, empty entries are
    dropped)."""
    directories: List[str] = []
    start = 0
    while start < len(path_value):
        if path_value[start] == ";":
            start += 1
            continue
        quote = path_value[start]
        quoted = quote in ('"', "'")
        quote_end = path_value.find(quote, start + 1) if quoted else -1
        separator = path_value.find(
            ";",
            (quote_end if quote_end >= 0 else len(path_value))
            if quoted else start)
        end = len(path_value) if separator < 0 else separator
        directory = path_value[start:end]
        if directory[:1] in ('"', "'"):
            directory = directory[1:]
        if directory[-1:] in ('"', "'"):
            directory = directory[:-1]
        if len(directory) > 0:
            directories.append(directory)
        start = end + 1
    return directories


def windows_search_path_join(directory: str, name: str, cwd: str) -> str:
    """Join one search root and file name (verbatim prefix rules: UNC keeps
    no prefix, a leading slash borrows the cwd drive, a drive-relative root
    borrows the cwd drive only on the same drive, an absolute drive root
    keeps no prefix; the append helper adds one backslash when needed)."""
    prefix = cwd
    adjusted_directory = directory

    def slash(value: str) -> bool:
        return value in ("\\", "/")

    if len(directory) > 2 and slash(directory[0]) and slash(directory[1]):
        prefix = ""
    elif len(directory) >= 1 and slash(directory[0]):
        prefix = cwd[:2]
    elif (len(directory) >= 2 and directory[1] == ":"
          and (len(directory) < 3 or not slash(directory[2]))):
        if len(cwd) < 2 or cwd[:2].lower() != directory[:2].lower():
            prefix = ""
        else:
            adjusted_directory = directory[2:]
    elif len(directory) > 2 and len(directory) > 1 and directory[1] == ":":
        prefix = ""

    def append(base: str, part: str) -> str:
        if len(base) == 0 or len(part) == 0:
            return base + part
        if base[-1] in "\\/:":
            return base + part
        return f"{base}\\{part}"

    return append(append(prefix, adjusted_directory), name)


def windows_executable_names(command: str, name: str) -> List[str]:
    """The candidate names searched for one Windows file name (verbatim:
    an extension keeps the command as-is first; a trailing dot needs no
    separator; ``.com``/``.exe`` variants always follow)."""
    dot = name.find(".")
    has_extension = 0 <= dot < len(name) - 1
    separator = "" if name.endswith(".") else "."
    names: List[str] = []
    if has_extension:
        names.append(command)
    names.append(f"{command}{separator}com")
    names.append(f"{command}{separator}exe")
    return names


def executable_candidate_exists(candidate: str) -> bool:
    """libuv's existence check: a stat hit must be a non-directory; on a
    stat failure an lstat hit must be a regular file or symlink."""
    try:
        status = os.stat(candidate)
        return not stat_module.S_ISDIR(status.st_mode)
    except OSError:
        pass
    try:
        status = os.lstat(candidate)
        return stat_module.S_ISREG(status.st_mode) or \
            stat_module.S_ISLNK(status.st_mode)
    except OSError:
        return False


def resolve_windows_executable(command: str, cwd: str,
                               env: Dict[str, Optional[str]],
                               exists: Callable[[str], bool] =
                               executable_candidate_exists,
                               current_env: Optional[Any] = None
                               ) -> Optional[str]:
    """Resolve the executable path with libuv/Node's Windows spawn search
    order, preserving the caller's original argv entry separately.

    :param command: original target argv[0].
    :param cwd: final target working directory for relative search roots.
    :param env: final target environment containing the child PATH.
    :param exists: injectable non-directory candidate probe.
    :param current_env: runner environment supplying the PATH fallback and
        the cwd-search policy.
    :returns: a resolved application name, or None when no candidate exists.
    """
    current = cast(Dict[str, Optional[str]],
                   current_env if current_env is not None else os.environ)
    name_start = windows_file_name_start(command)
    directory = command[:name_start]
    name = command[name_start:]
    has_path = name_start != 0
    roots: List[str] = []
    if has_path:
        roots.append(directory)
    else:
        if windows_environment_value(
                current, "NODEFAULTCURRENTDIRECTORYINEXEPATH") is None:
            roots.append("")
        path = windows_environment_value(env, "PATH")
        if path is None:
            path = windows_environment_value(current, "PATH")
        roots.extend(windows_path_directories(path or ""))
    for root in roots:
        base = windows_search_path_join(root, name, cwd)
        for candidate in windows_executable_names(base, name):
            if exists(candidate):
                return candidate
    return None


# -------------------------------------------------------- error records

def serialize_runner_error(error: Any) -> Dict[str, Any]:
    """Convert an unknown failure into the bounded cross-process error
    record (name/message/code/syscall/path)."""
    record: Dict[str, Any] = {
        "name": type(error).__name__,
        "message": str(error),
    }
    code = getattr(error, "code", None)
    if isinstance(code, str):
        record["code"] = code
    syscall = getattr(error, "syscall", None)
    if isinstance(syscall, str):
        record["syscall"] = syscall
    path = getattr(error, "path", None)
    if isinstance(path, str):
        record["path"] = path
    return record


def deserialize_runner_error(serialized: Dict[str, Any]) -> Exception:
    """Rebuild a Node-shaped error from a strict runner record."""
    error = RuntimeError(serialized.get("message", ""))
    error.name = serialized.get("name", "Error")  # type: ignore[attr-defined]
    for key in ("code", "syscall", "path"):
        if serialized.get(key) is not None:
            setattr(error, key, serialized[key])
    return error


def node_spawn_error(syscall: str, code: str,
                     path: Optional[str] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {"name": "Error", "message": f"{syscall} {code}",
                              "code": code, "syscall": syscall}
    if path is not None:
        record["path"] = path
    return record


def _errno_to_code(error: OSError) -> Optional[str]:
    mapping = {errno_module.ENOENT: "ENOENT", errno_module.EACCES: "EACCES",
               errno_module.EPERM: "EPERM"}
    if error.errno in mapping:
        return mapping[error.errno]
    if sys.platform == "win32" and error.winerror in WINDOWS_SPAWN_ERROR_CODES:
        return WINDOWS_SPAWN_ERROR_CODES[error.winerror]
    return None


def as_spawn_error(error: Any, program: str) -> Dict[str, Any]:
    """Normalize one launch failure into the bounded Node-shaped record."""
    serialized = serialize_runner_error(error)
    if not isinstance(error, win32_process.Win32Error):
        if serialized.get("code") is None and isinstance(error, OSError):
            code = _errno_to_code(error)
            if code is not None:
                return node_spawn_error(f"spawn {program}", code, program)
        return serialized
    code = WINDOWS_SPAWN_ERROR_CODES.get(error.win32_code, "UNKNOWN")
    if code in NODE_SPAWN_DETAIL_CODES:
        return node_spawn_error(f"spawn {program}", code, program)
    return node_spawn_error("spawn", code)


def windows_path_not_found_error(program: str) -> Dict[str, Any]:
    return node_spawn_error(f"spawn {program}", "ENOENT", program)


def windows_start_cancelled_error() -> Dict[str, Any]:
    return {"name": "Error",
            "message": "subprocess target start was cancelled"}


# ------------------------------------------------------------- taskkill

def taskkill_process_tree(pid: Optional[int]) -> None:
    """Terminate one Windows process tree with ``taskkill /T /F``.

    Contained like POSIX group signalling — delivery races tree exit, so an
    absent tree, a nonzero status, or a missing taskkill binary must not
    break idempotent teardown.
    """
    if pid is None or pid <= 0:
        return
    try:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE, exactly like Node windowsHide
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, startupinfo=startup,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW",
                                             0),
                       timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # a failed tree kill must never break idempotent teardown


#: Kept as the injectable seam name used by owners and tests
#: (taskkill_tree(pid, force) lives with the inspector internals; this
#: always-force form is the fallbackOwner signalTree seam).
taskkill_process_tree_always = taskkill_process_tree


def signal_tree(platform: str, pid: Optional[int], signal_name: str,
                child: Any, taskkill: Callable[[int], None]) -> None:
    """Signal a detached process tree with platform-correct semantics.

    POSIX signals the negative process-group id and falls back to the
    direct child when the group is gone; Windows terminates the tree via
    taskkill (any signal value force-terminates — Node maps signals to
    TerminateProcess).
    """
    if pid is None:
        return
    if platform == "win32":
        taskkill(pid)
        return
    import signal as signal_module
    number = _SIGNAL_NUMBERS.get(signal_name)
    if number is None:
        number = getattr(signal_module, signal_name)
    try:
        os.kill(-pid, number)
    except OSError:
        try:
            os.kill(pid, number)
        except OSError:
            pass


#: POSIX signal numbers used on hosts whose signal module lacks the names
#: (win32 test runs exercise the Linux scope state machine).
_SIGNAL_NUMBERS: Dict[str, int] = {"SIGTERM": 15, "SIGKILL": 9}


# ------------------------------------------------------- linux /proc plane

def _os_pread(fd: int, length: int, offset: int) -> bytes:
    """os.pread where available (posix); the Windows analysis branch never
    executes this because the Linux inspection plane is posix-only."""
    pread = getattr(os, "pread", None)
    if pread is None:
        raise OSError("pread is unavailable on this platform")
    return pread(fd, length, offset)  # type: ignore[no-any-return]


def _kill_with_name(pid: int, signal_name: str) -> None:
    """os.kill over a signal NAME (the inspector kill seam shape)."""
    number = _SIGNAL_NUMBERS.get(signal_name)
    if number is None:
        import signal as signal_module
        number = getattr(signal_module, signal_name)
    os.kill(pid, number)


def _exec_capture(file: str, args: Sequence[str]) -> str:
    """execFileSync-equivalent: utf8 stdout, non-zero exits raise."""
    completed = subprocess.run([file, *args], capture_output=True,
                               text=True, check=True)
    return completed.stdout


DEFAULT_INTERNALS: Dict[str, Any] = {
    "readFile": lambda path: Path(path).read_text(encoding="utf-8",
                                                   errors="replace"),
    "readDir": lambda path: os.listdir(path),
    "stat": os.stat,
    "readLink": os.readlink,
    "open": lambda path: os.open(path, os.O_RDONLY),
    "read": _os_pread,
    "close": os.close,
    "exec": _exec_capture,
    "kill": _kill_with_name,
}

#: Syscall numbers whose blocking form can wait on stdin (verbatim table).
SYSCALLS: Dict[str, Dict[str, int]] = {
    "x64": {"read": 0, "select": 23, "pselect": 270, "poll": 7,
            "ppoll": 271, "epollWait": 232, "epollPwait": 281},
    "arm64": {"read": 63, "pselect": 72, "ppoll": 73, "epollPwait": 22},
}

#: Every supported table participates in matching after the primary one.
SUPPORTED_SYSCALL_TABLES: List[Dict[str, int]] = list(SYSCALLS.values())


def linux_syscall_tables(arch: str) -> Optional[List[Dict[str, int]]]:
    """Primary table for the arch first, then every other supported table
    (verbatim linuxSyscallTables)."""
    primary = SYSCALLS.get(arch)
    if primary is None:
        return None
    return [primary, *[table for table in SUPPORTED_SYSCALL_TABLES
                       if table is not primary]]


def parse_proc_stat(text: str) -> Optional[Dict[str, Any]]:
    """Parse /proc/pid/stat around the comm parentheses (verbatim field
    positions and validation)."""
    open_index = text.find("(")
    close = text.rfind(")")
    if open_index <= 0 or close <= open_index:
        return None
    pid = int(text[:open_index].strip())
    rest = text[close + 2:].strip().split()
    # the upstream validation requires the fields through starttime
    # (position 19 in the rest array); anything shorter is not a stat row
    if len(rest) < 20:
        return None
    state = rest[0]
    parent_pid = int(rest[1])
    pgrp = int(rest[2])
    session = int(rest[3])
    tty_device = int(rest[4])
    tpgid = int(rest[5])
    started = rest[19]
    if len(state) != 1:
        return None
    return {"pid": pid, "parentPid": parent_pid, "pgrp": pgrp,
            "session": session, "state": state, "ttyDevice": tty_device,
            "tpgid": tpgid, "started": started}


def read_linux_stat(internals: Dict[str, Any], pid: int
                    ) -> Optional[Dict[str, Any]]:
    try:
        return parse_proc_stat(internals["readFile"](f"/proc/{pid}/stat"))
    except (OSError, ValueError, IndexError):
        return None


def linux_device_number(value: int) -> int:
    return value & 0xFFFFFFFF


def read_linux_terminal_device(internals: Dict[str, Any], pid: int,
                               tty_device: int, tid: Optional[int] = None
                               ) -> Optional[int]:
    terminal_device = linux_device_number(tty_device)
    if terminal_device == 0:
        return None
    path = (f"/proc/{pid}/fd/0" if tid is None
            else f"/proc/{pid}/task/{tid}/fd/0")
    try:
        if internals["readLink"](path) == "/dev/tty":
            return terminal_device
        status = internals["stat"](path)
        if stat_module.S_ISCHR(status.st_mode) and \
                linux_device_number(status.st_rdev) == terminal_device:
            return terminal_device
        return None
    except OSError:
        return None


def numeric_entries(internals: Dict[str, Any], path: str
                    ) -> Optional[List[int]]:
    try:
        return [int(entry) for entry in internals["readDir"](path)
                if entry.isdigit()]
    except OSError:
        return None


def linux_process_group_has_live_members(process_group_id: int,
                                         internals: Optional[Dict[str, Any]] = None
                                         ) -> Optional[bool]:
    """Whether a Linux process group has an executing member.

    False means only zombie/dead entries; None means the process table
    could not prove either outcome.
    """
    api = internals or DEFAULT_INTERNALS
    entries = numeric_entries(api, "/proc")
    if entries is None:
        return None
    matched = False
    for pid in entries:
        stat_row = read_linux_stat(api, pid)
        if stat_row is None or stat_row["pgrp"] != process_group_id:
            continue
        matched = True
        if stat_row["state"] not in ("Z", "X", "x"):
            return True
    return False if matched else None


def read_syscall(internals: Dict[str, Any], pid: int, tid: int
                 ) -> Optional[Dict[str, Any]]:
    try:
        text = internals["readFile"](
            f"/proc/{pid}/task/{tid}/syscall").strip()
        if text == "running" or text.startswith("-1 "):
            return None
        fields = text.split()
        number = int(fields[0])
        args = [int(field, 16) for field in fields[1:7]]
        if len(args) != 6:
            return None
        return {"number": number, "args": args}
    except (OSError, ValueError, IndexError):
        return None


def read_memory(internals: Dict[str, Any], pid: int, address: int,
                length: int) -> Optional[bytes]:
    fd = None
    try:
        fd = internals["open"](f"/proc/{pid}/mem")
        return internals["read"](fd, length, address)
    except OSError:
        return None
    finally:
        if fd is not None:
            internals["close"](fd)


def fd_set_has_stdin(internals: Dict[str, Any], pid: int, address: int) -> bool:
    if address == 0:
        return False
    memory = read_memory(internals, pid, address, 8)
    if not memory:
        return False
    return memory[0] % 2 == 1


def poll_has_stdin(internals: Dict[str, Any], pid: int, address: int,
                   count: int) -> bool:
    if address == 0 or count <= 0:
        return False
    memory = read_memory(internals, pid, address, min(count, 1024) * 8)
    if memory is None:
        return False
    for offset in range(0, len(memory) - 7, 8):
        fd = int.from_bytes(memory[offset:offset + 4], "little", signed=True)
        events = int.from_bytes(memory[offset + 4:offset + 6], "little",
                                signed=True)
        if fd == 0 and (events & 1) != 0:
            return True
    return False


def epoll_has_stdin(internals: Dict[str, Any], pid: int, tid: int,
                    epfd: int) -> bool:
    try:
        text = internals["readFile"](
            f"/proc/{pid}/task/{tid}/fdinfo/{epfd}")
        return any(line.strip().startswith("tfd:") and
                   re.match(r"^tfd:\s+0\b", line.strip())
                   for line in text.split("\n"))
    except OSError:
        return False


def syscall_waits_on_stdin(internals: Dict[str, Any], pid: int, tid: int,
                           syscall: Dict[str, Any],
                           tables: Sequence[Dict[str, int]]) -> bool:
    """Decide whether one thread blocks in a stdin-capable syscall
    (verbatim syscallWaitsOnStdin: the first table match across the
    ordered tables decides; an unmatched number is NOT waiting)."""
    args = list(syscall.get("args") or [])
    a0 = args[0] if len(args) > 0 else 0
    a1 = args[1] if len(args) > 1 else 0
    a2 = args[2] if len(args) > 2 else 0
    number = syscall["number"]
    for table in tables:
        if number == table.get("read"):
            return a0 == 0
        if number in (table.get("select"), table.get("pselect")):
            return a0 >= 1 and fd_set_has_stdin(internals, pid, a1)
        if number in (table.get("poll"), table.get("ppoll")):
            return a1 >= 1 and poll_has_stdin(internals, pid, a0, a1)
        if number in (table.get("epollWait"), table.get("epollPwait")):
            return a2 >= 1 and epoll_has_stdin(internals, pid, tid, a0)
    return False


# ------------------------------------------------------------- inspectors

def quiescent(state: Optional[str]) -> bool:
    """Zombie/dead process states (verbatim /^[ZXx]$/)."""
    return state is not None and state in ("Z", "X", "x")


class PosixProcessSnapshot:
    """One consistent process-table observation.

    Rows carry {pid, parentPid, started, session, state}; ``complete``
    reports whether every enumerated entry was readable. Identities are
    plain {"pid", "started"} mappings.
    """

    def __init__(self, rows: List[Dict[str, Any]], complete: bool) -> None:
        self.rows = rows
        self.complete = complete
        self.by_pid: Dict[int, Dict[str, Any]] = {
            row["pid"]: row for row in rows}

    def tree(self, root_pid: int) -> List[Dict[str, Any]]:
        return process_tree(self.rows, root_pid)

    def session(self, session_id: int) -> List[Dict[str, Any]]:
        return [{"pid": row["pid"], "started": row["started"]}
                for row in self.rows if row.get("session") == session_id]

    def alive(self, identity: Dict[str, Any]) -> bool:
        row = self.by_pid.get(identity["pid"])
        return (row is not None and row["started"] == identity["started"]
                and not quiescent(row["state"]))


def process_tree(entries: List[Dict[str, Any]], root_pid: int
                 ) -> List[Dict[str, Any]]:
    """Walk the table from one root children-first (verbatim processTree:
    every row joins as a {pid, started} member, the root lands last, and
    an absent root yields an empty list)."""
    by_id = {entry["pid"]: entry for entry in entries}
    root = by_id.get(root_pid)
    if root is None:
        return []
    by_parent: Dict[int, List[Dict[str, Any]]] = {}
    for entry in entries:
        by_parent.setdefault(entry["parentPid"], []).append(entry)
    visited: Set[int] = set()
    result: List[Dict[str, Any]] = []

    def visit(entry: Dict[str, Any]) -> None:
        if entry["pid"] in visited:
            return
        visited.add(entry["pid"])
        for child in by_parent.get(entry["pid"], []):
            visit(child)
        result.append({"pid": entry["pid"], "started": entry["started"]})

    visit(root)
    return result


class PosixProcessInspector:
    """Shared POSIX signalling: group-first with an identity-fenced
    single-process form (verbatim PosixProcessInspector).

    The kill seam receives (pid, signal-name string); the default
    internals translate through the platform signal numbers.
    """

    def __init__(self, internals: Dict[str, Any]) -> None:
        self.internals = internals

    def signalGroup(self, pgid: int, signal_name: str) -> None:
        self.internals["kill"](-pgid, signal_name)

    def signalProcess(self, identity: Dict[str, Any],
                      signal_name: str) -> None:
        if self.isAlive(identity):
            self.internals["kill"](identity["pid"], signal_name)

    def isAlive(self, identity: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def snapshot(self) -> PosixProcessSnapshot:
        raise NotImplementedError


class LinuxProcessInspector(PosixProcessInspector):
    """Linux /proc process-table operations (verbatim inspector face:
    foreground pgid, the stdin-wait detector over every group member's
    threads, identity-fenced liveness, and the completeness-reporting
    snapshot)."""

    def __init__(self, arch: str, internals: Dict[str, Any]) -> None:
        super().__init__(internals)
        self.arch = arch

    def foregroundPgid(self, shell_pid: int) -> Optional[int]:
        stat_row = read_linux_stat(self.internals, shell_pid)
        if stat_row is None:
            return None
        tpgid = stat_row["tpgid"]
        return tpgid if tpgid > 0 else None

    def isStdinWaiting(self, pgid: int, shell_pid: int) -> bool:
        tables = linux_syscall_tables(self.arch)
        if tables is None:
            return False
        shell = read_linux_stat(self.internals, shell_pid)
        if shell is None:
            return False
        terminal_device = read_linux_terminal_device(
            self.internals, shell_pid, shell["ttyDevice"])
        if terminal_device is None:
            return False
        for pid in numeric_entries(self.internals, "/proc") or []:
            process = read_linux_stat(self.internals, pid)
            if process is None or process["pgrp"] != pgid:
                continue
            for tid in numeric_entries(
                    self.internals, f"/proc/{pid}/task") or []:
                syscall = read_syscall(self.internals, pid, tid)
                if syscall is not None \
                        and syscall_waits_on_stdin(self.internals, pid, tid,
                                                   syscall, tables) \
                        and read_linux_terminal_device(
                            self.internals, pid, process["ttyDevice"],
                            tid) == terminal_device:
                    return True
        return False

    def isAlive(self, identity: Dict[str, Any]) -> bool:
        stat_row = read_linux_stat(self.internals, identity["pid"])
        return (stat_row is not None
                and stat_row["started"] == identity["started"]
                and not quiescent(stat_row["state"]))

    def snapshot(self) -> PosixProcessSnapshot:
        pids = numeric_entries(self.internals, "/proc")
        if pids is None:
            raise RuntimeError(
                "Cannot inspect processes: /proc directory is unreadable")
        complete = True
        rows: List[Dict[str, Any]] = []
        for pid in pids:
            stat_row = read_linux_stat(self.internals, pid)
            if stat_row is None:
                complete = False
                continue
            rows.append({"pid": pid, "parentPid": stat_row["parentPid"],
                         "started": stat_row["started"],
                         "session": stat_row["session"],
                         "state": stat_row["state"]})
        return PosixProcessSnapshot(rows, complete)


def mac_process_table(internals: Dict[str, Any]) -> Dict[str, Any]:
    """Parse `ps -axo pid=,ppid=,lstart=` into rows + completeness
    (verbatim macProcessTable: unmatched non-blank lines mark the table
    incomplete; session/state stay unknown on macOS)."""
    complete = True
    rows: List[Dict[str, Any]] = []
    text = internals["exec"]("/bin/ps", ["-axo", "pid=,ppid=,lstart="])
    for line in text.split("\n"):
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$", line)
        if match is None:
            if line.strip():
                complete = False
            continue
        rows.append({"pid": int(match.group(1)),
                     "parentPid": int(match.group(2)),
                     "started": match.group(3),
                     "session": None, "state": None})
    return {"rows": rows, "complete": complete}


class MacProcessInspector(PosixProcessInspector):
    """macOS variant: ps-based table, tpgid via ps, no stdin detector
    (verbatim MacProcessInspector)."""

    def foregroundPgid(self, shell_pid: int) -> Optional[int]:
        try:
            text = self.internals["exec"](
                "/bin/ps", ["-o", "tpgid=", "-p", str(shell_pid)])
            value = int(text.strip())
            return value if value > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def isStdinWaiting(self, pgid: int, shell_pid: int) -> bool:
        return False

    def isAlive(self, identity: Dict[str, Any]) -> bool:
        rows = mac_process_table(self.internals)["rows"]
        return any(row["pid"] == identity["pid"]
                   and row["started"] == identity["started"]
                   for row in rows)

    def snapshot(self) -> PosixProcessSnapshot:
        table = mac_process_table(self.internals)
        return PosixProcessSnapshot(table["rows"], table["complete"])


class WindowsProcessObservation:
    """Windows snapshot observation: an identity-fenced tree over one
    Toolhelp32 table read, an empty session face (Windows has no POSIX
    sessions), and the handle-state liveness check."""

    def __init__(self, inspector: "WindowsProcessInspector") -> None:
        self._inspector = inspector
        self._entries: Optional[List[Dict[str, Any]]] = None

    def _table(self) -> List[Dict[str, Any]]:
        if self._entries is None:
            self._entries = self._inspector.internals["snapshot"]()
        return self._entries

    def tree(self, root_pid: int) -> List[Dict[str, Any]]:
        def started(pid: int) -> Optional[str]:
            state = self._inspector.internals["processState"](pid)
            return state["started"] if state is not None else None

        return windows_process_tree(self._table(), root_pid, started)

    def session(self, session_id: int) -> List[Dict[str, Any]]:
        return []

    def alive(self, identity: Dict[str, Any]) -> bool:
        return self._inspector.isAlive(identity)


class WindowsProcessInspector:
    """Windows process-table operations for terminal readiness,
    signalling, and teardown: Toolhelp32 snapshot enumeration with
    GetProcessTimes creation-time identity and process-handle wait-state
    liveness, the shell pid as a pseudo process group (Windows has no
    POSIX groups), and taskkill tree signalling. All decision logic takes
    an injectable internals boundary (verbatim WindowsProcessInspector)."""

    def __init__(self, internals: Optional[Dict[str, Any]] = None) -> None:
        self.internals = (internals if internals is not None
                          else default_windows_process_internals())

    def foregroundPgid(self, shell_pid: int) -> int:
        return shell_pid

    def isStdinWaiting(self, pgid: int, shell_pid: int) -> bool:
        return False

    def isAlive(self, identity: Dict[str, Any]) -> bool:
        state = self.internals["processState"](identity["pid"])
        return (state is not None and state.get("active") is True
                and state["started"] == identity["started"])

    def snapshot(self) -> WindowsProcessObservation:
        return WindowsProcessObservation(self)

    def signalGroup(self, pgid: int, signal_name: str) -> None:
        self.internals["taskkill"](pgid, signal_name == "SIGKILL")

    def signalProcess(self, identity: Dict[str, Any],
                      signal_name: str) -> None:
        if self.isAlive(identity):
            self.internals["taskkill"](identity["pid"],
                                       signal_name == "SIGKILL")


def taskkill_tree(pid: Optional[int], force: bool) -> None:
    """Terminate one Windows process tree with taskkill, contained like
    POSIX group signalling (verbatim taskkillTree: /T always, /F only
    when forced; spawn failures are swallowed by the caller contract)."""
    if pid is None or pid <= 0:
        return
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        subprocess.run(args, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       startupinfo=startup, creationflags=creationflags,
                       check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def default_windows_process_internals() -> Dict[str, Any]:
    """The real ctypes process-table boundary (the koffi default's port;
    lazy so non-Windows hosts never touch Win32 libraries)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cCntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    assert ctypes.sizeof(PROCESSENTRY32W) == 568  # upstream koffi check
    assert ctypes.sizeof(FILETIME) == 8

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.restype = wintypes.HANDLE
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    first = kernel32.Process32FirstW
    first.restype = wintypes.BOOL
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    next_ = kernel32.Process32NextW
    next_.restype = wintypes.BOOL
    next_.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    open_process = kernel32.OpenProcess
    open_process.restype = wintypes.HANDLE
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    get_times = kernel32.GetProcessTimes
    get_times.restype = wintypes.BOOL
    get_times.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
                          ctypes.POINTER(FILETIME),
                          ctypes.POINTER(FILETIME),
                          ctypes.POINTER(FILETIME)]
    wait_single = kernel32.WaitForSingleObject
    wait_single.restype = wintypes.DWORD
    wait_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    close = kernel32.CloseHandle
    close.restype = wintypes.BOOL
    close.argtypes = [wintypes.HANDLE]

    def snapshot() -> List[Dict[str, Any]]:
        handle = create_snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if handle is None or handle == win32_process.INVALID_HANDLE:
            raise win32_process.Win32Error(
                "CreateToolhelp32Snapshot", int(ctypes.get_last_error()))
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            rows: List[Dict[str, Any]] = []
            if not first(handle, ctypes.byref(entry)):
                return rows
            while True:
                rows.append({"pid": int(entry.th32ProcessID),
                             "parentPid": int(entry.th32ParentProcessID)})
                if not next_(handle, ctypes.byref(entry)):
                    return rows
        finally:
            close(handle)

    def process_state(pid: int) -> Optional[Dict[str, Any]]:
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE (upstream 1052672)
        handle = open_process(0x1000 | 0x100000, False, pid)
        if handle is None or handle == 0:
            return None
        try:
            creation = FILETIME()
            exit_time = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            if not get_times(handle, ctypes.byref(creation),
                             ctypes.byref(exit_time),
                             ctypes.byref(kernel_time),
                             ctypes.byref(user_time)):
                return None
            wait = wait_single(handle, 0)
            if wait not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
                return None
            started = (f"{creation.dwHighDateTime}:"
                       f"{creation.dwLowDateTime}")
            return {"started": started, "active": wait == 258}
        finally:
            close(handle)

    return {"snapshot": snapshot, "processState": process_state,
            "taskkill": taskkill_tree}


def windows_process_tree(entries: List[Dict[str, Any]], root_pid: int,
                         started: Callable[[int], Optional[str]]
                         ) -> List[Dict[str, Any]]:
    """Walk a process table from one root in children-first order,
    retaining only members whose start identity is readable (unreadable
    members are detector misses; their descendants are still walked).
    Verbatim windowsProcessTree."""
    by_id = {entry["pid"]: entry for entry in entries}
    root = by_id.get(root_pid)
    if root is None:
        return []
    by_parent: Dict[int, List[Dict[str, Any]]] = {}
    for entry in entries:
        by_parent.setdefault(entry["parentPid"], []).append(entry)
    visited: Set[int] = set()
    result: List[Dict[str, Any]] = []

    def visit(entry: Dict[str, Any]) -> None:
        if entry["pid"] in visited:
            return
        visited.add(entry["pid"])
        for child in by_parent.get(entry["pid"], []):
            visit(child)
        identity = started(entry["pid"])
        if identity is not None:
            result.append({"pid": entry["pid"], "started": identity})

    visit(root)
    return result


def create_windows_process_inspector(
        internals: Optional[Dict[str, Any]] = None
) -> WindowsProcessInspector:
    return WindowsProcessInspector(internals)


def create_process_inspector(platform: Optional[str] = None,
                             arch: Optional[str] = None,
                             internals: Optional[Dict[str, Any]] = None
                             ) -> Any:
    """Create the platform process inspector (verbatim dispatch; the
    win32 branch does NOT forward internals upstream — its default is the
    real Win32 table)."""
    active = platform if platform is not None else sys.platform
    api = internals if internals is not None else DEFAULT_INTERNALS
    active_arch = arch if arch is not None else _host_arch()
    if active == "linux":
        return LinuxProcessInspector(active_arch, api)
    if active == "darwin":
        return MacProcessInspector(api)
    if active == "win32":
        return create_windows_process_inspector()
    raise RuntimeError(
        "subprocess-local: terminal inspection is unsupported on "
        f"platform {active}")


def _host_arch() -> str:
    """Node process.arch-equivalent for the syscall table selection."""
    import platform as platform_module
    machine = platform_module.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x64"
    return machine


# ------------------------------------------------------- fallback launch

def spawn_subprocess(program: str, args: Sequence[str], cwd: str,
                     env: Dict[str, str], stdio: List[Any],
                     platform: Optional[str] = None,
                     internals: Optional[Dict[str, Any]] = None) -> Any:
    """One platform spawn with hidden initial windows (verbatim Node
    options: detached only off-win32; windowsHide only on win32).

    The stdio list maps "ignore"→DEVNULL, "pipe"→PIPE, "inherit"→None.
    """
    api = internals or {}
    active = platform or sys.platform
    popen = api.get("spawn") or subprocess.Popen
    kwargs: Dict[str, Any] = {
        "cwd": cwd, "env": env, "stdin": stdio[0], "stdout": stdio[1],
        "stderr": stdio[2],
    }
    if active == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE — Node windowsHide semantics
        kwargs["startupinfo"] = startup
        kwargs["close_fds"] = False
    else:
        kwargs["start_new_session"] = True  # detached: platform !== win32
        kwargs["close_fds"] = True
    return popen([program, *args], **kwargs)


def _map_stdio(mode: Any) -> Any:
    """Map one spec stdio mode to its Popen constant (verbatim
    spawnSubprocess rules: stdin ignore else pipe; out/err inherit else
    pipe; collect objects read through a parent pipe)."""
    if mode == "ignore":
        return subprocess.DEVNULL
    if mode == "inherit":
        return None
    return subprocess.PIPE


def fallback_owner(platform: str, pid: Optional[int], child: Any,
                   taskkill: Callable[[int], None],
                   linux_group_has_live_members: Callable[[int], Optional[bool]],
                   direct: threading.Event) -> Dict[str, Any]:
    """Fallback managed-range owner (verbatim fallbackOwner): tree
    signalling plus a 15 ms liveness poll whose timer keeps running until
    the tree really exits.

    ``direct`` is the Event analogue of the settled direct-outcome promise.
    """
    lock = threading.RLock()
    state: Dict[str, Any] = {"stopped": False, "observation": None}

    def alive() -> bool:
        if state["stopped"] or pid is None:
            return False
        if platform == "win32":
            return child.poll() is None
        try:
            os.kill(-pid, 0)
            if direct.is_set() and platform == "linux" and \
                    linux_group_has_live_members(pid) is False:
                return False
            return True
        except OSError as error:
            if error.errno == errno_module.ESRCH:
                return False
            if error.errno == errno_module.EPERM:
                return True
            return child.poll() is None

    def signal(signal_name: str) -> None:
        if not alive():
            state["stopped"] = True
            return
        signal_tree(platform, pid, signal_name, child, taskkill)

    def wait_for_exit() -> None:
        with lock:
            if state["stopped"]:
                return
            if state["observation"] is None:
                def observe() -> None:
                    while alive():
                        time.sleep(0.015)
                    with lock:
                        state["stopped"] = True
                thread = threading.Thread(target=observe, daemon=True)
                state["observation"] = thread
                thread.start()
            observation = state["observation"]
        observation.join()

    def terminate_for_host_exit() -> None:
        if state["stopped"]:
            return
        signal_tree(platform, pid, "SIGKILL", child, taskkill)

    return {"signal": signal, "waitForExit": wait_for_exit,
            "terminateForHostExit": terminate_for_host_exit,
            "alive": alive}


# ------------------------------------------------- managed process binding

class ManagedProcessHandle:
    """Live handle over one managed process range (the bindManagedProcess
    result): pipe-mode stream exposure, collector objects, the done
    outcome, the termination ladder, and the range-exit observer."""

    def __init__(self, spec: Dict[str, Any], launch: Dict[str, Any],
                 internals: Optional[Dict[str, Any]] = None) -> None:
        supplied = internals or {}
        binding = prepare_managed_process_binding(supplied)
        spill_dir = binding["spillDir"]
        on_spill_failure = binding["onSpillFailure"]
        self.spec = spec
        self._state_lock = threading.RLock()
        self._outcome_resolver = launch.get("resolveOutcome")
        self._range_cleanup = launch.get("cleanup")
        self._range_error: Optional[BaseException] = None
        stdio = spec.get("stdio") or {}
        stdin_mode = stdio.get("stdin")
        out_mode = stdio.get("stdout")
        err_mode = stdio.get("stderr")
        self._child = launch.get("child")
        self._owner = launch["owner"]
        self._direct_event: threading.Event = launch["directEvent"]
        self.control = launch.get("control")
        #: the direct child's pid (upstream handle.pid)
        self.pid: Optional[int] = launch.get("pid")
        self._reader_threads: List[threading.Thread] = []
        self._collectors: Dict[str, OutputCollector] = {}
        for label, mode, stream in (("stdout", out_mode, launch.get("stdout")),
                                    ("stderr", err_mode, launch.get("stderr"))):
            if not isinstance(mode, dict) or stream is None:
                continue
            spill = None
            if mode.get("spill") is not None:
                spill = {"maxBytes": mode["spill"]["maxBytes"],
                         "dir": spill_dir, "onFailure": on_spill_failure}
            collector = OutputCollector(mode["maxBytes"], label, spill)
            self._collectors[label] = collector
            thread = threading.Thread(target=self._pump,
                                      args=(stream, collector),
                                      name=f"subprocess-{label}-reader",
                                      daemon=True)
            thread.start()
            self._reader_threads.append(thread)
        #: collector objects keyed by stream (upstream handle.collected)
        self.collected: Dict[str, OutputCollector] = dict(self._collectors)
        # pipe-mode streams are exposed raw; every other mode hides them
        stdin_stream = launch.get("stdin")
        self.stdin = stdin_stream if stdin_mode == "pipe" else None
        self.stdout = (launch.get("stdout") if out_mode == "pipe" else None)
        self.stderr = (launch.get("stderr") if err_mode == "pipe" else None)
        # a {data} stdin writes its payload immediately and ends (verbatim
        # stdin.end(stdinMode.data))
        if isinstance(stdin_mode, dict) and stdin_stream is not None:
            try:
                stdin_stream.write(stdin_mode.get("data", b""))
                stdin_stream.close()
            except (OSError, ValueError):
                pass
        self._settled = False
        self._termination_started = False
        self._range_exit_observed = False
        self._grace_timer: Optional[threading.Timer] = None
        self._range_observation: Optional[threading.Thread] = None
        self.done_event = threading.Event()
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[BaseException] = None
        signal = spec.get("signal")
        self._signal = signal
        if signal is not None:
            self._on_abort = lambda: self.terminate_with_reason(
                signal.reason)
            signal.add_listener(self._on_abort)
        self._exit_thread = threading.Thread(target=self._wait_direct,
                                             name="subprocess-exit",
                                             daemon=True)
        self._exit_thread.start()

    # -- outcome plumbing -------------------------------------------------

    def _wait_direct(self) -> None:
        child = self._child
        try:
            returncode = child.wait() if child is not None else None
        except BaseException as error:
            self._fail(error)
            return
        signal_name: Optional[str] = None
        if returncode is not None and returncode < 0:
            signal_name = f"SIG{-returncode}"
            returncode = None
        self._direct_event.set()
        try:
            outcome = {"exitCode": returncode, "signal": signal_name}
            if self._outcome_resolver is not None:
                outcome = self._outcome_resolver(outcome)
            self._settle(outcome)
        except BaseException as error:
            self._fail(error)

    def _settle(self, outcome: Dict[str, Any]) -> None:
        with self._state_lock:
            if self._settled:
                return
            self._settled = True
        if self._drain_needed():
            # the direct outcome arrived first: bound the pipe drain by
            # graceMs, then settle on stream EOF or the timer, whichever
            # comes first (verbatim pipeDrainTimer race)
            timer = threading.Timer(
                self.spec.get("graceMs", 5000) / 1000,
                self._finish_settle, args=(outcome, True))
            timer.daemon = True
            self._drain_timer = timer
            timer.start()
            drain = threading.Thread(target=self._drain_streams,
                                     args=(outcome,), daemon=True)
            drain.start()
            return
        self._finish_settle(outcome)

    def _drain_needed(self) -> bool:
        return len(self._reader_threads) > 0

    def _drain_streams(self, outcome: Dict[str, Any]) -> None:
        for thread in self._reader_threads:
            thread.join()
        self._finish_settle(outcome)

    def _finish_settle(self, outcome: Dict[str, Any],
                       drain_timed_out: bool = False) -> None:
        with self._state_lock:
            if self.done_event.is_set():
                return
            drain = getattr(self, "_drain_timer", None)
            if drain is not None:
                drain.cancel()
            self._stop_collectors()
            self.result = dict(outcome)
            if drain_timed_out:
                self.result["drainTimedOut"] = True
            self.done_event.set()

    def _fail(self, error: BaseException) -> None:
        with self._state_lock:
            if self.done_event.is_set():
                return
            self._settled = True
            self._direct_event.set()
            self._stop_collectors()
            self.error = error
            self.done_event.set()
        self.terminate()

    def _stop_collectors(self) -> None:
        for collector in self._collectors.values():
            collector.seal()

    def _pump(self, stream: Any, collector: OutputCollector) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                collector.push(chunk)
        except (OSError, ValueError):
            pass  # the stream died with the process; the tail stays readable

    # -- termination ladder -------------------------------------------------

    def _kill(self, signal_name: str) -> None:
        with self._state_lock:
            if self._range_exit_observed:
                return
            self._owner["signal"](signal_name)

    def terminate(self) -> None:
        self.terminate_with_reason(
            RuntimeError("subprocess terminated before target start"))

    def terminate_with_reason(self, reason: Optional[BaseException]) -> None:
        with self._state_lock:
            if self._range_exit_observed or self._termination_started:
                return
            self._termination_started = True
        # upstream ordering guarantee: the SIGTERM dispatch runs in the same
        # synchronous block that starts the range-exit observation, so the
        # signal always precedes the observation's settled continuation;
        # the owner's own alive() check keeps a dead range harmless
        self._kill("SIGTERM")
        self.observe_range_exit()
        grace_ms = self.spec.get("graceMs", 5000)
        self._grace_timer = threading.Timer(grace_ms / 1000,
                                            self._kill, args=("SIGKILL",))
        self._grace_timer.daemon = True
        with self._state_lock:
            if not self._range_exit_observed:
                self._grace_timer.start()

    def observe_range_exit(self) -> threading.Thread:
        """Share one observer; failed observation never proves range absence."""
        with self._state_lock:
            if self._range_observation is None:
                def observe() -> None:
                    try:
                        self._owner["waitForExit"]()
                        with self._state_lock:
                            self._range_exit_observed = True
                            if self._grace_timer is not None:
                                self._grace_timer.cancel()
                        if self._signal is not None:
                            self._signal.remove_listener(self._on_abort)
                        if self._range_cleanup is not None:
                            self._range_cleanup()
                    except BaseException as error:
                        with self._state_lock:
                            self._range_error = error
                thread = threading.Thread(target=observe, daemon=True)
                self._range_observation = thread
                thread.start()
            return self._range_observation

    def wait_for_exit(self) -> bool:
        observer = self.observe_range_exit()
        observer.join()
        with self._state_lock:
            if self._range_error is not None:
                raise self._range_error
            return self._range_exit_observed

    def terminate_for_host_exit(self) -> None:
        self._owner["terminateForHostExit"]()

    # -- convenience waits ---------------------------------------------------

    @property
    def done(self) -> threading.Event:
        return self.done_event

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        return self.done_event.wait(timeout)

    def finalize_collected(self) -> Dict[str, Any]:
        """Seal both collectors at settlement and return their final
        outputs (the consumer-side finalize of handle.collected)."""
        self.done_event.wait()
        return {label: collector.finalize()
                for label, collector in self._collectors.items()}

    def read_from(self, label: str, from_byte: int = 0) -> Dict[str, Any]:
        collector = self._collectors.get(label)
        if collector is None:
            raise KeyError(f"{label} is not collected")
        return collector.read_from(from_byte)

    def snapshot(self, label: str) -> Dict[str, Any]:
        collector = self._collectors.get(label)
        if collector is None:
            raise KeyError(f"{label} is not collected")
        return collector.snapshot()


def bind_managed_process(spec: Dict[str, Any], launch: Dict[str, Any],
                         internals: Optional[Dict[str, Any]] = None
                         ) -> ManagedProcessHandle:
    """Bind platform launch facts to the stdio, outcome, abort, and
    termination lifecycle."""
    return ManagedProcessHandle(spec, launch, internals)


def _fallback_launch(spec: Dict[str, Any],
                     internals: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """Spawn one detached PGID/taskkill fallback and expose its launch
    facts (verbatim spawnSubprocess: env built from childEnv + the control
    marker; stdio ignore/inherit/pipe mapping; fd-7 control vocabulary)."""
    api = internals or {}
    platform = api.get("platform") or spec.get("platform") or sys.platform
    stdio_spec = spec.get("stdio") or {}
    program = spec["argv"][0]
    args = spec["argv"][1:]
    env = control_environment(
        {key: value for key, value in child_env(spec.get("env")).items()
         if value is not None},
        stdio_spec.get("control"))
    if stdio_spec.get("control") == "pipe":
        raise RuntimeError(
            "control-pipe spawning requires the fd-7 carrier layout; this "
            "host's sandbox refuses CreatePipe, so the control channel "
            "plane is not loaded here (documented boundary)")
    stdin_mode = stdio_spec.get("stdin", "ignore")
    stdio = [_map_stdio("ignore" if stdin_mode == "ignore" else "pipe"),
             _map_stdio("inherit" if stdio_spec.get("stdout") == "inherit"
                        else "pipe"),
             _map_stdio("inherit" if stdio_spec.get("stderr") == "inherit"
                        else "pipe")]
    child = spawn_subprocess(program, args, spec["cwd"], env, stdio,
                             platform, api)
    direct_event = threading.Event()
    owner = fallback_owner(
        platform, child.pid, child,
        api.get("taskkill", taskkill_process_tree),
        api.get("linuxProcessGroupHasLiveMembers",
                lambda pid: linux_process_group_has_live_members(pid)),
        direct_event)
    stdin_stream = child.stdin if stdin_mode != "ignore" else None
    return {"child": child, "stdin": stdin_stream,
            "stdout": child.stdout, "stderr": child.stderr,
            "control": None, "directEvent": direct_event, "owner": owner,
            "pid": child.pid}


# --------------------------------------------- windows Job direct ownership

def probe_windows_job(internals: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the current token can create and release one unnamed
    kill-on-close Job right now.

    Direct-containment adaptation: upstream probes the Node runner
    invocation; on this host the Job primitives are owned in-process, so
    the probe is the real capability check (binding load + Job create).
    """
    try:
        api = (internals or {}).get("bindings") or \
            win32_process.load_win32_process_bindings()
        win32_process.probe_current_token_job_support(api)
        return True
    except (OSError, RuntimeError, win32_process.Win32Error):
        return False


class WindowsJobOwner:
    """Owns the unique kill-on-close Job handle of one Windows target.

    The runner's settle-before-exit discipline lives here: a zero-exit wait
    must first prove the managed range empty; terminate signals through
    TerminateJobObject; host exit sweeps through the same Job.
    """

    def __init__(self, job: int, process: int, api: Dict[str, Any],
                 internals: Optional[Dict[str, Any]] = None) -> None:
        supplied = internals or {}
        self.poll_process_exit = supplied.get(
            "pollProcessExit", win32_process.poll_process_exit)
        self.is_job_empty = supplied.get("isJobEmpty",
                                         win32_process.is_job_empty)
        self.terminate_job = supplied.get("terminateJob",
                                          win32_process.terminate_job)
        self.close_handle_checked = supplied.get(
            "closeHandleChecked", win32_process.close_handle_checked)
        self.api = api
        self.job = job
        self.process = process
        self.stopped = False
        self._lock = threading.Lock()
        self._exit_code: Optional[int] = None
        self._exit_event = threading.Event()

    def _poll_once(self) -> bool:
        """One 10 ms poll tick; True when settled."""
        code = self.poll_process_exit(self.api, self.process)
        if code is not None:
            with self._lock:
                self._exit_code = code
            return True
        return False

    def wait_target_exit(self) -> Optional[int]:
        """Wait for the direct target exit code (poll loop)."""
        while not self.stopped:
            if self._poll_once():
                break
            time.sleep(TERMINATE_POLL_INTERVAL_MS / 1000)
        with self._lock:
            return self._exit_code

    def wait_for_exit(self) -> None:
        """Before a zero-exit wait settles, prove the managed range empty:
        a live member would survive this owner and defeat the promise that
        disposal leaves nothing running."""
        if self.stopped:
            return
        while True:
            if self._poll_once():
                break
            if self.is_job_empty(self.api, self.job):
                break
            time.sleep(TERMINATE_POLL_INTERVAL_MS / 1000)
        self.stopped = True
        self.close_handle_checked(self.api, self.process,
                                  "direct Windows process")
        self.close_handle_checked(self.api, self.job, "managed Windows range")
        self._exit_event.set()

    def signal(self, signal_name: str) -> None:
        if signal_name not in ("SIGTERM", "SIGKILL"):
            raise ValueError(
                f"Windows managed processes do not support POSIX signal "
                f"{signal_name}")
        with self._lock:
            exit_code = self._exit_code
        self.terminate_job(self.api, self.job, 1 if exit_code is None
                           else exit_code)

    def terminate_for_host_exit(self) -> None:
        try:
            self.terminate_job(self.api, self.job, 1)
        except (OSError, RuntimeError, win32_process.Win32Error):
            pass


def launch_windows_job(spec: Dict[str, Any], program: str,
                       env: Dict[str, str],
                       internals: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """Direct Windows Job launch (the runner-free adaptation).

    stdio carriers: "ignore"→the NUL device handle, "inherit"→the current
    standard handle, "pipe" is refused on this wave's direct form (the
    sandbox-visible launch surface is the no-pipe shape; pipe stdio routes
    through the fallback spawn where Python owns the pipes).
    """
    api = (internals or {}).get("bindings") or \
        win32_process.load_win32_process_bindings()
    stdio_spec = spec.get("stdio") or {}
    nul_handle = None

    def resolve_handle(mode: Any, selector: int, label: str) -> Dict[str, int]:
        # {"handle": N} direct-ownership shape (target_carrier_handles
        # passes it through without a CRT descriptor round-trip)
        nonlocal nul_handle
        if mode == "pipe":
            raise RuntimeError(
                "windows-job direct containment does not carry pipe stdio; "
                "the fallback spawn owns pipes")
        if mode == "ignore":
            if nul_handle is None:
                nul_handle = _open_nul()
            return {"handle": nul_handle}
        handle = api["getStdHandle"](selector)
        if win32_process.is_null_ptr(handle):
            win32_process.throw_last_error(api, "GetStdHandle",
                                           f"null {label} handle")
        return {"handle": int(handle)}

    try:
        stdio = {
            "stdin": resolve_handle(stdio_spec.get("stdin", "ignore"), -10,
                                    "stdin"),
            "stdout": resolve_handle(stdio_spec.get("stdout"), -11, "stdout"),
            "stderr": resolve_handle(stdio_spec.get("stderr"), -12, "stderr"),
        }
        result = win32_process.spawn_current_token_job_process(api, {
            "command": program,
            "args": spec["argv"][1:],
            "cwd": spec["cwd"],
            "env": env,
            "applicationName": None,
            "stdio": stdio,
        })
    finally:
        if nul_handle is not None:
            api["closeHandle"](nul_handle)
    try:
        owner = WindowsJobOwner(result["job"], result["process"], api, internals)
        direct_event = threading.Event()
        wait_thread = threading.Thread(target=_job_direct_wait,
                                       args=(owner, direct_event), daemon=True)
        wait_thread.start()
    except BaseException:
        try:
            win32_process.terminate_job(api, result["job"], 1)
        finally:
            win32_process.close_best_effort(api, result["process"])
            win32_process.close_best_effort(api, result["job"])
        raise
    return {"child": _JobChild(owner, wait_thread, direct_event),
            "stdin": None, "stdout": None, "stderr": None, "control": None,
            "directEvent": direct_event,
            "owner": {"signal": owner.signal,
                      "waitForExit": owner.wait_for_exit,
                      "terminateForHostExit": owner.terminate_for_host_exit},
            "pid": result["pid"]}


def _open_nul() -> int:
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]
    handle = create_file("NUL", 0x40000000 | 0x80000000, 0x1 | 0x2, None,
                         3, 0, None)  # GENERIC_READ|WRITE, SHARE_R|W, OPEN_EXISTING
    if handle is None or handle == win32_process.INVALID_HANDLE:
        raise OSError("could not open the NUL device")
    return int(handle)


def _job_direct_wait(owner: WindowsJobOwner, event: threading.Event) -> None:
    owner.wait_target_exit()
    event.set()


class _JobChild:
    """Popen-shaped view over the Job-owned direct process."""

    def __init__(self, owner: WindowsJobOwner, thread: threading.Thread,
                 event: threading.Event) -> None:
        self._owner = owner
        self._thread = thread
        self._event = event
        self.pid = owner.process

    def poll(self) -> Optional[int]:
        with self._owner._lock:
            return self._owner._exit_code

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        self._event.wait(timeout)
        with self._owner._lock:
            return self._owner._exit_code


# ------------------------------------------------------ linux scope plane

def quote(value: str) -> str:
    """POSIX single-quote embedding (verbatim)."""
    return "'" + value.replace("'", "'\\''") + "'"


def unit_stem(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{os.urandom(6).hex()}"


def scope_args(unit_base: str, invocation: Sequence[str],
               argv: Sequence[str]) -> List[str]:
    return ["--user", "--scope", "--quiet", "--collect",
            "--expand-environment=no", f"--unit={unit_base}", "--",
            *invocation, "--", *argv]


def manager_environment() -> Dict[str, str]:
    env = {key: value for key, value in child_env({"LC_ALL": "C"}).items()
           if value is not None}
    env.pop("SYSTEMD_LOG_TARGET", None)
    return env


def quiet_systemd_environment() -> Dict[str, str]:
    return {key: value for key, value in child_env(
        {"LC_ALL": "C", "SYSTEMD_LOG_TARGET": "null"}).items()
        if value is not None}


def has_exact_keys(value: Dict[str, Any], required: Sequence[str],
                   optional: Sequence[str] = ()) -> bool:
    """Verbatim hasExactKeys over one record."""
    allowed = set(required) | set(optional)
    return (all(key in value for key in required)
            and all(key in allowed for key in value))


def is_string_record(value: Any) -> bool:
    return is_record(value) and all(isinstance(entry, str)
                                    for entry in value.values())


def is_serialized_runner_error(value: Any) -> bool:
    """Verbatim isSerializedRunnerError shape check."""
    if not is_record(value) or not has_exact_keys(
            value, ["name", "message"], ["code", "syscall", "path"]):
        return False
    return (isinstance(value.get("name"), str)
            and isinstance(value.get("message"), str)
            and all(value.get(key) is None or isinstance(value.get(key), str)
                    for key in ("code", "syscall", "path")))


def parse_error_result(value: Any) -> Dict[str, Any]:
    """Strictly parse the single bounded error record (verbatim
    parseErrorResult)."""
    if not is_record(value) or not has_exact_keys(value, ["type", "error"]) \
            or not is_serialized_runner_error(value.get("error")):
        raise RuntimeError(
            "subprocess runner emitted an invalid error result")
    if value.get("type") != "error":
        raise RuntimeError(
            "subprocess runner emitted an unknown error result")
    return {"type": "error", "error": value["error"]}


class LinuxScopeStartup:
    """Parent-side invocation and bootstrap state for one private native
    runner launch (verbatim LinuxScopeStartup: requested termination
    preserves the observed signal even before bootstrap consumption)."""

    def __init__(self, files: Dict[str, str], kind: str) -> None:
        self.files = files
        self.kind = kind
        self.termination_signals: Set[str] = set()

    def resolve_outcome(self, outcome: Dict[str, Any]) -> Dict[str, Any]:
        startup = read_linux_startup_error(self.files["startupErrorPath"])
        if startup is not None:
            raise deserialize_runner_error(startup["error"])
        if os.path.exists(self.files["requestPath"]) and not (
                outcome.get("signal") is not None
                and outcome["signal"] in self.termination_signals):
            raise RuntimeError(
                f"{self.kind} scope exited before its bootstrap consumed "
                "the launch request")
        return outcome


def run_sync_default(command: str, args: Sequence[str],
                     options: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """The spawnSync boundary (documented Python adaptation: one
    subprocess.run call; capture requests use pipes, which sandboxed
    hosts may refuse — tests inject this seam)."""
    opts = options or {}
    kwargs: Dict[str, Any] = {}
    env = opts.get("env")
    if env is not None:
        kwargs["env"] = env
    timeout = opts.get("timeout")
    if timeout is not None:
        kwargs["timeout"] = timeout / 1000
    try:
        if opts.get("stdio") == "ignore":
            proc = subprocess.run(
                [command, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, **kwargs)
            return {"status": proc.returncode, "stdout": "", "stderr": ""}
        proc = subprocess.run([command, *args], capture_output=True,
                              text=True, check=False, **kwargs)
        return {"status": proc.returncode, "stdout": proc.stdout or "",
                "stderr": proc.stderr or ""}
    except FileNotFoundError as error:
        return {"status": None, "stdout": "", "stderr": "", "error": error}
    except subprocess.TimeoutExpired as error:
        return {"status": None, "stdout": "", "stderr": "", "error": error}
    except OSError as error:
        return {"status": None, "stdout": "", "stderr": "", "error": error}


def sleep_with_abort(delay_ms: float,
                     wake: Optional[threading.Event] = None) -> None:
    """The timers/promises setTimeout boundary: an interruptible sleep
    (wake.set() ends it early — the wakeObservation analogue)."""
    if wake is not None:
        wake.wait(delay_ms / 1000)
    else:
        time.sleep(delay_ms / 1000)


def query_systemctl(command: str, args: Sequence[str]) -> Dict[str, Any]:
    """The async systemctl query boundary (verbatim querySystemctl result
    shape: numeric exit statuses, string error codes mapped to a null
    status, and the exception object when one interrupted the query)."""
    try:
        proc = subprocess.run(
            [command, *args], capture_output=True, text=True,
            env=manager_environment(),
            timeout=SYSTEMCTL_TIMEOUT_MS / 1000, check=False)
        return {"status": proc.returncode, "stdout": proc.stdout or "",
                "stderr": proc.stderr or ""}
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": None, "stdout": "", "stderr": str(error),
                "error": error}


class SystemdScopeOwner:
    """One transient user-scope owner (verbatim SystemdScopeOwner: the
    establishment latch, task-count observation, wake generations, the
    direct fallback signal, kill-failure escalation, and the empty-range
    release). The direct seam is {"running", "signal", "settled"} with a
    threading.Event settled form."""

    def __init__(self, unit: str, startup: LinuxScopeStartup,
                 direct: Dict[str, Any], systemctl: str = "systemctl",
                 run_sync: Optional[Callable[..., Dict[str, Any]]] = None,
                 query: Optional[Callable[..., Dict[str, Any]]] = None,
                 sleep: Optional[Callable[..., None]] = None) -> None:
        self._state_lock = threading.RLock()
        self.unit = unit
        self.startup = startup
        self.direct = direct
        self.systemctl = systemctl
        self.run_sync: Callable[..., Dict[str, Any]]
        if run_sync is not None:
            self.run_sync = run_sync
        else:
            self.run_sync = run_sync_default
        self.query: Callable[..., Dict[str, Any]]
        if query is not None:
            self.query = query
        else:
            self.query = query_systemctl
        self.sleep: Callable[..., Any]
        if sleep is not None:
            self.sleep = sleep
        else:
            self.sleep = sleep_with_abort
        self.establishment = "pending"
        self.stopped = False
        self.termination_requested = False
        self.observation: Optional[threading.Thread] = None
        self.observation_error: Optional[BaseException] = None
        self.kill_failure: Optional[BaseException] = None
        self.direct_kill_settlement: Optional[threading.Event] = None
        self.wake_generation = 0
        self.wake_waiter: Optional[threading.Event] = None

    def inspect_task_count(self) -> Optional[int]:
        result = self.run_sync(
            self.systemctl,
            ["--user", "show", self.unit, "--property=TasksCurrent",
             "--property=LoadState", "--property=ActiveState"],
            {"env": manager_environment(), "encoding": "utf8",
             "timeout": SYSTEMCTL_TIMEOUT_MS})
        if result.get("error") is not None or result.get("status") != 0 \
                or not isinstance(result.get("stdout"), str):
            return None
        try:
            state = self.parse_unit_state(result["stdout"])
        except RuntimeError:
            return None
        if state["loadState"] == "loaded" and state["activeState"] == "active":
            return state.get("tasksCurrent")
        return None

    def signal(self, signal_name: str) -> None:
        with self._state_lock:
            if self.stopped:
                return
            self.termination_requested = True
            if self.direct["running"]():
                self.startup.termination_signals.add(signal_name)
            self.observe_request_consumption()
            direct_fallback_required = self.establishment == "pending"
        if direct_fallback_required and self.direct["running"]():
            self.direct["signal"](signal_name)
        result = self.run_sync(
            self.systemctl,
            ["--user", "kill", "--kill-whom=all",
             f"--signal={signal_name}", self.unit],
            {"env": manager_environment(), "timeout": SYSTEMCTL_TIMEOUT_MS})
        self.wake_observation()
        if result.get("error") is None and result.get("status") == 0:
            if signal_name == "SIGKILL":
                with self._state_lock:
                    self.kill_failure = None
                    self.direct_kill_settlement = None
            return
        if not direct_fallback_required and self.direct["running"]():
            self.direct["signal"](signal_name)
        if signal_name == "SIGKILL" and not MISSING_UNIT.search(
                str(result.get("stderr") or "")):
            error = result.get("error")
            if isinstance(error, BaseException):
                with self._state_lock:
                    self.kill_failure = error
            else:
                stderr_text = str(result.get("stderr") or "").strip()
                detail = stderr_text or f"exit {result.get('status')}"
                with self._state_lock:
                    self.kill_failure = RuntimeError(
                        f"systemctl could not terminate {self.unit}: {detail}")
            with self._state_lock:
                self.direct_kill_settlement = self.direct["settled"]

    def terminate_for_host_exit(self) -> None:
        if self.stopped:
            return
        if self.direct["running"]():
            try:
                self.direct["signal"]("SIGKILL")
            except BaseException:
                pass
        self.run_sync(
            self.systemctl,
            ["--user", "kill", "--kill-whom=all", "--signal=SIGKILL",
             self.unit],
            {"env": manager_environment(), "stdio": "ignore",
             "timeout": SYSTEMCTL_TIMEOUT_MS})

    def observe_request_consumption(self) -> None:
        if self.establishment == "pending" and not os.path.exists(
                self.startup.files["requestPath"]):
            self.establishment = "established"

    def absent_unit(self) -> bool:
        self.observe_request_consumption()
        if self.establishment == "established":
            return False
        if not self.direct["running"]() and os.path.exists(
                self.startup.files["requestPath"]):
            return False
        if self.kill_failure is not None:
            raise self.kill_failure
        return True

    def empty_range(self, tasks: Optional[int]) -> bool:
        return (self.termination_requested and tasks == 0
                and (not self.direct["running"]()
                     or not os.path.exists(
                         self.startup.files["requestPath"])))

    def release_empty_range(self) -> None:
        self.run_sync(
            self.systemctl, ["--user", "stop", self.unit],
            {"env": quiet_systemd_environment(), "stdio": "ignore",
             "timeout": SYSTEMCTL_TIMEOUT_MS})

    def parse_unit_state(self, stdout: str) -> Dict[str, Any]:
        """Parse systemctl show state (verbatim parseUnitState: first '='
        splits, duplicate and malformed detection, the '[not set]'
        TasksCurrent omission, and the completeness fence)."""
        values: Dict[str, str] = {}
        for line in stdout.split("\n"):
            if not line.strip():
                continue
            separator = line.find("=")
            if separator <= 0:
                raise RuntimeError(
                    f"systemctl returned malformed state for {self.unit}: "
                    f"{stdout}")
            name = line[:separator]
            value = line[separator + 1:]
            if name in values:
                raise RuntimeError(
                    f"systemctl returned duplicate {name} for {self.unit}: "
                    f"{stdout}")
            if name == "TasksCurrent" and value == "[not set]":
                continue
            values[name] = value
        expected = 3 if "TasksCurrent" in values else 2
        if len(values) != expected or "LoadState" not in values \
                or "ActiveState" not in values:
            raise RuntimeError(
                f"systemctl returned incomplete state for {self.unit}: "
                f"{stdout}")
        tasks_current = values.get("TasksCurrent")
        if tasks_current is not None \
                and re.fullmatch(r"\d+", tasks_current) is None:
            raise RuntimeError(
                f"systemctl returned a non-numeric TasksCurrent for "
                f"{self.unit}: {tasks_current}")
        state: Dict[str, Any] = {"loadState": values["LoadState"],
                                 "activeState": values["ActiveState"]}
        if tasks_current is not None:
            state["tasksCurrent"] = int(tasks_current)
        return state

    def range_active(self) -> bool:
        return self._range_active(rechecked=False)

    def _range_active(self, rechecked: bool) -> bool:
        with self._state_lock:
            if self.stopped:
                return False
            self.observe_request_consumption()
            generation = self.wake_generation
        direct_running = self.direct["running"]()
        result = self.query(
            self.systemctl,
            ["--user", "show", self.unit, "--property=LoadState",
             "--property=ActiveState", "--property=TasksCurrent"])
        with self._state_lock:
            if generation != self.wake_generation:
                return True
            kill_failure = self.kill_failure
            kill_settlement = self.direct_kill_settlement
        output = f"{result.get('stdout') or ''}{result.get('stderr') or ''}"
        status = result.get("status")
        if status == 0:
            state = self.parse_unit_state(str(result.get("stdout") or ""))
            load_state = state["loadState"]
            active_state = state["activeState"]
            tasks = state.get("tasksCurrent")
            if not (load_state == "not-found" and active_state == "inactive")\
                    and load_state != "loaded":
                raise RuntimeError(
                    f"systemctl returned unknown state for {self.unit}: "
                    + json.dumps({"loadState": load_state,
                                  "activeState": active_state}))
            self.establishment = "established"
            if active_state in ("inactive", "failed"):
                return False
            if active_state not in ("active", "reloading", "inactive",
                                    "failed"):
                raise RuntimeError(
                    f"systemctl returned unknown state for {self.unit}: "
                    + json.dumps({"activeState": active_state}))
            if self.empty_range(tasks):
                self.release_empty_range()
                return False
            if kill_failure is not None:
                if (not rechecked and direct_running
                        and kill_settlement is not None):
                    if not kill_settlement.wait(
                            SYSTEMCTL_TIMEOUT_MS / 1000):
                        raise TimeoutError("direct scope termination did not settle")
                    return self._range_active(rechecked=True)
                raise kill_failure
            return True
        if not MISSING_UNIT.search(output):
            error = result.get("error")
            if isinstance(error, BaseException):
                raise error
            detail = output.strip() or f"exit {status}"
            raise RuntimeError(
                f"systemctl could not read {self.unit}: {detail}")
        return self.absent_unit()

    def wake_observation(self) -> None:
        with self._state_lock:
            self.wake_generation += 1
            waiter, self.wake_waiter = self.wake_waiter, None
            if waiter is not None:
                waiter.set()

    def wait_for_poll(self, delay_ms: float, generation: int) -> None:
        with self._state_lock:
            if generation != self.wake_generation:
                return
            waiter = threading.Event()
            self.wake_waiter = waiter
        try:
            self.sleep(delay_ms, waiter)
        finally:
            with self._state_lock:
                if self.wake_waiter is waiter:
                    self.wake_waiter = None

    def _observe_loop(self) -> None:
        try:
            poll_interval = float(SCOPE_INITIAL_POLL_INTERVAL_MS)
            generation = self.wake_generation
            while self.range_active():
                self.wait_for_poll(poll_interval, generation)
                generation = self.wake_generation
                if self.establishment == "established":
                    poll_interval = min(poll_interval * 2,
                                        float(SYSTEMCTL_TIMEOUT_MS))
            with self._state_lock:
                self.stopped = True
        except BaseException as error:  # re-raised by wait_for_exit
            with self._state_lock:
                self.observation_error = error

    def wait_for_exit(self) -> None:
        with self._state_lock:
            if self.stopped:
                return
            if self.observation is None:
                thread = threading.Thread(target=self._observe_loop,
                                          name="scope-observation", daemon=True)
                self.observation = thread
                thread.start()
            observation = self.observation
        observation.join()
        with self._state_lock:
            if self.observation_error is not None:
                raise self.observation_error

    def cleanup(self) -> None:
        cleanup_linux_launch_files(self.startup.files)


def load_linux_execve() -> Callable[..., None]:
    """Load the libc execve binding (Python-native: os.execve with the
    close-on-exec flags cleared on the standard descriptors — and the
    control descriptor when the launch carries the control pipe — exactly
    like the upstream koffi bootstrap)."""
    fcntl = __import__("fcntl")  # posix-only; dynamic to keep win32 analysis

    def execve(file: str, argv: Sequence[str], env: Dict[str, str],
               control: Optional[str] = None) -> None:
        descriptors = [0, 1, 2]
        if control == "pipe":
            descriptors.append(SUBPROCESS_CONTROL_FD)
        for fd in descriptors:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            if flags & fcntl.FD_CLOEXEC:
                fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        os.execve(file, list(argv), env)

    return execve


def create_linux_launch_files(request: Dict[str, Any]) -> Dict[str, str]:
    """Create one private 0700 directory and one complete 0600 launch
    request (verbatim createLinuxLaunchFiles)."""
    directory = tempfile.mkdtemp(prefix="dsh-subprocess-launch-")
    files = {
        "directory": directory,
        "requestPath": os.path.join(directory, "launch-request.json"),
        "startupErrorPath": os.path.join(directory, "startup-error.json"),
    }
    try:
        os.chmod(directory, 0o700)
        fd = os.open(files["requestPath"],
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(request))
        return files
    except BaseException:
        cleanup_linux_launch_files(files)
        raise


def linux_launch_files_from_locator(request_path: str) -> Dict[str, str]:
    """Derive the only permitted startup-error path from one absolute
    request locator (verbatim linuxLaunchFilesFromLocator)."""
    if not os.path.isabs(request_path) or \
            os.path.basename(request_path) != "launch-request.json":
        raise RuntimeError(
            "subprocess runner received an invalid Linux launch-request "
            "locator")
    directory = os.path.dirname(request_path)
    return {
        "directory": directory,
        "requestPath": request_path,
        "startupErrorPath": os.path.join(directory, "startup-error.json"),
    }


def consume_linux_launch_request(request_path: str) -> Dict[str, Any]:
    """Strictly read and remove the single Linux launch request (verbatim
    consumeLinuxLaunchRequest validation)."""
    with open(request_path, "r", encoding="utf-8") as handle:
        text = handle.read()
    os.unlink(request_path)
    value = json.loads(text)
    if not is_record(value) \
            or not has_exact_keys(value, ["cwd", "env"], ["control"]) \
            or not isinstance(value.get("cwd"), str) \
            or not is_string_record(value.get("env")) \
            or (value.get("control") is not None
                and value.get("control") != "pipe"):
        raise RuntimeError(
            "subprocess runner received an invalid Linux launch request")
    result: Dict[str, Any] = {"cwd": value["cwd"], "env": value["env"]}
    if value.get("control") == "pipe":
        result["control"] = "pipe"
    return result


def write_linux_startup_error(files: Dict[str, str],
                              error: Dict[str, Any]) -> None:
    """Publish one strict 0600 Linux pre-exec error."""
    fd = os.open(files["startupErrorPath"],
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(error))


def read_linux_startup_error(path: str) -> Optional[Dict[str, Any]]:
    """Read the Linux pre-exec error, if the bootstrap published one
    (verbatim readLinuxStartupError: the record passes parseErrorResult).
    """
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        value = json.loads(handle.read())
    if not is_record(value):
        raise RuntimeError(
            "subprocess runner emitted an invalid startup error")
    return parse_error_result(value)


def cleanup_linux_launch_files(files: Dict[str, Any]) -> None:
    """Best-effort removal of only the private paths created for one
    launch (verbatim cleanupLinuxLaunchFiles: symlink-fenced, ENOENT
    tolerated per file)."""
    try:
        directory = files["directory"]
        if os.path.islink(directory):
            os.unlink(directory)
            return
        for path in (files["requestPath"], files["startupErrorPath"]):
            try:
                os.unlink(path)
            except OSError as error:
                if error.errno != errno_module.ENOENT:
                    raise
        os.rmdir(directory)
    except OSError:
        pass


def exec_linux_file(file: str, argv: Sequence[str], env: Dict[str, str],
                    internals: Dict[str, Any],
                    control: Optional[str] = None) -> None:
    """One execve attempt with the ENOEXEC /bin/sh fallback (verbatim)."""
    execve = internals["execve"]
    try:
        if control is None:
            execve(file, argv, env)
        else:
            execve(file, argv, env, control)
    except OSError as error:
        if error.errno != errno_module.ENOEXEC:
            raise
        sh_argv = ["/bin/sh", file, *list(argv)[1:]]
        if control is None:
            execve("/bin/sh", sh_argv, env)
        else:
            execve("/bin/sh", sh_argv, env, control)


def exec_linux_target(request: Dict[str, Any], argv: Sequence[str],
                      internals: Dict[str, Any]) -> None:
    """Resolve and exec one Linux target: absolute paths exec directly;
    bare names walk PATH (relative entries anchored at cwd) with the
    EACCES-remembered ENOENT outcome."""
    program = argv[0]
    if "/" in program:
        exec_linux_file(program, argv, request["env"], internals,
                        request.get("control"))
        return
    path_value = request["env"].get("PATH", "/usr/bin:/bin")
    cwd = request["cwd"]
    permission_failure: Optional[OSError] = None
    for directory in path_value.split(":"):
        if directory.startswith("/"):
            root = directory
        else:
            root = f"{cwd}{'' if cwd.endswith('/') else '/'}{directory}"
        candidate = f"{root}{'' if root.endswith('/') else '/'}{program}"
        try:
            exec_linux_file(candidate, argv, request["env"], internals,
                            request.get("control"))
            return
        except OSError as error:
            code = error.errno
            if code == errno_module.EACCES:
                permission_failure = permission_failure or error
            elif code != errno_module.ENOENT:
                raise
    if permission_failure is not None:
        raise permission_failure
    failure = OSError(errno_module.ENOENT, "No such file or directory")
    failure.filename = program  # type: ignore[assignment]
    raise failure


def _signal_number(signal_name: str) -> int:
    number = _SIGNAL_NUMBERS.get(signal_name)
    if number is None:
        import signal as signal_module
        number = getattr(signal_module, signal_name)
    return number


def signal_linux_direct_process(pid: int, send: Callable[[], Any]) -> bool:
    """Send one direct-process signal, distinguishing an absent PID from
    failed delivery (verbatim signalLinuxDirectProcess)."""
    try:
        if send():
            return True
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return False
    except OSError as error:
        return error.errno == errno_module.ESRCH


def signal_child_group(child: Any, signal_name: str) -> bool:
    """Group-first signal with the direct-process fallback (verbatim
    signalChildGroup)."""
    group_signalled = False
    try:
        os.kill(-child.pid, _signal_number(signal_name))
        group_signalled = True
    except OSError:
        pass
    if group_signalled and signal_name == "SIGTERM":
        return True

    def send_direct() -> bool:
        os.kill(child.pid, _signal_number(signal_name))
        return True

    return signal_linux_direct_process(child.pid, send_direct)


#: The private Linux bootstrap: one -c program that consumes the launch
#: request locator from SUBPROCESS_RUNNER_ENV and execs the target
#: (documented adaptation: the Node IPC runner protocol is not ported;
#: the Python parent owns the scope through these primitives).
LINUX_BOOTSTRAP_SOURCE = """
import os, sys
from AstralNexus.runtime.subprocess_local import (
    consume_linux_launch_request, exec_linux_target, load_linux_execve,
    linux_launch_files_from_locator, write_linux_startup_error, as_spawn_error)
files = linux_launch_files_from_locator(os.environ['DSH_SUBPROCESS_RUNNER'])
argv = sys.argv[1:]
if argv and argv[0] == '--':
    argv = argv[1:]
try:
    request = consume_linux_launch_request(files['requestPath'])
    exec_linux_target(request, argv, {'execve': load_linux_execve()})
except BaseException as error:
    write_linux_startup_error(files, {'type': 'error',
        'error': as_spawn_error(error, argv[0] if argv else '')})
    sys.exit(127)
"""


def spawn_runner_invocation() -> List[str]:
    """The private runner invocation for this host (documented Python
    adaptation of spawnRunnerInvocation: the interpreter plus the
    bootstrap source; the request locator travels in the environment)."""
    return [sys.executable, "-X", "utf8", "-c", LINUX_BOOTSTRAP_SOURCE]


def runner_invocation_available(invocation: Sequence[str]) -> bool:
    """Verbatim runnerInvocationAvailable: the interpreter must be an
    executable absolute path; a distinct absolute entry must be readable.
    """
    try:
        if invocation and os.path.isabs(invocation[0]):
            if not os.access(invocation[0], os.X_OK):
                return False
        entry = invocation[-1] if invocation else None
        if entry is not None and entry != (invocation[0] if invocation
                                           else None) \
                and os.path.isabs(entry):
            if not os.access(entry, os.R_OK):
                return False
        return True
    except OSError:
        return False


def probe_linux_bootstrap(internals: Optional[Dict[str, Any]] = None
                          ) -> bool:
    """Whether the exec bootstrap can load on this host (verbatim
    probeLinuxBootstrap)."""
    api = internals or {}
    try:
        (api.get("loadLinuxExecve") or load_linux_execve)()
        invocation = api.get("runnerInvocation")
        if invocation is None:
            resolve = api.get("resolveRunnerInvocation")
            invocation = (resolve() if resolve is not None
                          else spawn_runner_invocation())
        available = api.get("runnerAvailable") or runner_invocation_available
        return bool(available(invocation))
    except BaseException:
        return False


def probe_linux_manager(internals: Optional[Dict[str, Any]] = None) -> bool:
    """Whether one user systemd manager answers (verbatim
    probeLinuxManager)."""
    api = internals or {}
    run = api.get("spawnSync") or run_sync_default
    systemctl = api.get("systemctl", "systemctl")
    result = run(systemctl, ["--user", "show", "--property=Version",
                             "--value"],
                 {"env": manager_environment(), "stdio": "ignore",
                  "timeout": SYSTEMCTL_TIMEOUT_MS})
    return result.get("error") is None and result.get("status") == 0


def probe_linux_scope(internals: Optional[Dict[str, Any]] = None) -> bool:
    """Whether one transient probe scope can start and report (verbatim
    probeLinuxScope: the nested systemctl show must exit zero)."""
    api = internals or {}
    run = api.get("spawnSync") or run_sync_default
    systemd_run = api.get("systemdRun", "systemd-run")
    unit_base = unit_stem("dsh-subprocess-probe")
    result = run(systemd_run,
                 ["--user", "--scope", "--quiet", "--collect",
                  "--expand-environment=no", f"--unit={unit_base}", "--",
                  api.get("systemctl", "systemctl"), "--user", "show",
                  f"{unit_base}.scope", "--property=ActiveState", "--value"],
                 {"env": quiet_systemd_environment(), "stdio": "ignore",
                  "timeout": SYSTEMCTL_TIMEOUT_MS})
    return result.get("error") is None and result.get("status") == 0


def probe_linux_native(internals: Optional[Dict[str, Any]] = None) -> bool:
    """Whether one exec bootstrap can enter a user systemd scope."""
    return (probe_linux_bootstrap(internals)
            and probe_linux_scope(internals))


#: Runner-control environment prefixes stripped from the private runner
#: environment (verbatim RUNNER_CONTROL_ENV_PREFIXES).
RUNNER_CONTROL_ENV_PREFIXES = ("NODE_", "TSX_")

#: The packaged-source tsconfig anchor for .ts runner entries (verbatim
#: SOURCE_TSCONFIG_PATH relative anchor; unreachable for the Python
#: invocation, kept for shape fidelity).
SOURCE_TSCONFIG_PATH = str(
    Path(__file__).resolve().parents[3] / "tsconfig.base.json")


def runner_environment(selection: str,
                       invocation: Optional[Sequence[str]] = None
                       ) -> Dict[str, str]:
    """The exact private runner environment (verbatim runnerEnvironment:
    the child environment minus runner-control variables, plus the launch
    locator, quiet systemd logging, and the source tsconfig anchor for
    .ts entries)."""
    env = child_env()
    result: Dict[str, str] = {}
    for key, value in env.items():
        if value is None:
            continue
        upper = key.upper()
        if any(upper.startswith(prefix)
               for prefix in RUNNER_CONTROL_ENV_PREFIXES):
            continue
        result[key] = value
    result[SUBPROCESS_RUNNER_ENV] = selection
    result["SYSTEMD_LOG_TARGET"] = "null"
    entry = invocation[-1] if invocation else None
    if entry is not None and entry.endswith(".ts"):
        result["TSX_TSCONFIG_PATH"] = SOURCE_TSCONFIG_PATH
    return result


def prepare_linux_terminal_scope(spec: Dict[str, Any],
                                 target_env: Dict[str, str],
                                 internals: Optional[Dict[str, Any]] = None
                                 ) -> Dict[str, Any]:
    """Prepare one Linux PTY scope using the same launch request and
    bootstrap core (verbatim prepareLinuxTerminalScope)."""
    api = internals or {}
    invocation = api.get("runnerInvocation") or spawn_runner_invocation()
    files = create_linux_launch_files({"cwd": spec["cwd"], "env": target_env})
    startup = LinuxScopeStartup(files, "terminal")
    unit_base = unit_stem("dsh-terminal")
    return {
        "command": api.get("systemdRun", "systemd-run"),
        "args": scope_args(unit_base, invocation, spec["argv"]),
        "cwd": os.getcwd(),
        "env": runner_environment(files["requestPath"], invocation),
        "bindOwner": lambda direct: SystemdScopeOwner(
            f"{unit_base}.scope", startup, direct,
            api.get("systemctl", "systemctl"),
            api.get("spawnSync") or run_sync_default,
            api.get("systemctlQuery") or query_systemctl,
            api.get("sleep") or sleep_with_abort),
        "resolveOutcome": startup.resolve_outcome,
        "cleanup": lambda: cleanup_linux_launch_files(files),
    }


def launch_linux_scope(spec: Dict[str, Any], target_env: Dict[str, str],
                       internals: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """Launch one ordinary target inside a transient user scope (verbatim
    launchLinuxScope: the detached systemd-run child, the direct-outcome
    settlement, and the owner's direct seam)."""
    api = internals or {}
    invocation = api.get("runnerInvocation") or spawn_runner_invocation()
    request: Dict[str, Any] = {"cwd": spec["cwd"], "env": target_env}
    control = (spec.get("stdio") or {}).get("control")
    if control is not None:
        request["control"] = control
    files = create_linux_launch_files(request)
    startup = LinuxScopeStartup(files, "subprocess")
    unit_base = unit_stem("dsh-subprocess")
    stdio = spec.get("stdio") or {}
    if stdio.get("control") == "pipe":
        # documented boundary: the control-pipe runner protocol is not
        # ported (the Python parent owns the scope directly)
        cleanup_linux_launch_files(files)
        raise RuntimeError(
            "control-pipe launches are unavailable in the Python-native "
            "subprocess-local port")

    def _stdio_target(mode: Any) -> Any:
        if mode == "ignore" or mode is None:
            return subprocess.DEVNULL
        if mode == "inherit":
            return None
        return subprocess.PIPE

    popen = api.get("spawn") or subprocess.Popen
    try:
        child = popen(
            [api.get("systemdRun", "systemd-run"),
             *scope_args(unit_base, invocation, spec["argv"])],
            cwd=os.getcwd(),
            env=runner_environment(files["requestPath"], invocation),
            stdin=_stdio_target(stdio.get("stdin")),
            stdout=_stdio_target(stdio.get("stdout")),
            stderr=_stdio_target(stdio.get("stderr")),
            start_new_session=True)
    except BaseException:
        cleanup_linux_launch_files(files)
        raise
    settled = threading.Event()

    def wait_direct() -> None:
        try:
            child.wait()
        finally:
            settled.set()

    threading.Thread(target=wait_direct, name="scope-direct",
                     daemon=True).start()
    direct = {
        "running": lambda: child.poll() is None,
        "signal": lambda signal_name: signal_child_group(child, signal_name),
        "settled": settled,
    }
    owner = SystemdScopeOwner(
        f"{unit_base}.scope", startup, direct,
        api.get("systemctl", "systemctl"),
        api.get("spawnSync") or run_sync_default,
        api.get("systemctlQuery") or query_systemctl,
        api.get("sleep") or sleep_with_abort)
    return {
        "child": child,
        "stdin": child.stdin,
        "stdout": child.stdout,
        "stderr": child.stderr,
        "control": None,
        "directEvent": settled,
        "owner": {"signal": owner.signal,
                  "waitForExit": owner.wait_for_exit,
                  "terminateForHostExit": owner.terminate_for_host_exit,
                  "alive": direct["running"]},
        "scopeOwner": owner,
        "resolveOutcome": startup.resolve_outcome,
        "cleanup": owner.cleanup,
        "pid": child.pid,
    }


# ------------------------------------------------- descriptor shape check

def descriptor_shape(stdio: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one stdio request into its mapped launch shape.

    stdin accepts "ignore"/"pipe"/"inherit" or a {data} object (written
    then ended immediately at bind time); stdout/stderr accept
    "ignore"/"inherit"/"pipe" or a collect object {maxBytes, spill?}.
    """
    result: Dict[str, Any] = {}
    stdin_mode = stdio.get("stdin", "ignore")
    if not (stdin_mode in ("ignore", "inherit", "pipe", None)
            or isinstance(stdin_mode, dict)):
        raise ValueError(f"invalid stdin mode: {stdin_mode!r}")
    if isinstance(stdin_mode, dict) and not isinstance(
            stdin_mode.get("data"), (bytes, bytearray)):
        raise ValueError("stdin data must be bytes")
    result["stdin"] = stdin_mode if stdin_mode is not None else "ignore"
    for label in ("stdout", "stderr"):
        mode = stdio.get(label)
        if isinstance(mode, dict):
            if "maxBytes" not in mode:
                raise ValueError(
                    f"invalid {label} collect mode: missing maxBytes")
            for descriptor in (mode, mode.get("spill")):
                if descriptor is None:
                    continue
                if (not isinstance(descriptor, dict)
                        or type(descriptor.get("maxBytes")) is not int
                        or descriptor["maxBytes"] < 0):
                    raise ValueError(f"invalid {label} collector byte limit")
            result[label] = mode
        elif mode in ("pipe", "inherit", "ignore", None):
            result[label] = mode or "pipe"
        else:
            raise ValueError(f"invalid {label} mode: {mode!r}")
    control = stdio.get("control")
    if control is not None and control != "pipe":
        raise ValueError(f"invalid control mode: {control!r}")
    if control is not None:
        result["control"] = "pipe"
    return result


# ------------------------------------------------------------ the service

class LocalSubprocessRuntime(SubprocessRuntime):
    """Local subprocess service: platform-selected managed ranges,
    Node-shaped stdio (verbatim LocalSubprocessRuntime faces: containment
    selection with the one-shot fallback warning, ordinary spawns with
    release-on-exit, terminal spawns, executable resolution, and the
    disposal/host-exit sweeps).

    Documented adaptation: the Cordis effect teardown becomes close()
    plus an atexit host-exit registration; a closed runtime refuses new
    spawns (upstream relies on service destruction).
    """

    def __init__(self, internals: Optional[Dict[str, Any]] = None) -> None:
        self._lifecycle_lock = threading.RLock()
        self.internals = internals or {}
        self.platform = self.internals.get("platform") or sys.platform
        self._logger = self.internals.get("logger")
        self._fallback_warning_issued = False
        self._linux_deep_probe_passed = False
        self.managed: Set[ManagedProcessHandle] = set()
        self.terminals: Set[Any] = set()
        self.control_channels: Set[Any] = set()
        self.terminal_inspector = self.internals.get("terminalInspector")
        self._closed = False
        self._host_exit_registered = False
        #: Spill failures reach the plugin logger; the log line is the
        #: only trace of why a result has no spill path.
        self.report_spill_failure = log_spill_failure(
            self._logger, "subprocess-local")

    # -- containment selection ------------------------------------------------

    def select_containment_mode(self, kind: str) -> str:
        """The containment mode one managed-range kind uses on this host
        (verbatim selectContainmentMode: the linux deep probe is cached
        after its first pass; only ordinary ranges select the Windows
        Job)."""
        fallback_reason: Optional[str] = None
        if self.platform == "linux":
            if self._linux_deep_probe_passed:
                available = probe_linux_manager(self.internals)
            else:
                available = probe_linux_native(self.internals)
            if available:
                self._linux_deep_probe_passed = True
                return "linux-scope"
            fallback_reason = ("the current user-systemd scope or private "
                               "bootstrap is unavailable")
        if kind == "ordinary" and self.platform == "win32":
            if probe_windows_job(self.internals):
                return "windows-job"
        self._warn_fallback(kind, fallback_reason)
        return "fallback"

    def _warn_fallback(self, kind: str,
                       selected_reason: Optional[str] = None) -> None:
        """One warning per runtime instance (verbatim warnFallback text).
        """
        if self._fallback_warning_issued:
            return
        self._fallback_warning_issued = True
        reason = selected_reason
        if reason is None:
            if self.platform == "darwin":
                reason = ("macOS has no supported persistent process-range "
                          "owner")
            elif self.platform == "win32":
                reason = ("Windows ConPTY remains outside Job containment"
                          if kind == "terminal"
                          else "the Win32 Job runner is unavailable")
            else:
                reason = (f"platform {self.platform} has no native managed "
                          "range")
        message = (
            "subprocess-local is using weaker process-tree containment "
            f"because {reason}; descendants that escape the process group "
            "or direct-parent tree are not guaranteed to terminate or "
            "delay waitForExit()")
        if self._logger is not None:
            self._logger.warn(message)
        else:
            sys.stderr.write(message + "\n")

    # -- spawn -------------------------------------------------------------------

    def spawn(self, spec: Dict[str, Any]) -> ManagedProcessHandle:
        with self._lifecycle_lock:
            return self._spawn_locked(spec)

    def _spawn_locked(self, spec: Dict[str, Any]) -> ManagedProcessHandle:
        """Spawn one fully specified process range; returns a live handle
        (verbatim spawn: containment-routed launch, control-channel
        registration, and release-on-exit)."""
        if self._closed:
            raise RuntimeError("subprocess runtime is closed")
        validate_subprocess_spec(spec)
        if spec.get("signal") is not None:
            spec["signal"].throw_if_aborted()
        resolved = dict(spec)
        resolved["stdio"] = descriptor_shape(spec.get("stdio") or {})
        env = target_environment(resolved)
        mode = self.select_containment_mode("ordinary")
        internals = {**self.internals,
                     "onSpillFailure": self.report_spill_failure}
        launch: Dict[str, Any]
        if mode == "fallback":
            launch = _fallback_launch(resolved, internals)
        elif mode == "linux-scope":
            launch = launch_linux_scope(resolved, env, internals)
        else:  # windows-job
            stdio = resolved["stdio"]
            if stdio.get("control") == "pipe" \
                    or isinstance(stdio.get("stdin"), dict) \
                    or stdio.get("stdout") == "pipe" \
                    or stdio.get("stderr") == "pipe" \
                    or isinstance(stdio.get("stdout"), dict) \
                    or isinstance(stdio.get("stderr"), dict):
                # documented boundary: the direct Job form carries
                # inherited/ignored stdio only; pipe stdio and {data}
                # stdin route through the fallback spawn where Python
                # owns the pipes (the Node IPC runner is not ported)
                self._warn_fallback("ordinary", "pipe/data stdio is not carried by the Windows Job launcher")
                launch = _fallback_launch(resolved, internals)
            else:
                launch = launch_windows_job(resolved, resolved["argv"][0],
                                            env, internals)
        try:
            handle = bind_managed_process(resolved, launch, internals)
        except BaseException as bind_error:
            try:
                launch["owner"]["terminateForHostExit"]()
            except BaseException as cleanup_error:
                getattr(bind_error, "add_note")(f"launch termination failed: {cleanup_error}")
            for label in ("stdin", "stdout", "stderr"):
                stream = launch.get(label)
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError) as cleanup_error:
                        getattr(bind_error, "add_note")(f"stream cleanup failed: {cleanup_error}")
            cleanup = launch.get("cleanup")
            if cleanup is not None:
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    getattr(bind_error, "add_note")(f"launch cleanup failed: {cleanup_error}")
            raise
        self.managed.add(handle)
        control = launch.get("control")
        if control is not None:
            self.control_channels.add(control)
            register = getattr(control, "on_close", None)
            if register is not None:
                def release_control() -> None:
                    with self._lifecycle_lock:
                        self.control_channels.discard(control)
                register(release_control)
        if not self._host_exit_registered:
            self._host_exit_registered = True
            import atexit
            atexit.register(self.terminate_for_host_exit)

        def release() -> None:
            handle.done_event.wait()
            try:
                handle.wait_for_exit()
            except BaseException:
                return  # Failed observation is not proof that the range exited.
            with self._lifecycle_lock:
                self.managed.discard(handle)

        threading.Thread(target=release, name="subprocess-release",
                         daemon=True).start()
        return handle

    # -- executable resolution ---------------------------------------------------

    def executable_candidates(self, command: str,
                              env: Dict[str, Optional[str]]
                              ) -> List[str]:
        """Ordered PATH candidate cross product (verbatim
        executableCandidates: every PATH directory times every extension,
        fully resolved)."""
        path = environment_value(env, "PATH") or ""
        if self.platform == "win32" and os.path.splitext(command)[1] == "":
            extensions = (environment_value(env, "PATHEXT")
                          or ".COM;.EXE;.BAT;.CMD").split(";")
        else:
            extensions = [""]
        cwd = os.getcwd()
        return [os.path.normpath(os.path.join(cwd, directory,
                                              command + extension))
                for directory in path.split(os.pathsep)
                for extension in extensions]

    def resolve_executable(self, command: str,
                           env: Optional[Dict[str, Optional[str]]] = None,
                           signal: Optional[CancellationToken] = None
                           ) -> str:
        """Resolve one executable (verbatim resolveExecutable: absolute
        commands are checked directly; bare names walk the candidate cross
        product; the first executable file wins; the EACCES-preferred
        failure message depends on the command form)."""
        if len(command) == 0:
            raise RuntimeError(
                "subprocess-local: executable must be non-empty")
        if signal is not None:
            signal.throw_if_aborted()
        environment = child_env(env)
        absolute = os.path.isabs(command)
        if not absolute and ("/" in command
                             or (self.platform == "win32"
                                 and "\\" in command)):
            raise RuntimeError(
                f"subprocess-local: command {json.dumps(command)} is a "
                "relative path; use an absolute path or a bare PATH name")
        candidates = ([command] if absolute
                      else self.executable_candidates(command, environment))
        for candidate in candidates:
            if signal is not None:
                signal.throw_if_aborted()
            try:
                if not os.path.isfile(candidate):
                    continue
                if not os.access(candidate, os.X_OK):
                    continue
                if signal is not None:
                    signal.throw_if_aborted()
                return candidate
            except OSError:
                continue
        if signal is not None:
            signal.throw_if_aborted()
        raise SubprocessExecutableNotFoundError(
            f"subprocess-local: command {json.dumps(command)} "
            + ("is not an executable file" if absolute
               else "was not found on PATH"))

    # -- terminal environment ---------------------------------------------------

    def terminal_environment(self,
                             signal: Optional[CancellationToken] = None
                             ) -> Dict[str, str]:
        """The terminal launch facts (verbatim terminalEnvironment: the
        platform label plus the default shell when one is discoverable).
        """
        if signal is not None:
            signal.throw_if_aborted()
        platform = "windows" if self.platform == "win32" else "posix"
        default_shell: Optional[str] = None
        if platform == "windows":
            default_shell = environment_value(
                dict(os.environ), "ComSpec") or None
        else:
            default_shell = environment_value(dict(os.environ), "SHELL") \
                or None
            if not default_shell:
                try:
                    pwd = __import__("pwd")  # posix-only, dynamic
                    getuid = getattr(os, "getuid")
                    default_shell = pwd.getpwuid(getuid()).pw_shell \
                        or None
                except (ImportError, KeyError, OSError, AttributeError):
                    default_shell = None
        result: Dict[str, str] = {"platform": platform}
        if default_shell is not None:
            result["defaultShell"] = default_shell
        return result

    # -- terminal ------------------------------------------------------------------

    def spawn_terminal(self, spec: Dict[str, Any]) -> Any:
        with self._lifecycle_lock:
            return self._spawn_terminal_locked(spec)

    def _spawn_terminal_locked(self, spec: Dict[str, Any]) -> Any:
        """Allocate one terminal session (the verbatim spawnTerminal
        wiring lives in the terminal module against this runtime's
        seams)."""
        if self._closed:
            raise RuntimeError("subprocess runtime is closed")
        from AstralNexus.runtime import subprocess_terminal
        return subprocess_terminal.spawn_terminal(self, spec)

    # -- lifecycle -----------------------------------------------------------------

    def dispose_managed_processes(self) -> None:
        """Bound the complete teardown; timeouts remain explicit failures."""
        timeout = float(self.internals.get("teardownTimeoutMs", 10000)) / 1000
        if timeout <= 0:
            raise ValueError("teardownTimeoutMs must be positive")
        deadline = time.monotonic() + timeout
        failures: List[BaseException] = []
        threads: List[threading.Thread] = []

        def run(action: Callable[[], None], name: str) -> None:
            def invoke() -> None:
                try:
                    action()
                except BaseException as error:
                    failures.append(error)
            thread = threading.Thread(target=invoke, daemon=True, name=name)
            thread.start()
            threads.append(thread)

        with self._lifecycle_lock:
            handles = list(self.managed)
            terminals = list(self.terminals)
            channels = list(self.control_channels)
        for handle in handles:
            def wait_one(bound: ManagedProcessHandle = handle) -> None:
                bound.terminate()
                if not bound.done_event.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError("subprocess outcome did not settle")
                bound.wait_for_exit()
                with self._lifecycle_lock:
                    self.managed.discard(bound)
            run(wait_one, "dispose-handle")
        for terminal in terminals:
            def term_one(bound: Any = terminal) -> None:
                bound.terminate().result()
                with self._lifecycle_lock:
                    self.terminals.discard(bound)
            run(term_one, "dispose-terminal")
        for channel in channels:
            def close_one(bound: Any = channel) -> None:
                destroy = getattr(bound, "destroy", None) or getattr(bound, "close")
                destroy()
                wait_closed = getattr(bound, "wait_closed", None)
                if wait_closed is not None:
                    wait_closed()
                with self._lifecycle_lock:
                    self.control_channels.discard(bound)
            run(close_one, "dispose-channel")
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
            if thread.is_alive():
                failures.append(TimeoutError(f"{thread.name} exceeded teardown deadline"))
        if failures:
            # Never let the best-effort OS sweep reintroduce an unbounded wait.
            threading.Thread(target=self.terminate_for_host_exit, daemon=True).start()
            if len(failures) == 1:
                raise failures[0]
            raise ExceptionGroup("local subprocess teardown failed", [
                failure if isinstance(failure, Exception) else RuntimeError(str(failure))
                for failure in failures])

    def terminate_for_host_exit(self) -> None:
        """Best-effort synchronous sweep for host exit (verbatim
        terminateForHostExit: ordinary ranges first, then terminals)."""
        with self._lifecycle_lock:
            handles = list(self.managed)
            terminals = list(self.terminals)
        for handle in handles:
            try:
                handle.terminate_for_host_exit()
            except BaseException:
                pass
        for terminal in terminals:
            try:
                terminal.terminate_for_host_exit()
            except BaseException:
                pass

    def close(self) -> None:
        """Service disposal: terminate managed ranges and refuse new
        spawns (the Cordis effect teardown adaptation)."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.dispose_managed_processes()
        finally:
            if self._host_exit_registered:
                import atexit
                atexit.unregister(self.terminate_for_host_exit)
                self._host_exit_registered = False
