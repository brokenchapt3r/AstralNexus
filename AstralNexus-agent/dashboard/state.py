"""Materialized control-plane state rebuilt from append-only run evidence."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import threading
from typing import Any

from AstralNexus.core.state_machine import RunStatus, TaskStatus, normalize_status

from .event_parser import (
    event_payload,
    event_type_of,
    first_text,
    lifecycle_event_to_trace,
    node_id_of,
    string_list,
)
from .metrics import RuntimeMetrics
from .models import (
    ACTIVE_AGENT_STATES,
    ACTIVE_TASK_STATES,
    AgentViewState,
    CommunicationLink,
    TERMINAL_TASK_STATES,
    new_task_node,
)


class LiveRunState:
    """Task, population, topology, resource and metric projections."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str, str]] = set()
        self.agents: dict[str, AgentViewState] = {}
        self.communication_edges: set[CommunicationLink] = set()
        self.resources: dict[str, dict[str, Any]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=2000)
        self.activity: deque[dict[str, Any]] = deque(maxlen=300)
        self.runtime_events: deque[dict[str, Any]] = deque(maxlen=500)
        self._runtime_event_ids: set[str] = set()
        self.current_wave: list[str] = []
        self.current_wave_id: int | str | None = None
        self.current_wave_roles: list[str] = []
        self.run_status = RunStatus.CREATED.value
        self.last_updated = ""
        self.original_request = ""
        self.root_node_id = "P-001"
        self.planning_decision: dict[str, Any] = {}
        self.planning_history: list[dict[str, Any]] = []
        self.metrics = RuntimeMetrics()
        self._node_agents: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def apply_lifecycle(self, event: dict[str, Any]) -> None:
        self.apply(lifecycle_event_to_trace(event))

    @staticmethod
    def _project_agent_status(value: Any, current: str) -> tuple[str, str]:
        """Project lifecycle-only labels without weakening TaskStatus.

        Agent side logs include transient UI states such as ``initializing``
        and ``paused``. They are not valid task states, so translate them only
        at the dashboard read boundary and retain the original label for
        diagnostics.
        """
        raw = str(value or current or "")
        translated = {
            "initializing": TaskStatus.ADMITTED.value,
            "created": TaskStatus.ADMITTED.value,
            "active": TaskStatus.RUNNING.value,
            "paused": TaskStatus.BLOCKED.value,
            "success": TaskStatus.COMPLETED.value,
            "failure": TaskStatus.FAILED.value,
        }.get(raw, raw)
        try:
            return normalize_status(translated), raw
        except ValueError:
            return normalize_status(current or TaskStatus.ADMITTED.value), raw

    def apply(self, event: dict[str, Any]) -> None:
        event_type = event_type_of(event)
        payload = event_payload(event)
        node_id = node_id_of(payload)
        with self._lock:
            self.events.append(event)
            self.metrics.apply(event)
            self.last_updated = str(
                event.get("recorded_at") or payload.get("recorded_at") or ""
            )
            self._apply_original_request(event_type, payload)
            self._apply_run_status(event_type, payload)
            self._apply_wave(event_type, payload)
            self._apply_graph_snapshot(event_type, payload)
            self._apply_graph_patch(event, payload)
            self._apply_planning_decision(event_type, payload, event)
            self._apply_task_event(event_type, payload, node_id, event)
            self._apply_recovery_event(event_type, payload, node_id)
            self._apply_agent_event(event_type, payload, node_id)
            self._apply_communication_event(event_type, payload)
            self._apply_resource_event(event_type, payload, node_id)
            self._apply_runtime_event(event_type, payload, event)
            self._record_activity(event_type, payload, node_id)

    def _apply_runtime_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        public_types = {
            "task.received", "planning.started", "planning.summary",
            "agent.spawned", "agent.activity", "tool.started",
            "tool.finished", "artifact.created", "recovery.triggered",
            "task.completed",
            "task.failed",
        }
        if event_type not in public_types:
            return
        runtime_event_id = str(payload.get("runtime_event_id") or "")
        if runtime_event_id and runtime_event_id in self._runtime_event_ids:
            return
        if (
            event_type == "agent.spawned"
            and payload.get("source") == "agent_lifecycle.jsonl"
        ):
            # The lifecycle side log is already projected into Agent
            # Population. Its generic ``created`` entry is not a meaningful
            # user-facing progress update and duplicates the runtime event.
            return
        if runtime_event_id:
            self._runtime_event_ids.add(runtime_event_id)
        title_by_type = {
            "task.received": "已接收任务",
            "planning.started": "Planner 开始规划",
            "planning.summary": "规划摘要",
            "agent.spawned": "已创建 Agent 实例",
            "agent.activity": "Agent 正在工作",
            "tool.started": "开始调用工具",
            "tool.finished": "工具调用完成",
            "artifact.created": "交付物已创建",
            "recovery.triggered": "触发故障恢复",
            "task.completed": "任务执行完成",
            "task.failed": "任务验收未通过",
        }
        runtime_event = {
            "event_type": event_type,
            "title": str(payload.get("title") or title_by_type[event_type]),
            "summary": str(
                payload.get("summary")
                or payload.get("goal")
                or payload.get("path")
                or payload.get("reason")
                or ""
            ),
            "rationale": str(payload.get("rationale") or ""),
            "details": payload.get("details"),
            "actor": str(payload.get("actor") or payload.get("role") or ""),
            "node_id": str(payload.get("node_id") or ""),
            "status": str(payload.get("status") or ""),
            "recorded_at": str(event.get("recorded_at") or self.last_updated),
            "collapsible": bool(payload.get("collapsible", True)),
        }
        self.runtime_events.append(runtime_event)
        node_id = runtime_event["node_id"]
        if node_id:
            node = self.nodes.setdefault(
                node_id, new_task_node(node_id, self.last_updated)
            )
            if payload.get("role"):
                node["role"] = str(payload["role"]).lower()
            rationale = runtime_event["rationale"]
            if rationale:
                node["public_reasoning"] = rationale
            reports = node.setdefault("stage_reports", [])
            report = {
                "event_type": event_type,
                "title": runtime_event["title"],
                "summary": runtime_event["summary"],
                "rationale": rationale,
                "status": runtime_event["status"],
                "recorded_at": runtime_event["recorded_at"],
            }
            signature = (
                report["event_type"], report["summary"], report["rationale"]
            )
            previous = reports[-1] if reports else {}
            previous_signature = (
                previous.get("event_type"), previous.get("summary"),
                previous.get("rationale"),
            )
            if signature != previous_signature:
                reports.append(report)
                del reports[:-50]

    def _apply_original_request(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Capture the immutable user request outside the mutable task DAG."""
        if event_type == "task.source.loaded":
            request = self._clean_user_request(
                str(payload.get("original_request") or "")
            )
            if not request:
                source = Path(str(payload.get("path") or ""))
                try:
                    request = self._clean_user_request(
                        source.read_text(encoding="utf-8")
                    )
                except OSError:
                    request = str(payload.get("goal") or "").strip()
            if request and not self.original_request:
                self.original_request = request
        elif event_type == "orchestrator.initialized":
            self.root_node_id = str(
                payload.get("root_node_id") or self.root_node_id
            )
            if not self.original_request:
                self.original_request = str(payload.get("goal") or "").strip()

    @staticmethod
    def _clean_user_request(value: str) -> str:
        """Remove legacy staged metadata while preserving multiline intent."""
        text = str(value or "").strip()
        for marker in (
            "\n\n相关会话上下文（仅用于保持既有约束）：",
            "\n\nGovernor 路由契约：",
            "\n\n运行输入工作区（已由天枢 staging",
        ):
            if marker in text:
                text = text.split(marker, 1)[0].rstrip()
        return text

    def _apply_run_status(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        if event_type in {
            "orchestrator.initialized", "task.source.loaded",
            "governor.graph_patch.applied", "node.started",
        }:
            self.run_status = RunStatus.RUNNING.value
        elif event_type in {
            "orchestrator.completed", "interactive.run.summary", "task.completed",
        }:
            self.run_status = RunStatus.COMPLETED.value
            self.current_wave = []
            self.current_wave_id = None
            self.current_wave_roles = []
            for agent in self.agents.values():
                if agent.status in ACTIVE_AGENT_STATES or agent.source == "control_plane":
                    agent.status = TaskStatus.COMPLETED.value
                    agent.updated_at = self.last_updated
        elif event_type in {
            "orchestrator.failed", "interactive.run.error", "task.failed",
        }:
            self.run_status = RunStatus.FAILED.value
            self.current_wave = []
            self.current_wave_id = None
            self.current_wave_roles = []
            for agent in self.agents.values():
                if agent.status in ACTIVE_AGENT_STATES or agent.source == "control_plane":
                    agent.status = TaskStatus.FAILED.value
                    agent.updated_at = self.last_updated

    def _apply_wave(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type not in {
            "router.ready_wave", "orchestrator.ready_wave", "task.ready_wave",
        }:
            return
        wave = (
            payload.get("node_ids")
            or payload.get("ready_nodes")
            or payload.get("nodes")
            or []
        )
        self.current_wave = string_list(wave)
        self.current_wave_id = payload.get("wave_id")
        self.current_wave_roles = string_list(payload.get("roles"))

    def _apply_graph_snapshot(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        if event_type != "task_graph.snapshot":
            return
        snapshot_ids: set[str] = set()
        for item in payload.get("nodes") or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("node_id") or item.get("id") or "").strip()
            if not item_id:
                continue
            snapshot_ids.add(item_id)
            node = self.nodes.setdefault(
                item_id, new_task_node(item_id, self.last_updated)
            )
            for key in (
                "status", "role", "goal", "resource_tier",
                "resource_id", "selected_model", "plane",
            ):
                if item.get(key) not in (None, ""):
                    if key != "status":
                        node[key] = item[key]
                        continue
                    try:
                        node[key] = normalize_status(item[key])
                    except ValueError:
                        node["reported_noncanonical_status"] = str(item[key])
            if isinstance(item.get("metadata"), dict):
                node["metadata"] = dict(item["metadata"])
            node["updated_at"] = self.last_updated
            for dependency in item.get("dependencies", []) or []:
                self.edges.add((str(dependency), item_id, "dependency"))
        if snapshot_ids:
            self.nodes = {
                key: value for key, value in self.nodes.items()
                if key in snapshot_ids
            }
        snapshot_edges = self._parse_edges(payload.get("edges") or [])
        if snapshot_edges:
            self.edges = snapshot_edges

    def _apply_graph_patch(
        self, event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        event_type = event_type_of(event)
        if event_type in {"task_graph.edge.added", "graph.edge.added", "edge.added"}:
            edge = self._edge_from_payload(payload)
            if edge:
                self.edges.add(edge)
        semantic = payload.get("semantic_edges") or []
        self.edges.update(self._parse_edges(semantic, default_kind="semantic"))
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            return
        self.edges.update(self._parse_edges(patch.get("add_edges") or []))
        for item in patch.get("remove_edges") or []:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or item.get("from")
            target = item.get("target") or item.get("to")
            if source and target:
                self.edges = {
                    edge for edge in self.edges
                    if not (edge[0] == str(source) and edge[1] == str(target))
                }
        for item in patch.get("add_nodes") or []:
            if not isinstance(item, dict) or not item.get("node_id"):
                continue
            item_id = str(item["node_id"])
            node = self.nodes.setdefault(
                item_id, new_task_node(item_id, self.last_updated)
            )
            self._merge_task_fields(node, item)
        for item in patch.get("update_nodes") or []:
            if not isinstance(item, dict) or not item.get("node_id"):
                continue
            item_id = str(item["node_id"])
            node = self.nodes.setdefault(
                item_id, new_task_node(item_id, self.last_updated)
            )
            self._merge_task_fields(node, item)
        for item in patch.get("cancel_nodes") or []:
            item_id = item.get("node_id") if isinstance(item, dict) else item
            if str(item_id) in self.nodes:
                self.nodes[str(item_id)]["status"] = TaskStatus.CANCELLED.value

    def _apply_planning_decision(
        self,
        event_type: str,
        payload: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        """Project public planning explanations and every replan round."""
        if event_type not in {
            "governor.graph_patch.proposed",
            "governor.graph_patch.applied",
            "orchestrator.graph_patch.applied",
        }:
            return
        reason_codes = string_list(payload.get("reason_codes"))
        explanation = str(
            payload.get("planning_explanation")
            or payload.get("reasoning")  # historical traces only
            or ""
        ).strip()
        added = string_list(payload.get("added"))
        add_node_count = int(payload.get("add_node_count") or 0)
        if event_type == "governor.graph_patch.proposed" and add_node_count > 0:
            round_item = {
                "round": len(self.planning_history) + 1,
                "kind": "initial_plan" if not self.planning_history else "replan",
                "planning_explanation": explanation,
                "reason_codes": reason_codes,
                "add_node_count": add_node_count,
                "added": [],
                "recorded_at": str(event.get("recorded_at") or self.last_updated),
            }
            self.planning_history.append(round_item)
            if not self.planning_decision:
                self.planning_decision = round_item
            return
        if added and self.planning_history:
            pending = next(
                (
                    item for item in reversed(self.planning_history)
                    if not item.get("added")
                ),
                self.planning_history[-1],
            )
            pending["added"] = added
            if reason_codes:
                pending["reason_codes"] = reason_codes
            if explanation:
                pending["planning_explanation"] = explanation

    def _apply_task_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        node_id: str,
        event: dict[str, Any],
    ) -> None:
        creation_events = {
            "node.started", "node.io.input", "task.execution.started",
            "governor.worker_selected",
        }
        if not node_id or (
            node_id not in self.nodes and event_type not in creation_events
        ):
            return
        node = self.nodes.setdefault(
            node_id, new_task_node(node_id, self.last_updated)
        )
        node["updated_at"] = str(
            event.get("recorded_at") or node.get("updated_at") or ""
        )
        if event_type == "agent.activity.changed":
            if payload.get("role"):
                node["role"] = payload["role"]
        else:
            self._merge_task_fields(node, payload)
        if event_type in {"node.started", "task.execution.started"}:
            node["status"] = TaskStatus.RUNNING.value
        elif event_type == "node.result_submitted":
            node["status"] = TaskStatus.RESULT_SUBMITTED.value
        elif event_type in {"node.completed", "task.execution.completed"}:
            node["status"] = TaskStatus.COMPLETED.value
        elif event_type in {"node.repair_pending", "task.repair_pending"}:
            node["status"] = TaskStatus.REPAIR_PENDING.value
        elif event_type in {
            "node.failed", "task.execution.error", "llm.error",
            "llm.stream.failed",
        }:
            node["status"] = TaskStatus.FAILED.value
        if event_type == "node.io.input":
            node["input"] = {
                "path": payload.get("path", ""),
                "goal": payload.get("goal", ""),
                "role": payload.get("role", ""),
            }
            node["agent_instance"] = str(
                payload.get("agent_id") or node.get("agent_instance") or ""
            )
            node["assigned_by"] = str(
                payload.get("assigned_by") or node.get("assigned_by")
                or "GlobalGovernor"
            )
            node["prompt_source"] = str(
                payload.get("prompt_source") or node.get("prompt_source")
                or "Governor Task Contract"
            )
        if event_type == "node.io.output":
            node["output_refs"] = {
                "json": payload.get("json_path", ""),
                "markdown": payload.get("markdown_path", ""),
            }
        if event_type == "task.structured_message":
            node["summary"] = payload.get("summary", node.get("summary", ""))
        if event_type in {"llm.text.delta", "node.output.delta"}:
            node["output"] = (
                str(node.get("output") or "")
                + str(payload.get("text", payload.get("delta", "")))
            )[-100_000:]
        if event_type in {
            "llm.tool_execution.start",
            "llm.tool_execution.done",
            "llm.tool_batch",
        }:
            self._merge_tool_activity(node, event_type, payload)
        if event_type == "agent.activity.changed":
            activity = payload.get("activity") or {}
            if isinstance(activity, dict):
                projected = {
                    **activity,
                    "status": str(payload.get("status") or "running"),
                    "agent_id": str(payload.get("agent_id") or ""),
                    "recorded_at": str(event.get("recorded_at") or ""),
                }
                node["current_activity"] = projected
                history = node.setdefault("activity_history", [])
                if (
                    history
                    and history[-1].get("source_event")
                    == projected.get("source_event")
                    and history[-1].get("type") == projected.get("type")
                ):
                    history.pop()
                history.append(projected)
                del history[:-100]
        elif event_type in {
            "llm.request", "llm.response", "llm.text.delta",
            "llm.tool_execution.start", "llm.tool_execution.done",
            "llm.tool_batch", "coder.validation_failed",
            "task.execution.completed", "node.completed",
        }:
            activity = self._activity_from_runtime_event(event_type, payload)
            if activity:
                projected = {
                    **activity,
                    "agent_id": self._latest_agent_for(node_id),
                    "recorded_at": str(event.get("recorded_at") or ""),
                    "source_event": event_type,
                }
                node["current_activity"] = projected
                history = node.setdefault("activity_history", [])
                if not history or (
                    history[-1].get("type"), history[-1].get("tool")
                ) != (projected.get("type"), projected.get("tool")):
                    history.append(projected)
                    del history[:-100]
        for dependency in payload.get("dependencies", []) or []:
            self.edges.add((str(dependency), node_id, "dependency"))

    @staticmethod
    def _activity_from_runtime_event(
        event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        mapping = {
            "llm.request": ("model_request", "正在请求模型生成下一步"),
            "llm.response": ("response_received", "模型响应已返回，正在整理结果"),
            "llm.text.delta": ("generating_output", "正在流式生成节点输出"),
            "coder.validation_failed": ("verification", "代码验证失败，正在准备修复"),
            "task.execution.completed": ("completed", "节点执行完成"),
            "node.completed": ("completed", "节点执行完成"),
        }
        if event_type in mapping:
            activity_type, label = mapping[event_type]
            return {"type": activity_type, "label": label, "tool": ""}
        if event_type.startswith("llm.tool_"):
            calls = payload.get("calls") or []
            first = calls[0] if calls and isinstance(calls[0], dict) else {}
            tool = str(first.get("name") or "tool")
            if event_type == "llm.tool_execution.start":
                return {
                    "type": "tool_call",
                    "label": f"正在调用工具 {tool}",
                    "tool": tool,
                }
            failed = any(
                isinstance(item, dict)
                and str(item.get("status")) in {"error", "failed"}
                for item in calls
            )
            return {
                "type": "tool_result",
                "label": f"工具 {tool} {'执行失败' if failed else '执行完成'}",
                "tool": tool,
            }
        return {}

    @staticmethod
    def _merge_tool_activity(
        node: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        activities = node.setdefault("tool_activity", [])
        calls = payload.get("calls") or []
        if not isinstance(calls, list):
            return
        tool_round = int(payload.get("tool_round") or 0)
        request_index = int(payload.get("request_index") or 0)
        for index, raw_call in enumerate(calls):
            call = raw_call if isinstance(raw_call, dict) else {"name": raw_call}
            name = str(call.get("name") or "tool")
            call_id = str(call.get("call_id") or "")
            key = call_id or f"{request_index}:{tool_round}:{index}:{name}"
            current = next(
                (item for item in activities if item.get("key") == key), None
            )
            if current is None:
                current = {"key": key}
                activities.append(current)
            previous_arguments = current.get("arguments") or {}
            current.update({
                "name": name,
                "status": (
                    str(call.get("status") or "completed")
                    if event_type in {"llm.tool_execution.done", "llm.tool_batch"}
                    else "running"
                ),
                "arguments": (
                    call.get("arguments")
                    or call.get("args")
                    or previous_arguments
                ),
                "reason": str(payload.get("reason") or ""),
                "tool_round": tool_round,
                "request_index": request_index,
                "path": str(call.get("path") or call.get("run_dir") or ""),
                "error": str(call.get("error") or ""),
                "stdout": str(call.get("stdout_tail") or ""),
            })
        del activities[:-100]

    def _apply_agent_event(
        self, event_type: str, payload: dict[str, Any], node_id: str
    ) -> None:
        if event_type == "agent.activity.changed":
            return
        if event_type == "governor.worker_selected":
            selected = payload.get("selected")
            if not isinstance(selected, dict):
                return
            task_id = str(payload.get("task_id") or node_id)
            task = self.nodes.setdefault(
                task_id,
                new_task_node(task_id, self.last_updated),
            )
            task["role"] = str(
                selected.get("worker_type")
                or selected.get("profile_id")
                or task.get("role")
                or "unknown"
            ).lower()
            task["resource_tier"] = str(
                selected.get("resource_tier") or "unknown"
            )
            task["selected_model"] = str(
                selected.get("selected_model") or ""
            )
            return
        if not event_type.startswith("agent."):
            return
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            return
        if node_id:
            task = self.nodes.setdefault(
                node_id, new_task_node(node_id, self.last_updated)
            )
            task["agent_instance"] = agent_id
        if node_id:
            for previous_id in self._node_agents.get(node_id, []):
                previous = self.agents.get(previous_id)
                if (
                    previous
                    and previous_id != agent_id
                    and previous.status in ACTIVE_AGENT_STATES
                ):
                    previous.status = TaskStatus.SUPERSEDED.value
                    previous.updated_at = self.last_updated
        agent = self.agents.get(agent_id) or AgentViewState(id=agent_id)
        agent.role = str(payload.get("role") or agent.role or "unknown").lower()
        agent.current_task = node_id or agent.current_task
        agent.status, reported_status = self._project_agent_status(
            payload.get("status") or payload.get("state"), agent.status
        )
        if reported_status != agent.status:
            agent.metadata["reported_lifecycle_status"] = reported_status
        agent.resource_tier = str(
            payload.get("resource_tier") or agent.resource_tier or "unknown"
        )
        agent.resource_id = str(payload.get("resource_id") or agent.resource_id)
        agent.model = str(payload.get("model") or agent.model)
        agent.capabilities = string_list(
            payload.get("capabilities")
        ) or agent.capabilities
        agent.tools = string_list(payload.get("tools")) or agent.tools
        agent.updated_at = self.last_updated
        agent.started_at = agent.started_at or self.last_updated
        agent.source = str(payload.get("source") or "trace")
        agent.metadata["last_reason"] = str(payload.get("reason") or "")
        self.agents[agent_id] = agent
        self._remember_agent(agent.current_task, agent_id)

    def _apply_recovery_event(
        self, event_type: str, payload: dict[str, Any], node_id: str
    ) -> None:
        if not node_id or node_id not in self.nodes:
            return
        node = self.nodes[node_id]
        context = node.setdefault("recovery_context", {})
        if event_type in {"node.failed", "task.execution.error"}:
            context.update({
                "failed_node": node_id,
                "failure_stage": str(payload.get("stage") or "execution"),
                "failure_type": str(
                    payload.get("error_type") or payload.get("type") or "execution_error"
                ),
                "failure_reason": first_text(payload, "error", "reason", "summary"),
            })
        elif event_type == "verifier.verified":
            context.update({
                "triggered_by": "Verifier",
                "verdict": str(payload.get("verdict") or ""),
                "verification_score": payload.get("overall_score"),
                "failed_dimensions": string_list(payload.get("failed_dimensions")),
            })
        elif "recovery" in event_type and event_type.startswith("governor."):
            context.update({
                "triggered_by": "GlobalGovernor",
                "recovery_action": event_type,
                "reason_codes": string_list(payload.get("reason_codes")),
            })

    def _apply_communication_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        if event_type == "agent.communication.link_created":
            source = str(payload.get("source") or payload.get("source_agent") or "")
            target = str(payload.get("target") or payload.get("target_agent") or "")
            if source and target:
                self.communication_edges.add(CommunicationLink(
                    source=source,
                    target=target,
                    kind=str(payload.get("kind") or "structured_message"),
                    reason=str(payload.get("reason") or ""),
                    source_task=str(payload.get("source_task") or ""),
                    target_task=str(payload.get("target_task") or ""),
                ))
            return
        if event_type == "task.structured_message":
            source_task = str(payload.get("node_id") or "")
            source = self._latest_agent_for(source_task)
            if not source:
                return
            governor_id = "control:governor"
            if governor_id not in self.agents:
                self.agents[governor_id] = AgentViewState(
                    id=governor_id,
                    role="governor",
                    capabilities=[
                        "global_planning", "graph_governance",
                        "result_reconciliation",
                    ],
                    status=TaskStatus.RUNNING.value,
                    current_task="global",
                    resource_tier="control_plane",
                    updated_at=self.last_updated,
                    source="control_plane",
                )
            self.communication_edges.add(CommunicationLink(
                source=source,
                target=governor_id,
                kind="result_submission",
                reason="submit validated structured result to Governor",
                source_task=source_task,
                target_task="global",
            ))
            for edge_source, target_task, edge_kind in self.edges:
                if edge_source != source_task:
                    continue
                target = self._latest_agent_for(target_task)
                if not target:
                    continue
                self.communication_edges.add(CommunicationLink(
                    source=source,
                    target=target,
                    kind="structured_message",
                    reason=(
                        "structured result routed over "
                        f"{edge_kind} task dependency"
                    ),
                    source_task=source_task,
                    target_task=target_task,
                    inferred=True,
                ))
            return
        if event_type != "task.evidence_chain.created":
            return
        source_task = str(payload.get("source_node") or "")
        target_task = str(payload.get("target_node") or "")
        source = self._latest_agent_for(source_task)
        target = self._latest_agent_for(target_task)
        if source and target:
            self.communication_edges.add(CommunicationLink(
                source=source,
                target=target,
                kind="evidence_route",
                reason="share structured evidence",
                source_task=source_task,
                target_task=target_task,
            ))

    def _apply_resource_event(
        self, event_type: str, payload: dict[str, Any], node_id: str
    ) -> None:
        if event_type == "governor.worker_selected":
            selected = payload.get("selected")
            if isinstance(selected, dict):
                self._register_resource(
                    tier=str(selected.get("resource_tier") or "unknown"),
                    resource_id=str(selected.get("resource_id") or ""),
                    model=str(selected.get("selected_model") or ""),
                    task_id=str(payload.get("task_id") or node_id),
                    agent_id="",
                )
        if event_type == "task.execution.started":
            self._register_resource(
                tier=str(payload.get("resource_tier") or "unknown"),
                resource_id=str(payload.get("resource_id") or ""),
                model=str(payload.get("selected_model") or ""),
                task_id=node_id,
                agent_id=self._latest_agent_for(node_id),
            )
        if event_type.startswith("resource."):
            self._register_resource(
                tier=str(payload.get("tier") or payload.get("resource_tier") or "unknown"),
                resource_id=str(payload.get("resource_id") or ""),
                model=str(payload.get("model") or ""),
                task_id=node_id,
                agent_id=str(payload.get("agent_id") or ""),
            )

    def _register_resource(
        self,
        *,
        tier: str,
        resource_id: str,
        model: str,
        task_id: str,
        agent_id: str,
    ) -> None:
        tier = tier if tier not in {"", "None"} else "unknown"
        key = resource_id or f"{tier}:{model or 'default'}"
        item = self.resources.setdefault(key, {
            "id": key,
            "tier": tier,
            "model": model,
            "status": "active",
            "tasks": [],
            "agents": [],
            "updated_at": self.last_updated,
        })
        item["tier"] = tier
        item["model"] = model or item.get("model", "")
        item["updated_at"] = self.last_updated
        if task_id and task_id not in item["tasks"]:
            item["tasks"].append(task_id)
        if agent_id and agent_id not in item["agents"]:
            item["agents"].append(agent_id)

    def _record_activity(
        self, event_type: str, payload: dict[str, Any], node_id: str
    ) -> None:
        interesting = {
            "node.started", "node.completed", "node.failed",
            "llm.request", "llm.tool_execution.start", "llm.tool_batch",
            "governor.graph_patch.applied", "task.structured_message",
            "agent.spawned", "agent.state_changed", "agent.failed",
            "agent.communication.link_created", "router.ready_wave",
        }
        if event_type not in interesting:
            return
        detail = ""
        if event_type == "llm.tool_execution.start":
            calls = payload.get("calls") or []
            detail = ", ".join(
                str(item.get("name") or "tool")
                if isinstance(item, dict) else str(item)
                for item in calls
            )
        else:
            detail = first_text(
                payload, "summary", "goal", "reason", "reasoning", "text"
            )
        self.activity.append({
            "event_type": event_type,
            "node_id": node_id,
            "agent_id": str(payload.get("agent_id") or ""),
            "role": str(payload.get("role") or ""),
            "detail": detail[:2000],
            "recorded_at": self.last_updated,
        })

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tasks = [dict(item) for item in self.nodes.values()]
            for task in tasks:
                metadata = task.get("metadata") or {}
                role = str(task.get("role") or "").lower()
                task["plane"] = (
                    task.get("plane")
                    or metadata.get("plane")
                    or ("control" if role in {"user_intent", "planner", "governor"} else "execution")
                )
            agents = [item.to_dict() for item in self.agents.values()]
            task_edges = [
                {"source": a, "target": b, "kind": k}
                for a, b, k in sorted(self.edges)
            ]
            task_role_map = {
                str(item.get("id")): str(item.get("role") or "").lower()
                for item in tasks
            }
            for edge in task_edges:
                source_role = task_role_map.get(str(edge.get("source")), "")
                target_role = task_role_map.get(str(edge.get("target")), "")
                if source_role == "user_intent" and target_role == "planner":
                    edge["kind"] = "task_admission"
                elif source_role in {"planner", "governor"} and target_role not in {
                    "user_intent", "planner", "governor",
                }:
                    edge["kind"] = "governed_admission"
            display_tasks = tasks
            display_edges = task_edges
            if self.original_request:
                input_node = next(
                    (
                        item for item in tasks
                        if str(item.get("role") or "").lower() == "user_intent"
                        or str((item.get("metadata") or {}).get("type")) == "user_intent"
                    ),
                    None,
                )
                has_real_input_node = input_node is not None
                if input_node is None:
                    input_node = {
                    "id": "U-001",
                    "role": "user_intent",
                    "goal": self.original_request,
                    "status": TaskStatus.COMPLETED.value,
                    "summary": "已接收原始任务，交由 GlobalGovernor 规划。",
                    "output": "原始需求已冻结为任务契约。",
                    "updated_at": self.last_updated,
                    "is_input": True,
                    "plane": "control",
                    }
                else:
                    input_node["goal"] = self.original_request
                    input_node["summary"] = "已接收原始任务，交由 GlobalGovernor 规划。"
                    input_node["output"] = "原始需求已冻结为任务契约。"
                    input_node["is_input"] = True
                    input_node["plane"] = "control"
                planner = next(
                    (
                        item for item in tasks
                        if str(item.get("id")) == self.root_node_id
                    ),
                    next(
                        (
                            item for item in tasks
                            if str(item.get("role") or "").lower() == "planner"
                        ),
                        None,
                    ),
                )
                if planner is not None:
                    planner_id = str(planner.get("id") or self.root_node_id)
                    planner["goal"] = self.original_request
                    planner["assigned_by"] = "用户 / GlobalGovernor"
                    planner["agent_instance"] = (
                        planner.get("agent_instance")
                        or "GlobalGovernor · Planning Control"
                    )
                    planner["prompt_source"] = (
                        "User Intent + Governor Planning Contract"
                    )
                    planner["plane"] = "control"
                    planner["input_sources"] = [{
                        "node_id": "U-001",
                        "role": "user_intent",
                        "relation": "task_admission",
                        "summary": "用户提交的完整原始任务",
                    }]
                    planner["planning_view"] = self._planning_view(
                        tasks, task_edges
                    )
                    if not planner.get("current_activity"):
                        planner["current_activity"] = {
                            "type": "planning_completed",
                            "label": "规划补丁已生成并应用到任务图",
                            "status": "completed",
                            "agent_id": planner["agent_instance"],
                            "recorded_at": planner.get("updated_at") or "",
                            "source_event": "governor.graph_patch.applied",
                        }
                display_tasks = tasks if has_real_input_node else [input_node, *tasks]
                has_admission_edge = any(
                    str(edge.get("source")) == str(input_node.get("id"))
                    and str(edge.get("target")) == self.root_node_id
                    for edge in task_edges
                )
                if (
                    planner is not None
                    and not has_admission_edge
                ):
                    display_edges = [
                        {
                            "source": str(input_node.get("id") or "U-001"),
                            "target": planner_id if planner is not None else self.root_node_id,
                            "kind": "task_admission",
                        },
                        *task_edges,
                    ]
            self._project_input_provenance(display_tasks, display_edges)
            display_task_map = {
                str(item.get("id")): item for item in display_tasks
            }
            planner_id = next(
                (
                    str(item.get("id")) for item in display_tasks
                    if str(item.get("role") or "").lower() == "planner"
                ),
                self.root_node_id,
            )
            intent_id = next(
                (
                    str(item.get("id")) for item in display_tasks
                    if str(item.get("role") or "").lower() == "user_intent"
                ),
                "U-001",
            )
            execution_ids = {
                node_id for node_id, item in display_task_map.items()
                if str(item.get("plane") or "execution") != "control"
            }
            planning_view = (
                display_task_map.get(planner_id, {}).get("planning_view") or {}
            )
            planning_explanation = str(
                planning_view.get("planning_explanation")
                or "Planner 根据用户任务、交付契约和能力边界生成节点契约。"
            )
            for node_id in execution_ids:
                item = display_task_map[node_id]
                metadata = item.get("metadata") or {}
                source_requirements = [
                    str(value) for value in metadata.get("source_requirements", [])
                    if str(value).strip()
                ]
                derivation = str(metadata.get("derivation") or "").strip()
                if not derivation:
                    derivation = (
                        f"Planner 将原始任务按 {item.get('role') or 'dynamic'} 能力边界"
                        f"拆分为当前节点：{item.get('goal') or ''}"
                    )
                item["requirement_lineage"] = {
                    "user_intent_node": intent_id,
                    "planner_node": planner_id,
                    "source_requirements": source_requirements or [
                        self.original_request or "用户原始任务"
                    ],
                    "planning_explanation": planning_explanation,
                    "derivation": derivation,
                    "derived_requirement": item.get("goal") or "",
                }
                runtime_agent = next(
                    (
                        agent for agent in agents
                        if str(agent.get("current_task") or "") == node_id
                    ),
                    None,
                )
                executed_by = str(
                    item.get("agent_instance")
                    or (runtime_agent or {}).get("id")
                    or "等待 Router 分配 Worker"
                )
                item["governance_chain"] = {
                    "requested_by": str(
                        metadata.get("requested_by")
                        or f"control-planner-{planner_id}"
                    ),
                    "submitted_by": str(
                        metadata.get("submitted_by")
                        or "Orchestrator.plan_task_decomposition"
                    ),
                    "approved_by": str(
                        metadata.get("approved_by") or "GlobalGovernor"
                    ),
                    "applied_by": str(
                        metadata.get("applied_by")
                        or "GlobalGovernor.apply_task_graph_patch"
                    ),
                    "scheduled_by": str(
                        metadata.get("scheduled_by") or "Orchestrator/Router"
                    ),
                    "executed_by": executed_by,
                }
            governor_node = {
                "id": "G-001",
                "role": "global_governor",
                "status": (
                    TaskStatus.COMPLETED.value
                    if self.run_status in TERMINAL_TASK_STATES
                    else TaskStatus.RUNNING.value
                ),
                "goal": "审批 Planner 的 TaskGraphPatch 并原子写入任务图",
                "summary": "唯一任务图变更审批与提交边界",
                "plane": "control",
                "entity_type": "control_authority",
            }
            orchestrator_node = {
                "id": "O-001",
                "role": "orchestrator",
                "status": (
                    TaskStatus.COMPLETED.value
                    if self.run_status in TERMINAL_TASK_STATES
                    else TaskStatus.RUNNING.value
                ),
                "goal": "提交 Planner 的 TaskGraphPatch，并驱动后续调度生命周期",
                "summary": "唯一执行生命周期与控制面调用边界",
                "plane": "control",
                "entity_type": "control_executor",
            }
            contract_nodes = []
            contract_by_task: dict[str, str] = {}
            for node_id in sorted(execution_ids):
                task = display_task_map[node_id]
                metadata = task.get("metadata") or {}
                lineage = task.get("requirement_lineage") or {}
                contract_id = f"TC-{node_id}"
                contract_by_task[node_id] = contract_id
                contract_nodes.append({
                    "id": contract_id,
                    "role": "task_contract",
                    "status": TaskStatus.COMPLETED.value,
                    "goal": task.get("goal") or "",
                    "summary": f"GlobalGovernor 已审批 {node_id} 的执行契约",
                    "plane": "contract",
                    "entity_type": "task_contract",
                    "target_task": node_id,
                    "source_requirements": list(
                        lineage.get("source_requirements") or []
                    ),
                    "derivation": str(lineage.get("derivation") or ""),
                    "required_capabilities": list(
                        metadata.get("required_capabilities")
                        or metadata.get("capabilities")
                        or []
                    ),
                    "constraints": list(task.get("constraints") or []),
                    "acceptance_criteria": list(
                        task.get("acceptance_criteria")
                        or task.get("acceptance")
                        or []
                    ),
                    "task_version": task.get("task_version") or 1,
                    "approved_by": "GlobalGovernor",
                    "applied_by": "GlobalGovernor.apply_task_graph_patch",
                })
                task.setdefault("input_sources", []).append({
                    "node_id": contract_id,
                    "role": "task_contract",
                    "relation": "task_contract",
                    "summary": "经 GlobalGovernor 审批的节点目标、约束与验收契约",
                })
            authorization_nodes = [
                *display_tasks,
                orchestrator_node,
                governor_node,
                *contract_nodes,
            ]
            authorization_edges = []
            if intent_id in display_task_map and planner_id in display_task_map:
                authorization_edges.append({
                    "source": intent_id,
                    "target": planner_id,
                    "kind": "planning_requested",
                    "reason": "用户请求 Planner 拆解原始任务",
                })
            if planner_id in display_task_map:
                authorization_edges.append({
                    "source": planner_id,
                    "target": orchestrator_node["id"],
                    "kind": "patch_proposed",
                    "reason": "Planner 请求新增或调整任务节点",
                })
                authorization_edges.append({
                    "source": orchestrator_node["id"],
                    "target": governor_node["id"],
                    "kind": "patch_submitted",
                    "reason": "Orchestrator 调用 Governor 提交 TaskGraphPatch",
                })
                for node_id in sorted(execution_ids):
                    contract_id = contract_by_task[node_id]
                    authorization_edges.extend((
                        {
                            "source": governor_node["id"],
                            "target": contract_id,
                            "kind": "contract_approved",
                            "reason": "GlobalGovernor 审批并原子写入节点契约",
                        },
                        {
                            "source": contract_id,
                            "target": node_id,
                            "kind": "contract_issued",
                            "reason": "任务契约向执行节点签发目标、约束和验收标准",
                        },
                    ))
            for agent in agents:
                task_id = str(agent.get("current_task") or "")
                if task_id not in execution_ids:
                    continue
                agent_view_id = f"agent::{agent.get('id')}"
                authorization_nodes.append({
                    "id": agent_view_id,
                    "role": agent.get("role") or "worker",
                    "status": agent.get("status") or TaskStatus.ADMITTED.value,
                    "goal": f"执行任务节点 {task_id}",
                    "summary": str(agent.get("id") or ""),
                    "plane": "execution",
                    "entity_type": "runtime_agent",
                    "agent_instance": agent.get("id") or "",
                    "current_task": task_id,
                    "selected_model": agent.get("model") or "",
                    "resource_tier": agent.get("resource_tier") or "",
                })
                authorization_edges.append({
                    "source": task_id,
                    "target": agent_view_id,
                    "kind": "executed_by",
                    "reason": "Orchestrator/Router 将节点派发给该 Worker 实例",
                })
            data_flow_edges = []
            if intent_id in display_task_map and planner_id in display_task_map:
                data_flow_edges.append({
                    "source": intent_id,
                    "target": planner_id,
                    "kind": "requirement_intake",
                    "reason": "用户原始任务进入 Planner",
                })
            if planner_id in display_task_map:
                for node_id in sorted(execution_ids):
                    contract_id = contract_by_task[node_id]
                    data_flow_edges.extend((
                        {
                            "source": planner_id,
                            "target": contract_id,
                            "kind": "requirement_derivation",
                            "reason": display_task_map[node_id]["requirement_lineage"]["derivation"],
                        },
                        {
                            "source": contract_id,
                            "target": node_id,
                            "kind": "task_contract",
                            "reason": "审批后的任务契约成为 Worker 的直接输入",
                        },
                    ))
            data_flow_edges.extend(
                dict(edge) for edge in display_edges
                if str(edge.get("source")) in execution_ids
                and str(edge.get("target")) in execution_ids
                and str(edge.get("kind")) not in {
                    "task_admission", "governed_admission",
                }
            )
            communication = [
                item.to_dict()
                for item in sorted(
                    self.communication_edges,
                    key=lambda link: (link.source, link.target, link.kind),
                )
            ]
            status_counts: dict[str, int] = {}
            for node in tasks:
                status = str(node.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            effective_status = self.run_status
            if tasks and all(
                str(node.get("status") or "") in TERMINAL_TASK_STATES
                for node in tasks
            ):
                effective_status = (
                    "failed" if status_counts.get("failed", 0) else "completed"
                )
            if effective_status in TERMINAL_TASK_STATES:
                for agent in agents:
                    if (
                        str(agent.get("status") or "") in ACTIVE_AGENT_STATES
                        or agent.get("source") == "control_plane"
                    ):
                        agent["status"] = (
                            TaskStatus.FAILED.value
                            if effective_status == TaskStatus.FAILED.value
                            else TaskStatus.COMPLETED.value
                        )
            active_nodes = [
                {
                    "id": node.get("id"),
                    "role": node.get("role"),
                    "goal": node.get("goal"),
                    "status": node.get("status"),
                    "output": str(node.get("output") or "")[-4000:],
                    "summary": node.get("summary"),
                }
                for node in tasks
                if str(node.get("status") or "") in ACTIVE_TASK_STATES
            ]
            active_agents = [
                item for item in agents
                if str(item.get("status")) in ACTIVE_AGENT_STATES
            ]
            metrics = self.metrics.snapshot(tasks, agents, len(communication))
            wave = {
                "id": self.current_wave_id,
                "node_ids": list(self.current_wave),
                "roles": [
                    (
                        self.current_wave_roles[index]
                        if index < len(self.current_wave_roles)
                        and self.current_wave_roles[index] != "governed_worker"
                        else str(self.nodes.get(node_id, {}).get("role") or "dynamic")
                    )
                    for index, node_id in enumerate(self.current_wave)
                ],
                "width": len(self.current_wave),
                "status": (
                    effective_status
                    if effective_status in TERMINAL_TASK_STATES
                    else TaskStatus.RUNNING.value
                    if active_nodes
                    else TaskStatus.WAITING_DEPENDENCIES.value
                ),
            }
            completed_count = status_counts.get(TaskStatus.COMPLETED.value, 0)
            failed_count = status_counts.get(TaskStatus.FAILED.value, 0)
            public_events = list(self.runtime_events)
            latest_runtime = next(
                (
                    item for item in reversed(public_events)
                    if str(item.get("summary") or "").strip().lower()
                    not in {"", "created", "initializing"}
                ),
                public_events[-1] if public_events else {},
            )
            terminal = effective_status in TERMINAL_TASK_STATES
            terminal_failed = effective_status == TaskStatus.FAILED.value
            runtime_brief = {
                "phase": (
                    "验收失败" if terminal_failed else "验收完成"
                    if terminal else str(latest_runtime.get("title") or effective_status)
                ),
                "summary": str(
                    ("任务已经结束，所有运行中状态已关闭。" if terminal else "")
                    or latest_runtime.get("summary")
                    or "控制面正在等待下一项可观测运行事件。"
                ),
                "current_work": [
                    {
                        "id": item.get("id"),
                        "role": item.get("role"),
                        "action": (
                            (item.get("current_activity") or {}).get("label")
                            or item.get("goal")
                            or "执行节点契约"
                        ),
                    }
                    for item in ([] if terminal else active_nodes[:8])
                ],
                "next_step": (
                    "验收已结束；可查看交付目录与节点阶段报告。"
                    if terminal
                    else "等待运行中 Worker 提交结构化结果并进入 Verifier。"
                    if active_nodes
                    else "由 Governor 检查交付完整性并决定完成或最小补图。"
                ),
                "total_nodes": len(tasks),
                "successful_nodes": completed_count,
                "failed_nodes": failed_count,
                "recovery_count": int(metrics.get("recovery_count") or 0),
                "output_dir": str(self.run_dir),
            }
            return {
                "run_dir": str(self.run_dir),
                "run_status": effective_status,
                "last_updated": self.last_updated,
                "status_counts": status_counts,
                "active_nodes": active_nodes,
                "active_agents": active_agents,
                "current_wave": list(self.current_wave),
                "wave": wave,
                "activity": list(self.activity)[-100:],
                "runtime_events": list(self.runtime_events)[-200:],
                "runtime_brief": runtime_brief,
                # Flat projections for the live client.
                "nodes": display_tasks,
                "edges": display_edges,
                # Explicit control-plane projections.
                "task_graph": {"nodes": display_tasks, "edges": display_edges},
                "task_authorization_graph": {
                    "nodes": authorization_nodes,
                    "edges": authorization_edges,
                },
                "data_flow_graph": {
                    "nodes": [*display_tasks, *contract_nodes],
                    "edges": data_flow_edges,
                },
                "agent_population": {
                    "agents": agents,
                    "active_count": len(active_agents),
                    "by_role": self._count_by(agents, "role"),
                    "by_status": self._count_by(agents, "status"),
                },
                "communication_topology": {
                    "edges": communication,
                    "sparsity": metrics.get("topology_sparsity"),
                    "message_count": metrics.get("communication_messages", 0),
                },
                "resource_state": {
                    "resources": list(self.resources.values()),
                    "by_tier": self._count_by(
                        list(self.resources.values()), "tier"
                    ),
                },
                "metrics": metrics,
                "events": list(self.events)[-300:],
                "execution_timeline": self._execution_timeline(),
                "artifact_flow": self._artifact_flow(display_tasks),
            }

    def _execution_timeline(self) -> list[dict[str, Any]]:
        visible = {
            "task.source.loaded": "接收用户意图",
            "orchestrator.initialized": "初始化运行",
            "governor.graph_patch.proposed": "Governor 生成规划",
            "governor.graph_patch.applied": "任务图变更生效",
            "agent.spawned": "创建 Agent 实例",
            "router.ready_wave": "形成 Ready-Wave",
            "node.started": "节点开始执行",
            "agent.activity.changed": "Agent 活动变化",
            "node.failed": "节点执行失败",
            "verifier.verified": "Verifier 完成质量判断",
            "task.execution.completed": "节点执行完成",
            "orchestrator.completed": "运行完成",
        }
        timeline = []
        for event in self.events:
            event_type = event_type_of(event)
            if event_type not in visible:
                continue
            payload = event_payload(event)
            activity = payload.get("activity") or {}
            timeline.append({
                "id": f"timeline-{len(timeline) + 1}",
                "event_type": event_type,
                "label": (
                    activity.get("label")
                    if isinstance(activity, dict) and activity.get("label")
                    else visible[event_type]
                ),
                "node_id": node_id_of(payload),
                "agent_id": str(payload.get("agent_id") or ""),
                "recorded_at": str(event.get("recorded_at") or ""),
            })
        return timeline[-300:]

    @staticmethod
    def _artifact_flow(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flow = []
        for task in tasks:
            node_id = str(task.get("id") or "")
            paths = list(task.get("artifacts") or [])
            refs = task.get("output_refs") or {}
            paths.extend(value for value in refs.values() if value)
            for tool in task.get("tool_activity") or []:
                if tool.get("path"):
                    paths.append(tool["path"])
            for path in dict.fromkeys(str(item) for item in paths if item):
                flow.append({"node_id": node_id, "path": path})
        return flow

    def _planning_view(
        self,
        tasks: list[dict[str, Any]],
        task_edges: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task_map = {str(item.get("id")): item for item in tasks}
        added = self.planning_decision.get("added") or [
            node_id for node_id, item in task_map.items()
            if node_id != self.root_node_id
            and str(item.get("role") or "").lower() != "user_intent"
        ]
        planned_nodes = []
        for node_id in added:
            item = task_map.get(str(node_id))
            if not item:
                continue
            dependencies = [
                str(edge.get("source"))
                for edge in task_edges
                if str(edge.get("target")) == str(node_id)
                and str(edge.get("source")) != self.root_node_id
            ]
            planned_nodes.append({
                "node_id": node_id,
                "role": item.get("role") or "dynamic",
                "goal": item.get("goal") or "",
                "dependencies": dependencies,
                "source_requirements": list(
                    (item.get("metadata") or {}).get("source_requirements", [])
                ),
                "derivation": str(
                    (item.get("metadata") or {}).get("derivation") or ""
                ),
            })
        roots = [
            item for item in planned_nodes if not item.get("dependencies")
        ]
        strategy = (
            f"首个 Ready-Wave 包含 {len(roots)} 个可并行节点；"
            "存在依赖的节点在上游证据通过验证后由 Router 唤醒。"
        )
        return {
            "summary": f"规划形成 {len(planned_nodes)} 个执行节点。",
            "execution_strategy": strategy,
            "planning_explanation": self.planning_decision.get(
                "planning_explanation", "依据任务依赖、能力分离和并行价值生成任务图。"
            ),
            "reason_codes": self.planning_decision.get("reason_codes", []),
            "planned_nodes": planned_nodes,
            "planning_history": [dict(item) for item in self.planning_history],
        }

    def _project_input_provenance(
        self,
        tasks: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> None:
        task_map = {str(item.get("id")): item for item in tasks}
        for task in tasks:
            node_id = str(task.get("id") or "")
            if task.get("is_input"):
                task["assigned_by"] = "用户"
                task["input_sources"] = []
                continue
            sources = list(task.get("input_sources") or [])
            known = {str(item.get("node_id")) for item in sources}
            for edge in edges:
                if str(edge.get("target")) != node_id:
                    continue
                source_id = str(edge.get("source") or "")
                if not source_id or source_id in known:
                    continue
                source = task_map.get(source_id, {})
                relation = str(edge.get("kind") or "dependency")
                if relation == "governed_admission":
                    summary = "GlobalGovernor 通过 Planner 任务图补丁准入该节点"
                elif relation == "task_admission":
                    summary = "用户提交的完整原始任务"
                else:
                    summary = source.get("summary") or source.get("goal") or ""
                sources.append({
                    "node_id": source_id,
                    "role": source.get("role") or "unknown",
                    "relation": relation,
                    "summary": summary,
                })
                known.add(source_id)
            task["input_sources"] = sources
            task["assigned_by"] = task.get("assigned_by") or "GlobalGovernor"

    def _merge_task_fields(
        self, node: dict[str, Any], source: dict[str, Any]
    ) -> None:
        for key in (
            "status", "role", "goal", "summary", "artifacts", "input",
            "resource_tier", "resource_id", "selected_model", "metadata",
            "parent_node_id", "trigger", "plane", "public_reasoning",
            "stage_reports",
        ):
            if source.get(key) not in (None, ""):
                if key != "status":
                    node[key] = source[key]
                    continue
                try:
                    node[key] = normalize_status(source[key])
                except ValueError:
                    # Historical traces may contain an execution outcome such
                    # as ``success`` or ``failure`` in a field named status.
                    # The dashboard is a read-only projection and must not
                    # relax the canonical task state protocol or abort replay
                    # because of one malformed legacy event. Keep the last
                    # canonical state and expose the raw value for diagnosis.
                    node["reported_noncanonical_status"] = str(source[key])

    def _remember_agent(self, node_id: str, agent_id: str) -> None:
        if not node_id:
            return
        values = self._node_agents.setdefault(node_id, [])
        if agent_id not in values:
            values.append(agent_id)

    def _latest_agent_for(self, node_id: str) -> str:
        values = self._node_agents.get(node_id, [])
        return values[-1] if values else ""

    @staticmethod
    def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        return counts

    @staticmethod
    def _edge_from_payload(
        payload: dict[str, Any], default_kind: str = "dependency"
    ) -> tuple[str, str, str] | None:
        source = payload.get("source") or payload.get("from") or payload.get("upstream")
        target = payload.get("target") or payload.get("to") or payload.get("downstream")
        if not source or not target:
            return None
        return (
            str(source),
            str(target),
            str(payload.get("kind") or payload.get("edge_type") or default_kind),
        )

    @classmethod
    def _parse_edges(
        cls, items: list[Any], default_kind: str = "dependency"
    ) -> set[tuple[str, str, str]]:
        result: set[tuple[str, str, str]] = set()
        for item in items:
            if isinstance(item, dict):
                edge = cls._edge_from_payload(item, default_kind)
                if edge:
                    result.add(edge)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                kind = item[2] if len(item) > 2 else default_kind
                result.add((str(item[0]), str(item[1]), str(kind)))
        return result
