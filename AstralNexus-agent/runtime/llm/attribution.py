"""Centralize the non-secret product identity every provider request sends as
``User-Agent``, keeping adapters from drifting.

Port of ``@deepseek-ai/dsh-llm`` lib/types/attribution.js (commit 5badb150).

Python adaptation (documented): upstream reads the version from the
package's own manifest (``createRequire(import.meta.url)('../package.json')``)
so the User-Agent cannot drift from what is published. The ported package
carries no npm manifest; the constant below is pinned to the baseline
commit's published version (0.1.7-rc.2) and must be bumped with any
re-baseline.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["APP_IDENTITY", "PACKAGE_VERSION", "attribution_headers",
           "user_agent"]

PACKAGE_VERSION = "0.1.7-rc.2"

# The harness's own identity: the default every adapter sends. Deployments
# that need a white-label identity pass their own AppIdentity to
# attribution_headers — omission falls back to this default; nothing can
# suppress attribution entirely.
APP_IDENTITY: Dict[str, str] = {
    "product": "deepseek-harness",
    "version": PACKAGE_VERSION,
    "url": "https://github.com/deepseek-ai/deepseek-harness",
}


def user_agent(identity: Optional[Dict[str, Any]] = None) -> str:
    """The standard User-Agent value: ``product/version (+url)``.

    The parenthesized ``+url`` comment is the conventional self-
    identification form (RFC 9110 §10.1.5 product + comment syntax).
    """
    if identity is None:
        identity = APP_IDENTITY
    return f"{identity['product']}/{identity['version']} (+{identity['url']})"


def attribution_headers(
        identity: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Build the attribution headers an adapter must send on every provider
    request. Header names are lowercase (HTTP field names are case-insensitive
    on the wire). Omission cannot suppress attribution."""
    return {"user-agent": user_agent(identity)}
