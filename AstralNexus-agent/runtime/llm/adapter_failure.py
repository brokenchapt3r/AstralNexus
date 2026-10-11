"""Normalization for values thrown by a final LLM adapter boundary.

Port of ``@deepseek-ai/dsh-llm`` lib/types/adapter-failure.js
(commit 5badb150).

Python adaptations (documented):
- ``value instanceof Error`` becomes ``isinstance(value, BaseException)``.
- The own-data-property reads (``Object.getOwnPropertyDescriptor`` without
  invoking SDK accessors) become ``__dict__`` lookups, which bypass
  class-level properties exactly like an own data descriptor check.
- ``error.message`` becomes ``str(error)`` guarded against a hostile
  ``__str__``.
"""
from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Dict, Optional

from .errors import HarnessError

__all__ = ["normalize_llm_failure"]

_MAX_SAFE_INTEGER = 2 ** 53 - 1


def normalize_llm_failure(value: Any) -> Any:
    """Detach serializable provider facts from a value thrown by an adapter.

    :param value: arbitrary value thrown during adapter dispatch or
        iteration.
    :returns: immutable provider-neutral facts suitable for a terminal
        finish chunk.
    """
    error = value if isinstance(value, BaseException) \
        else HarnessError(_thrown_message(value), "UNKNOWN", {"cause": value})
    # Cross-package copies preserve own data but not class identity. Trust
    # the carried facts only when both own properties agree after
    # validation.
    carried = _own_failure_snapshot(error)
    if carried is not None and carried.get("code") == _own_error_code(error):
        return carried
    return MappingProxyType({
        "message": _error_message(error),
        "code": _harness_error_code(error),
    })


def _thrown_message(value: Any) -> str:
    """Render a non-Error throw without letting hostile coercion escape
    normalization."""
    try:
        message = str(value)
        return message if len(message) > 0 else "LLM adapter failed"
    except BaseException:
        return "LLM adapter failed"


def _own_error_code(error: Any) -> Any:
    """Read a foreign error's own data-backed code without invoking
    accessors."""
    try:
        own = getattr(error, "__dict__", None)
        return own.get("code") if own is not None else None
    except BaseException:
        return None


def _own_failure_snapshot(error: Any) -> Optional[Any]:
    """Snapshot an own data property without invoking an SDK-defined
    accessor."""
    try:
        own = getattr(error, "__dict__", None)
        if own is None or "failure" not in own:
            return None
        return _failure_snapshot(own["failure"])
    except BaseException:
        return None


def _failure_snapshot(value: Any) -> Optional[Any]:
    """Validate and detach an arbitrary serializable failure payload."""
    if not isinstance(value, (dict, MappingProxyType)):
        return None
    try:
        message = value.get("message")
        code = value.get("code")
        status = value.get("status")
        retry_after = value.get("providerRetryAfterMs")
        request_id = value.get("requestId")
        offload_images = value.get("offloadImages")
        if not isinstance(message, str) or message == "" \
                or not isinstance(code, str) or code == "" \
                or (status is not None and (
                    isinstance(status, bool) or not isinstance(status, int)
                    or status < 100 or status > 599)) \
                or (retry_after is not None and (
                    not isinstance(retry_after, (int, float))
                    or isinstance(retry_after, bool)
                    or not math.isfinite(retry_after) or retry_after <= 0)) \
                or (request_id is not None and (
                    not isinstance(request_id, str) or request_id == "")) \
                or (offload_images is not None and (
                    isinstance(offload_images, bool)
                    or not isinstance(offload_images, int)
                    or abs(offload_images) > _MAX_SAFE_INTEGER
                    or offload_images <= 0)):
            return None
        snapshot: Dict[str, Any] = {"message": message, "code": code}
        if status is not None:
            snapshot["status"] = status
        if retry_after is not None:
            snapshot["providerRetryAfterMs"] = retry_after
        if request_id is not None:
            snapshot["requestId"] = request_id
        if offload_images is not None:
            snapshot["offloadImages"] = offload_images
        return MappingProxyType(snapshot)
    except BaseException:
        return None


def _error_message(error: Any) -> str:
    """Read an SDK error message without letting an accessor replace the
    primary failure."""
    try:
        message = str(error)
        if isinstance(message, str) and len(message) > 0:
            return message
    except BaseException:
        # The fallback below preserves a serializable failure beside the
        # original Error.
        pass
    return "LLM adapter failed"


def _harness_error_code(error: Any) -> str:
    """Trust only Harness-owned codes; third-party SDK codes are not our
    taxonomy."""
    return error.code if isinstance(error, HarnessError) else "UNKNOWN"
