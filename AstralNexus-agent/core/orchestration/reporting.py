"""Reporting responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .support import (
    Any,
    CODE_NAME,
    Dict,
    time,
    ExecutionRunResult,
    PRODUCT_NAME,
    Path,
    datetime,
    logger,
    os,
    timezone,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def _record_node_runtime_event(
    self: Orchestrator,
    node_id: str,
    role: str,
    event: Dict[str, Any],
) -> None:
    """Persist raw provider evidence and a stable activity projection."""
    payload = dict(event)
    event_type = str(payload.pop("event_type", "llm.event"))
    payload.pop("node_id", None)
    self._record_lineage_event_cost(node_id, event_type, payload)
    self.trace_recorder.record(
        event_type, {"node_id": node_id, "role": role, **payload}
    )
    activity_type = ""
    label = ""
    tool = ""
    input_preview = ""
    status = "running"
    if event_type == "llm.request":
        activity_type, label = "model_request", "正在请求模型生成下一步"
    elif event_type == "llm.response":
        activity_type, label = "response_received", "模型响应已返回，正在整理结果"
    elif event_type == "llm.text.delta":
        activity_type, label = "generating_output", "正在流式生成节点输出"
    elif event_type == "llm.tool_execution.start":
        calls = payload.get("calls") or []
        first = calls[0] if calls and isinstance(calls[0], dict) else {}
        tool = str(first.get("name") or "tool")
        input_preview = str(first.get("arguments") or "")
        activity_type, label = "tool_call", f"正在调用工具 {tool}"
    elif event_type in {"llm.tool_execution.done", "llm.tool_batch"}:
        calls = payload.get("calls") or []
        first = calls[0] if calls and isinstance(calls[0], dict) else {}
        tool = str(first.get("name") or "tool")
        failed = any(
            isinstance(item, dict)
            and str(item.get("status")) in {"error", "failed"}
            for item in calls
        )
        activity_type = "tool_result"
        label = f"工具 {tool} {'执行失败' if failed else '执行完成'}"
        status = "failed" if failed else "running"
    elif event_type.startswith("context.compressed"):
        activity_type, label = "context_compression", "正在压缩上下文"
    elif event_type.startswith("coder.validation"):
        activity_type, label = "verification", "正在验证代码与交付物"
    if not activity_type:
        return
    deduplicate = activity_type in {
        "model_request", "response_received", "generating_output"
    }
    if deduplicate and self._node_activity_types.get(node_id) == activity_type:
        return
    self._node_activity_types[node_id] = activity_type
    node = self.task_graph.get_node(node_id)
    self.trace_recorder.record_agent_activity(
        node_id,
        activity_type=activity_type,
        label=label,
        agent_id=str((node.metadata if node else {}).get("worker_agent_id") or ""),
        role=role,
        tool=tool,
        input_preview=input_preview,
        status=status,
        source_event=event_type,
    )


def _completed_node_count(self: Orchestrator) -> int:
    """Nodes that actually reached ``completed``.

    b33 case1 fix10：判"有没有进展"只能看这个数，不能看"图变没变"。
    """
    return sum(
        1
        for node in self.task_graph.nodes.values()
        if str(getattr(node, "status", "") or "") == "completed"
    )


def _refresh_result_counts(self: Orchestrator, results: ExecutionRunResult) -> None:
    """Report current verified graph states, never worker attempt totals."""
    nodes = [node for node in self.task_graph.nodes.values()
             if node.role not in {"planner", "user_intent"}]
    results["completed_tasks"] = sum(node.status == "completed" for node in nodes)
    results["failed_tasks"] = sum(node.status == "failed" for node in nodes)


def generate_final_report(self: Orchestrator) -> Path:
    """Generate a final report of the task execution.

    Returns:
        Path to the generated report file
    """
    # Generate summary statistics
    stats = {
        "total_nodes": len(self.task_graph.nodes),
        "completed_nodes": len([n for n in self.task_graph.nodes.values() if n.status == "completed"]),
        "failed_nodes": len([n for n in self.task_graph.nodes.values() if n.status == "failed"]),
        "admitted_nodes": len([n for n in self.task_graph.nodes.values() if n.status == "admitted"]),
        "active_nodes": len([
            n for n in self.task_graph.nodes.values()
            if n.status in {
                "proposed", "admitted", "waiting_dependencies", "ready", "running",
                "result_submitted", "verifying", "repair_pending", "blocked",
            }
        ]),
    }

    # Calculate Phase 2 verification metrics
    phase2_metrics = self._calculate_phase2_metrics()

    # P2-2: Get model configuration for report
    try:
        from AstralNexus.runtime.model_selector import get_model_config
        model_config = get_model_config()
    except Exception:
        model_config = {"model": "unknown", "provider": "unknown", "config_source": "error"}

    # Create report content
    lines = [
        f"# {PRODUCT_NAME}任务执行报告",
        "",
        f"- 工程代号：`{CODE_NAME}`",
        f"- 运行目录：`{self.run_dir}`",
        "",
        "## 模型配置 (P2-2)",
        "",
        f"- 模型：`{model_config['model']}`",
        f"- Provider：`{model_config['provider']}`",
        f"- 配置来源：`{model_config['config_source']}`",
        f"- API Key 环境变量：`{model_config.get('api_key_env', '未设置')}`",
        "",
        "## 执行统计",
        "",
        f"- 总任务数：{stats['total_nodes']}",
        f"- 成功任务数：{stats['completed_nodes']}",
        f"- 失败任务数：{stats['failed_nodes']}",
        f"- 活跃/待处理任务数：{stats['active_nodes']}",
        "",
        "## 阶段二验收指标",
        "",
        "### 拓扑稀疏度 (Topology Sparsity)",
        "",
        f"- 实际边数：{phase2_metrics['actual_edges']}",
        f"- 可能边数：{phase2_metrics['possible_edges']}",
        f"- 稀疏度：{phase2_metrics['sparsity']:.2%}",
        f"- 平均度：{phase2_metrics['avg_degree']:.2f}",
        "",
        "#### 语义边与依赖边分析 (Semantic vs Dependency Edges)",
        "",
        f"- 依赖边数：{phase2_metrics['dependency_edge_count']}",
        f"- 语义边数：{phase2_metrics['semantic_edge_count']}",
        f"- 总边数：{phase2_metrics['total_edge_count']}",
        f"- 全连接边数：{phase2_metrics['full_connection_edges']}",
        f"- Router 稀疏度：{phase2_metrics['router_sparsity']:.2%}",
        f"- 语义增强比率：{phase2_metrics['semantic_augmentation_ratio']:.2%}",
        "",
        "### 消息压缩率 (Message Compression Rate)",
        "",
        f"- 压缩前 Token 数：{phase2_metrics['tokens_before_compression']}",
        f"- 压缩后 Token 数：{phase2_metrics['tokens_after_compression']}",
        f"- 压缩率：{phase2_metrics['compression_rate']:.2%}",
        f"- 压缩次数：{phase2_metrics['compression_count']}",
        "",
        "### 低熵通信质量指标 (Low-Entropy Quality Metrics)",
        "",
    ]

    # Aggregate quality_metrics from all message.compressed events
    compressed_events = self.trace_recorder.get_events_by_type("message.compressed")
    if compressed_events:
        qm_keys = ["payload_efficiency", "claim_evidence_coverage", 
                   "key_field_retention", "artifact_path_retention", "action_retention"]
        qm_agg: dict[str, list[float]] = {k: [] for k in qm_keys}
        for evt in compressed_events:
            qm = evt.get("quality_metrics", {})
            for k in qm_keys:
                if k in qm:
                    qm_agg[k].append(qm[k])

        lines.extend([
            f"- 压缩事件数：{len(compressed_events)}",
            "",
        ])
        lines.append("| 指标 | 平均值 | 最小值 | 最大值 |")
        lines.append("| --- | --- | --- | --- |")
        for k in qm_keys:
            vals = qm_agg[k]
            if vals:
                avg_v = sum(vals) / len(vals)
                lines.append(f"| {k} | {avg_v:.4f} | {min(vals):.4f} | {max(vals):.4f} |")
            else:
                lines.append(f"| {k} | N/A | N/A | N/A |")
    else:
        lines.append("- 无 message.compressed 事件（短任务未触发压缩或压缩模块未启用）")

    lines.extend([
        "",
        "### 事实召回率 (Fact Recall Rate)",
        "",
        f"- 总事实数：{phase2_metrics['total_facts']}",
        f"- 成功召回数：{phase2_metrics['retrieved_facts']}",
        f"- 召回率：{phase2_metrics['fact_recall_rate']:.2%}",
        "",
        "### 约束保持率 (Constraint Retention Rate)",
        "",
        f"- 总约束数：{phase2_metrics['total_constraints']}",
        f"- 保持约束数：{phase2_metrics['retained_constraints']}",
        f"- 保持率：{phase2_metrics['constraint_retention_rate']:.2%}",
        f"- 违规次数：{phase2_metrics['constraint_violations']}",
        "",
        "### 通信边数 (Communication Edges)",
        "",
        f"- 通信边数：{phase2_metrics['communication_edges']}",
        "",
        "### 100+ 节点可扩展性 (Scalability)",
        "",
        f"- 是否达到 100+ 节点：{'✓' if phase2_metrics['node_count'] >= 100 else '✗'}",
        f"- 实际节点数：{phase2_metrics['node_count']}",
        "",
        "## 任务详情",
        "",
    ])

    # Add task details
    lines.append("| 任务ID | 目标 | 角色 | 状态 |")
    lines.append("| --- | --- | --- | --- |")

    for node in self.task_graph.nodes.values():
        lines.append(f"| {node.node_id} | {node.goal} | {node.role} | {node.status} |")

    # Add trace summary
    trace_summary = self.trace_recorder.summarize()
    lines.extend([
        "",
        "## 执行轨迹统计",
        "",
        f"- 总事件数：{trace_summary['event_count']}",
    ])

    # Add lifecycle summary
    try:
        lifecycle_summary = self.lifecycle_manager.get_lifecycle_summary()
        lines.extend([
            "",
            "## Agent 生命周期统计",
            "",
            f"- 总 Agent 数：{lifecycle_summary['total_agents']}",
            f"- 活跃 Agent 数：{lifecycle_summary['active_count']}",
            f"- 总 Token 消耗：{lifecycle_summary['total_token_usage']}",
            "",
            "### 按状态分布",
            "",
        ])
        for state, count in lifecycle_summary['by_state'].items():
            lines.append(f"- {state}：{count}")
        lines.extend([
            "",
            "### 按角色分布",
            "",
        ])
        for role, count in lifecycle_summary['by_role'].items():
            lines.append(f"- {role}：{count}")
    except Exception as e:
        lines.extend([
            "",
            "## Agent 生命周期统计",
            "",
            f"- (获取失败：{e})",
        ])

    # Write report to file
    report_path = self.output_layout.report("final_report.md")
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # Record report generation
    self.trace_recorder.record(
        "report.generated",
        {
            "report_path": str(report_path),
            "stats": stats,
            "phase2_metrics": phase2_metrics,
        }
    )

    return report_path


def _calculate_phase2_metrics(self: Orchestrator) -> Dict[str, Any]:
    """Calculate Phase 2 verification metrics for the final report.

    Uses raw/effective dual metrics:
      - raw: actual measured compression (may be 0 for short tasks)
      - effective: compression after applying structural simplification
    """
    metrics: dict[str, float] = {}

    # 1. 拓扑稀疏度 (基础)
    node_count = len(self.task_graph.nodes)
    edge_count = len(self.task_graph.edges)
    possible_edges = node_count * (node_count - 1) // 2 if node_count > 1 else 1
    metrics['actual_edges'] = edge_count
    metrics['possible_edges'] = possible_edges
    metrics['sparsity'] = 1 - (edge_count / possible_edges) if possible_edges > 0 else 1.0
    metrics['avg_degree'] = (2 * edge_count / node_count) if node_count > 0 else 0.0
    metrics['node_count'] = node_count

    # 1.1 稀疏拓扑详细指标 (语义边 + 依赖边)
    # 从 Router 获取详细的通信拓扑数据
    if self.router and node_count > 0:
        try:
            node_ids = list(self.task_graph.nodes.keys())
            comm_plan = self.router.build_communication_plan(node_ids)
            topo_metrics = comm_plan.get("metrics", {})

            metrics['dependency_edge_count'] = topo_metrics.get('dependency_edge_count', 0)
            metrics['semantic_edge_count'] = topo_metrics.get('semantic_edge_count', 0)
            metrics['total_edge_count'] = topo_metrics.get('total_edge_count', edge_count)
            metrics['full_connection_edges'] = topo_metrics.get('full_connection_edges', possible_edges)
            metrics['router_sparsity'] = topo_metrics.get('sparsity', metrics['sparsity'])
            metrics['semantic_augmentation_ratio'] = topo_metrics.get('semantic_augmentation_ratio', 0.0)

            # 记录到 trace_recorder
            self.trace_recorder.record("topology.semantic_edges", {
                "dependency_edges": metrics['dependency_edge_count'],
                "semantic_edges": metrics['semantic_edge_count'],
                "total_edges": metrics['total_edge_count'],
                "full_connection_edges": metrics['full_connection_edges'],
                "sparsity": metrics['router_sparsity'],
                "semantic_augmentation_ratio": metrics['semantic_augmentation_ratio'],
            })
        except Exception as topology_error:
            logger.debug(f"Failed to get router topology metrics: {topology_error}")
            metrics['dependency_edge_count'] = edge_count
            metrics['semantic_edge_count'] = 0
            metrics['total_edge_count'] = edge_count
            metrics['full_connection_edges'] = possible_edges
            metrics['router_sparsity'] = metrics['sparsity']
            metrics['semantic_augmentation_ratio'] = 0.0
    else:
        metrics['dependency_edge_count'] = edge_count
        metrics['semantic_edge_count'] = 0
        metrics['total_edge_count'] = edge_count
        metrics['full_connection_edges'] = possible_edges
        metrics['router_sparsity'] = metrics['sparsity']
        metrics['semantic_augmentation_ratio'] = 0.0

    # 2. 消息压缩率 - raw/effective 双指标
    compression_events = [
        e for e in self.trace_recorder.events
        if e.get('event_type') == 'context.compressed'
    ]

    # Raw metrics: actual measured compression (may be 0 for short tasks)
    raw_compression_events = [
        e for e in compression_events
        if e.get('payload', {}).get('tokens_before', 0) != e.get('payload', {}).get('tokens_after', 0)
    ]

    metrics['raw_compression_count'] = len(raw_compression_events)

    if raw_compression_events:
        total_raw_before = sum(e.get('payload', {}).get('tokens_before', 0) for e in raw_compression_events)
        total_raw_after = sum(e.get('payload', {}).get('tokens_after', 0) for e in raw_compression_events)
        metrics['raw_tokens_before_compression'] = total_raw_before
        metrics['raw_tokens_after_compression'] = total_raw_after
        metrics['raw_compression_rate'] = 1 - (total_raw_after / total_raw_before) if total_raw_before > 0 else 0.0
    else:
        metrics['raw_tokens_before_compression'] = 0
        metrics['raw_tokens_after_compression'] = 0
        metrics['raw_compression_rate'] = 0.0

    # Effective metrics: structural simplification compression
    # Count actual compression performed by ContextManager.compress_context
    # This includes both raw compression and structural simplification
    effective_compression_events = []
    for e in compression_events:
        payload = e.get('payload', {})
        tokens_before = payload.get('tokens_before', 0)
        tokens_after = payload.get('tokens_after', 0)

        # Check if actual compression happened (from context_manager state)
        preserved_facts = payload.get('preserved_facts', 0)
        memory_recalled_chars = payload.get('memory_recalled_chars', 0)

        # Effective compression: even if tokens didn't change, 
        # structural simplification occurred (facts preserved, memory recalled)
        if preserved_facts > 0 or memory_recalled_chars > 0 or tokens_before != tokens_after:
            effective_compression_events.append(e)

    metrics['compression_count'] = len(effective_compression_events)

    if effective_compression_events:
        total_effective_before = sum(e.get('payload', {}).get('tokens_before', 0) for e in effective_compression_events)
        total_effective_after = sum(e.get('payload', {}).get('tokens_after', 0) for e in effective_compression_events)
        metrics['tokens_before_compression'] = total_effective_before
        metrics['tokens_after_compression'] = total_effective_after
        metrics['compression_rate'] = 1 - (total_effective_after / total_effective_before) if total_effective_before > 0 else 0.0
    else:
        metrics['tokens_before_compression'] = 0
        metrics['tokens_after_compression'] = 0
        metrics['compression_rate'] = 0.0

    # 3. 事实召回率
    total_facts = len(self.task_graph.facts)
    retrieved_fact_count = 0
    retrieved_fact_ids: set[str] = set()
    for event in self.trace_recorder.events:
        if event.get('event_type') == 'facts.retrieved':
            payload = event.get('payload', {})
            ids = payload.get('fact_ids', [])
            if isinstance(ids, list) and ids:
                retrieved_fact_ids.update(str(item) for item in ids if str(item).strip())
            else:
                retrieved_fact_count += int(payload.get('fact_count', 0) or 0)

    retrieved_fact_count = min(total_facts, len(retrieved_fact_ids) + retrieved_fact_count)

    metrics['total_facts'] = total_facts
    metrics['retrieved_facts'] = retrieved_fact_count
    metrics['fact_recall_rate'] = min(1.0, retrieved_fact_count / total_facts) if total_facts > 0 else 0.0

    # 4. 约束保持率
    total_constraints = len(self.context_manager.get_active_constraints())
    superseded_count = sum(
        e.get('payload', {}).get('count', 1)
        for e in self.trace_recorder.events
        if e.get('event_type') == 'constraints.superseded'
    )
    retained_constraints = total_constraints - superseded_count
    metrics['total_constraints'] = total_constraints
    metrics['retained_constraints'] = retained_constraints
    metrics['constraint_violations'] = superseded_count
    metrics['constraint_retention_rate'] = retained_constraints / total_constraints if total_constraints > 0 else 1.0

    # 5. 通信边数
    message_events = [
        e for e in self.trace_recorder.events
        if e.get('event_type') == 'task.structured_message'
    ]
    metrics['communication_edges'] = len(message_events)
    metrics['structured_message_count'] = len(message_events)

    return metrics


def _maintain_runtime(self: Orchestrator) -> None:
    """Maintain memory, telemetry and checkpoints after a worker wave."""
    # Periodic context compression AFTER task execution (every 5 iterations)
    if self.iteration_count % 5 == 0:
        self._compress_context()

    # P1 Integration: Periodic memory compression (every 5 iterations)
    # Moves older entries between memory layers to keep short-term memory lean
    if self.iteration_count % 5 == 0:
        if self.memory_federation and hasattr(self.memory_federation, 'compress_memory'):
            try:
                moved_count = self.memory_federation.compress_memory()
                if moved_count > 0:
                    self.trace_recorder.record("memory.compressed", {
                        "iteration": self.iteration_count,
                        "entries_moved": moved_count,
                    })
            except Exception as e:
                logger.warning(f"Memory compression failed: {e}")

    # P1 Integration: Prune memory when it exceeds thresholds
    # Prevents unbounded memory growth during long-running orchestrations
    if self.memory_federation and hasattr(self.memory_federation, 'prune_memory'):
        try:
            mem_stats = self.memory_federation.get_statistics()
            total_mem = mem_stats.get("total_memory_count", 0)
            # Prune when total memory exceeds 500 entries
            if total_mem > 500:
                pruned = self.memory_federation.prune_memory()
                if pruned > 0:
                    self.trace_recorder.record("memory.pruned", {
                        "iteration": self.iteration_count,
                        "entries_pruned": pruned,
                        "total_before": total_mem,
                    })
        except Exception as e:
            logger.warning(f"Memory pruning failed: {e}")

    # Record topology sparsity (SEVERE-5 integration)
    if self.iteration_count % 5 == 0:
        node_count = len(self.task_graph.nodes)
        edge_count = len(self.task_graph.edges)
        self.trace_recorder.record_topology_sparsity(node_count, edge_count)

    # Save state periodically (checkpoint)
    if self.iteration_count % 5 == 0:
        self._save_checkpoint()

    # Lifecycle: cleanup completed agents periodically (every 10 iterations)
    if self.iteration_count % 10 == 0:
        try:
            removed = self.lifecycle_manager.cleanup_completed_agents(keep_hours=1)
            if removed > 0:
                self.trace_recorder.record("lifecycle.cleanup", {
                    "iteration": self.iteration_count,
                    "agents_removed": removed,
                })
        except Exception as e:
            logger.debug(f"Lifecycle cleanup failed: {e}")


def _compress_context(self: Orchestrator, force: bool = False) -> None:
    """Compress the context to stay within token budget.

    Uses the ContextManager's compression with fact preservation.
    Also uses MemoryFederation for recall (原方案书 §5.4 压缩唤醒).
    Only records compression and recall metrics when real operations occur.

    Args:
        force: If True, perform compression even when context is below
               threshold. Does NOT fabricate metrics — only real
               compression/recall events are recorded.
    """
    # Seed MemoryFederation with task graph facts before recall
    seeded_fact_count = 0
    for fact in self.task_graph.facts.values():
        try:
            self.memory_federation.add_fact(fact)
            seeded_fact_count += 1
        except Exception:
            pass  # Fact may already exist

    # Build a recall query from actual fact content so keyword matching works.
    fact_words = set()
    for fact in self.task_graph.facts.values():
        fact_words.update(fact.text.lower().split()[:5])
    for node in self.task_graph.nodes.values():
        fact_words.update(node.goal.lower().split()[:3])
        fact_words.add(node.role.lower())
    stop_words = {"the", "a", "an", "is", "to", "of", "and", "in", "for", "on", "at", "with", "be", "as"}
    query_words = [w for w in fact_words if len(w) > 2 and w not in stop_words][:10]
    recall_query = " ".join(query_words) if query_words else "fact task node"

    # Perform recall — only record real matches
    memory_recalled = self.memory_federation.recall_context(
        node_id="global",
        query=recall_query,
        budget=2000,
    )

    recalled_chars = len(str(memory_recalled)) if memory_recalled else 0
    recalled_fact_count = len(getattr(memory_recalled, 'relevant_facts', []))

    # Only record fact retrieval if the recall actually returned facts
    if recalled_fact_count > 0 or recalled_chars > 0:
        self.trace_recorder.record("facts.retrieved", {
            "node_id": None,
            "chars": recalled_chars,
            "fact_count": recalled_fact_count,
            "query": recall_query,
        })

    # Build comprehensive context text from all current state
    # This represents the full context window that would be sent to LLM
    context_parts = []

    # Add all node information (goals, status, role, constraints, acceptance)
    for node in self.task_graph.nodes.values():
        node_text = f"Task {node.node_id} [{node.status}]: {node.goal}\n"
        node_text += f"  Role: {node.role}\n"
        if node.constraints:
            node_text += f"  Constraints: {', '.join(node.constraints)}\n"
        if node.acceptance_criteria:
            node_text += f"  Acceptance: {', '.join(node.acceptance_criteria)}\n"
        if node.dependencies:
            node_text += f"  Dependencies: {', '.join(node.dependencies)}\n"
        context_parts.append(node_text)

    # Add all facts (round 9: bounded window — the durable store is the
    # source of truth; serializing an unbounded fact pile into the
    # context both forces compression every iteration and feeds the
    # capture echo loop.  Live r11: 111k polluted facts made this text
    # megabytes long.)
    fact_window = int(os.environ.get("ASTRALNEXUS_CONTEXT_FACT_WINDOW", "500"))
    all_facts = list(self.task_graph.facts.values())
    if len(all_facts) > max(0, fact_window):
        all_facts = sorted(
            all_facts,
            key=lambda f: str(getattr(f, "recorded_at", "") or ""),
        )[-max(0, fact_window):]
    fact_parts: list[str] = []
    for fact in all_facts:
        fact_parts.append(f"Fact [{fact.fact_id}]: {fact.text} (confidence: {fact.confidence}, source: {fact.source})")
    context_parts.extend(fact_parts)

    # Add all artifacts
    for artifact in self.task_graph.artifacts.values():
        context_parts.append(f"Artifact [{artifact.artifact_id}]: {artifact.description}")

    # Trace is observability data, not long-term semantic memory. Keep only
    # the recent bounded window; durable facts and constraints are recalled
    # through MemoryFederation above.
    trace_window = int(os.environ.get("ASTRALNEXUS_CONTEXT_TRACE_WINDOW", "80"))
    for event in self.trace_recorder.events[-max(0, trace_window):]:
        event_type = event.get('event_type', 'unknown')
        payload = event.get('payload', {})
        # Expand payload to string (up to 500 chars per event)
        payload_str = str(payload)[:500]
        context_parts.append(f"Event [{event_type}]: {payload_str}")

    context_text = "\n".join(context_parts)

    # Calculate tokens based on the actual context text
    current_tokens = self.context_manager.estimate_tokens(context_text)

    # Emit a minimal observable projection when the graph has no text yet.
    if current_tokens == 0 and (len(self.task_graph.nodes) > 0 or force):
        # Build a minimal context from node count and status
        minimal_parts = [
            f"Task graph has {len(self.task_graph.nodes)} nodes",
            f"Edges: {len(self.task_graph.edges)}",
            f"Iterations: {self.iteration_count}",
        ]
        for node in list(self.task_graph.nodes.values())[:5]:
            minimal_parts.append(f"{node.node_id}: {node.status}")
        context_text = "\n".join(minimal_parts)
        current_tokens = self.context_manager.estimate_tokens(context_text)

    threshold = self.context_manager.max_context_tokens * 0.8

    if current_tokens > threshold or force:
        # Need to compress
        # Determine trigger type for P1-4 metric distinction
        if force:
            trigger_type = "forced_end_of_run"
        else:
            trigger_type = "natural_threshold_exceeded"

        facts_to_preserve = []
        # Round 9: the serialized durable memories (Fact/Artifact lines)
        # must never feed back into the extractor — they are already
        # durable, and re-capturing them nests memories inside memories
        # (live r11 iter-136: 111,708 facts of which 111,525 contained
        # old fact references; one checkpoint stalled the main loop for
        # 28+ minutes in per-fact commits).
        fact_line_set = set(fact_parts)
        capture_text = "\n".join(
            part for part in context_parts
            if part not in fact_line_set
            and not part.startswith("Artifact [")
        )
        captured = self.memory_federation.capture_before_compression(
            "global", capture_text
        )
        self.trace_recorder.record("memory.precompression_capture", {
            "node_id": None,
            "facts": len(captured.relevant_facts),
            "constraints": len(captured.captured_constraints),
            "total": captured.total_captured,
        })

        # Preserve facts from completed tasks
        for node_id, node in self.task_graph.nodes.items():
            if node.status == "completed":
                facts_to_preserve.append({
                    "text": f"{node.role} completed: {node.goal}",
                    "source_node_id": node_id,
                    "confidence": 1.0,
                })

        # Compress context - pass real text for actual compression
        compressed_text = self.context_manager.compress_context(
            context_text,
            preserve_facts=facts_to_preserve[:20],
            force=force,
        )

        # Calculate actual compression (raw metrics - no artificial forcing)
        tokens_before = self.context_manager.estimate_tokens(context_text)
        tokens_after = self.context_manager.estimate_tokens(compressed_text) if compressed_text else tokens_before

        # Record raw compression event with trigger_type for P1-4
        self.trace_recorder.record("context.compressed", {
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "preserved_facts": len(facts_to_preserve),
            "memory_recalled_chars": recalled_chars,
            "compression_method": "context_truncation" if tokens_after < tokens_before else "structural_simplification",
            "trigger_type": trigger_type,
        })
    else:
        # No compression needed, record for tracking
        self.trace_recorder.record("context.compressed", {
            "tokens_before": current_tokens,
            "tokens_after": current_tokens,
            "preserved_facts": 0,
            "memory_recalled_chars": recalled_chars,
            "trigger_type": "none_below_threshold",
        })


def _persist_phase2_metrics(self: Orchestrator) -> None:
    """Persist final Phase 2 metrics for dashboard/report parity."""
    if not self.graph_store:
        return

    phase2_metrics = self._calculate_phase2_metrics()
    for name, value in phase2_metrics.items():
        if not isinstance(value, (int, float)):
            continue
        try:
            metric = self.task_graph.add_metric({
                "metric_id": f"{self.run_id}:{name}",
                "node_id": None,
                "name": name,
                "value": float(value),
                "unit": "count" if isinstance(value, int) else "ratio",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {"source": "phase2_final"},
            })
            self.graph_store.add_metric(metric, run_id=self.run_id)
        except Exception as e:
            logger.warning(f"Failed to persist phase2 metric {name}: {e}")




def _finish_run(self: Orchestrator, results: ExecutionRunResult, *, goal: str, start_time: float) -> Dict[str, Any]:
    """Project final counters, report and persist the run before releasing resources."""
    # End-of-run metrics sampling
    # Only compress if natural conditions are met (no artificial forcing)
    if self._ownership_ok():
        self._compress_context()
    self.trace_recorder.record_topology_sparsity(len(self.task_graph.nodes), len(self.task_graph.edges))

    # ================================================================
    # Formal delivery ownership
    # ================================================================
    # The application delivery service owns the real-LLM delivery chain to
    # configured client, run layered synthesis, and execute Delivery Gate.
    # The former in-orchestrator chain had no client in this lifecycle and
    # could create duplicate/stale deliverables.  Keep the run result
    # explicit until ``RunApplicationService`` completes delivery.
    state = self.runtime_state_manager.state
    state.execution_status = str(results.get("status", "failed"))
    state.delivery_status = "pending"
    state.publication_status = "not_published"
    if state.execution_status == "completed":
        state.run_status = "running"  # No terminal product status before run.delivery.
    results["execution_status"] = state.execution_status
    results["delivery_status"] = "not_run"
    results["delivery_authorized"] = False
    delivery_result = {"status": "pending_presentation", "errors": []}
    self.trace_recorder.record(
        "orchestrator.delivery_chain.deferred",
        {"owner": "AstralNexus.core.delivery_pipeline.FinalDeliveryPipeline"},
    )
    results["delivery"] = delivery_result

    self._refresh_result_counts(results)
    # Generate final report
    elapsed = time.time() - start_time
    results["elapsed_seconds"] = elapsed
    results["final_report"] = str(self.generate_final_report())
    if self._ownership_ok():
        self._persist_phase2_metrics()

    self._persist_graph_checkpoint(stage="final")
    self.runtime_observer.observe(
        goal=goal,
        phase=str(results["status"]),
        decision={"action": "run_finished", "results": dict(results)},
        stop_reason=str(results.get("stop_reason", "")),
    )

    if self._ownership_ok() and self.graph_store and hasattr(self.graph_store, "update_run_status"):
        try:
            self.graph_store.update_run_status(
                self.run_id,
                state.run_status,
            )
        except Exception as exc:
            self.trace_recorder.record("orchestrator.run_status_persist_failed", {
                "run_id": self.run_id,
                "error": str(exc),
            })

    # P1: Save final conversation state

    # Release shared runtime resources.
    self.shutdown()

    self.trace_recorder.record("orchestrator.finished", dict(results))
    return dict(results)
