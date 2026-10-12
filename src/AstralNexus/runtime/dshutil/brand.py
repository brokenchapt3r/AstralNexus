"""Stateless branded primitive types (Python adaptation).

Port of ``@deepseek-ai/dsh-brand`` (commit 5badb15). Upstream brands are
compile-time only: ``BrandString<T>``/``BrandNumber<T>`` erase to plain
string/number at runtime, giving nominal safety without runtime cost.

Python has no structural type erasure, so the faithful adaptation is
``typing.NewType``: brands are distinct names for static checkers (mypy
rejects passing an unbranded ``str`` where a brand is expected) while
remaining the base type at runtime. The ``brand_*`` constructors are the
runtime validation seam: they check the base type once at the boundary where
an untrusted string/number becomes branded.
"""
from __future__ import annotations

from typing import Any, NewType, TypeVar

__all__ = ["brand", "BrandString", "BrandNumber", "brand_string", "brand_number"]


def brand(name: str, base: type = str) -> Any:
    """Create a nominal brand over ``base`` (mypy-visible NewType)."""
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError(f"brand name must be an identifier: {name!r}")
    if base not in (str, int, float, bytes):
        raise ValueError(f"brand base must be a primitive type, got {base!r}")
    return NewType(name, base)  # type: ignore[misc]


BrandString = TypeVar("BrandString", bound=str)
BrandNumber = TypeVar("BrandNumber", int, float)


def brand_string(value: Any) -> str:
    """Runtime boundary check for a branded string; returns the same object."""
    if not isinstance(value, str):
        raise TypeError(f"branded string expected, got {type(value).__name__}")
    return value


def brand_number(value: Any) -> Any:
    """Runtime boundary check for a branded number; returns the same object."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"branded number expected, got {type(value).__name__}")
    return value
