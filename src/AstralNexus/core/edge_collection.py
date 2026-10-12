"""List-compatible edge mutations with revision invalidation and graph locking."""
from __future__ import annotations
from copy import deepcopy
from functools import wraps


def mutating(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            try:
                return function(self, *args, **kwargs)
            finally:
                self._changed()
    return wrapped


class EdgeCollection(list):
    def __init__(self, values, lock, changed):
        super().__init__(values)
        self._lock, self._changed = lock, changed
    def __deepcopy__(self, memo):
        return deepcopy(list(self), memo)
    @mutating
    def append(self, value): return super().append(value)
    @mutating
    def extend(self, values): return super().extend(values)
    @mutating
    def insert(self, index, value): return super().insert(index, value)
    @mutating
    def __setitem__(self, index, value): return super().__setitem__(index, value)
    @mutating
    def __delitem__(self, index): return super().__delitem__(index)
    @mutating
    def pop(self, index=-1): return super().pop(index)
    @mutating
    def clear(self): return super().clear()
    @mutating
    def remove(self, value): return super().remove(value)
    @mutating
    def reverse(self): return super().reverse()
    @mutating
    def sort(self, *args, **kwargs): return super().sort(*args, **kwargs)
    @mutating
    def __iadd__(self, values): return super().__iadd__(values)
    @mutating
    def __imul__(self, value): return super().__imul__(value)
