"""Unit tests for core.agent_registry module.

Validates AgentRole and AgentRegistry functionality.
Implements 天枢方案书 §3.3 role registry requirements.
"""
from __future__ import annotations

import pytest

from AstralNexus.core.agent_registry import (
    AgentRegistry,
    AgentRole,
    get_agent_registry,
)


@pytest.fixture
def registry():
    """Create a fresh AgentRegistry instance."""
    return AgentRegistry()


@pytest.fixture
def sample_role():
    """Create a sample AgentRole."""
    return AgentRole(
        name="coder",
        description="编码员，负责代码修改和实现",
        default_tools=["read_file", "write_file", "terminal"],
        capabilities=["programming", "debugging"],
        input_contract="功能需求、代码规范",
        output_contract="修改后的代码、测试结果",
    )


class TestAgentRole:
    """Test AgentRole dataclass."""

    def test_creation(self, sample_role):
        assert sample_role.name == "coder"
        assert "编码员" in sample_role.description
        assert len(sample_role.default_tools) == 3

    def test_default_tools_empty(self):
        role = AgentRole(name="test", description="test role")
        assert role.default_tools == []

    def test_capabilities_empty(self):
        role = AgentRole(name="test", description="test role")
        assert role.capabilities == []

    def test_contracts_optional(self):
        role = AgentRole(name="test", description="test role")
        assert role.input_contract is None
        assert role.output_contract is None


class TestAgentRegistry:
    """Test AgentRegistry functionality."""

    def test_register_role(self, registry, sample_role):
        registry.register_role(sample_role)
        assert "coder" in registry.list_roles()

    def test_get_role(self, registry, sample_role):
        registry.register_role(sample_role)
        retrieved = registry.get_role("coder")
        assert retrieved is sample_role

    def test_get_role_not_found(self, registry):
        result = registry.get_role("nonexistent")
        assert result is None

    def test_list_roles(self, registry):
        role1 = AgentRole(name="coder", description="coder role")
        role2 = AgentRole(name="verifier", description="verifier role")
        
        registry.register_role(role1)
        registry.register_role(role2)
        
        roles = registry.list_roles()
        assert "coder" in roles
        assert "verifier" in roles
        assert len(roles) == 2

    def test_register_role_factory(self, registry, sample_role):
        def coder_factory(**kwargs):
            return {"type": "coder", **kwargs}
        
        registry.register_role(sample_role)
        registry.register_role_factory("coder", coder_factory)
        
        factory = registry.get_role_factory("coder")
        assert factory is not None
        
        result = factory(node_id="T-001")
        assert result["type"] == "coder"
        assert result["node_id"] == "T-001"

    def test_get_role_factory_not_found(self, registry):
        result = registry.get_role_factory("nonexistent")
        assert result is None

    def test_create_agent_with_factory(self, registry, sample_role):
        def coder_factory(**kwargs):
            return {"type": "coder", **kwargs}
        
        registry.register_role(sample_role)
        registry.register_role_factory("coder", coder_factory)
        
        agent = registry.create_agent("coder", node_id="T-001", priority="high")
        assert agent is not None
        assert agent["type"] == "coder"
        assert agent["node_id"] == "T-001"
        assert agent["priority"] == "high"

    def test_create_agent_no_factory(self, registry, sample_role):
        registry.register_role(sample_role)
        # No factory registered
        agent = registry.create_agent("coder")
        assert agent is None

    def test_multiple_roles(self, registry):
        roles = [
            AgentRole(name="planner", description="planner role"),
            AgentRole(name="researcher", description="researcher role"),
            AgentRole(name="coder", description="coder role"),
        ]
        
        for role in roles:
            registry.register_role(role)
        
        assert len(registry.list_roles()) == 3
        assert registry.get_role("planner") is not None
        assert registry.get_role("researcher") is not None
        assert registry.get_role("coder") is not None


class TestGlobalRegistry:
    """Test global registry singleton."""

    def test_get_agent_registry_returns_instance(self):
        registry = get_agent_registry()
        assert isinstance(registry, AgentRegistry)

    def test_get_agent_registry_is_singleton(self):
        registry1 = get_agent_registry()
        registry2 = get_agent_registry()
        assert registry1 is registry2

    def test_global_registry_persists_roles(self):
        registry = get_agent_registry()
        
        # Register a test role
        test_role = AgentRole(
            name=f"test_role_{id(registry)}",
            description="test",
        )
        registry.register_role(test_role)
        
        # Retrieve from global registry
        retrieved = get_agent_registry().get_role(test_role.name)
        assert retrieved is test_role


class TestAgentRoleContracts:
    """Test role input/output contracts."""

    def test_input_contract_present(self, sample_role):
        assert sample_role.input_contract is not None
        assert "功能需求" in sample_role.input_contract

    def test_output_contract_present(self, sample_role):
        assert sample_role.output_contract is not None
        assert "修改后的代码" in sample_role.output_contract

    def test_contract_can_be_multiline(self):
        role = AgentRole(
            name="test",
            description="test",
            input_contract="Line 1\nLine 2\nLine 3",
        )
        assert "\n" in role.input_contract

    def test_role_with_many_tools(self):
        role = AgentRole(
            name="complex",
            description="complex role",
            default_tools=[f"tool_{i}" for i in range(20)],
        )
        assert len(role.default_tools) == 20
