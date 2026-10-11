from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
import sqlite3
from contextlib import closing

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
        owner_token_provider: Callable[[], dict[str, Any] | None] | None = None,
    ):
        self.run_dir = run_dir
        self.trace_path = run_dir / "trace.jsonl"
        self.layout = RunOutputLayout(run_dir)
        self.layout.ensure_runtime_dirs()
        self.node_io_dir = self.layout.node_io_dir
        self.events: list[dict[str, Any]] = []
        self._state_lock = RLock()
        self._recorder_id = uuid.uuid4().hex
        self._record_sequence = 0
        self._owner_token_provider = owner_token_provider
        self._last_owner: dict[str, Any] = {}
        self._execution_context: ContextVar[dict[str, tuple[str, int, int, str]]] = ContextVar("trace_execution_context", default={})
        self._recovery_context: ContextVar[str] = ContextVar("recovery_episode", default="")
        self._recovery_attempt: ContextVar[int] = ContextVar("recovery_attempt", default=-1)
        self.graph_store = graph_store
        self.default_node_id = default_node_id
        self.event_callback = event_callback
        self.event_bus = event_bus or RuntimeEventBus(run_dir)
        self.event_bus.subscribe(self._record_public_event)
        self._execution_counter = 0
        self._active_executions: dict[str, tuple[str, int, int, str]] = {}

    @property
    def recovery_operation_id(self) -> str:
        context = getattr(self, "_recovery_context", None)
        episode = str(context.get() or "") if context is not None else ""
        attempt = self._recovery_attempt.get() if hasattr(self, "_recovery_attempt") else -1
        return episode + (":attempt-%s" % attempt if episode and attempt >= 0 else "")

    @contextmanager
    def recovery_scope(self):
        if not hasattr(self, "_recovery_context"):
            self._recovery_context = ContextVar("recovery_operation", default="")
        current = str(self._recovery_context.get() or "")
        token = self._recovery_context.set(current or uuid.uuid4().hex)
        if not hasattr(self, "_recovery_attempt"):
            self._recovery_attempt = ContextVar("recovery_attempt", default=-1)
        attempt_token = self._recovery_attempt.set(self._recovery_attempt.get())
        try:
            yield self.recovery_operation_id
        finally:
            self._recovery_attempt.reset(attempt_token)
            self._recovery_context.reset(token)

    def set_recovery_attempt(self, attempt: int) -> None:
        self._recovery_attempt.set(int(attempt))

    def _append_event(self, event: dict[str, Any]) -> None:
        # Observers run after this boundary; callbacks may safely query state.
        with self._state_lock:
            self._record_sequence += 1
            event["trace_recorder_id"] = self._recorder_id
            event["trace_sequence"] = self._record_sequence
            append_jsonl(self.trace_path, event)
            self.events.append(event)

    def _owner_info(self) -> dict[str, Any]:
        if self._owner_token_provider is None:
            return {}
        try:
            token = self._owner_token_provider()
            if token is not None:
                if token.get("runs_db_path"):
                    with closing(sqlite3.connect(Path(token["runs_db_path"]).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as database:
                        current = database.execute(
                            "SELECT 1 FROM runs WHERE run_id=? AND owner_id=? AND generation=? AND status='running'"
                            " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                            (token["run_id"], token["owner_id"], token["generation"], token["now_iso"]),
                        ).fetchone()
                    if current is None:
                        token = None
                if token is None:
                    with self._state_lock:
                        return {**self._last_owner, "trace_owner_valid": False, "trace_owner_error": "ownership_unavailable"}
                info = {"trace_owner_valid": True, "generation": int(token["generation"]), "owner_id": str(token["owner_id"])}
                with self._state_lock:
                    self._last_owner = info
                return info
            error_type = "ownership_unavailable"
        except Exception as error:
            error_type = type(error).__name__
        with self._state_lock:
            return {**self._last_owner, "trace_owner_valid": False, "trace_owner_error": error_type}

    def _require_node_io_owner(self) -> dict[str, Any]:
        info = self._owner_info()
        if info.get("trace_owner_valid") is False:
            raise TraceOwnershipLost("cannot write node IO after ownership loss")
        return info

    def record(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event_type = str(getattr(event_type, "value", event_type))
        normalized_payload = dict(payload or {})
        normalized_payload.update(self._owner_info())
        if self.recovery_operation_id:
            normalized_payload.setdefault("recovery_operation_id", self.recovery_operation_id)
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
        event: dict[str, Any] = {
            "product": PRODUCT_NAME,
            "code_name": CODE_NAME,
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "payload": normalized_payload,
        }
        self._append_event(event)
        if self.graph_store is not None and normalized_payload.get("trace_owner_valid") is not False:
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
        self._append_event(event)
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
        owner = self._require_node_io_owner()
        task_version = int(payload.get("task_version") or payload.get("metadata", {}).get("task_version") or 1)
        attempt = int(payload.get("attempt") or payload.get("retry_count", 0) or 0) + 1
        with self._state_lock:
            self._execution_counter += 1
            execution_id = f"exec-{self._execution_counter:06d}-{self._recorder_id}"
            stem = self.layout.execution_stem(execution_id, node_id, task_version, attempt)
            active = (execution_id, task_version, attempt, stem)
            self._active_executions[node_id] = active
        local = dict(self._execution_context.get())
        local[node_id] = active
        self._execution_context.set(local)
        persisted = dict(payload)
        persisted.update(owner)
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
        owner = self._require_node_io_owner()
        with self._state_lock:
            active = self._execution_context.get().get(node_id) or self._active_executions.get(node_id)
            if active is None:
                task_version = int(payload.get("task_version") or 1)
                attempt = int(payload.get("attempt") or 1)
                self._execution_counter += 1
                execution_id = f"exec-{self._execution_counter:06d}-{self._recorder_id}"
                stem = self.layout.execution_stem(execution_id, node_id, task_version, attempt)
            else:
                execution_id, task_version, attempt, stem = active
        persisted = dict(payload)
        persisted.update(owner)
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
             "execution_outcome": persisted.get("execution_outcome"),
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
        with self._state_lock:
            if self._active_executions.get(node_id) == active:
                self._active_executions.pop(node_id, None)
        local = dict(self._execution_context.get())
        if local.get(node_id) == active:
            local.pop(node_id, None)
            self._execution_context.set(local)
        return json_path, md_path

    def get_events_by_type(self, event_type: str) -> list[dict[str, Any]]:
        """Return trace events matching an exact type or namespace prefix.

        Fault-recovery evaluation historically used this helper from its
        lightweight recorder. Keeping it here makes the production recorder and
        recovery reporter share the same interface.
        """
        matches = []
        prefix = event_type + "."
        with self._state_lock:
            events = list(self.events)
        for event in events:
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
                "result": result if isinstance(result, dict) else None,
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
        with self._state_lock:
            events = list(self.events)
        counts = Counter(event["event_type"] for event in events)
        summary = {
            "product": PRODUCT_NAME,
            "code_name": CODE_NAME,
            "event_count": len(events),
            "event_counts": dict(sorted(counts.items())),
            "trace_path": str(self.trace_path),
        }
        write_json(self.run_dir / "trace_summary.json", summary)
        return summary

class TraceOwnershipLost(RuntimeError):
    pass


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
