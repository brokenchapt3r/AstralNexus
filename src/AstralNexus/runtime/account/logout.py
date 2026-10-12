"""Bounded, memory-only revocation of a token already removed from local credentials.

Port of the logout layer of ``@deepseek-ai/dsh-deepseek-account-platform``
(commit 5badb15, lib/types/logout.js). Contract kept: remote failures stay in
the background — ``revoke_account`` returns after success, exhaustion, or
cancellation, never raising; retries back off exponentially
(``delayMs * 2 ** (attempt - 1)``) and every wait/request honors the lifetime
token.

Python adaptation (documented): the awaitable exponential delay becomes
``lifetime.wait(delay)`` on the CancelToken (a cancelled lifetime ends the
retry loop exactly like the upstream abortable setTimeout); the per-request
``AbortSignal.timeout`` becomes the urllib timeout bound.
"""
from __future__ import annotations

from typing import Any, Dict

from .protocol import logout_account


def revoke_account(origin: str, token: str, policy: Dict[str, Any], lifetime: Any,
                   headers: Dict[str, str]) -> None:
    """Revoke one captured grant without changing local account state."""
    max_retries = int(policy["max_retries"])
    delay_ms = float(policy["delay_ms"])
    request_timeout_ms = float(policy["request_timeout_ms"])
    for attempt in range(max_retries + 1):
        if lifetime.aborted:
            return
        if attempt > 0:
            cancelled = lifetime.wait((delay_ms * (2 ** (attempt - 1))) / 1000.0)
            if cancelled:
                return
        try:
            logout_account(origin, token, headers, timeout=request_timeout_ms / 1000.0)
            return
        except Exception:  # noqa: BLE001 - remote failures stay in the background
            continue
