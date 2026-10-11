"""Context-local console capture that leaves the host protocol channel intact."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import io
import sys
import threading
from typing import Any, Iterator, TextIO

_targets: ContextVar[dict[str, TextIO] | None] = ContextVar('console_capture', default=None)
_lock = threading.RLock()
_active = 0

class RoutedStream(io.TextIOBase):
    def __init__(self, channel: str, original: TextIO):
        self.channel, self.original = channel, original

    def target(self):
        return (_targets.get() or {}).get(self.channel, self.original)

    def write(self, text: str) -> int:
        return self.target().write(text)

    def flush(self) -> None:
        self.target().flush()

    def isatty(self) -> bool:
        return self.target().isatty()

    @property
    def encoding(self):
        return self.target().encoding

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target(), name)


def protocol_stdout():
    stream = sys.stdout
    return stream.original if isinstance(stream, RoutedStream) else stream


@contextmanager
def capture_console(stdout: TextIO, stderr: TextIO) -> Iterator[None]:
    global _active
    with _lock:
        if _active == 0:
            sys.stdout = RoutedStream('stdout', sys.stdout)
            sys.stderr = RoutedStream('stderr', sys.stderr)
        _active += 1
    token = _targets.set({'stdout': stdout, 'stderr': stderr})
    try:
        yield
    finally:
        _targets.reset(token)
        with _lock:
            _active -= 1
            if _active == 0:
                if isinstance(sys.stdout, RoutedStream): sys.stdout = sys.stdout.original
                if isinstance(sys.stderr, RoutedStream): sys.stderr = sys.stderr.original
