"""Zero-dependency timeout/deadline primitives: timing and classification only.

Port of ``@deepseek-ai/dsh-timeout`` (commit 5badb15). Like upstream, nothing
here terminates a task: ``Deadline``/``IdleWatchdog`` only expose whether the
budget expired and a typed ``TimeoutReason`` carriers can be matched with
``timeout_of``.

Contract kept from upstream:
- ``clamp_timeout``: positive finite milliseconds, ``min(requested or default,
  maximum)``.
- ``MAX_TIMER_DELAY_MS`` = 2147483647 (the int32 ceiling); timer construction
  refuses delays above it instead of firing immediately.
- ``Deadline``: one timer fusing an optional upstream cancel signal; the timer
  owns the reason (upstream cancel ⇒ expired with reason None), ``close()``
  cancels the timer on both outcomes, and every construction path arms exactly
  once (or not at all).
- ``IdleWatchdog``: per-call timers; each ``next()`` arms, runs, and clears; a
  second outstanding call or use after disposal is refused loudly; ``pulse()``
  extends only while a call is outstanding.
- ``timeout_of``: returns the attached ``TimeoutReason`` only when present and
  the code filter matches.

Python adaptation: the fused signal is a ``threading.Event`` set by the timer
thread plus a checked upstream ``Event``; ``wait()`` polls in bounded slices
instead of ``AbortSignal.any`` (no event-loop coupling).
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

MAX_TIMER_DELAY_MS = 2147483647
_WAIT_SLICE_S = 0.02


class TimeoutReason(Exception):
    """Stable timeout classification carried through failure paths."""

    def __init__(self, code: str, timeout_ms: float):
        if not isinstance(code, str) or not code:
            raise ValueError("timeout code must be a non-empty string")
        _assert_timer_delay(timeout_ms, "timeout_ms")
        self.code = code
        self.timeout_ms = float(timeout_ms)
        super().__init__(f"{code} after {timeout_ms:g}ms")


def _assert_timer_delay(timeout_ms: Any, name: str) -> None:
    if not isinstance(timeout_ms, (int, float)) or isinstance(timeout_ms, bool) \
            or not math.isfinite(timeout_ms):
        raise ValueError(f"{name} must be a finite number of milliseconds")
    if not 0 < timeout_ms <= MAX_TIMER_DELAY_MS:
        raise ValueError(f"{name} must be within (0, {MAX_TIMER_DELAY_MS}]")


def clamp_timeout(requested: Optional[float], default: float, maximum: float,
                  name: str = "timeout_ms") -> float:
    """Bound a requested timeout: ``min(requested or default, maximum)``."""
    for label, value in (("default", default), ("maximum", maximum)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be a positive finite number")
    if requested is None:
        requested = default
    if not isinstance(requested, (int, float)) or isinstance(requested, bool) \
            or not math.isfinite(requested) or requested <= 0:
        raise ValueError(f"{name} must be a positive finite number when provided")
    return min(float(requested), float(maximum))


class _TimedSignal:
    """Shared base: timer-owned reason, fused upstream cancel, honest close."""

    def __init__(self, upstream: Optional[threading.Event], timeout_ms: Optional[float], code: str):
        self._upstream = upstream
        self._code = code
        self._timeout_ms = timeout_ms
        self._timer_event = threading.Event()
        self._reason: Optional[TimeoutReason] = None
        self._timer: Optional[threading.Timer] = None
        self._closed = False

    def _arm(self) -> None:
        if self._timeout_ms is None or self._timeout_ms <= 0:
            return
        _assert_timer_delay(self._timeout_ms, "timeout_ms")
        if self._timer is not None:
            raise RuntimeError("timer already armed")
        self._timer = threading.Timer(self._timeout_ms / 1000.0, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        if self._reason is None:
            self._reason = TimeoutReason(self._code, float(self._timeout_ms or 0))
        self._timer_event.set()

    @property
    def expired(self) -> bool:
        return self._timer_event.is_set() or (self._upstream is not None and self._upstream.is_set())

    @property
    def reason(self) -> Optional[TimeoutReason]:
        return self._reason

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until expired (or ``timeout`` seconds elapse); returns expired."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.expired:
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.expired
                self._timer_event.wait(min(_WAIT_SLICE_S, remaining))
            else:
                self._timer_event.wait(_WAIT_SLICE_S)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()

    def __enter__(self) -> "_TimedSignal":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class Deadline(_TimedSignal):
    """One fixed deadline, fused with an optional upstream cancel signal."""

    def __init__(self, upstream: Optional[threading.Event] = None,
                 timeout_ms: Optional[float] = None, code: str = "timeout"):
        super().__init__(upstream, timeout_ms, code)
        self._arm()


class IdleWatchdog(_TimedSignal):
    """A deadline re-armed per awaited call: the budget bounds idleness, not total time."""

    def __init__(self, upstream: Optional[threading.Event] = None,
                 timeout_ms: float = 60000, code: str = "idle"):
        if timeout_ms is None:
            raise ValueError("timeout_ms must be a positive finite number")
        super().__init__(upstream, timeout_ms, code)
        self._outstanding = False
        self._disposed = False
        self._lock = threading.Lock()

    def next(self, call: Callable[[], T]) -> T:
        """Arm, run one call, clear on both outcomes; refuses double-outstanding use."""
        with self._lock:
            if self._disposed:
                raise RuntimeError("idleWatchdog is disposed")
            if self._outstanding:
                raise RuntimeError("idleWatchdog next is already outstanding")
            self._outstanding = True
            # Each call gets a fresh signal: the previous call's timeout must
            # not leak into this one (upstream fuses a new AbortSignal per next()).
            self._reason = None
            self._timer_event.clear()
        try:
            self._arm()
            return call()
        finally:
            with self._lock:
                self._clear_timer()
                # Faithful to upstream clear(): once the call settles, its
                # per-call signal is gone — reason/expiry do not outlive it.
                self._reason = None
                self._timer_event.clear()
                self._outstanding = False

    def pulse(self) -> None:
        """Extend the idle budget once while a call is outstanding."""
        with self._lock:
            if self._disposed:
                raise RuntimeError("idleWatchdog is disposed")
            if self._outstanding:
                self._clear_timer()
                self._timer_event.clear()
                self._arm()

    def _clear_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def close(self) -> None:
        with self._lock:
            self._disposed = True
            self._clear_timer()
            super().close()


def timeout_of(carrier: Any, code: Optional[str] = None) -> Optional[TimeoutReason]:
    """Return the attached reason only when present and matching ``code``."""
    reason = getattr(carrier, "reason", None)
    if not isinstance(reason, TimeoutReason):
        return None
    if code is not None and reason.code != code:
        return None
    return reason
