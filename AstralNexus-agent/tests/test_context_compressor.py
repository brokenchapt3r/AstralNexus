from AstralNexus.runtime.context_compressor import ContextCompressor, SUMMARY_MARKER


class FakeSummaryClient:
    model = "fake-aux"

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        assert kwargs.get("tools") is None
        return "Task snapshot\n- verified fact: keep the database constraint\nPending work\n- verify delivery"


def _messages():
    return (
        [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
        + [{"role": "assistant", "content": f"historical fact {i} " + "x" * 300} for i in range(8)]
        + [
            {"role": "assistant", "content": "calling tool", "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "latest evidence"},
            {"role": "user", "content": "latest request"},
        ]
    )


def test_auxiliary_model_summarizes_middle_and_preserves_tail():
    aux = FakeSummaryClient()
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=3)
    compacted, stats = compressor.compress(_messages(), context_length=1000)

    assert stats["compressed"] is True
    assert len(aux.calls) == 1
    assert any(SUMMARY_MARKER in item.get("content", "") for item in compacted)
    assert compacted[-1]["content"] == "latest request"
    # The assistant tool call and its tool result remain an indivisible tail.
    roles = [item["role"] for item in compacted]
    assert roles[-3:] == ["assistant", "tool", "user"]


def test_no_compression_below_threshold():
    aux = FakeSummaryClient()
    messages = [{"role": "user", "content": "short"}] * 5
    compressor = ContextCompressor(aux, threshold=0.9)
    compacted, stats = compressor.compress(messages, context_length=10000)
    assert compacted == messages
    assert stats["compressed"] is False
    assert not aux.calls


def test_auxiliary_failure_is_not_replaced_by_offline_summary():
    class Broken:
        model = "broken"
        def chat(self, *args, **kwargs):
            raise RuntimeError("network")

    compressor = ContextCompressor(Broken(), threshold=0.01, protect_last_n=2)
    import pytest

    with pytest.raises(RuntimeError, match="network"):
        compressor.compress(_messages(), context_length=1000)
