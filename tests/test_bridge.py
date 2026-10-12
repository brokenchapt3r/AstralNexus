"""Tests for core.bridge — AgentResponse ↔ StructuredMessage conversion.

Validates the low-entropy communication protocol bridge (天枢方案书 §5.3).
"""

from __future__ import annotations
from AstralNexus.core.state_machine import TaskStatus

import pytest

from AstralNexus.agents.base import (
    AgentContext,
    AgentResponse,
    ExecutionOutcome,
)
from AstralNexus.core.bridge import (
    _estimate_confidence,
    batch_responses_to_messages,
    message_to_context,
    response_to_message,
)
from AstralNexus.core.message_protocol import StructuredMessage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_response() -> AgentResponse:
    """A typical successful agent response."""
    return AgentResponse(
        outcome=ExecutionOutcome.SUCCEEDED,
        content="快速排序算法实现完成，时间复杂度O(n log n)",
        artifacts=["sort.py", "test_sort.py"],
        next_actions=[{"type": "verify", "target": "T-002", "reason": "需要验证"}],
        metrics={
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "elapsed_seconds": 2.5,
            "test_passed": 5,
            "coverage": 0.85,
        },
    )


@pytest.fixture
def sample_message() -> StructuredMessage:
    """A typical structured message for downstream consumption."""
    return StructuredMessage(
        node_id="T-001",
        role="coder",
        status=TaskStatus.RESULT_SUBMITTED,
        summary="排序算法实现完成",
        claims=[
            {"text": "代码已通过所有测试", "evidence": ["test.py"], "confidence": 0.95},
            {"text": "性能待优化", "evidence": [], "confidence": 0.6},
        ],
        artifacts=["sort.py", "test_sort.py"],
        risks=["性能瓶颈"],
        next_actions=[{"type": "verify", "target": "T-002", "reason": "验证正确性"}],
        budget_used={"prompt_tokens": 1000, "completion_tokens": 500, "wall_time_s": 2.5},
        metadata={
            "goal": "实现排序算法",
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    )


# ===========================================================================
# response_to_message tests
# ===========================================================================

class TestResponseToMessage:
    """Tests for converting AgentResponse → StructuredMessage."""

    def test_basic_conversion(self, sample_response: AgentResponse):
        msg = response_to_message(
            sample_response,
            node_id="T-001",
            role="coder",
            goal="实现排序算法",
        )
        assert isinstance(msg, StructuredMessage)
        assert msg.node_id == "T-001"
        assert msg.role == "coder"
        assert msg.metadata["goal"] == "实现排序算法"

    def test_status_mapping_success(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        assert msg.status == "result_submitted"

    def test_status_mapping_failure(self):
        resp = AgentResponse(outcome=ExecutionOutcome.FAILED, content="执行失败")
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        assert msg.status == "failed"

    def test_status_mapping_partial(self):
        resp = AgentResponse(outcome=ExecutionOutcome.PARTIAL, content="部分完成")
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        assert msg.status == "blocked"

    def test_status_mapping_unknown(self):
        with pytest.raises(ValueError, match="unknown_status"):
            AgentResponse(outcome="unknown_status", content="?")

    def test_claims_created_from_content(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        assert len(msg.claims) >= 1
        # Claims are dicts with "text" key
        assert "快速排序" in msg.claims[0]["text"]
        assert "evidence" in msg.claims[0]
        assert "confidence" in msg.claims[0]

    def test_claims_include_metrics(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        metric_texts = [c["text"] for c in msg.claims]
        has_test = any("test_passed" in t for t in metric_texts)
        has_cov = any("coverage" in t for t in metric_texts)
        assert has_test or has_cov

    def test_artifacts_preserved(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        assert "sort.py" in msg.artifacts
        assert "test_sort.py" in msg.artifacts

    def test_next_actions_preserved(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        assert len(msg.next_actions) == 1
        assert msg.next_actions[0]["type"] == "verify"
        assert msg.next_actions[0]["target"] == "T-002"

    def test_budget_extracted_from_metrics(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        assert msg.budget_used["prompt_tokens"] == 1000
        assert msg.budget_used["completion_tokens"] == 500
        assert msg.budget_used["wall_time_s"] == 2.5

    def test_summary_truncated_for_low_entropy(self):
        long_content = "A" * 300
        resp = AgentResponse(outcome=ExecutionOutcome.SUCCEEDED, content=long_content)
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        assert len(msg.summary) <= 120

    def test_empty_content_produces_empty_summary(self):
        resp = AgentResponse(outcome=ExecutionOutcome.SUCCEEDED, content="")
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        assert msg.summary == ""

    def test_confidence_estimation_success(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="test")
        assert msg.claims[0]["confidence"] >= 0.9

    def test_confidence_estimation_failure(self):
        resp = AgentResponse(outcome=ExecutionOutcome.FAILED, content="失败")
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        assert msg.claims[0]["confidence"] < 0.5

    def test_runtime_metadata_preserved(self):
        resp = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="runtime result",
            metrics={"runtime_status": "succeeded", "execution_path": "runtime_llm_direct"},
        )
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        assert msg.metadata.get("execution_path") == "runtime_llm_direct"
        assert msg.metadata.get("execution_outcome") == "succeeded"
        assert ("dry" + "_run") not in msg.metadata


# ===========================================================================
# message_to_context tests
# ===========================================================================

class TestMessageToContext:
    """Tests for converting StructuredMessage → AgentContext."""

    def test_basic_conversion(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        assert isinstance(ctx, AgentContext)
        assert ctx.node_id == "T-002"

    def test_goal_preserved(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        assert ctx.goal == "实现排序算法"

    def test_constraints_from_high_confidence_claims(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        # Only claims with confidence >= 0.8 become constraints
        assert len(ctx.constraints) >= 1
        assert any("测试" in c for c in ctx.constraints)

    def test_artifacts_preserved(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        assert "sort.py" in ctx.artifacts
        assert "test_sort.py" in ctx.artifacts

    def test_metadata_includes_upstream_info(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        assert ctx.metadata["upstream_node_id"] == "T-001"
        assert ctx.metadata["upstream_role"] == "coder"
        assert ctx.metadata["upstream_status"] == "result_submitted"

    def test_resources_include_artifacts(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        assert "artifacts" in ctx.resources

    def test_tools_are_empty(self, sample_message: StructuredMessage):
        ctx = message_to_context(sample_message, target_node_id="T-002")
        assert ctx.tools == []

    def test_low_confidence_claims_filtered(self):
        msg = StructuredMessage(
            node_id="T-001",
            role="researcher",
            status=TaskStatus.RESULT_SUBMITTED,
            summary="研究结果",
            claims=[
                {"text": "高置信度发现", "evidence": [], "confidence": 0.95},
                {"text": "低置信度假设", "evidence": [], "confidence": 0.3},
            ],
            metadata={"goal": "test"},
        )
        ctx = message_to_context(msg, target_node_id="T-002")
        assert len(ctx.constraints) == 1
        assert "高置信度发现" in ctx.constraints[0]

    def test_empty_claims_produce_no_constraints(self):
        msg = StructuredMessage(
            node_id="T-001",
            role="coder",
            status=TaskStatus.RESULT_SUBMITTED,
            summary="done",
            claims=[],
            metadata={"goal": "test"},
        )
        ctx = message_to_context(msg, target_node_id="T-002")
        assert ctx.constraints == []


# ===========================================================================
# batch_responses_to_messages tests
# ===========================================================================

class TestBatchResponsesToMessages:
    """Tests for batch conversion."""

    def test_batch_conversion(self, sample_response: AgentResponse):
        batch = [
            (sample_response, "T-001", "coder", "goal1"),
            (
                AgentResponse(outcome=ExecutionOutcome.FAILED, content="fail"),
                "T-002",
                "verifier",
                "goal2",
            ),
        ]
        messages = batch_responses_to_messages(batch)
        assert len(messages) == 2
        assert messages[0].node_id == "T-001"
        assert messages[1].node_id == "T-002"

    def test_empty_batch(self):
        messages = batch_responses_to_messages([])
        assert messages == []

    def test_batch_preserves_all_fields(self, sample_response: AgentResponse):
        batch = [(sample_response, "T-001", "coder", "test")]
        messages = batch_responses_to_messages(batch)
        msg = messages[0]
        assert msg.metadata["goal"] == "test"
        assert msg.role == "coder"
        assert len(msg.artifacts) == 2


# ===========================================================================
# Round-trip tests
# ===========================================================================

class TestRoundTrip:
    """Tests for round-trip conversion fidelity."""

    def test_round_trip_preserves_key_info(self, sample_response: AgentResponse):
        msg = response_to_message(sample_response, node_id="T-001", role="coder", goal="排序")
        ctx = message_to_context(msg, target_node_id="T-002")
        assert ctx.node_id == "T-002"
        assert ctx.goal == "排序"
        assert "sort.py" in ctx.artifacts

    def test_round_trip_status_consistency(self):
        resp = AgentResponse(outcome=ExecutionOutcome.SUCCEEDED, content="done")
        msg = response_to_message(resp, node_id="T-001", role="coder", goal="test")
        ctx = message_to_context(msg, target_node_id="T-002")
        assert ctx.metadata["upstream_status"] == "result_submitted"


# ===========================================================================
# Confidence estimation unit tests
# ===========================================================================

class TestEstimateConfidence:
    """Tests for _estimate_confidence heuristic."""

    def test_success_high_confidence(self):
        resp = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="ok",
            artifacts=["a.py"],
        )
        conf = _estimate_confidence(resp)
        assert conf >= 0.9

    def test_failure_low_confidence(self):
        resp = AgentResponse(outcome=ExecutionOutcome.FAILED, content="error")
        conf = _estimate_confidence(resp)
        assert conf < 0.5

    def test_partial_medium_confidence(self):
        resp = AgentResponse(outcome=ExecutionOutcome.PARTIAL, content="partial")
        conf = _estimate_confidence(resp)
        assert 0.5 <= conf <= 0.8

    def test_artifact_bonus(self):
        resp_no_artifacts = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="ok",
        )
        resp_with_artifacts = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="ok",
            artifacts=["a", "b", "c"],
        )
        assert _estimate_confidence(resp_with_artifacts) >= _estimate_confidence(resp_no_artifacts)

    def test_metric_bonus(self):
        resp_no_metrics = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="ok",
        )
        resp_with_metrics = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="ok",
            metrics={"a": 1, "b": 2, "c": 3},
        )
        assert _estimate_confidence(resp_with_metrics) >= _estimate_confidence(resp_no_metrics)

    def test_confidence_capped_at_one(self):
        resp = AgentResponse(
            outcome=ExecutionOutcome.SUCCEEDED,
            content="ok",
            artifacts=["a"] * 20,
            metrics={f"m{i}": i for i in range(20)},
        )
        assert _estimate_confidence(resp) <= 1.0
