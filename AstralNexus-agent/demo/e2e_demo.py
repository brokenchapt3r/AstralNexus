#!/usr/bin/env python3
"""
AstralNexus 端到端完整演示脚本
================================

展示所有核心子系统协同工作：
1. 真实 LLM 智能链路（默认模式）
2. 多因子路由器（语义+依赖+角色+历史）
3. 低熵通信协议（状态验证）
4. 角色执行器（生产链路保护）
5. 故障注入与恢复
6. 完整交付包生成

用法:
    python e2e_demo.py --goal "设计并实现一个推荐系统"
    python e2e_demo.py --goal "分析气候变化数据并生成可视化报告" --constraints "数据量<10GB,实时处理"
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TypedDict


class EnvironmentStatus(TypedDict, total=False):
    mode: str
    model: str
    provider: str
    warnings: list[str]
from datetime import datetime

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def print_banner():
    """Print demo banner."""
    print("\n" + "=" * 80)
    print("  天枢 AstralNexus — 超长程复杂任务动态异构群体智能协同推理引擎")
    print("  端到端完整演示")
    print("=" * 80 + "\n")


def print_section(title: str):
    """Print section header."""
    print(f"\n{'─' * 80}")
    print(f"  {title}")
    print(f"{'─' * 80}\n")


def validate_environment() -> dict:
    """Validate runtime environment."""
    status: EnvironmentStatus = {
        "mode": "real_llm_required",
        "warnings": [],
    }
    
    from AstralNexus.runtime.model_selector import get_model_config
    model_config = get_model_config()
    status["model"] = model_config["model"]
    status["provider"] = model_config["provider"]
    if not model_config.get("api_key_env"):
        status["warnings"].append("未检测到真实 LLM API 凭证，请先完成 AstralNexus 模型配置。")
    
    return dict(status)


def demonstrate_multifactor_router(orchestrator):
    """展示多因子路由器。"""
    print_section("📡 多因子路由器演示")
    
    router = orchestrator.router
    
    # 1. 语义相似度边
    print("1. 语义相似度边 (Semantic Similarity Edges):")
    node_ids = list(orchestrator.task_graph.nodes.keys())[:10]
    semantic_edges = router.compute_semantic_edges(node_ids, threshold=0.3)
    print(f"   - 检测到 {len(semantic_edges)} 条语义相关边")
    for src, dst, score in semantic_edges[:3]:
        print(f"   - {src} ↔ {dst} (相似度: {score:.3f})")
    
    # 2. 多因子综合边
    print("\n2. 多因子综合边 (Multifactor Edges):")
    multifactor_edges = router.compute_multifactor_edges(
        node_ids,
        w_semantic=0.4,
        w_dependency=0.3,
        w_role=0.2,
        w_history=0.1
    )
    print(f"   - 生成 {len(multifactor_edges)} 条多因子加权边")
    for src, dst, score in multifactor_edges[:3]:
        print(f"   - {src} ↔ {dst} (综合得分: {score:.3f})")
    
    # 3. 稀疏拓扑
    print("\n3. 稀疏拓扑 (Sparse Topology):")
    topology = router.refresh_sparse_topology()
    print(f"   - 活跃边数: {topology['active_edges']}")
    print(f"   - 平均度: {topology['avg_degree']:.2f}")
    print(f"   - 稀疏度: {topology['sparsity']:.3f}")
    
    return {
        "semantic_edges": len(semantic_edges),
        "multifactor_edges": len(multifactor_edges),
        "topology": topology
    }


def demonstrate_message_protocol(orchestrator):
    """展示低熵通信协议。"""
    print_section("💬 低熵通信协议演示")
    
    from AstralNexus.core.message_protocol import MessageProtocol, Message
    
    protocol = MessageProtocol(
        task_graph=orchestrator.task_graph,
        store=orchestrator.store
    )
    
    # 创建测试消息
    test_msg = Message(
        from_node="node_planner_1",
        to_node="node_coder_1",
        message_type="instruction",
        content="请实现用户认证模块",
        metadata={"priority": "high"}
    )
    
    print("1. 消息创建:")
    print(f"   - From: {test_msg.from_node}")
    print(f"   - To: {test_msg.to_node}")
    print(f"   - Type: {test_msg.message_type}")
    
    # 验证消息
    print("\n2. 消息验证 (State Validation):")
    is_valid, errors = protocol.validate_message(test_msg)
    print(f"   - 验证结果: {'✓ 有效' if is_valid else '✗ 无效'}")
    if errors:
        for err in errors:
            print(f"   - 错误: {err}")
    
    # 压缩消息
    print("\n3. 消息压缩 (Entropy Reduction):")
    compressed = protocol.compress_message(test_msg)
    original_size = len(json.dumps(test_msg.__dict__))
    compressed_size = len(json.dumps(compressed))
    reduction = (1 - compressed_size / original_size) * 100
    print(f"   - 原始大小: {original_size} bytes")
    print(f"   - 压缩后: {compressed_size} bytes")
    print(f"   - 压缩率: {reduction:.1f}%")
    
    return {
        "valid": is_valid,
        "compression_ratio": reduction
    }


def demonstrate_role_executor(orchestrator):
    """展示角色执行器（生产链路保护）。"""
    print_section("🎭 角色执行器演示")
    
    # Factory lookup is the production registry boundary. Execution itself
    # belongs to the Governor-approved graph run above, not a second executor.
    registry = orchestrator.agent_registry
    unknown = registry.create_agent("nonexistent_expert")
    if unknown is not None:
        raise RuntimeError("Unregistered role unexpectedly resolved to a worker")
    planner = registry.create_agent("planner")
    if planner is None:
        raise RuntimeError("Planner role has no registered production factory")
    print("Unregistered role rejected; planner factory is available.")
    print("Worker execution is owned by the Governor-approved task graph.")
    return {"fake_role_tested": True, "planner_registered": True}


def generate_complete_delivery_package(orchestrator, run_dir: Path):
    """生成完整交付包。"""
    print_section("📦 交付包生成")
    
    from AstralNexus.demo.delivery_package import generate_delivery_package
    
    pkg_dir = generate_delivery_package(orchestrator, run_dir)
    
    print(f"交付包路径: {pkg_dir}")
    print("\n包含文件:")
    
    files = [
        ("trace.jsonl", "完整执行轨迹"),
        ("task_graph.json", "任务图快照"),
        ("communications.json", "角色通信记录"),
        ("metrics_report.json", "指标报告"),
        ("fault_recovery_report.json", "故障恢复报告"),
        ("summary.md", "人类可读摘要")
    ]
    
    for filename, description in files:
        filepath = pkg_dir / filename
        if filepath.exists():
            size = filepath.stat().st_size
            print(f"  ✓ {filename:<35} ({size:>6} bytes) - {description}")
        else:
            print(f"  ✗ {filename:<35} - 未生成")
    
    return pkg_dir


def main():
    parser = argparse.ArgumentParser(description="AstralNexus 端到端完整演示")
    parser.add_argument("--goal", required=True, help="任务目标描述")
    parser.add_argument("--constraints", nargs="*", default=[], help="任务约束列表")
    parser.add_argument("--output", default=None, help="输出目录")
    
    args = parser.parse_args()
    
    # 打印 banner
    print_banner()
    
    # 验证环境
    env_status = validate_environment()
    print(f"运行模式: {env_status['mode']}")
    
    if env_status["warnings"]:
        for warning in env_status["warnings"]:
            print(warning)
        print("❌ 正式运行需要真实 LLM API 凭证。")
        sys.exit(1)
    
    # 设置输出目录
    if args.output:
        run_dir = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path(f"demo_runs/e2e_demo_{timestamp}")
    
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {run_dir}")
    
    # 创建编排器
    print_section("🚀 系统初始化")
    
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskGraph
    from AstralNexus.storage.sqlite_store import SQLiteStore
    
    task_graph = TaskGraph()
    store = SQLiteStore(run_dir / "astralnexus.db")
    
    orchestrator = Orchestrator(
        task_graph=task_graph,
        store=store,
        config={"max_concurrent_tasks": 5}
    )
    
    print(f"✓ Orchestrator 初始化完成")
    print(f"✓ 任务图: {len(task_graph.nodes)} 节点")
    print(f"✓ 存储: SQLite @ {store.db_path}")
    
    # 执行演示
    results = {}
    
    try:
        # 1. 多因子路由器
        results["router"] = demonstrate_multifactor_router(orchestrator)
        
        # 2. 低熵通信协议
        results["message_protocol"] = demonstrate_message_protocol(orchestrator)
        
        # 3. 角色执行器
        results["role_executor"] = demonstrate_role_executor(orchestrator)
        
        # 4. 故障恢复
        
        # 5. 生成完整交付包
        pkg_dir = generate_complete_delivery_package(orchestrator, run_dir)
        results["delivery_package"] = {"path": str(pkg_dir)}
        
    except Exception as e:
        print(f"\n❌ 演示过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # 保存结果摘要
    summary_file = run_dir / "demo_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "goal": args.goal,
            "constraints": args.constraints,
            "mode": env_status["mode"],
            "results": results
        }, f, ensure_ascii=False, indent=2)
    
    print_section("✅ 演示完成")
    print(f"结果摘要: {summary_file}")
    print(f"交付包: {results.get('delivery_package', {}).get('path', 'N/A')}")
    print()


if __name__ == "__main__":
    main()
