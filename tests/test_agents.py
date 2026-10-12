"""Unit tests for real-LLM-only role agent contracts.

These tests intentionally do not assert synthetic dry-run content. Role
execution must either call the configured LLM runtime successfully or return
an explicit runtime failure for orchestration/recovery.
"""
from __future__ import annotations

import os

import pytest

from AstralNexus.agents.base import AgentContext, AgentResponse, ExecutionOutcome
from AstralNexus.agents.coder import Coder
from AstralNexus.agents.memory_curator import MemoryCurator
from AstralNexus.agents.planner import PlannerAgent
from AstralNexus.agents.recovery_agent import RecoveryAgent
from AstralNexus.agents.researcher import Researcher
from AstralNexus.agents.resource_broker import ResourceBroker
from AstralNexus.agents.reviewer import Reviewer
from AstralNexus.agents.verifier import VerificationWorker
from AstralNexus.agents.writer import Writer


LIVE_PATHS = {"runtime_llm_direct", "runtime_llm_failed"}
LEGACY_SYNTHETIC_KEY = "dry" + "_run"
LIVE_LLM_ENABLED = os.environ.get("ASTRALNEXUS_RUN_LIVE_TESTS") == "1"


@pytest.fixture
def sample_context(tmp_path):
    return AgentContext(
        node_id="T-001",
        goal="实现一个排序算法",
        constraints=["时间复杂度O(n log n)", "支持自定义比较函数"],
        artifacts=[],
        metadata={"priority": "high", "run_dir": tmp_path},
    )


@pytest.fixture
def recovery_context(tmp_path):
    return AgentContext(
        node_id="T-002",
        goal="恢复失败的任务",
        constraints=["不能丢失已完成工作"],
        artifacts=[],
        metadata={
            "run_dir": tmp_path,
            "failure_info": {
                "fault_type": "timeout",
                "target": "T-001",
                "reason": "API调用超时",
            }
        },
    )


def _assert_real_runtime_response(response: AgentResponse) -> None:
    assert isinstance(response, AgentResponse)
    assert response.outcome == ExecutionOutcome.SUCCEEDED
    assert LEGACY_SYNTHETIC_KEY not in response.metrics
    assert response.metrics.get("execution_path") == "runtime_llm_direct"
    assert response.content


def test_live_acceptance_rejects_runtime_failure():
    with pytest.raises(AssertionError):
        _assert_real_runtime_response(AgentResponse(
            outcome=ExecutionOutcome.FAILED,
            content="provider failed",
            metrics={"execution_path": "runtime_llm_failed"},
        ))


def test_worker_package_is_in_installation_manifest():
    from pathlib import Path
    from setuptools import find_packages
    root = Path(str(__import__('AstralNexus').__file__)).resolve().parent
    assert "agents" in find_packages(where=str(root))


@pytest.mark.parametrize(
    "AgentClass",
    [Coder, Researcher, VerificationWorker, Reviewer, MemoryCurator, ResourceBroker, Writer, PlannerAgent],
)
@pytest.mark.live_llm
@pytest.mark.skipif(
    not LIVE_LLM_ENABLED,
    reason="set ASTRALNEXUS_RUN_LIVE_TESTS=1 for real provider validation",
)
def test_agents_execute_through_real_runtime_contract(AgentClass, sample_context):
    agent = AgentClass()
    response = agent.execute(sample_context)
    _assert_real_runtime_response(response)


@pytest.mark.live_llm
@pytest.mark.skipif(
    not LIVE_LLM_ENABLED,
    reason="set ASTRALNEXUS_RUN_LIVE_TESTS=1 for real provider validation",
)
def test_recovery_agent_execute_through_real_runtime_contract(recovery_context):
    agent = RecoveryAgent()
    response = agent.execute(recovery_context)
    _assert_real_runtime_response(response)


def test_verification_worker_cannot_issue_acceptance_verdicts():
    worker = VerificationWorker()
    assert not hasattr(worker, "verify_acceptance_criteria")


@pytest.mark.parametrize(
    "AgentClass",
    [Coder, Researcher, VerificationWorker, Reviewer, MemoryCurator, ResourceBroker, RecoveryAgent, Writer, PlannerAgent],
)
def test_all_agents_have_role_and_capabilities(AgentClass):
    agent = AgentClass()
    assert agent.role
    assert agent.capabilities
