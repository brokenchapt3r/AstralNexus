"""Regression tests for the r11 blocked-run causal chain (doc 14 §07:55).

Covers the three code-level seams that terminated run r11 as blocked:

1. Subprocess output encoding closed loop — background process logs are
   byte-faithful (children write raw console-codepage bytes straight to the
   OS handle), every spawn forces UTF-8 for Python children, and foreground
   capture decodes bytes parent-side instead of returning mojibake.
2. ``admitted``/``waiting_dependencies`` nodes permanently blocked by a
   terminal dependency now persist the ``blocked`` lifecycle status, so
   Governor recovery patches may legally target them.
3. Verification-recovery proposals that try to reopen a COMPLETED producer
   are converted into a legal ``<producer>-R<k>`` repair-node derivation that
   inherits responsibility, acceptance, and re-arm wiring.
"""
from __future__ import annotations

import sys

from AstralNexus.tools.builtin_tools import _decode_output_bytes
from AstralNexus.tools.executor import ToolExecutor


# ---------------------------------------------------------------------------
# Seam 1: subprocess output encoding closed loop.
# ---------------------------------------------------------------------------

def test_decode_output_bytes_prefers_utf8_then_gb18030_then_lossy():
    text, encoding = _decode_output_bytes("启动成功".encode("utf-8"))
    assert text == "启动成功" and encoding == "utf-8"
    text, encoding = _decode_output_bytes("启动成功".encode("gb18030"))
    assert text == "启动成功" and encoding == "gb18030"
    text, encoding = _decode_output_bytes(b"\xff\xfe\x00bad bytes")
    assert encoding == "utf-8(replace)" and text  # never drops diagnostics


def test_foreground_capture_decodes_gbk_child_output(tmp_path, monkeypatch):
    """r11 N-004 died reading GBK console output; capture must not mojibake."""
    monkeypatch.setenv("ASTRALNEXUS_ALLOW_TERMINAL", "true")
    executor = ToolExecutor(workspace_root=tmp_path)
    child = (
        "import sys; sys.stdout.buffer.write('启动成功\\n'.encode('gb18030')); "
        "sys.stdout.flush()"
    )
    result = executor.execute_tool(
        "terminal_exec", {"command": [sys.executable, "-c", child]}
    )
    assert result["status"] == "success", result
    assert "启动成功" in result["stdout"]
    assert result["output_encoding"] == "gb18030"


def test_terminal_exec_children_get_utf8_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_ALLOW_TERMINAL", "true")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    executor = ToolExecutor(workspace_root=tmp_path)
    child = "import os; print(os.environ.get('PYTHONIOENCODING', 'unset'))"
    result = executor.execute_tool(
        "terminal_exec", {"command": [sys.executable, "-c", child]}
    )
    assert result["status"] == "success", result
    assert result["stdout"].strip() == "utf-8"


def test_background_log_is_byte_faithful_and_decodable(tmp_path, monkeypatch):
    """Background logs record child bytes exactly; readers decode explicitly."""
    monkeypatch.setenv("ASTRALNEXUS_ALLOW_TERMINAL", "true")
    raw = "启动失败：端口占用".encode("gb18030")
    seen: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            self.pid = 424242
            seen["env"] = kwargs.get("env") or {}
            kwargs["stdout"].write(raw)

    monkeypatch.setattr(
        "AstralNexus.tools.builtin_tools.subprocess.Popen", FakePopen
    )
    executor = ToolExecutor(workspace_root=tmp_path)
    started = executor.execute_tool(
        "terminal_exec", {"command": ["uvicorn", "main:app", "--port", "5001"]}
    )
    assert started["status"] == "started" and started["background"] is True
    # Python children are forced UTF-8 even through terminal_exec now.
    assert seen["env"].get("PYTHONIOENCODING") == "utf-8"
    log_path = tmp_path / started["log_path"]
    assert log_path.read_bytes() == raw  # byte-faithful, no fake text layer

    blocked = executor.execute_tool("file_read", {"path": started["log_path"]})
    assert blocked["status"] == "error" and "encoding" in blocked["error"]
    decoded = executor.execute_tool(
        "file_read", {"path": started["log_path"], "encoding": "gb18030"}
    )
    assert decoded["status"] == "success"
    assert "启动失败" in decoded["content"]


# ---------------------------------------------------------------------------
# Seam 2: permanently blocked nodes persist a recoverable lifecycle status.
# ---------------------------------------------------------------------------

def test_state_machine_allows_admitted_and_waiting_into_blocked():
    from AstralNexus.core.state_machine import VALID_TRANSITIONS
    assert "blocked" in VALID_TRANSITIONS["admitted"]
    assert "blocked" in VALID_TRANSITIONS["waiting_dependencies"]


def test_terminal_dependency_block_is_persisted_and_recoverable(tmp_path):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="block-persist") as orchestrator:
        failed = TaskNode(node_id="T-001", goal="run e2e", role="tester", status="failed")
        failed.metadata["_terminal_failure"] = True
        failed.metadata["_terminal_reason"] = "recovery_exhausted"
        consumer = TaskNode(
            node_id="T-002", goal="publish report", role="coder",
            status="admitted", dependencies=["T-001"],
        )
        orchestrator.task_graph.add_node(failed)
        orchestrator.task_graph.add_node(consumer)

        blocked = orchestrator._check_for_blocked_tasks()
        assert "T-002" in blocked
        # The lifecycle status — not only metadata — must say blocked, or
        # every Governor recovery patch targeting T-002 stays invalid.
        assert orchestrator.task_graph.get_node("T-002").status == "blocked"

        applied = orchestrator.governor.apply_task_graph_patch(
            TaskGraphPatch(
                add_nodes=[{
                    "node_id": "T-002-R1",
                    "goal": "recover the report publication path",
                    "task_type": "implementation",
                    "recovery_of": "T-002",
                    "owned_paths": ["deliverables/reports/"],
                }],
                reason_codes=["TEST_RECOVERY"],
            ),
            parent_task_id="T-002",
        )
        assert "T-002-R1" in applied["added"]


def test_artifact_handoff_block_still_keeps_consumer_admitted(tmp_path):
    """The missing-artifact branch must not demote consumers (existing contract)."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="handoff-keep") as orchestrator:
        graph = orchestrator.task_graph
        graph.add_node(TaskNode(
            node_id="producer", goal="create SQL", role="coder", status="completed",
            output_artifacts=["artifact:db_init_sql"],
        ))
        graph.add_node(TaskNode(
            node_id="consumer", goal="use SQL", role="coder",
            dependencies=["producer"], input_artifacts=["artifact:db_init_sql"],
        ))
        assert orchestrator._check_for_blocked_tasks() == ["consumer"]
        assert graph.get_node("consumer").status == "admitted"


# ---------------------------------------------------------------------------
# Seam 3: completed producers are repaired by derivation, never reopened.
# ---------------------------------------------------------------------------

def test_completed_reopen_proposal_converts_to_repair_node(tmp_path, monkeypatch):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.core.verifier import (
        VerificationDimension,
        VerificationResult,
        VerificationVerdict,
    )

    with Orchestrator(tmp_path, run_id="reopen-convert") as orchestrator:
        graph = orchestrator.task_graph
        producer = TaskNode(
            node_id="N-007", role="coder", goal="write database doc",
            status="completed",
            owned_paths=["deliverables/docs/database.md"],
            output_artifacts=["artifact:database_doc"],
        )
        producer.metadata["required_capabilities"] = ["code_generation"]
        producer.metadata["acceptance_criteria"] = ["database.md matches schema.sql"]
        graph.add_node(producer)
        source = TaskNode(
            node_id="N-008", role="tester", goal="validate database doc",
            status="repair_pending",
        )
        graph.add_node(source)

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery",
            lambda **kwargs: TaskGraphPatch(
                update_nodes=[{
                    "node_id": "N-007", "status": "ready",
                    "details": "database.md mismatches schema.sql",
                }],
                reason_codes=["VERIFICATION_REPAIR"],
            ),
        )
        verdict = VerificationResult(
            node_id="N-008",
            verdict=VerificationVerdict.REPAIR_REQUIRED,
            dimensions=[VerificationDimension(
                name="accuracy", score=0.2, passed=False,
                details="tables section contradicts schema.sql",
            )],
            overall_score=0.2,
        )

        applied = orchestrator._apply_governed_verification_recovery(source, verdict)

        assert applied.get("added") == ["N-007-R1"]
        repair = graph.get_node("N-007-R1")
        assert repair is not None
        assert repair.status in {"admitted", "waiting_dependencies", "ready"}
        # Responsibility and acceptance are inherited from the producer.
        assert "deliverables/docs/database.md" in repair.owned_paths
        assert "artifact:database_doc" in repair.output_artifacts
        assert repair.metadata["_is_repair_node"] is True
        assert repair.metadata["_repair_target"] == "N-007"
        assert repair.metadata["repairs_completed_node"] == "N-007"
        # The producer's goal and the verification findings travel with the
        # repair node so the worker gets the full repair context.
        assert "write database doc" in repair.goal
        assert (
            repair.metadata["converted_from_invalid_reopen"]["details"]
            == "database.md mismatches schema.sql"
        )
        # Re-arm wiring: when the repair completes, the verification source
        # returns from repair_pending to ready.
        assert repair.metadata["parent_node_id"] == "N-008"
        assert "N-007" in repair.dependencies
        # Completed stays immutable.
        assert graph.get_node("N-007").status == "completed"
        events = [event["event_type"] for event in orchestrator.trace_recorder.events]
        assert "governor.verification_recovery.reopen_converted" in events


def test_conversion_inherits_contract_fields_into_patch(tmp_path):
    """The derived patch node carries role, capabilities, and acceptance."""
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="convert-unit") as orchestrator:
        producer = TaskNode(
            node_id="N-007", role="coder", goal="write database doc",
            status="completed",
            owned_paths=["deliverables/docs/database.md"],
            output_artifacts=["artifact:database_doc"],
        )
        producer.metadata["required_capabilities"] = ["code_generation"]
        producer.metadata["acceptance_criteria"] = ["database.md matches schema.sql"]
        producer.metadata["task_type"] = "implementation"
        orchestrator.task_graph.add_node(producer)
        source = TaskNode(
            node_id="N-008", role="tester", goal="validate", status="repair_pending",
        )
        orchestrator.task_graph.add_node(source)

        patch = TaskGraphPatch(
            update_nodes=[
                {"node_id": "N-007", "status": "ready"},
                {"node_id": "N-008", "status": "repair_pending"},
            ],
            reason_codes=["VERIFICATION_REPAIR"],
        )
        converted = orchestrator._convert_completed_reopen_to_repair_node(patch, source)

        # The illegal reopen is removed; the legal sibling update survives.
        assert converted.update_nodes == [{"node_id": "N-008", "status": "repair_pending"}]
        assert len(converted.add_nodes) == 1
        repair = converted.add_nodes[0]
        assert repair["node_id"] == "N-007-R1"
        assert repair["role"] == "coder"
        assert repair["task_type"] == "implementation"
        assert repair["required_capabilities"] == ["code_generation"]
        assert repair["acceptance_criteria"] == ["database.md matches schema.sql"]
        assert repair["dependencies"] == ["N-007"]
        assert "recovery_of" not in repair  # completed targets cannot be recovery_of
        assert repair["metadata"]["parent_node_id"] == "N-008"


def test_second_conversion_uses_next_repair_suffix(tmp_path, monkeypatch):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.core.verifier import (
        VerificationResult,
        VerificationVerdict,
    )

    with Orchestrator(tmp_path, run_id="reopen-suffix") as orchestrator:
        graph = orchestrator.task_graph
        producer = TaskNode(
            node_id="N-007", role="coder", goal="write doc", status="completed",
            owned_paths=["deliverables/docs/database.md"],
        )
        graph.add_node(producer)
        graph.add_node(TaskNode(
            node_id="N-007-R1", role="coder", goal="prior repair",
            status="completed", dependencies=["N-007"],
        ))
        source = TaskNode(
            node_id="N-008", role="tester", goal="validate doc",
            status="repair_pending",
        )
        graph.add_node(source)
        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery",
            lambda **kwargs: TaskGraphPatch(
                update_nodes=[{"node_id": "N-007", "status": "repair_pending"}],
                reason_codes=["VERIFICATION_REPAIR"],
            ),
        )
        verdict = VerificationResult(
            node_id="N-008", verdict=VerificationVerdict.REJECTED,
            dimensions=[], overall_score=0.0,
        )
        applied = orchestrator._apply_governed_verification_recovery(source, verdict)
        assert applied.get("added") == ["N-007-R2"]


def test_reverifier_ownership_deconflicts_with_repair_node(tmp_path, monkeypatch):
    """Live r11 09:12/10:03 shape: planner's re-verifier also claims the doc.

    The planner proposed add N-008-R1 owning docs/database.md (plus the
    producer artifact) AND update-reopen completed N-007.  After conversion
    both add nodes claimed the same leaf ownership and the Governor
    rejected the patch three times, terminalizing N-008 again.  The r11
    10:03 attempt-1 shape is harder still: the raw re-verifier carries
    ``recovery_of=N-007`` (the completed producer), so the naming
    normalization skips it and the Governor's ownership conflict check
    rejects it against the still-active source before the target-status
    gate can explain why.  The normalization must retarget recovery_of
    onto the source node, keep producer responsibility on the repair node,
    and re-anchor the re-verifier onto the source's verification evidence.
    """
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.core.verifier import (
        VerificationDimension,
        VerificationResult,
        VerificationVerdict,
    )

    with Orchestrator(tmp_path, run_id="reverifier-deconflict") as orchestrator:
        graph = orchestrator.task_graph
        producer = TaskNode(
            node_id="N-007", role="coder", goal="write database doc",
            status="completed",
            owned_paths=["deliverables/docs/database.md"],
            output_artifacts=["artifact:database_md"],
        )
        producer.metadata["acceptance_criteria"] = ["database.md matches schema.sql"]
        graph.add_node(producer)
        source = TaskNode(
            node_id="N-008", role="verifier", goal="validate database doc",
            status="repair_pending",
            owned_paths=["deliverables/runs/validation/database_doc_match.txt"],
            output_artifacts=["artifact:database_doc_validation_result"],
        )
        source.metadata["expected_outputs"] = [
            "deliverables/runs/validation/database_doc_match.txt"
        ]
        graph.add_node(source)

        monkeypatch.setattr(
            orchestrator.governor, "plan_verification_recovery",
            lambda **kwargs: TaskGraphPatch(
                add_nodes=[{
                    "node_id": "N-008-R1",
                    "goal": "Regenerate database documentation from schema.sql",
                    "role": "verifier",
                    "task_type": "verification",
                    "status": "ready",
                    "recovery_of": "N-007",
                    "dependencies": ["N-007"],
                    "owned_paths": ["deliverables/docs/database.md"],
                    "output_artifacts": ["artifact:database_md"],
                    "metadata": {
                        "expected_outputs": ["deliverables/docs/database.md"],
                    },
                }],
                update_nodes=[{
                    "node_id": "N-007", "status": "ready",
                    "details": "6 mismatches vs schema.sql",
                }],
                reason_codes=["VERIFICATION_REPAIR"],
            ),
        )
        verdict = VerificationResult(
            node_id="N-008",
            verdict=VerificationVerdict.REPAIR_REQUIRED,
            dimensions=[VerificationDimension(
                name="goal_coverage", score=0.0, passed=False,
                details="database.md contradicts schema.sql in 6 ways",
            )],
            overall_score=0.63,
        )

        applied = orchestrator._apply_governed_verification_recovery(source, verdict)

        # Both add nodes validate now: the patch is applied, not rejected.
        assert sorted(applied.get("added", [])) == ["N-007-R1", "N-008-R1"]
        repair = graph.get_node("N-007-R1")
        reverifier = graph.get_node("N-008-R1")
        # Producer responsibility stays exactly on the repair node.
        assert "deliverables/docs/database.md" in repair.owned_paths
        assert "artifact:database_md" in repair.output_artifacts
        assert "deliverables/docs/database.md" not in reverifier.owned_paths
        assert "artifact:database_md" not in reverifier.output_artifacts
        # Re-verifier is re-anchored onto the source verification evidence.
        assert "deliverables/runs/validation/database_doc_match.txt" in reverifier.owned_paths
        assert "artifact:database_doc_validation_result" in reverifier.output_artifacts
        assert reverifier.metadata.get("recovery_of") == "N-008"
        # Misattached producer target from the live 10:03 attempt-1 shape is
        # retargeted onto the source node, with the original kept for audit.
        assert reverifier.metadata.get("recovery_of_retargeted_from") == "N-007"
        assert reverifier.metadata["ownership_normalized_from_repair_conflict"][
            "stripped_paths"
        ] == ["deliverables/docs/database.md"]
        # A producer goal that lost its ownership carries the governance scope
        # note so the worker does not fight the ownership guard (r11 10:06
        # live: file_write refused, worker burned the attempt in a tool loop).
        assert "[governance-normalized]" in reverifier.goal
        assert "N-007-R1" in reverifier.goal
        assert reverifier.metadata.get("responsibility_scope") == "reverification_only"
        # A ready-born re-verifier must not bypass the dependency gate: it is
        # held at admitted until its repair dependency completes (r11 10:06:56
        # live race: N-008-R1 executed while N-007-R1 was still admitted).
        assert reverifier.status == "admitted"
        assert reverifier.metadata.get("ready_downgraded_for_repair_dependency") is True
        # Re-verification must run after the repair, not against stale docs.
        assert "N-007-R1" in reverifier.dependencies
        assert "N-007" in reverifier.dependencies
        # Completed producer remains immutable.
        assert graph.get_node("N-007").status == "completed"
        events = [event["event_type"] for event in orchestrator.trace_recorder.events]
        assert "governor.verification_recovery.reopen_converted" in events
        assert (
            "governor.verification_recovery.reverifier_ownership_normalized"
            in events
        )
        assert (
            "governor.verification_recovery.reverifier_recovery_of_retargeted"
            in events
        )
