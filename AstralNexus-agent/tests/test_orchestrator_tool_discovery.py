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


def test_governor_profile_tools_survive_implementation_role_filter():
    discover_builtin_tools()
    candidates = ["file_write", "python_exec", "nonexistent_tool"]
    assert not discover_tools("resource_broker", "write and validate result.json", candidates)
    actual = discover_tools("resource_broker", "write and validate result.json", candidates, profile_authorized=True)
    assert {item["name"] for item in actual} == {"file_write", "python_exec"}


def test_empty_profile_never_inherits_registry_tools():
    discover_builtin_tools()
    for candidates in ([], None):
        assert discover_tools("resource_broker", "write files", candidates, profile_authorized=True) == []
