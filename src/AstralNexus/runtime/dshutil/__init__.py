"""Native ports of DSH zero-dependency utility packages (migration batch B1).

Every module names its upstream package and the fixed baseline commit
``5badb15009ae1756c3afe0ae0cef1faafc290ccc``; deviations required by Python
semantics are documented in the module docstring and in
``04_归档/项目文档/2026-10-10_历史修复记录/49_B1插件闭环移植台账.md``.

Mapping (wave 1):
- ``atomic_write``      ← ``@deepseek-ai/dsh-atomic-write``
- ``timeout``           ← ``@deepseek-ai/dsh-timeout``
- ``time_util``         ← ``@deepseek-ai/dsh-util-time``
- ``dsh_deque``         ← ``@deepseek-ai/dsh-deque``
- ``chunked_list``      ← ``@deepseek-ai/dsh-chunked-list``
- ``home_paths``        ← ``@deepseek-ai/dsh-home-paths``
- ``values``            ← ``@deepseek-ai/dsh-util-values``
- ``brand``             ← ``@deepseek-ai/dsh-brand``
- ``crypto_util``       ← ``@deepseek-ai/dsh-util-crypto``
- ``lazy_require``      ← ``@deepseek-ai/dsh-lazy-require``
- ``launch_environment``← ``@deepseek-ai/dsh-launch-environment``
- ``package_manifest``  ← ``@deepseek-ai/dsh-package-manifest``

Wave 2:
- ``code_language``     ← ``@deepseek-ai/dsh-util-code-language``
- ``workspace_path``    ← ``@deepseek-ai/dsh-util-workspace-path``
- ``output_retention``  ← ``@deepseek-ai/dsh-output-retention``
"""
from __future__ import annotations

__all__ = [
    "atomic_write", "timeout", "time_util", "dsh_deque", "chunked_list",
    "home_paths", "values", "brand", "crypto_util", "lazy_require",
    "launch_environment", "package_manifest",
    "code_language", "workspace_path", "output_retention",
]
