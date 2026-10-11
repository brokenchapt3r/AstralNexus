from __future__ import annotations

from AstralNexus.tui_gateway import entry
import threading
import pytest


@pytest.fixture(autouse=True)
def isolated_gateway_home(tmp_path, monkeypatch):
    from AstralNexus.core import identity
    monkeypatch.setattr(identity, 'bootstrap_environment', lambda: tmp_path)


def test_gateway_creates_astralnexus_session() -> None:
    gateway = entry.Gateway()

    result = gateway.dispatch("session.create", {})

    assert result["session_id"].startswith("astral-")
    assert result["info"]["profile_name"] == "天枢 Runtime"
    assert result["info"]["model"]
    assert result["info"]["usage"]["context_max"] == 0
    assert sum(map(len, result["info"]["tools"].values())) >= 20
    assert sum(map(len, result["info"]["skills"].values())) >= 1


def test_background_status_steer_and_interrupt_are_bound_to_owner(monkeypatch):
    gateway = entry.Gateway()
    first, second = gateway.create_session(), gateway.create_session()
    gateway._controller_session_id = first.session_id
    calls = []
    monkeypatch.setattr(gateway.controller, 'status', lambda: {'running': True, 'run_dir': None})
    monkeypatch.setattr(gateway.controller, 'interrupt', lambda message: calls.append(message))
    monkeypatch.setattr(gateway.controller, 'inject_requirement_text', lambda text: calls.append(text))
    assert gateway.dispatch('session.status', {'session_id': first.session_id})['running']
    assert not gateway.dispatch('session.status', {'session_id': second.session_id})['running']
    gateway.dispatch('session.interrupt', {'session_id': second.session_id})
    assert not calls
    gateway.dispatch('session.steer', {'session_id': first.session_id, 'text': '修复 API'})
    assert calls == ['修复 API']
    gateway.dispatch('session.interrupt', {'session_id': first.session_id})
    assert calls[-1] == 'native_tui_interrupt'


def test_public_event_has_same_identity_in_transport_and_storage(monkeypatch):
    gateway = entry.Gateway()
    session = gateway.create_session()
    session.run_dir = '/explicit/run'
    wire = []
    monkeypatch.setattr(entry, '_write', lambda message: wire.append(message))
    session.emit('tool.start', {'node_id': 'A', 'execution_id': 'E', 'call_id': 'T', 'name': 'file_read'})
    persisted = gateway.store.events(session.session_id)[-1]['payload']['ux']
    assert wire[-1]['params']['ux'] == persisted
    assert persisted['node_id'] == 'A' and persisted['execution_id'] == 'E' and persisted['tool_call_id'] == 'T'
    assert persisted['phase'] == 'unknown' and persisted['status'] == 'unknown'
    assert persisted['actions'][0]['method'] == 'inspection.get'


def test_restart_restores_title_history_tools_and_selected_session(tmp_path):
    gateway = entry.Gateway(home=tmp_path)
    first, second = gateway.create_session(), gateway.create_session()
    first.history = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "answer"}]
    gateway.store.append(first.session_id, 'user.message', {'text': 'task'})
    first.emit('tool.complete', {'name': 'file_read', 'result_text': 'source evidence'})
    first.emit('message.complete', {'text': 'answer'})
    gateway.dispatch('session.title', {'session_id': first.session_id, 'title': '真实标题'})
    gateway.dispatch('session.activate', {'session_id': first.session_id})
    restarted = entry.Gateway(home=tmp_path)
    assert restarted.current_session_id == first.session_id
    assert restarted.session(first.session_id).history == first.history
    assert restarted.session(first.session_id).title == '真实标题'
    assert restarted.session(second.session_id).title == '天枢会话'
    restored = restarted.dispatch('session.resume', {'session_id': first.session_id})
    assert restored['messages'] == [
        {'role': 'user', 'text': 'task'}, {'role': 'tool', 'name': 'file_read', 'text': 'source evidence'},
        {'role': 'assistant', 'text': 'answer'}]
    assert restarted.store.host.session(first.session_id)['workspace'] == first.workspace


def test_history_restore_never_replays_unfinished_turn_or_approval(tmp_path):
    gateway = entry.Gateway(home=tmp_path)
    session = gateway.create_session()
    session.running = True
    gateway.store.save(session)
    session.emit('approval.request', {'request_id': 'old', 'command': 'do something'})
    restarted = entry.Gateway(home=tmp_path)
    assert not restarted.session(session.session_id).running
    assert not restarted._approvals and not restarted.controller.status()['running']
    assert not restarted.respond_approval('once', request_id='old')
    assert all(message['role'] == 'system' for message in restarted.store.transcript(session.session_id))


def test_inspection_requires_explicit_run_binding_and_reads_real_checkpoint(tmp_path):
    from AstralNexus.core.run_kernel import RunKernel
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    assert '尚未绑定' in gateway.dispatch('inspection.get', {'kind': 'plan'})['text']
    run = tmp_path / 'run'
    with RunKernel(run) as kernel:
        kernel.apply_patch({'add_nodes': [{'node_id': 'writer', 'goal': '编写 API', 'role': 'coder',
            'owned_paths': ['deliverables/api.py'], 'outputs': ['deliverables/api.py'],
            'acceptance_criteria': ['API exists']}]}).result(timeout=5)
    session.run_dir = str(run)
    plan = gateway.dispatch('inspection.get', {'kind': 'plan'})['text']
    assert 'writer' in plan and '编写 API' in plan and 'paused' in plan
    delivery = gateway.dispatch('inspection.get', {'kind': 'delivery'})['text']
    assert '尚无正式发布' in delivery


def test_unknown_rpc_reports_unsupported_instead_of_success():
    gateway = entry.Gateway()
    with pytest.raises(entry.UnsupportedMethodError, match="unsupported.future"):
        gateway.dispatch("unsupported.future", {})


def test_display_and_main_sub_models_persist_without_mutating_process_env(tmp_path, monkeypatch):
    import os
    gateway = entry.Gateway(home=tmp_path)
    session = gateway.create_session()
    before = dict(os.environ)
    gateway.dispatch('config.set', {'key': 'compact', 'value': 'on'})
    gateway.dispatch('models.set', {'session_id': session.session_id, 'main_model': 'main-test', 'sub_model': 'sub-test'})
    restarted = entry.Gateway(home=tmp_path)
    assert restarted.dispatch('config.get', {'key': 'full'})['config']['display']['tui_compact'] is True
    selected = restarted.dispatch('models.get', {'session_id': session.session_id})
    assert selected['effective_main'] == 'main-test' and selected['effective_sub'] == 'sub-test'
    assert dict(os.environ) == before
    with pytest.raises(ValueError, match='disabled'):
        restarted.dispatch('models.set', {'session_id': session.session_id, 'main_model': 'qwen3.8-max'})


def test_stdio_unknown_rpc_uses_method_not_found_and_closes_bridge(monkeypatch):
    import io
    errors, closed = [], []
    monkeypatch.setattr(entry.sys, "stdin", io.StringIO('{"id":7,"method":"image.attach","params":{}}\n'))
    monkeypatch.setattr(entry, "_event", lambda *args, **kw: None)
    monkeypatch.setattr(entry, "_error", lambda ident, message, code=-32000: errors.append((ident, code, message)))
    original = entry.Gateway.disconnect
    def disconnect(self):
        original(self)
        closed.append(True)
    monkeypatch.setattr(entry.Gateway, "disconnect", disconnect)
    assert entry.main() == 0
    assert errors == [(7, -32601, "unsupported Gateway method: image.attach")]
    assert closed == [True]


def test_direct_chat_executor_captures_session_specific_approval_bridge(monkeypatch):
    from AstralNexus.cli import main
    from AstralNexus.tools.executor import ToolExecutor
    gateway = entry.Gateway()
    first, second = gateway.create_session(), gateway.create_session()
    captured, requests = [], []
    monkeypatch.setattr(entry, "_event", lambda *args, **kw: None)
    monkeypatch.setattr(main, "_govern_interactive_turn", lambda *args, **kw: False)
    monkeypatch.setattr(main, "_interactive_chat_turn", lambda *args, **kw: captured.append(ToolExecutor()))
    def provider(*args, session_id=None):
        requests.append(session_id)
        return "deny"
    monkeypatch.setattr(gateway, "approval_provider", provider)
    gateway._run_turn(first, "one")
    gateway._run_turn(second, "two")
    assert captured[0].approval_identity != captured[1].approval_identity
    for executor in captured:
        assert executor.approval_provider is not None
        executor.approval_provider("file_write", {}, "test", "test")
    assert requests == [first.session_id, second.session_id]


def test_title_updates_the_selected_session_and_invalid_id_never_falls_back():
    gateway = entry.Gateway()
    first, second = gateway.create_session(), gateway.create_session()
    gateway.dispatch("session.title", {"session_id": first.session_id, "title": "修复 API"})
    rows = gateway.dispatch("session.active_list", {})["sessions"]
    assert {row["id"]: row["title"] for row in rows} == {
        first.session_id: "修复 API", second.session_id: "天枢会话"}
    with pytest.raises(ValueError, match="session not found"):
        gateway.dispatch("session.title", {"session_id": "missing", "title": "wrong"})


def test_parallel_tool_batches_keep_node_and_call_identity(monkeypatch):
    emitted = []
    monkeypatch.setattr(entry, "_event", lambda kind, payload=None, **kw: emitted.append((kind, payload)))
    output = entry.GatewayOutput(entry.Session("test"))
    for node in ("A", "B"):
        output.event({"event_type": "llm.tool_execution.start", "run_id": "run", "node_id": node,
            "execution_id": "execution-" + node, "tool_round": 1,
            "calls": [{"name": "read-" + node, "call_id": node, "arguments": {"path": node}}]})
    for node in ("A", "B"):
        output.event({"event_type": "llm.tool_batch", "run_id": "run", "node_id": node,
            "execution_id": "execution-" + node, "tool_round": 1,
            "calls": [{"name": "read-" + node, "call_id": node, "status": "success", "stdout_tail": node}]})
    started = {p["call_id"]: p["tool_id"] for k, p in emitted if k == "tool.start"}
    completed = [p for k, p in emitted if k == "tool.complete"]
    assert [(p["node_id"], p["name"], p["result_text"]) for p in completed] == [("A", "read-A", "A"), ("B", "read-B", "B")]
    assert all(p["tool_id"] == started[p["call_id"]] and not p["correlation_unknown"] for p in completed)
    assert len([p for k, p in emitted if k == "subagent.tool"]) == 2


def test_tool_call_ids_handle_reordered_results(monkeypatch):
    emitted = []
    monkeypatch.setattr(entry, "_event", lambda kind, payload=None, **kw: emitted.append((kind, payload)))
    output = entry.GatewayOutput(entry.Session("test"))
    output.event({"event_type": "llm.tool_execution.start", "calls": [
        {"name": "first", "call_id": "1"}, {"name": "second", "call_id": "2"}]})
    output.event({"event_type": "llm.tool_batch", "calls": [
        {"name": "second", "call_id": "2", "status": "timeout"},
        {"name": "first", "call_id": "1", "status": "success"}]})
    started = {p["call_id"]: p["tool_id"] for k, p in emitted if k == "tool.start"}
    done = [p for k, p in emitted if k == "tool.complete"]
    assert [(p["name"], p["tool_id"]) for p in done] == [("second", started["2"]), ("first", started["1"])]
    assert done[0]["error"] == "timeout"


def test_parallel_approvals_only_release_the_identified_request(monkeypatch):
    gateway = entry.Gateway(approval_timeout=3)
    session = gateway.create_session()
    events, answers = [], {}
    changed = threading.Condition()
    def emit(kind, payload=None, **kw):
        with changed:
            events.append((kind, payload, kw))
            changed.notify_all()
    monkeypatch.setattr(entry, "_event", emit)
    def request(tool):
        answers[tool] = gateway.approval_provider(tool, {"path": tool}, "worker", "test", session_id=session.session_id)
    first = threading.Thread(target=request, args=("first",))
    first.start()
    with changed:
        assert changed.wait_for(lambda: len(events) == 1, timeout=2)
    second = threading.Thread(target=request, args=("second",))
    second.start()
    # The lock guarantees both requests are registered before exercising ambiguity.
    import time
    deadline = time.monotonic() + 2
    while len(gateway._approvals) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    request_id = events[0][1]["request_id"]
    assert not gateway.respond_approval("once")
    assert not gateway.respond_approval("once", request_id=request_id, session_id="another-session")
    assert gateway.respond_approval("once", request_id=request_id, session_id=session.session_id)
    first.join(2)
    assert answers == {"first": "once"} and second.is_alive()
    with changed:
        assert changed.wait_for(lambda: len(events) == 2, timeout=2)
    assert gateway.respond_approval("deny", request_id=events[1][1]["request_id"])
    second.join(2)
    assert answers == {"first": "once", "second": "deny"}
    assert not gateway.respond_approval("once", request_id=request_id)


@pytest.mark.parametrize("action", ["timeout", "disconnect", "close", "interrupt"])
def test_approval_wait_has_bounded_and_cancelled_exit(monkeypatch, action):
    gateway = entry.Gateway(approval_timeout=.1 if action == "timeout" else 3)
    session = gateway.create_session()
    emitted = threading.Event()
    monkeypatch.setattr(entry, "_event", lambda *args, **kw: emitted.set())
    answers = []
    thread = threading.Thread(target=lambda: answers.append(gateway.approval_provider(
        "write", {}, "worker", "test", session_id=session.session_id)))
    thread.start()
    assert emitted.wait(2)
    if action == "disconnect": gateway.disconnect()
    elif action in {"close", "interrupt"}: gateway.dispatch("session." + action, {"session_id": session.session_id})
    thread.join(2)
    assert not thread.is_alive() and answers == ["deny"]
    assert not gateway._approvals


def test_gateway_exposes_required_startup_methods(monkeypatch) -> None:
    monkeypatch.setattr(entry, '_provider_status', lambda: {'provider_configured': True, 'credentials_verified': False, 'message': ''})
    gateway = entry.Gateway()
    session = gateway.dispatch("session.create", {})
    session_id = session["session_id"]

    assert gateway.dispatch("setup.status", {})["provider_configured"] is True
    catalog = gateway.dispatch("commands.catalog", {})
    names = {item[0] for item in catalog["pairs"]}
    assert {"/run", "/resume", "/dashboard", "/inject-requirement"} <= names
    assert catalog["canon"]["/r"] == "/resume"
    display = gateway.dispatch("config.get", {"key": "full"})["config"]["display"]
    assert display["busy_input_mode"] == "interrupt"
    assert display["mouse_tracking"] == "off"
    assert gateway.dispatch("session.status", {"session_id": session_id})["status"] == "idle"


def test_gateway_slash_exec_uses_real_command_handler() -> None:
    gateway = entry.Gateway()
    session_id = gateway.dispatch("session.create", {})["session_id"]

    result = gateway.dispatch(
        "slash.exec",
        {"session_id": session_id, "command": "version"},
    )

    assert result["ok"] is True
    assert result["code"] == 0
    assert "AstralNexus v1.0.0" in result["output"]


def test_gateway_slash_run_uses_background_controller(monkeypatch, tmp_path) -> None:
    def _capture_callback(path):
        started.append(path)
        return True

    gateway = entry.Gateway()
    session_id = gateway.dispatch("session.create", {})["session_id"]
    task = tmp_path / "task file.txt"
    task.write_text("build", encoding="utf-8")
    started: list[str] = []
    monkeypatch.setattr(
        gateway.controller,
        "start",
        _capture_callback,
    )

    result = gateway.dispatch(
        "slash.exec",
        {"session_id": session_id, "command": f'run "{task}"'},
    )

    assert result["ok"] is True
    assert started == [str(task)]


def test_gateway_session_close_removes_ui_session() -> None:
    gateway = entry.Gateway()
    session_id = gateway.dispatch("session.create", {})["session_id"]

    result = gateway.dispatch("session.close", {"session_id": session_id})

    assert result == {"closed": True, "ok": True}
    assert session_id not in gateway.sessions


def test_gateway_output_translates_stream_and_usage(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append((event_type, payload, kwargs)),
    )
    session = entry.Session("astral-test")
    output = entry.GatewayOutput(session)

    output.event({"event_type": "llm.text.delta", "text": "hello"})
    output.event(
        {
            "event_type": "llm.usage",
            "prompt_tokens": 120,
            "completion_tokens": 30,
        }
    )

    assert emitted[0][0] == "message.delta"
    assert emitted[0][1]["text"] == "hello"
    assert session.usage["input"] == 120
    assert session.usage["output"] == 30
    assert any(item[0] == "session.info" for item in emitted)


def test_context_limit_stays_unknown_until_runtime_supplies_it(monkeypatch):
    monkeypatch.setattr(entry, "_event", lambda *args, **kw: None)
    session = entry.Session("test")
    output = entry.GatewayOutput(session)
    output.event({"event_type": "llm.context", "estimated_input_tokens": 100})
    assert session.usage["context_max"] == 0 and session.usage["context_percent"] == 0
    output.event({"event_type": "llm.context", "estimated_input_tokens": 100, "context_total": 400})
    assert session.usage["context_max"] == 400 and session.usage["context_percent"] == 25


def test_gateway_output_keeps_governed_node_deltas_out_of_chat(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append((event_type, payload, kwargs)),
    )
    output = entry.GatewayOutput(entry.Session("astral-test"))

    output.event({
        "event_type": "llm.text.delta",
        "node_id": "P-001",
        "role": "planner",
        "text": '{"add_nodes":',
    })

    assert emitted == []


def test_gateway_output_translates_tool_lifecycle(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append((event_type, payload, kwargs)),
    )
    output = entry.GatewayOutput(entry.Session("astral-test"))

    output.event(
        {
            "event_type": "llm.tool_execution.start",
            "calls": [{"name": "file_read", "arguments": {"path": "README.md"}}],
        }
    )
    output.event(
        {
            "event_type": "llm.tool_batch",
            "calls": [{"name": "file_read", "status": "success", "result": "content"}],
        }
    )

    assert [item[0] for item in emitted] == [
        "tool.start", "tool.complete", "status.update",
    ]
    assert emitted[0][1]["name"] == "file_read"
    assert "README.md" in emitted[0][1]["args_text"]
    assert emitted[1][1]["result_text"] == "content"
    assert "成功 1" in emitted[2][1]["text"]


def test_gateway_output_reports_governed_worker_stages(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append(
            (event_type, payload, kwargs)
        ),
    )
    output = entry.GatewayOutput(entry.Session("astral-test"))

    output.event({
        "event_type": "governor.graph_patch.proposed",
        "add_node_count": 6,
        "reasoning": "split by evidence boundary",
    })
    output.event({
        "event_type": "router.ready_wave",
        "wave_id": 1,
        "node_ids": ["N-01", "N-02"],
    })
    output.event({
        "event_type": "node.completed",
        "node_id": "N-01",
        "role": "coder",
        "status": "completed",
        "summary": "created backend",
    })

    statuses = [
        payload["text"]
        for event_type, payload, _ in emitted
        if event_type == "status.update"
    ]
    assert not any("提议 6 个节点" in text for text in statuses)
    assert any("并行派发 2 个节点" in text for text in statuses)
    assert any("created backend" in text for text in statuses)


def test_gateway_output_renders_periodic_progress_report(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append(
            (event_type, payload, kwargs)
        ),
    )
    output = entry.GatewayOutput(entry.Session("astral-test"))

    output.event({
        "event_type": "runtime.progress.report",
        "text": "[天枢定时进展] 运行 2，完成 3；接下来进入 Verifier。",
    })

    assert emitted == [
        (
            "run.progress",
            {
                "text": "[天枢定时进展] 运行 2，完成 3；接下来进入 Verifier。",
                "phase": "unknown", "run_dir": "",
            },
            {"session_id": "astral-test"},
        )
    ]


def test_gateway_output_forwards_public_runtime_event(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append(
            (event_type, payload, kwargs)
        ),
    )
    output = entry.GatewayOutput(entry.Session("astral-test"))
    output.event({
        "event_type": "planning.summary",
        "title": "规划摘要",
        "summary": "生成 4 个异构节点",
        "rationale": "按能力边界拆分",
        "details": {"node_count": 4},
    })
    assert emitted[0][0] == "runtime.event"
    assert emitted[0][1]["title"] == "规划摘要"
    assert emitted[0][1]["rationale"] == "按能力边界拆分"


def test_gateway_decodes_streamed_tool_arguments_for_display(monkeypatch) -> None:
    emitted = []
    monkeypatch.setattr(
        entry,
        "_event",
        lambda event_type, payload=None, **kwargs: emitted.append((event_type, payload, kwargs)),
    )
    output = entry.GatewayOutput(entry.Session("astral-test"))

    output.event(
        {
            "event_type": "llm.tool_execution.start",
            "calls": [{"name": "web_search", "arguments": '{"query":"中标公告"}'}],
        }
    )

    assert emitted[0][0] == "tool.start"
    assert emitted[0][1]["args_text"] == '{"query": "中标公告"}'


def test_review_warning_reaches_visible_and_persisted_tool_event(tmp_path, monkeypatch):
    emitted = []
    monkeypatch.setattr(entry, '_event', lambda kind, payload=None, **kw: emitted.append((kind, payload)))
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    output = entry.GatewayOutput(session)
    output.event({'event_type': 'llm.tool_batch', 'calls': [{
        'name': 'terminal_exec', 'status': 'success', 'stdout_tail': 'started',
        'review_coverage': 'partial_background', 'review_warning': 'later writes are not captured', 'change_ids': ['change-1']}]})
    done = next(payload for kind, payload in emitted if kind == 'tool.complete')
    assert 'later writes are not captured' in done['result_text']
    assert done['review_coverage'] == 'partial_background' and done['change_ids'] == ['change-1']
    restored = entry.Gateway(home=tmp_path / 'home')
    assert any('later writes are not captured' in str(event) for event in restored.store.transcript(session.session_id))


def test_agent_history_restarts_passively_without_claiming_unfinished_success(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, '_event', lambda *args, **kwargs: None)
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    session.emit('subagent.start', {'id': 'N-1', 'goal': 'unfinished'})
    session.emit('subagent.start', {'id': 'N-2', 'goal': 'tested'})
    session.emit('subagent.complete', {'id': 'N-2', 'status': 'failed', 'summary': 'test failed'})
    restored = entry.Gateway(home=tmp_path / 'home')
    response = restored.dispatch('session.resume', {'session_id': session.session_id})
    nodes = {node['id']: node for node in response['spawn_trees'][0]['subagents']}
    assert nodes['N-1']['status'] == 'unknown'
    assert nodes['N-2']['status'] == 'failed'
    assert not response['running'] and not restored.controller.status()['running']


def test_unimplemented_features_are_explicitly_advertised_as_unavailable(tmp_path):
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    flags = session.info()['capabilities']
    assert flags == {'images': False, 'clipboard_images': False, 'voice': False, 'pets': False}
    assert gateway.dispatch('config.get', {'key': 'full'})['config']['capabilities'] == flags
    for method in ('image.attach', 'voice.record', 'pet.cells', 'clipboard.paste'):
        with pytest.raises(entry.UnsupportedMethodError):
            gateway.dispatch(method, {'session_id': session.session_id})


def test_command_home_is_context_local_and_protocol_bypasses_capture(tmp_path, monkeypatch):
    import io
    import os
    import sys
    from AstralNexus.runtime.run_context import get_env
    gateway = entry.Gateway(home=tmp_path / 'home')
    monkeypatch.setenv('ASTRALNEXUS_HOME', 'unrelated-home')
    transport = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', transport)
    def command(value, *, home):
        assert get_env('ASTRALNEXUS_HOME') == str(home)
        assert os.environ['ASTRALNEXUS_HOME'] == 'unrelated-home'
        print('command output')
        entry._result('from-command', {'ok': True})
        return 0
    monkeypatch.setattr('AstralNexus.cli.commands.process_command', command)
    code, text = gateway._execute_command_handler('/status', detached=False)
    assert code == 0 and text == 'command output'
    assert 'from-command' in transport.getvalue()
    assert os.environ['ASTRALNEXUS_HOME'] == 'unrelated-home'


def test_usage_snapshots_count_once_across_callbacks_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, '_event', lambda *args, **kwargs: None)
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    def usage(output, prompt, completion):
        output.event({'event_type': 'llm.usage', 'request_id': 'request-1', 'node_id': 'N', 'model': 'test-model',
                      'prompt_tokens': prompt, 'completion_tokens': completion})
    usage(entry.GatewayOutput(session), 100, 20)
    usage(entry.GatewayOutput(session), 100, 20)
    restored = entry.Gateway(home=tmp_path / 'home').sessions[session.session_id]
    usage(entry.GatewayOutput(restored), 110, 30)
    usage(entry.GatewayOutput(restored), 90, 10)
    assert restored.usage['input'] == 110 and restored.usage['output'] == 30
    assert restored.usage['by_model']['test-model'] == {'input': 110, 'output': 30}
    assert restored.usage['by_node']['N'] == {'input': 110, 'output': 30}


def test_setup_status_is_real_and_missing_credentials_do_not_block_history(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('AstralNexus.runtime.run_context.RunSettings.capture', lambda: SimpleNamespace(environment={}))
    monkeypatch.setattr(entry, '_event', lambda *args, **kwargs: None)
    gateway = entry.Gateway(home=tmp_path / 'home')
    status = gateway.dispatch('setup.status', {})
    assert status['provider_configured'] is False and status['credentials_verified'] is False
    session = gateway.create_session()
    response = gateway.dispatch('session.resume', {'session_id': session.session_id})
    assert response['info']['credential_warning'] and not response['running']
    monkeypatch.setattr('AstralNexus.runtime.run_context.RunSettings.capture', lambda: SimpleNamespace(environment={'ASTRALNEXUS_API_KEY': 'fake-for-local-test'}))
    status = gateway.dispatch('setup.status', {})
    assert status['provider_configured'] is True and status['credentials_verified'] is False
    assert 'fake-for-local-test' not in str(status)


def test_approval_shows_actual_tool_root_and_execution_not_gateway_cwd(tmp_path, monkeypatch):
    from AstralNexus.tools.context import tool_workspace, tool_owned_paths
    from AstralNexus.runtime.execution_fence import execution_fence_scope
    events = []
    monkeypatch.setattr(entry, '_event', lambda kind, payload=None, **kw: events.append((kind, payload)))
    gateway = entry.Gateway(home=tmp_path / 'home', approval_timeout=0.001)
    session = gateway.create_session()
    root = tmp_path / 'worker-run'
    root.mkdir()
    with tool_workspace(root), tool_owned_paths(['app.py']), execution_fence_scope(root, None, 'exec-actual'):
        assert gateway.approval_provider('file_write', {'path': 'app.py'}, 'worker', 'write', session_id=session.session_id) == 'deny'
    request = next(payload for kind, payload in events if kind == 'approval.request')
    assert request['workspace'] == str(root.resolve())
    assert request['execution_id'] == 'exec-actual' and request['owned_paths'] == ['app.py']
    assert str(root.resolve()) in request['description']


def test_checkpoint_creation_failure_ends_turn_as_failure_not_stuck_running(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(entry, '_event', lambda kind, payload=None, **kw: events.append((kind, payload)))
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    session.running = True
    def failed(*args, **kwargs):
        raise OSError('checkpoint storage unavailable')
    monkeypatch.setattr('AstralNexus.runtime.user_checkpoint.UserCheckpoints.create', failed)
    gateway._run_turn(session, 'task')
    assert not session.running
    assert any(kind == 'error' and 'checkpoint storage unavailable' in payload['message'] for kind, payload in events)
    assert gateway.store.load(session.session_id)['status'] == 'idle'


def test_early_sqlite_failure_is_visible_and_cannot_leave_busy_or_run_queue(tmp_path, monkeypatch):
    import sqlite3
    events = []
    monkeypatch.setattr(entry, '_event', lambda kind, payload=None, **kw: events.append((kind, payload)))
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    session.running = True
    session.queued.put('queued task')
    def failed(*args, **kwargs): raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(gateway.store, 'append', failed)
    monkeypatch.setattr(gateway.store, 'save', failed)
    monkeypatch.setattr(gateway, 'submit', lambda *args: (_ for _ in ()).throw(AssertionError('queued model task must not run')))
    gateway._run_turn(session, 'task')
    assert not session.running and session.queued.get_nowait() == 'queued task'
    assert any(kind == 'error' and payload.get('persisted') is False for kind, payload in events)
    assert any(kind == 'message.complete' and payload['status'] == 'failed' for kind, payload in events)
