"""Detached immutable JSON-compatible values for runtime/application boundaries."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, NoReturn, Self, cast

class FrozenDict(dict):
    def _deny(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("snapshot is read-only")
    def __setitem__(self, key: Any, value: Any) -> NoReturn: self._deny()
    def __delitem__(self, key: Any) -> NoReturn: self._deny()
    def clear(self) -> NoReturn: self._deny()
    def pop(self, *args: Any, **kwargs: Any) -> NoReturn: self._deny()
    def popitem(self) -> NoReturn: self._deny()
    def setdefault(self, *args: Any, **kwargs: Any) -> NoReturn: self._deny()
    def update(self, *args: Any, **kwargs: Any) -> NoReturn: self._deny()
    def __ior__(self, value: Any) -> Self: self._deny()
    def __or__(self, value: Any) -> Self:
        return cast(Self, freeze({**self, **value}))
    def __deepcopy__(self, memo: dict) -> Self:
        return self


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze(item) for item in value)
    return deepcopy(value)

