import json
from pathlib import Path

from AstralNexus.dashboard.live_server import LiveDashboardServer, LiveRunState, resolve_run_dir


def test_historical_edge_count_does_not_crash_replay(tmp_path):
    state = LiveRunState(tmp_path)
    state.apply({"event_type": "topology.updated", "payload": {"semantic_edges": 12}})
    assert LiveRunState._parse_edges(12) == set()
    assert LiveRunState._parse_edges([{"source": "A", "target": "B"}]) == {("A", "B", "dependency")}


def test_dashboard_control_panel_uses_public_narrative_events():
    html_path = Path(__file__).parents[1] / "dashboard" / "live_dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    assert "const events=Array.isArray(state.runtime_events)" in html
    assert "const narrative=e.rationale||e.summary||''" in html
    assert "阶段性汇报 / 公开执行依据" in html
    assert "const events=Array.isArray(state.events)" not in html


def test_dashboard_graph_supports_pointer_centered_wheel_zoom_and_pan():
    html = (
        Path(__file__).parents[1] / "dashboard" / "live_dashboard.html"
    ).read_text(encoding="utf-8")
    assert "addEventListener('wheel'" in html
    assert "event.preventDefault()" in html
    assert "graphViewport.scale" in html
    assert "addEventListener('pointermove'" in html
    assert 'id="zoom-reset"' in html


def test_dashboard_ignores_noncanonical_historical_status(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event("node.started", "N-legacy", status="running"))
    state.apply(_event("llm.response", "N-legacy", status="failure"))

    node = next(
        item for item in state.snapshot()["nodes"]
        if item["id"] == "N-legacy"
    )
    assert node["status"] == "running"
    assert node["reported_noncanonical_status"] == "failure"


def test_dashboard_projects_agent_only_lifecycle_statuses(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply_lifecycle({
        "event": "created",
        "timestamp": "2026-01-01T00:00:00Z",
        "agent_id": "agent-1",
        "node_id": "N-1",
        "role": "researcher",
        "state": "initializing",
    })
    state.apply_lifecycle({
        "event": "transitioned: running -> paused",
        "timestamp": "2026-01-01T00:00:01Z",
        "agent_id": "agent-1",
        "node_id": "N-1",
        "role": "researcher",
        "state": "paused",
    })

    agent = state.snapshot()["agent_population"]["agents"][0]
    assert agent["status"] == "blocked"
    assert agent["metadata"]["reported_lifecycle_status"] == "paused"


def test_dashboard_deduplicates_public_runtime_event_mirrors(tmp_path: Path):
    state = LiveRunState(tmp_path)
    event = _event(
        "planning.summary",
        "P-001",
        runtime_event_id="rte-same",
        summary="生成三个异构节点",
        rationale="检索、实现和验证需要不同能力",
    )
    state.apply(event)
    state.apply(event)

    snapshot = state.snapshot()
    assert len(snapshot["runtime_events"]) == 1
    planner = next(item for item in snapshot["nodes"] if item["id"] == "P-001")
    assert len(planner["stage_reports"]) == 1


def _event(event_type, node_id=None, **payload):
    body = dict(payload)
    if node_id:
        body["node_id"] = node_id
    return {"event_type": event_type, "recorded_at": "2026-01-01T00:00:00Z", "payload": body}


def test_live_run_state_materializes_nodes_edges_and_text(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event("node.started", "N-001", role="researcher", goal="collect evidence", dependencies=[]))
    state.apply(_event("task_graph.edge.added", source="N-001", target="N-002"))
    state.apply(_event("node.started", "N-002", role="reviewer", goal="check evidence", dependencies=["N-001"]))
    state.apply(_event("llm.text.delta", "N-002", text="first "))
    state.apply(_event("node.output.delta", "N-002", delta="answer"))
    state.apply(_event("node.completed", "N-002", status="completed"))

    snapshot = state.snapshot()
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    assert nodes["N-002"]["status"] == "completed"
    assert nodes["N-002"]["output"] == "first answer"
    assert {("N-001", "N-002", "dependency")} <= {tuple(edge.values()) for edge in snapshot["edges"]}


def test_live_run_state_projects_public_runtime_brief(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event("node.started", "N-01", role="researcher", goal="collect evidence"))
    state.apply(_event(
        "agent.activity",
        "N-01",
        role="researcher",
        title="正在检索公开证据",
        summary="查询权威数据源",
        rationale="满足节点证据契约",
        status="running",
    ))
    state.apply(_event(
        "recovery.triggered",
        "N-01",
        title="触发故障恢复",
        summary="provider timeout",
    ))

    snapshot = state.snapshot()
    assert snapshot["runtime_events"][-1]["event_type"] == "recovery.triggered"
    assert snapshot["runtime_brief"]["total_nodes"] == 1
    assert snapshot["runtime_brief"]["current_work"][0]["id"] == "N-01"
    assert snapshot["runtime_brief"]["output_dir"] == str(tmp_path)


def test_live_run_state_projects_node_reports_and_closes_terminal_activity(
    tmp_path: Path,
):
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "node.started", "N-01", role="researcher", goal="collect evidence",
    ))
    state.apply(_event(
        "agent.activity", "N-01", role="researcher",
        title="阶段证据收集完成", summary="已获得两个来源",
        rationale="采用双来源交叉验证以满足证据契约", status="completed",
    ))
    state.apply(_event(
        "task.completed", title="任务执行完成", summary="交付门已通过",
        status="completed",
    ))

    snapshot = state.snapshot()
    node = next(item for item in snapshot["nodes"] if item["id"] == "N-01")
    assert node["public_reasoning"] == "采用双来源交叉验证以满足证据契约"
    assert node["stage_reports"][-1]["title"] == "阶段证据收集完成"
    assert snapshot["run_status"] == "completed"
    assert snapshot["active_agents"] == []
    assert snapshot["current_wave"] == []
    assert snapshot["runtime_brief"]["phase"] == "验收完成"
    assert snapshot["runtime_brief"]["current_work"] == []


def test_live_run_state_rebuilds_complete_graph_from_snapshot(tmp_path: Path):
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "task_graph.snapshot",
        nodes=[
            {
                "node_id": "T-001", "status": "completed",
                "role": "planner", "goal": "plan", "dependencies": [],
            },
            {
                "node_id": "N-01", "status": "ready",
                "role": "coder", "goal": "build", "dependencies": ["T-001"],
            },
            {
                "node_id": "N-02", "status": "admitted",
                "role": "verifier", "goal": "verify", "dependencies": ["N-01"],
            },
        ],
        edges=[
            {"source": "T-001", "target": "N-01", "kind": "depends_on"},
            {"source": "N-01", "target": "N-02", "kind": "depends_on"},
        ],
    ))

    snapshot = state.snapshot()
    assert {item["id"] for item in snapshot["nodes"]} == {
        "T-001", "N-01", "N-02",
    }
    assert len(snapshot["edges"]) == 2


def test_live_run_state_places_original_request_above_planner(tmp_path: Path):
    task_file = tmp_path / "task.txt"
    task_file.write_text(
        "寻找已中标标书\n必须抓取真实公告并保存可验证交付物。"
        "\n\n相关会话上下文（仅用于保持既有约束）：\nassistant: legacy",
        encoding="utf-8",
    )
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "task.source.loaded",
        path=str(task_file),
        goal="错误的首行摘要",
    ))
    state.apply(_event(
        "task_graph.snapshot",
        nodes=[{
            "node_id": "T-001",
            "status": "completed",
            "role": "planner",
            "goal": "错误的首行摘要",
            "dependencies": [],
        }],
        edges=[],
    ))
    state.apply(_event(
        "governor.graph_patch.proposed",
        add_node_count=2,
        reasoning="拆分检索与汇总任务，检索节点并行执行。",
        reason_codes=["GOAL_DECOMPOSITION", "PARALLELIZABLE"],
    ))
    state.apply(_event(
        "governor.graph_patch.applied",
        added=["R-01", "S-01"],
        reasoning="拆分检索与汇总任务，检索节点并行执行。",
        reason_codes=["GOAL_DECOMPOSITION", "PARALLELIZABLE"],
    ))

    snapshot = state.snapshot()
    nodes = {item["id"]: item for item in snapshot["task_graph"]["nodes"]}
    assert nodes["U-001"]["goal"] == (
        "寻找已中标标书\n必须抓取真实公告并保存可验证交付物。"
    )
    assert nodes["U-001"]["is_input"] is True
    assert nodes["U-001"]["role"] == "user_intent"
    assert nodes["T-001"]["goal"] == nodes["U-001"]["goal"]
    assert nodes["T-001"]["assigned_by"] == "用户 / GlobalGovernor"
    assert nodes["T-001"]["input_sources"][0]["node_id"] == "U-001"
    assert nodes["T-001"]["planning_view"]["planning_explanation"] == (
        "拆分检索与汇总任务，检索节点并行执行。"
    )
    assert {
        (edge["source"], edge["target"], edge["kind"])
        for edge in snapshot["task_graph"]["edges"]
    } >= {("U-001", "T-001", "task_admission")}


def test_live_run_state_uses_real_control_nodes_and_keeps_replan_history(
    tmp_path: Path,
):
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "orchestrator.initialized",
        root_node_id="P-001",
        intent_node_id="U-001",
        goal="开发并验证一个可运行系统",
    ))
    state.apply(_event(
        "task_graph.snapshot",
        nodes=[
            {
                "node_id": "U-001", "role": "user_intent",
                "goal": "开发并验证一个可运行系统", "status": "completed",
                "dependencies": [], "metadata": {"plane": "control"},
            },
            {
                "node_id": "P-001", "role": "planner",
                "goal": "开发并验证一个可运行系统", "status": "completed",
                "dependencies": ["U-001"], "metadata": {"plane": "control"},
            },
            {
                "node_id": "C-01", "role": "coder", "goal": "实现系统",
                "status": "running", "dependencies": ["P-001"],
                "metadata": {
                    "source_requirements": ["必须创建可运行系统"],
                    "derivation": "将可运行交付要求分配给编码能力节点。",
                },
            },
        ],
        edges=[
            {"source": "U-001", "target": "P-001", "kind": "depends_on"},
            {"source": "P-001", "target": "C-01", "kind": "depends_on"},
        ],
    ))
    state.apply(_event(
        "governor.graph_patch.proposed", add_node_count=1,
        planning_explanation="按实现与验证职责拆分。",
        reason_codes=["CAPABILITY_SEPARATION"],
    ))
    state.apply(_event(
        "governor.graph_patch.applied", added=["C-01"],
        planning_explanation="按实现与验证职责拆分。",
    ))
    state.apply(_event(
        "governor.graph_patch.proposed", add_node_count=1,
        planning_explanation="验证发现缺口，增加恢复验证节点。",
        reason_codes=["VERIFICATION_GAP"],
    ))
    state.apply(_event(
        "governor.graph_patch.applied", added=["V-02"],
        planning_explanation="验证发现缺口，增加恢复验证节点。",
    ))
    state.apply(_event(
        "agent.spawned", "C-01",
        agent_id="agent-coder-C-01-0001",
        role="coder",
        status="running",
        model="qwen3-coder-plus",
        resource_tier="cloud",
    ))

    snapshot = state.snapshot()
    nodes = snapshot["task_graph"]["nodes"]
    assert [node["id"] for node in nodes].count("U-001") == 1
    planner = next(node for node in nodes if node["id"] == "P-001")
    assert planner["plane"] == "control"
    assert planner["input_sources"][0]["node_id"] == "U-001"
    history = planner["planning_view"]["planning_history"]
    assert [item["kind"] for item in history] == ["initial_plan", "replan"]
    assert history[-1]["added"] == ["V-02"]
    authorization = snapshot["task_authorization_graph"]
    assert {
        (edge["source"], edge["target"], edge["kind"])
        for edge in authorization["edges"]
    } >= {
        ("U-001", "P-001", "planning_requested"),
        ("P-001", "O-001", "patch_proposed"),
        ("O-001", "G-001", "patch_submitted"),
        ("G-001", "TC-C-01", "contract_approved"),
        ("TC-C-01", "C-01", "contract_issued"),
        ("C-01", "agent::agent-coder-C-01-0001", "executed_by"),
    }
    authorization_ids = {node["id"] for node in authorization["nodes"]}
    assert "G-001" in authorization_ids
    assert "O-001" in authorization_ids
    assert "TC-C-01" in authorization_ids
    assert "agent::agent-coder-C-01-0001" in authorization_ids
    assert {node["id"] for node in snapshot["data_flow_graph"]["nodes"]} == {
        "U-001", "P-001", "TC-C-01", "C-01",
    }
    data_edges = {
        (edge["source"], edge["target"], edge["kind"])
        for edge in snapshot["data_flow_graph"]["edges"]
    }
    assert ("U-001", "P-001", "requirement_intake") in data_edges
    assert ("P-001", "TC-C-01", "requirement_derivation") in data_edges
    assert ("TC-C-01", "C-01", "task_contract") in data_edges
    contract = next(
        node for node in snapshot["data_flow_graph"]["nodes"]
        if node["id"] == "TC-C-01"
    )
    assert contract["entity_type"] == "task_contract"
    assert contract["source_requirements"] == ["必须创建可运行系统"]
    coder = next(node for node in nodes if node["id"] == "C-01")
    assert coder["governance_chain"] == {
        "requested_by": "control-planner-P-001",
        "submitted_by": "Orchestrator.plan_task_decomposition",
        "approved_by": "GlobalGovernor",
        "applied_by": "GlobalGovernor.apply_task_graph_patch",
        "scheduled_by": "Orchestrator/Router",
        "executed_by": "agent-coder-C-01-0001",
    }
    assert coder["requirement_lineage"]["source_requirements"] == [
        "必须创建可运行系统"
    ]
    assert "编码能力节点" in coder["requirement_lineage"]["derivation"]


def test_live_run_state_projects_tool_activity_and_upstream_sender(
    tmp_path: Path,
):
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "task_graph.snapshot",
        nodes=[
            {
                "node_id": "T-001", "role": "planner",
                "goal": "plan", "status": "completed",
            },
            {
                "node_id": "REC-02", "role": "coder",
                "goal": "probe endpoint", "status": "running",
                "dependencies": ["T-001"],
            },
        ],
    ))
    state.apply(_event(
        "llm.tool_execution.start",
        "REC-02",
        request_index=2,
        tool_round=0,
        calls=[{
            "name": "python_exec", "call_id": "call-1",
            "arguments": {"code": "print('probe')"},
        }],
        reason="执行真实网络探测",
    ))
    state.apply(_event(
        "llm.tool_batch",
        "REC-02",
        request_index=2,
        tool_round=0,
        calls=[{
            "name": "python_exec", "call_id": "call-1",
            "status": "success", "path": "probe.txt",
        }],
        reason="收集执行证据",
    ))

    nodes = {item["id"]: item for item in state.snapshot()["nodes"]}
    recovery = nodes["REC-02"]
    assert recovery["assigned_by"] == "GlobalGovernor"
    assert recovery["input_sources"][0]["node_id"] == "T-001"
    assert recovery["input_sources"][0]["relation"] == "governed_admission"
    assert recovery["tool_activity"] == [{
        "key": "call-1",
        "name": "python_exec",
        "status": "success",
        "arguments": {"code": "print('probe')"},
        "reason": "收集执行证据",
        "tool_round": 0,
        "request_index": 2,
        "path": "probe.txt",
        "error": "",
        "stdout": "",
    }]


def test_live_run_state_projects_agent_instance_activity_and_timeline(
    tmp_path: Path,
):
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "node.started", "N-01", role="coder", goal="build application"
    ))
    state.apply(_event(
        "agent.spawned", "N-01", agent_id="agent-coder-01",
        role="coder", status="running",
    ))
    state.apply(_event(
        "agent.activity.changed", "N-01", agent_id="agent-coder-01",
        role="coder", status="running",
        activity={
            "type": "tool_call", "label": "正在调用工具 pytest_run",
            "tool": "pytest_run", "source_event": "llm.tool_execution.start",
        },
    ))

    snapshot = state.snapshot()
    node = next(item for item in snapshot["nodes"] if item["id"] == "N-01")
    assert node["agent_instance"] == "agent-coder-01"
    assert node["current_activity"]["tool"] == "pytest_run"
    assert snapshot["execution_timeline"][-1]["label"] == (
        "正在调用工具 pytest_run"
    )


def test_live_server_api_returns_snapshot_and_node_output(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(_event("node.started", "N-001", role="coder", goal="write code"), ensure_ascii=False)
        + "\n"
        + json.dumps(_event("llm.text.delta", "N-001", text="created file"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    server = LiveDashboardServer(tmp_path)
    server._load_trace()
    # Use Werkzeug's WSGI client directly. This keeps the test valid with
    # older Flask installations whose test client is incompatible with a
    # newer Werkzeug package; production still uses the same WSGI app.
    from werkzeug.test import Client
    from werkzeug.wrappers import Response

    client = Client(server._build_app(), Response)
    snapshot = client.get("/api/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.get_json()["nodes"][0]["output"] == "created file"
    node = client.get("/api/node/N-001")
    assert node.status_code == 200
    assert node.get_json()["id"] == "N-001"


def test_swarm_projection_separates_agents_routes_resources_and_metrics(
    tmp_path: Path,
):
    state = LiveRunState(tmp_path)
    state.apply(_event(
        "task_graph.snapshot",
        nodes=[
            {
                "node_id": "N-01", "role": "researcher",
                "goal": "collect evidence", "status": "ready",
            },
            {
                "node_id": "N-02", "role": "verifier",
                "goal": "verify evidence", "status": "admitted",
                "dependencies": ["N-01"],
            },
        ],
    ))
    state.apply(_event(
        "router.ready_wave",
        node_ids=["N-01", "N-02"],
        roles=["researcher", "verifier"],
        wave_id=3,
        wave_width=2,
    ))
    state.apply(_event(
        "governor.worker_selected",
        node_id="N-01",
        task_id="N-01",
        selected={
            "worker_type": "Researcher",
            "capabilities": ["web_research"],
            "tools": ["web_search"],
            "resource_tier": "cloud",
            "selected_model": "qwen-flash",
        },
    ))
    state.apply(_event(
        "agent.spawned",
        node_id="N-01",
        agent_id="agent-research-01",
        role="researcher",
        status="running",
        resource_tier="cloud",
        model="qwen-flash",
    ))
    state.apply(_event(
        "agent.communication.link_created",
        source="agent-research-01",
        target="assignment:N-02",
        source_task="N-01",
        target_task="N-02",
        kind="evidence_route",
        reason="share evidence",
    ))
    state.apply(_event(
        "llm.response",
        prompt_tokens=1200,
        completion_tokens=300,
    ))
    state.apply(_event(
        "context.compressed",
        tokens_before=1000,
        tokens_after=600,
    ))

    snapshot = state.snapshot()
    assert len(snapshot["task_graph"]["nodes"]) == 2
    assert {
        item["id"] for item in snapshot["agent_population"]["agents"]
    } == {"agent-research-01"}
    assert snapshot["communication_topology"]["edges"][0]["kind"] == (
        "evidence_route"
    )
    assert snapshot["resource_state"]["by_tier"]["cloud"] == 1
    assert snapshot["wave"]["width"] == 2
    assert snapshot["metrics"]["peak_parallelism"] == 2
    assert snapshot["metrics"]["token_cost"] == 1500
    assert snapshot["metrics"]["token_saving_ratio"] == 0.4


def test_live_server_loads_agent_lifecycle_side_log(tmp_path: Path):
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "agent_lifecycle.jsonl").write_text(
        json.dumps({
            "event": "created",
            "agent_id": "agent-coder-N-01",
            "role": "coder",
            "node_id": "N-01",
            "state": "admitted",
            "timestamp": "2026-01-01T00:00:00Z",
        }, ensure_ascii=False)
        + "\n"
        + json.dumps({
            "event": "transitioned from admitted to ready",
            "agent_id": "agent-coder-N-01",
            "role": "coder",
            "node_id": "N-01",
            "state": "ready",
            "timestamp": "2026-01-01T00:00:01Z",
        }, ensure_ascii=False)
        + "\n"
        + json.dumps({
            "event": "transitioned from ready to running",
            "agent_id": "agent-coder-N-01",
            "role": "coder",
            "node_id": "N-01",
            "state": "running",
            "timestamp": "2026-01-01T00:00:02Z",
        }, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    server = LiveDashboardServer(tmp_path)
    assert server._load_trace() == 3
    agents = server.state.snapshot()["agent_population"]["agents"]
    assert agents[0]["id"] == "agent-coder-N-01"
    assert agents[0]["status"] == "running"


def test_latest_prefers_current_run_pointer(tmp_path: Path):
    old_run = tmp_path / "runs" / "old-run"
    current_run = tmp_path / "project-output" / "current-run"
    old_run.mkdir(parents=True)
    current_run.mkdir(parents=True)
    (old_run / "trace.jsonl").write_text("", encoding="utf-8")
    (current_run / "trace.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "current_run.json").write_text(
        json.dumps({"run_id": "current-run", "run_dir": str(current_run)}),
        encoding="utf-8",
    )
    assert resolve_run_dir(tmp_path, "latest") == current_run.resolve()
