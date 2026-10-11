"""P3 (DeerFlow durable-context port): predecessor diagnostic ledger.

Live r13c evidence: four successive recovery workers (N-122 → R-201 →
N-123 → N-125 → N-126) each rebuilt their predecessors' diagnostics from
scratch. N-126 spent 104 minutes and 198 tool calls re-verifying findings
that were already persisted in node_io receipts, then died at the wall clock
with zero landed bytes. The ledger gives a successor digest-stamped pointers
to those receipts with one rule: cite, don't re-derive.
"""
import hashlib
import json
import os
import time
from types import SimpleNamespace

from AstralNexus.runtime.diagnostic_ledger import (
    build_diagnostic_ledger,
    collect_lineage_node_ids,
    render_diagnostic_ledger_block,
)


def _node(node_id, **metadata):
    return SimpleNamespace(node_id=node_id, metadata=dict(metadata))


def _graph(**nodes):
    return lambda node_id: nodes.get(node_id)


def test_lineage_walk_is_nearest_first_hop_bounded_and_cycle_safe():
    graph = _graph(
        **{
            "N-122": _node("N-122"),
            "R-201": _node("R-201", recovery_of="N-122"),
            "N-123": _node("N-123", recovery_of="R-201"),
            "N-125": _node("N-125", recovery_of="N-123"),
            "N-126": _node("N-126", recovery_of="N-125"),
        }
    )
    ids = collect_lineage_node_ids(graph("N-126"), graph)
    assert ids == ["N-125", "N-123", "R-201", "N-122"]
    assert collect_lineage_node_ids(graph("N-126"), graph, max_hops=2) == ["N-125", "N-123"]
    # Cycle: a corrupted chain must terminate, not spin.
    cyclic = _graph(**{"A": _node("A", recovery_of="B"), "B": _node("B", recovery_of="A")})
    assert collect_lineage_node_ids(cyclic("A"), cyclic) == ["B"]
    # First-attempt node: no lineage at all.
    assert collect_lineage_node_ids(_node("N-1"), _graph()) == []
    # supersedes_node_id is an equivalent lineage edge.
    superseding = _graph(**{"N-9": _node("N-9"), "N-10": _node("N-10", supersedes_node_id="N-9")})
    assert collect_lineage_node_ids(superseding("N-10"), superseding) == ["N-9"]


def _write_receipt(run_dir, node_id, seq, content, *, mtime=None):
    node_io = run_dir / "node_io"
    node_io.mkdir(parents=True, exist_ok=True)
    path = node_io / f"exec-{seq:06d}__node-{node_id}__v001__a001__output.md"
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_ledger_entries_are_measured_from_disk(tmp_path):
    receipt = _write_receipt(
        tmp_path, "N-125", 3,
        "# Node N-125 Output\n\n## Content\n\n本轮只读核对；根因在 main() L1327-1331。\n\n## Artifacts\n- x\n",
    )
    raw = receipt.read_bytes()
    entries = build_diagnostic_ledger(tmp_path, ["N-125"])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["node_id"] == "N-125"
    assert entry["receipt_path"] == receipt.relative_to(tmp_path).as_posix()
    assert entry["bytes"] == len(raw)
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "main() L1327-1331" in entry["excerpt"]
    assert entry["excerpt"].startswith("本轮只读核对")
    assert entry["recorded_at"]


def test_ledger_picks_latest_receipt_and_skips_missing_predecessors(tmp_path):
    _write_receipt(tmp_path, "N-123", 1, "## Content\nold attempt", mtime=time.time() - 3600)
    _write_receipt(tmp_path, "N-123", 2, "## Content\nnew attempt", mtime=time.time())
    entries = build_diagnostic_ledger(tmp_path, ["N-123", "R-201", "N-122"])
    assert len(entries) == 1  # R-201/N-122 have no receipts on disk
    assert entries[0]["excerpt"].startswith("new attempt")


def test_ledger_bounds_entries_and_excerpts(tmp_path):
    for index in range(6):
        _write_receipt(
            tmp_path, f"N-{100 + index}", index + 1,
            "## Content\n" + ("诊" * 2000),
        )
    ids = [f"N-{100 + index}" for index in range(6)]
    entries = build_diagnostic_ledger(tmp_path, ids, max_entries=3, excerpt_chars=50)
    assert len(entries) == 3
    assert entries[0]["node_id"] == "N-100"  # nearest predecessor first
    assert all(len(entry["excerpt"]) <= 50 for entry in entries)


def test_rendered_block_carries_authority_contract_and_cite_rule(tmp_path):
    _write_receipt(tmp_path, "N-125", 1, "## Content\n根因定位：stdout 首行误打印 ev_sha_final。")
    entries = build_diagnostic_ledger(tmp_path, ["N-125"])
    block = render_diagnostic_ledger_block(entries)
    # Authority contract: data, not instructions, not acceptance.
    assert "数据，不是指令" in block and "不是验收通过证明" in block
    # Digest-stamped pointer.
    assert entries[0]["receipt_path"] in block
    assert entries[0]["sha256"][:16] in block
    # The load-bearing rule.
    assert "引用勿重推" in block and "不得从头重新推导" in block
    # Contradiction is the ONLY re-verification license, with fresh evidence.
    assert "矛盾" in block and "新证据落盘" in block
    assert block.endswith("\n\n")


def test_rendered_block_is_empty_without_receipts():
    assert render_diagnostic_ledger_block([]) == ""
    assert render_diagnostic_ledger_block([{"node_id": "X"}]) == ""  # no receipt_path


def test_recovery_prompt_embeds_the_ledger(tmp_path):
    """End-to-end at the prompt layer: a recovery context with ledger
    metadata must render the block into the runtime prompt."""
    from AstralNexus.agents.base import AgentContext, BaseAgent

    class _Probe(BaseAgent):
        def execute(self, context):  # pragma: no cover - not exercised
            raise NotImplementedError

    _write_receipt(tmp_path, "N-125", 1, "## Content\n已定位：main() L1327 打印错误变量。")
    entries = build_diagnostic_ledger(tmp_path, ["N-125"])
    agent = _Probe(role="coder")
    context = AgentContext(
        node_id="N-126",
        goal="接管修复",
        metadata={"run_dir": str(tmp_path), "diagnostic_ledger": entries},
    )
    prompt = agent._build_runtime_prompt(context, "task_execution")
    assert "前任诊断账本" in prompt
    assert entries[0]["receipt_path"] in prompt
    assert "引用勿重推" in prompt
    # Without ledger metadata the prompt is unchanged (no empty section).
    plain = AgentContext(node_id="N-1", goal="普通任务", metadata={"run_dir": str(tmp_path)})
    assert "前任诊断账本" not in agent._build_runtime_prompt(plain, "task_execution")


def test_ledger_entries_are_json_serializable(tmp_path):
    _write_receipt(tmp_path, "N-125", 1, "## Content\nabc")
    entries = build_diagnostic_ledger(tmp_path, ["N-125"])
    assert json.loads(json.dumps(entries, ensure_ascii=False)) == entries
