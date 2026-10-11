"""Token reservations and separate wall-clock/worker-effort accounting."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable


class BudgetReservationRefused(ValueError):
    """No execution permit was issued; the caller must release its resource."""


@dataclass
class GlobalBudget:
    max_tokens: int = 0
    max_time_seconds: int = 0
    max_parallel_slots: int = 8
    max_recursion_depth: int = 256
    used_tokens: int = 0
    used_time_seconds: float = 0.0
    active_parallel_tasks: int = 0
    reserved_tokens: int = 0
    worker_time_seconds: float = 0.0
    unsettled_tokens: int = 0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)
    _reservations: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _starts: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _window_start: float | None = field(default=None, init=False, repr=False)

    def remaining_tokens(self) -> int:
        with self._lock:
            return 2**63 - 1 if self.max_tokens <= 0 else max(0, self.max_tokens - self.used_tokens - self.reserved_tokens)

    def elapsed_wall_seconds(self) -> float:
        with self._lock:
            return max(self.used_time_seconds, self._clock() - self._window_start if self._window_start is not None else 0.0)

    def remaining_time(self) -> float:
        with self._lock:
            return float(2**63 - 1) if self.max_time_seconds <= 0 else max(0.0, self.max_time_seconds - self.elapsed_wall_seconds())

    def available_parallel_slots(self) -> int:
        with self._lock:
            return max(0, self.max_parallel_slots - self.active_parallel_tasks)

    def can_admit_task(self, estimated_cost: int) -> bool:
        with self._lock:
            return (self.remaining_tokens() >= max(0, estimated_cost)
                    and (self.max_tokens <= 0 or self.remaining_tokens() > 0)
                    and self.remaining_time() > 0)

    def reserve(self, task_id: str, estimated_tokens: int) -> None:
        with self._lock:
            if task_id in self._starts:
                raise ValueError(f'task already holds a budget reservation: {task_id}')
            estimate = max(0, int(estimated_tokens))
            if not self.can_admit_task(estimate) or self.available_parallel_slots() <= 0:
                raise BudgetReservationRefused('task execution budget reservation refused')
            now = self._clock()
            if self._window_start is None:
                self._window_start = now
            self._starts[task_id] = now
            self._reservations[task_id] = estimate
            self.reserved_tokens += estimate
            self.active_parallel_tasks += 1

    def settle(self, task_id: str, actual_tokens: int | None, duration: float) -> None:
        with self._lock:
            if task_id not in self._starts:
                raise ValueError(f'task has no live budget reservation: {task_id}')
            start = self._starts.pop(task_id)
            estimate = self._reservations.pop(task_id)
            if actual_tokens is not None:
                self.reserved_tokens -= estimate
                self.used_tokens += max(0, int(actual_tokens))
            else:
                # Unknown consumption is not zero: hold the estimate outside
                # active reservations, while releasing the execution slot.
                self.unsettled_tokens += estimate
            self.worker_time_seconds += max(0.0, duration)
            self.used_time_seconds = max(self.used_time_seconds, start - (self._window_start if self._window_start is not None else start) + max(0.0, duration))
            self.active_parallel_tasks -= 1

    def update(self, *, tokens: int = 0, time_seconds: float = 0.0, parallel_delta: int = 0) -> None:
        with self._lock:
            self.used_tokens += max(0, tokens)
            self.worker_time_seconds += max(0.0, time_seconds)
            self.used_time_seconds = max(self.used_time_seconds, max(0.0, time_seconds))
            self.active_parallel_tasks = max(0, self.active_parallel_tasks + parallel_delta)


def resize_token_limit(limit: int, multiplier: float) -> int:
    """Zero/negative denotes unlimited and must remain unlimited."""
    return limit if limit <= 0 else max(1, int(limit * multiplier))
