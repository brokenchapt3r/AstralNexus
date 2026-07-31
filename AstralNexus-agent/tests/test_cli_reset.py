import json

from AstralNexus.cli.commands import _handle_reset


def test_reset_preserves_run_evidence(tmp_path):
    assert _handle_reset([], home=tmp_path) == 0
    record = json.loads((tmp_path / "session_reset.json").read_text(encoding="utf-8"))
    assert record["action"] == "context_reset_only"
    assert "task_graph.sqlite" in record["preserved"]
