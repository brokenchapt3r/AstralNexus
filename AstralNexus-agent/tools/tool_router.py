"""Role- and goal-aware tool selection for worker requests.

Tool schemas are part of the model input budget.  This router keeps the
registered role toolset as the hard allow-list, then applies a lightweight
semantic relevance filter to avoid broadcasting every available tool.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from AstralNexus.tools.registry import registry
from AstralNexus.tools.toolsets import resolve_toolset


_TOOL_HINTS = {
    "read": {"read", "file", "文献", "论文", "读取", "查看", "分析"},
    "search": {"search", "web", "github", "检索", "搜索", "资料", "趋势"},
    "write": {"write", "edit", "patch", "file", "修改", "写入", "实现"},
    "test": {"test", "pytest", "run", "验证", "测试", "验收"},
    "graph": {"task", "graph", "依赖", "节点", "任务图", "拆解"},
    "skill": {"skill", "能力", "工具", "安装"},
    "fault": {"fault", "recovery", "failure", "故障", "恢复", "失败"},
    "memory": {"memory", "fact", "artifact", "记忆", "事实", "产物"},
    "resource": {"resource", "model", "cost", "资源", "模型", "调度"},
}

_TOOL_ALTERNATIVES = {
    "openalex_search": ["crossref_search", "arxiv_search", "web_search"],
    "crossref_search": ["openalex_search", "arxiv_search", "web_search"],
    "arxiv_search": ["openalex_search", "crossref_search", "web_search"],
    "web_search": ["http_get", "browser_fetch"],
    "browser_fetch": ["http_get", "web_search"],
    "pytest_run": ["terminal_exec", "python_exec"],
    "python_exec": ["terminal_exec"],
    "file_read": ["search_files"],
}


def _words(text: str) -> set[str]:
    return {item.lower() for item in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}", text or "")}


def alternative_tools(tool_name: str, candidates: Iterable[str] | None = None) -> List[str]:
    """Return registered alternatives, preserving the caller's allow-list."""
    available = set(registry.get_all_tool_names())
    allowed = set(candidates) if candidates is not None else available
    return [name for name in _TOOL_ALTERNATIVES.get(tool_name, []) if name in available and name in allowed]


def discover_tools(
    role: str,
    goal: str,
    candidates: Iterable[str] | None = None,
    *,
    required_tools: Iterable[str] | None = None,
    max_tools: int | None = None,
    schema_budget: Optional[int] = None,
    governor_approval: bool = False,
) -> List[Dict[str, Any]]:
    """Return an auditable, budgeted tool capability catalogue.

    This is the Kimi-style discovery boundary: selection happens before tool
    schemas are injected into an LLM request.  Unregistered or disallowed
    names never appear in the result.
    
    Args:
        role: Worker role for toolset resolution
        goal: Task goal for semantic relevance scoring
        candidates: Optional allow-list of tool names
        max_tools: Maximum number of tools to return
        schema_budget: Optional token budget for tool schemas. If provided,
            tools will be filtered to fit within this budget.
        governor_approval: If True, marks high-risk tools as requiring
            Governor approval before execution.
    
    Returns:
        List of tool capability dictionaries with metadata
    """
    names = select_tools(
        role,
        goal,
        candidates,
        required_tools=required_tools,
        max_tools=max_tools,
    )
    
    # Apply schema budget filtering if specified
    if schema_budget is not None:
        names = filter_by_schema_budget(names, schema_budget)
    
    discovered: List[Dict[str, Any]] = []
    for name in names:
        entry = registry.get_entry(name)
        if entry is None:
            continue
        schema = dict(entry.schema or {})
        
        # Determine if Governor approval is needed
        needs_approval = entry.requires_approval
        if governor_approval and needs_approval:
            approval_status = "pending_governor_approval"
        else:
            approval_status = "auto_approved" if not needs_approval else "approval_required"
        
        discovered.append({
            "name": name,
            "capabilities": [entry.toolset],
            "permissions": ["approval_required"] if entry.requires_approval else ["read_only"],
            "input_schema": schema.get("parameters", {}),
            "output_schema": schema.get("returns", {}),
            "source": "registry",
            "installed": True,
            "risk_level": "high" if entry.requires_approval else "low",
            "approval_status": approval_status,
            "schema_tokens": estimate_schema_tokens(name),
        })
    return discovered


def select_tools(
    role: str,
    goal: str,
    candidates: Iterable[str] | None = None,
    *,
    required_tools: Iterable[str] | None = None,
    max_tools: int | None = None,
) -> List[str]:
    """Return the smallest useful registered tool set for a worker."""
    role_allow = set(resolve_toolset(role))
    available = set(registry.get_all_tool_names())
    candidate_set = set(candidates or available)
    allowed = (role_allow & candidate_set & available) if role_allow else (candidate_set & available)
    if not allowed:
        return []

    query = _words(goal)
    scored = []
    for name in allowed:
        name_words = _words(name.replace("_", " "))
        score = len(query & name_words)
        for hint_words in _TOOL_HINTS.values():
            if query & hint_words and name_words & hint_words:
                score += 2
        # Local workspace requests must prefer the local file/execution
        # surface.  Without this semantic bias, Chinese prose such as
        # "审视自己的代码" produces an all-zero tie and alphabetical order
        # can inject github_repo_contents before file_read.  AstralNexus resolves
        # toolsets before the model sees schemas; this is the same boundary,
        # expressed as domain-neutral local-vs-remote intent routing.
        goal_lower = (goal or "").lower()
        local_markers = (
            "代码", "源码", "工作区", "文件", "项目", "仓库", "代码库",
            "读取", "阅读", "审视", "审查", "检查", "修改", "运行测试",
        )
        remote_markers = ("github", "gitlab", "远程仓库", "开源仓库", "repo")
        web_markers = (
            "网上", "互联网", "公开渠道", "网页", "网站", "在线",
            "中标", "招标", "采购公告", "成交公告", "标书",
            "新闻", "最新", "web", "online", "internet",
        )
        if any(marker in goal_lower for marker in local_markers):
            if name in {"file_read", "search_files"}:
                score += 20
            elif name in {"python_exec", "pytest_run", "terminal_exec", "file_write"}:
                score += 8
            elif name == "github_repo_contents" and not any(
                marker in goal_lower for marker in remote_markers
            ):
                score -= 10
        if any(marker in goal_lower for marker in remote_markers) and name == "github_repo_contents":
            score += 20
        if any(marker in goal_lower for marker in web_markers):
            if name == "web_search":
                score += 30
            elif name in {"http_get", "browser_fetch"}:
                score += 15
            elif name == "search_files":
                score -= 20
        scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))

    # Explicitly required tools are Governor-approved additions to a role's
    # allow-list (for example file_write for a declared delivery artifact).
    # They still must be registered and remain subject to approval/schema
    # filtering below; they are not inferred from the task text.
    required = [
        name for name in dict.fromkeys(required_tools or [])
        if name in available and name in candidate_set
    ]

    # Keep all semantically relevant tools, but cap unscored role tools.  A
    # role with no matching hints still gets a small deterministic toolset.
    relevant = [name for score, name in scored if score > 0]
    neutral = [name for score, name in scored if score == 0]
    limit = max_tools or int(os.environ.get("ASTRALNEXUS_MAX_WORKER_TOOLS", "12"))
    ordered = required + [name for name in (relevant + neutral) if name not in required]
    return ordered[:max(1, limit)]


def estimate_schema_tokens(name: str) -> int:
    """Estimate the token cost of a tool's schema.
    
    Uses a heuristic: ~4 tokens per 100 characters of JSON schema.
    This is a conservative estimate for OpenAI-style function calling.
    """
    entry = registry.get_entry(name)
    if entry is None or not entry.schema:
        return 0
    schema_json = json.dumps(entry.schema)
    # Heuristic: ~1 token per 4 characters for JSON
    return len(schema_json) // 4


def filter_by_schema_budget(
    tool_names: List[str],
    token_budget: int,
    *,
    reserve_for_output: int = 200,
) -> List[str]:
    """Filter tools to fit within a token budget for schema injection.
    
    Args:
        tool_names: List of tool names to consider
        token_budget: Maximum tokens available for tool schemas
        reserve_for_output: Tokens to reserve for model output (default 200)
    
    Returns:
        Filtered list of tool names that fit within budget
    """
    available_budget = max(0, token_budget - reserve_for_output)
    
    # Estimate cost for each tool and sort by cost (cheapest first)
    tool_costs = [(name, estimate_schema_tokens(name)) for name in tool_names]
    tool_costs.sort(key=lambda x: x[1])
    
    # Greedily add tools until budget is exhausted
    selected = []
    used_tokens = 0
    for name, cost in tool_costs:
        if used_tokens + cost <= available_budget:
            selected.append(name)
            used_tokens += cost
        else:
            break
    
    return selected
