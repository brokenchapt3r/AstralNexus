"""Native port of the Typert foundation (DSH DI/reflection/remote protocol).

Modules map to upstream packages at commit ``5badb15009ae1756c3afe0ae0cef1faafc290ccc``:
- ``protocol`` ← ``@deepseek-ai/dsh-typert-protocol``
- ``registry`` ← ``@deepseek-ai/dsh-typert-registry``
- ``loader``   ← ``@deepseek-ai/dsh-typert-loader``

``@deepseek-ai/dsh-typert-generator`` is deliberately NOT ported: it is a
build-time TypeScript project analyzer whose artifacts have no consumer in the
native Python ecosystem (see ledger doc 49 §11).
"""
from __future__ import annotations

__all__ = ["protocol", "registry", "loader"]
