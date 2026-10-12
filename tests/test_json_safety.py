"""JSON 安全化：唯一实现 + 语义钉死（2026-10-08）。

起因：框架里曾有两份 `_json_safe`，语义不同 —— `core/runtime_state` 那份先试
``json.dumps``，失败就 ``str(value)``，于是 **dataclass 与带 ``to_dict()`` 的对象
被压成字符串、字段全丢**（实测：dataclass → ``"D(a=1, b='x')"``；带 to_dict 的对象
→ ``'<T object at 0x…>'``）。而那份写的正是 ``last_decision`` / ``topology_state`` /
agent 快照 —— 一旦这些位置出现结构化对象，落盘的就是无意义 repr，且**不会报错**。

现已统一到 ``core/json_safety.json_safe``（字段保留的那套）。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

FRAMEWORK_ROOT = Path(str(__import__('AstralNexus').__file__)).resolve().parent
SCAN_DIRS = ("core", "runtime", "agents", "tools", "cli", "dashboard")
EXCLUDE = ("skills/vendor", "skills/public", "__pycache__", "/tests/", "/test_")


@dataclass
class _Decision:
    node_id: str = "P-1"
    action: str = "retry"


class _Snapshot:
    def __init__(self) -> None:
        self.node_id = "P-2"
        self.state = "running"

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "state": self.state}


class _Opaque:
    """既非 dataclass、无 to_dict、也无 __dict__（走有界字符串分支）。"""

    __slots__ = ()


# --------------------------------------------------- 结构化对象必须保字段


def test_dataclass_keeps_its_fields():
    """曾经的缺陷：这一项在 runtime_state 版里会变成 "D(node_id='P-1', …)"。"""
    from AstralNexus.core.json_safety import json_safe

    result = json_safe(_Decision())

    assert result == {"node_id": "P-1", "action": "retry"}
    assert not isinstance(result, str)


def test_object_with_to_dict_keeps_its_fields():
    """曾经的缺陷：这一项在 runtime_state 版里会变成 '<_Snapshot object at 0x…>'。"""
    from AstralNexus.core.json_safety import json_safe

    result = json_safe(_Snapshot())

    assert result == {"node_id": "P-2", "state": "running"}
    assert "0x" not in str(result)


def test_plain_object_keeps_its_attributes():
    from AstralNexus.core.json_safety import json_safe

    class _Plain:
        pass

    plain = _Plain()
    plain.alpha = 1
    plain.beta = {"nested": (1, 2)}

    assert json_safe(plain) == {"alpha": 1, "beta": {"nested": [1, 2]}}


# ------------------------------------------------------------- 基本归一


def test_containers_normalise_to_json_shapes():
    from AstralNexus.core.json_safety import json_safe

    assert json_safe((1, 2)) == [1, 2]
    assert json_safe({1, 2}) == [1, 2]
    assert json_safe(frozenset({3})) == [3]
    assert json_safe([{"a": (1,)}]) == [{"a": [1]}]


def test_non_dict_mapping_becomes_a_str_keyed_dict():
    from collections import UserDict

    from AstralNexus.core.json_safety import json_safe

    assert json_safe(UserDict({1: "one"})) == {"1": "one"}


def test_primitives_pass_through_and_unknown_objects_degrade_to_str():
    from AstralNexus.core.json_safety import json_safe

    for value in (None, 0, 1.5, "s", True):
        assert json_safe(value) == value
    assert isinstance(json_safe(_Opaque()), str)


def test_depth_is_bounded_by_the_object_graph_not_by_json_dumps():
    """不依赖"恰好能被 dumps 接受"：tuple 一律归一成 list，不留原类型。"""
    from AstralNexus.core.json_safety import json_safe

    nested = {"rows": _Decision(), "pairs": ((1, 2), (3, 4))}

    assert json_safe(nested) == {
        "rows": {"node_id": "P-1", "action": "retry"},
        "pairs": [[1, 2], [3, 4]],
    }


# --------------------------------------------------- 唯一实现（防复发守护）


def _definition_sites() -> list[str]:
    sites: list[str] = []
    for directory in SCAN_DIRS:
        for path in (FRAMEWORK_ROOT / directory).rglob("*.py"):
            rel = str(path).replace("\\", "/")
            if any(token in rel for token in EXCLUDE):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name in ("json_safe", "_json_safe"):
                    sites.append("%s:%d" % (
                        str(path.relative_to(FRAMEWORK_ROOT)).replace("\\", "/"),
                        node.lineno,
                    ))
    return sites


def test_exactly_one_json_safe_definition_in_the_framework():
    sites = _definition_sites()

    assert len(sites) == 1, (
        "JSON 安全化必须只有一份实现（否则同一份数据在两处被不同归一）：" + repr(sites)
    )
    assert sites[0].startswith("core/json_safety.py"), sites


def test_both_former_owners_share_that_single_implementation():
    from AstralNexus.core import json_safety, orchestrator, runtime_state

    assert runtime_state._json_safe is json_safety.json_safe
    assert orchestrator._json_safe is json_safety.json_safe


# --------------------------------------------------------------- 接线


def test_runtime_state_as_dict_preserves_a_structured_decision():
    """现场位置：``last_decision`` 里放 dataclass，持久化载荷必须留字段。"""
    from AstralNexus.core.runtime_state import RuntimeState

    state = RuntimeState(run_id="json-safety")
    state.last_decision = {"decision": _Decision(), "reason": "x"}
    state.topology_state = {"snapshot": _Snapshot()}

    payload = state.as_dict()

    assert payload["last_decision"]["decision"] == {
        "node_id": "P-1", "action": "retry",
    }
    assert payload["topology_state"]["snapshot"] == {
        "node_id": "P-2", "state": "running",
    }