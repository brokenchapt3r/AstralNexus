"""Regression tests for runtime defects exposed by static analysis."""
from unittest.mock import MagicMock, patch

import pytest

from AstralNexus.core.agent_registry import AgentRegistry, AgentRole
from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools.mcp_client import WebSocketTransport


def test_profile_schema_lookup_uses_registered_role_and_alias():
    roles = AgentRegistry()
    roles.register_role(AgentRole("coder", "Code", default_tools=["file_read"]))
    roles.register_role_alias("coding", "coder")
    with patch("AstralNexus.core.agent_registry.get_agent_registry", return_value=roles), patch(
        "AstralNexus.tools.executor.registry"
    ) as tools:
        tools.get_all_tool_names.return_value = ["file_read", "file_write"]
        tools.get_definitions.return_value = [{"name": "file_read"}]
        result = ToolExecutor().get_tool_schemas(profile="coding")
        assert result == [{"name": "file_read"}]
        tools.get_definitions.assert_called_once_with({"file_read"})


def test_unknown_profile_cannot_fall_back_to_all_tools():
    with patch("AstralNexus.core.agent_registry.get_agent_registry", return_value=AgentRegistry()):
        with pytest.raises(RuntimeError, match="Unknown worker profile"):
            ToolExecutor().get_tool_schemas(profile="missing")


@pytest.mark.parametrize("operation", ["_ws_send", "_ws_recv"])
def test_disconnected_websocket_reports_connection_error(operation):
    with patch.object(WebSocketTransport, "reconnect"):
        transport = WebSocketTransport("ws://localhost")
    with pytest.raises(ConnectionError, match="not connected"):
        if operation == "_ws_send":
            transport._ws_send("hello")
        else:
            transport._ws_recv()


def test_failed_websocket_request_closes_failed_connection():
    with patch.object(WebSocketTransport, "reconnect"):
        transport = WebSocketTransport("ws://localhost")
    connection = MagicMock()
    connection.recv.side_effect = ConnectionError("disconnected")
    transport._ws = connection
    with pytest.raises(ConnectionError, match="disconnected"):
        transport.send({"id": 1, "method": "ping"})
    connection.close.assert_called_once()
    assert transport._ws is None


def test_first_plan_failure_recovers_without_a_rejected_patch(tmp_path, monkeypatch):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator

    orchestrator = Orchestrator(tmp_path, run_id="missing-initial-patch")
    try:
        root_id = orchestrator.initialize_task_graph("build verified delivery", [])
        governor = orchestrator.governor
        governor.llm_matching_enabled = True
        governor.llm_client = object()
        repair = TaskGraphPatch(add_nodes=[{
            "node_id": "N-ARCH",
            "goal": "design system architecture",
            "dependencies": [root_id],
            "required_capabilities": ["system_architecture", "api_design"],
        }])
        replan = MagicMock(return_value=repair)
        merge = MagicMock(side_effect=AssertionError("No candidate exists to merge"))
        monkeypatch.setattr(governor, "plan_task_graph_patch", MagicMock(side_effect=ValueError("Invalid planner JSON")))
        monkeypatch.setattr(governor, "replan_rejected_task_graph_patch", replan)
        monkeypatch.setattr(governor, "merge_rejected_task_graph_patch", merge)

        assert orchestrator.plan_task_decomposition(root_id) == ["N-ARCH"]
        replan.assert_called_once()
        assert replan.call_args.kwargs["rejected_patch"] is None
        merge.assert_not_called()
        root = orchestrator.task_graph.get_node(root_id)
        assert root is not None and root.status == "completed"
    finally:
        orchestrator.shutdown()
