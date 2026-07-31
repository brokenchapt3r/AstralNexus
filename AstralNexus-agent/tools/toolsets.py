"""Toolset definitions for AstralNexus (天枢).

Each toolset is a dict with ``description``, ``tools`` (direct tool list),
and ``includes`` (other toolsets to compose). Role-based toolsets are
defined here so the
router can restrict each agent to the tools appropriate for its role.

Usage::

    from AstralNexus.tools.toolsets import resolve_toolset, get_toolset_info

    tools = resolve_toolset("planner")          # -> ["read_file", "search_files", ...]
    info  = get_toolset_info("coder")           # -> {"name": "coder", "tools": [...], ...}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Role-based toolsets  (per 天枢方案书 §5 roles)
# ---------------------------------------------------------------------------

TOOLSETS: Dict[str, Dict[str, Any]] = {
    # --- Core agent role toolsets ------------------------------------------

    "planner": {
        "description": "任务规划者工具集 — 任务拆解、依赖规划、验收标准生成",
        "tools": ["file_read", "search_files", "find_skills", "skills_list", "skill_view"],
        "includes": [],
    },

    "researcher": {
        "description": "研究员工具集 — 资料检索、文档阅读、证据抽取",
        "tools": ["web_search", "file_read", "http_get",
                   "openalex_search", "crossref_search", "arxiv_search",
                   "github_repo_contents", "file_read", "browser_fetch", "skills_list", "skill_view"],
        "includes": [],
    },

    "coder": {
        "description": "编码员工具集 — 代码修改、脚本编写、修复实现",
        "tools": ["file_write", "terminal_exec", "search_files", "file_read",
                  "python_exec", "pytest_run", "github_repo_contents",
                  "skills_list", "skill_view"],
        "includes": [],
    },

    "verifier": {
        "description": "验证员工具集 — 测试执行、结果校验、产物验收",
        "tools": ["terminal_exec", "search_files", "file_read", "python_exec", "pytest_run",
                  "skills_list", "skill_view"],
        "includes": [],
    },

    "reviewer": {
        "description": "审查员工具集 — 风险审查、接口兼容性、证据一致性检查",
        "tools": ["file_read", "search_files", "github_repo_contents", "skills_list", "skill_view"],
        "includes": [],
    },

    "memory_curator": {
        "description": "记忆管理员工具集 — 事实沉淀、冲突消解、记忆唤醒",
        "tools": ["file_read", "search_files", "skills_list", "skill_view"],
        "includes": [],
    },

    "resource_broker": {
        "description": "资源经纪人工具集 — 推理位置和模型选择",
        "tools": ["find_skills", "skills_list", "skill_view"],
        "includes": [],
    },

    "recovery_agent": {
        "description": "恢复分析工具集 — 故障诊断、影响证据、恢复选项",
        "tools": ["file_read", "file_write", "search_files", "terminal_exec",
                  "python_exec", "pytest_run", "skills_list", "skill_view"],
        "includes": [],
    },

    "skill_curator": {
        "description": "Skill 策展工具集 — 搜索、审计并在 Governor 批准后安装外部 Skill",
        "tools": ["find_skills", "skills_list", "skill_view", "skill_list", "skill_audit", "skill_install", "skill_update", "skill_enable", "skill_disable"],
        "includes": [],
    },

    "writer": {
        "description": "文档撰写工具集 — 报告生成、文档输出",
        "tools": ["file_write", "file_read"],
        "includes": [],
    },

    # --- Composite / scenario toolsets ------------------------------------

    "base": {
        "description": "基础工具集 — 所有角色共享的最小工具集",
        "tools": ["file_read", "search_files"],
        "includes": [],
    },

    "software_engineering": {
        "description": "软件工程场景工具集 — 编码、测试、终端、文件操作",
        "tools": [],
        "includes": ["coder", "verifier", "base"],
    },

    "research_report": {
        "description": "研究报告场景工具集 — 检索、分析、文档生成",
        "tools": [],
        "includes": ["researcher", "writer", "base"],
    },

    "full": {
        "description": "完整工具集 — 包含所有可用工具",
        "tools": [],
        "includes": ["planner", "researcher", "coder", "verifier",
                     "reviewer", "memory_curator", "resource_broker",
                     "recovery_agent", "writer", "skill_curator"],
    },
}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def get_toolset(name: str) -> Optional[Dict[str, Any]]:
    """Get a toolset definition by name."""
    return TOOLSETS.get(name)


def resolve_toolset(name: str, visited: Optional[Set[str]] = None) -> List[str]:
    """Recursively resolve a toolset to get all tool names (deduplicated)."""
    if visited is None:
        visited = set()

    if name in {"all", "*"}:
        all_tools: Set[str] = set()
        for ts_name in TOOLSETS:
            all_tools.update(resolve_toolset(ts_name, visited.copy()))
        return sorted(all_tools)

    if name in visited:
        return []
    visited.add(name)

    toolset = get_toolset(name)
    if not toolset:
        return []

    tools = set(toolset.get("tools", []))
    for included_name in toolset.get("includes", []):
        tools.update(resolve_toolset(included_name, visited))

    return sorted(tools)


def resolve_multiple_toolsets(toolset_names: List[str]) -> List[str]:
    """Resolve multiple toolsets and combine (deduplicated)."""
    all_tools: Set[str] = set()
    for name in toolset_names:
        all_tools.update(resolve_toolset(name))
    return sorted(all_tools)


def get_toolset_info(name: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a toolset."""
    toolset = get_toolset(name)
    if not toolset:
        return None
    resolved = resolve_toolset(name)
    return {
        "name": name,
        "description": toolset["description"],
        "direct_tools": toolset["tools"],
        "includes": toolset["includes"],
        "resolved_tools": resolved,
        "tool_count": len(resolved),
        "is_composite": bool(toolset["includes"]),
    }


def get_all_toolsets() -> Dict[str, Dict[str, Any]]:
    """Return all defined toolsets."""
    return dict(TOOLSETS)


def get_toolset_names() -> List[str]:
    """Return sorted list of all toolset names."""
    return sorted(TOOLSETS.keys())


def create_custom_toolset(
    name: str,
    description: str,
    tools: Optional[List[str]] = None,
    includes: Optional[List[str]] = None,
) -> None:
    """Register a custom toolset at runtime."""
    TOOLSETS[name] = {
        "description": description,
        "tools": tools or [],
        "includes": includes or [],
    }


def validate_toolset(name: str) -> bool:
    """Check if a toolset name is valid."""
    return name in {"all", "*"} or name in TOOLSETS
