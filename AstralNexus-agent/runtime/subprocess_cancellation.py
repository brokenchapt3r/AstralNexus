"""Atomic cancellation shared by subprocess and terminal providers."""
from __future__ import annotations
import threading
from typing import Callable, List, Optional

class CancellationToken:
    """Thread-safe cancellation; callbacks run once outside the state lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._aborted = False
        self.reason: Optional[BaseException] = None
        self._listeners: List[Callable[[], None]] = []
        self.listener_errors: List[BaseException] = []

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted

    def _notify(self, listener: Callable[[], None]) -> None:
        try:
            listener()
        except BaseException as error:
            with self._lock:
                self.listener_errors.append(error)

    def abort(self, reason: Optional[BaseException] = None) -> None:
        with self._lock:
            if self._aborted:
                return
            self.reason = reason if reason is not None else RuntimeError(
                "This operation was aborted")
            self._aborted = True
            listeners, self._listeners = self._listeners, []
        for listener in listeners:
            self._notify(listener)

    def add_listener(self, listener: Callable[[], None]) -> None:
        with self._lock:
            aborted = self._aborted
            if not aborted:
                self._listeners.append(listener)
        if aborted:
            self._notify(listener)

    def throw_if_aborted(self) -> None:
        with self._lock:
            if self._aborted:
                raise self.reason or RuntimeError("This operation was aborted")

    def remove_listener(self, listener: Callable[[], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)


