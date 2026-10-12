"""Dependency-light retention: bounded model-facing output with exact omission metadata.

Port of ``@deepseek-ai/dsh-output-retention`` (commit 5badb15). The library
owns ONLY "what did we keep, what did we omit?" — business semantics (file
grouping, line numbers, exit codes, provider error states, spill files, prose)
stay in tool code. ``truncated`` means "the retainer omitted otherwise-available
content because of a budget", never "the upstream was incomplete".

Contract kept:
- ``ItemRetainer``: head retention of ordered logical units; ``push`` reports a
  per-unit decision; ``finish`` reports exact ``seen``/``kept``/``omitted``.
- ``TextRetainer``: byte-oriented ``head``/``tail``/``head_tail`` budgets over
  chunks (bytes or UTF-8 str); holds at most prefixCap + tailBytes + one chunk;
  ``finish`` trims a partial UTF-8 codepoint at each cut only when the budget
  actually omitted bytes, so no replacement char is introduced at boundaries
  (genuinely malformed interiors still decode with U+FFFD, as TextDecoder does).
- ``describe_omitted``: standardized false-precision-safe wording — ``exact``
  prints the count, ``unknown`` prints none, ``none`` is the empty string.
- ``format_retention_notice``: library omission clause + tool-supplied
  recovery guidance, each half may be empty, joined with a single space.
- ``truncate_without_splitting_surrogate_pair``: caps at N UTF-16 code units;
  a cut inside a surrogate pair drops the unpaired high surrogate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Union


def _assert_budget(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class Omitted:
    """Omission metadata: none / exact count / deliberately unknown."""
    kind: str  # "none" | "exact" | "unknown"
    count: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind not in ("none", "exact", "unknown"):
            raise ValueError(f"omitted kind must be none/exact/unknown, got {self.kind!r}")
        if self.kind == "exact" and (not isinstance(self.count, int) or self.count < 0):
            raise ValueError("exact omission requires a non-negative count")


NONE_OMITTED = Omitted(kind="none")


@dataclass(frozen=True)
class PushDecision:
    kept: bool
    truncated: bool


@dataclass(frozen=True)
class RetainedItems:
    items: List[Any]
    truncated: bool
    seen: int
    kept: int
    omitted: Omitted


class ItemRetainer:
    """Bounds an ordered stream of logical units, keeping the first ``max_items``."""

    def __init__(self, strategy: dict):
        if not isinstance(strategy, dict) or strategy.get("kind", "head") != "head":
            raise ValueError("item retention v1 supports only the head strategy")
        max_items = strategy.get("max_items", strategy.get("maxItems"))
        _assert_budget(max_items, "max_items")
        self.max_items: int = int(max_items)
        self._items: List[Any] = []
        self._seen = 0
        self._omitted_count = 0

    def push(self, item: Any) -> PushDecision:
        self._seen += 1
        if len(self._items) < self.max_items:
            self._items.append(item)
            return PushDecision(kept=True, truncated=False)
        self._omitted_count += 1
        return PushDecision(kept=False, truncated=True)

    def finish(self) -> RetainedItems:
        truncated = self._omitted_count > 0
        return RetainedItems(
            items=self._items, truncated=truncated, seen=self._seen,
            kept=len(self._items),
            omitted=Omitted(kind="exact", count=self._omitted_count) if truncated else NONE_OMITTED)


def _trim_trailing_partial_utf8(data: bytes) -> bytes:
    """Drop a trailing incomplete UTF-8 sequence so a prefix cut stays clean."""
    index = len(data) - 1
    while index >= 0 and (data[index] & 0xC0) == 0x80 and len(data) - index <= 3:
        index -= 1
    if index < 0:
        return data
    lead = data[index]
    expected = 1 if lead < 128 else 2 if lead < 224 else 3 if lead < 240 else 4 if lead < 248 else 0
    if expected == 0:
        return data
    return data[:index] if len(data) - index < expected else data


def _trim_leading_continuation_utf8(data: bytes) -> bytes:
    """Drop leading continuation bytes so a suffix cut starts on a lead byte."""
    index = 0
    while index < len(data) and (data[index] & 0xC0) == 0x80:
        index += 1
    return data[index:]


@dataclass(frozen=True)
class RetainedText:
    text: str
    truncated: bool
    omitted_bytes: Omitted


class TextRetainer:
    """Bounds a byte-oriented text stream: head, tail, or both."""

    def __init__(self, strategy: dict):
        if not isinstance(strategy, dict):
            raise ValueError("text retention strategy must be a mapping")
        kind = strategy.get("kind")
        if kind == "head":
            _assert_budget(strategy.get("max_bytes", strategy.get("maxBytes")), "max_bytes")
            self._prefix_cap = int(strategy.get("max_bytes", strategy.get("maxBytes")))
            self._suffix_cap = 0
        elif kind == "tail":
            _assert_budget(strategy.get("max_bytes", strategy.get("maxBytes")), "max_bytes")
            self._prefix_cap = 0
            self._suffix_cap = int(strategy.get("max_bytes", strategy.get("maxBytes")))
        elif kind == "head_tail":
            _assert_budget(strategy.get("head_bytes", strategy.get("headBytes")), "head_bytes")
            _assert_budget(strategy.get("tail_bytes", strategy.get("tailBytes")), "tail_bytes")
            self._prefix_cap = int(strategy.get("head_bytes", strategy.get("headBytes")))
            self._suffix_cap = int(strategy.get("tail_bytes", strategy.get("tailBytes")))
        else:
            raise ValueError(f"unknown text retention kind: {kind!r}")
        self._prefix_chunks: List[bytes] = []
        self._prefix_held = 0
        self._suffix_chunks: List[bytes] = []
        self._suffix_held = 0
        self._total = 0

    def push(self, chunk: Union[bytes, bytearray, memoryview, str]) -> PushDecision:
        data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        before = self._total
        self._total += len(data)
        room = self._prefix_cap - self._prefix_held
        take = max(0, min(room, len(data)))
        if take > 0:
            self._prefix_chunks.append(data[:take])
            self._prefix_held += take
        if self._suffix_cap > 0:
            self._suffix_chunks.append(data)
            self._suffix_held += len(data)
            while self._suffix_chunks and self._suffix_held - len(self._suffix_chunks[0]) >= self._suffix_cap:
                self._suffix_held -= len(self._suffix_chunks.pop(0))
            if self._suffix_chunks and self._suffix_held > self._suffix_cap:
                excess = self._suffix_held - self._suffix_cap
                self._suffix_chunks[0] = self._suffix_chunks[0][excess:]
                self._suffix_held -= excess
        return PushDecision(kept=self._omitted_at(self._total) == self._omitted_at(before),
                            truncated=self._omitted_at(self._total) > 0)

    def _omitted_at(self, total: int) -> int:
        prefix_len = min(total, self._prefix_cap)
        suffix_len = min(total - prefix_len, self._suffix_cap)
        return total - prefix_len - suffix_len

    def finish(self) -> RetainedText:
        prefix_len = min(self._total, self._prefix_cap)
        suffix_len = min(self._total - prefix_len, self._suffix_cap)
        prefix = b"".join(self._prefix_chunks)
        suffix = b"".join(self._suffix_chunks)[self._suffix_held - suffix_len:]
        budget_omitted = self._omitted_at(self._total)
        if budget_omitted > 0:
            kept_prefix = _trim_trailing_partial_utf8(prefix)
            kept_suffix = _trim_leading_continuation_utf8(suffix)
            text = (kept_prefix.decode("utf-8", errors="replace")
                    + kept_suffix.decode("utf-8", errors="replace"))
        else:
            kept_prefix, kept_suffix = prefix, suffix
            text = (prefix + suffix).decode("utf-8", errors="replace")
        omitted = self._total - len(kept_prefix) - len(kept_suffix)
        truncated = omitted > 0
        return RetainedText(text=text, truncated=truncated,
                            omitted_bytes=Omitted(kind="exact", count=omitted) if truncated else NONE_OMITTED)


def describe_omitted(omitted: Omitted, unit: str) -> str:
    """Standardized neutral omission clause ('' when nothing was omitted)."""
    if omitted.kind == "none":
        return ""
    if omitted.kind == "exact":
        return f"Omitted {omitted.count} {unit}."
    return f"More {unit} were omitted."


@dataclass(frozen=True)
class RetentionNotice:
    """Neutral retention outcome a tool hands to the notice formatter."""
    omitted: Omitted
    unit: str
    extra: dict = field(default_factory=dict)


def format_retention_notice(notice: RetentionNotice,
                            recovery: Callable[[RetentionNotice], str]) -> str:
    """Library omission clause + tool recovery guidance, single-space joined."""
    parts = [describe_omitted(notice.omitted, notice.unit), recovery(notice)]
    return " ".join(part for part in parts if len(part) > 0)


def truncate_without_splitting_surrogate_pair(text: str, max_chars: int) -> str:
    """Prefix of at most ``max_chars`` UTF-16 code units, never a lone high surrogate."""
    _assert_budget(max_chars, "max_chars")
    units = text.encode("utf-16-le", errors="surrogatepass")
    if len(units) // 2 <= max_chars:
        return text
    capped = units[:max_chars * 2]
    if capped:
        last = int.from_bytes(capped[-2:], "little")
        if 0xD800 <= last <= 0xDBFF:
            capped = capped[:-2]
    return capped.decode("utf-16-le", errors="surrogatepass")
