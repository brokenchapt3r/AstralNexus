from AstralNexus.tools.registry import discover_builtin_tools, registry
from AstralNexus.tools.tool_router import discover_tools, select_tools


def test_role_allow_list_and_goal_filter_are_applied():
    discover_builtin_tools()
    selected = select_tools("coder", "运行 pytest 测试并修改代码", [
        "pytest_run", "file_editor", "web_search", "unknown_tool"
    ])
    assert "unknown_tool" not in selected
    assert "web_search" not in selected
    assert set(selected).issubset(set(registry.get_all_tool_names()))


def test_tool_budget_has_a_deterministic_cap(monkeypatch):
    discover_builtin_tools()
    monkeypatch.setenv("ASTRALNEXUS_MAX_WORKER_TOOLS", "2")
    selected = select_tools("researcher", "检索论文和趋势", registry.get_all_tool_names())
    assert len(selected) <= 2


def test_tool_discovery_returns_registered_auditable_capabilities():
    discover_builtin_tools()
    discovered = discover_tools("coder", "运行 pytest 测试并修改代码", ["pytest_run", "file_write"])
    names = {item["name"] for item in discovered}
    assert names <= set(registry.get_all_tool_names())
    assert "pytest_run" in names
    pytest_tool = next(item for item in discovered if item["name"] == "pytest_run")
    assert pytest_tool["installed"] is True
    assert "permissions" in pytest_tool


def test_registry_keeps_governance_metadata_out_of_handlers():
    discover_builtin_tools()
    result = registry.dispatch(
        "search_files",
        {"query": "__astralnexus_missing_test_marker__", "max_results": 1},
        caller="unit-test",
        approval_basis="unit-test",
    )
    assert "unexpected keyword argument 'caller'" not in result


def test_public_procurement_request_prefers_web_over_workspace_search():
    discover_builtin_tools()
    selected = select_tools(
        "interactive",
        "帮我找一些已经中标的标书",
        ["web_search", "http_get", "browser_fetch", "search_files"],
        max_tools=4,
    )

    assert selected[0] == "web_search"
    assert "http_get" in selected
    assert "search_files" not in selected or selected.index("http_get") < selected.index("search_files")
