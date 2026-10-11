"""Zero-dependency UUID and byte-encoding helpers.

Port of ``@deepseek-ai/dsh-util-crypto`` (commit 5badb15). Contract kept:
- ``bytes_to_base64``: canonical standard-alphabet base64 of a complete byte
  sequence (upstream chunks to bound intermediate strings; Python's
  ``base64.b64encode`` is already single-pass, so chunking is unnecessary).
- ``random_uuid``: lowercase hyphenated RFC 4122 version-4 UUID drawn from a
  CSPRNG (``uuid.uuid4`` uses ``os.urandom``).
"""
from __future__ import annotations

import base64
import uuid


def bytes_to_base64(data: "bytes | bytearray | memoryview") -> str:
    """Canonical base64 encoding of the complete input."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"bytes-like object expected, got {type(data).__name__}")
    return base64.b64encode(bytes(data)).decode("ascii")


def random_uuid() -> str:
    """New version-4 UUID from a CSPRNG: ``xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx``."""
    return str(uuid.uuid4())
