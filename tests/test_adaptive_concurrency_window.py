"""Regression tests for the recent-window failure signal (F8 / live wave-10).

Grounding: the b31 autonomous acceptance run decided

    2026-10-05T23:18:16.846 ready_wave.concurrency_decided
    {"node_count": 2, "selected_parallel": 1, "max_parallel": 4, "wave_id": "wave-10"}

one wave after the governor had recovered two independent ready nodes
(E-701 + N-201).  The width collapsed because the *run-wide* failure rate
(1/6 = 0.1667, a single failure five waves earlier) stayed above the 0.1
threshold and ``int(2 * 0.9) == 1``; the audit then recorded
``ramp_up_after_success`` because the reason was derived from the
already-penalized base.  These tests pin the fixed signal, the anti-thrash
floor, the hard caps and the emitted decision fields.
"""
from AstralNexus.core.adaptive_concurrency import (
    FAILURE_WINDOW_WAVES,
    AdaptiveConcurrency,
)


def _controller_like_wave_10() -> AdaptiveConcurrency:
    """State at the live wave-10 decision: stale run-wide ratio, clean window."""
    controller = AdaptiveConcurrency(max_parallel=4, current=2)
    # One failure out of six observed tasks: the whole-run ratio is still stale.
    controller.total_tasks = 6
    controller.total_succeeded = 5
    # ... but the last four waves carried no failure at all, and the live
    # observation immediately before the wave-10 decision reported streak 4.
    controller.success_streak = 4
    controller.wave_history = [(1, 0), (1, 0), (1, 0), (1, 0)]
    return controller


def test_zero_failure_window_keeps_a_two_worker_frontier():
    """(a) A long-ago failure must not keep serializing a recovered frontier."""
    controller = _controller_like_wave_10()
    assert controller._failure_rate() > 0.1  # the signal that used to be used
    assert controller._recent_failure_rate() == 0.0

    selected = controller.choose(2, wave_id="wave-10")

    assert selected == 2
    decision = controller.last_decision()
    assert decision["selected"] == 2
    assert decision["base_before_penalty"] == 2
    assert decision["penalties"] == []
    assert "ramp_up_after_success" in decision["reason"]


def test_mild_pressure_never_collapses_a_two_worker_wave():
    """A clean *current* wave keeps the floor even at the mildest penalty."""
    controller = AdaptiveConcurrency(max_parallel=4, current=2)
    controller.wave_history = [(1, 0), (1, 0), (1, 0), (4, 1)]  # 1/7 = 0.143

    assert controller.choose(2, wave_id="wave-11") == 2
    decision = controller.last_decision()
    assert decision["base_before_penalty"] == 2
    assert decision["penalties"][0].startswith("recent_failure_backoff")
    # The helper documents the removed cliff: int(2 * 0.9) == 1.
    assert AdaptiveConcurrency._backoff(2, 0.9, floor=1) == 1
    assert AdaptiveConcurrency._backoff(2, 0.9, floor=2) == 2


def test_high_recent_failure_rate_backs_off_to_one():
    """(b) Genuine recent failures still back off to a single worker."""
    controller = AdaptiveConcurrency(max_parallel=4, current=2)
    controller.wave_history = [(2, 1), (2, 2), (2, 1), (1, 1)]  # 5/7 = 0.714

    selected = controller.choose(4, wave_id="wave-12")

    assert selected == 1
    decision = controller.last_decision()
    assert decision["base_before_penalty"] == 2
    assert decision["penalties"][0].startswith("recent_failure_backoff")


def test_sustained_recent_failures_never_ramp():
    """(c) A failing streak keeps shrinking instead of ramping forever."""
    controller = AdaptiveConcurrency(max_parallel=4, current=4)
    for index in range(6):
        controller.observe(
            total=2,
            succeeded=0,
            elapsed_seconds=10.0,
            wave_id=f"failing-{index}",
            actual_parallel=4,
        )

    assert controller.current == 1
    assert controller.success_streak == 0
    assert controller._recent_failure_rate() == 1.0
    assert controller.choose(4, wave_id="wave-next") == 1


def test_hard_caps_still_bound_the_wave():
    """(d) max_parallel, provider slots and ready_count stay hard upper bounds."""
    controller = AdaptiveConcurrency(max_parallel=3, current=3)
    controller.wave_history = [(4, 0)]

    assert controller.choose(8, wave_id="wave-1") == 3  # max_parallel

    controller.update_provider_slots(max_slots=8, occupied=6)
    assert controller.choose(8, wave_id="wave-2") == 2  # provider_available
    assert controller.last_decision()["provider_slots_available"] == 2

    controller.update_provider_slots(max_slots=8, occupied=0)
    assert controller.choose(2, wave_id="wave-3") == 2  # ready_count

    controller.update_provider_slots(max_slots=2, occupied=0)
    assert controller.choose(8, wave_id="wave-4") == 2  # provider_available


def test_ready_count_cap_is_named_in_the_reason():
    """A cap that actually bit is named, instead of a ramp-up that did not happen."""
    controller = AdaptiveConcurrency(max_parallel=4, current=4)
    controller.wave_history = [(1, 0)]

    assert controller.choose(1, wave_id="wave-5") == 1
    assert "ready_count_limit(1)" in controller.last_decision()["reason"]


def test_decision_event_fields_match_the_computed_decision():
    """(e) The emitted decision fields describe the width that was chosen."""
    controller = AdaptiveConcurrency(max_parallel=4, current=2)
    controller.wave_history = [(2, 1), (2, 1)]  # 0.5 recent failure rate

    selected = controller.choose(2, wave_id="wave-9")
    audit = controller.last_decision()
    payload = controller.decision_event_fields()

    assert selected == 1
    assert payload["base_before_penalty"] == 2
    assert payload["penalties"] == audit["penalties"] == ["recent_failure_backoff(0.50)"]
    assert payload["audit_reason"] == audit["reason"]
    assert "recent_failure_backoff(0.50)" in payload["audit_reason"]
    assert "ramp_up_after_success" not in payload["audit_reason"]
    assert payload["provider_slots_available"] == 10
    assert payload["recent_failure_rate"] == 0.5


def test_clean_window_decision_fields_report_the_ramp_it_made():
    """The same fields stay truthful when no penalty was applied."""
    controller = _controller_like_wave_10()

    assert controller.choose(2, wave_id="wave-10") == 2
    payload = controller.decision_event_fields()

    assert payload["penalties"] == []
    assert payload["recent_failure_rate"] == 0.0
    assert payload["audit_reason"] == "ramp_up_after_success"


def test_run_wide_ratio_is_the_fallback_when_no_wave_was_observed():
    """Pre-loaded counters (no wave history) keep their previous behaviour."""
    controller = AdaptiveConcurrency(max_parallel=10, current=5)
    controller.total_tasks = 100
    controller.total_succeeded = 60  # 40% run-wide failure rate

    assert controller.choose(10, wave_id="wave-1") < 5
    decision = controller.last_decision()
    assert decision["penalties"][0].startswith("run_failure_backoff")
    assert decision["recent_failure_rate"] is None


def test_window_keeps_only_the_recent_waves():
    controller = AdaptiveConcurrency(max_parallel=4)

    for index in range(6):
        controller.observe(total=1, succeeded=1, elapsed_seconds=1.0, wave_id=f"w{index}")

    assert len(controller.wave_history) == FAILURE_WINDOW_WAVES == 4


def test_observation_reports_window_rate_next_to_run_rate():
    controller = AdaptiveConcurrency(max_parallel=4, current=2)

    result = controller.observe(total=2, succeeded=1, elapsed_seconds=5.0, wave_id="w1")

    assert result["failure_rate"] == 0.5
    assert result["recent_failure_rate"] == 0.5
    assert result["next_selected"] == 1  # a failure still shrinks the next wave


def test_cold_start_decision_is_unpenalized_and_truthful():
    controller = AdaptiveConcurrency(max_parallel=4)

    assert controller.choose(8, wave_id="wave-1") == 4
    payload = controller.decision_event_fields()

    assert payload["base_before_penalty"] == 4
    assert payload["penalties"] == []
    assert payload["recent_failure_rate"] is None


# (total, succeeded, elapsed_seconds) of the six ready_wave.concurrency_observed
# records that preceded the live wave-10 decision, in order.
LIVE_WAVE_SEQUENCE = [
    (2, 2, 307.328),
    (2, 1, 1296.938),
    (1, 1, 766.063),
    (1, 1, 1711.985),
    (1, 1, 1305.359),
    (1, 1, 524.328),
]


def test_live_wave_10_sequence_keeps_the_recovered_frontier_parallel():
    """Replay the recorded r31 waves; the wave-10 decision must stay at two.

    Intermediate widths differ from the recorded ones because this replay always
    offers two ready nodes, while the live run only had one ready node until the
    governor recovered E-701 + N-201.  The wave-10 inputs are the recorded ones:
    node_count=2, max_parallel=4, whole-run failure rate 0.125 (one failure five
    waves earlier), which the old signal turned into ``selected_parallel=1``.
    """
    controller = AdaptiveConcurrency(max_parallel=4)
    for index, (total, succeeded, elapsed) in enumerate(LIVE_WAVE_SEQUENCE, start=1):
        selected = controller.choose(2, wave_id=f"wave-{index}")
        controller.observe(
            total=total,
            succeeded=succeeded,
            elapsed_seconds=elapsed,
            wave_id=f"wave-{index}",
            actual_parallel=selected,
        )

    assert controller._failure_rate() == 0.125  # the stale signal is still present
    assert controller._recent_failure_rate() == 0.0

    assert controller.choose(2, wave_id="wave-10") == 2
    decision = controller.last_decision()
    assert decision["penalties"] == []
    assert decision["base_before_penalty"] >= 2
    # Two ready nodes is a cap, not a backoff, and the reason now says so.
    assert "ready_count_limit(2)" in decision["reason"]