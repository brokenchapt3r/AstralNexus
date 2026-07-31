from AstralNexus.runtime.trace import TraceRecorder


def test_trace_events_have_common_protocol_fields(tmp_path):
    recorder = TraceRecorder(tmp_path, default_node_id="T-001")
    event = recorder.record("task.started", {"task_version": 3})
    payload = event["payload"]
    assert payload["event_version"] == 1
    assert payload["task_version"] == 3
    assert payload["node_id"] == "T-001"
    assert payload["reason_codes"] == []
    assert payload["artifact_refs"] == []


def test_trace_uses_task_id_without_inventing_a_phantom_node(tmp_path):
    recorder = TraceRecorder(tmp_path)
    scoped = recorder.record("governor.worker_selected", {"task_id": "T-002"})
    unscoped = recorder.record("provider.capacity.updated", {"available": 4})

    assert scoped["payload"]["node_id"] == "T-002"
    assert "node_id" not in unscoped["payload"]
