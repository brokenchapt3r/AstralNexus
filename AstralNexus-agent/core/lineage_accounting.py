"""谱系级进展与成本统计（b33 case1 fix10 第 4 步）。

业主定的口径：

* 运行级记录至少包含 **模型请求数 / Token / 工具调用数 / 恢复次数 / 耗时**，
  并**与原始任务关联**——键是**谱系根**，所以"换节点"不清零。
* **计入成本不等于处罚合法复验**：有依据的重测仍应允许，但必须**可见**。
* 熔断是**两种不同的判断**：
  - **无有效进展**（成本持续增长、同一验收缺口未改善）⇒ 停止自动恢复并给出具体阻塞项；
  - **有进展但成本过高** ⇒ 诚实标记"成本预算耗尽"，保留检查点与已成立证据。
    **不能冒充死循环，也不能悄悄续跑。**

本模块是纯函数 + 普通 dict 状态（便于持久化到 ``RuntimeState``），不产生 IO。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

LEDGER_SCHEMA = "astralnexus.lineage_ledger.v1"

STATE_OK = "ok"
STATE_NO_EFFECTIVE_PROGRESS = "no_effective_progress"
STATE_COST_EXHAUSTED = "cost_exhausted"

_COST_FIELDS = ("requests", "tokens", "tool_calls", "recoveries")


def empty_entry() -> dict[str, Any]:
    entry: dict[str, Any] = {field: 0 for field in _COST_FIELDS}
    entry["elapsed_seconds"] = 0.0
    entry["satisfied_items"] = []
    entry["gap_fingerprint"] = ""
    #: 同一缺口未改善而复验/恢复的次数——**无有效进展**判据只看这个。
    entry["gap_recoveries"] = 0
    entry["schema"] = LEDGER_SCHEMA
    return entry


def _merged(existing: Mapping[str, Any] | None) -> dict[str, Any]:
    entry = empty_entry()
    for key, value in dict(existing or {}).items():
        if key in entry:
            entry[key] = value
    entry["schema"] = LEDGER_SCHEMA
    return entry


def record_cost(
    entry: Mapping[str, Any] | None,
    *,
    requests: int = 0,
    tokens: int = 0,
    tool_calls: int = 0,
    recoveries: int = 0,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    """累加成本。**只记账、不判罚**——合法复验照样可见，但不因此被拦。"""
    merged = _merged(entry)
    for field, value in (
        ("requests", requests), ("tokens", tokens),
        ("tool_calls", tool_calls), ("recoveries", recoveries),
    ):
        merged[field] = int(merged.get(field, 0) or 0) + max(0, int(value or 0))
    merged["elapsed_seconds"] = round(
        float(merged.get("elapsed_seconds", 0.0) or 0.0) + max(0.0, float(elapsed_seconds or 0.0)),
        6,
    )
    return merged


def record_progress(
    entry: Mapping[str, Any] | None,
    satisfied_items: Iterable[str],
    *,
    gap_fingerprint: str = "",
    counted_as_recovery: bool = True,
) -> tuple[dict[str, Any], bool]:
    """记一次进展判定，返回 ``(entry, 是否出现新成立的验收项)``。

    * **换节点不清零**：条目按谱系根取，已成立项只增不减；
    * **结构接管不冒充验收转绿**：没有新成立项时这里返回 ``False``，
      并给该缺口的"未改善计数" +1（``counted_as_recovery`` 为假时不计数，
      例如纯查询）；
    * 缺口指纹**变了**（换了一个不同的缺口）⇒ 未改善计数归零——那是新问题，
      不是同一个问题反复没修好。
    """
    merged = _merged(entry)
    known = [str(item) for item in merged.get("satisfied_items") or []]
    incoming = [str(item) for item in satisfied_items if str(item)]
    new_items = [item for item in incoming if item not in known]
    if new_items:
        merged["satisfied_items"] = known + new_items
    fingerprint = str(gap_fingerprint or "")
    previous = str(merged.get("gap_fingerprint") or "")
    if not fingerprint:
        # 没有声明缺口 ⇒ 不触碰未改善计数（纯查询/无缺口语境）。
        pass
    elif fingerprint != previous:
        merged["gap_fingerprint"] = fingerprint
        # 首次观测到某缺口：这**一次**也已经试过且没改善，记 1；
        # 换成**另一个**缺口（此前已有缺口）：那是新问题，从 0 起算；
        # 有依据的合法复验（counted_as_recovery=False）只登记缺口，不判罚。
        merged["gap_recoveries"] = (
            1 if (not previous and counted_as_recovery) else 0
        )
    elif not new_items and counted_as_recovery:
        merged["gap_recoveries"] = int(merged.get("gap_recoveries", 0) or 0) + 1
    return merged, bool(new_items)


def breaker_verdict(entry: Mapping[str, Any] | None, *, budget: int, recovery_budget: int | None = None) -> dict[str, Any]:
    """两种不同的判断（业主口径），互不冒充。

    ``ok`` / ``no_effective_progress`` / ``cost_exhausted``。

    * ``no_effective_progress``：**同一缺口**未改善的复验次数触顶，
      成本持续增长 ⇒ 停止自动恢复，并给出**具体阻塞项**（缺口指纹）。
    * ``cost_exhausted``：成本触顶，但**有成立项**（进展是真的）⇒ 诚实标记
      "成本预算耗尽"，保留检查点与已成立证据；**不得**说成死循环。
    """
    merged = _merged(entry)
    cap = max(1, int(budget or 1))
    cost: dict[str, Any] = {
        field: int(merged.get(field, 0) or 0) for field in _COST_FIELDS
    }
    cost["elapsed_seconds"] = float(merged.get("elapsed_seconds", 0.0) or 0.0)
    cost["satisfied_items"] = list(merged.get("satisfied_items") or [])
    gap_recoveries = int(merged.get("gap_recoveries", 0) or 0)
    gap = str(merged.get("gap_fingerprint") or "")
    if gap_recoveries >= cap:
        return {
            "state": STATE_NO_EFFECTIVE_PROGRESS,
            "reason": "same_acceptance_gap_unchanged",
            "blocking_gap": gap,
            "gap_recoveries": gap_recoveries,
            "budget": cap,
            "cost": cost,
        }
    total_cap = cap if recovery_budget is None else max(1, recovery_budget)
    if cost["recoveries"] >= total_cap and cost["satisfied_items"]:
        return {
            "state": STATE_COST_EXHAUSTED,
            "reason": "cost_budget_exhausted_with_progress",
            "blocking_gap": "",
            "gap_recoveries": gap_recoveries,
            "budget": total_cap,
            "cost": cost,
        }
    return {
        "state": STATE_OK,
        "reason": "",
        "blocking_gap": "",
        "gap_recoveries": gap_recoveries,
        "budget": cap,
        "cost": cost,
    }


__all__ = [
    "LEDGER_SCHEMA",
    "STATE_COST_EXHAUSTED",
    "STATE_NO_EFFECTIVE_PROGRESS",
    "STATE_OK",
    "breaker_verdict",
    "empty_entry",
    "record_cost",
    "record_progress",
]