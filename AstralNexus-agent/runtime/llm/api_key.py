"""The one definition of a well-formed provider API key, shared by every
adapter that puts one in an HTTP header.

Port of ``@deepseek-ai/dsh-llm`` lib/types/api-key.js (commit 5badb150).
"""
from __future__ import annotations

import re
from typing import Any, Dict

__all__ = ["LEGAL_API_KEY", "normalize_api_key"]

# Characters an HTTP header value carries verbatim and every known provider
# key uses: printable ASCII, space excluded. A key outside this set cannot
# reach any provider — the HTTP client refuses to build the header — so this
# is a transport invariant rather than one provider's policy. Latin-1 is
# excluded deliberately: a header could carry it, but no provider issues it,
# and admitting it trades a local explained refusal for an opaque 401.
LEGAL_API_KEY = re.compile(r"^[\x21-\x7E]+$")


def normalize_api_key(raw: str) -> Dict[str, Any]:
    """Judge one *supplied* API key, trimming surrounding whitespace first.

    Trimming is silent because a padded key has one unambiguous reading;
    every other defect is reported. Absence is a configuration state this
    function never sees — a profile naming no credential authenticates
    through the provider's own ambient discovery or OAuth — so callers
    decide whether a value was supplied before asking.

    :param raw: the key exactly as configured, stored, or typed.
    :returns: ``{"ok": True, "value": ...}`` or
        ``{"ok": False, "reason": "empty"|"illegalCharacters"}``.
    """
    value = raw.trim() if hasattr(raw, "trim") else raw.strip()
    if len(value) == 0:
        return {"ok": False, "reason": "empty"}
    if not LEGAL_API_KEY.match(value):
        return {"ok": False, "reason": "illegalCharacters"}
    return {"ok": True, "value": value}
