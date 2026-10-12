# -*- coding: utf-8 -*-
"""Fix BA —— 语义验收自己的传输故障，不得记成节点的 0 分。

`_semantic_goal` 用一个宽 `except Exception` 把**任何**失败都写成
`score=0.0, details="Semantic verification failed: ..."`。于是验证者自己的
网络超时、熔断打开，被当成"这个节点的目标覆盖为零"。

线上实证（r13b，运行时证据族连续三代，六个维度里五个满分 1.0，
第六个被压成 0.0，综合分都是同一个 0.8333 → replan_required）：

    R018      goal_coverage 0.0  "Network error from LLM API after 216.3s: The read operation timed out"
    R020/R021 goal_coverage 0.0  "provider circuit open after 3 retries: ..."

而 R021 的证据是**真跑出来的**：acceptance-rerun.json 174,740 B、log 300,743 B、
重启对照两轮真实 HTTP（WO-002 assigned→assigned、SP-001 on_hand 18→18）。
每一次这样的拒绝都烧掉一代恢复与一格 churn 预算——烧在网络故障上。

Fix AL 早就定下了正确处置：未取得判定即不记判定、节点退回重排验收、预算有界。
但 AL 只看得见**逃出 `verify_node`** 的异常；这个 handler 在它下面一层把异常吞了。

修法：用 Fix AM 的 `is_transport_interruption`（沿 __cause__/__context__ 链识别）分类，
传输故障重新抛出交给 AL；真正的验收失败（JSON 畸形、分数越界、blocking_gaps）
仍旧照原样记 0.0。
"""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.error import URLError

import pytest

from AstralNexus.runtime.llm_client import is_transport_interruption

# 线上逐字出现的两种失败
LIVE_READ_TIMEOUT = socket.timeout("The read operation timed out")
LIVE_NETWORK_WRAPPED = URLError(socket.timeout("The read operation timed out"))


# ---------------------------------------------------------------- 1. 分类边界
@pytest.mark.parametrize("exc", [
    LIVE_READ_TIMEOUT,
    LIVE_NETWORK_WRAPPED,
    ssl.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING"),
    ConnectionResetError("connection reset by peer"),
    http.client.IncompleteRead(b""),
    TimeoutError("timed out"),
])
def test_transport_failures_are_recognised(exc):
    assert is_transport_interruption(exc)


@pytest.mark.parametrize("exc", [
    ValueError("Verifier score must be finite and within [0, 1]"),
    KeyError("score"),
    TypeError("Verifier blocking_gaps must be a list"),
])
def test_genuine_verification_failures_are_not_transport(exc):
    """畸形 JSON / 越界分数 / 错误类型仍然是"验收失败"，必须照旧记 0.0。"""
    assert not is_transport_interruption(exc)


def test_circuit_open_wrapped_as_client_error_is_transport():
    """线上 R020/R021 的形态：熔断被重新包装后类型丢失，靠 cause 链识别。"""
    from AstralNexus.runtime.provider_request_gate import ProviderCircuitOpenError

    try:
        try:
            raise ProviderCircuitOpenError(
                "provider circuit open for https://dashscope.aliyuncs.com/compatible-mode/v1;"
                " retry in 21.99s"
            )
        except ProviderCircuitOpenError as cause:
            raise RuntimeError("provider circuit open after 3 retries") from cause
    except RuntimeError as exc:
        assert is_transport_interruption(exc)


# ---------------------------------------------------------------- 2. 源码契约
def test_handler_reraises_transport_instead_of_scoring_zero():
    import inspect

    from AstralNexus.core import verifier as mod

    src = inspect.getsource(mod.Verifier._verify_semantic_goal_coverage)
    # 分类在前，记 0 分在后；传输故障走 raise
    assert "is_transport_interruption(exc)" in src
    assert "raise" in src
    index_classify = src.index("is_transport_interruption(exc)")
    index_zero = src.index('score, details = 0.0, f"Semantic verification failed')
    assert index_classify < index_zero, "必须先分类再决定是否记 0 分"


def test_transport_interrupt_is_recorded_for_audit():
    import inspect

    from AstralNexus.core import verifier as mod

    src = inspect.getsource(mod.Verifier._verify_semantic_goal_coverage)
    assert "verifier.semantic_goal.transport_interrupted" in src


def test_genuine_failure_path_is_unchanged():
    """回归锚点：非传输异常仍然落到同一行 0.0，措辞不变。"""
    import inspect

    from AstralNexus.core import verifier as mod

    src = inspect.getsource(mod.Verifier._verify_semantic_goal_coverage)
    assert 'score, details = 0.0, f"Semantic verification failed: {exc}"' in src


def test_blocking_gaps_still_force_zero():
    """blocking_gaps 归零是刻意设计，不在本次修改范围内。"""
    import inspect

    from AstralNexus.core import verifier as mod

    src = inspect.getsource(mod.Verifier._verify_semantic_goal_coverage)
    assert "if blocking_gaps:" in src
    assert "score = 0.0" in src
