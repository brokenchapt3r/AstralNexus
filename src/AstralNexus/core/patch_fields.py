"""Shape normalization at the model/graph boundary, without inventing work."""
from __future__ import annotations
from typing import Any
from copy import deepcopy


def text_items(value: Any) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def artifact_references(value: Any) -> list[str]:
    """Read reference identities, never Python reprs of reference objects."""
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ValueError("artifact references must be an array or one reference")
    references = []
    for item in values:
        reference = (item.get("artifact_id") or item.get("path") or item.get("artifact_path")) if isinstance(item, dict) else item
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("artifact reference requires an artifact_id or path string")
        text = reference.strip().replace("\\", "/")
        if text not in references:
            references.append(text)
    return references


def model_patch_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(parsed)
    for name in ("add_nodes", "update_nodes", "add_edges", "remove_edges", "background_candidates", "reason_codes"):
        value = result.get(name)
        if value is None:
            result[name] = []
        elif not isinstance(value, list):
            raise ValueError(f"planner {name} must be an array")
    cancel = result.get("cancel_nodes")
    if isinstance(cancel, str):
        cancel = [cancel]
    if cancel is None:
        cancel = []
    if not isinstance(cancel, list):
        raise ValueError("planner cancel_nodes must be an array or one node ID")
    for item in cancel:
        identity = item.get("node_id") if isinstance(item, dict) else item
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("planner cancellation requires a node ID")
    result["cancel_nodes"] = cancel
    mode = str(result.get("candidate_mode") or "merge").strip().lower()
    if mode not in {"merge", "replace"}:
        raise ValueError(f"invalid candidate_mode: {mode}")
    result["candidate_mode"] = mode
    for raw in [*result["add_nodes"], *result["update_nodes"]]:
        if not isinstance(raw, dict):
            raise ValueError("planner node must be an object")
        metadata = raw.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("planner node metadata must be an object")
        for container in (raw, metadata or {}):
            for field in ("input_artifacts", "output_artifacts"):
                if field in container:
                    container[field] = artifact_references(container[field])
    for raw in result["add_nodes"]:
        if not isinstance(raw, dict):
            raise ValueError("planner node must be an object")
        for key in ("dependencies", "required_capabilities"):
            value = raw.get(key)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"planner node {key} must be an array")
            raw[key] = text_items(value)
        for key in ("metadata", "budget"):
            value = raw.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"planner node {key} must be an object")
            if value is None:
                raw[key] = {}
        try:
            priority = int(raw.get("priority", 50))
        except (ValueError, TypeError, OverflowError):
            priority = 50
            raw.setdefault("metadata", {})["priority_defaulted"] = True
        raw["priority"] = max(0, min(100, priority))
    return result
