"""One model tool surface: registered, enabled tools within explicit caller scope."""
from __future__ import annotations
from collections.abc import Iterable
from .registry import discover_builtin_tools, registry

def capability_guidance(tools: list[dict]) -> str:
    names = {item["function"]["name"] for item in tools}
    lines = ["只能调用本轮实际提供的工具；工具结果是执行事实，不得虚构未安装能力或执行结果。"]
    if {"skills_list", "skill_view"} <= names:
        lines.append("复杂任务先用 skills_list 查看启用技能，再用 skill_view 读取并遵循所选技能。")
    if "runtime_dependencies" in names:
        lines.append("用 runtime_dependencies 检查当前环境依赖。")
    if {"mcp_servers", "mcp_tools", "mcp_call"} <= names:
        lines.append("MCP 使用 mcp_servers/mcp_tools 发现已配置能力，再用 mcp_call 实际调用。")
    return "".join(lines)


def assemble_tools(*, candidates: Iterable[str] | None = None, lead: bool = True) -> list[dict]:
    discover_builtin_tools()
    names = set(registry.get_all_tool_names()) if candidates is None else set(candidates)
    if not lead:
        names.difference_update({"present_files", "present", "delegate_task"})
    return registry.get_definitions(names, quiet=True)
