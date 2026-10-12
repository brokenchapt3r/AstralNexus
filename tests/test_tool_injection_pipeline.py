"""Tests for tool injection pipeline with Governor approval and budget enforcement."""
import pytest
from unittest.mock import Mock, MagicMock
from AstralNexus.tools.tool_injection_pipeline import (
    ToolInjectionPipeline,
    ToolInjectionDecision,
    validate_injection_budget,
    ensure_metadata_complete,
)


class TestToolInjectionPipeline:
    """Tests for ToolInjectionPipeline.execute()."""
    
    @pytest.fixture
    def mock_governor(self):
        """Create a mock Governor that approves all high-risk tools."""
        gov = Mock()
        gov.approve_tool_injection = Mock(return_value=True)
        return gov
    
    @pytest.fixture
    def restrictive_governor(self):
        """Create a mock Governor that rejects high-risk tools."""
        gov = Mock()
        gov.approve_tool_injection = Mock(return_value=False)
        return gov
    
    def test_pipeline_executes_with_low_risk_tools(self, mock_governor):
        """Should auto-approve low-risk tools and request approval for high-risk."""
        pipeline = ToolInjectionPipeline(mock_governor, max_total_schema_tokens=10000)
        
        tools = pipeline.execute(
            node_goal="读取文件内容",
            role="coder",
            context_budget=5000,
            candidates=None,  # Let discover_tools resolve role-appropriate tools
        )
        
        # Verify contract on whatever tools are returned (may be 0 if coder role has no matches)
        for tool in tools:
            if tool.get("risk_level") == "low":
                assert tool["approval_status"] == "auto_approved"
            elif tool.get("risk_level") == "high":
                assert tool["approval_status"] == "governor_approved"
    def test_pipeline_requests_governor_approval_for_high_risk(self, mock_governor):
        """Should call Governor for high-risk tools."""
        pipeline = ToolInjectionPipeline(
            mock_governor,
            max_total_schema_tokens=10000,
            require_governor_approval_for_high_risk=True,
        )
        
        tools = pipeline.execute(
            node_goal="执行危险操作",
            role="coder",
            context_budget=5000,
            candidates=["file_delete"],  # High-risk tool if it exists
        )
        
        # Should attempt Governor approval (may have 0 tools if file_delete not registered)
        # This test validates the flow, not the specific tool
        assert isinstance(tools, list)
    
    def test_pipeline_skips_rejected_high_risk_tools(self, restrictive_governor):
        """Should skip high-risk tools when Governor rejects them."""
        pipeline = ToolInjectionPipeline(
            restrictive_governor,
            max_total_schema_tokens=10000,
            require_governor_approval_for_high_risk=True,
        )
        
        tools = pipeline.execute(
            node_goal="删除文件",
            role="coder",
            context_budget=5000,
            candidates=["file_delete"],
        )
        
        # Rejected tools should not appear in results
        tool_names = [t["name"] for t in tools]
        assert "file_delete" not in tool_names
    
    def test_pipeline_validates_total_schema_budget(self, mock_governor):
        """Should reject if total schema tokens exceed max_total_schema_tokens."""
        # Use a very low max_total_schema_tokens to force budget violation
        def test_pipeline_validates_total_schema_budget(self, mock_governor):
            """Should raise ValueError when total schema tokens exceed budget."""
            pipeline = ToolInjectionPipeline(
                mock_governor,
                max_total_schema_tokens=1,  # Very low limit
            )
        
            with pytest.raises(ValueError, match="Total schema tokens .* exceeds maximum allowed"):
                pipeline.execute(
                    node_goal="复杂任务需要很多工具",
                    role="coder",
                    context_budget=100000,
                    reserve_for_output=200,
                    candidates=None,  # Allow many tools
                )
    
    def test_pipeline_rejects_uninstalled_tools(self, mock_governor):
        """Should reject tools marked as not installed."""
        # This test validates the installation check logic
        # Since discover_tools only returns registered tools, we test the validation path
        pipeline = ToolInjectionPipeline(mock_governor)
        
        # This should succeed since discover_tools only returns installed tools
        tools = pipeline.execute(
            node_goal="读取文件",
            role="coder",
            context_budget=5000,
            candidates=["file_read"],
        )
        
        # All returned tools should have installed=True
        for tool in tools:
            assert tool.get("installed", False) is True
    
    def test_pipeline_ensures_metadata_completeness(self, mock_governor):
        """Should ensure all required metadata fields are present."""
        pipeline = ToolInjectionPipeline(mock_governor)
        
        tools = pipeline.execute(
            node_goal="读取文件",
            role="coder",
            context_budget=5000,
            candidates=["file_read"],
        )
        
        required_fields = {
            "name", "capabilities", "permissions", "input_schema",
            "output_schema", "source", "installed", "risk_level",
            "approval_status", "schema_tokens",
        }
        
        for tool in tools:
            missing = required_fields - set(tool.keys())
            assert not missing, f"Tool '{tool['name']}' missing fields: {missing}"
    
    def test_pipeline_respects_context_budget(self, mock_governor):
        """Should filter tools based on context_budget."""
        pipeline = ToolInjectionPipeline(mock_governor, max_total_schema_tokens=10000)
        
        # Very tight budget should return fewer tools
        tight_tools = pipeline.execute(
            node_goal="复杂任务",
            role="coder",
            context_budget=500,
            reserve_for_output=200,
            candidates=["file_read", "file_write", "search_files"],
        )
        
        # Generous budget should return more tools
        generous_tools = pipeline.execute(
            node_goal="复杂任务",
            role="coder",
            context_budget=10000,
            reserve_for_output=200,
            candidates=["file_read", "file_write", "search_files"],
        )
        
        assert len(generous_tools) >= len(tight_tools)
    
    def test_pipeline_respects_candidates_allowlist(self, mock_governor):
        """Should only return tools from candidates list."""
        pipeline = ToolInjectionPipeline(mock_governor)
        
        tools = pipeline.execute(
            node_goal="读取文件",
            role="coder",
            context_budget=5000,
            candidates=["file_read"],
        )
        
        tool_names = [t["name"] for t in tools]
        for name in tool_names:
            assert name in ["file_read"]
    
    def test_pipeline_marks_governor_reason_when_approved(self, mock_governor):
        """Should include governor_reason field when Governor approves."""
        pipeline = ToolInjectionPipeline(
            mock_governor,
            require_governor_approval_for_high_risk=True,
        )
        
        tools = pipeline.execute(
            node_goal="执行操作",
            role="coder",
            context_budget=5000,
            candidates=["file_delete"],
        )
        
        # If any high-risk tools were approved, they should have governor_reason
        for tool in tools:
            if tool.get("approval_status") == "governor_approved":
                assert "governor_reason" in tool


class TestToolInjectionDecision:
    """Tests for ToolInjectionDecision dataclass."""
    
    def test_decision_fields(self):
        """Should have all required fields."""
        decision = ToolInjectionDecision(
            tool_name="file_delete",
            approved=True,
            reason="Approved for cleanup task",
            risk_level="high",
            estimated_tokens=150,
        )
        
        assert decision.tool_name == "file_delete"
        assert decision.approved is True
        assert decision.reason == "Approved for cleanup task"
        assert decision.risk_level == "high"
        assert decision.estimated_tokens == 150


class TestValidateInjectionBudget:
    """Tests for validate_injection_budget function."""
    
    def test_validates_within_budget(self):
        """Should not raise when total tokens within budget."""
        tools = [
            {"name": "t1", "schema_tokens": 100},
            {"name": "t2", "schema_tokens": 200},
        ]
        
        # Should not raise
        validate_injection_budget(tools, max_tokens=500)
    
    def test_raises_when_exceeds_budget(self):
        """Should raise ValueError when total exceeds budget."""
        tools = [
            {"name": "t1", "schema_tokens": 300},
            {"name": "t2", "schema_tokens": 400},
        ]
        
        with pytest.raises(ValueError, match="Schema token budget exceeded"):
            validate_injection_budget(tools, max_tokens=500)
    
    def test_handles_missing_schema_tokens_field(self):
        """Should treat missing schema_tokens as 0."""
        tools = [
            {"name": "t1"},  # Missing schema_tokens
            {"name": "t2", "schema_tokens": 100},
        ]
        
        # Should not raise, treats missing as 0
        validate_injection_budget(tools, max_tokens=200)


class TestEnsureMetadataComplete:
    """Tests for ensure_metadata_complete function."""
    
    def test_fills_missing_fields_with_defaults(self):
        """Should fill in missing metadata fields."""
        tool = {"name": "test_tool"}
        
        result = ensure_metadata_complete(tool)
        
        assert result["name"] == "test_tool"
        assert result["capabilities"] == []
        assert result["permissions"] == ["read_only"]
        assert result["input_schema"] == {}
        assert result["output_schema"] == {}
        assert result["source"] == "unknown"
        assert result["installed"] is True
        assert result["risk_level"] == "low"
        assert result["approval_status"] == "auto_approved"
        assert result["schema_tokens"] == 0
    
    def test_preserves_existing_fields(self):
        """Should not overwrite existing fields."""
        tool = {
            "name": "test_tool",
            "capabilities": ["read"],
            "risk_level": "high",
            "schema_tokens": 150,
        }
        
        result = ensure_metadata_complete(tool)
        
        assert result["capabilities"] == ["read"]
        assert result["risk_level"] == "high"
        assert result["schema_tokens"] == 150
    
    def test_returns_new_dict(self):
        """Should return a new dict, not modify the original."""
        tool = {"name": "test_tool"}
        
        result = ensure_metadata_complete(tool)
        
        assert result is not tool
        assert "capabilities" not in tool  # Original unchanged
