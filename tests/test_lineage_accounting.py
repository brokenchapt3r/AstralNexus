"""第 4 步：谱系级进展与成本统计（b33 case1 fix10）。

锁住业主定的三条：

* **换节点不清零**：账挂在谱系根上，接管/换代累计；
* **结构接管不冒充验收转绿**：没有新成立项 ⇒ 不计进展，只计成本；
* **两种熔断互不冒充**：同一缺口未改善 ⇒ `no_effective_progress`（给出具体阻塞项）；
  有进展但成本过高 ⇒ `cost_exhausted`（保留已成立证据，不说成死循环）。
"""
from __future__ import annotations


def test_cost_and_progress_accumulate_across_node_generations(tmp_path):
    """换节点不清零：P-104 → P-109 → P-111 的账累在同一个谱系根上。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    entry = la.record_cost(entry, requests=1, tokens=100, tool_calls=2, recoveries=1)
    entry, _ = la.record_progress(entry, ["tests_passed:python verify_case.py"])
    # 第二代接管者：同样累到这一条上
    entry = la.record_cost(entry, requests=1, tokens=250, tool_calls=3, recoveries=1)

    assert entry["requests"] == 2
    assert entry["tokens"] == 350
    assert entry["tool_calls"] == 5
    assert entry["recoveries"] == 2
    # 已成立项只增不减：换代不把上一代的进展抹掉
    assert entry["satisfied_items"] == ["tests_passed:python verify_case.py"]


def test_a_structural_takeover_does_not_masquerade_as_progress(tmp_path):
    """结构接管：有新节点、有成本，但**没有**验收项转绿 ⇒ 不算进展。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    entry = la.record_cost(entry, requests=1, tokens=90, recoveries=1)
    entry, progressed = la.record_progress(
        entry, [], gap_fingerprint="gap:verify_case_repeatability"
    )

    assert progressed is False
    assert entry["satisfied_items"] == []
    assert entry["gap_recoveries"] == 1
    assert entry["recoveries"] == 1        # 成本照样可见


def test_same_gap_unchanged_reaches_no_effective_progress(tmp_path):
    """同一缺口反复没修好 ⇒ 停止自动恢复，并给出**具体阻塞项**。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    for _ in range(3):
        entry = la.record_cost(entry, requests=1, tokens=50, recoveries=1)
        entry, _ = la.record_progress(
            entry, [], gap_fingerprint="gap:verify_case_repeatability"
        )

    verdict = la.breaker_verdict(entry, budget=3)

    assert verdict["state"] == la.STATE_NO_EFFECTIVE_PROGRESS
    assert verdict["reason"] == "same_acceptance_gap_unchanged"
    assert verdict["blocking_gap"] == "gap:verify_case_repeatability"
    assert verdict["gap_recoveries"] == 3


def test_progress_with_excessive_cost_is_labelled_honestly(tmp_path):
    """有进展但成本过高 ⇒ `cost_exhausted`，**不得**冒充死循环，且保留已成立证据。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    entry = la.record_cost(entry, requests=5, tokens=900, recoveries=5)
    entry, progressed = la.record_progress(
        entry, ["file_written:自测报告.md"], gap_fingerprint="gap:verify_case_repeatability"
    )
    assert progressed is True

    verdict = la.breaker_verdict(entry, budget=5)

    assert verdict["state"] == la.STATE_COST_EXHAUSTED
    assert verdict["state"] != la.STATE_NO_EFFECTIVE_PROGRESS
    assert verdict["reason"] == "cost_budget_exhausted_with_progress"
    assert verdict["blocking_gap"] == ""
    assert verdict["cost"]["satisfied_items"] == ["file_written:自测报告.md"]


def test_a_different_gap_resets_the_unchanged_counter(tmp_path):
    """换了一个**不同的**缺口 ⇒ 未改善计数归零（那是新问题，不是同一个没修好）。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    for _ in range(2):
        entry, _ = la.record_progress(entry, [], gap_fingerprint="gap:A")
    assert entry["gap_recoveries"] == 2

    entry, _ = la.record_progress(entry, [], gap_fingerprint="gap:B")

    assert entry["gap_fingerprint"] == "gap:B"
    assert entry["gap_recoveries"] == 0


def test_a_grounded_recheck_is_visible_but_not_punished(tmp_path):
    """**计入成本不等于处罚合法复验**：有依据的重测照样记账，但不因此被拦。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    entry = la.record_cost(entry, requests=1, tokens=120, tool_calls=4, recoveries=1)
    entry, progressed = la.record_progress(
        entry, [], gap_fingerprint="gap:A", counted_as_recovery=False
    )

    assert progressed is False
    assert entry["requests"] == 1 and entry["tokens"] == 120   # 可见
    assert entry["gap_recoveries"] == 0                        # 未判罚
    assert la.breaker_verdict(entry, budget=1)["state"] == la.STATE_OK


def test_verdict_never_widens_the_budget(tmp_path):
    """预算不被放宽：未触顶就是 ok，触顶才表态。"""
    from AstralNexus.core import lineage_accounting as la

    entry = la.empty_entry()
    entry, _ = la.record_progress(entry, [], gap_fingerprint="gap:A")

    assert la.breaker_verdict(entry, budget=5)["state"] == la.STATE_OK
    assert la.breaker_verdict(entry, budget=5)["budget"] == 5
    assert la.breaker_verdict(entry, budget=1)["state"] == la.STATE_NO_EFFECTIVE_PROGRESS


# ------------------------------------------------- run 级接线（持久化）


def test_run_level_ledger_survives_a_reload_keyed_by_lineage_root(tmp_path):
    """账挂在谱系根上并落盘：**换节点/重启都不清零**。"""
    from AstralNexus.core.runtime_state import RuntimeStateManager
    from AstralNexus.runtime.evidence_identity import lineage_root

    class _Node:
        def __init__(self, node_id, recovery_of=""):
            self.node_id = node_id
            self.metadata = {"recovery_of": recovery_of} if recovery_of else {}

    nodes = {"P-104": _Node("P-104"), "P-109": _Node("P-109", "P-104")}
    # 第二代接管者解析回**同一个**谱系根——这正是"换节点不清零"的落点。
    assert lineage_root("P-109", nodes) == "P-104"

    manager = RuntimeStateManager(tmp_path, "lineage-run")
    manager.record_lineage_cost("P-104", requests=1, tokens=120, tool_calls=3, recoveries=1)
    assert manager.record_lineage_progress(
        "P-104", ["tests_passed:python verify_case.py"]
    ) is True
    manager.record_lineage_cost(
        lineage_root("P-109", nodes), requests=1, tokens=80, recoveries=1,
    )

    reloaded = RuntimeStateManager(tmp_path, "lineage-run")
    entry = reloaded.lineage_entry("P-104")

    assert entry["requests"] == 2 and entry["tokens"] == 200
    assert entry["satisfied_items"] == ["tests_passed:python verify_case.py"]

    verdict = reloaded.lineage_breaker("P-104", budget=20)
    assert verdict["state"] == "ok"
    assert verdict["cost"]["satisfied_items"] == ["tests_passed:python verify_case.py"]


def test_legacy_state_without_the_lineage_ledger_still_loads(tmp_path):
    """pre-fix16 的 runtime_state.json 没有新键：照常加载，账本从空开始。"""
    import json

    from AstralNexus.core.runtime_state import RuntimeState, RuntimeStateManager

    run_dir = tmp_path / "legacy-lineage"
    run_dir.mkdir()
    seed = RuntimeState(run_id="legacy-lineage")
    legacy = {
        key: value for key, value in vars(seed).items()
        if key != "lineage_accounting"
    }
    assert "lineage_accounting" not in legacy
    (run_dir / "runtime_state.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8",
    )

    manager = RuntimeStateManager(run_dir, "legacy-lineage")

    assert manager.state.lineage_accounting == {}
    assert manager.lineage_entry("P-104")["requests"] == 0