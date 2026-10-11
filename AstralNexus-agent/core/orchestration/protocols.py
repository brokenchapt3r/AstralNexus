"""Shared state contract used by orchestration responsibility modules.

There is one concrete runtime context (Orchestrator). These declarations make
cross-component dependencies explicit; they do not allocate or duplicate state.
"""
from __future__ import annotations
from typing import Any, Protocol, TYPE_CHECKING
from threading import RLock
from queue import Queue

if TYPE_CHECKING:
    from AstralNexus.core.governor import GlobalGovernor
    from AstralNexus.core.verifier import Verifier
    from AstralNexus.core.memory_federation import MemoryFederation
    from AstralNexus.core.resource_scheduler import ResourceScheduler
    from AstralNexus.core.runtime_state import RuntimeStateManager
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


class RuntimeContext(Protocol):
    graph_store: SQLiteTaskGraphStore
    memory_federation: MemoryFederation
    governor: GlobalGovernor
    verifier: Verifier
    resource_scheduler: ResourceScheduler
    runtime_state_manager: RuntimeStateManager
    run_id: str
    max_parallel: int
    max_iterations: int
    iteration_count: int
    recovery_churn_budget: int
    _execution_lock: RLock
    _recovery_budget_exhausted: bool
    _completion_no_progress: int
    _completion_gap_fingerprint: str
    _completion_gap_repeats: int
    _interrupt_reason: str
    _interrupt_queue: Queue[dict[str, Any]] | None
    _lineage_stream_usage: dict[tuple[str, str], int]
    _lineage_envelope_cache: dict[str, list[dict[str, Any]]]
