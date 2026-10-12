"""Compatibility metadata projects the node's canonical data-plane fields."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, Self, Protocol, Iterable, overload, TypeVar

K = TypeVar('K')
V = TypeVar('V')

class MetadataMapping(Protocol):
    def keys(self) -> Iterable[str]: ...
    def __getitem__(self, key: str, /) -> Any: ...

CONTRACT_FIELDS = {'owned_paths': list, 'input_artifacts': list, 'output_artifacts': list, 'handoff_contract': dict, 'expected_outputs': list}


class LegacyDefaultList(list[str]):
    """Construction marker: omitted argument, distinct from explicit []."""


class LegacyDefaultDict(dict[str, Any]):
    """Construction marker: omitted handoff, distinct from explicit {}."""


class ContractMetadata(dict[str, Any]):
    def __init__(self, values=(), *, owner=None):
        dict.__init__(self, values)
        self._owner = owner

    def detach(self) -> None:
        # A replaced compatibility projection becomes a snapshot, including
        # mutable lists, rather than a second hidden way to mutate the node.
        for key in CONTRACT_FIELDS:
            if key in self: dict.__setitem__(self, key, deepcopy(self[key]))
        nested = self.get('task_contract')
        if isinstance(nested, ContractMetadata): nested.detach()
        self._owner = None

    def __setitem__(self, key: str, value: Any) -> None:
        if self._owner is not None and key in CONTRACT_FIELDS:
            setattr(self._owner, key, value)
        elif self._owner is not None and key == 'task_contract':
            if not isinstance(value, dict): raise ValueError('task_contract must be an object')
            projection = ContractMetadata(value, owner=self._owner)
            for field in CONTRACT_FIELDS:
                dict.__setitem__(projection, field, getattr(self._owner, field))
            dict.__setitem__(self, key, projection)
        else:
            dict.__setitem__(self, key, value)

    def __delitem__(self, key: str) -> None:
        if self._owner is not None and key in CONTRACT_FIELDS:
            setattr(self._owner, key, CONTRACT_FIELDS[key]())
        dict.__delitem__(self, key)

    def update(self, values=(), /, **kwargs: Any) -> None:
        for key, value in dict(values, **kwargs).items(): self[key] = value

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self: self[key] = default
        return self[key]

    def pop(self, key: str, *default: Any) -> Any:
        if key not in self:
            if default: return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def popitem(self):
        if not self: raise KeyError('popitem(): dictionary is empty')
        key = next(reversed(self))
        return key, self.pop(key)

    def clear(self) -> None:
        for key in list(self): del self[key]

    # Like dict itself, |= accepts key/getitem mappings and iterables while |
    # only accepts dictionaries. Mypy checks these unequal operand contracts
    # as one operator; typeshed's dict.__ior__ uses the same misc exception.
    @overload  # type: ignore[misc]
    def __ior__(self, values: MetadataMapping, /) -> Self: ...
    @overload
    def __ior__(self, values: Iterable[tuple[str, Any]], /) -> Self: ...
    @overload
    def __ior__(self, values: dict[K, V], /) -> dict[str | K, Any | V]: ...
    def __ior__(self, values: Any, /) -> Any:  # type: ignore[misc]
        self.update(values)
        return self

    def __deepcopy__(self, memo):
        # Worker inputs receive a detached plain dict, not a pointer back to a
        # graph node. TaskNode.__setstate__ binds its own copied projection.
        return deepcopy(dict(self), memo)
