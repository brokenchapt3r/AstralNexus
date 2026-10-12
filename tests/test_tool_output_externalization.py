"""P4 (DeerFlow tool_output_budget port): per-result context budget.

Live r13c evidence: worker rounds slowed to 40-90 seconds against 87K+
token contexts because every python_exec/terminal_exec stdout (13-29 KB
draft dumps, full file listings) entered the conversation verbatim and
stayed there for the rest of the attempt — the attempt then died at the
wall clock with its work stranded in context. Oversized results are now
persisted to disk FIRST (the file is the source of truth) and replaced in
the model-bound view by a head+tail synopsis with a digest-stamped file
reference. Ledger receipts, loop detection and the trace keep seeing the
ORIGINAL result: the budget must never blind a guard.
"""
import hashlib
import json

from AstralNexus.runtime.tool_output_budget import (
    EXTERNAL_DIRNAME,
    context_cap_chars,
    externalize_tool_result,
)


def test_small_results_pass_through_untouched(tmp_path):
    result = {"status": "success", "stdout": "ok"}
    view, event = externalize_tool_result(
        result, workspace_root=tmp_path, call_id="c1", tool_round=1,
    )
    assert view is result
    assert event is None
    assert not (tmp_path / EXTERNAL_DIRNAME).exists()


def test_oversized_stdout_is_persisted_first_then_synopsised(tmp_path):
    payload = "DRAFT-LINE\n" * 3000  # ~33 KB, the live draft-dump shape
    result = {"status": "success", "returncode": 0, "stdout": payload}
    view, event = externalize_tool_result(
        result, workspace_root=tmp_path, call_id="call-abc123", tool_round=7,
    )
    # The full result is on disk, byte-identical to the serialization.
    ref_file = tmp_path / view["full_output_ref"]
    assert ref_file.is_file()
    assert json.loads(ref_file.read_text(encoding="utf-8")) == result
    digest = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, default=str).encode("utf-8", errors="replace")
    ).hexdigest()
    assert view["externalized"]["sha256"] == digest
    assert event["sha256"] == digest
    assert event["event_type"] == "llm.tool_output.externalized"
    assert event["tool_round"] == 7 and event["call_id"] == "call-abc123"
    # The in-context view is lossy by construction and says so.
    assert len(view["stdout"]) < len(payload)
    assert view["stdout"].startswith(payload[:1500])
    assert view["stdout"].endswith(payload[-1500:])
    assert "已外置" in view["stdout"] and view["full_output_ref"] in view["stdout"]
    # Non-bulky provenance keys survive verbatim.
    assert view["status"] == "success" and view["returncode"] == 0
    # The original result object is never mutated (guards keep seeing it).
    assert result["stdout"] == payload and "full_output_ref" not in result


def test_many_small_fields_collapse_to_typed_synopsis(tmp_path):
    result = {"status": "success", **{f"k{i:03d}": "v" * 60 for i in range(300)}}
    view, event = externalize_tool_result(
        result, workspace_root=tmp_path, call_id="c2", tool_round=2,
    )
    assert event is not None
    assert len(json.dumps(view, ensure_ascii=False)) <= context_cap_chars()
    assert view["status"] == "success"
    assert "k000" not in view
    assert view["full_output_ref"]


def test_disabled_by_env_cap_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_TOOL_OUTPUT_CONTEXT_CAP", "0")
    result = {"status": "success", "stdout": "X" * 40_000}
    view, event = externalize_tool_result(
        result, workspace_root=tmp_path, call_id="c3", tool_round=3,
    )
    assert view is result and event is None


def test_missing_workspace_root_fails_open(tmp_path):
    result = {"status": "success", "stdout": "X" * 40_000}
    view, event = externalize_tool_result(
        result, workspace_root=None, call_id="c4", tool_round=4,
    )
    assert view is result and event is None


def test_persistence_failure_never_blocks_the_result(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    result = {"status": "success", "stdout": "X" * 40_000}
    view, event = externalize_tool_result(
        result, workspace_root=blocker, call_id="c5", tool_round=5,
    )
    assert view is result and event is None  # fail-open: original stays


def test_stream_chat_conversation_gets_the_synopsis_and_ledger_keeps_the_original(tmp_path, monkeypatch):
    from AstralNexus.runtime.llm_client import LLMClient

    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    requests = []
    observed = []
    huge = "LOG\n" * 8000  # 32 KB stdout — the live python_exec dump shape

    def stream(messages, **kwargs):
        requests.append((list(messages), kwargs))
        if len(requests) == 1:
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "dump-1",
                "function": {"name": "python_exec", "arguments": json.dumps({"code": "print(1)"})},
            }]}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "analyzed"}},
                               {"delta": {}, "finish_reason": "stop"}]}

    def execute(_executor, calls):
        return [{"status": "success", "stdout": huge} for _ in calls]

    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", execute)
    answer = client.stream_chat(
        [{"role": "user", "content": "go"}],
        tools=[{"type": "function"}],
        workspace_root=tmp_path,
        event_callback=observed.append,
    )
    assert answer == "analyzed"

    # The second request's conversation carries the compacted view only.
    tool_messages = [m for m in requests[1][0] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    view = json.loads(tool_messages[0]["content"])
    assert len(tool_messages[0]["content"]) < len(huge)
    assert view["full_output_ref"].startswith(EXTERNAL_DIRNAME + "/")
    # The persisted file holds the untouched original result.
    persisted = json.loads((tmp_path / view["full_output_ref"]).read_text(encoding="utf-8"))
    assert persisted["stdout"] == huge
    # The externalization is observable in the trace stream.
    events = [e for e in observed if e.get("event_type") == "llm.tool_output.externalized"]
    assert len(events) == 1
    assert events[0]["sha256"] == view["externalized"]["sha256"]


def test_externalization_directory_is_runtime_scratch_not_evidence(tmp_path):
    """The directory must sit beside .astralnexus_state semantics: excluded
    from owned snapshots (pinned in test_workspace_delta.py) and named
    deterministically per call so receipts stay correlatable."""
    result = {"status": "success", "stdout": "Y" * 40_000}
    view, _ = externalize_tool_result(
        result, workspace_root=tmp_path, call_id="call/xyz:9", tool_round=12,
    )
    ref = view["full_output_ref"]
    assert ref.startswith(EXTERNAL_DIRNAME + "/r0012_")
    # Unsafe call-id characters are sanitized, never path-injected.
    assert "/" not in ref[len(EXTERNAL_DIRNAME) + 1:]
    assert ":" not in ref
