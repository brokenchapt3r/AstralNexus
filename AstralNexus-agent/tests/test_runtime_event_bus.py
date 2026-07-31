from __future__ import annotations

import json

from AstralNexus.runtime.event_bus import RuntimeEventBus
from AstralNexus.runtime.trace import TraceRecorder


def test_runtime_event_bus_maps_and_redacts_private_reasoning(tmp_path):
    received = []
    bus = RuntimeEventBus(tmp_path)
    bus.subscribe(received.append)

    events = bus.ingest_trace(
        "governor.graph_patch.proposed",
        {
            "add_node_count": 3,
            "planning_explanation": "按检索、实现、验证职责拆分。",
            "reason_codes": ["CAPABILITY_SEPARATION"],
            "chain_of_thought": "private",
            "thinking": "private too",
        },
    )

    assert events[0]["event_type"] == "planning.summary"
    payload = events[0]["payload"]
    assert payload["rationale"] == "按检索、实现、验证职责拆分。"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "chain_of_thought" not in serialized
    assert "private" not in serialized
    assert received == events
    assert (tmp_path / "runtime_events.jsonl").exists()


def test_trace_recorder_mirrors_public_runtime_events(tmp_path):
    displayed = []
    trace = TraceRecorder(tmp_path, event_callback=displayed.append)

    trace.record(
        "governor.graph_patch.proposed",
        {
            "add_node_count": 2,
            "planning_explanation": "两个无依赖节点并行。",
        },
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event_type"] for row in rows] == [
        "governor.graph_patch.proposed",
        "planning.summary",
    ]
    assert displayed[-1]["event_type"] == "planning.summary"
    assert displayed[-1]["runtime_normalized"] is True


def test_runtime_event_bus_deduplicates_identical_public_events(tmp_path):
    bus = RuntimeEventBus(tmp_path)
    payload = {
        "add_node_count": 3,
        "planning_explanation": "按检索、撰写、验证拆分。",
    }

    first = bus.ingest_trace("governor.graph_patch.proposed", payload)
    duplicate = bus.ingest_trace("governor.graph_patch.proposed", payload)

    assert len(first) == 1
    assert duplicate == []
    assert first[0]["payload"]["level"] == "info"


def test_runtime_event_bus_projects_worker_public_reasoning(tmp_path):
    bus = RuntimeEventBus(tmp_path)
    events = bus.ingest_trace(
        "node.result_submitted",
        {
            "node_id": "R-01",
            "role": "researcher",
            "status": "completed",
            "summary": "已完成双来源核验",
            "public_reasoning": "选择两个独立来源以降低单点证据风险。",
            "reasoning": "private raw field",
        },
    )

    payload = events[0]["payload"]
    assert events[0]["event_type"] == "agent.activity"
    assert payload["rationale"] == "选择两个独立来源以降低单点证据风险。"
    assert "private raw field" not in json.dumps(payload, ensure_ascii=False)


def test_runtime_event_bus_marks_delivery_complete_only_after_final_summary(
    tmp_path,
):
    bus = RuntimeEventBus(tmp_path)
    graph_done = bus.ingest_trace("orchestrator.completed", {})
    delivery_done = bus.ingest_trace(
        "interactive.run.summary",
        {"status": "completed", "summary": "交付物已落盘并通过验收"},
    )

    assert graph_done[0]["event_type"] == "agent.activity"
    assert graph_done[0]["payload"]["status"] == "verifying"
    assert delivery_done[0]["event_type"] == "task.completed"
    assert delivery_done[0]["payload"]["status"] == "completed"


def test_trace_node_output_emits_artifact_events(tmp_path):
    trace = TraceRecorder(tmp_path)
    trace.record_node_output(
        "N-01",
        {"role": "coder", "status": "completed", "content": "done"},
    )

    public = [event for event in trace.event_bus.history() if event["event_type"] == "artifact.created"]
    assert len(public) == 2
    assert {event["payload"]["details"]["path"].split(".")[-1] for event in public} == {"json", "md"}
