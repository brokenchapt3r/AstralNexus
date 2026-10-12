"""ExceptionGroup type shape for the conservative 3.10 mypy target.

Runtime support is Python >=3.11, as declared in setup.py.
"""
from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    # Declaration only: the unified 3.10-target check sees a faithful shape
    # (constructor + ``exceptions`` member access after isinstance), while
    # the 3.11+ runtime binds the genuine builtin below. mypy treats the
    # else branch as unreachable and keeps this class as the only binding.
    class ExceptionGroup(BaseException):
        exceptions: Sequence[BaseException]
else:
    if not hasattr(builtins, "ExceptionGroup"):
        raise ImportError("AstralNexus requires Python 3.11 or newer")
    ExceptionGroup = getattr(builtins, "ExceptionGroup")

__all__ = ["ExceptionGroup"]
