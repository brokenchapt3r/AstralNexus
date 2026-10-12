"""Fix Y：熔断打开时的有界等待——临时冷却不该判节点死刑。

实测来源（doc 22, r13b, 2026-09-15）：
  * PATCH-R002-E2E 死于 5m10s 静默流（Fix X 的靶子）；
  * PATCH-R002-SEC 在其后 21 秒死于
      "provider circuit open for https://dashscope...; retry in 25.99s"
    —— 它的最大化重试预算已经花在 2s/4s 退避睡眠上，最后一次尝试撞上仍然打开的
    熔断器，于是节点被写成 failed。冷却只剩 26 秒，却已经没有任何代码打算等它。

修复：主调用路径（流式与非流式）对**短**冷却做有界等待，且不消耗重连/重试预算；
冷却超过等待预算（默认 45s）或等待次数用尽时，行为与之前完全一致（照旧失败）。
"""
from __future__ import annotations

import itertools

import pytest

from AstralNexus.runtime import llm_client as mod
from AstralNexus.runtime.llm_client import LLMClient, LLMClientError
from AstralNexus.runtime.provider_request_gate import ProviderCircuitOpenError

_KEY_SEQ = itertools.count()


def _client(**kw) -> LLMClient:
    """每个用例独立 provider_key：provider_request_gate 是模块级单例。"""
    return LLMClient(
        api_key="k",
        base_url=f"https://example-{next(_KEY_SEQ)}.invalid/v1",
        model="test-model",
        timeout=600,
        **kw,
    )


def _rejecting_slot(monkeypatch, message: str, times: int) -> None:
    """让 request_slot 先按给定文案拒绝 times 次，之后交回真实实现。"""
    real = mod.provider_request_gate.request_slot
    left = {"n": times}

    def fake(key):
        if left["n"] > 0:
            left["n"] -= 1
            raise ProviderCircuitOpenError(message)
        return real(key)

    monkeypatch.setattr(mod.provider_request_gate, "request_slot", fake)


def _record_sleeps(monkeypatch) -> list:
    slept: list = []
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: slept.append(round(seconds, 3)))
    return slept


class _DoneResponse:
    """正常完成的 SSE：一帧 [DONE]。"""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([b"data: [DONE]\n"])


class _JsonResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return (
            b'{"choices":[{"message":{"content":"ok"}}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":1}}'
        )


def _events(client, monkeypatch, **kwargs):
    collected: list = []
    list(client.stream(
        [{"role": "user", "content": "hi"}],
        event_callback=collected.append,
        **kwargs,
    ))
    return [event["event_type"] for event in collected]


# ------------------------------------------------------------------ 流式路径

def test_stream_waits_out_short_cooldown_instead_of_failing_the_node(monkeypatch):
    monkeypatch.delenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("ASTRALNEXUS_CIRCUIT_MAX_WAITS", raising=False)
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")  # 不给普通重连机会
    client = _client()
    _rejecting_slot(
        monkeypatch,
        f"provider circuit open for {client.provider_key}; retry in 25.99s",
        times=1,
    )
    monkeypatch.setattr(mod, "urlopen", lambda request, timeout=None: _DoneResponse())
    slept = _record_sleeps(monkeypatch)

    kinds = _events(client, monkeypatch)

    assert "llm.stream.completed" in kinds, kinds
    assert "llm.stream.failed" not in kinds, kinds
    # 等待不是重连：普通重连预算必须原封不动
    assert "llm.stream.reconnected" not in kinds, kinds
    assert kinds.count("llm.stream.circuit_wait") == 1, kinds
    assert abs(sum(slept) - 27.99) < 0.01, slept  # 25.99 + 2.0 安全边距，等满


def test_cooldown_beyond_budget_still_fails_fast(monkeypatch):
    """超过等待预算的长冷却行为不变：立刻失败，绝不空等。"""
    monkeypatch.setenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", "10")
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    client = _client()
    _rejecting_slot(monkeypatch, "provider circuit open for x; retry in 600.0s", times=99)
    slept = _record_sleeps(monkeypatch)

    with pytest.raises(ProviderCircuitOpenError):
        _events(client, monkeypatch)

    assert slept == [], slept


def test_circuit_waits_are_bounded(monkeypatch):
    """一次流式调用能吸收的冷却次数有上限，用尽后照旧失败。"""
    monkeypatch.setenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", "45")
    monkeypatch.setenv("ASTRALNEXUS_CIRCUIT_MAX_WAITS", "1")
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    client = _client()
    _rejecting_slot(monkeypatch, "provider circuit open for x; retry in 5.0s", times=99)
    slept = _record_sleeps(monkeypatch)
    collected: list = []

    with pytest.raises(ProviderCircuitOpenError):
        list(client.stream(
            [{"role": "user", "content": "hi"}],
            event_callback=collected.append,
        ))

    kinds = [event["event_type"] for event in collected]
    assert kinds.count("llm.stream.circuit_wait") == 1, kinds
    assert "llm.stream.failed" in kinds, kinds
    assert abs(sum(slept) - 7.0) < 0.01, slept


def test_zero_budget_disables_the_wait(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", "0")
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    client = _client()
    _rejecting_slot(monkeypatch, "provider circuit open for x; retry in 5.0s", times=99)
    slept = _record_sleeps(monkeypatch)

    with pytest.raises(ProviderCircuitOpenError):
        _events(client, monkeypatch)

    assert slept == [], slept


def test_local_bug_is_never_waited_out(monkeypatch):
    """非熔断异常不得被这个机制吸收。"""
    monkeypatch.setenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", "45")
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "0")
    client = _client()
    monkeypatch.setattr(
        mod, "urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(AttributeError("local bug")),
    )
    slept = _record_sleeps(monkeypatch)
    collected: list = []

    with pytest.raises(AttributeError):
        list(client.stream(
            [{"role": "user", "content": "hi"}],
            event_callback=collected.append,
        ))

    assert slept == [], slept
    kinds = [event["event_type"] for event in collected]
    assert "llm.stream.circuit_wait" not in kinds, kinds


# ---------------------------------------------------------------- 非流式路径

def test_nonstream_waits_out_full_cooldown(monkeypatch):
    """非流式路径原本把 25.99s 冷却截成 2/4/8s 退避，重试预算耗尽后仍失败。"""
    monkeypatch.delenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", raising=False)
    client = _client()
    _rejecting_slot(
        monkeypatch,
        f"provider circuit open for {client.provider_key}; retry in 25.99s",
        times=1,
    )
    monkeypatch.setattr(mod, "urlopen", lambda request, timeout=None: _JsonResponse())
    slept = _record_sleeps(monkeypatch)

    assert client.chat([{"role": "user", "content": "hello"}]) == "ok"
    assert abs(sum(slept) - 27.99) < 0.01, slept


def test_nonstream_stop_request_interrupts_the_cooldown_wait(monkeypatch):
    """停机请求不得被冷却等待阻塞（Fix V 的教训）。

    chat() 在发请求前就已检查一次 cancel_check（llm_client.py:459），所以这里
    让停机信号在**等待开始之后**到达：断言等待只走了一个 0.5s 切片就退出，
    而不是空转 27.99s。
    """
    monkeypatch.delenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", raising=False)
    client = _client()
    _rejecting_slot(monkeypatch, "provider circuit open for x; retry in 25.99s", times=99)
    slept = _record_sleeps(monkeypatch)

    with pytest.raises(LLMClientError) as excinfo:
        client.chat(
            [{"role": "user", "content": "hello"}],
            cancel_check=lambda: len(slept) >= 1,
        )

    assert "interrupted while waiting out" in str(excinfo.value)
    assert slept == [0.5], slept  # 一个切片内响应停机


def test_nonstream_long_cooldown_keeps_the_old_capped_backoff(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", "10")
    client = _client()
    _rejecting_slot(monkeypatch, "provider circuit open for x; retry in 600.0s", times=99)
    slept = _record_sleeps(monkeypatch)

    with pytest.raises(LLMClientError):
        client.chat([{"role": "user", "content": "hello"}])

    # 旧行为：退避 2s/4s/8s，总和 14s，绝不等于整冷却
    assert slept == [2.0, 4.0, 8.0], slept