"""Worker Matcher transient-failure semantics.

Live r13c evidence (2026-09-29T11:35:29Z, generation 10): one malformed LLM
matcher response ("worker matcher returned non-object") raised straight out of
``select_worker_profile``; the orchestrator blocked healthy running node N-122
before any attempt ("execution failed after 0 attempts"), a single malformed
fault-recovery proposal then escalated to ``recovery_exhausted`` terminal
blocked, dragged N-119 into ``node_blocked_by_terminal``, and pushed the run
into replan churn.

A transient output-format/transport fault is not a capability decision and must
not become a fatal graph state.  Contract encoded here:

1. malformed response -> one bounded retry (a valid second answer wins);
2. persistent malformed responses -> degrade to the deterministic capability
   validator rematch (the path the design already calls authoritative),
   observably, never silently;
3. hallucinated profile names remain an immediate hard governance failure
   (existing contract, no retry);
4. explicit null decisions remain true capability-gap decisions (untouched);
5. matcher failure without any capable candidate keeps gap semantics
   (NO_CAPABILITY_CANDIDATE), never a generic-agent fallback.
"""

from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
from AstralNexus.core.task_graph import TaskGraph
import pytest


CODING_TASK = {
    "task_id": "transient-coding",
    "task_type": "implementation",
    "goal": "修复采集器脚本并重跑生成证据文件",
    "required_capabilities": ["code_generation", "backend_development"],
}


class ScriptedMatcher:
    """Chat stub returning scripted responses; records call count."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class CollectingRecorder:
    """Duck-typed trace recorder collecting (event_type, payload) pairs."""

    def __init__(self):
        self.events = []

    def record(self, event_type, payload=None):
        self.events.append((str(getattr(event_type, "value", event_type)), payload or {}))


def _governor(matcher, recorder=None):
    return GlobalGovernor(
        TaskGraph(),
        recorder if recorder is not None else CollectingRecorder(),
        llm_client=matcher,
    )


def test_transient_non_object_response_is_retried_then_succeeds():
    matcher = ScriptedMatcher([
        "[1,2,3]",  # malformed: JSON array, not an object
        '{"worker_type":"Coding","reason_codes":["RETRY_OK"],"confidence":0.9}',
    ])
    decision = _governor(matcher).select_worker_profile(
        dict(CODING_TASK), HETEROGENEOUS_WORKERS
    )
    assert matcher.calls == 2
    assert decision["selected"] is not None
    assert decision["selected"]["worker_type"] == "Coding"
    assert "RETRY_OK" in decision["reason_codes"]


def test_transient_transport_error_is_retried_then_succeeds():
    matcher = ScriptedMatcher([
        TimeoutError("The read operation timed out"),
        '{"worker_type":"Coding","reason_codes":["TRANSPORT_RETRY"],"confidence":0.8}',
    ])
    decision = _governor(matcher).select_worker_profile(
        dict(CODING_TASK), HETEROGENEOUS_WORKERS
    )
    assert matcher.calls == 2
    assert decision["selected"]["worker_type"] == "Coding"


def test_persistent_transient_failure_degrades_to_validated_rematch():
    matcher = ScriptedMatcher(["not json at all"])
    recorder = CollectingRecorder()
    decision = _governor(matcher, recorder).select_worker_profile(
        dict(CODING_TASK), HETEROGENEOUS_WORKERS
    )
    assert matcher.calls == 2  # bounded: exactly one retry, no infinite loop
    assert decision["selected"] is not None
    assert decision["selected"]["worker_type"] == "Coding"
    assert "MATCHER_FAILURE_REMATCH" in decision["reason_codes"]
    events = {event for event, _ in recorder.events}
    assert "governor.worker_matcher.failed" in events  # observable, never silent


def test_hallucinated_profile_remains_immediate_hard_failure():
    matcher = ScriptedMatcher(['{"worker_type":"InventedWorker","confidence":1.0}'])
    with pytest.raises(RuntimeError, match="real LLM Worker Matcher failed"):
        _governor(matcher).select_worker_profile(
            {"task_id": "hallucination", "required_capabilities": ["research_design"]},
            HETEROGENEOUS_WORKERS,
        )
    assert matcher.calls == 1  # governance violation: no retry


def test_explicit_null_decision_stays_capability_gap():
    matcher = ScriptedMatcher([
        '{"worker_type":null,"confidence":0.9,"reason_codes":["CAPABILITY_GAP"]}'
    ])
    decision = _governor(matcher).select_worker_profile(
        {"task_id": "null-decision", "required_capabilities": ["research_design"]},
        HETEROGENEOUS_WORKERS,
    )
    assert decision["selected"] is None
    assert "NO_CAPABILITY_CANDIDATE" in decision["reason_codes"]


def test_transient_failure_without_capable_candidate_keeps_gap_semantics():
    matcher = ScriptedMatcher(["[1,2,3]"])
    decision = _governor(matcher).select_worker_profile(
        {
            "task_id": "no-candidate",
            "required_capabilities": ["quantum_widget_weaving"],
        },
        HETEROGENEOUS_WORKERS,
    )
    assert matcher.calls == 2  # retried once, then degraded
    assert decision["selected"] is None
    assert "NO_CAPABILITY_CANDIDATE" in decision["reason_codes"] or (
        "ZERO_CAPABILITY_MATCH_REJECTED" in decision["reason_codes"]
    )
