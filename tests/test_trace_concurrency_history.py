from __future__ import annotations
import json
import subprocess
import sys
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.core.artifacts import append_jsonl


def test_recreated_recorder_never_overwrites_prior_node_io(tmp_path):
    first = TraceRecorder(tmp_path)
    old = first.record_node_input("same", {"goal": "old"})
    output, _ = first.record_node_output("same", {"content": "old output"})
    old_bytes, output_bytes = old.read_bytes(), output.read_bytes()
    second = TraceRecorder(tmp_path)
    new = second.record_node_input("same", {"goal": "new"})
    new_output, _ = second.record_node_output("same", {"content": "new output"})
    assert new != old and new_output != output
    assert old.read_bytes() == old_bytes and output.read_bytes() == output_bytes


def test_parallel_same_node_input_output_keeps_each_execution_identity(tmp_path):
    recorder = TraceRecorder(tmp_path)
    barrier = Barrier(8)
    def execute(index):
        source = recorder.record_node_input("same", {"goal": str(index)})
        barrier.wait(timeout=5)
        output, _ = recorder.record_node_output("same", {"content": str(index)})
        incoming, outgoing = json.loads(source.read_text(encoding="utf-8")), json.loads(output.read_text(encoding="utf-8"))
        assert incoming["execution_id"] == outgoing["execution_id"]
        assert incoming["goal"] == outgoing["content"]
        return source, output
    with ThreadPoolExecutor(max_workers=8) as pool:
        files = list(pool.map(execute, range(8)))
    assert len({item for pair in files for item in pair}) == 16


def test_parallel_events_are_complete_ordered_per_recorder_and_callbacks_reenter(tmp_path):
    recorder = TraceRecorder(tmp_path)
    observed = []
    def observe(event):
        recorder.get_events_by_type("parallel")
        observed.append(event["event_type"])
    recorder.event_callback = observe
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: recorder.record("parallel", {"index": index, "payload": "长" * 20_000}), range(32)))
    records = [json.loads(line) for line in recorder.trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == len(observed) == 32
    assert [item["trace_sequence"] for item in records] == list(range(1, 33))
    assert {item["payload"]["index"] for item in records} == set(range(32))


def test_cross_process_jsonl_append_does_not_interleave_large_records(tmp_path):
    path = tmp_path / "shared.jsonl"
    script = "from pathlib import Path; import sys; from AstralNexus.core.artifacts import append_jsonl; p=Path(sys.argv[1]); [append_jsonl(p, {'process':sys.argv[2], 'index':i, 'text':'x'*40000}) for i in range(16)]"
    processes = [subprocess.Popen([sys.executable, "-c", script, str(path), str(index)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(2)]
    try:
        for process in processes:
            _, errors = process.communicate(timeout=20)
            assert process.returncode == 0, errors.decode(errors="replace")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 32
        assert len({(row["process"], row["index"]) for row in rows}) == 32
        assert all(len(row["text"]) == 40_000 for row in rows)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_repeated_output_without_new_input_retains_previous_output_file(tmp_path):
    recorder = TraceRecorder(tmp_path)
    recorder.record_node_input("node", {"goal": "task"})
    first, _ = recorder.record_node_output("node", {"content": "first"})
    second, _ = recorder.record_node_output("node", {"content": "second"})
    assert first != second
    assert json.loads(first.read_text(encoding="utf-8"))["content"] == "first"


def test_lost_owner_cannot_write_node_io_and_diagnostic_cannot_change_live_state(tmp_path):
    from AstralNexus.runtime.trace import TraceOwnershipLost
    from AstralNexus.dashboard.state import LiveRunState
    valid = [True]
    state = LiveRunState(tmp_path)
    recorder = TraceRecorder(tmp_path, event_callback=state.apply,
        owner_token_provider=lambda: {"owner_id": "owner", "generation": 1} if valid[0] else None)
    recorder.record_node_input("node", {"goal": "task", "role": "coder"})
    valid[0] = False
    with pytest.raises(TraceOwnershipLost): recorder.record_node_output("node", {"content": "unowned"})
    diagnostic = recorder.record("node.completed", {"node_id": "node", "role": "coder", "status": "completed"})
    assert diagnostic["payload"]["trace_owner_valid"] is False
    assert diagnostic["payload"]["generation"] == 1
    assert state.nodes["node"]["status"] != "completed"
    assert not list(recorder.node_io_dir.glob("*__output.json"))
    assert recorder.get_events_by_type("node.completed")


def test_owner_provider_fault_is_never_unfenced_node_io(tmp_path):
    from AstralNexus.runtime.trace import TraceOwnershipLost
    def faulty(): raise OSError("lease database unavailable")
    recorder = TraceRecorder(tmp_path, owner_token_provider=faulty)
    with pytest.raises(TraceOwnershipLost): recorder.record_node_input("node", {"goal": "task"})
    event = recorder.record("ownership.failure", {})
    assert event["payload"]["trace_owner_valid"] is False
    assert event["payload"]["trace_owner_error"] == "OSError"


def test_real_generation_takeover_invalidates_old_trace_writer(tmp_path):
    from datetime import datetime, timezone, timedelta
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    from AstralNexus.runtime.trace import TraceOwnershipLost
    now = [datetime(2026, 10, 9, tzinfo=timezone.utc)]
    coordinator = RunCoordinator(tmp_path, run_id="trace-run", now_fn=lambda: now[0])
    old = coordinator.acquire("A")
    recorder = TraceRecorder(tmp_path, owner_token_provider=lambda: coordinator.fence_token(old))
    recorder.record_node_input("node", {"goal": "old"})
    now[0] += timedelta(seconds=1000)
    newer = coordinator.acquire("B")
    assert newer.generation > old.generation
    with pytest.raises(TraceOwnershipLost): recorder.record_node_output("node", {"content": "late"})
    assert coordinator.current()["owner_id"] == "B"
