import json

from AstralNexus.tools.builtin_tools import _file_read
from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools.registry import discover_builtin_tools
from AstralNexus.tools.tool_router import alternative_tools


def test_tool_alternatives_are_registry_and_allowlist_bound():
    discover_builtin_tools()
    assert alternative_tools("openalex_search", ["crossref_search"]) == ["crossref_search"]
    assert "unknown_tool" not in alternative_tools("openalex_search", ["unknown_tool"])


def test_tool_failure_contains_structured_recovery_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    result = json.loads(_file_read({"path": "missing.txt"}))
    annotated = ToolExecutor._annotate_failure("file_read", result)
    assert annotated["failure_class"] == "tool_execution_failed"
    assert annotated["next_action"] == "switch_tool_or_replan"
    assert annotated["alternative_tools"] == ["search_files"]
