import json
from unittest.mock import patch

from AstralNexus.runtime.llm_client import LLMClient


class _StreamResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n'
        yield b'\n'
        yield b'data: [DONE]\n'


class _FlakyStreamResponse(_StreamResponse):
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"recovered"}}]}\n'
        raise ConnectionError("stream disconnected")


def test_llm_client_stream_yields_sse_events(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_API_KEY", "test-key")
    observed = []
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=_StreamResponse()):
        events = list(client.stream(
            [{"role": "user", "content": "hello"}],
            event_callback=observed.append,
        ))
    assert events[0]["choices"][0]["delta"]["content"] == "hello"
    assert client.last_request_stats["stream"] is True
    assert [event["event_type"] for event in observed] == [
        "llm.stream.started",
        "llm.stream.completed",
    ]


def test_llm_client_stream_reconnects_after_disconnect(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "1")
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    observed = []
    responses = [_FlakyStreamResponse(), _StreamResponse()]
    with patch(
        "AstralNexus.runtime.llm_client.urlopen",
        side_effect=lambda *_args, **_kwargs: responses.pop(0),
    ):
        events = list(client.stream(
            [{"role": "user", "content": "hello"}],
            event_callback=observed.append,
        ))
    assert len(events) == 2
    assert any(item["event_type"] == "llm.stream.reconnected" for item in observed)
