"""Tests for enhanced adaptive concurrency with Provider/budget/latency integration."""
import time
from AstralNexus.core.adaptive_concurrency import AdaptiveConcurrency, ConcurrencyAudit


class TestEnhancedAdaptiveConcurrency:
    """Test the enhanced adaptive concurrency controller."""

    def test_basic_choose_still_works(self):
        """Backward compatibility: basic choose without enhancements."""
        ac = AdaptiveConcurrency(max_parallel=5, current=3)
        selected = ac.choose(ready_count=10)
        assert selected >= 1
        assert selected <= 5

    def test_choose_respects_provider_slots(self):
        """Provider slot availability limits concurrency."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        ac.update_provider_slots(max_slots=8, occupied=7)  # Only 1 slot available
        
        selected = ac.choose(ready_count=10)
        assert selected == 1  # Limited by provider slots

    def test_choose_penalizes_high_failure_rate(self):
        """High failure rate reduces concurrency."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        
        # Simulate 40% failure rate
        ac.total_tasks = 100
        ac.total_succeeded = 60
        
        selected = ac.choose(ready_count=10)
        # Should be reduced by ~30% due to high failure rate
        assert selected < 5

    def test_choose_keeps_parallelism_for_slow_successful_provider(self):
        """Latency without failures must not serialize independent work."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        
        # Simulate high latency (p95 > 60s)
        ac.latency_history = [65.0, 70.0, 80.0, 75.0, 90.0]
        
        selected = ac.choose(ready_count=10)
        assert selected == 5

    def test_choose_penalizes_low_budget(self):
        """Low budget reduces concurrency."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        ac.update_budget(limit=100.0, used=90.0)  # Only 10% remaining
        
        selected = ac.choose(ready_count=10)
        # Should be reduced by ~40% due to budget pressure
        assert selected < 5

    def test_choose_boosts_high_value_tasks(self):
        """High-value tasks get more concurrency when budget allows."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        ac.update_budget(limit=100.0, used=30.0)  # 70% remaining
        
        selected = ac.choose(ready_count=10, task_value_score=0.9)
        # Should be boosted by ~10% due to high value
        assert selected >= 5

    def test_choose_zero_ready_returns_one(self):
        """Zero ready tasks returns minimum concurrency."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        selected = ac.choose(ready_count=0)
        assert selected == 1

    def test_audit_recorded_on_choose(self):
        """Audit log records every choose() call."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        initial_log_len = len(ac.audit_log)
        
        ac.choose(ready_count=5, wave_id="wave_001")
        
        assert len(ac.audit_log) == initial_log_len + 1
        audit = ac.audit_log[-1]
        assert audit.wave_id == "wave_001"
        assert audit.ready_count == 5

    def test_observe_updates_global_stats(self):
        """observe() updates total_waves, total_tasks, total_succeeded."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        
        result = ac.observe(total=5, succeeded=3, elapsed_seconds=10.0, wave_id="w1")
        
        assert ac.total_waves == 1
        assert ac.total_tasks == 5
        assert ac.total_succeeded == 3
        assert result["failure_rate"] == 0.4

    def test_observe_updates_latency_history(self):
        """observe() appends to latency_history."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        
        ac.observe(total=5, succeeded=5, elapsed_seconds=25.0)
        
        assert len(ac.latency_history) == 1
        assert ac.latency_history[0] == 5.0  # 25s / 5 tasks

    def test_observe_updates_audit_parallel_window(self):
        """observe() updates parallel window in audit log."""
        ac = AdaptiveConcurrency(max_parallel=10, current=3)
        
        ac.choose(ready_count=5, wave_id="wave_123")
        time.sleep(0.1)
        ac.observe(total=3, succeeded=3, elapsed_seconds=5.0, wave_id="wave_123", actual_parallel=2)
        
        audit = ac.audit_log[-1]
        assert audit.parallel_window_start is not None
        assert audit.parallel_window_end is not None
        assert audit.actual_parallel == 2
        assert audit.parallel_window_end > audit.parallel_window_start

    def test_get_audit_summary_empty(self):
        """get_audit_summary() handles empty log."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        summary = ac.get_audit_summary()
        
        assert summary["total_decisions"] == 0
        assert summary["common_reasons"] == []

    def test_get_audit_summary_with_data(self):
        """get_audit_summary() returns aggregated stats."""
        ac = AdaptiveConcurrency(max_parallel=10, current=3)
        
        for i in range(5):
            ac.choose(ready_count=5, wave_id=f"wave_{i}")
        
        summary = ac.get_audit_summary(last_n=5)
        
        assert summary["total_decisions"] == 5
        assert summary["avg_selected"] > 0
        assert len(summary["common_reasons"]) > 0

    def test_latency_p95_calculation(self):
        """_latency_p95() calculates 95th percentile correctly."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        
        # Add 100 observations
        ac.latency_history = list(range(1, 101))  # 1 to 100
        
        p95 = ac._latency_p95()
        # 95th percentile of 1-100 should be around 95
        assert 94 <= p95 <= 96

    def test_budget_remaining_ratio_no_limit(self):
        """_budget_remaining_ratio() returns 1.0 when no limit set."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        
        ratio = ac._budget_remaining_ratio()
        assert ratio == 1.0

    def test_budget_remaining_ratio_with_usage(self):
        """_budget_remaining_ratio() calculates correctly with usage."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        ac.update_budget(limit=100.0, used=30.0)
        
        ratio = ac._budget_remaining_ratio()
        assert ratio == 0.7

    def test_multiple_constraints_applied(self):
        """Multiple constraints are all applied to reduce concurrency."""
        ac = AdaptiveConcurrency(max_parallel=10, current=8)
        
        # Apply multiple constraints
        ac.update_provider_slots(max_slots=10, occupied=5)  # 5 slots available
        ac.total_tasks = 100
        ac.total_succeeded = 80  # 20% failure rate
        ac.latency_history = [35.0, 40.0, 38.0]  # High latency
        ac.update_budget(limit=100.0, used=60.0)  # 40% remaining
        
        selected = ac.choose(ready_count=10)
        
        # Should be significantly reduced from 8
        assert selected < 8
        assert selected >= 1

    def test_concurrency_audit_to_dict(self):
        """ConcurrencyAudit.to_dict() serializes correctly."""
        audit = ConcurrencyAudit(
            timestamp=time.time(),
            wave_id="w1",
            selected=5,
            ready_count=10,
            provider_slots_available=8,
            provider_slots_occupied=2,
            failure_rate=0.15,
            latency_p95=45.5,
            budget_remaining_ratio=0.6,
            task_value_score=0.75,
            reason="stable",
        )
        
        d = audit.to_dict()
        assert d["wave_id"] == "w1"
        assert d["selected"] == 5
        assert d["failure_rate"] == 0.15

    def test_reason_determination_provider_limit(self):
        """_determine_reason() identifies provider slot limit."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        
        reason = ac._determine_reason(
            base=5, selected=3, provider_available=3,
            failure_rate=0.0, latency_p95=10.0,
            budget_ratio=1.0, task_value_score=0.5
        )
        
        assert "provider_slot_limit" in reason

    def test_reason_determination_failure_rate(self):
        """_determine_reason() identifies high failure rate."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        
        reason = ac._determine_reason(
            base=5, selected=4, provider_available=10,
            failure_rate=0.25, latency_p95=10.0,
            budget_ratio=1.0, task_value_score=0.5
        )
        
        assert "high_failure_rate" in reason

    def test_latency_history_truncation(self):
        """observe() truncates latency_history to last 1000 entries."""
        ac = AdaptiveConcurrency(max_parallel=10, current=1)
        
        # Add 1500 observations
        for _ in range(1500):
            ac.observe(total=1, succeeded=1, elapsed_seconds=1.0)
        
        assert len(ac.latency_history) == 1000

    def test_choose_with_all_constraints_balanced(self):
        """Balanced scenario: moderate constraints, reasonable concurrency."""
        ac = AdaptiveConcurrency(max_parallel=10, current=5)
        
        # Moderate constraints
        ac.update_provider_slots(max_slots=10, occupied=3)  # 7 available
        ac.total_tasks = 100
        ac.total_succeeded = 85  # 15% failure rate
        ac.latency_history = [25.0, 28.0, 30.0]  # Moderate latency
        ac.update_budget(limit=100.0, used=40.0)  # 60% remaining
        
        selected = ac.choose(ready_count=10)
        
        # Should be reasonable (not too high, not too low)
        assert 3 <= selected <= 7
