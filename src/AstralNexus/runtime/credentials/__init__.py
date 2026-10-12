"""Native port of the DSH credentials family (commit 5badb15).

Modules map to upstream packages:
- ``base`` ← ``@deepseek-ai/dsh-credentials`` (abstract seam + key grammar)
- ``local`` ← ``@deepseek-ai/dsh-credentials-local`` (file-backed provider)
- ``anonymous_user_id`` ← ``@deepseek-ai/dsh-anonymous-user-id``
- ``plugin_entry`` — native PluginHost entry exposing the local provider

``dsh-authorization`` / ``dsh-deepseek-account`` / ``-platform`` follow in the
next wave (async conversation seam + PKCE platform provider).
"""
from __future__ import annotations

__all__ = ["base", "local", "anonymous_user_id", "plugin_entry"]
