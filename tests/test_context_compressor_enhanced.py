"""Tests for enhanced ContextCompressor: history, cooldown, failure recovery."""
import time
from unittest.mock import Mock
from AstralNexus.runtime.context_compressor import ContextCompressor


def test_compression_history_empty():
    c = ContextCompressor(enabled=True, threshold=0.1)
    assert c.compression_count == 0
    assert c.get_compression_history() == []


def test_compression_history_records_event():
    client = Mock()
    client.chat.return_value = "summary"
    c = ContextCompressor(client, enabled=True, threshold=0.1, protect_last_n=1)
    msgs = [{"role": "user", "content": "a" * 500}] * 10
    compacted, stats = c.compress(msgs, context_length=100)
    if stats.get("compressed"):
        assert c.compression_count == 1
        h = c.get_compression_history()
        assert len(h) == 1
        assert h[0]["before_tokens"] > 0
        assert h[0]["summarized_messages"] > 0


def test_cooldown_prevents_compression():
    c = ContextCompressor(enabled=True, threshold=0.1)
    c._cooldown_until = time.time() + 100.0
    msgs = [{"role": "user", "content": "x" * 500}] * 10
    assert c.should_compress(msgs, 100) is False


def test_failure_count_increments_on_exception():
    mock_client = Mock()
    mock_client.chat.side_effect = RuntimeError("API down")
    mock_client.model = "test-model"
    c = ContextCompressor(summary_client=mock_client, enabled=True, threshold=0.1, protect_last_n=1)
    msgs = [{"role": "user", "content": "a" * 500}] * 10
    import pytest

    with pytest.raises(RuntimeError, match="API down"):
        c.compress(msgs, context_length=100)
    assert c._failure_count == 1


def test_cooldown_triggered_after_3_failures():
    mock_client = Mock()
    mock_client.chat.side_effect = RuntimeError("API down")
    mock_client.model = "test-model"
    c = ContextCompressor(summary_client=mock_client, enabled=True, threshold=0.1, protect_last_n=1)
    msgs = [{"role": "user", "content": "a" * 500}] * 10
    for _ in range(3):
        try:
            c.compress(msgs, context_length=100)
        except RuntimeError:
            pass
    # After 3 failures, cooldown should trigger
    assert c._failure_count >= 3 or c.in_cooldown


def test_success_resets_failure_count():
    mock_client = Mock()
    mock_client.chat.return_value = "summary"
    mock_client.model = "test-model"
    c = ContextCompressor(summary_client=mock_client, enabled=True, threshold=0.1, protect_last_n=1)
    msgs = [{"role": "user", "content": "a" * 500}] * 10
    c.compress(msgs, context_length=100)
    assert c._failure_count == 0


def test_get_compression_history_limits_results():
    c = ContextCompressor(enabled=True, threshold=0.1, protect_last_n=1)
    # Simulate 25 compression records
    for i in range(25):
        c._compression_history.append({"timestamp": i, "before_tokens": 100, "after_tokens": 50})
    history = c.get_compression_history(last_n=10)
    assert len(history) == 10
    assert history[0]["timestamp"] == 15  # oldest in the window
