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


def test_missing_artifact_repair_retains_original_goal(tmp_path, monkeypatch):
    from AstralNexus.agents.resource_broker import ResourceBroker
    from AstralNexus.runtime.llm_client import LLMClient
    monkeypatch.setenv("ASTRALNEXUS_API_KEY", "test-key")
    calls = []
    def chat(client, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 2:
            prompt = messages[-1]["content"]
            assert '"total":6' in prompt
            assert "Only these two outputs" in prompt
            (tmp_path / "result.json").write_text('{"total":6}', encoding="utf-8")
        return "Calculated total is 6"
    monkeypatch.setattr(LLMClient, "stream_chat", chat)
    result = ResourceBroker().try_execute_with_runtime(AgentContext(
        node_id="repair", goal='Write result.json with exactly {"total":6}.',
        constraints=["Only these two outputs"], tools=["file_write"],
        metadata={"run_dir": str(tmp_path), "expected_artifact_paths": ["result.json"],
                  "owned_paths": ["result.json"], "worker_profile": {"tools": ["file_write"]}},
    ), task_kind="test")
    assert result.outcome.value == "succeeded", result.content
    assert len(calls) == 2
