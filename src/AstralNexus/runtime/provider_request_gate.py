"""Provider-level concurrency and transient-failure control.

The orchestrator may run several heterogeneous workers at once, while a
provider endpoint usually has a much smaller stable concurrency envelope.
This module keeps that policy process-local and keyed by endpoint, so worker
roles do not each create an independent retry storm.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


class ProviderCircuitOpenError(RuntimeError):
    """Raised when an endpoint is in its transient-failure cooldown."""


@dataclass
class _ProviderState:
    semaphore: threading.BoundedSemaphore
    consecutive_failures: int = 0
    opened_until: float = 0.0


class ProviderRequestGate:
    """Technical concurrency/circuit gate with no task-level authority."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _ProviderState] = {}

    def _state(self, key: str) -> _ProviderState:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                limit = max(1, int(os.environ.get("ASTRALNEXUS_PROVIDER_MAX_CONCURRENCY", "2")))
                state = _ProviderState(threading.BoundedSemaphore(limit))
                self._states[key] = state
            return state

    @contextmanager
    def request_slot(self, key: str) -> Iterator[None]:
        state = self._state(key)
        now = time.monotonic()
        with self._lock:
            if state.opened_until > now:
                remaining = round(state.opened_until - now, 2)
                raise ProviderCircuitOpenError(
                    f"provider circuit open for {key}; retry in {remaining}s"
                )
        wait = max(0.1, float(os.environ.get("ASTRALNEXUS_PROVIDER_ACQUIRE_TIMEOUT", "30")))
        if not state.semaphore.acquire(timeout=wait):
            raise ProviderCircuitOpenError(
                f"provider concurrency limit reached for {key}; retry later"
            )
        try:
            yield
        finally:
            state.semaphore.release()

    def record_success(self, key: str) -> None:
        state = self._state(key)
        with self._lock:
            state.consecutive_failures = 0
            state.opened_until = 0.0

    def record_failure(self, key: str, category: str, retryable: bool) -> None:
        if not retryable or category not in {"network", "rate_limit", "provider_server"}:
            return
        state = self._state(key)
        threshold = max(1, int(os.environ.get("ASTRALNEXUS_PROVIDER_FAILURE_THRESHOLD", "3")))
        cooldown = max(1.0, float(os.environ.get("ASTRALNEXUS_PROVIDER_COOLDOWN", "30")))
        with self._lock:
            state.consecutive_failures += 1
            if state.consecutive_failures >= threshold:
                state.opened_until = time.monotonic() + cooldown

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        """Return provider-wide scheduling state for trace/dashboard inspection."""
        now = time.monotonic()
        with self._lock:
            return {
                key: {
                    "consecutive_failures": state.consecutive_failures,
                    "circuit_open": state.opened_until > now,
                    "cooldown_remaining": round(max(0.0, state.opened_until - now), 2),
                }
                for key, state in self._states.items()
            }


provider_request_gate = ProviderRequestGate()
