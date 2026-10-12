"""Write grants belong to committed graph metadata, not planner proposals."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from AstralNexus.core.output_paths import identity_key, paths_overlap, same_or_under

TRANSFER_KEY = "write_permission_transfers"
INACTIVE = frozenset({"completed", "failed", "superseded", "cancelled"})


def committed_transfers(nodes: Mapping[str, Any]) -> list[dict[str, str]]:
    result = []
    for node in nodes.values():
        for entry in node.metadata.get(TRANSFER_KEY, []) or []:
            if isinstance(entry, dict) and entry.get("to_node") == node.node_id:
                result.append({key: str(entry.get(key) or "")
                               for key in ("path", "from_node", "to_node")})
    return result


def validate_current_holders(
    projected: Mapping[str, Mapping[str, Any]],
    transfers: Iterable[Mapping[str, Any]],
) -> None:
    """Reject conflicts before the graph commit, including missing owned_paths."""
    for transfer in transfers:
        holder = str(transfer.get("to_node") or "")
        path = identity_key(transfer.get("path"))
        existing = projected.get(holder)
        if not path or existing is None or existing.get("status") in INACTIVE:
            continue
        for node_id, candidate in projected.items():
            if node_id == holder or candidate.get("status") in INACTIVE:
                continue
            for declared in candidate.get("owned_paths", []) or []:
                if paths_overlap(path, declared):
                    raise ValueError(
                        f"current write authority conflict: {node_id} conflicts with "
                        f"{holder}: {path} (claimed {declared})"
                    )


def validate_staged_transfers(projected: Mapping[str, Mapping[str, Any]],
                              proposals: Iterable[Mapping[str, Any]]) -> None:
    for proposal in proposals:
        target = str(proposal.get("node_id") or proposal.get("task_id") or "")
        for transfer in (proposal.get("metadata") or {}).get(TRANSFER_KEY, []) or []:
            source = projected.get(str(transfer.get("from_node") or ""), {})
            path = identity_key(transfer.get("path"))
            claimant = projected.get(target, {})
            if (transfer.get("to_node") != target or source.get("status") != "completed"
                    or not path or not any(same_or_under(owned, path) for owned in source.get("owned_paths", []))
                    or not any(same_or_under(owned, path) for owned in claimant.get("owned_paths", []))):
                raise ValueError(f"invalid staged write transfer: {target}: {path}")
