"""Explicit, versioned legacy graph migrations; current graphs are not healed by text."""
from __future__ import annotations
from typing import Callable, Any
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.core.graph_transaction import TaskGraphTransaction

CURRENT_CHECKPOINT_SCHEMA = 1


class CheckpointSchemaError(ValueError):
    pass


def _legacy_retired_owner_conflict(graph: TaskGraph, emit: Callable[[str, dict], Any]) -> None:
    for node in graph.nodes.values():
        if (node.status == "failed" and node.metadata.get("_recovery_patch_rejected")
                and "graph patch update has overlapping output ownership" in str(node.metadata.get("_terminal_reason", ""))
                and not node.metadata.get("_ownership_takeover_recovery_rearmed")):
            node.metadata["_ownership_takeover_recovery_rearmed"] = True
            node.metadata["_terminal_failure"] = False
            node.metadata["_recovery_patch_rejected"] = False
            graph.update_node_status(node.node_id, "repair_pending")
            graph.update_node_status(node.node_id, "ready")
            emit("checkpoint.migration.retired_owner_conflict", {"node_id": node.node_id,
                "migration": "legacy-v0-to-v1", "diagnostic_preserved": node.metadata.get("_terminal_reason")})


def _legacy_transfer_residue(graph: TaskGraph, emit: Callable[[str, dict], Any]) -> None:
    for torn in graph.nodes.values():
        target_id = str(torn.metadata.get("ownership_transfer_to") or "").strip()
        if not target_id or target_id == torn.node_id or torn.metadata.get("_ownership_transfer_strip_completed"):
            continue
        target = graph.get_node(target_id)
        if target is None or target.status in {"cancelled", "superseded"}:
            emit("checkpoint.ownership_transfer_strip_skipped", {"node_id": torn.node_id,
                "transferee": target_id, "reason": "transferee_absent_or_retired"})
            continue
        paths = [str(item) for item in (torn.owned_paths or []) if str(item).strip()]
        artifacts = [str(item) for item in (torn.output_artifacts or []) if str(item).strip()]
        ignored = getattr(torn, '_ignored_legacy_contracts', {})
        meta_paths = [str(item) for item in (torn.metadata.get("owned_paths") or ignored.get('owned_paths') or []) if str(item).strip()]
        meta_artifacts = [str(item) for item in (torn.metadata.get("output_artifacts") or ignored.get('output_artifacts') or []) if str(item).strip()]
        if not (paths or artifacts or meta_paths or meta_artifacts): continue
        torn.owned_paths = []
        torn.output_artifacts = []
        if "owned_paths" in torn.metadata: torn.metadata["owned_paths"] = []
        if "output_artifacts" in torn.metadata: torn.metadata["output_artifacts"] = []
        torn.metadata["_ownership_transfer_strip_completed"] = True
        emit("checkpoint.ownership_transfer_completed", {"node_id": torn.node_id, "transferee": target_id,
            "status": torn.status, "stripped_paths": paths or meta_paths, "stripped_artifacts": artifacts or meta_artifacts,
            "canonical_stripped_paths": paths, "ignored_legacy_paths": meta_paths})


MIGRATIONS = {0: (_legacy_retired_owner_conflict, _legacy_transfer_residue)}


def migrate_checkpoint_graph(graph: TaskGraph, version: int, emit: Callable[[str, dict], Any]) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 0 or version > CURRENT_CHECKPOINT_SCHEMA:
        raise CheckpointSchemaError(f"unsupported checkpoint schema: {version!r}")
    events: list[tuple[str, dict]] = []
    with TaskGraphTransaction(graph):
        while version < CURRENT_CHECKPOINT_SCHEMA:
            for migrate in MIGRATIONS[version]:
                migrate(graph, lambda kind, payload: events.append((kind, payload)))
            events.append(("checkpoint.schema.migrated", {"from_version": version, "to_version": version + 1}))
            version += 1
    for kind, payload in events:
        emit(kind, payload)
    return version
