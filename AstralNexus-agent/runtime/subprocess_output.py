"""Native port of @deepseek-ai/dsh-subprocess-local lib/output.js (commit
5badb150, 239 lines read in full; this file follows the upstream text
line-for-line) — bounded output tails and private spill files shared by
process providers.

Contract (verbatim semantics):
- ``private_spill_dir``: a private (0700) per-process ``mkdtemp`` directory
  under the OS tmpdir, created lazily once and never recreated; at a
  process-observable exit it is removed only when empty (spill files are
  retained as full-output recovery artifacts until an external cleanup)
- ``OutputCollector`` keeps a bounded in-memory tail (whole chunks dropped
  from the head, or the head of a single over-cap chunk sliced). With spill
  options, on first overflow a spill file is created (``wx``, 0600) and
  every chunk — already-collected ones included — is appended there from
  then on, so the spill holds the complete stream while the stream stays
  within ``spill.maxBytes``
- when the whole-stream ``total`` would exceed the spill cap,
  ``discard_spill`` closes AND UNLINKS the file: a partial spill is never
  advertised (this is a bounded policy result, reported nowhere)
- an open/write failure likewise discards the spill, reports once through
  ``spill.onFailure`` (ENOENT gets the cleaner explanation in
  ``log_spill_failure``), and never interrupts in-memory collection
- ``read_from(from_byte)`` is incremental in whole-stream coordinates:
  returns everything retained since ``from_byte``, the ``nextOffset``
  (total), and ``lossy: True`` when the offset slid out of the tail window;
  ``spillPath`` rides along whenever a spill file exists
- ``seal`` closes the spill at stream end; a failed close stops advertising
  the path (the file may lack its tail); idempotent. ``finalize`` seals and
  returns ``{text, truncated, spillPath?}``.
"""
from __future__ import annotations

import atexit
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional

__all__ = ["OutputCollector", "log_spill_failure",
           "prepare_managed_process_binding", "private_spill_dir",
           "report_spill_failure_to_stderr"]

_spill_counter = 0
_default_spill_dir: Optional[str] = None
_exit_registered = False


def private_spill_dir() -> str:
    """The lazily created private (0700) per-process spill directory."""
    global _default_spill_dir, _exit_registered
    if _default_spill_dir is None:
        _default_spill_dir = tempfile.mkdtemp(prefix="dsh-subprocess-")
        if not _exit_registered:
            _exit_registered = True

            def remove_when_empty() -> None:
                if _default_spill_dir is None:
                    return
                try:
                    os.rmdir(_default_spill_dir)
                except OSError:
                    pass

            atexit.register(remove_when_empty)
    return _default_spill_dir


def report_spill_failure_to_stderr(error: BaseException, label: str) -> None:
    """The stderr reporter used when no owner supplies one."""
    import sys
    sys.stderr.write(
        f"dsh-subprocess-local: {label} spill failed; only the in-memory "
        f"tail is retained: {error}\n")


def log_spill_failure(logger: Any, owner: str
                      ) -> Callable[[BaseException, str], None]:
    """Build the reporter an owner passes as spill ``onFailure``."""
    def report(error: BaseException, label: str) -> None:
        removed_directory = str(getattr(error, "code", "")) == "ENOENT" or \
            getattr(error, "errno", None) == 2
        logger.error(
            f"{owner} could not write the complete {label} stream to its "
            f"spill file; the result keeps only the in-memory tail and "
            f"reports no full-output path."
            + (" The spill directory no longer exists; a temporary-file "
               "cleaner removing it while empty is the usual cause."
               if removed_directory else ""), error)
    return report


def prepare_managed_process_binding(internals: Optional[Dict[str, Any]] = None
                                    ) -> Dict[str, Any]:
    """Resolve the spill inputs before starting a managed native process."""
    supplied = internals or {}
    return {
        "spillDir": supplied.get("spillDir") or private_spill_dir(),
        "onSpillFailure": supplied.get("onSpillFailure")
        or report_spill_failure_to_stderr,
    }


def _collector_locked(method: Any) -> Any:
    from functools import wraps
    @wraps(method)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class OutputCollector:
    """Collects one stream with a bounded in-memory tail and optional
    best-effort spill file (verbatim output.js semantics)."""

    def __init__(self, max_bytes: int, label: str,
                 spill: Optional[Dict[str, Any]] = None) -> None:
        import threading
        self._lock = threading.RLock()
        self._sealed = False
        self.max_bytes = max_bytes
        self.label = label
        self.spill = spill
        self.chunks: List[bytes] = []
        self.bytes = 0
        self.dropped = False
        self.spill_fd: Optional[int] = None
        self.spill_file: Optional[str] = None
        self.spill_disabled = spill is None
        #: Total bytes ever pushed (not just retained).
        self.total = 0

    @_collector_locked
    def push(self, chunk: bytes) -> None:
        """Ingest one stream chunk, counting it toward the whole-stream
        total; spill (when enabled) happens BEFORE the tail trims, so the
        spill file receives the complete stream within its cap."""
        if self._sealed:
            return
        self.total += len(chunk)
        overflows = self.bytes + len(chunk) > self.max_bytes
        if self.spill is not None and not self.spill_disabled and (
                overflows or self.spill_fd is not None):
            self.spill_all(self.spill, chunk)
        self.chunks.append(chunk)
        self.bytes += len(chunk)
        while self.bytes > self.max_bytes:
            head = self.chunks[0]
            excess = self.bytes - self.max_bytes
            if len(head) <= excess:
                self.chunks.pop(0)
                self.bytes -= len(head)
            else:
                self.chunks[0] = head[excess:]
                self.bytes -= excess
            self.dropped = True

    @_collector_locked
    def spill_all(self, spill: Dict[str, Any], chunk: bytes) -> None:
        """Open the spill file lazily and append the chunk (and any prior
        chunks once). Every filesystem failure is contained here."""
        global _spill_counter
        if self.total > spill["maxBytes"]:
            self.discard_spill()
            return
        try:
            if self.spill_fd is None:
                _spill_counter += 1
                file = os.path.join(
                    spill["dir"],
                    f"dsh-subprocess-{os.getpid()}-{_spill_counter}-"
                    f"{os.urandom(6).hex()}-{self.label}.log")
                fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
                self.spill_file = file
                self.spill_fd = fd
                for prior in self.chunks:
                    os.write(fd, prior)
            os.write(self.spill_fd, chunk)
        except OSError as error:
            self.discard_spill()
            try:
                spill["onFailure"](error, self.label)
            except Exception as reporter_failure:
                import sys
                sys.stderr.write(
                    "dsh-subprocess-local: spill failure reporter threw: "
                    f"{reporter_failure}\n")

    @_collector_locked
    def discard_spill(self) -> None:
        """Stop spilling and remove the file once it can no longer hold the
        complete stream."""
        fd = self.spill_fd
        file = self.spill_file
        self.spill_fd = None
        self.spill_file = None
        self.spill_disabled = True
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                self.spill_fd = fd
        if file is not None:
            try:
                os.unlink(file)
            except OSError:
                pass

    @_collector_locked
    def read_from(self, from_byte: int = 0) -> Dict[str, Any]:
        """Incremental read in whole-stream byte coordinates."""
        window_start = self.total - self.bytes
        buffer = b"".join(self.chunks)
        lossy = from_byte < window_start
        text = (buffer if lossy
                else buffer[from_byte - window_start:]).decode(
                    "utf-8", errors="replace")
        result: Dict[str, Any] = {"text": text, "nextOffset": self.total,
                                  "lossy": lossy}
        if self.spill_file is not None:
            result["spillPath"] = self.spill_file
        return result

    @_collector_locked
    def snapshot(self) -> Dict[str, Any]:
        """Copy the retained raw tail with its whole-stream position."""
        return {"bytes": b"".join(self.chunks), "totalBytes": self.total}

    @_collector_locked
    def seal(self) -> None:
        """Close the spill file once the stream ended. A failed close stops
        advertising the path. Idempotent."""
        self._sealed = True
        if self.spill_fd is None:
            return
        try:
            os.close(self.spill_fd)
        except OSError:
            self.spill_file = None
        self.spill_fd = None

    @_collector_locked
    def finalize(self) -> Dict[str, Any]:
        """Seal the spill file and return the final collected output."""
        self.seal()
        result: Dict[str, Any] = {
            "text": b"".join(self.chunks).decode("utf-8", errors="replace"),
            "truncated": self.dropped,
        }
        if self.spill_file is not None:
            result["spillPath"] = self.spill_file
        return result
