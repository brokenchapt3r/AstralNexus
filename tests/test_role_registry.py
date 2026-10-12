from __future__ import annotations

import pytest

from AstralNexus.agents.verifier import VerificationWorker
from AstralNexus.core.agent_registry import get_agent_registry
from AstralNexus.core.role_registry import (
    get_role_capabilities,
    initialize_role_registry,
)


@pytest.fixture(autouse=True)
def setup_registry():
    initialize_role_registry()


def test_core_roles_have_contracts_and_real_factories():
    registry = get_agent_registry()
    core_roles = {
        "planner",
        "researcher",
        "coder",
        "architect",
        "tester",
        "synthesizer",
        "verifier",
        "reviewer",
        "memory_curator",
        "resource_broker",
        "recovery_agent",
    }
    for name in core_roles:
        role = registry.get_role(name)
        assert role is not None
        assert role.description
        assert role.capabilities
        assert role.input_contract
        assert role.output_contract
        assert registry.get_role_factory(name) is not None


def test_profile_aliases_resolve_to_registered_factories():
    registry = get_agent_registry()
    for label in (
        "retrieval", "reasoning", "coding", "architecture", "testing",
        "synthesis", "tool", "critic", "recovery",
    ):
        assert registry.get_role_factory(label) is not None


def test_verifier_role_is_evidence_worker_not_quality_authority():
    registry = get_agent_registry()
    worker = registry.create_agent("verifier")
    assert isinstance(worker, VerificationWorker)
    assert not hasattr(worker, "verify_acceptance_criteria")


def test_capability_lookup_uses_registry_source_of_truth():
    assert "programming" in get_role_capabilities("coder")
    assert get_role_capabilities("missing-role") == []
