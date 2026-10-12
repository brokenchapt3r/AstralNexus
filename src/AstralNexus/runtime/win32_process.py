"""Native port of @deepseek-ai/dsh-win32-process (commit 5badb150,
lib/index.js 761 lines read in full) — shared low-level Win32 process,
stdio, and Job Object primitives.

Upstream binds kernel32/advapi32 through Koffi FFI; the Python adaptation
binds the identical API set through ctypes (both are foreign-function
interfaces over the same __stdcall Win32 entry points). The binding table
keeps the upstream shape — a dict of callables — so every decision function
takes an injectable boundary and suites can pin it on any host, exactly as
upstream's ``api`` parameter does.

Contract surface (all 24 upstream exports):
- ``Win32Error`` with the exact ``{api} failed (Win32 {code})[: detail]``
  message, ``ERROR_INSUFFICIENT_BUFFER``, ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
- struct layouts with the upstream ABI guards (STARTUPINFOW 104 bytes,
  PROCESS_INFORMATION 24 bytes — supported Windows targets are 64-bit)
- ``error_text`` (FormatMessageW system message), ``throw_last_error`` /
  ``throw_win32``
- CommandLineToArgvW-exact ``quote_arg`` / ``build_command_line``;
  case-folded environment ordering and the UTF-16LE NUL-joined
  double-terminated ``encode_windows_environment`` block
- CRT descriptor-table encoding for one inherited control pipe
  (``inherited_control_stdio``) with the FOPEN/FPIPE/FDEV flags and the
  pipe-type check on the control descriptor
- pipe creation with caller-owned sets, ``drain_pipe`` (PeekNamedPipe poll
  with the 109/232 EOF codes), ``wait_for_process_exit``,
  ``poll_process_exit`` (258 WAIT_TIMEOUT → None)
- kill-on-close Job lifecycle: ``create_kill_on_close_job``,
  ``is_job_empty`` (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION ActiveProcesses),
  ``terminate_job``, ``probe_current_token_job_support``
- the shared suspended-create → Job-assign → resume lifecycle
  (``spawn_job_process``) with its complete failure-cleanup chain and
  best-effort inheritability restore, plus the three spawn shapes:
  ``spawn_piped_process`` (anonymous pipes + immediate stdin EOF),
  ``spawn_inherited_job_process`` (restricted token), and
  ``spawn_current_token_job_process`` (CREATE_UNICODE_ENVIRONMENT |
  CREATE_SUSPENDED with an explicit environment block)
- handle inspection: ``inherited_standard_handles`` (GetStdHandle),
  ``descriptor_handle`` / ``target_carrier_handles`` (fd → OS handle via
  the CRT, upstream's uv_get_osfhandle), ``is_null_ptr``,
  ``close_handle_checked``, ``close_best_effort``

Python adaptation notes (documented):
- Koffi alloc/free → ctypes objects whose lifetime is managed by holding
  Python references through the call (``free_native`` disappears; buffers
  are locals of the calling frame).
- ``uv_get_osfhandle`` → ``msvcrt.get_osfhandle`` (the same CRT operation).
- GetLastError → ``ctypes.get_last_error()`` captured immediately after the
  failing call (the binding table exposes it under the upstream name).
- async ``drain_pipe`` → synchronous (this host has no event-loop
  requirement; the 1 ms poll cadence and EOF codes are verbatim).
- Non-Windows platforms raise on binding load, mirroring upstream's lazy
  Koffi require that non-Windows processes never touch.
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time
from typing import (Any, Callable, Dict, Iterable, List, NoReturn, Optional,
                    Set, Tuple)

__all__ = [
    "ERROR_INSUFFICIENT_BUFFER", "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "Win32Error", "alloc_ptr_slot", "alloc_uint32", "build_command_line",
    "close_all_owned", "close_best_effort", "close_handle_checked",
    "close_owned", "create_kill_on_close_job", "create_pipe",
    "decode_ptr", "decode_uint32", "descriptor_handle", "drain_pipe",
    "encode_windows_environment", "error_text", "extend_win32_process_bindings",
    "inherited_control_stdio", "inherited_standard_handles", "is_job_empty",
    "is_null_ptr", "load_win32_process_bindings", "poll_process_exit",
    "probe_current_token_job_support", "quote_arg",
    "spawn_current_token_job_process", "spawn_inherited_job_process",
    "spawn_piped_process", "target_carrier_handles", "terminate_job",
    "throw_last_error", "throw_win32", "wait_for_process_exit",
]

#: Win32 code reporting a caller-provided buffer is too small.
ERROR_INSUFFICIENT_BUFFER = 122
#: Job limit that terminates every member when the final Job handle closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 8192

#: Windows CRT startup descriptor constants (verbatim upstream values).
HANDLE_BYTES = 8
INVALID_HANDLE = (1 << 64) - 1
FOPEN = 1
FPIPE = 8
FDEV = 64
FILE_TYPE_CHAR = 2
FILE_TYPE_PIPE = 3

#: uv/ CRT invalid-handle sentinels checked by descriptor_handle.
UV_INVALID_OS_FILE_HANDLE = (1 << 64) - 1
UV_INVALID_FILE_DESCRIPTOR = (1 << 64) - 2

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12

_STARTF_USESTDHANDLES = 0x00000001
_STARTF_USESHOWWINDOW = 0x00000002
_SW_HIDE = 0
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_INFINITE = 0xFFFFFFFF
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE_OFFSET = 16
_JobObjectExtendedLimitInformation = 9
_JobObjectBasicAccountingInformation = 1
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_FORMAT_MESSAGE_FROM_SYSTEM_IGNORE_INSERTS = 0x00001200


class Win32Error(Exception):
    """Win32 call failure with the exact API name and error code."""

    def __init__(self, api: str, win32_code: int,
                 detail: Optional[str] = None) -> None:
        suffix = "" if detail is None else f": {detail}"
        super().__init__(f"{api} failed (Win32 {win32_code}){suffix}")
        self.name = "Win32Error"
        #: Win32 function whose checked result failed.
        self.api = api
        #: Exact GetLastError value or direct Win32 API error code.
        self.win32_code = win32_code


def _platform_guard() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            "dsh-win32-process bindings require Windows; non-Windows hosts "
            "must select another containment mode")


# ------------------------------------------------------------- structures

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", _wt.DWORD),
            ("lpReserved", _wt.LPWSTR),
            ("lpDesktop", _wt.LPWSTR),
            ("lpTitle", _wt.LPWSTR),
            ("dwX", _wt.DWORD),
            ("dwY", _wt.DWORD),
            ("dwXSize", _wt.DWORD),
            ("dwYSize", _wt.DWORD),
            ("dwXCountChars", _wt.DWORD),
            ("dwYCountChars", _wt.DWORD),
            ("dwFillAttribute", _wt.DWORD),
            ("dwFlags", _wt.DWORD),
            ("wShowWindow", _wt.WORD),
            ("cbReserved2", _wt.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_uint8)),
            ("hStdInput", _wt.HANDLE),
            ("hStdOutput", _wt.HANDLE),
            ("hStdError", _wt.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", _wt.HANDLE),
            ("hThread", _wt.HANDLE),
            ("dwProcessId", _wt.DWORD),
            ("dwThreadId", _wt.DWORD),
        ]

    # ABI guards pinned by the native headers (upstream fact: supported
    # Windows targets are 64-bit, where these layouts are 104 and 24 bytes)
    assert ctypes.sizeof(STARTUPINFOW) == 104, (
        f"STARTUPINFOW layout mismatch: ctypes computed "
        f"{ctypes.sizeof(STARTUPINFOW)}, expected 104")
    assert ctypes.sizeof(PROCESS_INFORMATION) == 24, (
        f"PROCESS_INFORMATION layout mismatch: ctypes computed "
        f"{ctypes.sizeof(PROCESS_INFORMATION)}, expected 24")

    _HANDLE = _wt.HANDLE
    _PHANDLE = ctypes.POINTER(_wt.HANDLE)
    _PVOID = ctypes.c_void_p
    _PPVOID = ctypes.POINTER(_PVOID)
    _PDWORD = ctypes.POINTER(_wt.DWORD)
else:  # pragma: no cover — non-Windows hosts never touch the layouts
    STARTUPINFOW = None  # type: ignore[assignment]
    PROCESS_INFORMATION = None  # type: ignore[assignment]


def is_null_ptr(value: Any) -> bool:
    """Whether a handle value represents NULL (None or address zero)."""
    return value is None or value == 0


# --------------------------------------------------------------- bindings

_cached_bindings: Optional[Dict[str, Any]] = None


def _build_bindings() -> Dict[str, Any]:
    """Bind the shared kernel32/advapi32 operation table once."""
    _platform_guard()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    def last_error() -> int:
        return int(ctypes.get_last_error())

    close_handle = kernel32.CloseHandle
    close_handle.restype = _wt.BOOL
    close_handle.argtypes = [_HANDLE]

    get_file_type = kernel32.GetFileType
    get_file_type.restype = _wt.DWORD
    get_file_type.argtypes = [_HANDLE]

    format_message_w = kernel32.FormatMessageW
    format_message_w.restype = _wt.DWORD
    format_message_w.argtypes = [_wt.DWORD, _PVOID, _wt.DWORD, _wt.DWORD,
                                 _PVOID, _wt.DWORD, _PVOID]

    create_pipe = kernel32.CreatePipe
    create_pipe.restype = _wt.BOOL
    create_pipe.argtypes = [_PHANDLE, _PHANDLE, _PVOID, _wt.DWORD]

    set_handle_information = kernel32.SetHandleInformation
    set_handle_information.restype = _wt.BOOL
    set_handle_information.argtypes = [_HANDLE, _wt.DWORD, _wt.DWORD]

    create_process_w = kernel32.CreateProcessW
    create_process_w.restype = _wt.BOOL
    create_process_w.argtypes = [
        _wt.LPCWSTR, ctypes.c_wchar_p, _PVOID, _PVOID, _wt.BOOL, _wt.DWORD,
        _PVOID, _wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION)]

    create_process_as_user_w = advapi32.CreateProcessAsUserW
    create_process_as_user_w.restype = _wt.BOOL
    create_process_as_user_w.argtypes = [
        _HANDLE, _wt.LPCWSTR, ctypes.c_wchar_p, _PVOID, _PVOID, _wt.BOOL,
        _wt.DWORD, _PVOID, _wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION)]

    read_file = kernel32.ReadFile
    read_file.restype = _wt.BOOL
    read_file.argtypes = [_HANDLE, _PVOID, _wt.DWORD, _PDWORD, _PVOID]

    peek_named_pipe = kernel32.PeekNamedPipe
    peek_named_pipe.restype = _wt.BOOL
    peek_named_pipe.argtypes = [_HANDLE, _PVOID, _wt.DWORD, _PDWORD, _PDWORD,
                                _PDWORD]

    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.restype = _wt.DWORD
    wait_for_single_object.argtypes = [_HANDLE, _wt.DWORD]

    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.restype = _wt.BOOL
    get_exit_code_process.argtypes = [_HANDLE, _PDWORD]

    create_job_object_w = kernel32.CreateJobObjectW
    create_job_object_w.restype = _HANDLE
    create_job_object_w.argtypes = [_PVOID, _wt.LPCWSTR]

    set_information_job_object = kernel32.SetInformationJobObject
    set_information_job_object.restype = _wt.BOOL
    set_information_job_object.argtypes = [_HANDLE, ctypes.c_int, _PVOID,
                                           _wt.DWORD]

    query_information_job_object = kernel32.QueryInformationJobObject
    query_information_job_object.restype = _wt.BOOL
    query_information_job_object.argtypes = [_HANDLE, ctypes.c_int, _PVOID,
                                             _wt.DWORD, _PVOID]

    assign_process_to_job_object = kernel32.AssignProcessToJobObject
    assign_process_to_job_object.restype = _wt.BOOL
    assign_process_to_job_object.argtypes = [_HANDLE, _HANDLE]

    resume_thread = kernel32.ResumeThread
    resume_thread.restype = _wt.DWORD
    resume_thread.argtypes = [_HANDLE]

    terminate_process = kernel32.TerminateProcess
    terminate_process.restype = _wt.BOOL
    terminate_process.argtypes = [_HANDLE, _wt.DWORD]

    terminate_job_object = kernel32.TerminateJobObject
    terminate_job_object.restype = _wt.BOOL
    terminate_job_object.argtypes = [_HANDLE, _wt.DWORD]

    get_std_handle = kernel32.GetStdHandle
    get_std_handle.restype = _HANDLE
    get_std_handle.argtypes = [ctypes.c_int]

    import msvcrt

    return {
        "closeHandle": close_handle,
        "getLastError": last_error,
        "getFileType": get_file_type,
        "formatMessageW": format_message_w,
        "createPipe": create_pipe,
        "setHandleInformation": set_handle_information,
        "createProcessAsUserW": create_process_as_user_w,
        "createProcessW": create_process_w,
        "readFile": read_file,
        "peekNamedPipe": peek_named_pipe,
        "waitForSingleObject": wait_for_single_object,
        "getExitCodeProcess": get_exit_code_process,
        "createJobObjectW": create_job_object_w,
        "setInformationJobObject": set_information_job_object,
        "queryInformationJobObject": query_information_job_object,
        "assignProcessToJobObject": assign_process_to_job_object,
        "resumeThread": resume_thread,
        "terminateProcess": terminate_process,
        "terminateJobObject": terminate_job_object,
        "getStdHandle": get_std_handle,
        # upstream binds libuv's uv_get_osfhandle; the identical CRT
        # operation on this host is msvcrt.get_osfhandle
        "uvGetOsfhandle": msvcrt.get_osfhandle,
    }


def load_win32_process_bindings() -> Dict[str, Any]:
    """Load the generic process binding table without policy extensions."""
    global _cached_bindings
    if _cached_bindings is None:
        _cached_bindings = _build_bindings()
    return _cached_bindings


def extend_win32_process_bindings(create: Callable[[Dict[str, Any]], Dict[str, Any]]
                                  ) -> Dict[str, Any]:
    """Extend the shared process table with caller-owned Win32 API families.

    ``create`` receives the binding context (the loaded libraries) and
    returns only the caller-specific operations.
    """
    context = {"kernel32": ctypes.WinDLL("kernel32", use_last_error=True),
               "advapi32": ctypes.WinDLL("advapi32", use_last_error=True)}
    merged = dict(load_win32_process_bindings())
    merged.update(create(context))
    return merged


# ------------------------------------------------------- out-parameter slots

def alloc_ptr_slot() -> Any:
    """Allocate a pointer-sized out-parameter slot."""
    return _HANDLE()


def alloc_uint32() -> Any:
    """Allocate a uint32 out-parameter slot."""
    return _wt.DWORD()


def decode_ptr(slot: Any) -> Optional[int]:
    """Decode a pointer out-parameter; None for address zero."""
    value = slot.value
    return None if is_null_ptr(value) else int(value)


def decode_uint32(slot: Any) -> int:
    """Decode a uint32 out-parameter."""
    return int(slot.value)


# ------------------------------------------------------------ error surface

def error_text(api: Dict[str, Any], win32_code: int) -> str:
    """Format a Win32 error code through FormatMessageW.

    Returns the trimmed system message, or an empty string when
    unavailable.
    """
    buffer = ctypes.create_unicode_buffer(512)
    length = api["formatMessageW"](
        _FORMAT_MESSAGE_FROM_SYSTEM_IGNORE_INSERTS, None, win32_code, 0,
        buffer, 512, None)
    return "" if length == 0 else buffer.value.strip()


def throw_last_error(api: Dict[str, Any], name: str,
                     detail: Optional[str] = None) -> NoReturn:
    """Throw the current GetLastError value. Never returns."""
    win32_code = api["getLastError"]()
    raise Win32Error(name, win32_code,
                     detail if detail is not None
                     else error_text(api, win32_code))


def throw_win32(api: Dict[str, Any], name: str, win32_code: int,
                detail: Optional[str] = None) -> NoReturn:
    """Throw an explicitly captured Win32 error code. Never returns."""
    raise Win32Error(name, win32_code,
                     detail if detail is not None
                     else error_text(api, win32_code))


# ------------------------------------------------------ CRT control stdio

def inherited_control_stdio(api: Dict[str, Any], stdio: Dict[str, Any]) -> bytes:
    """Encode the CRT descriptor table before the child runtime allocates
    descriptors.

    Supported Windows targets use 64-bit handles. Empty slots remain closed,
    and the returned bytes must remain alive until CreateProcess returns.

    :param api: native file-type inspection for inherited handles.
    :param stdio: standard handles plus the provider-owned fd-7 control pipe.
    :returns: descriptor count, flag bytes, and handle values for
        STARTUPINFO's reserved CRT fields.
    :raises RuntimeError: when the control descriptor is not a Windows pipe.
    """
    control = stdio["control"]
    count = control["fileDescriptor"] + 1
    handle_offset = 4 + count
    data = bytearray(handle_offset + count * HANDLE_BYTES)
    struct.pack_into("<I", data, 0, count)
    for index in range(count):
        struct.pack_into("<Q", data, handle_offset + index * HANDLE_BYTES,
                         INVALID_HANDLE)
    entries: List[Tuple[int, int]] = [
        (0, stdio["stdin"]),
        (1, stdio["stdout"]),
        (2, stdio["stderr"]),
        (control["fileDescriptor"], control["handle"]),
    ]
    for fd, handle in entries:
        kind = api["getFileType"](handle)
        if fd == control["fileDescriptor"] and kind != FILE_TYPE_PIPE:
            raise RuntimeError(
                "subprocess control descriptor is not a Windows pipe")
        flags = FOPEN | (FPIPE if kind == FILE_TYPE_PIPE
                         else FDEV if kind == FILE_TYPE_CHAR else 0)
        data[4 + fd] = flags
        struct.pack_into("<Q", data, handle_offset + fd * HANDLE_BYTES,
                         int(handle))
    return bytes(data)


# ------------------------------------------------------- command line / env

def quote_arg(argument: str) -> str:
    """Quote one argument according to CommandLineToArgvW parsing."""
    if argument == "":
        return '""'
    if not any(char.isspace() or char == '"' for char in argument):
        return argument
    quoted = '"'
    index = 0
    length = len(argument)
    while index < length:
        backslashes = 0
        while index < length and argument[index] == "\\":
            backslashes += 1
            index += 1
        if index == length:
            quoted += "\\" * (backslashes * 2)
        elif argument[index] == '"':
            quoted += "\\" * (backslashes * 2 + 1) + '"'
            index += 1
        else:
            quoted += "\\" * backslashes + argument[index]
            index += 1
    return quoted + '"'


def build_command_line(program: str, args: Iterable[str]) -> str:
    """Build the mutable command line accepted by CreateProcessW."""
    return " ".join(quote_arg(entry) for entry in [program, *args])


def compare_windows_environment_keys(left: Tuple[str, Any],
                                     right: Tuple[str, Any]) -> int:
    """Case-insensitive environment-key ordering (verbatim fold rule)."""
    folded_left = left[0].upper()
    folded_right = right[0].upper()
    return -1 if folded_left < folded_right else (
        1 if folded_left > folded_right else 0)


def encode_windows_environment(env: Dict[str, str]) -> bytes:
    """Encode the CREATE_UNICODE_ENVIRONMENT block: case-folded sorted
    ``KEY=VALUE`` entries, NUL-joined and double-NUL terminated, UTF-16LE."""
    import functools
    entries = sorted(env.items(),
                     key=functools.cmp_to_key(compare_windows_environment_keys))
    strings = [f"{key}={value}" for key, value in entries]
    return ("\0".join(strings) + "\0\0").encode("utf-16-le")


# -------------------------------------------------------------- handle ops

def close_best_effort(api: Dict[str, Any], handle: Optional[int]) -> None:
    """Close a handle when non-null, ignoring the result."""
    if not is_null_ptr(handle):
        api["closeHandle"](handle)


def create_pipe(api: Dict[str, Any], owned: Set[int]) -> Dict[str, int]:
    """Create one anonymous pipe, registering both ends in ``owned``.

    Out-parameter slots are passed as ctypes instances; the real binding's
    declared POINTER argtypes pass them by reference automatically, and an
    injected fake mutates ``slot.value`` directly.
    """
    read_slot = alloc_ptr_slot()
    write_slot = alloc_ptr_slot()
    if api["createPipe"](read_slot, write_slot, None, 0) == 0:
        throw_last_error(api, "CreatePipe")
    read = decode_ptr(read_slot)
    write = decode_ptr(write_slot)
    if read is None or write is None:
        close_best_effort(api, read)
        close_best_effort(api, write)
        throw_last_error(api, "CreatePipe", "null pipe handle")
    owned.add(read)
    owned.add(write)
    return {"read": read, "write": write}


def close_owned(api: Dict[str, Any], owned: Set[int], handle: int) -> None:
    """Close one uniquely owned handle when still in the set."""
    if handle not in owned:
        return
    owned.discard(handle)
    api["closeHandle"](handle)


def close_all_owned(api: Dict[str, Any], owned: Set[int]) -> None:
    """Close every remaining owned handle and clear the set."""
    for handle in owned:
        api["closeHandle"](handle)
    owned.clear()


def close_handle_checked(api: Dict[str, Any], handle: int,
                         detail: str) -> None:
    """Close a caller-owned handle and report a labelled Win32 failure."""
    if api["closeHandle"](handle) == 0:
        throw_last_error(api, "CloseHandle", detail)


# ------------------------------------------------------------ process ops

def _startup_info(*, stdio: Dict[str, Any],
                  control_block: Optional[Tuple[ctypes.Array, int]] = None
                  ) -> Any:
    info = STARTUPINFOW()
    ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
    info.cb = 104
    info.dwFlags = _STARTF_USESTDHANDLES | _STARTF_USESHOWWINDOW
    info.wShowWindow = _SW_HIDE
    info.hStdInput = stdio["stdin"]
    info.hStdOutput = stdio["stdout"]
    info.hStdError = stdio["stderr"]
    if control_block is not None:
        pointer, length = control_block
        info.cbReserved2 = length
        info.lpReserved2 = ctypes.cast(pointer,
                                       ctypes.POINTER(ctypes.c_uint8))
    return info


def spawn_piped_process(api: Dict[str, Any], options: Dict[str, Any]
                        ) -> Dict[str, Any]:
    """Spawn with anonymous-pipe stdout/stderr and immediate stdin EOF.

    New console windows start hidden without changing console inheritance.

    :param api: active binding table.
    :param options: command, cwd, args, and restricted primary token.
    :returns: caller-owned ``{pid, process, stdoutRead, stderrRead}``.
    """
    owned: Set[int] = set()
    try:
        std_in = create_pipe(api, owned)
        std_out = create_pipe(api, owned)
        std_err = create_pipe(api, owned)
        for handle, label in ((std_in["read"], "stdin read end"),
                              (std_out["write"], "stdout write end"),
                              (std_err["write"], "stderr write end")):
            if api["setHandleInformation"](handle, 1, 1) == 0:
                throw_last_error(api, "SetHandleInformation", label)
        startup_info = _startup_info(stdio={
            "stdin": std_in["read"], "stdout": std_out["write"],
            "stderr": std_err["write"]})
        process_info = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(
            build_command_line(options["command"], options["args"]))
        created = api["createProcessAsUserW"](
            options["token"], None, command_line, None, None, 1, 0, None,
            options["cwd"], startup_info, process_info)
        if created == 0:
            throw_win32(api, "CreateProcessAsUserW", api["getLastError"](),
                        f"command: {options['command']}, cwd: {options['cwd']}")
        if process_info.hProcess is None or process_info.hThread is None:
            if process_info.hProcess is not None:
                api["terminateProcess"](process_info.hProcess, 1)
            close_best_effort(api, process_info.hThread)
            close_best_effort(api, process_info.hProcess)
            raise RuntimeError(
                "CreateProcessAsUserW succeeded but returned null "
                f"process/thread handles (pid {process_info.dwProcessId})")
        result = {
            "pid": int(process_info.dwProcessId),
            "process": int(process_info.hProcess),
            "stdoutRead": int(std_out["read"]),
            "stderrRead": int(std_err["read"]),
        }
        # the child ends are now the child's; stdin closes both ends for the
        # immediate-EOF contract
        close_owned(api, owned, std_in["read"])
        close_owned(api, owned, std_in["write"])
        close_owned(api, owned, std_out["write"])
        close_owned(api, owned, std_err["write"])
        close_best_effort(api, process_info.hThread)
        owned.discard(std_out["read"])
        owned.discard(std_err["read"])
        return result
    except BaseException:
        close_all_owned(api, owned)
        raise


def drain_pipe(api: Dict[str, Any], handle: int) -> bytes:
    """Drain one anonymous pipe until the writer closes it.

    Returns the complete bytes read before EOF; the handle is always
    closed. Synchronous adaptation of the upstream async poll loop (1 ms
    cadence; PeekNamedPipe failures 109/232 are the writer-closed EOF).
    """
    chunks: List[bytes] = []
    count_slot = alloc_uint32()
    try:
        while True:
            if api["peekNamedPipe"](handle, None, 0, None, count_slot,
                                    None) == 0:
                win32_code = api["getLastError"]()
                if win32_code in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                    break
                throw_last_error(
                    api, "PeekNamedPipe",
                    f"drain failure after {len(chunks)} chunk(s)")
            available = decode_uint32(count_slot)
            if available > 0:
                chunk = ctypes.create_string_buffer(available)
                if api["readFile"](handle, chunk, available, count_slot,
                                   None) == 0:
                    throw_last_error(
                        api, "ReadFile",
                        f"drain failure after {len(chunks)} chunk(s)")
                chunks.append(chunk.raw[:decode_uint32(count_slot)])
            time.sleep(0.001)
        return b"".join(chunks)
    finally:
        api["closeHandle"](handle)


def wait_for_process_exit(api: Dict[str, Any], process: int) -> int:
    """Wait for a process and always close its handle; return its direct
    exit code."""
    exit_slot = alloc_uint32()
    try:
        if api["waitForSingleObject"](process, _INFINITE) == _WAIT_FAILED:
            throw_last_error(api, "WaitForSingleObject")
        if api["getExitCodeProcess"](process, exit_slot) == 0:
            throw_last_error(api, "GetExitCodeProcess")
        return decode_uint32(exit_slot)
    finally:
        api["closeHandle"](process)


def poll_process_exit(api: Dict[str, Any], process: int) -> Optional[int]:
    """Poll one process handle without blocking; the direct exit code when
    signalled, or None while running (258 WAIT_TIMEOUT)."""
    wait_result = api["waitForSingleObject"](process, 0)
    if wait_result == _WAIT_TIMEOUT:
        return None
    if wait_result == _WAIT_FAILED:
        throw_last_error(api, "WaitForSingleObject")
    exit_slot = alloc_uint32()
    if api["getExitCodeProcess"](process, exit_slot) == 0:
        throw_last_error(api, "GetExitCodeProcess")
    return decode_uint32(exit_slot)


# ------------------------------------------------------------- Job objects

def create_kill_on_close_job(api: Dict[str, Any]) -> int:
    """Create one unnamed kill-on-close Job Object."""
    job = api["createJobObjectW"](None, None)
    if is_null_ptr(job):
        throw_last_error(api, "CreateJobObjectW")
    information = bytearray(144)
    struct.pack_into("<I", information,
                     _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE_OFFSET,
                     JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    buffer = (ctypes.c_char * len(information)).from_buffer(information)
    if api["setInformationJobObject"](
            job, _JobObjectExtendedLimitInformation, buffer,
            len(information)) == 0:
        win32_code = api["getLastError"]()
        api["closeHandle"](job)
        throw_win32(api, "SetInformationJobObject", win32_code)
    return int(job)


def is_job_empty(api: Dict[str, Any], job: int) -> bool:
    """Whether a Job has no active processes (basic accounting
    ActiveProcesses at offset 40)."""
    information = bytearray(48)
    buffer = (ctypes.c_char * len(information)).from_buffer(information)
    if api["queryInformationJobObject"](
            job, _JobObjectBasicAccountingInformation, buffer,
            len(information), None) == 0:
        throw_last_error(api, "QueryInformationJobObject",
                         "active process count")
    return struct.unpack_from("<I", information, 40)[0] == 0


def terminate_job(api: Dict[str, Any], job: int, exit_code: int) -> None:
    """Terminate every process in a Job with a direct Windows exit code."""
    if api["terminateJobObject"](job, exit_code) == 0:
        throw_last_error(api, "TerminateJobObject")


def probe_current_token_job_support(api: Dict[str, Any]) -> None:
    """Verify an unnamed kill-on-close Job can be created and released now."""
    close_handle_checked(api, create_kill_on_close_job(api),
                         "current-token Job capability probe")


# ------------------------------------------------------- stdio resolution

def descriptor_handle(api: Dict[str, Any], file_descriptor: int,
                      label: str) -> int:
    """Resolve one CRT descriptor to its OS handle, rejecting the invalid
    sentinels."""
    handle = api["uvGetOsfhandle"](file_descriptor)
    if (is_null_ptr(handle) or handle == UV_INVALID_OS_FILE_HANDLE
            or handle == UV_INVALID_FILE_DESCRIPTOR):
        raise RuntimeError(
            "uv_get_osfhandle returned an invalid handle for target "
            f"{label} fd {file_descriptor}")
    return int(handle)


def inherited_standard_handles(api: Dict[str, Any],
                               control_file_descriptor: Optional[int] = None
                               ) -> Dict[str, Any]:
    """Read the current process's standard handles (and optionally the
    control descriptor's handle)."""
    def get(selector: int, label: str) -> int:
        handle = api["getStdHandle"](selector)
        if not is_null_ptr(handle):
            return int(handle)
        throw_last_error(api, "GetStdHandle", f"null {label} handle")
        raise AssertionError("unreachable")  # for type checkers

    stdio: Dict[str, Any] = {
        "stdin": get(_STD_INPUT_HANDLE, "stdin"),
        "stdout": get(_STD_OUTPUT_HANDLE, "stdout"),
        "stderr": get(_STD_ERROR_HANDLE, "stderr"),
    }
    if control_file_descriptor is not None:
        stdio["control"] = {
            "fileDescriptor": control_file_descriptor,
            "handle": descriptor_handle(api, control_file_descriptor,
                                        "control"),
        }
    return stdio


def target_carrier_handles(api: Dict[str, Any], descriptors: Dict[str, Any]
                           ) -> Dict[str, Any]:
    """Resolve the target carrier descriptors to OS handles.

    Each standard-stream entry accepts either a CRT file descriptor
    (resolved through uv_get_osfhandle like the upstream runner) or a
    ``{"handle": N}`` mapping when the caller already owns the OS handle
    (the Python-parent adaptation: no CRT descriptor round-trip). The
    control entry keeps the upstream ``{"fileDescriptor", "handle"}``-capable
    shape.
    """
    def resolve_one(descriptor: Any, label: str) -> int:
        if isinstance(descriptor, dict):
            handle = descriptor.get("handle")
            if handle is None:
                handle = descriptor_handle(api,
                                           descriptor["fileDescriptor"],
                                           label)
            return int(handle)
        return descriptor_handle(api, descriptor, label)

    handles: Dict[str, Any] = {
        "stdin": resolve_one(descriptors["stdin"], "stdin"),
        "stdout": resolve_one(descriptors["stdout"], "stdout"),
        "stderr": resolve_one(descriptors["stderr"], "stderr"),
    }
    if descriptors.get("control") is not None:
        handles["control"] = {
            "fileDescriptor": descriptors["control"],
            "handle": descriptor_handle(api, descriptors["control"],
                                        "control"),
        }
    return handles


# ------------------------------------------------- shared Job spawn lifecycle

def spawn_job_process(api: Dict[str, Any], options: Dict[str, Any],
                      resolve_stdio: Callable[[], Dict[str, Any]],
                      create_name: str,
                      create: Callable[[Any, Any], int]) -> Dict[str, Any]:
    """Shared suspended-create, Job-assignment, and resume lifecycle.

    The inheritability bits required by STARTF_USESTDHANDLES are enabled
    around the create and restored best-effort afterwards: a restore
    failure must not replace the already-created child's outcome.
    """
    job = create_kill_on_close_job(api)
    enabled: List[int] = []
    control_keepalive: Optional[ctypes.Array] = None
    created = 0
    create_failure_code = 0
    startup_info = None
    process_info = PROCESS_INFORMATION()
    try:
        stdio = resolve_stdio()
        inherited: List[Tuple[int, str]] = [
            (stdio["stdin"], "stdin"), (stdio["stdout"], "stdout"),
            (stdio["stderr"], "stderr")]
        if stdio.get("control") is not None:
            inherited.append((stdio["control"]["handle"], "control"))
        for handle, label in inherited:
            if api["setHandleInformation"](handle, 1, 1) == 0:
                throw_last_error(api, "SetHandleInformation",
                                 f"{label} (enable inherit)")
            enabled.append(handle)
        control_block: Optional[Tuple[ctypes.Array, int]] = None
        if stdio.get("control") is not None:
            control_bytes = inherited_control_stdio(api, stdio)
            keepalive = (ctypes.c_char * len(control_bytes))()
            ctypes.memmove(keepalive, control_bytes, len(control_bytes))
            control_block = (keepalive, len(control_bytes))
        startup_info = _startup_info(stdio=stdio, control_block=control_block)
        created = create(startup_info, process_info)
        if created == 0:
            create_failure_code = api["getLastError"]()
    except BaseException:
        api["closeHandle"](job)
        raise
    finally:
        for handle in enabled:
            api["setHandleInformation"](handle, 1, 0)
    if created == 0:
        api["closeHandle"](job)
        throw_win32(api, create_name, create_failure_code,
                    f"command: {options['command']}, cwd: {options['cwd']}")
    if process_info.hProcess is None or process_info.hThread is None:
        if process_info.hProcess is not None:
            api["terminateProcess"](process_info.hProcess, 1)
        api["closeHandle"](job)
        close_best_effort(api, process_info.hThread)
        close_best_effort(api, process_info.hProcess)
        raise RuntimeError(
            f"{create_name} succeeded but returned null process/thread "
            f"handles (pid {process_info.dwProcessId})")
    if api["assignProcessToJobObject"](job, process_info.hProcess) == 0:
        win32_code = api["getLastError"]()
        api["terminateProcess"](process_info.hProcess, 1)
        close_best_effort(api, process_info.hThread)
        close_best_effort(api, process_info.hProcess)
        api["closeHandle"](job)
        throw_win32(api, "AssignProcessToJobObject", win32_code,
                    f"pid {process_info.dwProcessId}")
    if api["resumeThread"](process_info.hThread) == _INFINITE:
        win32_code = api["getLastError"]()
        close_best_effort(api, process_info.hThread)
        close_best_effort(api, process_info.hProcess)
        api["closeHandle"](job)
        throw_win32(api, "ResumeThread", win32_code,
                    f"pid {process_info.dwProcessId}")
    close_best_effort(api, process_info.hThread)
    return {
        "pid": int(process_info.dwProcessId),
        "process": int(process_info.hProcess),
        "job": int(job),
    }


def spawn_inherited_job_process(api: Dict[str, Any], options: Dict[str, Any]
                                ) -> Dict[str, Any]:
    """Spawn a restricted-token process suspended with hidden initial
    windows, assign its Job, then resume it."""
    command_line_text = build_command_line(options["command"], options["args"])

    def create(startup_info: Any, process_info: Any) -> int:
        command_line = ctypes.create_unicode_buffer(command_line_text)
        return api["createProcessAsUserW"](
            options["token"], None, command_line, None, None, 1,
            _CREATE_SUSPENDED, None, options["cwd"],
            startup_info, process_info)

    return spawn_job_process(
        api, options,
        lambda: inherited_standard_handles(api,
                                           options.get("controlFileDescriptor")),
        "CreateProcessAsUserW", create)


def spawn_current_token_job_process(api: Dict[str, Any],
                                    options: Dict[str, Any]
                                    ) -> Dict[str, Any]:
    """Spawn an ordinary process suspended with hidden initial windows,
    assign its Job, then resume it (CREATE_UNICODE_ENVIRONMENT |
    CREATE_SUSPENDED with the explicit environment block)."""
    command_line_text = build_command_line(options["command"], options["args"])
    environment = encode_windows_environment(options["env"])

    def create(startup_info: Any, process_info: Any) -> int:
        command_line = ctypes.create_unicode_buffer(command_line_text)
        env_buffer = (ctypes.c_char * len(environment)).from_buffer_copy(
            environment)
        return api["createProcessW"](
            options.get("applicationName"), command_line, None, None, 1,
            _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT, env_buffer,
            options["cwd"], startup_info, process_info)

    return spawn_job_process(
        api, options,
        lambda: target_carrier_handles(api, options["stdio"]),
        "CreateProcessW", create)
