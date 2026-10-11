"""Fix X：流式路径的 chunk 看门狗、诚实的错误归因、熔断拒绝不再自我续期。

实测来源（doc 18, r13b N-008, 2026-09-15）：provider 接受请求后一字未发，约 5 分钟
后才关闭连接；轮次级重试又空等 5 分钟。全程代码无察觉，因为流式 urlopen 的
per-read 超时被设成了整请求预算（self.timeout，默认 600s）。trace 里记录的是
"stream ended without [DONE]"（协议不完整），而 stderr 里的真实原因是
SSL: UNEXPECTED_EOF_WHILE_READING —— SSE 读循环 `for raw_line in response:` 在对端
关闭时正常结束、不抛异常，于是传输层故障被写成了协议层故障。
"""
from __future__ import annotations

import http.client
import json
import itertools
import socket
import ssl

import pytest

from AstralNexus.runtime import llm_client as mod
from AstralNexus.runtime.llm_client import LLMClient, LLMClientError
from AstralNexus.runtime.provider_request_gate import (
    ProviderCircuitOpenError,
    provider_request_gate,
)


_KEY_SEQ = itertools.count()


def _client(**kw):
    """每个用例独立 provider_key。

    provider_request_gate 是模块级单例，状态按 provider_key 累积；共用一个 URL 时
    前一个用例打开的熔断会让后一个用例在 request_slot 处就被拒，根本走不到 urlopen。
    """
    return LLMClient(api_key="k",
                     base_url=f"https://example-{next(_KEY_SEQ)}.invalid/v1",
                     model="test-model", timeout=600, **kw)


# ---------------------------------------------------------------- 看门狗

def test_stream_socket_timeout_is_a_chunk_interval_not_the_whole_budget(monkeypatch):
    """默认 180s，且必须远小于 self.timeout(600s)。"""
    monkeypatch.delenv("ASTRALNEXUS_STREAM_CHUNK_TIMEOUT", raising=False)
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        raise socket.timeout("no chunk")

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    client = _client()
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert seen["timeout"] == 180.0
    assert seen["timeout"] < client.timeout


def test_chunk_timeout_is_configurable_and_capped_by_request_timeout(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_STREAM_CHUNK_TIMEOUT", "5000")
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        raise socket.timeout("no chunk")

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    client = _client()
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    # 不得超过整请求预算
    assert seen["timeout"] == 600.0


def test_chunk_timeout_has_a_floor(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_STREAM_CHUNK_TIMEOUT", "0")
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        raise socket.timeout("no chunk")

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    with pytest.raises(Exception):
        list(_client().stream([{"role": "user", "content": "hi"}]))
    assert seen["timeout"] == 5.0


# ---------------------------------------------------------------- 诚实归因

class _FakeResponse:
    """对端静默关闭：迭代正常结束，不抛异常 —— 正是线上那条路径。"""

    def __init__(self, lines):
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


def test_silent_peer_close_is_reported_as_peer_close_not_protocol(monkeypatch):
    monkeypatch.setattr(mod, "urlopen", lambda request, timeout=None: _FakeResponse([]))
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    with pytest.raises(LLMClientError) as excinfo:
        list(_client().stream([{"role": "user", "content": "hi"}]))
    text = str(excinfo.value)
    assert "closed by peer" in text, text
    assert "received 0 SSE event(s)" in text, text
    # 旧文案把传输故障说成协议故障，不得再出现
    assert "stream ended without [DONE]" not in text


def test_peer_close_message_counts_events_actually_received(monkeypatch):
    payload = b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
    monkeypatch.setattr(mod, "urlopen",
                        lambda request, timeout=None: _FakeResponse([payload, payload]))
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    with pytest.raises(LLMClientError) as excinfo:
        list(_client().stream([{"role": "user", "content": "hi"}]))
    assert "received 2 SSE event(s)" in str(excinfo.value)


# ---------------------------------------------------------------- 熔断计账

def _failures(key):
    return provider_request_gate.snapshot().get(key, {}).get("consecutive_failures", 0)


def test_circuit_rejection_does_not_count_as_a_provider_failure(monkeypatch):
    """活锁根因：熔断期间被拒的尝试曾以 network 记账并续期冷却。"""
    client = _client()
    key = client.provider_key
    before = _failures(key)

    class _Slot:
        def __enter__(self):
            raise ProviderCircuitOpenError("circuit open for test")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod.provider_request_gate, "request_slot", lambda k: _Slot())
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert _failures(key) == before, "熔断拒绝不得再计入 consecutive_failures"


def test_local_bug_does_not_open_the_circuit_against_a_healthy_provider(monkeypatch):
    client = _client()
    key = client.provider_key
    before = _failures(key)

    def boom(request, timeout=None):
        raise AttributeError("local bug, not the provider's fault")

    monkeypatch.setattr(mod, "urlopen", boom)
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert _failures(key) == before


@pytest.mark.parametrize("exc", [
    ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"),
    socket.timeout("no chunk"),
    http.client.IncompleteRead(b""),
])
def test_real_transport_failures_still_count_as_network(monkeypatch, exc):
    client = _client()
    key = client.provider_key
    before = _failures(key)
    monkeypatch.setattr(mod, "urlopen",
                        lambda request, timeout=None: (_ for _ in ()).throw(exc))
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert _failures(key) == before + 1, "真实传输故障仍必须计入熔断"


# ------------------------------------------- Fix X 回归：文案与判据必须解耦

def test_turn_retry_predicate_accepts_both_wordings():
    """Fix L 的轮次重试曾按字面匹配旧文案，Fix X 改文案后整类故障静默失去重试。"""
    from AstralNexus.runtime.llm_client import is_incomplete_stream_error

    assert is_incomplete_stream_error(
        LLMClientError("stream ended without [DONE]; response is incomplete"))
    assert is_incomplete_stream_error(
        LLMClientError("stream closed by peer before [DONE]; received 3 SSE event(s), "
                       "chunk timeout 180s; response is incomplete"))
    assert not is_incomplete_stream_error(
        LLMClientError("streamed response truncated by output limit; completion is unverified"))
    assert not is_incomplete_stream_error(ValueError("unrelated"))


def test_real_peer_close_still_triggers_a_turn_retry(monkeypatch):
    """走真实抛出点，而不是伪造异常文本 —— 这正是既有测试的盲区。

    第一轮对端静默关闭（无工具调用已执行），必须触发 llm.stream.turn_retried；
    第二轮正常完成。
    """
    calls = {"n": 0}
    done = b'data: [DONE]\n'
    text = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse([])          # 静默关闭：迭代直接结束
        return _FakeResponse([text, done])

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    events = []
    client = _client()
    answer = client.stream_chat(
        [{"role": "user", "content": "hi"}],
        event_callback=events.append,
    )
    kinds = [e.get("event_type") for e in events]
    assert "llm.stream.turn_retried" in kinds, kinds
    assert calls["n"] == 2
    assert answer is not None


# ------------------------------- Fix Z / Fix AA：截断不再是节点死刑

def test_no_output_cap_is_sent_by_default(monkeypatch):
    """Fix AA：不设上限 = 请求里根本不带 max_tokens 字段，由 provider 决定。"""
    monkeypatch.delenv("ASTRALNEXUS_ROLE_MAX_TOKENS", raising=False)
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        raise socket.timeout("stop here")

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    client = LLMClient(api_key="k", base_url="https://example-cap.invalid/v1",
                       model="m", timeout=600, max_tokens=None)
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert "max_tokens" not in seen["payload"], seen["payload"].get("max_tokens")


def test_explicit_cap_is_still_honoured(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        raise socket.timeout("stop here")

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    client = LLMClient(api_key="k", base_url="https://example-cap2.invalid/v1",
                       model="m", timeout=600, max_tokens=4096)
    with pytest.raises(Exception):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert seen["payload"]["max_tokens"] == 4096


def _length_capped_stream(chunks):
    """finish_reason=length 且无工具调用 —— 线上 PATCH-R003-RUNTIME 的死法。"""
    body = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n'
    return _FakeResponse([body] + list(chunks))


def test_first_truncation_nudges_instead_of_killing_the_node(monkeypatch):
    calls = {"n": 0}
    done = b'data: [DONE]\n'
    ok = b'data: {"choices":[{"delta":{"content":"short answer"}}]}\n'

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _length_capped_stream([done])
        return _FakeResponse([ok, done])

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)
    events = []
    client = LLMClient(api_key="k", base_url="https://example-tr.invalid/v1",
                       model="m", timeout=600)
    answer = client.stream_chat([{"role": "user", "content": "hi"}],
                                event_callback=events.append)
    kinds = [e.get("event_type") for e in events]
    assert "llm.output_truncation_nudge" in kinds, kinds
    assert calls["n"] == 2
    assert "short answer" in answer


def test_second_truncation_still_fails_honestly(monkeypatch):
    done = b'data: [DONE]\n'
    monkeypatch.setattr(mod, "urlopen",
                        lambda request, timeout=None: _length_capped_stream([done]))
    client = LLMClient(api_key="k", base_url="https://example-tr2.invalid/v1",
                       model="m", timeout=600)
    with pytest.raises(LLMClientError) as excinfo:
        client.stream_chat([{"role": "user", "content": "hi"}])
    text = str(excinfo.value)
    assert "truncated by output limit twice" in text, text
    assert "completion is unverified" in text


# ---------------------------- Fix AB：传输层中断也应享有轮次重试

def test_mid_turn_ssl_cut_is_reissued_once(monkeypatch):
    """线上 PATCH-R004-RUNTIME：9 次成功 2 次 SSL 断连，其中一次就断送了整个节点。

    Fix L 的幂等理由取决于「切断发生在工具执行之前」，与异常类无关；
    原 handler 只 except LLMClientError，裸 URLError 直接穿透。
    """
    from urllib.error import URLError
    calls = []
    client = LLMClient(api_key="k", base_url="https://example-ab.invalid/v1",
                       model="m", timeout=600)

    def stream(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield {"choices": [{"delta": {"content": "partial "}}]}
            raise URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))
        yield {"choices": [{"delta": {"content": "final answer"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    monkeypatch.setattr(client, "stream", stream)
    observed = []
    answer = client.stream_chat([{"role": "user", "content": "go"}],
                                event_callback=observed.append)
    assert answer == "final answer"
    assert len(calls) == 2
    assert [e.get("event_type") for e in observed].count("llm.stream.turn_retried") == 1


def test_mid_turn_transport_retry_is_bounded(monkeypatch):
    from urllib.error import URLError
    monkeypatch.setenv("ASTRALNEXUS_TURN_RETRIES", "0")
    calls = []
    client = LLMClient(api_key="k", base_url="https://example-ab2.invalid/v1",
                       model="m", timeout=600)

    def stream(messages, **kwargs):
        calls.append(1)
        yield {"choices": [{"delta": {"content": "x"}}]}
        raise URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))

    monkeypatch.setattr(client, "stream", stream)
    with pytest.raises(URLError):
        client.stream_chat([{"role": "user", "content": "go"}])
    assert len(calls) == 1


def test_http_error_is_not_reissued_as_a_transport_cut():
    """4xx/5xx 是 provider 对请求的裁决，不是连接被切断。"""
    from urllib.error import HTTPError
    from AstralNexus.runtime.llm_client import is_mid_turn_transport_error
    err = HTTPError("https://x", 400, "bad request", {}, None)
    assert not is_mid_turn_transport_error(err)
    assert is_mid_turn_transport_error(ssl.SSLError("eof"))
    assert is_mid_turn_transport_error(socket.timeout("t"))
    assert not is_mid_turn_transport_error(ValueError("unrelated"))
