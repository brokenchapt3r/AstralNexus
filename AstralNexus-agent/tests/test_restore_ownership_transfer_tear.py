# -*- coding: utf-8 -*-
"""所有权转移状态撕裂 —— 恢复必须完成已被框架自己标记的转移。

Live r13c（2026-09-30T01:12:52Z，generation 12 普通接管）：checkpoint restore
确定性死亡，整个 run 无法恢复：

    ValueError: graph patch update has overlapping output ownership:
    N-122 conflicts with N-123        (orchestrator.py:9696 → governor.py:2128)

因果链（全部有持久化证据）：
1. 12:57:03Z 的接管补丁经 Fix H 规范化给终局 blocked 的 N-122 生成所有权剥离
   （owned_paths=[]，metadata.ownership_transfer_to=N-123）。验证用的是内存
   预合并态（通过），但**变异边界丢弃了对终局节点的路径剥离、却持久化了
   metadata 标记** —— 部分变异，撕裂诞生：N-122 同时"已转移"且"仍持有"。
2. 恢复时 R-201 的接管重放试图把 N-122 置 superseded；状态变更又被终局保护
   丢弃（doc 14 §12:50：terminal markers must survive —— 该保护本身正确）。
3. 继承游走（9672）把 N-122 当作在世继承者：它是 blocked（不在
   {completed,cancelled,superseded,failed} 跳过集），recovery_of=N-118
   （superseded 捐赠者），路径顺序差异触发合成 owned_paths 更新。
4. 验证器把 blocked 计入 active_owner_statuses（governor.py:1117-1120）→
   N-122 的合并路径与 N-123（admitted，同一组路径）冲突 → 每次恢复必炸。

修复语义：
A. 恢复期完成"有标记、有残留"的转移（剥离残留路径，留痕、幂等）——
   标记是框架自己持久化的转移意图，完成它不是篡改状态；
B. 继承游走不得给"已转移 / 终局 blocked"的死者回赠职责（留痕跳过）。
健康继承（在世继承者 ← 退役捐赠者）必须原样保留；无标记的普通 blocked
节点所有权不得被触碰。
"""
from __future__ import annotations

import json
from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator

PATHS_ORDER_1 = [
    "deliverables/runs/consistency-check/consistency-report.md",
    "deliverables/runs/consistency-check/consistency-evidence.json",
]
PATHS_ORDER_2 = list(reversed(PATHS_ORDER_1))  # 顺序差 → 合并结果 != 存量


def _tear_run(tmp_path: Path, run_id: str) -> Orchestrator:
    """复刻线上形状：A(superseded) ← T(blocked 终局+转移标记) ← M(failed) ← S(admitted)。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id=run_id)
    orch.task_graph.add_node({
        "node_id": "A", "goal": "原始生产者", "role": "coder",
        "status": "superseded", "dependencies": [],
        "owned_paths": PATHS_ORDER_1, "metadata": {},
    })
    orch.task_graph.add_node({
        "node_id": "T", "goal": "终局恢复节点", "role": "coder",
        "status": "blocked", "dependencies": ["A"],
        "owned_paths": PATHS_ORDER_2,
        "metadata": {
            "recovery_of": "A",
            "_terminal_failure": True,
            "_terminal_reason": "recovery_exhausted",
            # 12:57:03Z 规范化持久化的转移标记（剥离本体被变异边界丢弃）
            "ownership_transfer_to": "S",
        },
    })
    orch.task_graph.add_node({
        "node_id": "M", "goal": "中途接管者", "role": "coder",
        "status": "failed", "dependencies": ["T"],
        "owned_paths": PATHS_ORDER_2,
        "metadata": {"recovery_of": "T", "_terminal_failure": True},
    })
    orch.task_graph.add_node({
        "node_id": "S", "goal": "最终接管者", "role": "coder",
        "status": "admitted", "dependencies": ["M"],
        "owned_paths": PATHS_ORDER_2,
        "metadata": {"recovery_of": "M"},
    })
    # This fixture reproduces a pre-versioned checkpoint from the old writer.
    orch.runtime_state_manager.state.checkpoint_schema_version = 0
    orch._save_checkpoint()
    return orch


def _trace_events(tmp_path: Path, needle: str):
    trace = Path(tmp_path) / "trace.jsonl"
    if not trace.exists():
        return []
    return [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
        if needle in line
    ]


# ------------------------------------------------------------------ 1. 复现
def test_restore_survives_persisted_ownership_transfer_tear(tmp_path):
    """线上形状：修复前 restore_checkpoint 抛 'T conflicts with S'。"""
    _tear_run(tmp_path, "tear")
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="tear")
    assert restored.restore_checkpoint() is True
    t = restored.task_graph.get_node("T")
    s = restored.task_graph.get_node("S")
    # 终局标记不得被洗白（doc 14 §12:50）
    assert t.status == "blocked"
    assert t.metadata.get("_terminal_reason") == "recovery_exhausted"
    # 转移被完成：唯一活跃所有者是 S
    assert list(t.owned_paths) == []
    assert sorted(s.owned_paths) == sorted(PATHS_ORDER_2)


def test_completed_transfer_is_auditable(tmp_path):
    """完成转移与游走跳过都必须留痕，不能是静默修复。"""
    _tear_run(tmp_path, "audited")
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="audited")
    assert restored.restore_checkpoint() is True
    completed = _trace_events(tmp_path, "checkpoint.ownership_transfer_completed")
    assert completed, "转移完成必须留下审计事件"
    payload = completed[-1].get("payload") or {}
    assert payload.get("node_id") == "T"
    assert payload.get("transferee") == "S"
    assert sorted(payload.get("stripped_paths") or []) == sorted(PATHS_ORDER_2)
    skipped = _trace_events(tmp_path, "checkpoint.inheritance_walk_skipped")
    assert skipped, "继承游走对死者的跳过必须留痕"
    assert (skipped[-1].get("payload") or {}).get("node_id") == "T"


def test_completed_transfer_is_idempotent(tmp_path):
    """再次恢复不得重复剥离、不得崩溃。"""
    _tear_run(tmp_path, "idem")
    first = Orchestrator(tmp_path, max_iterations=1, run_id="idem")
    assert first.restore_checkpoint() is True
    second = Orchestrator(tmp_path, max_iterations=1, run_id="idem")
    assert second.restore_checkpoint() is True
    t = second.task_graph.get_node("T")
    assert list(t.owned_paths) == []
    assert t.metadata.get("_ownership_transfer_strip_completed") is True


# ------------------------------------------------------- 2. 既有语义不被削弱
def test_living_successor_still_inherits_from_retired_donor(tmp_path):
    """继承游走存在的理由必须保留：在世继承者获得退役捐赠者的职责。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id="healthy")
    orch.task_graph.add_node({
        "node_id": "D", "goal": "退役生产者", "role": "coder",
        "status": "superseded", "dependencies": [],
        "owned_paths": ["p/x.md"], "metadata": {},
    })
    orch.task_graph.add_node({
        "node_id": "L", "goal": "在世继承者", "role": "coder",
        "status": "ready", "dependencies": ["D"],
        "owned_paths": ["p/y.md"], "metadata": {"recovery_of": "D"},
    })
    orch._save_checkpoint()
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="healthy")
    assert restored.restore_checkpoint() is True
    l_node = restored.task_graph.get_node("L")
    from AstralNexus.core.output_paths import declared_relative
    assert sorted(l_node.owned_paths) == sorted(declared_relative(path) for path in ["p/x.md", "p/y.md"])


def test_blocked_without_marker_keeps_ownership(tmp_path):
    """无转移标记、非终局的 blocked 是正常等待者：所有权分毫不动。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id="nomarker")
    orch.task_graph.add_node({
        "node_id": "B", "goal": "等待依赖的普通阻塞节点", "role": "coder",
        "status": "blocked", "dependencies": [],
        "owned_paths": ["p/b.md"], "metadata": {},
    })
    orch._save_checkpoint()
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="nomarker")
    assert restored.restore_checkpoint() is True
    assert list(restored.task_graph.get_node("B").owned_paths) == ["p/b.md"]


def test_transfer_to_retired_transferee_is_skipped_not_stripped(tmp_path):
    """受让者已退役时保守处理：不剥离、留痕，等待治理重新分配。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id="retired-transferee")
    orch.task_graph.add_node({
        "node_id": "X", "goal": "已退役受让者", "role": "coder",
        "status": "superseded", "dependencies": [],
        "owned_paths": [], "metadata": {},
    })
    orch.task_graph.add_node({
        "node_id": "Y", "goal": "带标记的终局节点", "role": "coder",
        "status": "blocked", "dependencies": [],
        "owned_paths": ["p/y.md"],
        "metadata": {
            "_terminal_reason": "recovery_exhausted",
            "ownership_transfer_to": "X",
        },
    })
    orch.runtime_state_manager.state.checkpoint_schema_version = 0
    orch._save_checkpoint()
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="retired-transferee")
    assert restored.restore_checkpoint() is True
    y = restored.task_graph.get_node("Y")
    assert list(y.owned_paths) == ["p/y.md"]  # 保守：不剥离
    skipped = _trace_events(tmp_path, "checkpoint.ownership_transfer_strip_skipped")
    assert skipped, "跳过剥离也必须留痕"


def test_metadata_ghost_ownership_is_also_swept(tmp_path):
    """Live gen13 取证形状（部分撕裂）：owned_paths 列已被剥离为空，但
    metadata.owned_paths / metadata.output_artifacts 副本残留。验证器候选
    快照在列为空时回退读 metadata（governor.py:1013），幽灵所有权会继续
    毒化后续补丁的冲突扫描——完成的转移必须清扫两个平面。"""
    orch = Orchestrator(tmp_path, max_iterations=1, run_id="ghost")
    orch.runtime_state_manager.state.checkpoint_schema_version = 0
    orch.task_graph.add_node({
        "node_id": "S2", "goal": "受让者", "role": "coder",
        "status": "admitted", "dependencies": [],
        "owned_paths": PATHS_ORDER_2, "metadata": {"recovery_of": "T2"},
    })
    orch.task_graph.add_node({
        "node_id": "T2", "goal": "部分撕裂的终局节点", "role": "coder",
        "status": "blocked", "dependencies": [],
        "owned_paths": [],  # 列已剥离
        "metadata": {
            "_terminal_failure": True,
            "_terminal_reason": "recovery_exhausted",
            "ownership_transfer_to": "S2",
            # 撕裂残留：metadata 副本仍持有全部所有权
            "owned_paths": PATHS_ORDER_2,
            "output_artifacts": ["ART-REPORT", "ART-EVIDENCE"],
        },
    })
    orch._save_checkpoint()

    # Create the historical tear in the persisted row, not through a modern
    # canonical constructor which correctly suppresses stale metadata now.
    import sqlite3
    import json
    with sqlite3.connect(tmp_path / 'task_graph.sqlite') as database:
        row = database.execute("SELECT metadata FROM nodes WHERE run_id='ghost' AND node_id='T2'").fetchone()
        metadata = json.loads(row[0])
        metadata['owned_paths'] = PATHS_ORDER_2
        metadata['output_artifacts'] = ['ART-REPORT', 'ART-EVIDENCE']
        database.execute("UPDATE nodes SET owned_paths='[]',output_artifacts='[]',metadata=? WHERE run_id='ghost' AND node_id='T2'", (json.dumps(metadata),))
    restored = Orchestrator(tmp_path, max_iterations=1, run_id="ghost")
    assert restored.restore_checkpoint() is True
    t2 = restored.task_graph.get_node("T2")
    assert list(t2.owned_paths) == []
    assert list(t2.metadata.get("owned_paths") or []) == [], "metadata 幽灵必须被清扫"
    assert list(t2.metadata.get("output_artifacts") or []) == []
    assert t2.metadata.get("_ownership_transfer_strip_completed") is True
    # 受让者所有权分毫不动
    s2 = restored.task_graph.get_node("S2")
    assert sorted(s2.owned_paths) == sorted(PATHS_ORDER_2)
    completed = _trace_events(tmp_path, "checkpoint.ownership_transfer_completed")
    assert completed, "metadata 平面的清扫同样必须留痕"
    payload = completed[-1].get("payload") or {}
    assert payload.get("node_id") == "T2"
    assert payload["canonical_stripped_paths"] == []
    assert sorted(payload["ignored_legacy_paths"]) == sorted(PATHS_ORDER_2)
    assert sorted(payload.get("stripped_paths") or []) == sorted(PATHS_ORDER_2)
    assert sorted(payload.get("stripped_artifacts") or []) == ["ART-EVIDENCE", "ART-REPORT"]
