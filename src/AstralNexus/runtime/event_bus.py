"""Public runtime observability events derived from the canonical trace stream."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from AstralNexus.core.artifacts import append_jsonl
from AstralNexus.runtime.public_rationale import public_rationale


PUBLIC_RUNTIME_EVENT_TYPES = frozenset(
    {
        "task.received",
        "planning.started",
        "planning.summary",
        "agent.spawned",
        "agent.activity",
        "tool.started",
        "tool.finished",
        "artifact.created",
        "recovery.triggered",
        "task.completed",
        "task.failed",
    }
)

_PRIVATE_KEYS = frozenset(
    {
        "analysis",
        "chain_of_thought",
        "cot",
        "internal_reasoning",
        "reasoning_content",
        "reasoning",
        "thinking",
        "thoughts",
    }
)


RuntimeSubscriber = Callable[[dict[str, Any]], None]


class RuntimeEventBus:
    """Thread-safe projection of internal trace events into public progress events.

    The bus is an observability boundary, not another control plane. It never
    changes graph or lifecycle state and deliberately excludes private model
    reasoning from its public payloads.
    """

    def __init__(self, run_dir: Path | None = None, *, history_size: int = 1000):
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.event_path = self.run_dir / "runtime_events.jsonl" if self.run_dir else None
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, history_size))
        self._subscribers: list[RuntimeSubscriber] = []
        self._lock = threading.RLock()
        self._recent_signatures: dict[tuple[str, str, str, str], float] = {}

    def subscribe(self, subscriber: RuntimeSubscriber) -> Callable[[], None]:
        with self._lock:
            if subscriber not in self._subscribers:
                self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            # Payloads contain nested lists/dicts. A shallow copy lets a UI or
            # diagnostic consumer rewrite the canonical replay history.
            return deepcopy(list(self._history))

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        recorded_at: str | None = None,
        source_event: str = "",
    ) -> dict[str, Any]:
        if event_type not in PUBLIC_RUNTIME_EVENT_TYPES:
            raise ValueError(f"unsupported public runtime event: {event_type}")
        public_payload = _sanitize(dict(payload or {}))
        public_payload.setdefault("runtime_event_id", f"rte-{uuid.uuid4().hex[:12]}")
        public_payload.setdefault("source_event", source_event or event_type)
        public_payload.setdefault("collapsible", True)
        public_payload.setdefault(
            "level",
            "detail" if event_type in {"agent.activity", "tool.started", "tool.finished"} else "info",
        )
        event = {
            "event_type": event_type,
            "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
            "payload": public_payload,
        }
        with self._lock:
            self._history.append(deepcopy(event))
            subscribers = list(self._subscribers)
            if self.event_path is not None:
                append_jsonl(self.event_path, event)
        for subscriber in subscribers:
            try:
                # One callback may not mutate what later callbacks, history
                # replay, or the publisher observe.
                subscriber(deepcopy(event))
            except Exception:
                continue
        return deepcopy(event)

    def ingest_trace(
        self,
        event_type: str,
        payload: dict[str, Any] | None,
        *,
        recorded_at: str | None = None,
    ) -> list[dict[str, Any]]:
        mapped = _map_trace_event(str(event_type), dict(payload or {}))
        if any(item[0] == "task.received" for item in mapped):
            with self._lock:
                if any(
                    item.get("event_type") == "task.received"
                    for item in self._history
                ):
                    mapped = [item for item in mapped if item[0] != "task.received"]
        published: list[dict[str, Any]] = []
        now = time.monotonic()
        with self._lock:
            self._recent_signatures = {
                key: seen_at
                for key, seen_at in self._recent_signatures.items()
                if now - seen_at < 2.0
            }
        for public_type, public_payload in mapped:
            signature = (
                public_type,
                str(public_payload.get("node_id") or ""),
                str(public_payload.get("summary") or ""),
                str(public_payload.get("rationale") or ""),
            )
            with self._lock:
                seen_at = self._recent_signatures.get(signature)
                if seen_at is not None and now - seen_at < 0.5:
                    continue
                self._recent_signatures[signature] = now
            published.append(
                self.publish(
                    public_type,
                    public_payload,
                    recorded_at=recorded_at,
                    source_event=event_type,
                )
            )
        return published


def _map_trace_event(
    event_type: str, payload: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    node_id = str(payload.get("node_id") or payload.get("task_id") or "")
    role = str(payload.get("role") or payload.get("worker_type") or "")
    actor = str(payload.get("agent_id") or role or "AstralNexus")
    common = {"node_id": node_id, "actor": actor, "role": role}
    common.update({key: payload[key] for key in ("trace_owner_valid", "generation", "owner_id") if key in payload})

    if event_type == "task.source.loaded":
        request = str(payload.get("original_request") or payload.get("goal") or "").strip()
        return [("task.received", {**common, "title": "已接收任务", "summary": request})]
    if event_type == "orchestrator.initialized" and payload.get("goal"):
        return [("task.received", {**common, "title": "已接收任务", "summary": str(payload["goal"])})]
    if event_type == "agent.spawned" and role == "planner":
        return [
            ("planning.started", {**common, "title": "Planner 开始规划", "summary": "正在生成动态任务图与执行契约"}),
            ("agent.spawned", {**common, "title": "已创建 Planner", "summary": str(payload.get("goal") or "控制平面规划实例已就绪")}),
        ]
    if event_type == "agent.spawned":
        return [("agent.spawned", {**common, "title": f"已创建 {role or 'Worker'}", "summary": str(payload.get("goal") or payload.get("reason") or "执行实例已就绪")})]
    if event_type == "governor.graph_patch.proposed":
        explanation = str(payload.get("planning_explanation") or payload.get("decision_rationale") or "").strip()
        count = int(payload.get("add_node_count") or 0)
        return [("planning.summary", {
            **common,
            "title": "Planner 规划完成",
            "summary": f"提议新增 {count} 个任务节点，并按依赖形成 Ready-Wave。",
            "rationale": explanation,
            "details": {"reason_codes": payload.get("reason_codes") or [], "node_count": count},
        })]
    if event_type == "agent.activity.changed":
        activity = payload.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        return [("agent.activity", {
            **common,
            "title": str(activity.get("label") or f"{role or 'Worker'} 正在工作"),
            "summary": str(activity.get("input_preview") or payload.get("summary") or activity.get("type") or "执行当前节点契约"),
            "rationale": _public_rationale(payload, activity),
            "status": str(payload.get("status") or "running"),
            "details": {"activity_type": activity.get("type"), "tool": activity.get("tool")},
        })]
    if event_type == "node.result_submitted":
        outcome = str(payload.get("execution_outcome") or payload.get("status") or "").lower()
        succeeded = outcome not in {"failed", "failure", "error", "cancelled", "interrupted"}
        return [("agent.activity", {
            **common,
            "title": f"{role or 'Worker'} 阶段{'完成' if succeeded else '失败'}",
            "summary": str(payload.get("summary") or "节点已提交结构化执行结果"),
            "rationale": _public_rationale(payload),
            "status": "completed" if succeeded else "failed",
            "details": {"execution_id": payload.get("execution_id"), "attempt": payload.get("attempt")},
        })]
    if event_type in {"llm.tool_execution.start", "tool.call.started"}:
        calls = payload.get("calls")
        calls = calls if isinstance(calls, list) else []
        tool_names = [str(item.get("name") or item.get("tool_name")) for item in calls if isinstance(item, dict)]
        if payload.get("tool_name"):
            tool_names.append(str(payload["tool_name"]))
        return [("tool.started", {
            **common,
            "title": "开始调用工具",
            "summary": "、".join(dict.fromkeys(tool_names)) or "执行节点所需工具",
            "rationale": str(payload.get("reason") or "为当前节点获取必要证据或执行结果"),
            "details": {"tools": list(dict.fromkeys(tool_names))},
        })]
    if event_type in {"llm.tool_batch", "tool.call.completed"}:
        calls = payload.get("calls")
        calls = calls if isinstance(calls, list) else []
        names = [str(item.get("name") or item.get("tool_name")) for item in calls if isinstance(item, dict)]
        if payload.get("tool_name"):
            names.append(str(payload["tool_name"]))
        failed = any(
            str(item.get("status", "")).lower() in {"failed", "failure", "error", "cancelled", "interrupted"}
            or item.get("success") is False or bool(item.get("error"))
            or item.get("returncode") not in (None, 0)
            for item in [payload, payload.get("result"), *calls] if isinstance(item, dict)
        )
        return [("tool.finished", {
            **common,
            "title": "工具调用完成",
            "summary": "、".join(dict.fromkeys(names)) or str(payload.get("summary") or "工具结果已返回"),
            "status": "failed" if failed else str(payload.get("status") or "completed"),
            "details": {"tools": list(dict.fromkeys(names)), "result_preview": payload.get("result_preview")},
        })]
    if event_type == "artifact.created":
        path = str(payload.get("path") or payload.get("artifact_path") or payload.get("uri") or "")
        return [("artifact.created", {**common, "title": "交付物已创建", "summary": path or str(payload.get("description") or "产物已落盘"), "details": {"path": path}})]
    if event_type in {
        "recovery.triggered",
        "recovery.attempted",
        "recovery.planned",
        "orchestrator.autonomous_cycle.recovering",
        "orchestrator.verification_recovery.applied",
        "orchestrator.dynamic_spawn.exception",
    } or event_type.endswith(".recovery.decided"):
        return [("recovery.triggered", {
            **common,
            "title": "触发故障恢复",
            "summary": str(payload.get("reason") or payload.get("error") or payload.get("fault_id") or event_type),
            "rationale": str(payload.get("decision_rationale") or payload.get("recovery_strategy") or "根据运行反馈执行恢复或局部重规划"),
        })]
    if event_type == "orchestrator.completed":
        return [("agent.activity", {
            **common,
            "title": "执行图完成，正在整理最终交付",
            "summary": "所有执行节点已收敛，正在生成交付物并执行最终验收。",
            "rationale": "任务图完成不等于产品交付完成；最终状态将在交付物落盘和验收结束后确认。",
            "status": "verifying",
        })]
    if event_type == "interactive.run.summary":
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        status = str(payload.get("status") or result.get("status") or "completed").lower()
        completed = status == "completed"
        return [("task.completed" if completed else "task.failed", {
            **common,
            "title": "任务验收完成" if completed else "任务验收未通过",
            "summary": str(payload.get("summary") or payload.get("text") or (
                "最终交付物已经落盘并通过验收" if completed else f"任务以 {status} 状态结束"
            )),
            "status": "completed" if completed else "failed",
            "details": {key: payload.get(key) for key in ("total_nodes", "successful_nodes", "failed_nodes", "recovery_count", "output_dir") if payload.get(key) is not None},
        })]
    return []


def _public_rationale(*sources: Any) -> str:
    """Backward-compatible alias for the official public-rationale interface."""
    return public_rationale(*sources)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value
