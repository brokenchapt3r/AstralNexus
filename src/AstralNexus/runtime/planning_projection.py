"""Project retired task history without turning it into current requirements."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def project_planning_graph(records: list[dict[str, Any]], *, focus_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Keep live contracts intact and retire only unreferenced historical detail.

    Identity, ownership, dependency and artifact links remain available for graph
    validation. Full retired contracts remain in the checkpoint, never mutated.
    """
    retired = {"superseded", "cancelled"}
    required = set(focus_ids)
    by_id = {record["node_id"]: record for record in records}
    required.update(record["node_id"] for record in records if record.get("status") not in retired)
    pending = list(required)
    while pending:
        record = by_id.get(pending.pop(), {})
        for dependency in record.get("dependencies", []):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    projected = []
    for record in records:
        if record["node_id"] in required:
            projected.append(dict(record))
            continue
        item = dict(record)
        omitted = {key: item.pop(key) for key in (
            "goal", "acceptance_criteria", "handoff_contract", "worker_profile",
            "readiness_blockers", "required_capabilities",
        ) if key in item}
        item["historical_contract"] = {
            "checkpoint_node_id": record["node_id"],
            "sha256": hashlib.sha256(json.dumps(omitted, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
            "omitted_fields": list(omitted),
            "rule": "Retired, unreferenced historical contract omitted from this request; not a current requirement or proof of completion. Do not reactivate this node based on this projection.",
        }
        projected.append(item)
    return projected
