"""A verifier transport outage is a manual-review stop, never a verdict."""
from __future__ import annotations

import json
import socket
from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskNode


def _events(run_dir: Path) -> list[dict]:
    events = []
    for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def test_verifier_transport_budget_exhaustion_blocks_without_verdict_or_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", "3")
    with Orchestrator(tmp_path, run_id="verifier-outage", max_iterations=5) as orch:
        node = TaskNode(
            node_id="submitted-work",
            goal="verify already submitted work",
            role="coder",
            status="result_submitted",
            metadata={"verifier_transport_interrupts": 3},
        )
        orch.task_graph.add_node(node)

        def unavailable(_node_id: str):
            raise socket.timeout("provider read timed out")

        monkeypatch.setattr(orch.verifier, "verify_node", unavailable)
        result = orch.run()
        final_node = orch.task_graph.get_node("submitted-work")
        events = _events(tmp_path)
        event_types = [event.get("event_type") for event in events]

    assert result["status"] == "blocked"
    assert result["stop_code"] == "verification_unavailable"
    assert result["verification_unavailable"]["attempts"] == 4
    assert final_node is not None and final_node.status == "result_submitted"
    assert "verifier.unavailable" in event_types
    assert "verifier.exception" not in event_types
    assert "verifier.failed" not in event_types
    assert "task.evidence.finalized" not in event_types
    assert "governor.verification_recovery.proposed" not in event_types
    assert orch.runtime_state_manager.state.recovery_churn_total == 0
