from AstralNexus.runtime.context_compressor import ContextCompressor, SUMMARY_MARKER


class FakeSummaryClient:
    model = "fake-aux"

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        assert kwargs.get("tools") is None
        return "Task snapshot\n- verified fact: keep the database constraint\nPending work\n- verify delivery"


def test_repeated_compaction_refreshes_prior_summary_instead_of_pinning_it():
    class UpdatingSummaryClient(FakeSummaryClient):
        def chat(self, messages, **kwargs):
            super().chat(messages, **kwargs)
            return f"handoff-version-{len(self.calls)}"

    def tool_history(start, end):
        history = []
        for index in range(start, end):
            call_id = f"read-{index}"
            history.extend([
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "file_read", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": call_id,
                 "content": f"observed-version-{index}: " + "x" * 2000},
            ])
        return history

    aux = UpdatingSummaryClient()
    compressor = ContextCompressor(aux, threshold=0.01, protect_last_n=2)
    instructions = [{"role": "system", "content": "rules"},
                    {"role": "user", "content": "create executable tests"}]
    first, stats = compressor.compress(instructions + tool_history(0, 5), 1000)
    assert stats["compressed"]
    second, stats = compressor.compress(first + tool_history(5, 10), 1000)
    assert stats["compressed"]
    summaries = [m for m in second if SUMMARY_MARKER in m.get("content", "")]
    assert len(summaries) == 1
    assert "handoff-version-2" in summaries[0]["content"]
    assert "handoff-version-1" in aux.calls[1][0][1]["content"]
    assert second[:2] == instructions


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


def test_compression_uses_the_same_conservative_estimate_as_request_preflight():
    import json

    from AstralNexus.runtime.request_sanitizer import _token_estimate
    from AstralNexus.core.token_utils import rough_tokens_for_messages

    messages = [
        {"role": "system", "content": "Keep requirements and verified facts."},
        {"role": "user", "content": "Verify the submitted evidence."},
        *[
            {"role": "assistant", "content": "验收证据与结果摘要。" * 900}
            for _ in range(4)
        ],
    ]
    serialized_chars = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    request_estimate = _token_estimate(serialized_chars)
    compressor_estimate = rough_tokens_for_messages(messages)
    aux = FakeSummaryClient()

    compacted, stats = ContextCompressor(aux).compress(
        messages, context_length=request_estimate * 2,
    )

    assert compressor_estimate == request_estimate
    assert stats["before_tokens_estimate"] == request_estimate
    assert stats["compressed"] is True
    assert any(SUMMARY_MARKER in item.get("content", "") for item in compacted)
    assert len(aux.calls) == 1


def test_tool_schema_chars_use_same_estimate_and_can_trigger_compression():
    import json

    from AstralNexus.runtime.request_sanitizer import _token_estimate

    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "inspect"},
        *[{"role": "assistant", "content": "prior result"} for _ in range(4)],
    ]
    tools = [{"type": "function", "function": {"name": "inspect", "description": "工具说明。" * 6000}}]
    message_chars = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    tool_chars = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    request_estimate = _token_estimate(message_chars + tool_chars)
    aux = FakeSummaryClient()

    compressor = ContextCompressor(aux)
    # 本用例的本意：工具 schema 的体积必须与请求预检用同一把尺子计入压缩预算，
    # 因此它**能够触发**压缩尝试——这条不变式原样保留（须在尝试之前问，
    # 因为一次被判无效的尝试会抑制紧接着的同尺寸重复测量）。
    assert compressor.should_compress(
        messages, request_estimate * 2, extra_chars=tool_chars,
    ) is True

    compacted, stats = compressor.compress(
        messages, context_length=request_estimate * 2, extra_chars=tool_chars,
    )

    assert stats["before_tokens_estimate"] == request_estimate
    assert len(aux.calls) == 1

    # Fix BE：触发不等于应当采纳。此处正文只有 4 条短消息，体积全在工具 schema 上，
    # 把中段换成摘要反而把请求**撑大**（实测 10109 -> 10206 token），
    # 旧行为却把这份更大的上下文当作"压缩成功"返回。现在它被量出来并拒绝。
    assert stats["compressed"] is False
    assert stats["reason"] == "compression_ineffective"
    assert stats["rejected_after_tokens_estimate"] > stats["before_tokens_estimate"]
    assert stats["rejected_reduction_ratio"] < 0
    assert compacted == messages
    assert not any(SUMMARY_MARKER in str(item.get("content", "")) for item in compacted)


def test_compact_three_message_context_can_compress_safe_middle_turn():
    messages = [
        {"role": "system", "content": "Preserve these requirements."},
        {"role": "user", "content": "Original task and acceptance contract."},
        {"role": "assistant", "content": "historical evidence " + "x" * 18000},
        {"role": "user", "content": "Continue the current task."},
    ]
    aux = FakeSummaryClient()
    compacted, stats = ContextCompressor(aux).compress(
        messages, context_length=10000,
    )

    assert stats["compressed"] is True
    assert stats["summarized_messages"] == 1
    assert compacted[:2] == messages[:2]
    assert compacted[-1] == messages[-1]
    assert any(SUMMARY_MARKER in item.get("content", "") for item in compacted)


def test_two_message_oversized_prompt_is_reported_uncompressed_without_losing_task():
    messages = [
        {"role": "system", "content": "system constraints"},
        {"role": "user", "content": "current task " + "x" * 18000},
    ]
    compacted, stats = ContextCompressor(FakeSummaryClient()).compress(
        messages, context_length=10000,
    )
    assert compacted == messages
    assert stats["compressed"] is False
    assert stats["reason"].startswith("no_safe_middle")


def test_stream_context_charges_state_to_budget_but_keeps_it_out_of_compression():
    """Fix BC 后的不变式（原名 ..._includes_current_execution_state_before_compression）。

    原设计把执行状态视图**放进**可压缩列表，好让压缩预算算上它的体积。预算的意图是对的，
    但这让压缩器可以把这份"当前文件摘要"总结掉或挪位置，工作者于是拿着过期的世界视图干活。
    Fix BC 改为：把状态的体积以 extra_chars 计入压缩预算，但状态本身在压缩**之后**追加，
    因此永远是最后一条、永远不进可压缩中段。

    Fix BE 后又修正一处：本用例的中段换成摘要反而更大，压缩被量出来并拒绝，
    因此断言改为"压缩被拒绝且报告了实测值"；头两条原样保留、状态在最后逐条保留，
    只修正了一处口径错误：estimated_input_tokens 是 message_chars + tool_chars，
    原断言只算了 message_chars，靠取整恰好相等而通过，体积一变就会差 1 个 token。
    """
    from AstralNexus.runtime.context_compressor import ContextCompressor
    from AstralNexus.runtime.llm_client import LLMClient

    client = LLMClient(
        model="deepseek-v4.1-flash", api_key="test-key",
        base_url="https://example.invalid/v1", interactive=False,
        context_budget=10000,
    )
    client._context_compressor = ContextCompressor(
        FakeSummaryClient(), threshold=0.5, protect_last_n=1,
    )
    messages = [
        {"role": "system", "content": "system constraints"},
        {"role": "user", "content": "original task and acceptance contract"},
        {"role": "assistant", "content": "older completed turn"},
        {"role": "assistant", "content": "old state " + "x" * 1200},
    ]
    state = {"role": "user", "content": "current execution state " + "s" * 18000}

    compacted, _, stats = client._prepare_stream_context(messages, None, state)

    # Fix BE：本用例的中段只有一条短消息，摘要比它还长（实测 6489 -> 6584 token），
    # 压缩换不回余量，因此被量出来并拒绝——原断言 compressed is True 记录的是
    # "把更大的上下文当成压缩成功"的旧行为。Fix BC 的三条实质不变式照旧成立。
    assert client.last_compression_stats["compressed"] is False
    assert client.last_compression_stats["reason"] == "compression_ineffective"
    assert (
        client.last_compression_stats["rejected_after_tokens_estimate"]
        > client.last_compression_stats["before_tokens_estimate"]
    )
    assert compacted[:2] == messages[:2]
    assert compacted[-1]["content"] == state["content"]
    # 状态没有被压缩器动过：原样出现，而不是被总结成别的文本
    assert compacted[-1] == state
    # 中段里不得再出现状态
    assert all(item != state for item in compacted[:-1])
    assert stats["estimated_input_tokens"] == _token_estimate_for(compacted, tools=None)


def _token_estimate_for(messages, tools=None):
    """与 prepare_request 同口径：estimated_input_tokens = _token_estimate(消息字符 + 工具字符)。"""
    import json

    from AstralNexus.runtime.request_sanitizer import _token_estimate

    message_chars = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    tool_chars = len(json.dumps(tools or [], ensure_ascii=False, separators=(",", ":")))
    return _token_estimate(message_chars + tool_chars)


def test_auxiliary_failure_is_not_replaced_by_offline_summary():
    class Broken:
        model = "broken"
        def chat(self, *args, **kwargs):
            raise RuntimeError("network")

    compressor = ContextCompressor(Broken(), threshold=0.01, protect_last_n=2)
    import pytest

    with pytest.raises(RuntimeError, match="network"):
        compressor.compress(_messages(), context_length=1000)


def test_compression_summarizes_first_tool_group_without_pinning_source_forever():
    first = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "tool_calls": [{"id": "first", "function": {"name": "file_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "first", "content": "original file"},
    ]
    messages = first + [{"role": "user", "content": "history" * 1000} for _ in range(8)]
    compacted, stats = ContextCompressor(FakeSummaryClient(), threshold=.01, protect_last_n=2).compress(messages, 1000)
    assert stats["compressed"]
    assert compacted[:2] == first[:2]
    assert first[2] not in compacted
    assert first[3] not in compacted
    assert stats["protected_head_messages"] == 2
