from __future__ import annotations
from AstralNexus.runtime.recovery_operation import recovery_operation
from AstralNexus.core.runtime_state import synchronized_state

import json
import glob
import hashlib
import logging
import os
import re
import tempfile
import time
import traceback
from queue import Empty
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, TYPE_CHECKING

if TYPE_CHECKING:
    from AstralNexus.core.governor import TaskGraphPatch

logger = logging.getLogger(__name__)


from .models import ExecutionRunResult


# JSON 安全化只有**一份**实现（`core/json_safety.py`）。本文件曾有自己的一份，
# 与 `core/runtime_state.py` 的同名函数语义不同（dataclass / ``to_dict`` 对象
# 在一侧被压成字符串而丢字段）；已统一到共享实现，别名保持 11 处调用点不变。
from AstralNexus.core.json_safety import json_safe as _json_safe

from AstralNexus.core.agent_registry import get_agent_registry
from AstralNexus.core.exceptions import (
    AstralNexusError,
    CoreError,
    InterruptionRequested,
    RealLLMRequiredError,
    RecoveryError,
    Severity,
    format_tool_error,
)
from AstralNexus.core.fault_recovery import Fault
from AstralNexus.core.identity import CODE_NAME, PRODUCT_NAME
from AstralNexus.core.lifecycle import AgentLifecycleManager
from AstralNexus.runtime.model_selector import DEFAULT_MODEL
from AstralNexus.core.resource_scheduler import (
    Resource,
    ResourceLeaseError,
    ResourcePool,
    ResourceScheduler,
    ResourceTier,
)
from AstralNexus.core.router import Router
from AstralNexus.core.state_machine import (
    TaskStatus,
    assert_transition,
    can_transition,
    transition_repair_plan,
)
from AstralNexus.core.task_graph import (
    Artifact,
    ArtifactAcceptanceStatus,
    Fact,
    TaskGraph,
)
from AstralNexus.core.memory_federation import MemoryFederation
from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
from AstralNexus.core.output_paths import (
    PathIdentityError,
    RunOutputLayout,
    declared_output_claims,
    declared_output_is_required,
    declared_output_path_tokens,
    declared_tree_member,
    identity_key,
    paths_overlap,
    resolve_declared_output,
    workspace_identity,
)
from AstralNexus.runtime.context import ContextManager
from AstralNexus.runtime.llm_client import LLMClientError
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
from AstralNexus.core.adaptive_concurrency import AdaptiveConcurrency
from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.runtime_observer import RuntimeObserver
from AstralNexus.agents.base import ExecutionOutcome, _extract_response_envelope
from AstralNexus.core.verifier import (
    VerificationDimension,
    VerificationResult,
    VerificationVerdict,
)
# Fix AV (2026-09-17): the delivery verifier must be shown the evidence the
# delivery would actually contain.  The publish layer already excludes
# interpreter/log/probe by-products (Fix Q); the verifier's evidence assembly
# used a bare ``rglob`` with no such exclusion, so ~83 by-product files inside
# ``deliverables/app`` consumed the whole bounded evidence budget and the sealed
# acceptance evidence was never sampled.  Reuse the publish sets rather than
# maintaining a second copy of the list.
from AstralNexus.core.delivery import (
    _PUBLISH_EXCLUDE_PARTS,
    _PUBLISH_EXCLUDE_SUFFIXES,
)


def _delivery_evidence_visible(path: Path) -> bool:
    """Fix AV: True when ``path`` is not a publish-excluded by-product.

    Kept module-level (not a method) so the rule is testable on its own and so
    the verifier's view provably matches ``DeliveryManager._publishable``.
    """
    if set(path.parts) & _PUBLISH_EXCLUDE_PARTS:
        return False
    return not path.name.endswith(_PUBLISH_EXCLUDE_SUFFIXES)


# Fix T (2026-09-15): ``declared_output_path_tokens`` and
# ``resolve_declared_output`` now live in ``core.output_paths`` so the
# Verifier's deterministic-acceptance leaves can reuse the same extraction
# without importing this module (which would close an import cycle).  They
# stay re-exported here: Fix S's regression module and the in-method
# wrappers below import them from ``core.orchestrator``.


def _existing_run_id(run_dir: Path) -> str:
    """The run_id a run directory already declares, or "" when it declares none."""
    from AstralNexus.core.runtime_state import read_authoritative_cursor
    cursor = read_authoritative_cursor(run_dir)
    if cursor and cursor.get("source") == "db_read_failed":
        raise RuntimeError("Cannot determine run identity: checkpoint database is unreadable")
    if cursor and "multiple_fence_identities" in cursor.get("anomalies", []):
        raise ValueError("Multiple checkpoint identities: an explicit run_id is required")
    payload = (cursor or {}).get("cursor") or {}
    value = payload.get("run_id")
    return str(value) if isinstance(value, str) and value.strip() else ""


# b33 S2 (D4, safe half): the declared-root-cause miner's suffix lexer is ONE
# table.  It used to be written twice inside
# ``Orchestrator._resolve_declared_root_causes`` — once as a tuple and once
# again inside the regex alternation — so editing one silently left the other
# stale.  The regex is now derived from this table, and the redundant
# ``token.endswith(...)`` re-check is gone because the alternation is the last
# element of the pattern (so every match already ends with one of these
# suffixes).  The extraction itself is unchanged: same suffixes, same order.
# The ``deliverables|src|data|run`` prefix regex and the evidence-artifact
# heuristic below are NOT this table and are deliberately untouched (S4 owns the
# tokenizer unification, and that half is not behaviour-preserving).
ROOT_CAUSE_TEXT_SUFFIXES = (
    ".py", ".sql", ".bat", ".md", ".json", ".js", ".ts", ".html",
    ".css", ".csv", ".txt", ".sh", ".ps1", ".yaml", ".yml", ".toml",
    ".log",
)


