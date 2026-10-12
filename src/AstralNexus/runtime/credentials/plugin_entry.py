"""Native PluginHost entry exposing the local credentials provider.

Start builds the file-backed provider from validated plugin config, runs the
boot read (an invalid document fails the plugin's activation), starts the
optional hot-reload watcher, and publishes the seam as ``services['credentials']``.
Stop closes the provider (refusing new writes) and withdraws the service.
"""
from __future__ import annotations

from typing import Any

from .local import LocalCredentialProvider


def start(ctx: Any) -> Any:
    provider = LocalCredentialProvider(dict(ctx.config or {}), ctx=ctx)
    provider.start()
    ctx.services["credentials"] = provider
    return provider


def stop(ctx: Any, handle: Any) -> None:
    try:
        handle.close()
    finally:
        if ctx.services.get("credentials") is handle:
            del ctx.services["credentials"]
