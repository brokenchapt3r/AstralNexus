#!/usr/bin/env python3
"""
AstralNexus Demo Runner — 演示环境入口
======================================

正式产品入口只允许真实 LLM + 工具调用 + 结构化输出 + 图通信链路。
模型、Provider 和 API 凭证由 AstralNexus 统一配置读取；配置缺失或调用失败时直接失败并记录原因。

用法:
    # 正式演示 (需配置 LLM 凭证)
    python demo_runner.py --goal "研究量子计算的最新进展并撰写报告"

环境变量:
    ASTRALNEXUS_MODEL              指定模型 (默认: 从 runtime config 读取)
    ASTRALNEXUS_PROVIDER           指定 provider (默认: 从 runtime config 读取)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def validate_environment() -> dict:
    """Validate that the runtime environment is ready for real LLM execution."""
    status = {
        "mode": "real_llm_required",
        "model": os.getenv("ASTRALNEXUS_MODEL", "default"),
        "provider": os.getenv("ASTRALNEXUS_PROVIDER", "default"),
        "warnings": [],
    }
    
    from AstralNexus.runtime.model_selector import get_model_config
    model_config = get_model_config()
    status["model"] = model_config["model"]
    status["provider"] = model_config["provider"]
    if not model_config.get("api_key_env"):
        status["warnings"].append("未检测到真实 LLM API 凭证，请先完成 AstralNexus 模型配置。")
    
    return status


def run_demo(goal: str, constraints: list[str], run_dir: str | None = None):
    """Execute a full demo run with all AstralNexus subsystems."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
    
    # Set up run directory
    if run_dir:
        rd = Path(run_dir)
    else:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        rd = Path(f"demo-runs/demo-{timestamp}")
    rd.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{'='*70}")
    print(f"  天枢 AstralNexus — 超长程复杂任务动态异构群体智能协同推理引擎")
    print(f"{'='*70}")
    print("\n  运行模式: 🟢 真实 LLM 智能链路（强制）")
    print(f"  目标: {goal}")
    if constraints:
        print(f"  约束: {', '.join(constraints)}")
    print(f"  运行目录: {rd.absolute()}")
    print()
    
    # Initialize persistence
    db_path = rd / "task_graph.db"
    graph_store = SQLiteTaskGraphStore(str(db_path))
    
    # Initialize orchestrator
    orchestrator = Orchestrator(
        run_dir=rd,
        graph_store=graph_store,
        max_iterations=50,
        max_parallel=4,
    )
    
    # Use the canonical orchestrator loop so dependency waves, routing,
    # verification, recovery, and dynamic admission are all observable.
    results = orchestrator.run(goal=goal, constraints=constraints)
    subtasks = list(orchestrator.task_graph.nodes.values())
    iteration = results.get("iterations", 0)
    completed = results.get("completed_tasks", 0)
    failed = results.get("failed_tasks", 0)
    
    # Generate final report
    print(f"\n{'='*70}")
    print(f"  执行结果")
    print(f"{'='*70}")
    print(f"  迭代次数: {iteration}")
    print(f"  完成任务: {completed}")
    print(f"  失败任务: {failed}")
    print(f"  总任务数: {len(subtasks)}")
    
    # Generate delivery package
    try:
        from AstralNexus.demo.delivery_package import generate_delivery_package
        pkg_dir = generate_delivery_package(orchestrator, rd)
        print(f"\n  📦 交付包已生成: {pkg_dir}")
        for f in sorted(pkg_dir.iterdir()):
            size = f.stat().st_size
            print(f"    {f.name:40s} {size:>8,d} bytes")
    except ImportError:
        print(f"\n  📦 交付包模块不可用，正式运行终止")
        raise
    
    print(f"\n{'='*70}\n")
    return orchestrator


def main():
    parser = argparse.ArgumentParser(
        description="天枢 AstralNexus 演示运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--goal", "-g", required=True, help="任务目标")
    parser.add_argument("--constraint", "-c", action="append", default=[], help="约束条件 (可多次指定)")
    parser.add_argument("--run-dir", help="运行目录 (默认: demo-runs/demo-<timestamp>)")
    parser.add_argument("--validate-only", action="store_true", help="仅验证环境配置，不执行任务")
    
    args = parser.parse_args()
    
    # Validate environment
    env_status = validate_environment()
    
    print(f"\n  环境验证:")
    print(f"    模式: {env_status['mode']}")
    print(f"    模型: {env_status['model']}")
    print(f"    Provider: {env_status['provider']}")
    
    if env_status.get("api_key"):
        print(f"    API Key: {env_status['api_key']}")
    
    if env_status["warnings"]:
        for w in env_status["warnings"]:
            print(f"    {w}")
        print("\n  ❌ 正式运行需要真实 LLM API 凭证。")
        sys.exit(1)
    
    if args.validate_only:
        print(f"\n  ✅ 环境验证通过" if not env_status["warnings"] else f"\n  ⚠️ 环境有问题")
        sys.exit(0 if not env_status["warnings"] else 1)
    
    # Run the demo
    run_demo(args.goal, args.constraint, args.run_dir)


if __name__ == "__main__":
    main()
