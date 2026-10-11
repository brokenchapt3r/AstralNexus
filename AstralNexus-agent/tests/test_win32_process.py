"""Tests for runtime/win32_process.py — the native dsh-win32-process port.

Follows upstream's own strategy for thin native bindings: decision logic is
pinned with an injectable fake API table on any host, while the real ctypes
surface (struct ABI guards, bindings load, Job primitives, a real
suspended-create + Job-assign + resume spawn) is exercised on win32.
"""
import os
import struct
import sys
from typing import Any, Dict, List, Optional, Set

import pytest

from AstralNexus.runtime import win32_process as wp

IS_WIN32 = sys.platform == "win32"


# ---------------------------------------------------------------- helpers

class FakeApi:
    """Records calls and replays canned Win32 results."""

    def __init__(self, **overrides: Any) -> None:
        self.calls: List[str] = []
        self.closed: List[int] = []
        self.handle_sequence = iter(range(1000, 2000))
        table: Dict[str, Any] = {
            "closeHandle": self._close,
            "getLastError": lambda: 0,
            "getFileType": lambda handle: wp.FILE_TYPE_CHAR,
            "formatMessageW": lambda *args: 0,
            "createPipe": self._create_pipe,
            "setHandleInformation": lambda *args: 1,
            "createProcessAsUserW": self._create_process,
            "createProcessW": self._create_process,
            "readFile": self._read_file,
            "peekNamedPipe": self._peek,
            "waitForSingleObject": lambda handle, ms: 0,
            "getExitCodeProcess": self._exit_code,
            "createJobObjectW": lambda *args: next(self.handle_sequence),
            "setInformationJobObject": lambda *args: 1,
            "queryInformationJobObject": self._query_job,
            "assignProcessToJobObject": lambda *args: 1,
            "resumeThread": lambda handle: 1,
            "terminateProcess": lambda *args: 1,
            "terminateJobObject": lambda *args: 1,
            "getStdHandle": lambda selector: 500 - selector,
            "uvGetOsfhandle": lambda fd: 900 + fd,
        }
        table.update(overrides)
        self.table = table
        self.pipe_data: List[bytes] = []
        self.peek_results: List[Any] = []
        self.exit_code_value = 0
        self.process_handles: Dict[int, int] = {}
        self.job_information = bytearray(48)
        self.created: List[Dict[str, Any]] = []

    def _close(self, handle: int) -> int:
        self.calls.append("closeHandle")
        self.closed.append(handle)
        return 1

    def _create_pipe(self, read_slot: Any, write_slot: Any, *args: Any) -> int:
        self.calls.append("createPipe")
        read_slot.value = next(self.handle_sequence)
        write_slot.value = next(self.handle_sequence)
        return 1

    def _create_process(self, *args: Any) -> int:
        self.calls.append("createProcess")
        # argtypes-declared POINTER parameters receive the ctypes instance
        # itself through the injected fake table
        pi = args[-1]
        pi.hProcess = next(self.handle_sequence)
        pi.hThread = next(self.handle_sequence)
        pi.dwProcessId = 4242
        self.created.append({"args": args})
        return 1

    def _read_file(self, handle: int, buffer: Any, length: int,
                   read_slot: Any, *args: Any) -> int:
        self.calls.append("readFile")
        data = self.pipe_data.pop(0) if self.pipe_data else b""
        ctypes_mod = sys.modules["ctypes"]
        ctypes_mod.memmove(buffer, data, len(data))
        read_slot.value = len(data)
        return 1

    def _peek(self, handle: int, *args: Any) -> int:
        self.calls.append("peekNamedPipe")
        if not self.peek_results:
            return 1
        result = self.peek_results.pop(0)
        if isinstance(result, tuple):  # (BOOL return, totalBytesAvail)
            # args: buffer, size, bytesRead, totalBytesAvail, bytesLeft
            count_slot = args[3]
            count_slot.value = result[1]
            return result[0]
        return result

    def _exit_code(self, handle: int, slot: Any) -> int:
        self.calls.append("getExitCodeProcess")
        slot.value = self.exit_code_value
        return 1

    def _query_job(self, job: int, kind: int, buffer: Any, length: int,
                   returned: Any) -> int:
        self.calls.append("queryInformationJobObject")
        import ctypes
        ctypes.memmove(buffer, bytes(self.job_information), length)
        return 1


# ------------------------------------------------------------------ errors

def test_win32_error_message_shape() -> None:
    error = wp.Win32Error("CreatePipe", 5)
    assert str(error) == "CreatePipe failed (Win32 5)"
    assert error.api == "CreatePipe"
    assert error.win32_code == 5
    with_detail = wp.Win32Error("ReadFile", 109, "drain failure")
    assert str(with_detail) == "ReadFile failed (Win32 109): drain failure"


def test_throw_last_error_uses_detail_or_formatted_text() -> None:
    api = FakeApi(getLastError=lambda: 122,
                  formatMessageW=lambda *args: 0).table
    with pytest.raises(wp.Win32Error) as caught:
        wp.throw_last_error(api, "QueryInformationJobObject")
    assert caught.value.win32_code == 122
    with pytest.raises(wp.Win32Error) as detailed:
        wp.throw_last_error(api, "CreatePipe", "explicit detail")
    assert str(detailed.value).endswith(": explicit detail")


def test_error_text_reads_format_message() -> None:
    import ctypes
    captured: Dict[str, Any] = {}

    def fake_format(flags: int, source: Any, code: int, lang: int,
                    buffer: Any, size: int, arguments: Any) -> int:
        captured["flags"] = flags
        captured["code"] = code
        text = "The data area passed to a system call is too small."
        encoded = text.encode("utf-16-le")
        ctypes.memmove(buffer, encoded, len(encoded))
        return len(text)

    api = FakeApi(formatMessageW=fake_format).table
    assert wp.error_text(api, 122) == \
        "The data area passed to a system call is too small."
    assert captured["flags"] == 0x00001200
    assert captured["code"] == 122
    empty = FakeApi(formatMessageW=lambda *args: 0).table
    assert wp.error_text(empty, 1) == ""


# ------------------------------------------------------------ quoting / env

@pytest.mark.parametrize(("raw", "expected"), [
    ("", '""'),
    ("plain", "plain"),
    ("back\\", "back\\"),
    ("two words", '"two words"'),
    ('say "hi"', r'"say \"hi\""'),
    ("x \\", r'"x \\"'),
    ('a\\"b', r'"a\\\"b"'),
    ("tab\there", '"tab\there"'),
])
def test_quote_arg_matches_command_line_to_argv_w(raw: str,
                                                  expected: str) -> None:
    assert wp.quote_arg(raw) == expected


def test_build_command_line_leaves_program_verbatim() -> None:
    assert wp.build_command_line("prog", ["a b", "c"]) == 'prog "a b" c'
    assert wp.build_command_line("prog", []) == "prog"


def test_environment_ordering_folds_case() -> None:
    assert wp.compare_windows_environment_keys(("a", "1"), ("B", "2")) == -1
    assert wp.compare_windows_environment_keys(("B", "1"), ("a", "2")) == 1
    assert wp.compare_windows_environment_keys(("a", "1"), ("A", "2")) == 0


def test_encode_windows_environment_block() -> None:
    block = wp.encode_windows_environment({"b": "2", "A": "1", "a2": "x"})
    assert block.decode("utf-16-le") == "A=1\0a2=x\0b=2\0\0"
    # an empty environment is still double-NUL terminated (utf16le: 4 bytes)
    assert wp.encode_windows_environment({}) == "\0\0".encode("utf-16-le")


# ---------------------------------------------------------- control stdio

def test_control_stdio_encodes_crt_descriptor_table() -> None:
    seen: List[int] = []

    def file_type(handle: int) -> int:
        seen.append(handle)
        return wp.FILE_TYPE_PIPE if handle == 777 else wp.FILE_TYPE_CHAR

    api = FakeApi(getFileType=file_type).table
    data = wp.inherited_control_stdio(api, {
        "stdin": 111, "stdout": 222, "stderr": 333,
        "control": {"fileDescriptor": 7, "handle": 777}})
    assert len(data) == 4 + 8 + 8 * 8
    assert struct.unpack_from("<I", data, 0)[0] == 8
    assert data[4 + 0] == wp.FOPEN | wp.FDEV
    assert data[4 + 7] == wp.FOPEN | wp.FPIPE
    assert struct.unpack_from("<Q", data, 4 + 8 + 7 * 8)[0] == 777
    # empty slots stay the invalid sentinel
    assert struct.unpack_from("<Q", data, 4 + 8 + 3 * 8)[0] == wp.INVALID_HANDLE
    assert seen == [111, 222, 333, 777]


def test_control_stdio_requires_pipe_control_descriptor() -> None:
    api = FakeApi(getFileType=lambda handle: wp.FILE_TYPE_CHAR).table
    with pytest.raises(RuntimeError,
                       match="control descriptor is not a Windows pipe"):
        wp.inherited_control_stdio(api, {
            "stdin": 111, "stdout": 222, "stderr": 333,
            "control": {"fileDescriptor": 7, "handle": 777}})


# ------------------------------------------------------------ handle ops

def test_create_pipe_registers_both_ends() -> None:
    fake = FakeApi()
    owned: Set[int] = set()
    pipe = wp.create_pipe(fake.table, owned)
    assert pipe["read"] in owned and pipe["write"] in owned
    assert pipe["read"] != pipe["write"]


def test_create_pipe_failure_raises_win32_error() -> None:
    def fail(*args: Any) -> int:
        return 0

    api = FakeApi(createPipe=fail, getLastError=lambda: 5).table
    with pytest.raises(wp.Win32Error) as caught:
        wp.create_pipe(api, set())
    assert caught.value.api == "CreatePipe"


def test_create_pipe_null_handle_cleans_up() -> None:
    def null_write(read_slot: Any, write_slot: Any, *args: Any) -> int:
        read_slot.value = 77
        write_slot.value = None
        return 1

    fake = FakeApi(createPipe=null_write)
    with pytest.raises(wp.Win32Error):
        wp.create_pipe(fake.table, set())
    assert 77 in fake.closed


def test_close_owned_and_close_all_owned() -> None:
    fake = FakeApi()
    owned = {11, 22}
    wp.close_owned(fake.table, owned, 11)
    assert owned == {22}
    assert fake.closed == [11]
    wp.close_owned(fake.table, owned, 99)  # not owned: untouched
    assert fake.closed == [11]
    wp.close_all_owned(fake.table, owned)
    assert owned == set()
    assert fake.closed == [11, 22]


def test_close_handle_checked_reports_label() -> None:
    api = FakeApi(closeHandle=lambda handle: 0,
                  getLastError=lambda: 6).table
    with pytest.raises(wp.Win32Error) as caught:
        wp.close_handle_checked(api, 5, "job release")
    assert "job release" in str(caught.value)


def test_close_best_effort_ignores_null_and_failures() -> None:
    closed: List[int] = []

    def failing_close(handle: int) -> int:
        closed.append(handle)
        return 0

    fake = FakeApi(closeHandle=failing_close)
    wp.close_best_effort(fake.table, None)
    wp.close_best_effort(fake.table, 0)
    wp.close_best_effort(fake.table, 12)
    assert closed == [12]


# ------------------------------------------------------------------- pipes

def test_drain_pipe_polls_until_writer_closed() -> None:
    fake = FakeApi()
    fake.peek_results = [(1, 5), (1, 3), (0, 0)]  # data, data, then EOF
    fake.table["getLastError"] = lambda: 109
    fake.pipe_data = [b"hello", b"abc"]
    # third peek returns 0 with GetLastError 109 (broken pipe)
    result = wp.drain_pipe(fake.table, 42)
    assert result == b"helloabc"
    assert 42 in fake.closed  # always closes


def test_drain_pipe_no_data_code_also_ends() -> None:
    fake = FakeApi()
    fake.peek_results = [0]
    fake.table["getLastError"] = lambda: 232
    assert wp.drain_pipe(fake.table, 42) == b""
    assert 42 in fake.closed


def test_drain_pipe_unexpected_peek_failure_throws() -> None:
    fake = FakeApi()
    fake.peek_results = [0]
    fake.table["getLastError"] = lambda: 5
    with pytest.raises(wp.Win32Error, match="PeekNamedPipe"):
        wp.drain_pipe(fake.table, 42)
    assert 42 in fake.closed  # closed even on failure


# ------------------------------------------------------------- process ops

def test_wait_for_process_exit_returns_code_and_closes() -> None:
    fake = FakeApi()
    fake.exit_code_value = 7
    assert wp.wait_for_process_exit(fake.table, 31) == 7
    assert 31 in fake.closed


def test_wait_for_process_exit_wait_failure_raises_but_closes() -> None:
    fake = FakeApi(waitForSingleObject=lambda handle, ms: wp._WAIT_FAILED,
                   getLastError=lambda: 6)
    with pytest.raises(wp.Win32Error, match="WaitForSingleObject"):
        wp.wait_for_process_exit(fake.table, 31)
    assert 31 in fake.closed


def test_poll_process_exit_maps_wait_timeout_to_none() -> None:
    fake = FakeApi(waitForSingleObject=lambda handle, ms: 258)
    assert wp.poll_process_exit(fake.table, 31) is None
    assert fake.closed == []


def test_poll_process_exit_signalled_returns_code() -> None:
    fake = FakeApi(waitForSingleObject=lambda handle, ms: 0)
    fake.exit_code_value = 3
    assert wp.poll_process_exit(fake.table, 31) == 3


def test_poll_process_exit_wait_failed_raises() -> None:
    fake = FakeApi(waitForSingleObject=lambda handle, ms: wp._WAIT_FAILED,
                   getLastError=lambda: 6)
    with pytest.raises(wp.Win32Error):
        wp.poll_process_exit(fake.table, 31)


# ------------------------------------------------------------- job objects

def test_create_kill_on_close_job_sets_limit_at_offset_16() -> None:
    captured: Dict[str, Any] = {}

    def set_info(job: int, kind: int, buffer: Any, length: int) -> int:
        import ctypes
        captured["kind"] = kind
        captured["length"] = length
        captured["limit"] = struct.unpack_from(
            "<I", bytes(ctypes.string_at(buffer, length)), 16)[0]
        return 1

    api = FakeApi(setInformationJobObject=set_info).table
    job = wp.create_kill_on_close_job(api)
    assert job >= 1000
    assert captured["kind"] == 9
    assert captured["length"] == 144
    assert captured["limit"] == wp.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_create_kill_on_close_job_failure_closes_handle() -> None:
    fake = FakeApi(setInformationJobObject=lambda *args: 0,
                   getLastError=lambda: 5)
    with pytest.raises(wp.Win32Error, match="SetInformationJobObject"):
        wp.create_kill_on_close_job(fake.table)
    assert fake.closed  # the job handle was released


def test_is_job_empty_reads_active_processes() -> None:
    fake = FakeApi()
    struct.pack_into("<I", fake.job_information, 40, 0)
    assert wp.is_job_empty(fake.table, 9) is True
    struct.pack_into("<I", fake.job_information, 40, 2)
    assert wp.is_job_empty(fake.table, 9) is False


def test_is_job_empty_query_failure_raises() -> None:
    api = FakeApi(queryInformationJobObject=lambda *args: 0,
                  getLastError=lambda: 122).table
    with pytest.raises(wp.Win32Error, match="active process count"):
        wp.is_job_empty(api, 9)


def test_terminate_job_and_probe() -> None:
    recorded: List[Any] = []

    def record_terminate(job: int, code: int) -> int:
        recorded.append((job, code))
        return 1

    fake = FakeApi(terminateJobObject=record_terminate)
    wp.terminate_job(fake.table, 9, 3)
    assert recorded == [(9, 3)]
    failing = FakeApi(terminateJobObject=lambda *args: 0,
                      getLastError=lambda: 5).table
    with pytest.raises(wp.Win32Error, match="TerminateJobObject"):
        wp.terminate_job(failing, 9, 3)


def test_probe_current_token_job_support_closes_probe_job() -> None:
    fake = FakeApi()
    wp.probe_current_token_job_support(fake.table)
    assert fake.closed  # the probe job handle was released


# ----------------------------------------------------- stdio resolution

def test_descriptor_handle_rejects_invalid_sentinels() -> None:
    for sentinel in (wp.UV_INVALID_OS_FILE_HANDLE,
                     wp.UV_INVALID_FILE_DESCRIPTOR, None, 0):
        api = FakeApi(uvGetOsfhandle=lambda fd, s=sentinel: s).table
        with pytest.raises(RuntimeError, match="invalid handle"):
            wp.descriptor_handle(api, 7, "control")


def test_descriptor_handle_resolves_crt_descriptor() -> None:
    api = FakeApi().table
    assert wp.descriptor_handle(api, 4, "stdout") == 904


def test_inherited_standard_handles_reads_get_std_handle() -> None:
    api = FakeApi(getStdHandle=lambda selector: {-10: 11, -11: 22,
                                                 -12: 33}[selector]).table
    assert wp.inherited_standard_handles(api) == {
        "stdin": 11, "stdout": 22, "stderr": 33}


def test_inherited_standard_handles_null_throws() -> None:
    api = FakeApi(getStdHandle=lambda selector: None,
                  getLastError=lambda: 6).table
    with pytest.raises(wp.Win32Error, match="GetStdHandle"):
        wp.inherited_standard_handles(api)


def test_inherited_standard_handles_with_control_descriptor() -> None:
    api = FakeApi(getStdHandle=lambda selector: 11).table
    stdio = wp.inherited_standard_handles(api, 7)
    assert stdio["control"] == {"fileDescriptor": 7, "handle": 907}


def test_target_carrier_handles_maps_descriptors() -> None:
    api = FakeApi().table
    handles = wp.target_carrier_handles(api, {
        "stdin": 0, "stdout": 1, "stderr": 2, "control": 7})
    assert handles == {"stdin": 900, "stdout": 901, "stderr": 902,
                       "control": {"fileDescriptor": 7, "handle": 907}}
    without_control = wp.target_carrier_handles(api, {
        "stdin": 0, "stdout": 1, "stderr": 2})
    assert "control" not in without_control


# --------------------------------------------------- spawn job lifecycle

def _job_options(**extra: Any) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "command": "C:\\prog.exe", "args": ["a"], "cwd": "C:\\work",
        "token": 55, "stdio": {"stdin": 0, "stdout": 1, "stderr": 2},
    }
    options.update(extra)
    return options


def test_spawn_job_process_happy_path() -> None:
    fake = FakeApi()
    inherit_calls: List[Any] = []
    resumed: List[int] = []

    def record_inherit(handle: int, mask: int, flags: int) -> int:
        inherit_calls.append((handle, flags))
        return 1

    def record_resume(handle: int) -> int:
        resumed.append(handle)
        return 1

    fake.table["setHandleInformation"] = record_inherit
    fake.table["resumeThread"] = record_resume
    result = wp.spawn_job_process(
        fake.table, _job_options(),
        lambda: {"stdin": 11, "stdout": 22, "stderr": 33},
        "CreateProcessW",
        lambda startup, info: fake.table["createProcessW"](
            None, None, None, None, 1, 0, None, None, startup, info))
    assert result["pid"] == 4242
    assert result["job"] >= 1000
    # inherit bits enabled then restored best-effort
    assert inherit_calls == [(11, 1), (22, 1), (33, 1),
                             (11, 0), (22, 0), (33, 0)]
    assert len(resumed) == 1  # the child thread was resumed exactly once


def test_spawn_job_process_create_failure_closes_job_and_restores() -> None:
    fake = FakeApi()

    def fail_create(*args: Any) -> int:
        return 0

    fake.table["createProcessW"] = fail_create
    fake.table["getLastError"] = lambda: 2
    with pytest.raises(wp.Win32Error) as caught:
        wp.spawn_job_process(
            fake.table, _job_options(),
            lambda: {"stdin": 11, "stdout": 22, "stderr": 33},
            "CreateProcessW",
            lambda startup, info: fail_create(startup, info))
    assert caught.value.win32_code == 2
    assert fake.closed  # job handle released


def test_spawn_job_process_assign_failure_terminates_child() -> None:
    fake = FakeApi(assignProcessToJobObject=lambda *args: 0,
                   getLastError=lambda: 5)
    terminated: List[Any] = []

    def record_terminate(handle: int, code: int) -> int:
        terminated.append(handle)
        return 1

    fake.table["terminateProcess"] = record_terminate
    with pytest.raises(wp.Win32Error, match="AssignProcessToJobObject"):
        wp.spawn_job_process(
            fake.table, _job_options(),
            lambda: {"stdin": 11, "stdout": 22, "stderr": 33},
            "CreateProcessW",
            lambda startup, info: fake.table["createProcessW"](
                None, None, None, None, 1, 0, None, None, startup, info))
    assert terminated


def test_spawn_job_process_resume_failure_raises() -> None:
    fake = FakeApi(resumeThread=lambda handle: wp._INFINITE,
                   getLastError=lambda: 5)
    with pytest.raises(wp.Win32Error, match="ResumeThread"):
        wp.spawn_job_process(
            fake.table, _job_options(),
            lambda: {"stdin": 11, "stdout": 22, "stderr": 33},
            "CreateProcessW",
            lambda startup, info: fake.table["createProcessW"](
                None, None, None, None, 1, 0, None, None, startup, info))


def test_spawn_job_process_set_handle_failure_aborts_before_create() -> None:
    fake = FakeApi(setHandleInformation=lambda *args: 0,
                   getLastError=lambda: 6)
    created: List[Any] = []

    def record_create(startup: Any, info: Any) -> int:
        created.append(1)
        return 1

    with pytest.raises(wp.Win32Error, match="SetHandleInformation"):
        wp.spawn_job_process(
            fake.table, _job_options(),
            lambda: {"stdin": 11, "stdout": 22, "stderr": 33},
            "CreateProcessW", record_create)
    assert created == []


def test_spawn_job_process_control_descriptor_encodes_crt_block() -> None:
    fake = FakeApi(getFileType=lambda handle: wp.FILE_TYPE_PIPE)
    result = wp.spawn_job_process(
        fake.table, _job_options(),
        lambda: {"stdin": 11, "stdout": 22, "stderr": 33,
                 "control": {"fileDescriptor": 7, "handle": 777}},
        "CreateProcessW",
        lambda startup, info: fake.table["createProcessW"](
            None, None, None, None, 1, 0, None, None, startup, info))
    assert result["pid"] == 4242


# ------------------------------------------------------ piped spawn shape

def test_spawn_piped_process_closes_child_ends_and_stdin_both() -> None:
    fake = FakeApi()
    result = wp.spawn_piped_process(fake.table, {
        "command": "C:\\prog.exe", "args": [], "cwd": "C:\\work",
        "token": 55})
    assert result["pid"] == 4242
    assert result["stdoutRead"] and result["stderrRead"]
    # closed on success: stdin read + stdin write (immediate EOF) +
    # stdout write + stderr write (now the child's) + the thread handle
    assert len(fake.closed) == 5


def test_spawn_piped_process_create_failure_closes_all_owned() -> None:
    fake = FakeApi()

    def fail_create(*args: Any) -> int:
        return 0

    fake.table["createProcessAsUserW"] = fail_create
    fake.table["getLastError"] = lambda: 2
    with pytest.raises(wp.Win32Error, match="CreateProcessAsUserW"):
        wp.spawn_piped_process(fake.table, {
            "command": "C:\\prog.exe", "args": [], "cwd": "C:\\work",
            "token": 55})
    assert len(fake.closed) == 6  # every owned pipe end released


def test_spawn_piped_process_null_child_handle_terminates() -> None:
    fake = FakeApi()

    def null_process(*args: Any) -> int:
        info = args[-1]
        info.hProcess = None
        info.hThread = None
        info.dwProcessId = 99
        return 1

    fake.table["createProcessAsUserW"] = null_process
    terminated: List[Any] = []

    def record_terminate(handle: int, code: int) -> int:
        terminated.append(handle)
        return 1

    fake.table["terminateProcess"] = record_terminate
    with pytest.raises(RuntimeError, match="null process/thread handles"):
        wp.spawn_piped_process(fake.table, {
            "command": "C:\\prog.exe", "args": [], "cwd": "C:\\work",
            "token": 55})


# ----------------------------------------------------------- real surface

@pytest.mark.skipif(not IS_WIN32, reason="real ctypes surface is win32-only")
def test_real_struct_abi_guards() -> None:
    import ctypes
    assert ctypes.sizeof(wp.STARTUPINFOW) == 104
    assert ctypes.sizeof(wp.PROCESS_INFORMATION) == 24


@pytest.mark.skipif(not IS_WIN32, reason="real ctypes surface is win32-only")
def test_real_bindings_load_and_error_text() -> None:
    api = wp.load_win32_process_bindings()
    assert len(api) >= 21
    # a well-known system message resolves through FormatMessageW
    text = wp.error_text(api, 2)
    assert isinstance(text, str) and text != ""


@pytest.mark.skipif(not IS_WIN32, reason="real Job primitives are win32-only")
def test_real_job_probe_and_direct_spawn() -> None:
    api = wp.load_win32_process_bindings()
    wp.probe_current_token_job_support(api)
    cmd = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
    result = wp.spawn_current_token_job_process(api, {
        "command": cmd, "args": ["/d", "/s", "/c", "exit 5"],
        "cwd": os.getcwd(),
        "env": {"SystemRoot": os.environ["SystemRoot"],
                "PATH": os.environ.get("PATH", "")},
        "applicationName": None,
        "stdio": {"stdin": 0, "stdout": 1, "stderr": 2},
    })
    try:
        assert wp.wait_for_process_exit(api, result["process"]) == 5
        assert wp.is_job_empty(api, result["job"]) is True
    finally:
        wp.close_best_effort(api, result["job"])


@pytest.mark.skipif(not IS_WIN32, reason="real std handles are win32-only")
def test_real_inherited_standard_handles() -> None:
    api = wp.load_win32_process_bindings()
    stdio = wp.inherited_standard_handles(api)
    assert set(stdio) == {"stdin", "stdout", "stderr"}
    for value in stdio.values():
        assert isinstance(value, int)
