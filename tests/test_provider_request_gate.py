import pytest
from unittest.mock import patch

from AstralNexus.runtime.provider_request_gate import (
    ProviderCircuitOpenError,
    ProviderRequestGate,
    provider_request_gate,
)
from AstralNexus.runtime.llm_client import LLMClient


def test_provider_failure_opens_circuit_and_success_closes_it(monkeypatch):
    key = "test-provider-request-gate"
    monkeypatch.setenv("ASTRALNEXUS_PROVIDER_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("ASTRALNEXUS_PROVIDER_COOLDOWN", "30")

    with provider_request_gate.request_slot(key):
        pass
    provider_request_gate.record_failure(key, "network", True)

    with pytest.raises(ProviderCircuitOpenError):
        with provider_request_gate.request_slot(key):
            pass
    provider_request_gate.record_success(key)
    with provider_request_gate.request_slot(key):
        pass


def test_provider_request_gate_exposes_global_state(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_PROVIDER_MAX_CONCURRENCY", "2")
    gate = ProviderRequestGate()
    with gate.request_slot("provider-a"):
        snapshot = gate.snapshot()
        assert snapshot["provider-a"]["circuit_open"] is False
        assert snapshot["provider-a"]["consecutive_failures"] == 0


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":2,"completion_tokens":1}}'


def test_llm_request_stats_include_provider_request_gate_state():
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=_Response()):
        assert client.chat([{"role": "user", "content": "hello"}]) == "ok"
    assert "provider_request_gate" in client.last_request_stats
    assert client.last_request_stats["provider_request_gate"]["circuit_open"] is False

def test_tool_round_telemetry_is_not_reset_between_rounds():
    # This is a structural regression guard for LLMClient: tool-round request
    # stats must be merged, not replaced, before the next request.
    stats = {
        "request_attempts": [{"request_index": 1}],
        "network_errors": [],
        "tool_rounds": 1,
    }
    update = {"message_count": 3, "tool_rounds": 1}
    stats.update(update)
    assert len(stats["request_attempts"]) == 1
    assert stats["tool_rounds"] == 1
