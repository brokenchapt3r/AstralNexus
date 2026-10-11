"""Zero-dependency time vocabulary shared by wire boundaries.

Port of ``@deepseek-ai/dsh-util-time`` (commit 5badb15):
``canonical_client_time_zone`` performs IANA zone validation and
canonicalization only — no formatting, no failure vocabulary (each boundary
declares and throws its own refusal).

Contract kept verbatim from upstream:
- Profile: ``UTC`` or ``^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+$``
  (IANA Area/Location style, at least one slash segment).
- Empty, padded (``value.strip() != value``), profile-mismatched, or unknown
  zones are refused — never guessed into a local zone.
- Zone membership is proven against the IANA database (``zoneinfo`` + the
  ``tzdata`` package on Windows).

Documented Python deviations:
- Alias→canonical resolution (upstream uses ICU ``Intl`` canonicalization,
  e.g. ``US/Eastern`` → ``America/New_York``) has no offline equivalent in
  Python 3.11/tzdata; an accepted alias is returned as-is. The pip ``tzdata``
  ``zones`` list includes aliases, so no reliable canonical test exists here.
  Wire readers that compare zone identities must canonicalize at the ICU side
  or treat alias/canonical pairs as equal explicitly.
- Non-string input returns ``None`` instead of raising TypeError (upstream's
  ``value.length`` throws on non-strings); this boundary is fail-soft by role.
"""
from __future__ import annotations

import re
from typing import Any, Optional

IANA_TIME_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+$")


def _resolves(value: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
        return True
    except Exception:
        return False


def canonical_client_time_zone(value: Any) -> Optional[str]:
    """Canonical zone for a caller-supplied name, or ``None`` when unusable."""
    if not isinstance(value, str) or len(value) == 0 or value.strip() != value:
        return None
    if value != "UTC" and not IANA_TIME_ZONE.match(value):
        return None
    if value != "UTC" and not _resolves(value):
        return None
    return value
