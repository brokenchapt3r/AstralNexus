"""Historical source replay through real tool execution, local HTTP tests and publish.

This is an offline integration fixture, not autonomous model development. Only
the provider response is scripted; files, processes, HTTP and receipts are real.
"""
import hashlib
import json
import os
import importlib.util
from pathlib import Path
import shutil
import shlex
import sys

import pytest

from AstralNexus.core.delivery import DeliveryManager
from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files
from AstralNexus.runtime.llm_client import LLMClient
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.tools.registry import discover_builtin_tools, registry


def _browser_provider_launchable() -> bool:
    """Environment boundary probe (R16; P0-4 four-layer boundary, 台账 §25/§26).

    The historical browser driver (fixtures/offline_browser_probe.py) performs a
    REAL user flow through Playwright: login, create, HTTP-response assertion,
    reload persistence, admin views, screenshot. Playwright's driver transport
    requires anonymous pipes; the sandbox refuses CreatePipe (WinError 5), and
    the pipe-free browser forms die in crashpad/mojo (P0-4 evidence chain).
    The probe script then honestly reports the failed browser check (rc=1) and
    the acceptance store correctly refuses the unanchored execution — that is
    the chain working as designed, not a defect to paper over. Skip the whole
    replay when no browser provider can launch; never fake the browser leg.
    """
    if importlib.util.find_spec("playwright") is None:
        return False
    if sys.platform != 'win32':
        return True
    try:
        import _winapi
    except ImportError:
        return True
    try:
        read_handle, write_handle = _winapi.CreatePipe(None, 0)
    except OSError:
        return False
    _winapi.CloseHandle(read_handle)
    _winapi.CloseHandle(write_handle)
    return True


pytestmark = pytest.mark.skipif(
    not _browser_provider_launchable(),
    reason='browser dependency absent or provider launch unavailable (CreatePipe; '
           'P0-4 boundary) — the historical browser leg requires a real browser')


