import json
import threading
import time
import pytest
from unittest.mock import patch

from AstralNexus.runtime.llm_client import LLMClient, LLMClientError
from AstralNexus.core.exceptions import InterruptionRequested
from AstralNexus.runtime.task_execution_budget import TaskExecutionBudget


@pytest.fixture(autouse=True)
def isolated_provider_gate(monkeypatch):
    from AstralNexus.runtime.provider_request_gate import ProviderRequestGate
    monkeypatch.setattr("AstralNexus.runtime.llm_client.provider_request_gate", ProviderRequestGate())


def test_truncated_tool_response_is_not_executed_and_can_retry(tmp_path, monkeypatch):
    client = LLMClient(model='qwen-plus', api_key='test-key', interactive=False)
    requests = []
    executed = []
    def stream(messages, **kwargs):
        requests.append((list(messages), kwargs))
        if len(requests) <= 2:
            yield {'choices': [{'delta': {'tool_calls': [{
                'index': 0, 'id': f'c{len(requests)}',
                'function': {'name': 'file_write', 'arguments': json.dumps({'path': 'x.txt', 'content': 'ok'})},
            }]}}]}
            yield {'choices': [{'delta': {}, 'finish_reason': 'length' if len(requests) == 1 else 'tool_calls'}]}
        else:
            yield {'choices': [{'delta': {'content': 'verified'}}]}
    def execute(_executor, calls):
        executed.extend(calls)
        return [{'status': 'success'} for _ in calls]
    monkeypatch.setattr(client, 'stream', stream)
    monkeypatch.setattr('AstralNexus.tools.executor.ToolExecutor.execute_tool_calls', execute)
    assert client.stream_chat([{'role': 'user', 'content': 'write'}], tools=[{'type': 'function'}], workspace_root=tmp_path) == 'verified'
    assert [call['id'] for call in executed] == ['c2']
    assert requests[1][1]['tools'] is not None
    assert any('truncated_output' in str(message) for message in requests[1][0])


def test_task_tool_call_budget_is_shared_and_stops_extra_calls(tmp_path, monkeypatch):
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    executed = []
    requests = []
    observed = []

    def stream(messages, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            calls = [
                {"index": index, "id": f"budget-{index}", "function": {
                    "name": "python_exec", "arguments": json.dumps({"code": "print('ok')"}),
                }}
                for index in range(2)
            ]
            yield {"choices": [{"delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}]}
        elif len(requests) == 2:
            yield {"choices": [{"delta": {"content": "I will continue with more tools"}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    def execute(_executor, calls):
        executed.extend(calls)
        return [{"status": "success", "output": "ok"} for _ in calls]

    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", execute)
    budget = TaskExecutionBudget(max_tool_calls=1)
    answer = client.stream_chat(
        [{"role": "user", "content": "work"}],
        tools=[{"type": "function"}],
        workspace_root=tmp_path,
        task_execution_budget=budget,
        event_callback=observed.append,
    )

    assert answer == "I will continue with more tools"
    assert [call["id"] for call in executed] == ["budget-0"]
    assert budget.tool_calls_used == 1
    assert requests[1]["tools"] is None
    assert [event["reason_code"] for event in observed if event.get("event_type") == "llm.task_budget.exhausted"] == ["max_tool_calls"]


def test_task_seconds_budget_blocks_calls_after_deadline(tmp_path, monkeypatch):
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    now = [0.0]
    executed = []
    observed = []
    requests = []

    def stream(messages, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            now[0] = 2.0
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "late-call", "function": {
                    "name": "python_exec", "arguments": json.dumps({"code": "print('late')"}),
                },
            }]}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "unfinished"}, "finish_reason": "stop"}]}

    def execute(_executor, calls):
        executed.extend(calls)
        return [{"status": "success"} for _ in calls]

    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", execute)
    budget = TaskExecutionBudget(max_seconds=1, clock=lambda: now[0])
    answer = client.stream_chat(
        [{"role": "user", "content": "work"}],
        tools=[{"type": "function"}],
        workspace_root=tmp_path,
        task_execution_budget=budget,
        event_callback=observed.append,
    )

    assert answer == "unfinished"
    assert executed == []
    assert [event["reason_code"] for event in observed if event.get("event_type") == "llm.task_budget.exhausted"] == ["max_seconds"]


def test_truncated_text_cannot_be_reported_as_completion(monkeypatch):
    client = LLMClient(model='qwen-plus', api_key='test-key', interactive=False)
    monkeypatch.setattr(client, 'stream', lambda *args, **kwargs: iter([
        {'choices': [{'delta': {'content': 'Everything passed'}, 'finish_reason': 'length'}]},
    ]))
    with pytest.raises(LLMClientError, match='truncated'):
        client.stream_chat([{'role': 'user', 'content': 'run'}])


class _StreamResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n'
        yield b'\n'
        yield b'data: [DONE]\n'


class _FlakyStreamResponse(_StreamResponse):
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"recovered"}}]}\n'
        raise ConnectionError("stream disconnected")


def test_llm_client_stream_yields_sse_events(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_API_KEY", "test-key")
    observed = []
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=_StreamResponse()):
        events = list(client.stream(
            [{"role": "user", "content": "hello"}],
            event_callback=observed.append,
        ))
    assert events[0]["choices"][0]["delta"]["content"] == "hello"
    assert client.last_request_stats["stream"] is True
    assert [event["event_type"] for event in observed] == [
        "llm.stream.started",
        "llm.stream.completed",
    ]


def test_llm_stream_cancellation_closes_response_mid_stream(monkeypatch):
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    cancel = {"requested": False}
    observed = []

    class TrackedResponse(_StreamResponse):
        closed = False

        def __exit__(self, *_args):
            self.closed = True
            return False

    response = TrackedResponse()
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=response):
        stream = client.stream(
            [{"role": "user", "content": "cancel while streaming"}],
            cancel_check=lambda: cancel["requested"],
            event_callback=observed.append,
        )
        first = next(stream)
        assert first["choices"][0]["delta"]["content"] == "hello"
        cancel["requested"] = True
        with pytest.raises(InterruptionRequested, match="cancelled"):
            next(stream)

    assert response.closed is True
    assert any(
        event.get("event_type") == "llm.interrupted"
        and event.get("phase") == "during_stream"
        for event in observed
    )


def test_llm_stream_cancellation_interrupts_a_silent_read(monkeypatch):
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    cancel = threading.Event()
    released = threading.Event()
    observed = []

    class BlockingResponse(_StreamResponse):
        closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n'
            if not released.wait(2):
                raise AssertionError("cancel did not close the blocked stream")
            raise OSError("response closed during blocked read")

        def close(self):
            self.closed = True
            released.set()

    response = BlockingResponse()
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=response):
        stream = client.stream(
            [{"role": "user", "content": "cancel a silent response"}],
            cancel_check=cancel.is_set,
            event_callback=observed.append,
        )
        assert next(stream)["choices"][0]["delta"]["content"] == "hello"
        threading.Timer(0.05, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(InterruptionRequested, match="cancelled"):
            next(stream)

    assert time.monotonic() - started < 1.0
    assert response.closed is True
    assert any(
        event.get("event_type") == "llm.interrupted"
        for event in observed
    )


def test_stream_chat_passes_cancel_check_to_transport(monkeypatch):
    client = LLMClient(model="qwen-flash", api_key="test-key", interactive=False)
    cancel_check = lambda: False

    def fake_stream(messages, **kwargs):
        assert kwargs["cancel_check"] is cancel_check
        yield {"choices": [{"delta": {"content": "response"}}]}

    monkeypatch.setattr(client, "stream", fake_stream)
    assert client.stream_chat(
        [{"role": "user", "content": "test cancellation"}],
        cancel_check=cancel_check,
    ) == "response"


def test_stream_disconnect_after_delta_cannot_splice_responses(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "1")
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=_FlakyStreamResponse()) as request:
        with pytest.raises(ConnectionError, match="disconnected"):
            list(client.stream([{"role": "user", "content": "hello"}]))
    assert request.call_count == 1


def test_stream_can_retry_connection_failure_before_any_delta(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_STREAM_RETRIES", "1")
    client = LLMClient(model="qwen-flash", api_key="test-key", timeout=5)
    with patch("AstralNexus.runtime.llm_client.urlopen", side_effect=[ConnectionError("connect"), _StreamResponse()]) as request:
        events = list(client.stream([{"role": "user", "content": "hello"}]))
    assert request.call_count == 2
    assert events[0]["choices"][0]["delta"]["content"] == "hello"


def test_incomplete_tool_stream_never_executes_partial_call(monkeypatch, tmp_path):
    class Incomplete(_StreamResponse):
        def __iter__(self):
            event = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {
                "name": "file_write", "arguments": '{"path":"x.txt","content":"partial"}'}}]}}]}
            yield ("data: " + json.dumps(event)).encode()
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=Incomplete()), patch(
        "AstralNexus.tools.executor.ToolExecutor.execute_tool_calls") as execute:
        with pytest.raises(LLMClientError, match="incomplete"):
            client.stream_chat([{"role": "user", "content": "write"}], workspace_root=tmp_path)
    execute.assert_not_called()


def test_stream_repeated_failure_requests_targeted_recovery(tmp_path, monkeypatch):
    client = LLMClient(model='qwen-plus', api_key='test-key', interactive=False)
    requests = []
    def stream(messages, **kwargs):
        requests.append((list(messages), kwargs))
        if len(requests) <= 2:
            yield {'choices': [{'delta': {'tool_calls': [{
                'index': 0, 'id': f'c{len(requests)}',
                'function': {'name': 'terminal_exec', 'arguments': '{"command":"broken"}'},
            }]}, 'finish_reason': 'tool_calls'}]}
        else:
            yield {'choices': [{'delta': {'content': 'blocked: command failed'}}]}
    monkeypatch.setattr(client, 'stream', stream)
    monkeypatch.setattr('AstralNexus.tools.executor.ToolExecutor.execute_tool_calls',
                        lambda self, calls: [{'status': 'failed', 'error': 'invalid command'} for _ in calls])
    result = client.stream_chat([{'role': 'user', 'content': 'verify'}],
                               tools=[{'type': 'function'}], workspace_root=tmp_path)
    assert result == 'blocked: command failed'
    feedback = requests[2][0]
    tool_result = json.loads([m for m in feedback if m['role'] == 'tool'][-1]['content'])
    assert tool_result['status'] == 'failed'
    assert tool_result['recovery']['action'] == 'switch_tool_or_replan'
    assert tool_result['error'] == 'invalid command'
    assert any('最小修复' in m['content'] for m in feedback if m['role'] == 'user')
    assert requests[2][1]['tools'] is not None


def test_compacted_history_survives_tool_round_and_state_is_ephemeral(monkeypatch):
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    compression_inputs = []
    payloads = []
    def compress(messages, tools, **kwargs):
        compression_inputs.append(list(messages))
        if len(compression_inputs) == 1:
            return [{"role": "user", "content": "retained summary"}]
        return messages
    class Response(_StreamResponse):
        def __iter__(self):
            if len(payloads) == 1:
                event = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {
                    "name": "python_exec", "arguments": '{"code":"print(1)"}'}}]}}]}
            else:
                event = {"choices": [{"delta": {"content": "done"}}]}
            yield ("data: " + json.dumps(event)).encode()
            yield b"data: [DONE]"
    def open_request(request, **kwargs):
        payloads.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr(client, "_maybe_compress_conversation", compress)
    monkeypatch.setattr("AstralNexus.runtime.llm_client.urlopen", open_request)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls",
                        lambda self, calls: [{"status": "success", "returncode": 0}])
    assert client.stream_chat([{"role": "user", "content": "large old history"}], tools=[{"type": "function", "function": {"name": "python_exec", "parameters": {"type": "object"}}}]) == "done"
    assert compression_inputs[1][0]["content"] == "retained summary"
    assert "large old history" not in str(compression_inputs[1])
    assert any(message["role"] == "tool" for message in payloads[1]["messages"])


def test_runtime_state_bypasses_compression_without_entering_history(monkeypatch):
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    retained = []
    state = {"role": "user", "content": "exact current file digest"}
    monkeypatch.setattr(client, "_maybe_compress_conversation",
                        lambda messages, tools, **kwargs: [messages[0]])
    with patch("AstralNexus.runtime.llm_client.urlopen", return_value=_StreamResponse()) as transport:
        list(client.stream([{"role": "user", "content": "goal"}],
                           prepared_messages_callback=retained.extend, current_state=state))
    payload = json.loads(transport.call_args.args[0].data)
    assert payload["messages"][-1] == state
    assert state not in retained


def test_withdrawn_tools_cannot_continue_hidden_execution(monkeypatch, tmp_path):
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    calls = []
    executed = []
    def stream(messages, **kwargs):
        calls.append(kwargs.get("tools"))
        yield {"choices": [{"delta": {"content": "partial evidence"}}]}
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "read", "function": {
            "name": "file_read", "arguments": '{"path":"a.txt"}'}}]}}]}
    def execute(self, batch):
        executed.extend(batch)
        return [{"status": "success"} for call in batch]
    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", execute)
    # Fix 14 (live r11 resume #21): the round cap withdraws tools; a model
    # that keeps demanding tools is nudged once toward a text wrap-up and
    # then ended gracefully — but no tool may ever execute post-withdrawal,
    # and the accumulated evidence text becomes the submitted result.
    result = client.stream_chat([{"role": "user", "content": "read"}],
                                tools=[{"type": "function"}], max_tool_rounds=1,
                                workspace_root=tmp_path)
    assert len(executed) == 1
    assert calls[0] is not None
    assert calls[1] is None and calls[2] is None
    assert "partial evidence" in result


def test_withdrawn_tools_nudge_then_graceful_text_end(monkeypatch):
    """Fix 14: five consecutive tester generations died with a hard
    LLMClientError when the model kept emitting tool calls after a guard
    withdrawal — forfeiting collected receipts and bypassing verification,
    so the governed coder pivot could never trigger.  The runtime must nudge
    once, then end gracefully with the accumulated text."""
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    requests = []
    events = []

    def stream(messages, **kwargs):
        requests.append(list(messages))
        yield {"choices": [{"delta": {"content": "evidence"}}]}
        yield {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": f"c{len(requests)}",
            "function": {"name": "terminal_exec", "arguments": "{}"},
        }]}}]}

    monkeypatch.setattr(client, "stream", stream)
    result = client.stream_chat(
        [{"role": "user", "content": "test"}],
        tools=None,
        event_callback=events.append,
    )
    assert "evidence" in result
    assert len(requests) == 2  # exactly one nudge round, then graceful end
    nudge = requests[1][-1]
    assert nudge["role"] == "user" and "工具已被收回" in nudge["content"]
    assert client.last_request_stats["post_withdrawal"] == "graceful_text_end"
    kinds = [e["event_type"] for e in events]
    assert "llm.post_withdrawal_nudge" in kinds
    assert "llm.post_withdrawal_graceful_end" in kinds


def test_withdrawn_tools_graceful_end_without_text_is_honest(monkeypatch):
    """A withdrawn model that produced no text at all must yield an honest
    fallback — never a raise, never a fabricated completion."""
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)

    def stream(messages, **kwargs):
        yield {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "c1",
            "function": {"name": "terminal_exec", "arguments": "{}"},
        }]}}]}

    monkeypatch.setattr(client, "stream", stream)
    result = client.stream_chat([{"role": "user", "content": "test"}])
    assert "execution ended after tool withdrawal" in result
    assert "remain on disk" in result


def test_successful_read_loop_hands_off_without_provider_cooperation(monkeypatch, tmp_path):
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    events = []
    attempts = []
    def stream(messages, **kwargs):
        attempts.append(1)
        assert len(attempts) <= 6, "runtime failed to take back control"
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": str(len(attempts)),
            "function": {"name": "file_read", "arguments": '{"path":"same.txt"}'}}]}}]}
    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls",
                        lambda self, calls: [{"status": "success", "content": "unchanged"} for _ in calls])
    with pytest.raises(LLMClientError, match="runtime_no_progress"):
        client.stream_chat([{"role": "user", "content": "deliver documents"}],
                           tools=[{"type": "function"}], workspace_root=tmp_path, event_callback=events.append)
    assert [e["action"] for e in events if e.get("event_type") == "llm.progress_stalled"] == ["replan", "handoff"]


def test_mid_stream_cut_is_reissued_once_without_text_duplication(monkeypatch):
    """Fix L (doc 18 attempt8): attempt7 lost 14 node sessions to mid-stream
    provider cuts ("stream ended without [DONE]").  A tool only runs after the
    stream completes, so when a cut happens no side effect has occurred and
    the round can be reissued once.  The abandoned attempt's partial text is
    already in the trace (auditable) but must not leak into the final answer."""
    client = LLMClient(model='qwen-plus', api_key='test-key', interactive=False)
    observed = []
    calls = []

    def stream(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            yield {'choices': [{'delta': {'content': 'partial abandoned text '}}]}
            raise LLMClientError('stream ended without [DONE]; response is incomplete')
        yield {'choices': [{'delta': {'content': 'final answer'}}]}
        yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}

    monkeypatch.setattr(client, 'stream', stream)
    answer = client.stream_chat([{'role': 'user', 'content': 'go'}], event_callback=observed.append)

    assert answer == 'final answer'
    assert len(calls) == 2
    retried = [event for event in observed if event.get('event_type') == 'llm.stream.turn_retried']
    assert len(retried) == 1
    assert retried[0]['attempt'] == 2
    assert [event['text'] for event in observed if event.get('event_type') == 'llm.text.delta'] == [
        'partial abandoned text ',
        'final answer',
    ]


def test_mid_stream_cut_retry_budget_is_configurable(monkeypatch):
    """The reissue is bounded by ASTRALNEXUS_TURN_RETRIES (default 1)."""
    monkeypatch.setenv('ASTRALNEXUS_TURN_RETRIES', '0')
    client = LLMClient(model='qwen-plus', api_key='test-key', interactive=False)
    calls = []

    def stream(messages, **kwargs):
        calls.append(1)
        yield {'choices': [{'delta': {'content': 'x'}}]}
        raise LLMClientError('stream ended without [DONE]; response is incomplete')

    monkeypatch.setattr(client, 'stream', stream)
    with pytest.raises(LLMClientError, match='stream ended without'):
        client.stream_chat([{'role': 'user', 'content': 'go'}])
    assert len(calls) == 1


def test_truncated_output_is_never_reissued_identically(monkeypatch):
    """Fix L must not swallow honest truncation semantics, and Fix Z must not
    turn truncation into a blind retry.

    The invariant this guards is "no IDENTICAL reissue" — an identical reissue
    truncates identically — plus "still fails".  Fix Z (doc 18, r13b) sends one
    corrective instruction and lets the model answer shorter; the turn-retry
    path (Fix L), which DOES reissue the same request, must stay out of it.
    Live evidence for the nudge: PATCH-R003-RUNTIME died twice on truncation
    with no feedback, and each death cost a Governor generation plus churn.
    """
    client = LLMClient(model='qwen-plus', api_key='test-key', interactive=False)
    calls = []
    observed = []

    def stream(messages, **kwargs):
        calls.append([dict(m) for m in messages])
        yield {'choices': [{'delta': {'content': 'too long'}, 'finish_reason': 'length'}]}

    monkeypatch.setattr(client, 'stream', stream)
    with pytest.raises(LLMClientError, match='truncated'):
        client.stream_chat([{'role': 'user', 'content': 'go'}], event_callback=observed.append)

    # Exactly one corrective attempt, then an honest failure.
    assert len(calls) == 2
    # The retry is NOT the same request: it carries the shorter-output instruction.
    assert calls[1] != calls[0]
    assert '截断' in calls[1][-1]['content']
    assert [e.get('event_type') for e in observed].count('llm.output_truncation_nudge') == 1
    # Fix L's identical reissue must never be used for truncation.
    assert not [e for e in observed if e.get('event_type') == 'llm.stream.turn_retried']



@pytest.mark.parametrize("streaming", [False, True])
def test_readonly_worker_persists_real_command_receipts_without_owned_paths(tmp_path, monkeypatch, streaming):
    import subprocess
    import sys
    from AstralNexus.runtime.execution_store import ExecutionStore
    from AstralNexus.core.verifier import Verifier
    from AstralNexus.core.task_graph import TaskNode
    source = tmp_path / "deliverables/app/main.py"
    source.parent.mkdir(parents=True)
    source.write_text("print(5)\n", encoding="utf-8")
    criteria = ['file:<deliverables/app/main.py> exists',
                'command_output:{"command":"python main.py","stdout":"5\\n"}']
    contract = {"owned_paths": [], "upstream_artifacts": [{"path": "deliverables/app/main.py", "acceptance_status": "accepted"}],
                "acceptance_criteria": criteria}
    store = ExecutionStore(tmp_path)
    execution = store.begin("verifier", contract)
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    client.runtime_execution_id = execution
    attempts = []
    def stream(messages, **kwargs):
        attempts.append(messages)
        if len(attempts) == 1:
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "readonly-command", "function": {
                "name": "terminal_exec", "arguments": json.dumps({"command": ["python", "deliverables/app/main.py"]}),
            }}]}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
    def execute(executor, name, params):
        process = subprocess.run([sys.executable, str(source)], capture_output=True, text=True)
        return {"status": "success", "returncode": process.returncode,
                "stdout": process.stdout, "stderr": process.stderr, "cwd": str(tmp_path)}
    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool", execute)
    if not streaming:
        from contextlib import nullcontext
        def open_request(request, **kwargs):
            payload = json.loads(request.data)
            class Response:
                def read(self):
                    if not any(message["role"] == "tool" for message in payload["messages"]):
                        message = {"content": None, "tool_calls": [{"id": "readonly-command", "type": "function", "function": {
                            "name": "terminal_exec", "arguments": json.dumps({"command": ["python", "deliverables/app/main.py"]}),
                        }}]}
                    else:
                        message = {"content": "done"}
                    return json.dumps({"choices": [{"message": message}], "usage": {}}).encode()
            return nullcontext(Response())
        monkeypatch.setattr("AstralNexus.runtime.llm_client.llm_urlopen", open_request)
    run = client.stream_chat if streaming else client.chat
    tool_schema = [{"type": "function", "function": {"name": "terminal_exec", "parameters": {"type": "object", "properties": {}}}}]
    assert run([{"role": "user", "content": "verify"}], tools=tool_schema,
               workspace_root=tmp_path, owned_paths=[]) == "done"
    assert len(store.get(execution)["receipts"]) == 1
    node = TaskNode(node_id="V", goal="verify", role="verifier", acceptance_criteria=criteria,
                    metadata={"runtime_execution_id": execution, "owned_paths": []})
    from AstralNexus.core.task_graph import TaskGraph
    from AstralNexus.runtime.trace import TraceRecorder
    observations = Verifier(TaskGraph(), TraceRecorder(tmp_path), workspace_root=tmp_path).deterministic_observations(node)
    assert observations and all(item["holds"] is True for item in observations), json.dumps(observations, indent=2)

    # A real later source change still invalidates the readonly command proof.
    source.write_text("print(9)\n", encoding="utf-8")
    changed = Verifier(TaskGraph(), TraceRecorder(tmp_path), workspace_root=tmp_path).deterministic_observations(node)
    assert any(item["reason"] == "TEST_EXECUTION_STALE" for item in changed)
