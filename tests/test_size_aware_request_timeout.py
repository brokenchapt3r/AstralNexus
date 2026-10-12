# -*- coding: utf-8 -*-
"""Fix BB —— 读超时必须随请求自身大小伸缩，否则大请求每次都撞同一堵墙。

`chat()` 的每请求读超时原本是一个常数（默认 120s），与请求大小无关。
独立验证者的请求是**一条 2 消息的巨型请求**：整个证据包都在里面
（线上实测 message_chars 195,692–198,845、estimated_input_tokens 65,232–66,283）。

线上失败逐字都是：

    Network error from LLM API after 120.5s: The read operation timed out

**每次都恰好卡在超时上，从来不是连接错误**——说明服务端多半还在算，是我们先切了线。
压缩也救不了：`ContextCompressor.should_compress` 要求 `len(messages) >= 4`，
而验证者的请求只有 2 条消息，结构上永远不会被压缩。

后果：PATCH-R023 与 PATCH-R026 各自把 4 次验收尝试全部烧在同一堵 120s 墙上
（`verifier.transport_interrupted` attempt=4 budget=3 disposition=budget_exhausted），
最终被强制 `inconclusive` / 0.0。

**修法不是加次数**（项目规则：不能增加次数掩盖问题）——尝试预算一次没动。
改的是：一个请求按它自己实测的输入规模拿到成比例的读窗口，
仍有绝对上限，仍永远不超过节点剩余时间。
"""
from __future__ import annotations

import pytest

from AstralNexus.runtime.llm_client import LLMClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("ASTRALNEXUS_LLM_MAX_REQUEST_TIMEOUT", raising=False)
    monkeypatch.delenv("ASTRALNEXUS_LLM_TIMEOUT_SCALE_TOKENS", raising=False)
    obj = LLMClient.__new__(LLMClient)          # 不触发 __init__ 的配置解析
    obj.timeout = 120
    obj.last_request_stats = {}
    return obj


# ---------------------------------------------------------------- 1. 小请求行为不变
@pytest.mark.parametrize("stats", [
    {}, None, {"estimated_input_tokens": 0}, {"estimated_input_tokens": None},
    {"estimated_input_tokens": True},          # bool 不当数字
])
def test_small_or_unknown_requests_keep_the_base_timeout(client, stats):
    assert client._size_aware_timeout(stats) == 120.0


def test_base_timeout_is_a_floor(client):
    assert client._size_aware_timeout({"estimated_input_tokens": 1}) >= 120.0


# ---------------------------------------------------------------- 2. 线上那个请求
def test_the_live_verifier_request_gets_a_real_window(client):
    """66,283 token 的请求不该只有 120s。"""
    got = client._size_aware_timeout({"estimated_input_tokens": 66283})
    assert got > 300.0, got
    assert got <= 600.0


@pytest.mark.parametrize("tokens", [65232, 66283])
def test_both_live_sizes_exceed_the_old_wall(client, tokens):
    assert client._size_aware_timeout({"estimated_input_tokens": tokens}) > 120.5


def test_timeout_grows_monotonically_with_size(client):
    values = [client._size_aware_timeout({"estimated_input_tokens": n})
              for n in (0, 8_000, 32_768, 66_283, 120_000)]
    assert values == sorted(values)


# ---------------------------------------------------------------- 3. 有上限
def test_an_enormous_request_is_capped(client):
    assert client._size_aware_timeout({"estimated_input_tokens": 10_000_000}) == 600.0


def test_ceiling_is_configurable(client, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_LLM_MAX_REQUEST_TIMEOUT", "240")
    assert client._size_aware_timeout({"estimated_input_tokens": 10_000_000}) == 240.0


def test_a_ceiling_below_the_base_cannot_shorten_the_base(client, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_LLM_MAX_REQUEST_TIMEOUT", "5")
    assert client._size_aware_timeout({"estimated_input_tokens": 66283}) >= 120.0


@pytest.mark.parametrize("bad", ["", "abc", "nan-ish"])
def test_bad_ceiling_env_falls_back_to_default(client, monkeypatch, bad):
    monkeypatch.setenv("ASTRALNEXUS_LLM_MAX_REQUEST_TIMEOUT", bad)
    got = client._size_aware_timeout({"estimated_input_tokens": 66283})
    assert 120.0 <= got <= 600.0


# ---------------------------------------------------------------- 4. 关停开关
def test_scale_zero_disables_the_feature(client, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_LLM_TIMEOUT_SCALE_TOKENS", "0")
    assert client._size_aware_timeout({"estimated_input_tokens": 66283}) == 120.0


def test_bad_scale_env_falls_back_to_default(client, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_LLM_TIMEOUT_SCALE_TOKENS", "not-a-number")
    assert client._size_aware_timeout({"estimated_input_tokens": 66283}) > 120.0


def test_falls_back_to_last_request_stats(client):
    client.last_request_stats = {"estimated_input_tokens": 66283}
    assert client._size_aware_timeout(None) > 120.0


# ---------------------------------------------------------------- 5. 调用点契约
def test_chat_uses_the_size_aware_timeout_and_still_clamps_to_node_remaining():
    import inspect

    from AstralNexus.runtime import llm_client as mod

    src = inspect.getsource(mod.LLMClient.chat)
    assert "_size_aware_timeout(request_stats)" in src
    # 节点剩余时间仍是硬上限
    assert "node_remaining" in src
    # 旧的常数写法不得残留
    assert "min(float(self.timeout), node_remaining)" not in src


def test_attempt_budget_was_not_widened(tmp_path):
    """修的是单次请求的窗口，不是重试次数。"""
    from AstralNexus.runtime.llm_client import LLMClient
    from AstralNexus.runtime.run_context import RunSettings, RunContext
    for configured, expected in ((None, 3), ("1", 1)):
        env = {"ASTRALNEXUS_API_KEY": "test"}
        if configured is not None:
            env["ASTRALNEXUS_LLM_RETRIES"] = configured
        settings = RunSettings.capture(environment=env, config_path=tmp_path / "config.yaml")
        with RunContext(tmp_path, settings).scope():
            assert LLMClient(model="qwen-plus", interactive=False).max_retries == expected
