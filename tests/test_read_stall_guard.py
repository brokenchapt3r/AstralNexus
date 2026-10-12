"""Acceptance tests for the read-stall (livelock) guard budget.

These tests pin the three acceptance points required for the r11 N-007
livelock fix (doc 14, 2026-09-11):

1. An identical-state tool loop terminates and hands off automatically
   (guard halt -> client forces text-only / withdraws tools -> worker ends
   failed -> Governor recovery replan).
2. Legitimate long-running work is never falsely halted (interleaved
   writes, changed content versions, and many distinct reads all stay
   within budget; the historical eight-successful-reread contract holds).
3. When a stalled worker is terminated, its wave still converges: healthy
   siblings are verified/accepted and the stalled node enters recovery.
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.runtime.tool_guardrails import ToolCallGuard


def _read_call(path: str) -> dict:
    return {"function": {"name": "file_read", "arguments": json.dumps({"path": path})}}


def _write_call(path: str, content: str = "x") -> dict:
    return {
        "function": {
            "name": "file_write",
            "arguments": json.dumps({"path": path, "content": content}),
        }
    }


@pytest.mark.parametrize("tool", [
    "runtime_dependencies", "glob_files", "sqlite_query", "skills_list",
    "skill_view", "runtime_plugins", "mcp_servers", "mcp_tools", "lsp_query",
    "runtime_inspect", "workflow_status", "schedule_list", "session_history",
    "find_skills", "skill_list", "skill_audit", "browser_fetch",
    "arxiv_search", "crossref_search", "openalex_search", "github_repo_contents",
    "http_request",
])
def test_readonly_discovery_cannot_erase_unchanged_read_history(tmp_path, tool):
    (tmp_path / "same.txt").write_text("unchanged", encoding="utf-8")
    guard = ToolCallGuard(workspace_root=tmp_path, read_streak_limit=2)
    read = _read_call("same.txt")
    for index in range(3):
        assert guard.admit(read)[0]
        guard.observe(read, {"status": "success", "offset": 0, "returned_chars": 9})
        discovery = {"function": {"name": tool, "arguments": {"probe": index}}}
        assert guard.admit(discovery)[0]
        guard.observe(discovery, {"status": "success"})
    assert not guard.admit(read)[0]
    # A real change to the observed file still permits a new investigation.
    (tmp_path / "same.txt").write_text("changed", encoding="utf-8")
    assert guard.admit(read)[0]


# ---------------------------------------------------------------------------
# Point 1: identical-state loops halt and hand off automatically.
# ---------------------------------------------------------------------------

def test_identical_successful_reads_exceeding_stall_budget_are_halted(tmp_path):
    (tmp_path / "contract.md").write_text("v1", encoding="utf-8")
    guard = ToolCallGuard(read_streak_limit=3, workspace_root=tmp_path)
    call = _read_call("contract.md")
    # Budget 3 admits reads while streak <= 3, i.e. four successful unchanged
    # reads; the fifth is a livelock and must be denied.
    for _ in range(4):
        allowed, _reason = guard.admit(call)
        assert allowed is True
        guard.observe(call, {"status": "success"})
    decision = guard.decision(call)
    assert decision["action"] == "halt"
    assert decision["reason"] == "repeated_identical_tool_call"
    assert decision["failure_count"] == 0
    assert decision["read_streak_count"] == 4
    blocked, reason = guard.admit(call)
    assert blocked is False
    assert reason == "repeated_identical_tool_call"
    assert guard.snapshot()["read_streak_counts"]


def test_read_stall_halt_decision_hands_off_to_governor_replan(tmp_path):
    from AstralNexus.core.governor import GlobalGovernor
    from AstralNexus.core.task_graph import TaskGraph
    from AstralNexus.runtime.trace import TraceRecorder

    (tmp_path / "f.md").write_text("x", encoding="utf-8")
    guard = ToolCallGuard(read_streak_limit=1, workspace_root=tmp_path)
    call = _read_call("f.md")
    for _ in range(2):
        guard.admit(call)
        guard.observe(call, {"status": "success"})
    decision = guard.decision(call)
    assert decision["action"] == "halt"

    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    result = governor.handle_tool_loop_decision(
        node_id="N-007",
        tool_name="file_read",
        guard_decision=decision,
        role_toolset=["file_read", "file_write"],
    )
    # halt with recovery budget -> replan: the loop is handed off to a
    # governed repair task instead of spinning forever.
    assert result["strategy"] == "replan"
    assert "N-007" in result["repair_goal"]


def test_stream_chat_terminates_identical_read_livelock(monkeypatch, tmp_path):
    from AstralNexus.runtime.llm_client import LLMClient, LLMClientError

    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    monkeypatch.setenv("ASTRALNEXUS_TOOL_READ_STREAK_LIMIT", "2")
    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)

    state = {"rounds": 0}
    seen_tools: list = []

    def fake_stream(messages, **kwargs):
        seen_tools.append(kwargs.get("tools"))
        state["rounds"] += 1
        yield {
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": f"read-{state['rounds']}",
                    "function": {"name": "file_read", "arguments": '{"path":"a.txt"}'},
                }]},
            }]
        }

    executed_batches: list = []

    def fake_execute(self, batch):
        executed_batches.append(batch)
        return [{"status": "success", "content": "content"} for _ in batch]

    monkeypatch.setattr(client, "stream", fake_stream)
    monkeypatch.setattr(
        "AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", fake_execute
    )

    # The model stubbornly re-issues the identical read forever; with the
    # stall budget at 2, three identical reads execute, the guard halts and
    # tools are withdrawn.  Fix 14: the stubborn post-withdrawal tool call is
    # nudged once toward a text wrap-up, then the loop ends gracefully with
    # an honest result — never a hidden execution, never an unbounded loop.
    result = client.stream_chat(
        [{"role": "user", "content": "loop"}],
        tools=[{"type": "function"}],
        workspace_root=tmp_path,
    )
    assert "execution ended after tool withdrawal" in result

    assert sum(len(batch) for batch in executed_batches) == 3
    assert seen_tools[-1] is None
    assert seen_tools[-2] is None
    guard_stats = client.last_request_stats["tool_guard"]
    assert guard_stats["read_streak_limit"] == 2
    assert max(guard_stats["read_streak_counts"].values()) == 3
    assert "halt" in guard_stats["decisions"].values()


# ---------------------------------------------------------------------------
# Point 2: legitimate long-running work is never falsely halted.
# ---------------------------------------------------------------------------

def test_alternating_reads_and_writes_never_halt(tmp_path):
    """A progressing worker may re-read the same unchanged file forever."""
    (tmp_path / "src.py").write_text("v1", encoding="utf-8")
    guard = ToolCallGuard(read_streak_limit=3, workspace_root=tmp_path)
    read = _read_call("src.py")
    for cycle in range(40):
        for _ in range(3):
            allowed, _ = guard.admit(read)
            assert allowed is True, f"cycle {cycle} falsely halted"
            guard.observe(read, {"status": "success"})
            assert guard.decision(read)["action"] == "retry_or_continue"
        # Real long-running work writes new content each time, so every
        # write has a distinct fingerprint and counts as progress.
        write = _write_call("out.md", content=f"cycle-{cycle}")
        allowed, _ = guard.admit(write)
        assert allowed is True
        guard.observe(write, {"status": "success", "artifact_created": True})


def test_content_version_change_resets_read_streak(tmp_path):
    """Re-reading a file after every mutation is a fresh state each time."""
    target = tmp_path / "data.json"
    guard = ToolCallGuard(read_streak_limit=2, workspace_root=tmp_path)
    for version in range(30):
        target.write_text(f"v{version}", encoding="utf-8")
        call = _read_call("data.json")
        allowed, _ = guard.admit(call)
        assert allowed is True
        guard.observe(call, {"status": "success"})
        assert guard.decision(call)["action"] == "retry_or_continue"


def test_many_distinct_reads_without_writes_never_halt(tmp_path):
    """Evidence-gathering workers read many distinct files without acting."""
    guard = ToolCallGuard(read_streak_limit=3, workspace_root=tmp_path)
    for i in range(50):
        name = f"file_{i}.md"
        (tmp_path / name).write_text("x", encoding="utf-8")
        call = _read_call(name)
        allowed, _ = guard.admit(call)
        assert allowed is True
        guard.observe(call, {"status": "success"})
        assert guard.decision(call)["action"] == "retry_or_continue"


def test_stall_budget_blocks_only_after_budget_and_write_rearms(tmp_path):
    (tmp_path / "plan.md").write_text("p", encoding="utf-8")
    guard = ToolCallGuard(read_streak_limit=3, workspace_root=tmp_path)
    read = _read_call("plan.md")
    for _ in range(4):
        assert guard.admit(read)[0] is True
        guard.observe(read, {"status": "success"})
    assert guard.decision(read)["action"] == "halt"
    assert guard.admit(read)[0] is False
    write = _write_call("plan.md", "q")
    assert guard.admit(write)[0] is True
    guard.observe(write, {"status": "success"})
    (tmp_path / "plan.md").write_text("q", encoding="utf-8")
    assert guard.admit(read)[0] is True
    guard.observe(read, {"status": "success"})
    assert guard.decision(read)["action"] == "retry_or_continue"


def test_default_budget_preserves_eight_successful_rereads_contract(tmp_path):
    """Doc 14 §04:54 contract: default budget admits 8 identical re-reads."""
    (tmp_path / "m.py").write_text("old", encoding="utf-8")
    guard = ToolCallGuard(max_repeats=1, workspace_root=tmp_path)
    assert guard.read_streak_limit == 8
    call = _read_call("m.py")
    for _ in range(8):
        assert guard.admit(call)[0] is True
        guard.observe(call, {"status": "success"})
        assert guard.decision(call)["action"] == "retry_or_continue"


# ---------------------------------------------------------------------------
# Point 3: a terminated stalled worker lets its wave verify and recover.
# ---------------------------------------------------------------------------

def test_stalled_worker_termination_lets_wave_verify_and_recover(tmp_path, monkeypatch):
    from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
    from AstralNexus.agents.coder import Coder
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.core.verifier import VerificationResult, VerificationVerdict

    guard_message = "tool loop guard halted repeated call (repeated_identical_tool_call)"

    with Orchestrator(tmp_path, run_id="stall-converge", max_iterations=1) as orchestrator:
        orchestrator.task_graph.add_node(TaskNode(
            node_id="synth", role="coder", goal="synthesize seven docs", status="ready",
            metadata={"required_capabilities": ["code_generation"]},
        ))
        orchestrator.task_graph.add_node(TaskNode(
            node_id="writer", role="coder", goal="write report", status="ready",
            metadata={"required_capabilities": ["code_generation"]},
        ))
        agent = Coder()

        def execute(context):
            if context.node_id == "synth":
                # What a livelocked worker now looks like after the guard
                # trips: forced text-only, model keeps calling tools, client
                # raises "withdrawn", agent reports FAILED with the reason.
                return AgentResponse(
                    outcome=ExecutionOutcome.FAILED,
                    content=f"Real LLM role execution failed: {guard_message}",
                )
            return AgentResponse(outcome=ExecutionOutcome.SUCCEEDED, content="report written")

        monkeypatch.setattr(agent, "execute", execute)
        monkeypatch.setattr(orchestrator, "_instantiate_governed_worker", lambda *args: agent)

        verified: list = []

        def fake_verify(node_id):
            verified.append(node_id)
            return VerificationResult(
                node_id=node_id,
                verdict=VerificationVerdict.APPROVED,
                dimensions=[],
                overall_score=1.0,
            )

        monkeypatch.setattr(orchestrator.verifier, "verify_node", fake_verify)

        recovered: list = []

        def fake_recovery(node_id):
            recovered.append(node_id)
            return True

        monkeypatch.setattr(orchestrator, "_attempt_recovery", fake_recovery)

        results = orchestrator.run()

    # The wave converged: both nodes were processed in the same iteration.
    assert recovered == ["synth"]
    assert verified == ["writer"]
    assert results["completed_tasks"] == 1
    assert results["failed_tasks"] == 1
    assert results["recovery_attempts"] == 1
    assert orchestrator.task_graph.get_node("writer").status == "completed"
    saved = orchestrator.graph_store.load_task_graph("stall-converge").get_node("synth")
    assert saved.status == "failed"
    assert "tool loop guard" in saved.metadata["last_failure"]["message"]
