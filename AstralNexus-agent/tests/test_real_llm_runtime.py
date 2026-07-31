"""Tests for real-LLM-only role execution semantics."""

from pathlib import Path
from unittest.mock import patch

import pytest

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder


def _context() -> AgentContext:
    return AgentContext(
        node_id="T-001",
        goal="Return a concise implementation plan.",
        constraints=[],
        artifacts=[],
        metadata={"run_dir": Path("/tmp/test-run")},
    )


def test_runtime_failure_returns_failure_without_runtime_fallback(monkeypatch):
    """Runtime errors must be explicit failures, never silent offline fallback."""
    monkeypatch.setenv("ASTRALNEXUS_API_KEY", "test-key")
    agent = Coder()
    with patch(
        "AstralNexus.runtime.llm_client.LLMClient.stream_chat",
        side_effect=RuntimeError("LLM API timeout"),
    ):
        result = agent.try_execute_with_runtime(_context(), task_kind="test")

    assert result is not None
    assert result.outcome.value == "failed"
    assert result.metrics["execution_path"] == "runtime_llm_failed"
    assert "LLM API timeout" in result.content
