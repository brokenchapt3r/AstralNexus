from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json

from AstralNexus.core.artifacts import append_jsonl, write_json
from AstralNexus.core.identity import CODE_NAME, PRODUCT_NAME
from AstralNexus.core.output_paths import RunOutputLayout
from AstralNexus.core.task_graph import Artifact, Metric
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
from AstralNexus.runtime.event_bus import PUBLIC_RUNTIME_EVENT_TYPES, RuntimeEventBus


class TraceRecorder:
    def __init__(
        self,
        run_dir: Path,
        *,
        graph_store: SQLiteTaskGraphStore | None = None,
        default_node_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        event_bus: RuntimeEventBus | None = None,
    ):
        self.run_dir = run_dir
        self.trace_path = run_dir / "trace.jsonl"
        self.layout = RunOutputLayout(run_dir)
        self.layout.ensure_runtime_dirs()
        self.node_io_dir = self.layout.node_io_dir
        self.events: list[dict[str, Any]] = []
        self.graph_store = graph_store
        self.default_node_id = default_node_id
        self.event_callback = event_callback
        self.event_bus = event_bus or RuntimeEventBus(run_dir)
        self.event_bus.subscribe(self._record_public_event)
        self._execution_counter = 0
        self._active_executions: dict[str, tuple[str, int, int, str]] = {}

    def record(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event_type = str(getattr(event_type, "value", event_type))
        normalized_payload = dict(payload or {})
        metadata = normalized_payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        normalized_payload.setdefault("event_version", 1)
        normalized_payload.setdefault("task_version", normalized_payload.get("task_version") or metadata.get("task_version") or 1)
        node_id = normalized_payload.get("node_id") or normalized_payload.get("task_id")
        if not node_id:
            node_id = self.default_node_id
        if node_id:
            normalized_payload["node_id"] = str(node_id)
        normalized_payload.setdefault("reason_codes", [])
        normalized_payload.setdefault("artifact_refs", [])
        event = {
            "product": PRODUCT_NAME,
            "code_name": CODE_NAME,
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "payload": normalized_payload,
        }
        self.events.append(event)
        append_jsonl(self.trace_path, event)
        if self.graph_store is not None:
            node_id = normalized_payload.get("node_id")
            if node_id:
                self.graph_store.add_message(
                    str(node_id), event_type, _preview(normalized_payload)
                )
        if (
            self.event_callback is not None
            and event_type not in PUBLIC_RUNTIME_EVENT_TYPES
        ):
            try:
                display_event = dict(normalized_payload)
                display_event["event_type"] = event_type
                display_event["recorded_at"] = event["recorded_at"]
                display_event["run_dir"] = str(self.run_dir)
                self.event_callback(display_event)
            except Exception:
                pass
        self.event_bus.ingest_trace(
            event_type,
            normalized_payload,
            recorded_at=event["recorded_at"],
        )
        return event

    def _record_public_event(self, public_event: dict[str, Any]) -> None:
        """Mirror derived public events into the canonical trace without recursion."""
        event_type = str(public_event.get("event_type") or "")
        payload = dict(public_event.get("payload") or {})
        source_event = str(payload.get("source_event") or "")
        if event_type == source_event:
            self._emit_display_event(event_type, payload, public_event)
            return
        event = {
            "product": PRODUCT_NAME,
            "code_name": CODE_NAME,
            "event_type": event_type,
            "recorded_at": str(public_event.get("recorded_at") or datetime.now(timezone.utc).isoformat()),
            "payload": payload,
        }
        self.events.append(event)
        append_jsonl(self.trace_path, event)
        self._emit_display_event(event_type, payload, event)

    def _emit_display_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        if self.event_callback is None:
            return
        try:
            display_event = dict(payload)
            display_event.update(
                {
                    "event_type": event_type,
                    "recorded_at": event.get("recorded_at"),
                    "run_dir": str(self.run_dir),
                    "runtime_normalized": True,
                }
            )
            self.event_callback(display_event)
        except Exception:
            pass

    def record_node_input(self, node_id: str, payload: dict[str, Any]) -> Path:
        """Persist full node input for real-time observability."""
        task_version = int(payload.get("task_version") or payload.get("metadata", {}).get("task_version") or 1)
        attempt = int(payload.get("attempt") or payload.get("retry_count", 0) or 0) + 1
        self._execution_counter += 1
        execution_id = f"exec-{self._execution_counter:06d}"
        stem = self.layout.execution_stem(execution_id, node_id, task_version, attempt)
        self._active_executions[node_id] = (execution_id, task_version, attempt, stem)
        persisted = dict(payload)
        persisted.update({"execution_id": execution_id, "task_version": task_version, "attempt": attempt})
        path = self.layout.node_input(stem)
        _write_json_any(path, _with_recorded_at(persisted))
        self.record(
            "node.started",
            {"node_id": node_id, "execution_id": execution_id,
             "task_version": task_version, "attempt": attempt,
             "role": persisted.get("role"), "goal": str(persisted.get("goal", ""))[:240]},
        )
        self.record(
            "node.io.input",
            {
                "node_id": node_id,
                "execution_id": execution_id,
                "task_version": task_version,
                "attempt": attempt,
                "path": str(path),
                "goal": str(persisted.get("goal", ""))[:240],
                "role": persisted.get("role"),
                "agent_id": persisted.get("agent_id"),
                "assigned_by": persisted.get("assigned_by", "GlobalGovernor"),
                "prompt_source": persisted.get(
                    "prompt_source", "Governor Task Contract"
                ),
                "upstream_node_ids": list(
                    persisted.get("dependencies") or []
                ),
            },
        )
        return path

    def record_agent_activity(
        self,
        node_id: str,
        *,
        activity_type: str,
        label: str,
        agent_id: str = "",
        role: str = "",
        tool: str = "",
        input_preview: str = "",
        status: str = "running",
        source_event: str = "",
    ) -> dict[str, Any]:
        """Record the canonical, user-observable activity of an Agent."""
        return self.record(
            "agent.activity.changed",
            {
                "node_id": node_id,
                "agent_id": agent_id,
                "role": role,
                "status": status,
                "activity": {
                    "type": activity_type,
                    "label": label,
                    "tool": tool,
                    "input_preview": input_preview[:1200],
                    "source_event": source_event,
                },
            },
        )

    def record_node_output(self, node_id: str, payload: dict[str, Any]) -> tuple[Path, Path]:
        """Persist full node output as machine-readable JSON and readable Markdown."""
        active = self._active_executions.get(node_id)
        if active is None:
            task_version = int(payload.get("task_version") or 1)
            attempt = int(payload.get("attempt") or 1)
            self._execution_counter += 1
            execution_id = f"exec-{self._execution_counter:06d}"
            stem = self.layout.execution_stem(execution_id, node_id, task_version, attempt)
        else:
            execution_id, task_version, attempt, stem = active
        persisted = dict(payload)
        persisted.update({"execution_id": execution_id, "task_version": task_version, "attempt": attempt})
        persisted = _with_recorded_at(persisted)
        json_path = self.layout.node_output_json(stem)
        md_path = self.layout.node_output_markdown(stem)
        _write_json_any(json_path, persisted)
        md_path.write_text(_node_output_markdown(node_id, persisted), encoding="utf-8")
        self.record(
            "node.result_submitted",
            {"node_id": node_id, "execution_id": execution_id,
             "task_version": task_version, "attempt": attempt,
             "status": persisted.get("status"), "role": persisted.get("role"),
             "summary": str(persisted.get("summary") or persisted.get("content", ""))[:600],
             "public_reasoning": str(
                 persisted.get("public_reasoning")
                 or (persisted.get("metrics") or {}).get("public_reasoning")
                 or ""
             )[:4000]},
        )
        self.record(
            "node.io.output",
            {
                "node_id": node_id,
                "execution_id": execution_id,
                "task_version": task_version,
                "attempt": attempt,
                "json_path": str(json_path),
                "markdown_path": str(md_path),
                "status": persisted.get("status"),
                "role": persisted.get("role"),
                "content_chars": len(str(persisted.get("content", ""))),
                "summary": str(persisted.get("summary") or persisted.get("content", ""))[:600],
            },
        )
        for artifact_path, artifact_format in (
            (json_path, "json"),
            (md_path, "markdown"),
        ):
            self.record(
                "artifact.created",
                {
                    "node_id": node_id,
                    "role": persisted.get("role"),
                    "path": str(artifact_path),
                    "format": artifact_format,
                    "description": "节点执行结果已持久化",
                },
            )
        return json_path, md_path

    def get_events_by_type(self, event_type: str) -> list[dict[str, Any]]:
        """Return trace events matching an exact type or namespace prefix.

        Fault-recovery evaluation historically used this helper from its
        lightweight recorder. Keeping it here makes the production recorder and
        recovery reporter share the same interface.
        """
        matches = []
        prefix = event_type + "."
        for event in self.events:
            current_type = event.get("event_type", "")
            if current_type == event_type or current_type.startswith(prefix):
                flattened = dict(event.get("payload", {}))
                flattened.setdefault("event_type", current_type)
                flattened.setdefault("recorded_at", event.get("recorded_at"))
                matches.append(flattened)
        return matches

    def record_model_call(self, iteration: int, previous_tools: list[dict[str, Any]]) -> None:
        self.record(
            "llm.request",
            {"iteration": iteration, "tool_count": len(previous_tools)},
        )
        self.record(
            "model.call",
            {
                "iteration": iteration,
                "previous_tool_count": len(previous_tools),
                "previous_tools": [
                    {
                        "name": item.get("name"),
                        "has_result": bool(item.get("result")),
                    }
                    for item in previous_tools
                ],
            },
        )

    def record_tool_start(self, tool_call_id: str, name: str, args: dict[str, Any]) -> None:
        self.record(
            "tool.called",
            {"tool_call_id": tool_call_id, "tool_name": name, "arguments": args},
        )
        self.record(
            "tool.call.started",
            {
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "arguments": args,
            },
        )

    def record_tool_complete(
        self,
        tool_call_id: str,
        name: str,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        result_text = str(result)
        self.record(
            "tool.result",
            {"tool_call_id": tool_call_id, "tool_name": name,
             "result_preview": result_text[:800], "result_chars": len(result_text)},
        )
        self.record(
            "tool.call.completed",
            {
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "arguments": args,
                "result_preview": result_text[:800],
                "result_chars": len(result_text),
            },
        )
        if self.graph_store is not None:
            event_idx = len(self.events)
            self.graph_store.add_artifact(
                Artifact(
                    f"A-{event_idx:04d}",
                    self.default_node_id,
                    "tool_result",
                    f"trace://{tool_call_id}",
                    f"{name} returned {len(result_text)} chars",
                )
            )
            self.graph_store.add_metric(
                Metric(
                    f"M-{event_idx:04d}",
                    self.default_node_id,
                    f"tool.{name}.result_chars",
                    float(len(result_text)),
                    "chars",
                )
            )
        for derived_type in _classify_tool_result(name, args, result_text):
            self.record(
                derived_type,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "arguments": args,
                    "result_preview": result_text[:400],
                },
            )

    def record_event_callback(self, event_name: str, payload: dict[str, Any]) -> None:
        self.record(f"runtime.event.{event_name}", payload)

    def record_topology_sparsity(self, node_count: int, edge_count: int) -> dict[str, float]:
        """记录拓扑稀疏度指标 (SEVERE-5 fix)
        
        Args:
            node_count: 图中的节点数
            edge_count: 图中的边数
            
        Returns:
            包含稀疏度指标的字典
        """
        if node_count <= 1:
            sparsity = 1.0  # 单节点或无节点视为完全稀疏
        else:
            # 有向图最大可能边数 = N*(N-1)
            max_possible_edges = node_count * (node_count - 1)
            actual_edges = edge_count
            # 稀疏度 = 1 - (实际边数 / 最大可能边数)
            sparsity = 1.0 - (actual_edges / max_possible_edges) if max_possible_edges > 0 else 1.0
        
        metrics = {
            "node_count": node_count,
            "edge_count": edge_count,
            "sparsity": sparsity,
            "density": 1.0 - sparsity,
        }
        
        self.record("topology.sparsity", metrics)
        
        # 同时记录到 graph_store 的 metrics 表
        if self.graph_store is not None:
            self.graph_store.add_metric(
                Metric(
                    f"topology-sparsity-{len(self.events):04d}",
                    self.default_node_id,
                    "topology.sparsity",
                    sparsity,
                    "ratio",
                )
            )
            self.graph_store.add_metric(
                Metric(
                    f"topology-density-{len(self.events):04d}",
                    self.default_node_id,
                    "topology.density",
                    1.0 - sparsity,
                    "ratio",
                )
            )
        
        return metrics

    def summarize(self) -> dict[str, Any]:
        counts = Counter(event["event_type"] for event in self.events)
        summary = {
            "product": PRODUCT_NAME,
            "code_name": CODE_NAME,
            "event_count": len(self.events),
            "event_counts": dict(sorted(counts.items())),
            "trace_path": str(self.trace_path),
        }
        write_json(self.run_dir / "trace_summary.json", summary)
        return summary

def _classify_tool_result(name: str, args: dict[str, Any], result_text: str) -> list[str]:
    lowered = name.lower()
    events: list[str] = []
    file_tools = {"write_file", "edit_file", "replace_file", "apply_patch", "create_file"}
    if lowered in file_tools or any(key in args for key in ("path", "file_path", "target_file")):
        if any(token in lowered for token in ("write", "edit", "replace", "patch", "create")):
            events.append("file.modified")
    command = " ".join(str(args.get(key, "")) for key in ("cmd", "command", "script"))
    command_l = command.lower()
    if any(token in command_l for token in ("pytest", "npm test", "cargo test", "go test", "pnpm test")):
        events.append("test.executed")
    if "error" in result_text.lower() or "failed" in result_text.lower():
        events.append("tool.result.risk")
    return events


def _preview(payload: dict[str, Any]) -> str:
    return str(payload)[:1000]


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value))


def _with_recorded_at(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    return enriched


def _write_json_any(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _node_output_markdown(node_id: str, payload: dict[str, Any]) -> str:
    content = str(payload.get("content", ""))
    lines = [
        f"# Node {node_id} Output",
        "",
        f"- Role: `{payload.get('role', '')}`",
        f"- Status: `{payload.get('status', '')}`",
        f"- Execution Path: `{payload.get('metrics', {}).get('execution_path', '')}`",
        "",
        "## Content",
        "",
        content or "(empty)",
        "",
        "## Artifacts",
        "",
    ]
    artifacts = payload.get("artifacts") or []
    if artifacts:
        lines.extend(f"- `{artifact}`" for artifact in artifacts)
    else:
        lines.append("- None")
    lines.extend(["", "## Metrics", "", "```json"])
    lines.append(json.dumps(payload.get("metrics", {}), ensure_ascii=False, indent=2, default=str))
    lines.extend(["```", ""])
    return "\n".join(lines)
