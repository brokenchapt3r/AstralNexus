from AstralNexus.tools.registry import discover_builtin_tools, registry
from AstralNexus.tools.tool_router import discover_tools


def test_worker_tool_discovery_populates_registered_coding_tools():
    discover_builtin_tools()
    tools = discover_tools(
        "coder",
        "构建本地可运行的应用并编写测试",
        ["file_read", "file_write", "terminal_exec", "python_exec", "pytest_run"],
    )

    assert tools
    names = {item["name"] for item in tools}
    assert {"file_read", "file_write", "python_exec", "pytest_run"}.issubset(names)
    assert all(registry.get_entry(name) is not None for name in names)
