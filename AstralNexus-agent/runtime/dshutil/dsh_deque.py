"""Zero-dependency circular deque with amortized O(1) end operations.

Port of ``@deepseek-ai/dsh-deque`` (commit 5badb15). Semantics kept exactly:
- ``push_back``/``push_front`` grow by doubling from ``MIN_CAPACITY`` = 16.
- ``pop_front`` returns ``None`` when empty and clears the remaining slot
  reference, so a popped value never keeps an object alive through the buffer.
- After enough removals the storage compacts to at most one quarter of its
  current capacity (never below 16), so a bounded consumer also bounds vacant
  slots.
- ``clear`` drops every stored value and frees the buffer.
"""
from __future__ import annotations

from typing import Any, List, Optional

MIN_CAPACITY = 16


class Deque:
    """An array-backed circular queue with amortized O(1) end operations."""

    __slots__ = ("_buffer", "_head", "_count")

    def __init__(self, capacity: int = MIN_CAPACITY) -> None:
        capacity = max(MIN_CAPACITY, int(capacity))
        self._buffer: List[Any] = [None] * capacity
        self._head = 0
        self._count = 0

    @property
    def size(self) -> int:
        return self._count

    def __len__(self) -> int:
        return self._count

    def push_back(self, value: Any) -> None:
        if self._count == len(self._buffer):
            self._resize(len(self._buffer) * 2)
        self._buffer[(self._head + self._count) % len(self._buffer)] = value
        self._count += 1

    def push_front(self, value: Any) -> None:
        if self._count == len(self._buffer):
            self._resize(len(self._buffer) * 2)
        self._head = (self._head - 1 + len(self._buffer)) % len(self._buffer)
        self._buffer[self._head] = value
        self._count += 1

    def pop_front(self) -> Any:
        if self._count == 0:
            return None
        value = self._buffer[self._head]
        self._buffer[self._head] = None
        self._head = (self._head + 1) % len(self._buffer)
        self._count -= 1
        if self._count > 0 and self._count * 4 <= len(self._buffer) and len(self._buffer) > MIN_CAPACITY:
            self._resize(max(MIN_CAPACITY, len(self._buffer) // 2))
        return value

    def clear(self) -> None:
        self._buffer = []
        self._head = 0
        self._count = 0

    def _resize(self, capacity: int) -> None:
        next_buffer: List[Any] = [None] * capacity
        for index in range(self._count):
            next_buffer[index] = self._buffer[(self._head + index) % len(self._buffer)]
        self._buffer = next_buffer
        self._head = 0
