"""P2 (DeerFlow loop_capped port): zero-write budget-burn detection.

Live r13c evidence: N-125 spent its whole execution on ~70 DIVERSE read-only
calls — ProgressGuard's cycle detection never fired because no fingerprint
ever repeated — and submitted a zero-write confession at the end; N-123 and
N-126 died at the wall clock with complete drafts stranded in context and
zero landed bytes. WriteProgressGuard watches the budget fraction against
filesystem-measured owned-file versions and escalates nudge → final warning
→ tool withdrawal, converting wall deaths into early honest capped
submissions with receipts preserved.
"""
import json

from AstralNexus.runtime.progress_guard import WriteProgressGuard, write_progress_feedback
from AstralNexus.runtime.task_execution_budget import TaskExecutionBudget


class _FakeBudget:
    def __init__(self, calls=0, max_calls=0, seconds=0.0, max_seconds=0.0):
        self._snapshot = {
            "tool_calls_used": calls, "max_tool_calls": max_calls,
            "elapsed_seconds": seconds, "max_seconds": max_seconds,
        }

    def snapshot(self):
        return dict(self._snapshot)

    def exhaustion_reason(self):
        return None


def test_guard_silent_below_thresholds_and_until_first_observation():
    guard = WriteProgressGuard(_FakeBudget(seconds=10.0, max_seconds=100.0))
    assert guard.check() is None  # no baseline yet
    guard.observe("v1")
    assert guard.check() is None  # 10% consumed


def test_escalation_ladder_nudge_warn_cap_one_shot_each():
    guard = WriteProgressGuard(_FakeBudget())
    guard.observe("v1")

    def fraction(value):
        guard.budget._snapshot["elapsed_seconds"] = value
        return guard.check()

    guard.budget._snapshot["max_seconds"] = 100.0
    assert fraction(55.0)["action"] == "nudge"
    assert fraction(56.0) is None  # one-shot
    assert fraction(72.0)["action"] == "final_warning"
    signal = fraction(90.0)
    assert signal["action"] == "cap_text_only"
    assert signal["stop_reason"] == "zero_write_capped"
    assert fraction(95.0) is None  # one-shot
    assert guard.actions_taken == ["nudge", "final_warning", "cap_text_only"]


def test_sudden_jump_emits_only_the_highest_due_action():
    guard = WriteProgressGuard(_FakeBudget(seconds=0.0, max_seconds=100.0))
    guard.observe("v1")
    guard.budget._snapshot["elapsed_seconds"] = 95.0
    signal = guard.check()
    assert signal["action"] == "cap_text_only"
    assert signal["budget_fraction"] == 0.95


def test_real_owned_file_write_disarms_the_guard_permanently():
    guard = WriteProgressGuard(_FakeBudget(seconds=0.0, max_seconds=100.0))
    guard.observe("v1")
    guard.budget._snapshot["elapsed_seconds"] = 55.0
    assert guard.check()["action"] == "nudge"
    guard.observe("v2")  # filesystem-measured version change = landed bytes
    assert guard.writes_observed is True
    guard.budget._snapshot["elapsed_seconds"] = 99.0
    assert guard.check() is None  # never escalates after real writes


def test_call_budget_dimension_also_drives_the_fraction():
    guard = WriteProgressGuard(_FakeBudget(calls=45, max_calls=80))
    guard.observe("v1")
    signal = guard.check()
    assert signal["action"] == "nudge"
    assert signal["budget_fraction"] == round(45 / 80, 3)


def test_unbounded_budget_never_escalates():
    guard = WriteProgressGuard(_FakeBudget())
    guard.observe("v1")
    assert guard.budget_fraction() == 0.0
    assert guard.check() is None


def test_from_env_overrides_thresholds(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WRITE_PROGRESS_NUDGE", "0.2")
    monkeypatch.setenv("ASTRALNEXUS_WRITE_PROGRESS_WARN", "0.3")
    monkeypatch.setenv("ASTRALNEXUS_WRITE_PROGRESS_CAP", "0.4")
    guard = WriteProgressGuard.from_env(_FakeBudget(seconds=25.0, max_seconds=100.0))
    guard.observe("v1")
    assert (guard.nudge_fraction, guard.warn_fraction, guard.cap_fraction) == (0.2, 0.3, 0.4)
    assert guard.check()["action"] == "nudge"  # 0.25 >= 0.2 but < 0.3


def test_feedback_texts_are_honest_and_action_specific():
    cap = write_progress_feedback({"action": "cap_text_only", "budget_fraction": 0.9})
    assert "工具已被收回" in cap and "zero_write_capped" in cap and "不得声称验收通过" in cap
    warn = write_progress_feedback({"action": "final_warning", "budget_fraction": 0.72})
    assert "最后一次提醒" in warn and "增量写入" in warn
    nudge = write_progress_feedback({"action": "nudge", "budget_fraction": 0.55})
    assert "增量落盘" in nudge and "草稿会随预算耗尽全部丢失" in nudge


def test_stream_chat_nudges_then_caps_diverse_readonly_calls(tmp_path, monkeypatch):
    """The exact N-125 shape: every call different (no cycle fingerprint),
    nothing ever written — the runtime must take back control by budget
    fraction instead of burning 100% and hoping for a confession."""
    from AstralNexus.runtime.llm_client import LLMClient

    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    now = [0.0]
    requests = []
    observed = []

    def stream(messages, **kwargs):
        requests.append((list(messages), kwargs))
        round_index = len(requests)
        if round_index <= 4:
            now[0] = {1: 55.0, 2: 72.0, 3: 90.0, 4: 95.0}[round_index]
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": f"c{round_index}",
                "function": {"name": "file_read",
                             "arguments": json.dumps({"path": f"doc{round_index}.md"})},
            }]}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "honest capped summary"}},
                               {"delta": {}, "finish_reason": "stop"}]}

    def execute(_executor, calls):
        # Diverse successful reads with different evidence: ProgressGuard's
        # cycle detector must NOT fire; only the write-progress guard may.
        return [{"status": "success", "content": f"body-of-{c['id']}"} for c in calls]

    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", execute)
    budget = TaskExecutionBudget(max_seconds=100, clock=lambda: now[0])
    answer = client.stream_chat(
        [{"role": "user", "content": "fix the collector"}],
        tools=[{"type": "function"}],
        workspace_root=tmp_path,
        owned_paths=["deliverables"],
        task_execution_budget=budget,
        event_callback=observed.append,
    )

    assert answer == "honest capped summary"
    stalled = [e for e in observed if e.get("event_type") == "llm.write_progress_stalled"]
    assert [e["action"] for e in stalled] == ["nudge", "final_warning", "cap_text_only"]
    assert stalled[-1]["stop_reason"] == "zero_write_capped"
    # After the cap the tools are withdrawn for every subsequent request.
    capped_round = next(
        index for index, (_, kwargs) in enumerate(requests)
        if kwargs.get("tools") is None
    )
    assert all(kwargs.get("tools") is None for _, kwargs in requests[capped_round:])
    # The cap message is the honest-summary instruction, in conversation.
    cap_message = requests[capped_round][0][-1]
    assert cap_message["role"] == "user" and "工具已被收回" in cap_message["content"]
    assert client.last_request_stats["write_progress"] == "capped_zero_output"
    # ProgressGuard must not have been the one firing (no repeated cycles).
    assert not [e for e in observed if e.get("event_type") == "llm.progress_stalled"]


def test_stream_chat_guard_disarmed_by_real_owned_write(tmp_path, monkeypatch):
    """One real landed byte inside owned paths silences the guard for good —
    the signal is filesystem state, never a model claim."""
    from AstralNexus.runtime.llm_client import LLMClient

    client = LLMClient(model="qwen-plus", api_key="test-key", interactive=False)
    (tmp_path / "deliverables").mkdir()
    now = [0.0]
    observed = []
    requests = []

    def stream(messages, **kwargs):
        requests.append(kwargs)
        round_index = len(requests)
        if round_index <= 3:
            now[0] = {1: 40.0, 2: 60.0, 3: 80.0}[round_index]
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": f"w{round_index}",
                "function": {"name": "python_exec",
                             "arguments": json.dumps({"code": f"print({round_index})"})},
            }]}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "done"}},
                               {"delta": {}, "finish_reason": "stop"}]}

    def execute(_executor, calls):
        # Round 2's call actually lands a file inside owned paths.
        if calls and calls[0]["id"] == "w2":
            (tmp_path / "deliverables" / "landed.txt").write_text("bytes", encoding="utf-8")
        return [{"status": "success", "output": f"out-{c['id']}"} for c in calls]

    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr("AstralNexus.tools.executor.ToolExecutor.execute_tool_calls", execute)
    budget = TaskExecutionBudget(max_seconds=100, clock=lambda: now[0])
    answer = client.stream_chat(
        [{"role": "user", "content": "implement"}],
        tools=[{"type": "function"}],
        workspace_root=tmp_path,
        owned_paths=["deliverables"],
        task_execution_budget=budget,
        event_callback=observed.append,
    )
    assert answer == "done"
    # The 60% round would have nudged, but the write landed first (observe
    # runs inside the same round's per-call loop, check runs after it).
    actions = [e["action"] for e in observed if e.get("event_type") == "llm.write_progress_stalled"]
    assert "cap_text_only" not in actions
    assert all(kwargs.get("tools") is not None for kwargs in requests)
