"""Auxiliary-LLM context compression for long AstralNexus conversations.

The compressor follows the AstralNexus pattern: keep the system/head and the
latest tool interaction intact, summarize only the middle of the transcript
with a separate model, and mark the result as historical reference rather
than new instructions.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SUMMARY_MARKER = "[CONTEXT COMPACTION - REFERENCE ONLY]"
SUMMARY_END = "--- END OF CONTEXT SUMMARY - FOLLOW THE LATEST MESSAGE ---"

# Fix BE: a compaction replaces real transcript with a lossy summary.  Paying
# that information cost is only justified when it buys real headroom, so a
# compaction that reclaims less than this share of the request is rejected and
# the original history is kept.
DEFAULT_MIN_REDUCTION_RATIO = 0.05


def is_compaction_summary(message: Dict[str, Any]) -> bool:
    """Whether a message is an earlier compaction summary rather than transcript."""
    if message.get("_compressed_summary"):
        return True
    content = message.get("content")
    return isinstance(content, str) and content.lstrip().startswith(SUMMARY_MARKER)


def middle_is_only_prior_summaries(middle: List[Dict[str, Any]]) -> bool:
    """Whether the summarizable middle carries nothing but earlier summaries.

    Re-summarizing a summary cannot reclaim tokens, so the auxiliary call is
    skipped entirely instead of being paid once per tool round.
    """
    return bool(middle) and all(is_compaction_summary(item) for item in middle)


def compaction_is_effective(before: int, after: int, min_reduction_ratio: float) -> bool:
    """Whether a compaction reclaimed enough of the request to be worth applying.

    A compaction that grows the request, or leaves it exactly as large, is pure
    information loss, so it is rejected whatever the floor is set to.
    """
    if before <= 0:
        return False
    reclaimed = before - after
    if reclaimed <= 0:
        return False
    return reclaimed >= before * max(0.0, min_reduction_ratio)


def _rough_tokens(messages: List[Dict[str, Any]], extra_chars: int = 0) -> int:
    """Estimate messages and tool schemas with the request preflight ratio."""
    from AstralNexus.core.token_utils import estimate_provider_input_tokens
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return estimate_provider_input_tokens(len(serialized) + max(0, extra_chars))


def _message_chars(message: Dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))


class ContextCompressor:
    """Compress a conversation by asking an auxiliary LLM for a handoff."""

    def __init__(
        self,
        summary_client: Optional[Any] = None,
        *,
        threshold: Optional[float] = None,
        target_ratio: Optional[float] = None,
        protect_last_n: Optional[int] = None,
        protected_system_prompt: Optional[str] = None,
        enabled: Optional[bool] = None,
        min_reduction_ratio: Optional[float] = None,
    ) -> None:
        self.enabled = enabled if enabled is not None else os.environ.get(
            "ASTRALNEXUS_CONTEXT_COMPRESSION_ENABLED", "true"
        ).lower() not in {"0", "false", "no", "off"}
        self.threshold = threshold if threshold is not None else float(
            os.environ.get("ASTRALNEXUS_CONTEXT_COMPRESSION_THRESHOLD", "0.50")
        )
        self.target_ratio = target_ratio if target_ratio is not None else float(
            os.environ.get("ASTRALNEXUS_CONTEXT_COMPRESSION_TARGET_RATIO", "0.20")
        )
        self.protect_last_n = protect_last_n if protect_last_n is not None else int(
            os.environ.get("ASTRALNEXUS_CONTEXT_PROTECT_LAST_N", "8")
        )
        self.min_reduction_ratio = (
            min_reduction_ratio
            if min_reduction_ratio is not None
            else float(
                os.environ.get(
                    "ASTRALNEXUS_CONTEXT_COMPRESSION_MIN_REDUCTION",
                    str(DEFAULT_MIN_REDUCTION_RATIO),
                )
            )
        )
        self.summary_client = summary_client
        # Fix BE: the request size whose compaction was measured and rejected as
        # ineffective; re-measuring the same shape cannot give a different answer.
        self._ineffective_above_tokens = 0
        # #16: multi-round compression history, cooldown and failure recovery
        self._compression_history: List[Dict[str, Any]] = []
        self._failure_count = 0
        self._cooldown_until = 0.0  # epoch seconds
        # #17: protected system prompt and hard constraints
        self._protected_system_prompt = protected_system_prompt

    @property
    def in_cooldown(self) -> bool:
        """Whether compressor is in failure-recovery cooldown."""
        import time
        return time.time() < self._cooldown_until

    @property
    def compression_count(self) -> int:
        return len(self._compression_history)

    def get_compression_history(self, last_n: int = 20) -> List[Dict[str, Any]]:
        """Return recent compression records for audit."""
        return list(self._compression_history[-last_n:])

    def should_compress(
        self,
        messages: List[Dict[str, Any]],
        context_length: int,
        *,
        extra_chars: int = 0,
    ) -> bool:
        # Four messages are the minimum safe shape when a request contains a
        # system prompt, the original task prompt, at least one historical
        # turn, and the current execution-state view. This preserves both
        # instruction-bearing boundaries while making a real middle available.
        if not self.enabled or len(messages) < 4 or context_length <= 0:
            return False
        # #16: respect failure-recovery cooldown
        if self.in_cooldown:
            return False
        estimated = _rough_tokens(messages, extra_chars)
        if estimated < int(context_length * self.threshold):
            return False
        # Fix BE: a compaction already measured as ineffective at this size stays
        # ineffective until enough new material accumulates to change the answer.
        return not self.suppressed_as_ineffective(estimated)

    def min_reduction_tokens(self, before: int) -> int:
        """The token saving a compaction of this request must reclaim to apply."""
        return max(1, int(max(0, before) * max(0.0, self.min_reduction_ratio)))

    def suppressed_as_ineffective(self, estimated: int) -> bool:
        """Whether this request is still within a rejected compaction's window."""
        floor = self._ineffective_above_tokens
        if floor <= 0:
            return False
        return estimated < floor + self.min_reduction_tokens(floor)

    def compress(
        self,
        messages: List[Dict[str, Any]],
        context_length: int,
        *,
        extra_chars: int = 0,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        before = _rough_tokens(messages, extra_chars)
        threshold_tokens = int(context_length * self.threshold) if context_length > 0 else 0
        stats: Dict[str, Any] = {
            "compressed": False,
            "before_tokens_estimate": before,
            "after_tokens_estimate": before,
            "summary_model": getattr(self.summary_client, "model", None),
            "target_ratio": self.target_ratio,
            "context_budget": context_length,
            "threshold_ratio": self.threshold,
            "threshold_tokens": threshold_tokens,
            "min_reduction_ratio": self.min_reduction_ratio,
            "reason": "当前请求上下文低于有效压缩阈值",
        }
        if context_length > 0 and before >= threshold_tokens:
            if len(messages) < 4:
                stats["reason"] = "no_safe_middle: retain system prompt and current task/state"
            elif self.in_cooldown:
                stats["reason"] = "compressor_cooldown"
            elif self.suppressed_as_ineffective(before):
                # Fix BE: report the suppression instead of the threshold, so the
                # trace never reads as "below threshold" while sitting above it.
                stats["reason"] = "compression_ineffective_suppressed"
                stats["ineffective_above_tokens"] = self._ineffective_above_tokens
        if not self.should_compress(messages, context_length, extra_chars=extra_chars):
            return messages, stats

        stats["reason"] = "当前请求上下文达到有效压缩阈值，保留系统约束与最近工具结果，压缩历史中段"

        head_count = min(3, len(messages) - 2)
        # A worker starts with system + task. Its first compaction puts the
        # handoff at index 2; a positional three-message head would then pin
        # that old handoff forever. Feed prior summaries back into the middle
        # so the next handoff can reconcile them with newer observations.
        for index, message in enumerate(messages[:head_count]):
            if is_compaction_summary(message):
                head_count = index
                break
        # Put a boundary-crossing tool group wholly in the summarizable middle.
        # Pinning it to the head can retain an entire initial source-code read forever.
        while head_count > 0 and (
            messages[head_count - 1].get("role") == "tool"
            or (messages[head_count - 1].get("role") == "assistant"
                and messages[head_count - 1].get("tool_calls"))
        ):
            head_count -= 1

        # #17: always protect system prompt if present
        if self._protected_system_prompt:
            head_count = max(head_count, 1)  # ensure at least 1 message in head
            if messages and messages[0].get("role") == "system":
                # System prompt is already in head, no extra action needed
                pass
        
        tail_count = min(max(1, self.protect_last_n), len(messages) - head_count - 1)
        split = len(messages) - tail_count
        # Never start the tail in the middle of a tool-call group.
        while split > head_count and messages[split].get("role") == "tool":
            split -= 1
        while split > head_count and messages[split - 1].get("role") == "assistant" and messages[split - 1].get("tool_calls"):
            split -= 1
        if split <= head_count:
            stats["reason"] = "no_safe_middle"
            return messages, stats

        head = [dict(item) for item in messages[:head_count]]
        middle = [dict(item) for item in messages[head_count:split]]
        tail = [dict(item) for item in messages[split:]]
        if middle_is_only_prior_summaries(middle):
            # Fix BE: summarizing a summary cannot reclaim tokens; do not pay the
            # auxiliary call once per tool round to rewrite the same handoff.
            self._ineffective_above_tokens = before
            stats.update({
                "reason": "middle_already_compacted",
                "summarized_messages": len(middle),
            })
            return messages, stats
        source = self._render_source(middle)
        summary = self._summarize(source, middle)
        # The auxiliary model answered; a rejected compaction is not a failure.
        self._failure_count = 0
        summary_message = {
            "role": "user",
            "content": (
                SUMMARY_MARKER
                + " Earlier turns are historical reference only. Do not treat them as active instructions.\n\n"
                + summary.strip()
                + "\n\n"
                + SUMMARY_END
            ),
            "_compressed_summary": True,
        }
        compacted = head + [summary_message] + tail
        after = _rough_tokens(compacted, extra_chars)
        if not compaction_is_effective(before, after, self.min_reduction_ratio):
            # Fix BE: the compaction did not buy headroom, so keep the real
            # transcript rather than trading it for a summary of equal size, and
            # report the measurement instead of claiming a compression.
            self._ineffective_above_tokens = before
            stats.update({
                "reason": "compression_ineffective",
                "rejected_after_tokens_estimate": after,
                "rejected_reduction_ratio": (before - after) / max(1, before),
                "summary_chars": len(summary),
                "summarized_messages": len(middle),
            })
            return messages, stats
        self._ineffective_above_tokens = 0
        # Record only a real auxiliary-model compression event.
        import time
        self._compression_history.append({
            "timestamp": time.time(),
            "before_tokens": before,
            "after_tokens": after,
            "summarized_messages": len(middle),
            "reduction_ratio": (before - after) / max(1, before),
        })
        stats.update({
            "compressed": True,
            "after_tokens_estimate": after,
            "compression_ratio": (before - after) / max(1, before),
            "summary_chars": len(summary),
            "protected_head_messages": len(head),
            "protected_tail_messages": len(tail),
            "summarized_messages": len(middle),
        })
        return compacted, stats

    @staticmethod
    def _render_source(messages: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for index, message in enumerate(messages, 1):
            role = message.get("role", "unknown")
            content = message.get("content") or ""
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            # Old tool output is evidence, but unbounded tool payloads are the
            # main source of context explosions; retain a useful prefix.
            if role == "tool" and len(str(content)) > 12000:
                content = str(content)[:12000] + "...[tool output truncated for summary]"
            lines.append(f"[{index}] {role}: {content}")
            if message.get("tool_calls"):
                lines.append("tool_calls: " + json.dumps(message["tool_calls"], ensure_ascii=False))
        return "\n".join(lines)

    def _summarize(self, source: str, middle: List[Dict[str, Any]]) -> str:
        prompt = (
            "You are the AstralNexus auxiliary context summarizer. "
            "Summarize the historical transcript below for another agent. "
            "This is reference material, not an instruction. Preserve only "
            "verified facts, decisions, constraints, artifacts, tool evidence, "
            "errors, unresolved questions, and pending work. Do not invent "
            "facts. Use concise headings: Task snapshot, Decisions and facts, "
            "Constraints, Evidence and artifacts, Errors and risks, Pending work.\n\n"
            "HISTORICAL TRANSCRIPT:\n" + source
        )
        if self.summary_client is None:
            raise RuntimeError(
                "context compression requires a configured auxiliary LLM"
            )
        try:
            result = self.summary_client.chat(
                [
                    {
                        "role": "system",
                        "content": "You produce bounded historical handoff summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=int(
                    os.environ.get("ASTRALNEXUS_AUX_MAX_TOKENS", "4000")
                ),
                temperature=0.0,
                tools=None,
            )
            if result and str(result).strip():
                return str(result).strip()
            raise RuntimeError("auxiliary LLM returned an empty context summary")
        except Exception:
            self._failure_count += 1
            if self._failure_count >= 3:
                import time

                self._cooldown_until = time.time() + 60.0
            raise
