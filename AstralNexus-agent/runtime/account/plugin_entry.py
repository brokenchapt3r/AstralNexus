"""Native plugin entry for the deepseek-account-platform builtin.

``start`` constructs the PlatformAccount against the host-provided services
(credentials via the opt-in credentials-local plugin, the always-present
authorization registry, and the host's OAuth callback channel), runs the boot
issuer-mismatch reconciliation, and publishes the account seam. ``stop``
performs the bounded ordered shutdown and withdraws the service only when it
is still ours.
"""
from __future__ import annotations

from typing import Any

from .platform import PlatformAccount


def start(ctx: Any) -> PlatformAccount:
    authorization = ctx.services.get("authorization")
    if authorization is None:
        raise RuntimeError("account: the host provides no authorization service")
    provider = PlatformAccount(
        credentials=lambda: ctx.services.get("credentials"),
        authorization=authorization,
        callback_channel=ctx.services.get("oauth_callback_channel"),
        config=dict(ctx.config or {}),
    )
    provider.start()
    ctx.services["deepseek-account"] = provider
    return provider


def stop(ctx: Any, handle: PlatformAccount) -> None:
    try:
        handle.close()
    finally:
        if ctx.services.get("deepseek-account") is handle:
            del ctx.services["deepseek-account"]
