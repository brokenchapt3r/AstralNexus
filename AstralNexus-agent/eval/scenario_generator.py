"""Scenario generator: creates SEED task graphs for dynamic expansion.

Key principle: Generate only a SEED (5-10 nodes) that will grow dynamically
during execution based on:
- Verification failures → spawn repair nodes
- Information gaps → spawn retrieval nodes  
- Exceptions → spawn recovery nodes
- Value routing → spawn communication nodes

The final graph may reach 100+ nodes, but they are NOT pre-built.
They emerge from event-driven dynamic topology.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from AstralNexus.core.task_graph import TaskGraph, TaskNode


def generate_seed_scenario(
    scenario: str = "research_project",
    seed: Optional[int] = None,
    node_count: Optional[int] = None,
) -> TaskGraph:
    """Generate a SEED task graph (5-10 nodes) for dynamic expansion.
    
    The seed contains:
    - Root goal node
    - Initial decomposition (3-5 subtasks)
    - A few cross-cutting concerns (verification, planning)
    
    During execution, the graph will grow to 100+ nodes via:
    - Dynamic node spawning (verification failures, info gaps, exceptions)
    - Dynamic edge creation (5 conditions: dependency/resolution/conflict/capability/value)
    - Value-based routing (V(m) = I(m) × R(m) × U(m) - C(m))
    
    Args:
        scenario: Scenario type (research_project, code_refactor, data_analysis)
        seed: Random seed for reproducibility
        node_count: Optional minimum seed size for evaluation runs. Extra
            nodes are admitted as independent seed branches and still grow
            through the normal orchestrator lifecycle.
    
    Returns:
        TaskGraph with 5-10 seed nodes (will grow dynamically)
    """
    rng = random.Random(seed)
    graph = TaskGraph()
    
    if scenario == "research_project":
        # Root goal
        root = TaskNode(
            node_id="root",
            goal="完成研究项目：分析系统性能瓶颈并提出优化方案",
            role="Planner",
            status="admitted",
            metadata={"type": "root", "phase": 0},
        )
        graph.add_node(root)
        
        # Parallel branches: T1 and T2 are independent (both depend only on root)
        # T3 depends on T1+T2 (merge point)
        # T4 and T5 are independent (both depend on T3)
        # global_verifier depends on T4+T5
        #
        # DAG structure:
        #        root
        #       /    \
        #     T1      T2      (parallel: data collection & literature review)
        #       \    /
        #        T3           (merge: analysis)
        #       /    \
        #     T4      T5      (parallel: optimization design & risk assessment)
        #       \    /
        #   global_verifier    (final verification)
        tasks = [
            ("T1", "收集系统性能数据", "Retrieval"),
            ("T2", "调研相关优化文献", "Reasoning"),
            ("T3", "分析性能瓶颈原因", "Reasoning"),
            ("T4", "设计优化方案", "Coding"),
            ("T5", "评估方案风险", "Critic"),
        ]
        
        for tid, goal, role in tasks:
            node = TaskNode(
                node_id=tid,
                goal=goal,
                role=role,
                status="admitted",
                metadata={
                    "type": "seed_task",
                    "phase": 1,
                    "required_capabilities": _get_capabilities(role),
                    "estimated_cost": rng.randint(500, 2000),
                    "priority": rng.uniform(0.6, 0.9),
                    "risk": rng.uniform(0.3, 0.7),
                },
            )
            graph.add_node(node)
        
        # Parallel-ready edges:
        graph.add_edge("root", "T1", "dependency")
        graph.add_edge("root", "T2", "dependency")  # T1, T2 ready simultaneously
        graph.add_edge("T1", "T3", "dependency")
        graph.add_edge("T2", "T3", "dependency")    # T3 waits for both
        graph.add_edge("T3", "T4", "dependency")
        graph.add_edge("T3", "T5", "dependency")    # T4, T5 ready simultaneously
        
        # Cross-cutting: global verification
        verifier = TaskNode(
            node_id="global_verifier",
            goal="全局验收：确保所有子任务满足约束",
            role="Verifier",
            status="admitted",
            metadata={
                "type": "cross_cutting",
                "phase": 2,
                "required_capabilities": ["test_generation", "evidence_check"],
                "dependencies": ["T4", "T5"],
            },
        )
        graph.add_node(verifier)
        graph.add_edge("T4", "global_verifier", "dependency")
        graph.add_edge("T5", "global_verifier", "dependency")
        
    elif scenario == "code_refactor":
        root = TaskNode(
            node_id="root",
            goal="重构遗留代码模块，提升可维护性",
            role="Planner",
            status="admitted",
        )
        graph.add_node(root)
        
        # DAG: parallel analysis first, then parallel design+scaffold, then impl+test
        #        root
        #       /    \
        #     T1      T2      (parallel: structure analysis & dependency analysis)
        #       \    /
        #       /    \
        #     T3      T4      (parallel: refactor plan & test scaffold)
        #       \    /
        #        T5           (implementation + verification)
        tasks = [
            ("T1", "分析现有代码结构", "Reasoning"),
            ("T2", "分析模块间依赖关系", "Retrieval"),
            ("T3", "设计重构方案", "Coding"),
            ("T4", "搭建测试脚手架", "Verifier"),
            ("T5", "实现重构并验证", "Coding"),
        ]
        
        for tid, goal, role in tasks:
            node = TaskNode(
                node_id=tid,
                goal=goal,
                role=role,
                status="admitted",
                metadata={
                    "type": "seed_task",
                    "required_capabilities": _get_capabilities(role),
                    "estimated_cost": rng.randint(800, 3000),
                },
            )
            graph.add_node(node)
        
        graph.add_edge("root", "T1", "dependency")
        graph.add_edge("root", "T2", "dependency")    # T1, T2 ready simultaneously
        graph.add_edge("T1", "T3", "dependency")
        graph.add_edge("T1", "T4", "dependency")      # T3, T4 ready simultaneously (after T1)
        graph.add_edge("T2", "T3", "dependency")      # T3 also needs dependency info
        graph.add_edge("T3", "T5", "dependency")
        graph.add_edge("T4", "T5", "dependency")      # T5 waits for both plan + scaffold
    
    elif scenario == "data_analysis":
        root = TaskNode(
            node_id="root",
            goal="分析销售数据，生成洞察报告",
            role="Planner",
            status="admitted",
        )
        graph.add_node(root)
        
        # DAG: parallel data fetch + template setup, then cleaning,
        # then parallel analysis + viz, then report
        #        root
        #       /    \
        #     T1      T2      (parallel: data retrieval & report template)
        #       \    /
        #        T3           (data cleaning)
        #       /    \
        #     T4      T5      (parallel: statistical analysis & visualization)
        #       \    /
        #        T6           (report generation)
        tasks = [
            ("T1", "获取销售数据", "Retrieval"),
            ("T2", "准备报告模板", "Coding"),
            ("T3", "数据清洗与预处理", "Tool"),
            ("T4", "统计分析", "Reasoning"),
            ("T5", "数据可视化", "Vision"),
            ("T6", "生成报告", "Coding"),
        ]
        
        for tid, goal, role in tasks:
            node = TaskNode(
                node_id=tid,
                goal=goal,
                role=role,
                status="admitted",
                metadata={
                    "type": "seed_task",
                    "required_capabilities": _get_capabilities(role),
                    "estimated_cost": rng.randint(600, 1500),
                },
            )
            graph.add_node(node)
        
        graph.add_edge("root", "T1", "dependency")
        graph.add_edge("root", "T2", "dependency")    # T1, T2 parallel
        graph.add_edge("T1", "T3", "dependency")
        graph.add_edge("T2", "T3", "dependency")      # T3 waits for both
        graph.add_edge("T3", "T4", "dependency")
        graph.add_edge("T3", "T5", "dependency")      # T4, T5 parallel
        graph.add_edge("T4", "T6", "dependency")
        graph.add_edge("T5", "T6", "dependency")      # T6 waits for both
    
    else:
        raise ValueError(f"Unknown scenario: {scenario}")
    
    # ``node_count=0`` is the production dynamic-seed mode.  It deliberately
    # returns only the user-goal root; the first child wave must come from the
    # real LLM planner and Governor, not from this fixture.
    if node_count == 0:
        root = graph.get_node("root")
        dynamic_graph = TaskGraph()
        if root:
            dynamic_graph.add_node(root)
        return dynamic_graph

    if node_count is not None and node_count > len(graph.nodes):
        roles = ["researcher", "coder", "reviewer", "verifier", "resource_broker", "recovery_agent"]
        for index in range(len(graph.nodes) + 1, node_count + 1):
            node_id = f"seed-{index:03d}"
            role = roles[(index - 1) % len(roles)]
            graph.add_node(TaskNode(
                node_id=node_id,
                goal=f"完成评测场景的独立子任务 {index}",
                role=role,
                status="admitted",
                metadata={
                    "type": "evaluation_seed_extension",
                    "required_capabilities": _get_capabilities(role),
                    "estimated_cost": rng.randint(600, 1500),
                },
            ))
            graph.add_edge("root", node_id, "dependency")

    return graph


def _get_capabilities(role: str) -> List[str]:
    """Get required capabilities for a role."""
    capability_map = {
        "Planner": ["task_decomposition", "goal_setting", "constraint_analysis"],
        "Reasoning": ["logical_reasoning", "analysis", "comparison"],
        "Coding": ["code_generation", "debugging", "refactoring"],
        "Retrieval": ["web_search", "document_retrieval", "database_query"],
        "Tool": ["python_execution", "shell_commands", "api_calls"],
        "Verifier": ["test_generation", "evidence_check", "validation"],
        "Critic": ["counter_example", "risk_identification", "vulnerability_scan"],
        "Vision": ["image_analysis", "ui_inspection", "document_ocr"],
        "Device": ["terminal_control", "automation", "device_operation"],
        "Recovery": ["error_recovery", "plan_repair", "exception_handling"],
    }
    return capability_map.get(role, ["general_task"])

