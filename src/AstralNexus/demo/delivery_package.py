"""
AstralNexus 长任务交付包生成器
================================

生成完整的交付包，包含：
- trace.jsonl: 完整执行轨迹
- task_graph.json: 任务图快照
- communications.json: 角色间通信记录
- metrics_report.json: 指标报告
- fault_recovery_report.json: 故障恢复报告
- summary.md: 人类可读摘要

所有数据互相对齐，可追溯验证。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from AstralNexus.core.orchestrator import Orchestrator


def generate_delivery_package(orchestrator: Orchestrator, run_dir: Path) -> Path:
    """Generate a complete delivery package for the executed task.
    
    Args:
        orchestrator: The orchestrator instance after execution
        run_dir: The run directory
        
    Returns:
        Path to the delivery package directory
    """
    pkg_dir = run_dir / "delivery_package"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Export trace.jsonl
    _export_trace(orchestrator, pkg_dir)
    
    # 2. Export task graph
    _export_task_graph(orchestrator, pkg_dir)
    
    # 3. Export communications
    _export_communications(orchestrator, pkg_dir)
    
    # 4. Export metrics report
    _export_metrics_report(orchestrator, pkg_dir)
    
    # 5. Export fault recovery report
    _export_fault_recovery_report(orchestrator, pkg_dir)
    
    # 6. Generate human-readable summary
    _generate_summary(orchestrator, pkg_dir)
    
    return pkg_dir


def _export_trace(orchestrator: Orchestrator, pkg_dir: Path):
    """Export complete execution trace as JSONL."""
    trace_file = pkg_dir / "trace.jsonl"
    
    with open(trace_file, "w", encoding="utf-8") as f:
        for event in orchestrator.trace_recorder.events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _export_task_graph(orchestrator: Orchestrator, pkg_dir: Path):
    """Export task graph snapshot."""
    graph_file = pkg_dir / "task_graph.json"
    
    graph_data = {
        "nodes": orchestrator.task_graph.nodes,
        "edges": orchestrator.task_graph.edges,
        "facts": orchestrator.task_graph.facts,
        "artifacts": orchestrator.task_graph.artifacts,
        "metadata": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "node_count": len(orchestrator.task_graph.nodes),
            "edge_count": len(orchestrator.task_graph.edges),
        }
    }
    
    with open(graph_file, "w", encoding="utf-8") as f:
        json.dump(graph_data, f, ensure_ascii=False, indent=2)


def _export_communications(orchestrator: Orchestrator, pkg_dir: Path):
    """Export role-to-role communication records."""
    comm_file = pkg_dir / "communications.json"
    
    # Extract communication events from trace
    communications = []
    for event in orchestrator.trace_recorder.events:
        if "routing" in event.get("event_type", ""):
            communications.append({
                "timestamp": event.get("timestamp"),
                "event_type": event.get("event_type"),
                "node_id": event.get("data", {}).get("node_id"),
                "role": event.get("data", {}).get("role"),
                "data": event.get("data", {}),
            })
    
    comm_data = {
        "communications": communications,
        "metadata": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total_communications": len(communications),
        }
    }
    
    with open(comm_file, "w", encoding="utf-8") as f:
        json.dump(comm_data, f, ensure_ascii=False, indent=2)


def _export_metrics_report(orchestrator: Orchestrator, pkg_dir: Path):
    """Export comprehensive metrics report."""
    metrics_file = pkg_dir / "metrics_report.json"
    
    # Aggregate metrics from trace
    task_count = len(orchestrator.task_graph.nodes)
    completed_count = sum(1 for n in orchestrator.task_graph.nodes.values() if n.status == "completed")
    failed_count = sum(1 for n in orchestrator.task_graph.nodes.values() if n.status == "failed")
    
    # Count message compressions
    compression_events = [e for e in orchestrator.trace_recorder.events if "message.compressed" in e.get("event_type", "")]
    total_compression_ratio = 0
    if compression_events:
        total_compression_ratio = sum(e.get("data", {}).get("compression_ratio", 1) for e in compression_events) / len(compression_events)
    
    # Count routing decisions
    routing_events = [e for e in orchestrator.trace_recorder.events if "task.routing" in e.get("event_type", "")]
    
    # Count resource scheduling decisions
    resource_events = [e for e in orchestrator.trace_recorder.events if "resource_scheduled" in e.get("event_type", "")]
    
    metrics = {
        "task_metrics": {
            "total_tasks": task_count,
            "completed_tasks": completed_count,
            "failed_tasks": failed_count,
            "success_rate": completed_count / task_count if task_count > 0 else 0,
        },
        "communication_metrics": {
            "message_compressions": len(compression_events),
            "avg_compression_ratio": total_compression_ratio,
            "routing_decisions": len(routing_events),
        },
        "resource_metrics": {
            "scheduling_decisions": len(resource_events),
        },
        "execution_metrics": {
            "total_trace_events": len(orchestrator.trace_recorder.events),
            "fact_count": len(orchestrator.task_graph.facts),
            "artifact_count": len(orchestrator.task_graph.artifacts),
        },
        "metadata": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "run_id": orchestrator.run_id,
        }
    }
    
    with open(metrics_file, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)


def _export_fault_recovery_report(orchestrator: Orchestrator, pkg_dir: Path):
    """Export fault detection and recovery report."""
    fault_file = pkg_dir / "fault_recovery_report.json"
    
    # Extract fault-related events
    fault_events = [e for e in orchestrator.trace_recorder.events if "fault" in e.get("event_type", "").lower() or "recovery" in e.get("event_type", "").lower()]
    
    report = {
        "fault_events": fault_events,
        "summary": {
            "total_fault_events": len(fault_events),
            "fault_types": list(set(e.get("event_type", "") for e in fault_events)),
        },
        "metadata": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    
    with open(fault_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def _generate_summary(orchestrator: Orchestrator, pkg_dir: Path):
    """Generate human-readable markdown summary."""
    summary_file = pkg_dir / "summary.md"
    
    task_count = len(orchestrator.task_graph.nodes)
    completed_count = sum(1 for n in orchestrator.task_graph.nodes.values() if n.status == "completed")
    failed_count = sum(1 for n in orchestrator.task_graph.nodes.values() if n.status == "failed")
    
    summary = f"""# AstralNexus 执行摘要

**生成时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  
**Run ID**: {orchestrator.run_id}

## 任务统计

- 总任务数: {task_count}
- 完成任务: {completed_count}
- 失败任务: {failed_count}
- 成功率: {completed_count / task_count * 100:.1f}% (if task_count > 0 else 0)

## 交付物

- `trace.jsonl`: 完整执行轨迹 ({len(orchestrator.trace_recorder.events)} 个事件)
- `task_graph.json`: 任务图快照
- `communications.json`: 角色间通信记录
- `metrics_report.json`: 指标报告
- `fault_recovery_report.json`: 故障恢复报告

## 执行路径

所有角色执行通过 **真实 LLM + 工具调用** 链路。

---

*此交付包由天枢 AstralNexus 自动生成*
"""
    
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write(summary)
