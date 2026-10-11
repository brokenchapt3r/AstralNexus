from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import os
import threading
import time
from typing import Any, Callable


ProgressSink = Callable[[dict[str, Any]], None]


@dataclass
class _NodeProgress:
    node_id: str
    role: str = "unknown"
    worker_type: str = ""
    model: str = ""
    goal: str = ""
    status: str = "admitted"
    action: str = "等待调度"
    summary: str = ""
    started_at: float | None = None
    updated_at: float = field(default_factory=time.monotonic)
    request_count: int = 0
    tool_rounds: int = 0
    waiting_since: float | None = None
    last_wait_notice_at: float | None = None


class RunProgressReporter:
    """Project runtime trace events into concise, user-facing progress reports.

    TraceRecorder remains the sole event source. This class owns no execution
    decisions and never mutates the graph; it only maintains a disposable view
    used by the CLI/TUI.
    """

    def __init__(
        self,
        *,
        run_dir: str,
        sink: ProgressSink | None = None,
        interval_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        configured = (
            interval_seconds
            if interval_seconds is not None
            else float(os.environ.get("ASTRALNEXUS_PROGRESS_INTERVAL_SECONDS", "120"))
        )
        self.interval_seconds = max(1.0, float(configured))
        self.wait_notice_seconds = max(
            1.0,
            float(os.environ.get("ASTRALNEXUS_WAIT_NOTICE_SECONDS", "30")),
        )
        self.run_dir = str(run_dir)
        self.sink = sink
        self.clock = clock
        self.started_at = clock()
        self.last_event_at = self.started_at
        self.last_report_at = self.started_at
        self.nodes: dict[str, _NodeProgress] = {}
        self.current_wave = ""
        self.phase = "初始化"
        self.latest_completed = ""
        self.recovery_count = 0
        self.artifacts: list[str] = []
        self._closed = threading.Event()
        self._lock = threading.RLock()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="astralnexus-progress-reporter",
            daemon=True,
        )
        self._thread.start()

    def handle_event(self, event: dict[str, Any]) -> None:
        """Consume one trace event and forward it to the original UI sink."""
        event = dict(event or {})
        event_type = str(event.get("event_type", ""))
        now = self.clock()
        immediate: str | None = None
        with self._lock:
            self.last_event_at = now
            node_id = str(event.get("node_id") or "")

            node: _NodeProgress | None
            if event_type == "task.received":
                self.phase = "任务接收"
            elif event_type == "planning.started":
                self.phase = "动态规划"
            elif event_type == "planning.summary":
                self.phase = "规划完成，准备调度"
            elif event_type == "agent.activity" and node_id:
                node = self._node(node_id)
                node.role = str(event.get("role") or node.role)
                node.status = str(event.get("status") or node.status)
                node.action = self._clean(event.get("title"), 180) or node.action
                node.updated_at = now
            elif event_type == "artifact.created":
                details = event.get("details")
                path = (
                    str(details.get("path") or "")
                    if isinstance(details, dict)
                    else str(event.get("summary") or "")
                )
                if path and path not in self.artifacts:
                    self.artifacts.append(path)
            elif event_type == "recovery.triggered":
                self.recovery_count += 1
                self.phase = "恢复与重规划"
            elif event_type == "task.completed":
                self.phase = "已完成"
            elif event_type == "task.failed":
                self.phase = "验收未通过"
            elif event_type == "task.execution.started":
                node = self._node(node_id)
                node.role = str(event.get("role") or node.role)
                node.worker_type = str(event.get("worker_type") or node.worker_type)
                node.model = str(event.get("selected_model") or node.model)
                node.goal = self._clean(event.get("goal"), 180) or node.goal
                node.status = "running"
                node.action = "准备节点上下文并启动真实模型"
                node.started_at = node.started_at or now
                node.updated_at = now
                immediate = self._node_started(node)
            elif event_type == "node.started":
                node = self._node(node_id)
                node.role = str(event.get("role") or node.role)
                node.goal = self._clean(event.get("goal"), 180) or node.goal
                node.status = "running"
                node.action = "读取依赖证据并执行节点目标"
                node.started_at = node.started_at or now
                node.updated_at = now
                if not node.worker_type:
                    immediate = self._node_started(node)
            elif event_type == "task_graph.snapshot":
                for item in event.get("nodes") or []:
                    if not isinstance(item, dict):
                        continue
                    snapshot_node = self._node(str(item.get("node_id") or "unknown"))
                    snapshot_node.role = str(item.get("role") or snapshot_node.role)
                    snapshot_node.goal = (
                        self._clean(item.get("goal"), 180) or snapshot_node.goal
                    )
                    snapshot_node.status = str(
                        item.get("status") or snapshot_node.status
                    )
                    snapshot_node.updated_at = now
            elif event_type == "agent.state_changed" and node_id:
                node = self._node(node_id)
                node.role = str(event.get("role") or node.role)
                node.status = str(event.get("status") or node.status)
                node.action = self._clean(event.get("reason"), 180) or node.action
                node.updated_at = now
            elif event_type == "router.ready_wave":
                self.phase = "Ready-Wave 调度"
                wave_number = event.get("wave_id")
                self.current_wave = f"wave-{wave_number}"
                node_ids = [str(item) for item in event.get("node_ids") or []]
                immediate = (
                    f"[天枢阶段] Router 已形成 {self.current_wave}："
                    f"{'、'.join(node_ids) or '无节点'}。\n"
                    f"本波宽度：{int(event.get('wave_width') or len(node_ids))}。\n"
                    "接下来：Governor 为各节点匹配异构 Worker、模型、工具和资源，"
                    "满足依赖的节点并行启动。"
                )
            elif event_type in {"llm.request", "llm.stream.started"} and node_id:
                node = self.nodes.get(node_id)
                if node is not None:
                    node.request_count += 1
                    model = self._clean(event.get("model"), 80)
                    if model:
                        node.model = model
                    node.action = "正在等待模型生成下一步或工具调用"
                    node.waiting_since = node.waiting_since or now
                    node.updated_at = now
            elif event_type in {
                "llm.response", "llm.stream.completed",
            } and node_id:
                node = self.nodes.get(node_id)
                if node is not None:
                    node.waiting_since = None
                    node.last_wait_notice_at = None
                    node.updated_at = now
            elif event_type == "llm.tool_execution.start" and node_id:
                node = self.nodes.get(node_id)
                if node is not None:
                    node.waiting_since = None
                    node.last_wait_notice_at = None
                    calls = event.get("calls") or []
                    names = [
                        str(item.get("name") or "unknown")
                        for item in calls
                        if isinstance(item, dict)
                    ]
                    node.action = "正在执行工具: " + ", ".join(names[:4])
                    node.updated_at = now
                    immediate = self._node_action(node, event.get("reason"))
            elif event_type == "llm.tool_batch" and node_id:
                node = self.nodes.get(node_id)
                if node is not None:
                    node.waiting_since = None
                    node.last_wait_notice_at = None
                    calls = event.get("calls") or []
                    names = [
                        f"{item.get('name', 'unknown')}={item.get('status', 'unknown')}"
                        for item in calls
                        if isinstance(item, dict)
                    ]
                    node.tool_rounds += 1
                    node.action = "已取得工具结果，正在整理证据"
                    node.updated_at = now
                    # Tool-heavy workers can emit hundreds of batches. Report the
                    # first batch and then periodic milestones without flooding TUI.
                    if node.tool_rounds == 1 or node.tool_rounds % 10 == 0:
                        immediate = self._node_action(
                            node,
                            "工具批次 "
                            + str(node.tool_rounds)
                            + ("：" + "，".join(names[:4]) if names else ""),
                        )
            elif event_type == "node.result_submitted":
                node = self._node(node_id)
                node.status = "verifying"
                node.summary = self._clean(event.get("summary"), 320)
                node.action = "结果已提交，等待 Verifier 检查证据与验收条件"
                node.updated_at = now
                immediate = self._node_result(node)
            elif event_type == "verifier.verified":
                node = self._node(node_id)
                verdict = str(event.get("verdict") or event.get("status") or "unknown")
                score = event.get("overall_score", event.get("score"))
                node.action = f"Verifier 判定 {verdict}" + (
                    f"，得分 {float(score):.2f}" if isinstance(score, (int, float)) else ""
                )
                node.updated_at = now
                immediate = self._node_action(
                    node,
                    "验证完成；下一步由 Governor 决定通过、修复或重规划",
                )
            elif event_type == "node.completed":
                node = self._node(node_id)
                node.status = str(event.get("status") or "completed")
                node.action = "节点已完成"
                node.updated_at = now
                self.latest_completed = node.node_id
                immediate = self._node_completed(node, event)
            elif event_type in {
                "agent.failed",
                "llm.error",
                "llm.stream.failed",
                "task.execution.failed",
            } and node_id:
                node = self._node(node_id)
                node.status = "failed"
                node.action = "失败已记录，等待恢复策略"
                node.updated_at = now
                immediate = self._node_failure(node, event)
            elif event_type in {
                "orchestrator.verification_recovery.applied",
                "orchestrator.graph_patch.applied",
                "governor.graph_patch.applied",
                "orchestrator.requirement_change.replanned",
            }:
                self.phase = "恢复与重规划"
                immediate = self._graph_change(event_type, event)
            elif event_type == "ready_wave.concurrency_decided":
                self.phase = "Ready-Wave 并行执行"
                self.current_wave = str(event.get("wave_id") or "")
                immediate = (
                    f"[天枢阶段] {self.current_wave or 'Ready-Wave'} 准备执行："
                    f"{int(event.get('node_count') or 0)} 个就绪节点，"
                    f"并发宽度 {int(event.get('selected_parallel') or 1)}。\n"
                    "接下来：各 Worker 独立执行，完成后逐节点进入 Verifier。"
                )
            elif event_type.startswith("orchestrator.autonomous_cycle."):
                self.phase = event_type.rsplit(".", 1)[-1]
            elif event_type == "governor.execution_completion.assessed":
                self.phase = "全局验收"
                immediate = (
                    "[天枢阶段] Global Verifier 已完成交付完整性检查。\n"
                    f"结论：{self._clean(event.get('verdict') or event.get('status') or event.get('reason'), 260)}\n"
                    "接下来：通过则交给 Synthesizer 形成交付包；存在缺口则由 Governor 最小补图。"
                )
            elif event_type in {"orchestrator.completed", "orchestrator.blocked"}:
                self.phase = "已完成" if event_type.endswith("completed") else "已阻塞"
                immediate = self.build_report(final=True)
            elif event_type.startswith(("interactive.run.", "interactive.resume.")):
                immediate = self._clean(event.get("text"), 1200)

        if self.sink is not None:
            forwarded = dict(event)
            forwarded["progress_reported"] = True
            self._safe_sink(forwarded)
        if immediate:
            self._emit(immediate, kind="immediate")

    def build_report(self, *, final: bool = False) -> str:
        with self._lock:
            counts = Counter(node.status for node in self.nodes.values())
            active = [
                node for node in self.nodes.values()
                if node.status in {"running", "verifying", "recovering"}
            ]
            elapsed = max(0.0, self.clock() - self.started_at)
            title = "[天枢最终进展]" if final else "[天枢定时进展]"
            lines = [
                f"{title} 阶段：{self.phase}；已运行 {self._duration(elapsed)}",
                (
                    "节点："
                    f"完成 {counts['completed']}，运行 {counts['running']}，"
                    f"验证 {counts['verifying']}，失败 {counts['failed']}，"
                    f"已知总数 {len(self.nodes)}"
                ),
            ]
            if final:
                lines.extend(
                    [
                        f"总节点数：{len(self.nodes)}",
                        f"成功节点：{counts['completed']}",
                        f"失败节点：{counts['failed']}",
                        f"恢复次数：{self.recovery_count}",
                    ]
                )
            if active:
                lines.append("当前工作：")
                for node in sorted(active, key=lambda item: item.node_id)[:8]:
                    running = (
                        self._duration(self.clock() - node.started_at)
                        if node.started_at is not None else "刚启动"
                    )
                    identity = node.worker_type or node.role
                    model = f" / {node.model}" if node.model else ""
                    lines.append(
                        f"- {node.node_id} [{identity}{model}] {node.action}（{running}）"
                    )
            elif not final:
                lines.append("当前工作：没有运行中的节点，控制面正在选取下一波或执行验收。")
            queued = [
                node for node in self.nodes.values()
                if node.status in {
                    "proposed", "admitted", "waiting_dependencies", "ready",
                    "repair_pending", "recovering", "blocked",
                }
            ]
            if queued:
                lines.append(
                    "等待/恢复："
                    + "；".join(
                        f"{node.node_id}[{node.role}]={node.status}"
                        for node in sorted(queued, key=lambda item: item.node_id)[:10]
                    )
                )
            lines.append("接下来：" + self._next_step(active, counts))
            lines.append(f"输出目录：{self.run_dir}")
            if final and self.artifacts:
                lines.append(
                    "主要交付物：" + "；".join(self.artifacts[-5:])
                )
            return "\n".join(lines)

    def close(self, *, final_status: str | None = None) -> None:
        if final_status:
            with self._lock:
                self.phase = str(final_status)
        self._closed.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _heartbeat_loop(self) -> None:
        while not self._closed.wait(min(self.interval_seconds, 1.0)):
            now = self.clock()
            with self._lock:
                due = now - self.last_report_at >= self.interval_seconds
                waiting = [
                    node for node in self.nodes.values()
                    if node.waiting_since is not None
                    and now - node.waiting_since >= self.wait_notice_seconds
                    and (
                        node.last_wait_notice_at is None
                        or now - node.last_wait_notice_at >= self.wait_notice_seconds
                    )
                ]
                for node in waiting:
                    node.last_wait_notice_at = now
            for node in waiting:
                elapsed = self._duration(now - (node.waiting_since or now))
                self._emit(
                    f"[节点 {node.node_id}] 仍在等待 {node.model or '模型'} 返回，"
                    f"已等待 {elapsed}；进程仍在运行。\n"
                    "接下来：收到增量文本或工具调用后立即展示；若网络失败，"
                    "按 Provider 策略重试并记录错误。",
                    kind="provider_wait",
                )
            if due:
                self._emit(self.build_report(), kind="heartbeat")

    def _emit(self, text: str, *, kind: str) -> None:
        if text.startswith("[天枢最终进展]"):
            kind = "final"
        with self._lock:
            self.last_report_at = self.clock()
        event = {
            "event_type": "runtime.progress.report",
            "text": text,
            "kind": kind,
            "phase": self.phase,
            "run_dir": self.run_dir,
            "recorded_at": datetime.now().astimezone().isoformat(),
        }
        if self.sink is None:
            print(text, flush=True)
        else:
            self._safe_sink(event)

    def _safe_sink(self, event: dict[str, Any]) -> None:
        try:
            sink = self.sink
            if sink is not None:
                sink(dict(event))
        except Exception:
            return

    def _node(self, node_id: str) -> _NodeProgress:
        key = node_id or "unknown"
        if key not in self.nodes:
            self.nodes[key] = _NodeProgress(node_id=key)
        return self.nodes[key]

    def _node_started(self, node: _NodeProgress) -> str:
        identity = node.worker_type or node.role
        model = f"；模型 {node.model}" if node.model else ""
        return (
            f"[节点 {node.node_id}] 开始执行；Worker {identity}{model}。\n"
            f"目标：{node.goal or '按节点契约完成工作'}\n"
            "当前：准备依赖证据、工具和模型上下文。\n"
            "接下来：执行节点任务，提交结构化结果与真实产物，再进入 Verifier。"
        )

    def _node_action(self, node: _NodeProgress, reason: Any = "") -> str:
        return (
            f"[节点 {node.node_id}] 进展：{node.action}。\n"
            f"依据：{self._clean(reason, 240) or '当前节点契约要求取得可验证执行证据'}\n"
            "接下来：继续当前动作；取得结果后写入节点输出并提交验证。"
        )

    def _node_result(self, node: _NodeProgress) -> str:
        return (
            f"[节点 {node.node_id}] 已提交结果，进入验证。\n"
            f"阶段摘要：{node.summary or '结果已落盘，详细内容见 node_io'}\n"
            "接下来：Verifier 检查验收条件、证据引用和产物真实性。"
        )

    def _node_completed(self, node: _NodeProgress, event: dict[str, Any]) -> str:
        score = event.get("verification_score")
        score_text = (
            f"；验证分 {float(score):.2f}" if isinstance(score, (int, float)) else ""
        )
        return (
            f"[节点 {node.node_id}] 已完成{score_text}。\n"
            f"结果：{node.summary or '节点结果与证据已接受'}\n"
            "接下来：唤醒依赖该产物的节点；若当前波结束，则由 Router 选择下一 Ready-Wave。"
        )

    def _node_failure(self, node: _NodeProgress, event: dict[str, Any]) -> str:
        error = self._clean(
            event.get("error") or event.get("reason") or event.get("message"),
            260,
        )
        return (
            f"[节点 {node.node_id}] 执行失败：{error or '运行时已记录失败事件'}。\n"
            "接下来：故障分类后由 Governor 决定重试、替换 Worker、补图或终止。"
        )

    def _graph_change(self, event_type: str, event: dict[str, Any]) -> str:
        added = event.get("added") or event.get("patch", {}).get("added") or []
        updated = event.get("updated") or event.get("patch", {}).get("updated") or []
        reason = event.get("reasoning") or event.get("reason") or event_type
        return (
            f"[天枢控制面] 任务图已调整：新增 {len(added)}，更新 {len(updated)}。\n"
            f"原因：{self._clean(reason, 260)}\n"
            "接下来：重新计算依赖与能力匹配，只调度满足条件的 Ready 节点。"
        )

    @staticmethod
    def _clean(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").replace("\r", " ").split())
        return text[:limit]

    @staticmethod
    def _duration(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minutes:02d}m"
        if minutes:
            return f"{minutes}m{secs:02d}s"
        return f"{secs}s"

    @staticmethod
    def _next_step(active: list[_NodeProgress], counts: Counter) -> str:
        if active:
            ids = "、".join(node.node_id for node in active[:6])
            return f"等待 {ids} 产出并逐节点验证；不阻塞的同波节点继续并行。"
        if counts["failed"]:
            return "Governor 检查失败证据，选择恢复、替换 Worker 或最小补图。"
        if counts["verifying"]:
            return "等待 Verifier 判定，通过后唤醒下游节点。"
        return "Router 计算下一 Ready-Wave；任务图终止时转入 Global Verifier 与交付整合。"
