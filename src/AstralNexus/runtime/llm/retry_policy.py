"""Provider-owned request-retry policy configuration and resolution.

Port of ``@deepseek-ai/dsh-llm`` lib/types/retry-policy.js (commit 5badb150).
Adapters expose one resolved policy per registered provider route; the
optional dsh-llm-retry plugin executes it on the agent's failed-step
extension point.

Python adaptation (documented): the exported ``RetryPolicySchema`` is a
schemastery union embedded by each concrete provider configuration upstream.
No schema host is mounted yet (Cordis config plane), so the export is a
structural descriptor constant with the same defaults and bounds; the runtime
authority is ``resolve_retry_policy``, which upstream also implements as
hand-rolled validation (the zod schema only serves config embedding).
"""
from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Dict, Optional

from ..dshutil.timeout import MAX_TIMER_DELAY_MS
from .errors import EMPTY_RESPONSE_CODE

__all__ = ["DEFAULT_INITIAL_DELAY_MS", "DEFAULT_JITTER_RATIO",
           "DEFAULT_MAX_DELAY_MS", "DEFAULT_MAX_RETRIES",
           "DEFAULT_RETRYABLE_CODES", "RETRY_POLICY_SCHEMA",
           "resolve_retry_policy"]

_MAX_SAFE_INTEGER = 2 ** 53 - 1

DEFAULT_MAX_RETRIES = 5
DEFAULT_INITIAL_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 10_000
DEFAULT_JITTER_RATIO = 0.1
DEFAULT_RETRYABLE_CODES = tuple((
    EMPTY_RESPONSE_CODE,
    "RATE_LIMIT",
    "SERVER",
    "TIMEOUT",
    "TRANSPORT",
))

# Structural descriptor of the upstream schemastery union
# (z.union([normalPolicySchema, alwaysPolicySchema])) for future config-host
# embedding; resolve_retry_policy is the runtime authority.
RETRY_POLICY_SCHEMA: Any = MappingProxyType({
    "union": (
        MappingProxyType({
            "mode": "normal",
            "maxRetries": {"type": "number", "step": 1, "min": 0,
                           "max": _MAX_SAFE_INTEGER,
                           "default": DEFAULT_MAX_RETRIES},
            "retryableCodes": {"type": "array", "items": "string",
                               "default": list(DEFAULT_RETRYABLE_CODES)},
            "backoff": MappingProxyType({
                "initialDelayMs": {"type": "number",
                                   "max": MAX_TIMER_DELAY_MS,
                                   "default": DEFAULT_INITIAL_DELAY_MS},
                "maxDelayMs": {"type": "number", "max": MAX_TIMER_DELAY_MS,
                               "default": DEFAULT_MAX_DELAY_MS},
                "jitterRatio": {"type": "number", "min": 0, "max": 1,
                                "default": DEFAULT_JITTER_RATIO},
            }),
        }),
        MappingProxyType({
            "mode": "always",
            "backoff": MappingProxyType({
                "initialDelayMs": {"type": "number",
                                   "max": MAX_TIMER_DELAY_MS,
                                   "default": DEFAULT_INITIAL_DELAY_MS},
                "maxDelayMs": {"type": "number", "max": MAX_TIMER_DELAY_MS,
                               "default": DEFAULT_MAX_DELAY_MS},
                "jitterRatio": {"type": "number", "min": 0, "max": 1,
                                "default": DEFAULT_JITTER_RATIO},
            }),
        }),
    ),
})

NORMAL_POLICY_KEYS = frozenset(
    ("mode", "maxRetries", "retryableCodes", "backoff"))
# Layered configuration can retain normal-only fields after switching modes;
# always mode ignores those inactive values while still rejecting unknown
# keys.
ALWAYS_POLICY_KEYS = frozenset(
    ("mode", "maxRetries", "retryableCodes", "backoff"))
BACKOFF_KEYS = frozenset(("initialDelayMs", "maxDelayMs", "jitterRatio"))


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _is_safe_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and abs(value) <= _MAX_SAFE_INTEGER


def _validate_keys(value: Dict[str, Any], allowed: frozenset,
                   path: str) -> None:
    for key in value.keys():
        if key not in allowed:
            raise ValueError(f'{path}: unknown key "{key}"')


def _resolve_backoff(config: Optional[Dict[str, Any]],
                     path: str) -> Any:
    if config is not None:
        _validate_keys(config, BACKOFF_KEYS, path)
    initial = DEFAULT_INITIAL_DELAY_MS if config is None \
        else config.get("initialDelayMs", DEFAULT_INITIAL_DELAY_MS)
    if initial is None:
        initial = DEFAULT_INITIAL_DELAY_MS
    maximum = DEFAULT_MAX_DELAY_MS if config is None \
        else config.get("maxDelayMs", DEFAULT_MAX_DELAY_MS)
    if maximum is None:
        maximum = DEFAULT_MAX_DELAY_MS
    jitter = DEFAULT_JITTER_RATIO if config is None \
        else config.get("jitterRatio", DEFAULT_JITTER_RATIO)
    if jitter is None:
        jitter = DEFAULT_JITTER_RATIO
    if not _is_finite_number(initial) or initial <= 0 \
            or initial > MAX_TIMER_DELAY_MS:
        raise ValueError(
            f"{path}.initialDelayMs must be a positive finite number no "
            f"greater than {MAX_TIMER_DELAY_MS}")
    if not _is_finite_number(maximum) or maximum <= 0 \
            or maximum > MAX_TIMER_DELAY_MS:
        raise ValueError(
            f"{path}.maxDelayMs must be a positive finite number no greater "
            f"than {MAX_TIMER_DELAY_MS}")
    if initial > maximum:
        raise ValueError(
            f"{path}.initialDelayMs must be less than or equal to maxDelayMs")
    if not _is_finite_number(jitter) or jitter < 0 or jitter > 1:
        raise ValueError(f"{path}.jitterRatio must be between 0 and 1")
    return MappingProxyType({"initialDelayMs": initial, "maxDelayMs": maximum,
                             "jitterRatio": jitter})


def resolve_retry_policy(config: Optional[Dict[str, Any]],
                         path: str) -> Any:
    """Validate, default, and detach one provider-owned retry policy.

    :param config: optional provider configuration; omission selects normal
        defaults.
    :param path: diagnostic path naming the provider config that owns the
        value.
    :returns: an immutable policy safe to capture in provider registration
        state.
    """
    if config is None:
        return MappingProxyType({
            "mode": "normal",
            "maxRetries": DEFAULT_MAX_RETRIES,
            "retryableCodes": DEFAULT_RETRYABLE_CODES,
            **_resolve_backoff(None, f"{path}.backoff"),
        })
    mode = config.get("mode")
    if mode == "normal":
        _validate_keys(config, NORMAL_POLICY_KEYS, path)
        max_retries = config.get("maxRetries")
        if max_retries is None:
            max_retries = DEFAULT_MAX_RETRIES
        retryable = config.get("retryableCodes")
        if retryable is None:
            retryable = list(DEFAULT_RETRYABLE_CODES)
        if not _is_safe_integer(max_retries) or max_retries < 0:
            raise ValueError(
                f"{path}.maxRetries must be a non-negative safe integer")
        if len(retryable) == 0:
            raise ValueError(f"{path}.retryableCodes must not be empty")
        if any(not isinstance(code, str) or code == "" for code in retryable):
            raise ValueError(
                f"{path}.retryableCodes must contain only non-empty strings")
        if len(set(retryable)) != len(retryable):
            raise ValueError(
                f"{path}.retryableCodes must not contain duplicates")
        return MappingProxyType({
            "mode": "normal",
            "maxRetries": max_retries,
            "retryableCodes": tuple(retryable),
            **_resolve_backoff(config.get("backoff"), f"{path}.backoff"),
        })
    if mode == "always":
        _validate_keys(config, ALWAYS_POLICY_KEYS, path)
        return MappingProxyType({
            "mode": "always",
            **_resolve_backoff(config.get("backoff"), f"{path}.backoff"),
        })
    raise ValueError(f'{path}.mode must be "normal" or "always"')
