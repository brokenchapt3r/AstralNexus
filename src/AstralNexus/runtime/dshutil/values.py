"""Duplicate-install-safe value primitives.

Port of ``@deepseek-ai/dsh-util-values`` (commit 5badb15). Contract kept:
- ``assert_never``: fail loud in exhaustiveness positions with a stable
  message naming the unreachable value.
- ``is_json_value`` / ``snapshot_json_value``: JSON-lossless values only —
  None/bool/int/finite float/str/plain list/dict with str keys. ``NaN``,
  ``Infinity``, ``-0.0`` are rejected; the snapshot never shares mutable
  containers with the input and returns ``None`` for non-lossless input;
  cycles return False/None rather than recursing forever.
- ``deep_equal_json``: structural equality over lossless JSON values.
- ``deep_freeze``: Python adaptation — in-place freezing does not exist, so it
  returns a recursive read-only projection (dict→MappingProxyType, list→tuple,
  primitives as-is) with sharing preserved through a memo; cyclic graphs
  cannot be projected immutably and raise ValueError instead of silently passing.
- ``WeakMapWithValues``: weak-keyed map that also enumerates its distinct
  values in insertion order; entries vanish with their keys at GC.

Python adaptation for the "plain prototype" gate: exact type membership
(``type(v) is list/dict``) rejects subclasses and arbitrary objects, mirroring
upstream's ``Array.isArray``/plain-object check at the closest fidelity.
"""
from __future__ import annotations

import json
import math
from types import MappingProxyType
from typing import Any, Dict, Hashable, List, Optional
import weakref


class _Reject:
    """Internal marker: a walked value is not JSON-lossless."""
    __slots__ = ()


_REJECT = _Reject()


def assert_never(value: Any, context: Optional[str] = None) -> Any:
    """Throw in an unreachable branch; the message names the offending value."""
    prefix = f"unexpected value in {context}: " if context else "unexpected value: "
    try:
        rendered = json.dumps(value)
    except (TypeError, ValueError):
        rendered = repr(value)
    raise RuntimeError(prefix + rendered)


def _is_plain_container(value: Any) -> bool:
    return type(value) in (list, dict)  # noqa: E721 — exactness is the contract


def _is_lossless_number(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and not (value == 0.0 and math.copysign(1.0, value) < 0)
    return False


def is_json_value(value: Any) -> bool:
    """Whether the value survives JSON.stringify→parse losslessly (cycle-safe, iterative)."""
    seen: set = set()
    stack: List[Any] = [value]
    while stack:
        current = stack.pop()
        if current is None or isinstance(current, bool) or isinstance(current, str):
            continue
        if isinstance(current, (int, float)):
            if not _is_lossless_number(current):
                return False
            continue
        if not _is_plain_container(current):
            return False
        key = id(current)
        if key in seen:
            return False
        seen.add(key)
        if isinstance(current, list):
            stack.extend(current)
        else:
            for name, child in current.items():
                if not isinstance(name, str):
                    return False
                stack.append(child)
    return True


def _snapshot_walk(current: Any, ancestors: set) -> Any:
    """Recursive strict snapshot: returns _REJECT on any loss (cycles guarded)."""
    if current is None or isinstance(current, (bool, str)):
        return current
    if isinstance(current, (int, float)):
        return current if _is_lossless_number(current) else _REJECT
    if not _is_plain_container(current):
        return _REJECT
    key = id(current)
    if key in ancestors:
        return _REJECT
    ancestors.add(key)
    try:
        if isinstance(current, list):
            result: List[Any] = []
            for item in current:
                child = _snapshot_walk(item, ancestors)
                if isinstance(child, _Reject):
                    return _REJECT
                result.append(child)
            return result
        snapshot: Dict[str, Any] = {}
        for name, item in current.items():
            if not isinstance(name, str):
                return _REJECT
            child = _snapshot_walk(item, ancestors)
            if isinstance(child, _Reject):
                return _REJECT
            snapshot[name] = child
        return snapshot
    finally:
        ancestors.discard(key)


def snapshot_json_value(value: Any) -> Optional[Any]:
    """Deep isolated copy of a lossless JSON value; ``None`` when not lossless."""
    snapshot = _snapshot_walk(value, set())
    return None if isinstance(snapshot, _Reject) else snapshot


def deep_equal_json(left: Any, right: Any) -> bool:
    """Structural equality over lossless JSON values."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(deep_equal_json(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(deep_equal_json(left[name], right[name]) for name in left)
    return False


def deep_freeze(value: Any) -> Any:
    """Recursive read-only projection (Python adaptation of Object.freeze)."""
    return _freeze(value, {}, set())


def _freeze(value: Any, memo: Dict[int, Any], ancestors: set) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, MappingProxyType):
        return value
    key = id(value)
    if key in ancestors:
        raise ValueError("deep_freeze: cyclic graph cannot be projected immutably")
    if key in memo:
        return memo[key]
    if not isinstance(value, (dict, list, tuple)):
        raise ValueError(f"deep_freeze: {type(value).__name__} is not a JSON-lossless value")
    ancestors.add(key)
    try:
        frozen: Any
        if isinstance(value, dict):
            frozen = MappingProxyType(
                {name: _freeze(item, memo, ancestors) for name, item in value.items()})
        else:
            frozen = tuple(_freeze(item, memo, ancestors) for item in value)
        memo[key] = frozen
        return frozen
    finally:
        ancestors.discard(key)


class WeakMapWithValues:
    """Weak-keyed map enumerating its distinct values in insertion order.

    Keys stay weakly held (no strong key references anywhere), so entries
    vanish with their keys at GC; only the distinct VALUE set is kept strongly,
    ordered by first insertion.
    """

    __slots__ = ("_by_key", "_values")

    def __init__(self) -> None:
        self._by_key: "weakref.WeakKeyDictionary[Any, Hashable]" = weakref.WeakKeyDictionary()
        self._values: Dict[Hashable, None] = {}

    def get(self, key: Any) -> Optional[Hashable]:
        return self._by_key.get(key)

    def has(self, key: Any) -> bool:
        return key in self._by_key

    def set(self, key: Any, value: Hashable) -> None:
        existing = self._by_key.get(key)
        self._by_key[key] = value
        self._values.setdefault(value, None)
        if existing is not None and existing != value:
            self._prune(existing)

    def delete(self, key: Any) -> bool:
        if key not in self._by_key:
            return False
        value = self._by_key.pop(key)
        self._prune(value)
        return True

    def clear(self) -> None:
        self._by_key.clear()
        self._values.clear()

    def _prune(self, value: Hashable) -> None:
        """Drop a value from the enumeration once no live key maps to it."""
        if value not in list(self._by_key.values()):
            self._values.pop(value, None)

    @property
    def values(self) -> List[Hashable]:
        """Distinct live values in insertion order (entries of dead keys are gone)."""
        live = list(self._by_key.values())
        return [value for value in self._values if value in live]
