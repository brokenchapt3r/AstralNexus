"""DeerFlow 验收族的**兼容垫片**（移植用，不属冻结树）。

目的不是"让上游测试通过"，而是让上游测试的**断言原样**落在 AstralNexus 的实现上：

* 上游 ``_bash_execution(cmd, output_tail=…)`` → 我方 ``receipts`` 形状；
* 上游 ``check_acceptance_criteria(criteria, bash_executions=[…])`` → 我方
  ``run_acceptance_checks(criteria, receipts=[…])``，返回形状包装成 ``{"leaves": [...]}``。

于是上游测试体**逐字运行**，`checked`/`holds`/`detail` 的断言全部由我方实现判定 ——
**失败即真信号**（我方缺口或行为分歧），不会被垫片抹平。

上游没有而我没有的能力（如 ``_probe_file_size`` / ``Sandbox``），本垫片提供
``Missing`` 哨兵：**调用即抛** —— 移植器会把引用它们的测试标为 FEATURE_GAP 跳过，
既不假装通过，也不让整文件导入失败。
"""
from __future__ import annotations

import json
from typing import Any

from AstralNexus.runtime.acceptance_checks import run_acceptance_checks

UPSTREAM_REVISION = "2814bd5d498a"


class Missing:
    """上游有、我方没有的能力哨兵：调用即抛，供移植器识别 FEATURE_GAP。"""

    def __init__(self, name: str, module: str = ""):
        self._name = name
        self._module = module

    def __call__(self, *args: Any, **kwargs: Any):
        raise NotImplementedError(
            "FEATURE_GAP: AstralNexus has no equivalent of %s%s"
            % (self._module + "." if self._module else "", self._name)
        )

    def __getattr__(self, item: str):
        raise NotImplementedError(
            "FEATURE_GAP: %s%s has no attribute %r in AstralNexus"
            % (self._module + "." if self._module else "", self._name, item)
        )


def _bash_execution(command: str, *, status: str = "success", output_tail: str = "",
                    shell_persistent: bool | None = False) -> dict:
    """上游 helper，逐字同形（含 ``shell_persistent`` 这条来源标记）。"""
    return {
        "tool_call_id": "tc-%d" % (abs(hash(command)) % 10000),
        "tool_name": "bash",
        "command": command,
        "output_tail": output_tail,
        "status": status,
        "shell_persistent": shell_persistent,
    }


def _to_receipt(execution: dict) -> dict:
    """上游执行记录 → 我方终局收据。"""
    command = str(execution.get("command") or "")
    status = str(execution.get("status") or "success")
    receipt: dict[str, Any] = {
        "tool": "terminal_exec",
        "status": status,
        "returncode": 0 if status == "success" else 1,
        "stdout_tail": str(execution.get("output_tail") or ""),
        "files": {},
        "receipt_id": str(execution.get("tool_call_id") or "rc-x"),
        "arguments": json.dumps({"command": command}),
    }
    # 上游把持久 shell 的证据视为不可信来源；字段原样带过，由我方实现决定是否采信。
    if execution.get("shell_persistent") is not None:
        receipt["shell_persistent"] = bool(execution["shell_persistent"])
    return receipt


def _reader_bridge(content_reader):
    """上游 ``content_reader(runtime, path) -> str`` → 我方 ``reader(root, rel)``。

    上游用**假文件系统**喂内容（测试注入 reader 而不落盘）；我方 reader 的形状是
    ``(root, relative) -> (size, bytes, reason)``。不桥接就会让我方实现读不到文件，
    把"证据模型不同"误判成"失败"——所以这个桥是**忠实移植的前提**，不是便利设施。
    """
    if content_reader is None:
        return None

    def reader(root, relative):
        try:
            text = content_reader(root, relative)
        except FileNotFoundError:
            return None, None, "FILE_NOT_FOUND"
        except Exception as exc:  # 与上游一致：读失败 ⇒ 不可判定，不是"空文件"
            return None, None, "READ_FAILED:%s" % type(exc).__name__
        if text is None:
            return None, None, "READ_FAILED:none"
        blob = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        return len(blob), blob, None

    return reader


def check_acceptance_criteria(
    acceptance_criteria: list[str] | None,
    *,
    runtime: Any = None,
    thread_data: Any = None,
    bash_executions: list[dict] | None = None,
    content_reader: Any = None,
    size_prober: Any = None,
    readable_prober: Any = None,
) -> dict | None:
    """上游主入口 → 我方 ``run_acceptance_checks``，返回 ``{"leaves": [...]}``。"""
    criteria = [str(item) for item in (acceptance_criteria or []) if str(item).strip()]
    if not criteria:
        return None
    receipts = [_to_receipt(item) for item in (bash_executions or [])]
    raw = run_acceptance_checks(
        criteria,
        receipts=receipts,
        reader=_reader_bridge(content_reader),
    )
    leaves = [_as_upstream_leaf(item) for item in raw]
    return {
        # 上游 ``AcceptanceVerdict`` 的字段逐一补齐（缺一个就有测试以 KeyError 假失败）。
        "source": CHECK_SOURCE,
        "requirement": CHECK_REQUIREMENT,
        "leaves": leaves,
        "unchecked": [leaf["criterion"] for leaf in leaves if not leaf["checked"]],
        "all_hold": all(leaf["checked"] and leaf["holds"] for leaf in leaves),
        "criteria": list(criteria),
    }


#: 上游常量（逐字取自 ``deer-flow@2814bd5d498a subagents/acceptance_checks.py``）。
CHECK_SOURCE = "acceptance_checklist"
CHECK_REQUIREMENT = "delegation_acceptance_criteria"


def _as_upstream_leaf(observation: dict) -> dict:
    """我方观测 → 上游叶形状。

    上游叶键：``criterion`` / ``family`` / ``checked`` / ``holds`` / ``detail``；
    我方用 ``kind`` 而不是 ``family``，故补别名（不改值，只改可读名）。
    """
    leaf = dict(observation)
    leaf.setdefault("family", observation.get("kind", ""))
    leaf.setdefault("criterion", observation.get("criterion_id", ""))
    return leaf


#: 上游名字 → 垫片实现。未列出的名字由 ``missing()`` 提供哨兵。
SUPPORTED: dict[str, Any] = {
    "check_acceptance_criteria": check_acceptance_criteria,
    "_bash_execution": _bash_execution,
}


def missing(name: str, module: str = "") -> Any:
    return Missing(name, module)


__all__ = ["UPSTREAM_REVISION", "Missing", "SUPPORTED", "check_acceptance_criteria",
           "_bash_execution", "missing"]