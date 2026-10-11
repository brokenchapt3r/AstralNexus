"""Zero-dependency atomic file replacement and writer coordination.

Port of ``@deepseek-ai/dsh-atomic-write`` (commit 5badb15).

Contract kept from upstream:
- ``write_file_atomic`` writes a random-suffix sibling with exclusive create
  (refuses to follow a planted symlink), applies the caller-stated permission
  bits to the fresh inode, then renames over the target, so readers observe
  either the old or the new complete content. Windows replacement retries
  transient sharing violations (EACCES/EBUSY/EPERM class) with bounded
  exponential backoff while the complete temp file remains the rename source.
  On any remaining failure the temp file is removed and the error rethrown.
  Crash durability (fsync) is out of scope, as upstream.
- ``with_file_lock`` serializes cross-process writers through an
  exclusive-created ``<file>.lock`` sibling recording the holder PID; readers
  stay lock-free. A lock whose recorded holder provably exited is taken over
  through a claim file keyed by the record hash, so a removal never deletes a
  lock another contender acquired after the dead holder's (including PID
  reuse). Contention backs off exponentially and times out after the deadline.

Python adaptations (documented):
- Permission bits are applied with ``os.chmod`` on POSIX only; on Windows the
  upstream ``mode`` option is likewise inert, so it is skipped, not faked.
- The holder-exit probe uses ``psutil.pid_exists`` (os.kill(pid, 0) is unsafe
  on Windows); PID comparison stays contender-host-local, so cross-host /
  cross-namespace writers sharing the file remain unsupported, as upstream.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Callable, Iterator, Optional, TypeVar

T = TypeVar("T")

WINDOWS_TRANSIENT_RENAME_ERRORS = frozenset({"EACCES", "EBUSY", "EPERM"})
WINDOWS_RENAME_RETRY_INITIAL_MS = 20
WINDOWS_RENAME_RETRY_MAX_MS = 200
WINDOWS_RENAME_RETRY_LIMIT = 8
LOCK_RETRY_INITIAL_MS = 20
LOCK_RETRY_MAX_MS = 200
DEFAULT_LOCK_WAIT_MS = 2000
_EXCL = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _is_transient_windows_rename_error(exc: OSError) -> bool:
    if sys.platform != "win32":
        return False
    # Windows maps sharing violations onto PermissionError/OSError winerrno
    # 5 (access denied) and 32 (sharing violation); errno names the POSIX class.
    import errno
    name = errno.errorcode.get(exc.errno or 0, "")
    return name in WINDOWS_TRANSIENT_RENAME_ERRORS or isinstance(exc, PermissionError)


def _rename_atomic_temp(temp: str, filename: str) -> None:
    """Replace the target after bounded retries for transient Windows interference."""
    delay = WINDOWS_RENAME_RETRY_INITIAL_MS / 1000.0
    retries = 0
    while True:
        try:
            os.replace(temp, filename)
            return
        except OSError as exc:
            if not _is_transient_windows_rename_error(exc):
                raise
            if retries >= WINDOWS_RENAME_RETRY_LIMIT:
                raise
        time.sleep(delay)
        delay = min(delay * 2, WINDOWS_RENAME_RETRY_MAX_MS / 1000.0)
        retries += 1


def write_file_atomic(filename: "str | os.PathLike[str]", content: "bytes | bytearray | str",
                      *, mode: Optional[int] = None, dir_mode: Optional[int] = None) -> None:
    """Replace ``filename`` with ``content`` in one atomic step, creating parents."""
    target = os.fspath(filename)
    parent = os.path.dirname(os.path.abspath(target))
    if dir_mode is not None and sys.platform != "win32":
        os.makedirs(parent, exist_ok=True, mode=dir_mode)
    else:
        os.makedirs(parent, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    temp = f"{target}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(temp, _EXCL, 0o666 if mode is None else mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        if mode is not None and sys.platform != "win32":
            os.chmod(temp, mode)
        _rename_atomic_temp(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _holder_exited(record: str) -> bool:
    """Whether the holder a ``<pid>\\n`` record names is proven gone."""
    if not record.endswith("\n") or not record[:-1].isdigit():
        return False
    pid = int(record[:-1])
    if pid == 0 or pid > 2147483647 or pid == os.getpid():
        return False
    import psutil  # type: ignore[import-untyped]
    return not psutil.pid_exists(pid)


def _read_lock_record(lock_path: str) -> Optional[str]:
    try:
        with open(lock_path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _take_over_exited_lock(lock_path: str) -> bool:
    """Remove the lock when its recorded holder exited; serialized by a claim file."""
    record = _read_lock_record(lock_path)
    if record is None or not _holder_exited(record):
        return False
    claim = f"{lock_path}.takeover-{hashlib.sha256(record.encode('utf-8')).hexdigest()[:16]}"
    try:
        fd = os.open(claim, _EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
    except FileExistsError:
        return False
    except PermissionError:
        return False
    try:
        if _read_lock_record(lock_path) != record or not _holder_exited(record):
            return False
        try:
            os.unlink(lock_path)
        except OSError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            os.unlink(claim)  # claim files have no concurrent readers by design


def _release_lock(lock_path: str) -> None:
    """Delete our lock file, surviving Windows sharing violations loudly.

    CPython ``open()`` handles lack FILE_SHARE_DELETE, so a contender reading
    the record can transiently block deletion. Retry briefly; verify ownership
    before each retry (the lock may have been taken over meanwhile); refuse to
    silently leak a lock that would starve every later contender.
    """
    for attempt in range(6):
        try:
            os.unlink(lock_path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            record = _read_lock_record(lock_path)
            if record is None or (record.endswith("\n") and record[:-1].isdigit()
                                  and int(record[:-1]) != os.getpid()):
                return  # ownership changed; the current holder releases it
            time.sleep(0.002 * (attempt + 1))
    raise OSError(f"atomic-write: failed to release the writer lock at {lock_path}")


def _is_lock_contention(exc: OSError, lock_path: str) -> bool:
    if isinstance(exc, FileExistsError):
        return True
    if not isinstance(exc, PermissionError):
        return False
    return os.path.lexists(lock_path)


@contextlib.contextmanager
def file_lock(filename: "str | os.PathLike[str]", *, wait_ms: int = DEFAULT_LOCK_WAIT_MS) -> Iterator[None]:
    """Hold the cross-process writer lock for ``filename`` around one block."""
    lock_path = f"{os.fspath(filename)}.lock"
    deadline = time.monotonic() + wait_ms / 1000.0
    delay = LOCK_RETRY_INITIAL_MS / 1000.0
    retried_unconfirmed_permission_error = False
    while True:
        try:
            fd = os.open(lock_path, _EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            break
        except OSError as exc:
            if not _is_lock_contention(exc, lock_path):
                if sys.platform != "win32" or not isinstance(exc, PermissionError) \
                        or retried_unconfirmed_permission_error:
                    raise
                retried_unconfirmed_permission_error = True
            elif _take_over_exited_lock(lock_path):
                continue
        if time.monotonic() >= deadline:
            raise TimeoutError(f"atomic-write: timed out waiting for the writer lock at {lock_path}")
        time.sleep(delay)
        delay = min(delay * 2, LOCK_RETRY_MAX_MS / 1000.0)
    try:
        yield
    finally:
        _release_lock(lock_path)


def with_file_lock(filename: "str | os.PathLike[str]", operation: Callable[[], T],
                   *, wait_ms: int = DEFAULT_LOCK_WAIT_MS) -> T:
    """Run ``operation`` holding the writer lock; the lock releases on both outcomes."""
    with file_lock(filename, wait_ms=wait_ms):
        return operation()
