"""Normalization helpers for heterogeneous append-only runtime events."""
from __future__ import annotations

from typing import Any


def event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else event


def event_type_of(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "")


def node_id_of(payload: dict[str, Any]) -> str:
    return str(
        payload.get("node_id")
        or payload.get("task_id")
        or payload.get("current_task")
        or ""
    ).strip()


def first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if item not in (None, "")]


def lifecycle_event_to_trace(event: dict[str, Any]) -> dict[str, Any]:
    """Convert the lifecycle side log to the canonical dashboard event shape."""
    text = str(event.get("event") or "")
    if text == "created":
        event_type = "agent.spawned"
    elif text.startswith("transitioned"):
        event_type = "agent.state_changed"
    elif text.startswith("error"):
        event_type = "agent.failed"
    else:
        event_type = "agent.lifecycle"
    return {
        "event_type": event_type,
        "recorded_at": event.get("timestamp", ""),
        "payload": {
            "agent_id": event.get("agent_id", ""),
            "role": event.get("role", "unknown"),
            "node_id": event.get("node_id", ""),
            "status": event.get("state", "unknown"),
            "reason": text,
            "source": "agent_lifecycle.jsonl",
        },
    }
