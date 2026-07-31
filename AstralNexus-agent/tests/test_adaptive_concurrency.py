from AstralNexus.core.adaptive_concurrency import AdaptiveConcurrency


def test_adaptive_concurrency_uses_ready_wave_width_on_cold_start():
    controller = AdaptiveConcurrency(max_parallel=4)
    assert controller.choose(8) == 4
    decision = controller.observe(total=3, succeeded=3, elapsed_seconds=1.2)
    assert decision["reason"] == "ramp_up_after_success"
    assert controller.choose(8) == 4


def test_high_latency_without_failures_does_not_serialize_ready_wave():
    controller = AdaptiveConcurrency(max_parallel=4)
    assert controller.choose(4, wave_id="wave-1") == 4
    controller.observe(
        total=4,
        succeeded=4,
        elapsed_seconds=1600.0,
        wave_id="wave-1",
        actual_parallel=4,
    )
    assert controller.choose(3, wave_id="wave-2") == 3


def test_adaptive_concurrency_backs_off_after_failure():
    controller = AdaptiveConcurrency(max_parallel=4, current=3)
    decision = controller.observe(total=3, succeeded=2, elapsed_seconds=2.0)
    assert decision["reason"] == "backoff_after_failure"
    # After backoff: current=2, but failure_rate=1/3≈33% triggers additional penalty
    selected = controller.choose(8)
    assert selected >= 1
    assert selected <= 2


def test_observation_reports_actual_wave_width_not_next_wave_recommendation():
    controller = AdaptiveConcurrency(max_parallel=4, current=2)
    controller.choose(2, wave_id="wave-1")
    decision = controller.observe(
        total=2,
        succeeded=2,
        elapsed_seconds=1.0,
        wave_id="wave-1",
        actual_parallel=2,
    )
    assert decision["selected"] == 2
    assert decision["next_selected"] == 3
