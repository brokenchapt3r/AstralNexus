"""Fix BE: 一次换不回余量的压缩不得被当作压缩采纳。

现场证据（gen 34, PATCH-R028-CLAIM-REMEDIATION-EXEC-FROZEN，26 个工具轮里 23 次压缩）：

    19:21  before 66804 -> after 66786    省下 18 个 token
    19:13  before 83323 -> after 81725    省下 1.9%
    19:26  before 66195 -> after 63607    省下 3.9%

每一轮都付一次辅助模型调用（ASTRALNEXUS_AUX_MAX_TOKENS 默认 4000），
把真实转录换成摘要，却几乎换不回任何余量；下一轮仍在阈值之上，于是再压一次。
更糟的是：中段很短、体积在工具 schema 或状态视图上时，摘要比被替换的中段还长，
`after > before`——压缩把请求**撑大**，旧实现仍然返回这份更大的上下文并报告
`compressed: True`，`_compression_history` 里记下一条负的 reduction_ratio。

本文件把三件事钉住：
  1. 压缩后不比压缩前小的结果一律不采纳，原始历史保留；
  2. 拒绝要按实测值如实上报，不得报成"低于阈值"；
  3. 中段只剩早先的摘要时，连辅助调用都不必付。
"""
import json
from typing import Any, Dict, List, Tuple

import pytest

from AstralNexus.runtime.context_compressor import (
    DEFAULT_MIN_REDUCTION_RATIO,
    SUMMARY_MARKER,
    ContextCompressor,
    compaction_is_effective,
    is_compaction_summary,
    middle_is_only_prior_summaries,
)


class RecordingAux:
    """辅助模型桩：可指定摘要长度，并记录被调用了几次。"""

    model = "fake-aux"

    def __init__(self, summary: str = "handoff summary"):
        self.calls: List[Tuple[Any, Dict[str, Any]]] = []
        self.summary = summary

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.summary


def _conversation(history_chars: int, turns: int = 8):
    return (
        [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
        + [
            {"role": "assistant", "content": f"historical fact {i} " + "x" * history_chars}
            for i in range(turns)
        ]
        + [
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "latest evidence"},
            {"role": "user", "content": "latest request"},
        ]
    )


# --------------------------------------------------------------------------
# 1. compaction_is_effective：纯函数的判据
# --------------------------------------------------------------------------

def test_a_compaction_that_grows_the_request_is_never_effective():
    assert compaction_is_effective(1000, 1100, 0.0) is False
    assert compaction_is_effective(1000, 1100, 0.5) is False


def test_a_compaction_that_changes_nothing_is_not_effective_even_with_a_zero_floor():
    # 体积一样大的摘要是"净损失信息、换不回任何余量"，阈值设成 0 也不该采纳。
    assert compaction_is_effective(1000, 1000, 0.0) is False
    assert compaction_is_effective(1000, 999, 0.0) is True
    assert compaction_is_effective(1000, 1001, 0.0) is False


def test_the_floor_is_measured_against_the_request_not_against_the_middle():
    # 省下 18 个 token（现场 19:21 的实测量级）在 5% 阈值下不成立。
    assert compaction_is_effective(66804, 66786, DEFAULT_MIN_REDUCTION_RATIO) is False
    # 省下 10.8%（现场 19:11 的实测量级）成立。
    assert compaction_is_effective(89864, 80134, DEFAULT_MIN_REDUCTION_RATIO) is True


def test_a_negative_or_zero_floor_is_clamped_rather_than_inverting_the_judgement():
    assert compaction_is_effective(1000, 900, -1.0) is True
    assert compaction_is_effective(1000, 1000, -1.0) is False
    assert compaction_is_effective(1000, 1001, -1.0) is False


def test_an_empty_request_cannot_be_compacted_effectively():
    assert compaction_is_effective(0, 0, 0.05) is False
    assert compaction_is_effective(-5, -10, 0.05) is False


# --------------------------------------------------------------------------
# 2. 早先的摘要如何被识别
# --------------------------------------------------------------------------

def test_a_message_carrying_the_compaction_flag_is_a_prior_summary():
    assert is_compaction_summary({"role": "user", "content": "x", "_compressed_summary": True}) is True


def test_a_message_opening_with_the_compaction_marker_is_a_prior_summary():
    assert is_compaction_summary({"role": "user", "content": SUMMARY_MARKER + " earlier"}) is True
    assert is_compaction_summary({"role": "user", "content": "  \n" + SUMMARY_MARKER}) is True


def test_ordinary_transcript_and_non_string_content_are_not_prior_summaries():
    assert is_compaction_summary({"role": "assistant", "content": "real work"}) is False
    assert is_compaction_summary({"role": "tool", "content": [{"type": "text"}]}) is False
    assert is_compaction_summary({"role": "user", "content": "见 " + SUMMARY_MARKER}) is False


def test_an_empty_middle_is_not_treated_as_already_compacted():
    # 空中段应当走别的分支（no_safe_middle），不要被这条捷径吞掉。
    assert middle_is_only_prior_summaries([]) is False


def test_a_middle_holding_any_real_turn_still_deserves_the_auxiliary_call():
    middle = [
        {"role": "user", "content": SUMMARY_MARKER + " earlier", "_compressed_summary": True},
        {"role": "assistant", "content": "a new tool round worth folding in"},
    ]
    assert middle_is_only_prior_summaries(middle) is False


def test_a_middle_of_nothing_but_summaries_is_already_compacted():
    middle = [
        {"role": "user", "content": SUMMARY_MARKER + " a", "_compressed_summary": True},
        {"role": "user", "content": SUMMARY_MARKER + " b", "_compressed_summary": True},
    ]
    assert middle_is_only_prior_summaries(middle) is True


# --------------------------------------------------------------------------
# 3. compress()：拒绝路径的端到端行为
# --------------------------------------------------------------------------

def test_a_compaction_that_grows_the_request_keeps_the_original_history():
    # 中段极短而 extra_chars 很大：摘要比被替换的中段还长。
    aux = RecordingAux("S" * 4000)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "tiny"},
        {"role": "user", "content": "latest"},
    ]
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=1)
    compacted, stats = compressor.compress(messages, context_length=100000, extra_chars=60000)

    assert stats["compressed"] is False
    assert stats["reason"] == "compression_ineffective"
    assert stats["rejected_after_tokens_estimate"] > stats["before_tokens_estimate"]
    # 真实转录一条不少地留下来了。
    assert compacted == messages
    assert not any(SUMMARY_MARKER in str(item.get("content", "")) for item in compacted)


def test_a_rejected_compaction_reports_the_measurement_instead_of_the_threshold():
    aux = RecordingAux("S" * 4000)
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=1)
    _, stats = compressor.compress(
        [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "tiny"},
            {"role": "user", "content": "latest"},
        ],
        context_length=100000,
        extra_chars=60000,
    )
    # 不得报成"低于有效压缩阈值"——请求明明在阈值之上。
    assert stats["before_tokens_estimate"] >= stats["threshold_tokens"]
    assert "低于" not in stats["reason"]
    assert stats["min_reduction_ratio"] == compressor.min_reduction_ratio
    assert stats["rejected_reduction_ratio"] < stats["min_reduction_ratio"]
    assert stats["summary_chars"] == 4000


def test_a_rejected_compaction_is_not_recorded_as_a_compression_event():
    aux = RecordingAux("S" * 4000)
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=1)
    compressor.compress(
        [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "tiny"},
            {"role": "user", "content": "latest"},
        ],
        context_length=100000,
        extra_chars=60000,
    )
    assert compressor.compression_count == 0
    assert compressor.get_compression_history() == []


def test_a_reduction_below_the_floor_is_rejected_although_it_is_a_reduction():
    # 真的省下了一点，但不足以抵掉"把真实转录换成摘要"的信息代价。
    messages = _conversation(history_chars=400)
    aux = RecordingAux("S" * 100)
    compressor = ContextCompressor(
        aux, threshold=0.01, protect_last_n=3, min_reduction_ratio=0.99,
    )
    _, stats = compressor.compress(messages, context_length=100000)
    assert stats["compressed"] is False
    assert stats["reason"] == "compression_ineffective"
    # 头段与尾段本身就占掉相当体积，任何真实压缩都省不到 99%。
    assert 0 < stats["rejected_reduction_ratio"] < 0.99


def test_a_real_reduction_is_still_applied_and_recorded():
    messages = _conversation(history_chars=3000)
    aux = RecordingAux("S" * 200)
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=3)
    compacted, stats = compressor.compress(messages, context_length=100000)

    assert stats["compressed"] is True
    assert stats["after_tokens_estimate"] < stats["before_tokens_estimate"]
    assert stats["compression_ratio"] >= compressor.min_reduction_ratio
    assert any(SUMMARY_MARKER in str(item.get("content", "")) for item in compacted)
    assert compressor.compression_count == 1
    assert compacted[-1]["content"] == "latest request"


# --------------------------------------------------------------------------
# 4. 不重复付同一次无效测量
# --------------------------------------------------------------------------

def _ineffective_call(compressor, aux, *, extra_chars=60000):
    return compressor.compress(
        [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "tiny"},
            {"role": "user", "content": "latest"},
        ],
        context_length=100000,
        extra_chars=extra_chars,
    )


def test_the_same_request_shape_is_not_measured_a_second_time():
    aux = RecordingAux("S" * 4000)
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=1)
    _ineffective_call(compressor, aux)
    assert len(aux.calls) == 1

    _, stats = _ineffective_call(compressor, aux)
    # 第二轮不再付辅助调用，而且把抑制的理由说清楚。
    assert len(aux.calls) == 1
    assert stats["compressed"] is False
    assert stats["reason"] == "compression_ineffective_suppressed"
    assert stats["ineffective_above_tokens"] > 0


def test_enough_new_material_lifts_the_suppression():
    aux = RecordingAux("S" * 4000)
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=1)
    _ineffective_call(compressor, aux)
    before = compressor._ineffective_above_tokens
    assert compressor.suppressed_as_ineffective(before) is True
    assert compressor.suppressed_as_ineffective(before + compressor.min_reduction_tokens(before)) is False

    # 请求长大了一个 floor 以上，值得再量一次。
    _ineffective_call(compressor, aux, extra_chars=200000)
    assert len(aux.calls) == 2


def test_suppression_is_cleared_by_a_compaction_that_works():
    compressor = ContextCompressor(
        RecordingAux("S" * 4000), threshold=0.01, protect_last_n=1,
    )
    _ineffective_call(compressor, RecordingAux(), extra_chars=20000)
    floor = compressor._ineffective_above_tokens
    assert floor > 0

    # 下一轮请求大得多（越过抑制窗口），而且这次压缩真的换回了余量。
    compressor.summary_client = RecordingAux("S" * 200)
    messages = _conversation(6000)
    _, stats = compressor.compress(messages, context_length=1000000)
    assert stats["before_tokens_estimate"] > floor + compressor.min_reduction_tokens(floor)
    assert stats["compressed"] is True
    assert compressor._ineffective_above_tokens == 0


def test_a_fresh_compressor_suppresses_nothing():
    compressor = ContextCompressor(RecordingAux())
    assert compressor.suppressed_as_ineffective(10**9) is False


def test_an_already_compacted_middle_skips_the_auxiliary_call_entirely():
    aux = RecordingAux("S" * 200)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "goal"},
        {"role": "user", "content": SUMMARY_MARKER + " earlier " + "x" * 4000,
         "_compressed_summary": True},
        {"role": "user", "content": "latest request"},
    ]
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=1)
    compacted, stats = compressor.compress(messages, context_length=100000)

    assert aux.calls == []
    assert stats["compressed"] is False
    assert stats["reason"] == "middle_already_compacted"
    assert compacted == messages


# --------------------------------------------------------------------------
# 5. 可配置性与既有不变式
# --------------------------------------------------------------------------

def test_the_floor_is_configurable_by_environment(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_CONTEXT_COMPRESSION_MIN_REDUCTION", "0.25")
    assert ContextCompressor(RecordingAux()).min_reduction_ratio == pytest.approx(0.25)


def test_an_explicit_floor_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_CONTEXT_COMPRESSION_MIN_REDUCTION", "0.25")
    compressor = ContextCompressor(RecordingAux(), min_reduction_ratio=0.01)
    assert compressor.min_reduction_ratio == pytest.approx(0.01)


def test_the_floor_never_turns_a_below_threshold_request_into_a_compression():
    aux = RecordingAux()
    compressor = ContextCompressor(aux, threshold=0.99, min_reduction_ratio=0.0)
    _, stats = compressor.compress(_conversation(10), context_length=10**7)
    assert stats["compressed"] is False
    assert stats["reason"] == "当前请求上下文低于有效压缩阈值"
    assert aux.calls == []


def test_the_summary_still_carries_the_reference_only_framing_when_applied():
    compacted, stats = ContextCompressor(
        RecordingAux("S" * 200), threshold=0.01, protect_last_n=3,
    ).compress(_conversation(3000), context_length=100000)
    assert stats["compressed"] is True
    summary = next(item for item in compacted if item.get("_compressed_summary"))
    assert SUMMARY_MARKER in summary["content"]
    assert "historical reference only" in summary["content"]
