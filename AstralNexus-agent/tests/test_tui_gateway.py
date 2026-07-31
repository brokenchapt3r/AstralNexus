from __future__ import annotations

from AstralNexus.tui_gateway import entry


def test_gateway_creates_astralnexus_session() -> None:
    gateway = entry.Gateway()

    result = gateway.dispatch("session.create", {})

    assert result["session_id"].startswith("astral-")
    assert result["info"]["profile_name"] == "天枢 Runtime"
    assert result["info"]["model"]
    assert result["info"]["usage"]["context_max"] > 0
    assert sum(map(len, result["info"]["tools"].values())) >= 20
    assert sum(map(len, result["info"]["skills"].values())) >= 1


def test_gateway_exposes_required_startup_methods() -> None:
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
    gateway = entry.Gateway()
    session_id = gateway.dispatch("session.create", {})["session_id"]
    task = tmp_path / "task file.txt"
    task.write_text("build", encoding="utf-8")
    started = []
    monkeypatch.setattr(
        gateway.controller,
        "start",
        lambda path: started.append(path) or True,
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
            "runtime.event",
            {
                "event_type": "runtime.progress.report",
                "title": "阶段性工作汇报",
                "summary": "[天枢定时进展] 运行 2，完成 3；接下来进入 Verifier。",
                "rationale": "来自 Runtime 的阶段性执行快照，不包含模型私有思维链。",
                "details": {"phase": None, "output_dir": None},
                "actor": "AstralNexus Runtime",
                "status": "running",
                "recorded_at": "",
                "collapsible": True,
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
