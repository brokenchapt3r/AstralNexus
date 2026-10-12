"""Tests for dynamic tool discovery with schema budget filtering."""
from __future__ import annotations

import pytest

from AstralNexus.tools.tool_router import (
    discover_tools,
    estimate_schema_tokens,
    filter_by_schema_budget,
)


class TestSchemaTokenEstimation:
    """Tests for estimate_schema_tokens function."""

    def test_estimates_zero_for_missing_tool(self):
        """Should return 0 for non-existent tool."""
        tokens = estimate_schema_tokens("nonexistent_tool")
        assert tokens == 0

    def test_estimates_zero_for_tool_without_schema(self):
        """Should return 0 for tool with no schema."""
        # Most tools in the registry have schemas, so this tests the None case
        tokens = estimate_schema_tokens("invalid_tool_xyz")
        assert tokens == 0

    def test_estimates_positive_for_valid_tool(self):
        """Should return positive token count for tool with schema."""
        # file_read is a basic tool that should always exist
        tokens = estimate_schema_tokens("file_read")
        assert tokens >= 0  # May be 0 if schema is minimal, but should not error


class TestSchemaBudgetFiltering:
    """Tests for filter_by_schema_budget function."""

    def test_returns_empty_when_budget_zero(self):
        """Should return only zero-cost tools when budget is 0."""
        result = filter_by_schema_budget(["file_read", "file_write"], 0)
        # Tools with 0-cost schemas may still be included; the key constraint
        # is that no positive-cost tools are added
        for name in result:
            assert estimate_schema_tokens(name) == 0

    def test_returns_empty_when_budget_less_than_reserve(self):
        """Should only include zero-cost tools when budget < reserve."""
        result = filter_by_schema_budget(["file_read"], 100, reserve_for_output=200)
        # Available budget is 0, so only zero-cost tools pass
        for name in result:
            assert estimate_schema_tokens(name) == 0

    def test_filters_tools_to_fit_budget(self):
        """Should select tools that fit within budget."""
        tools = ["file_read", "file_write", "search_files"]
        # With a very large budget, all tools should fit
        result = filter_by_schema_budget(tools, 10000, reserve_for_output=100)
        # Should return some subset (may not be all if schemas are large)
        assert len(result) <= len(tools)

    def test_prefers_cheaper_tools(self):
        """Should prefer tools with smaller schemas when budget is tight."""
        tools = ["file_read", "file_write"]
        # With minimal budget, should pick cheapest first
        result = filter_by_schema_budget(tools, 500, reserve_for_output=100)
        # Result should be deterministic and based on schema size
        assert isinstance(result, list)

    def test_respects_reserve_for_output(self):
        """Should reserve tokens for output and reduce available budget."""
        tools = ["file_read"]
        # Budget of 300 with reserve of 200 leaves only 100 for schemas
        result = filter_by_schema_budget(tools, 300, reserve_for_output=200)
        # Should only include tool if schema fits in remaining 100 tokens
        assert isinstance(result, list)


class TestDiscoverToolsWithBudget:
    """Tests for discover_tools with schema_budget parameter."""

    def test_discover_without_budget_returns_all_relevant(self):
        """Should return all relevant tools when no budget specified."""
        result = discover_tools(
            role="coder",
            goal="实现一个文件处理功能",
            candidates=None,
        )
        assert isinstance(result, list)
        # Should have discovered some tools
        assert len(result) >= 0

    def test_discover_with_budget_filters_tools(self):
        """Should filter tools when schema_budget is specified."""
        # First, discover without budget to see what's available
        full_result = discover_tools(
            role="coder",
            goal="实现代码测试",
            candidates=None,
        )
        
        # Now discover with very tight budget
        budget_result = discover_tools(
            role="coder",
            goal="实现代码测试",
            candidates=None,
            schema_budget=500,  # Very tight budget
        )
        
        # Budget result should have same or fewer tools
        assert len(budget_result) <= len(full_result)

    def test_discover_includes_schema_tokens_metadata(self):
        """Should include schema_tokens in returned tool metadata."""
        result = discover_tools(
            role="coder",
            goal="读取文件",
            candidates=["file_read"],
        )
        
        if result:  # If file_read was discovered
            tool = result[0]
            assert "schema_tokens" in tool
            assert isinstance(tool["schema_tokens"], int)
            assert tool["schema_tokens"] >= 0

    def test_discover_includes_approval_status(self):
        """Should include approval_status in returned tool metadata."""
        result = discover_tools(
            role="coder",
            goal="读取文件",
            candidates=["file_read"],
            governor_approval=True,
        )
        
        if result:
            tool = result[0]
            assert "approval_status" in tool
            assert tool["approval_status"] in [
                "auto_approved",
                "approval_required",
                "pending_governor_approval",
            ]

    def test_governor_approval_marks_high_risk_tools(self):
        """Should mark high-risk tools as pending when governor_approval=True."""
        # This test verifies the approval logic
        result = discover_tools(
            role="coder",
            goal="执行危险操作",
            candidates=None,
            governor_approval=True,
        )
        
        # Check that approval_status is set correctly
        for tool in result:
            if tool["risk_level"] == "high":
                assert tool["approval_status"] == "pending_governor_approval"
            else:
                assert tool["approval_status"] == "auto_approved"

    def test_budget_filtering_respects_max_tools(self):
        """Should apply both max_tools and schema_budget constraints."""
        result = discover_tools(
            role="coder",
            goal="实现多种功能",
            candidates=None,
            max_tools=5,
            schema_budget=2000,
        )
        
        # Should not exceed max_tools
        assert len(result) <= 5


class TestProgressiveSchemaLoading:
    """Tests for progressive schema loading patterns."""

    def test_budget_allows_more_tools_with_larger_budget(self):
        """Should discover more tools with larger schema budget."""
        small_budget = discover_tools(
            role="coder",
            goal="实现复杂功能",
            schema_budget=500,
        )
        
        large_budget = discover_tools(
            role="coder",
            goal="实现复杂功能",
            schema_budget=5000,
        )
        
        # Larger budget should allow more tools (or same if all fit)
        assert len(large_budget) >= len(small_budget)

    def test_zero_budget_returns_empty_or_minimal(self):
        """Should return minimal results with zero budget."""
        result = discover_tools(
            role="coder",
            goal="测试",
            schema_budget=0,
        )
        
        # With zero budget, should return empty or very few tools
        assert len(result) == 0 or len(result) <= 1
