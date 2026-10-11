"""Small development capability priorities, never an authorization override."""
from __future__ import annotations


def development_tool_priority(goal: str) -> tuple[str, ...]:
    text = goal.casefold()
    if not any(marker in text for marker in (
        "全栈", "fullstack", "full-stack", "full stack", "接口测试", "api test",
        "前后端", "数据库验证",
    )):
        return ()
    # Select only from the caller's existing allow-list. Browser interaction
    # requires a configured provider; HTTP fetching cannot stand in for it.
    browser = ("mcp_servers", "mcp_tools", "mcp_call") if any(
        marker in text for marker in ("浏览器", "playwright", "browser", "e2e")
    ) else ()
    return browser + (
        "file_read", "search_files", "terminal_exec", "pytest_run",
        "http_request", "sqlite_query", "file_write", "runtime_dependencies",
        "python_exec", "skill_view", "skills_list",
    )
