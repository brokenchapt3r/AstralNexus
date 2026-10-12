"""JSON 安全化：**唯一**实现（2026-10-08）。

起因：框架里曾有两份 `_json_safe`，语义不同 ——

| 输入 | `core/runtime_state._json_safe` | `core/orchestrator._json_safe` |
|---|---|---|
| dataclass | `"D(a=1, b='x')"`（字符串化，**字段丢失**） | `{'a': 1, 'b': 'x'}` |
| 有 ``to_dict()`` 的对象 | `'<T object at 0x…>'`（无意义 repr） | `{'k': 'v'}` |
| tuple / set / bytes | 同 | 同 |

而 `runtime_state` 用它写的正是 `last_decision` / `topology_state` / agent 快照 ——
一旦这些位置出现 dataclass 或带 ``to_dict()`` 的对象，**落盘的就是一句无用的 repr，
字段全部丢失**，而且不会有任何报错。

本模块采用**字段保留**的那套语义（原 `orchestrator` 版），理由不只是去重：
`Orchestrator._json_safe` 的文档字符串早已写明这套策略是硬要求 ——
"Checkpoints may contain structured protocol objects in node metadata …
Persistence must retain their fields instead of failing the whole run."
即：**结构对象要保字段，未知对象才退化成有界字符串**。

纯函数，只依赖标准库；**不得**反向导入 `core.*`（否则与 orchestrator / runtime_state 成环）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any


def json_safe(value: Any) -> Any:
    """Return a JSON-serialisable copy of ``value``, preserving structured fields.

    顺序：

    1. 基本类型原样返回；
    2. ``dataclass`` → ``asdict`` 后递归（**保留字段**）；
    3. ``Mapping`` → 递归（键统一成 ``str``；比只认 ``dict`` 宽，原 ``runtime_state`` 版即如此）；
    4. ``list/tuple/set/frozenset`` → 递归成 ``list``；
    5. 带 ``to_dict()`` → 调用后递归（**保留字段**）；
    6. 有 ``__dict__`` → 递归其属性（**保留字段**）；
    7. 其余 → ``str(value)``（有界诊断串，绝不让整次持久化失败）。

    注意与"先试 ``json.dumps``"那套的区别：那套会让 ``tuple`` 原样留存、
    并把 dataclass / ``to_dict`` 对象压成字符串。本函数**统一归一**，
    不依赖"恰好能被 dumps 接受"。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return json_safe(asdict(value))
        except Exception:
            pass
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return json_safe(to_dict())
        except Exception:
            pass
    try:
        attributes = vars(value)
    except TypeError:
        attributes = None
    if attributes is not None:
        return json_safe(attributes)
    return str(value)


__all__ = ["json_safe"]