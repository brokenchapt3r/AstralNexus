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


def _rough_tokens(messages: List[Dict[str, Any]]) -> int:
    """Estimate token count using unified utility."""
    from AstralNexus.core.token_utils import rough_tokens_for_messages
    return rough_tokens_for_messages(messages)


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
        self.summary_client = summary_client
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
        if not self.enabled or len(messages) < 5 or context_length <= 0:
            return False
        # #16: respect failure-recovery cooldown
        if self.in_cooldown:
            return False
        estimated = _rough_tokens(messages) + max(0, extra_chars // 4)
        return estimated >= int(context_length * self.threshold)

    def compress(
        self,
        messages: List[Dict[str, Any]],
        context_length: int,
        *,
        extra_chars: int = 0,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        before = _rough_tokens(messages) + max(0, extra_chars // 4)
        stats: Dict[str, Any] = {
            "compressed": False,
            "before_tokens_estimate": before,
            "after_tokens_estimate": before,
            "summary_model": getattr(self.summary_client, "model", None),
            "target_ratio": self.target_ratio,
            "context_budget": context_length,
            "threshold_ratio": self.threshold,
            "threshold_tokens": int(context_length * self.threshold) if context_length > 0 else 0,
            "reason": "当前请求上下文低于有效压缩阈值",
        }
        if not self.should_compress(messages, context_length, extra_chars=extra_chars):
            return messages, stats

        stats["reason"] = "当前请求上下文达到有效压缩阈值，保留系统约束与最近工具结果，压缩历史中段"

        head_count = min(3, len(messages) - 2)
        
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
        source = self._render_source(middle)
        summary = self._summarize(source, middle)
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
        after = _rough_tokens(compacted) + max(0, extra_chars // 4)
        # Record only a real auxiliary-model compression event.
        import time
        self._compression_history.append({
            "timestamp": time.time(),
            "before_tokens": before,
            "after_tokens": after,
            "summarized_messages": len(middle),
            "reduction_ratio": (before - after) / max(1, before),
        })
        self._failure_count = 0
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
