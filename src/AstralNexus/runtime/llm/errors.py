"""Harness error base with a stable machine-routable code and chained cause.

Port of ``@deepseek-ai/dsh-llm`` lib/types/error.js (commit 5badb150).
Package errors extend ``HarnessError`` so tool results and replay can retain
failure class.

Python adaptations (documented):
- ``ErrorOptions.cause`` becomes an own ``cause`` attribute (default None);
  for foreign exceptions ``errorChain`` falls back to ``__cause__`` (the
  native Python chaining attribute).
- ``AggregateError`` members become ``ExceptionGroup.exceptions``.
- The render path set keys on ``id()`` because plain dict payloads (the
  structured-failure arm) are not hashable in Python.
- ``new.target.name`` becomes ``type(self).__name__``.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..compat import ExceptionGroup

__all__ = [
    "ACCOUNT_QUOTA_EXCEEDED_CODE",
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "EMPTY_RESPONSE_CODE",
    "HarnessError",
    "IMAGE_OFFLOAD_REQUIRED_CODE",
    "INVALID_CREDENTIAL_CODE",
    "QUOTA_EXCEEDED_CODE",
    "error_chain",
    "is_harness_error",
    "is_context_window_exceeded_error",
    "is_quota_exceeded_error",
]


class HarnessError(Exception):
    """Base class for all harness errors.

    Carries a ``code`` (stable, programmatic — e.g. ``NO_ADAPTER``,
    ``INVALID_ARGS``, ``INVARIANT``) distinct from the human-readable
    message, and supports ``cause`` chaining via the standard options
    mapping. ``name`` defaults to the subclass constructor name.
    """

    def __init__(self, message: str, code: str,
                 options: Optional[dict] = None) -> None:
        super().__init__(message)
        options = options or {}
        # Stable machine-routable failure class (e.g. RATE_LIMIT); route on
        # this, never by parsing the message.
        self.code = code
        self.name = type(self).__name__  # new.target.name
        self.cause = options.get("cause")


# Canonical provider-neutral code for a model request rejected because its
# context window was exceeded.
CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"
# Canonical provider-neutral code for an exhausted account quota or balance.
QUOTA_EXCEEDED_CODE = "QUOTA"
# Account-token quota that can be replenished through the first-party
# billing page.
ACCOUNT_QUOTA_EXCEEDED_CODE = "ACCOUNT_QUOTA"
# Canonical provider-neutral code for a response that completed normally but
# carried no content blocks at all. Providers occasionally emit a degenerate
# completion (a terminal stop with zero output); adapters classify it as this
# failure instead of yielding an empty assistant message, because an empty
# message silently ends the turn with nothing for the user or the loop to act
# on. The attempt produced nothing durable, so retry policy treats it as safe
# to repeat.
EMPTY_RESPONSE_CODE = "EMPTY_RESPONSE"
# Canonical provider-neutral code for a credential that was supplied but
# cannot be used — malformed rather than absent. Distinct from
# MISSING_CREDENTIAL because the fix differs: correct the stored value rather
# than supply one. Deliberately outside the default retryable set — a
# malformed credential fails identically on every attempt.
INVALID_CREDENTIAL_CODE = "INVALID_CREDENTIAL"

# Structured codes and plain phrases that explicitly name a context bound
# being exceeded.
_STRUCTURED_CONTEXT_OVERFLOW = re.compile(
    r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
    r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)"
    r"(?:$|[^a-z0-9])", re.IGNORECASE)
# Request-size wording that ties "too large" directly to model context
# capacity.
_TOO_LARGE_FOR_CONTEXT = re.compile(
    r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?"
    r"too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?"
    r"(?:model(?:'s)?\s+)?context(?:\s+window)?\b", re.IGNORECASE)
# "Exceeds" wording is safe only when its object is explicitly the model
# context.
_EXCEEDS_MODEL_CONTEXT = re.compile(
    r"\b(?:input|prompt|request|messages?)\b.{0,40}"
    r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
    r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context"
    r"(?:\s+(?:length|window))?\b", re.IGNORECASE)
_MAX_CONTEXT_BOUND = re.compile(
    r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+"
    r"(?:length|window)\b", re.IGNORECASE)
_TOO_LONG_FOR_MODEL = re.compile(
    r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+"
    r"(?:this|the)\s+model\b", re.IGNORECASE)


def is_context_window_exceeded_error(detail: str) -> bool:
    """Recognize the context-overflow wording used by OpenAI-compatible
    providers and library adapters. Adapters pass all available provider
    code, type, and message text so both thrown and in-band delivery styles
    share one classifier."""
    return bool(
        _STRUCTURED_CONTEXT_OVERFLOW.search(detail)
        or _MAX_CONTEXT_BOUND.search(detail)
        or _TOO_LARGE_FOR_CONTEXT.search(detail)
        or _TOO_LONG_FOR_MODEL.search(detail)
        or _EXCEEDS_MODEL_CONTEXT.search(detail))


_INSUFFICIENT_QUOTA = re.compile(
    r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", re.IGNORECASE)
_QUOTA_EXCEEDED = re.compile(
    r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b",
    re.IGNORECASE)
_EXCEEDS_QUOTA = re.compile(
    r"\bexceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?"
    r"(?:current[\s_-]+)?quota\b", re.IGNORECASE)
_BALANCE_EXHAUSTED = re.compile(
    r"\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b", re.IGNORECASE)
_OUT_OF_CREDITS = re.compile(
    r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", re.IGNORECASE)


def is_quota_exceeded_error(detail: str) -> bool:
    """Recognize provider wording that identifies an exhausted account quota
    rather than a transient request-rate limit."""
    return bool(
        _INSUFFICIENT_QUOTA.search(detail)
        or _QUOTA_EXCEEDED.search(detail)
        or _EXCEEDS_QUOTA.search(detail)
        or _BALANCE_EXHAUSTED.search(detail)
        or _OUT_OF_CREDITS.search(detail))


def _name_of(error: Any) -> str:
    name = getattr(error, "name", None)
    if isinstance(name, str) and name != "":
        return name
    return type(error).__name__


def _message_of(error: Any) -> str:
    # JS Error.message; the Python counterpart is the rendered exception text.
    message = str(error)
    return message if message != "" else _name_of(error)


def _cause_of(error: Any) -> Any:
    """Own ``cause`` data first (HarnessError), then the native chain."""
    cause = getattr(error, "cause", None)
    if cause is None:
        cause = error.__cause__
    return cause


def error_chain(value: Any) -> str:
    """Render a thrown value with its full cause chain and ExceptionGroup
    members, so transport wrappers surface the underlying failure instead of
    masking it. Plain structured failures render their own data-backed
    message. Diagnostic-surface rendering only (messages, notices, logs) —
    never parse the result; route on ``HarnessError.code``."""
    # Tracks the active recursion path (entries removed on exit), so only
    # true cycles are flagged and a diamond-shared cause still renders in
    # full. Keyed on id(): dict payloads are unhashable in Python.
    path: set = set()

    def render(current: Any) -> str:
        key = id(current)
        if key in path:
            return "<circular cause>"
        path.add(key)
        try:
            if not isinstance(current, BaseException):
                own = getattr(current, "__dict__", None)
                if own is not None:
                    descriptor = own.get("message")
                    if isinstance(descriptor, str):
                        return descriptor
                return str(current)
            message = _message_of(current)
            members = ""
            if isinstance(current, ExceptionGroup) and current.exceptions:
                rendered = [render(member) for member in current.exceptions]
                members = f" [{'; '.join(rendered)}]"
            cause_value = _cause_of(current)
            cause_text = "" if cause_value is None else render(cause_value)
            # Wrappers like HarnessError(str(value), code, {"cause": value})
            # repeat their cause verbatim; rendering it again would only add
            # noise.
            cause = "" if cause_text == "" or cause_text == message \
                else f": {cause_text}"
            return f"{message}{members}{cause}"
        except BaseException:
            # Only hostile coercion or hostile accessors: this renderer feeds
            # UI notices and logs, so nothing may escape. Inner frames catch
            # their own throws, so only the hostile node collapses, not the
            # whole chain.
            return "<unrenderable value>"
        finally:
            path.discard(key)

    return render(value)


def is_harness_error(value: Any) -> bool:
    """Narrow an arbitrary thrown value to a HarnessError (for isinstance at
    runtime boundaries). Duck-typed errors do not narrow."""
    return isinstance(value, HarnessError)


# Canonical code for a request an image-capable route cannot send until more
# of its images are offloaded. The failure's offloadImages names how many
# more of the oldest retained occurrences must be offloaded;
# dsh-compaction-image-offload records an image/offload selection before the
# agent or summarizer retries with freshly derived input.
IMAGE_OFFLOAD_REQUIRED_CODE = "IMAGE_OFFLOAD_REQUIRED"
