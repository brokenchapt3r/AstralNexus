"""Permanent failures must not be resent by either streaming retry layer."""
from email.message import Message
from urllib.error import HTTPError

import pytest

from AstralNexus.runtime import llm_client as mod
from AstralNexus.runtime.provider_request_gate import ProviderRequestGate


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(mod, "provider_request_gate", ProviderRequestGate())
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "2")
    monkeypatch.setenv("ASTRALNEXUS_TURN_RETRIES", "1")
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    return mod.LLMClient(api_key="test", model="test", base_url="https://test.invalid/v1")


@pytest.mark.parametrize("entry", ["stream", "stream_chat"])
@pytest.mark.parametrize("error", [
    HTTPError("https://test.invalid", code, "rejected", Message(), None)
    for code in (400, 401, 403, 404, 422)
] + [ValueError("local invalid value"), AttributeError("local bug")])
def test_permanent_failure_is_attempted_once(client, monkeypatch, entry, error):
    requests = []
    events = []

    def fail(request, timeout=None):
        requests.append(request)
        raise error

    monkeypatch.setattr(mod, "llm_urlopen", fail)
    with pytest.raises(type(error)) as raised:
        result = getattr(client, entry)(
            [{"role": "user", "content": "test"}], event_callback=events.append,
        )
        if entry == "stream":
            list(result)
    assert raised.value is error
    assert len(requests) == 1
    assert not any(e["event_type"] in {
        "llm.stream.reconnected", "llm.stream.turn_retried",
    } for e in events)
    assert events[-1]["event_type"] == "llm.stream.failed"


@pytest.mark.parametrize("code", [429, 500, 503])
def test_transient_http_failure_still_recovers(client, monkeypatch, code):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            yield b'data: [DONE]\n'

    def respond(request, timeout=None):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError("https://test.invalid", code, "transient", Message(), None)
        return Response()

    monkeypatch.setattr(mod, "llm_urlopen", respond)
    assert client.stream_chat([{"role": "user", "content": "test"}]) == "ok"
    assert len(requests) == 2
