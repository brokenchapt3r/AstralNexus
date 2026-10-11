"""第二批③ 展示层边界证明（doc 17）：/api/snapshot 返回有界预览+显式截断标记，
不返回上百MB完整快照；内部状态（审计/重放来源）不被投影改动；缺失字段不伪造。"""
from __future__ import annotations

import json
from pathlib import Path

from AstralNexus.dashboard.state import LiveRunState


def _event(event_type, node_id=None, **payload):
    body = dict(payload)
    if node_id:
        body["node_id"] = node_id
    return {
        "event_type": event_type,
        "recorded_at": "2026-01-01T00:00:00Z",
        "payload": body,
    }


def test_snapshot_returns_bounded_output_preview(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event("node.started", "N-1", role="coder", goal="g", dependencies=[]))
    state.apply(_event("llm.text.delta", "N-1", text="x" * 50_000))
    state.apply(_event("task.structured_message", "N-1", summary="S" * 30_000))

    snapshot = state.snapshot()
    node = next(item for item in snapshot["nodes"] if item["id"] == "N-1")
    assert node["output_truncated"] is True
    assert node["output_bytes"] == 50_000
    assert len(node["output"]) == LiveRunState._SNAPSHOT_OUTPUT_PREVIEW_CHARS
    assert node["summary_truncated"] is True
    assert node["summary_bytes"] == 30_000
    assert len(node["summary"]) == LiveRunState._SNAPSHOT_SUMMARY_PREVIEW_CHARS

    # The internal record (audit/replay source) is untouched by presentation.
    assert len(state.nodes["N-1"]["output"]) == 50_000
    assert len(state.nodes["N-1"]["summary"]) == 30_000

    # Whole-snapshot size stays presentation-scale, not hundreds of MB.
    assert len(json.dumps(snapshot, ensure_ascii=False, default=str)) < 300_000


def test_snapshot_small_outputs_are_untouched(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event("node.started", "N-2", role="coder", goal="g", dependencies=[]))
    state.apply(_event("llm.text.delta", "N-2", text="short answer"))

    node = next(item for item in state.snapshot()["nodes"] if item["id"] == "N-2")
    assert node["output"] == "short answer"
    assert "output_truncated" not in node
    assert "output_bytes" not in node


def test_snapshot_preview_keeps_latest_tail(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event("node.started", "N-3", role="coder", goal="g", dependencies=[]))
    state.apply(_event("llm.text.delta", "N-3", text="A" * 5_000 + "TAILMARK"))

    node = next(item for item in state.snapshot()["nodes"] if item["id"] == "N-3")
    assert node["output"].endswith("TAILMARK")
    assert node["output_truncated"] is True
