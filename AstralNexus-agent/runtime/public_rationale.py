"""Official interface for public decision/execution rationales.

This module is the single source of truth for turning an internal decision or
activity payload into a user-facing explanation. Only *explicitly public* keys
are consulted; private reasoning, chain-of-thought and hidden state are never
surfaced here.

Official interface::

    from AstralNexus.runtime.public_rationale import public_rationale

    text = public_rationale(payload, activity)   # first non-empty source wins

Contract:
    * candidates are scanned left to right; the first one carrying any public key
      decides the result;
    * within one candidate, :data:`PUBLIC_RATIONALE_KEYS` decides priority; each
      key is read from the candidate itself and then from its ``metrics`` mapping;
    * the result is stripped and truncated to :data:`PUBLIC_RATIONALE_LIMIT`;
    * no public key with a value means ``""`` (never ``None``).
"""
from __future__ import annotations

from typing import Any, Mapping

#: Keys consulted, in priority order (read from the source, then its ``metrics``).
PUBLIC_RATIONALE_KEYS: tuple[str, ...] = (
    "public_reasoning",
    "planning_explanation",
    "decision_rationale",
    "execution_rationale",
    "rationale",
    "reason",
)

#: Hard cap on the length of a publicized rationale.
PUBLIC_RATIONALE_LIMIT = 4000


def public_rationale(*sources: Any) -> str:
    """Return the first explicitly public rationale found across ``sources``.

    Args:
        *sources: Candidate payloads, most specific first. Non-mappings are
            skipped.

    Returns:
        The stripped rationale truncated to :data:`PUBLIC_RATIONALE_LIMIT`
        characters, or an empty string when no public key carries a value.
    """
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        metrics = source.get("metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        for key in PUBLIC_RATIONALE_KEYS:
            value = source.get(key) or metrics.get(key)
            if value:
                return str(value).strip()[:PUBLIC_RATIONALE_LIMIT]
    return ""