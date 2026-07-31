import pytest

from AstralNexus.tools.mcp_sessions import MCPSessionManager


def test_mcp_session_is_stateful_and_owner_scoped():
    manager = MCPSessionManager()
    session = manager.open_session(
        "research-tools",
        owner="run-1",
        capabilities={"search", "fetch"},
        metadata={"task_id": "T-001"},
    )
    assert manager.get(session.session_id, owner="run-1").authorize("search")
    assert not session.authorize("terminal")
    with pytest.raises(KeyError):
        manager.get(session.session_id, owner="run-2")

    manager.close(session.session_id, owner="run-1")
    assert not manager.get(session.session_id, owner="run-1").authorize("search")
    assert manager.snapshot()[0]["state"] == "closed"
