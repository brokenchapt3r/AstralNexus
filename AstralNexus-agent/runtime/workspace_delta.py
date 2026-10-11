"""Cap-outcome layering: objective workspace delta evidence at attempt end.

P1 (DeerFlow port, doc 30): live r13c burned four consecutive workers on the
same contract.  Three died at the wall-clock budget cap, and the framework
booked "landed 90% of the deliverables" (N-123 attempt 2) and "landed
nothing at all" (N-123 attempt 1, N-126) as the SAME undifferentiated
failure.  DeerFlow's status_contract never does this: a capped run that
produced usable output stays usable (``completed`` + cap annotation), only a
capped run with NO output is ``failed`` + ``stop_reason`` — and its
``workspace_changes`` machinery records objectively which files a run
actually touched.

This module supplies the AstralNexus analog: a pure classifier over the
execution store's existing ``snapshot_files`` / ``_file_inventory_delta``
primitives.  The worker layer captures an owned-path baseline at attempt
start and re-scans at every exit (success, honest failure, interruption);
the resulting payload rides along in ``AgentResponse.metrics``,
``context.metadata["workspace_delta"]`` and ``node.metadata`` so recovery
routing can tell a successor "your predecessor already landed these bytes —
continue from them" apart from "nothing was ever written — change strategy".

Vocabulary discipline (DeerFlow acceptance_checks port): the classification
describes BYTES ON DISK, never task acceptance.  ``capped_with_output``
means owned files changed before the cap; it never means the work passed.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .execution_store import _file_inventory_delta

#: Classification values. ``capped_*`` only when a budget dimension was
#: exhausted during the attempt; the plain values describe non-capped exits.
CLASSIFICATION_CAPPED_WITH_OUTPUT = "capped_with_output"
CLASSIFICATION_CAPPED_ZERO_OUTPUT = "capped_zero_output"
CLASSIFICATION_WITH_OUTPUT = "with_output"
CLASSIFICATION_ZERO_OUTPUT = "zero_output"

#: Bound on per-category path listings inside the payload so a large
#: generated tree cannot bloat node metadata or the trace event.
DEFAULT_DETAIL_LIMIT = 32


def build_workspace_delta(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    *,
    exhausted_reason: Optional[str] = None,
    node_id: str = "",
    detail_limit: int = DEFAULT_DETAIL_LIMIT,
) -> Dict[str, Any]:
    """Classify what an attempt actually landed inside its owned paths.

    Args:
        before: ``snapshot_files`` inventory captured at attempt start.
        after: ``snapshot_files`` inventory captured at attempt end.
        exhausted_reason: the budget dimension that fired (``max_seconds`` /
            ``max_tool_calls`` / ...), or ``None`` when the attempt ended
            without hitting a cap.
        node_id: recorded verbatim for trace/metadata correlation.
        detail_limit: max entries per change category in ``entries``.

    Returns:
        A JSON-serializable payload: ``classification``, ``counts``,
        bounded ``entries`` (path + kind + before/after bytes + sha prefix),
        ``exhausted_reason`` and ``entries_truncated``.
    """
    before = dict(before or {})
    after = dict(after or {})
    delta = _file_inventory_delta(before, after)
    counts = {label: len(delta[label]) for label in ("added", "removed", "changed")}
    changed_total = sum(counts.values())
    capped = bool(str(exhausted_reason or "").strip())
    if capped:
        classification = (
            CLASSIFICATION_CAPPED_WITH_OUTPUT if changed_total
            else CLASSIFICATION_CAPPED_ZERO_OUTPUT
        )
    else:
        classification = (
            CLASSIFICATION_WITH_OUTPUT if changed_total else CLASSIFICATION_ZERO_OUTPUT
        )

    entries: list[Dict[str, Any]] = []
    for label in ("added", "removed", "changed"):
        for path in delta[label][: max(0, int(detail_limit))]:
            entry: Dict[str, Any] = {"path": path, "kind": label}
            previous = before.get(path)
            current = after.get(path)
            if isinstance(previous, dict):
                entry["before_bytes"] = previous.get("bytes")
                entry["before_sha256"] = str(previous.get("sha256") or "")[:16]
            if isinstance(current, dict):
                entry["after_bytes"] = current.get("bytes")
                entry["after_sha256"] = str(current.get("sha256") or "")[:16]
            entries.append(entry)

    return {
        "node_id": str(node_id or ""),
        "classification": classification,
        "exhausted_reason": str(exhausted_reason or "") or None,
        "counts": counts,
        "entries": entries,
        "entries_truncated": changed_total > len(entries),
    }
