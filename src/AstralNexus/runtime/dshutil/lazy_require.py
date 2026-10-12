"""Success-cached lazy module loading.

Port of ``@deepseek-ai/dsh-lazy-require`` (commit 5badb15). Contract kept:
- The returned zero-argument loader imports on first use and caches the module
  after success only — a failed load is NOT cached and the next call retries.
- The loader takes no arguments, so call sites cannot accidentally load a
  different specifier.

Python adaptation: ``require``'s caller-relative resolution does not exist for
absolute Python imports; the specifier is an absolute module name resolved
with ``importlib.import_module``. Loading is serialized with a lock so
concurrent first calls import once.
"""
from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Callable


def create_lazy_import(specifier: str) -> Callable[[], ModuleType]:
    """Zero-argument, success-cached loader for one absolute module name."""
    if not isinstance(specifier, str) or not specifier.strip():
        raise ValueError("module specifier must be a non-empty string")
    cache: list = []
    lock = threading.Lock()

    def load() -> ModuleType:
        if cache:
            return cache[0]
        with lock:
            if not cache:  # another thread may have completed the import
                cache.append(importlib.import_module(specifier))
            return cache[0]

    return load
