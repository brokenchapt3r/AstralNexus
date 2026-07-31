"""Adaptive ready-wave concurrency control.

The controller keeps the existing ready-wave executor, but turns its worker
count into an observable decision based on wave size and recent outcomes.
It deliberately stays deterministic: the Governor may replace this decision
with a stronger model decision, while this class provides a safe local floor.

Enhanced to integrate with:
- Provider concurrent slots
- Real-time latency and failure rates
- Budget and task value
- Complete audit trail (reasons, slots, release times, parallel windows)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ConcurrencyAudit:
    """Complete audit record for a concurrency decision."""
    timestamp: float
    wave_id: str
    selected: int
    ready_count: int
    provider_slots_available: int
    provider_slots_occupied: int
    failure_rate: float
    latency_p95: float
    budget_remaining_ratio: float
    task_value_score: float
    reason: str
    parallel_window_start: Optional[float] = None
    parallel_window_end: Optional[float] = None
    actual_parallel: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "wave_id": self.wave_id,
            "selected": self.selected,
            "ready_count": self.ready_count,
            "provider_slots_available": self.provider_slots_available,
            "provider_slots_occupied": self.provider_slots_occupied,
            "failure_rate": round(self.failure_rate, 4),
            "latency_p95": round(self.latency_p95, 4),
            "budget_remaining_ratio": round(self.budget_remaining_ratio, 4),
            "task_value_score": round(self.task_value_score, 4),
            "reason": self.reason,
            "parallel_window_start": self.parallel_window_start,
            "parallel_window_end": self.parallel_window_end,
            "actual_parallel": self.actual_parallel,
        }


@dataclass
class AdaptiveConcurrency:
    max_parallel: int
    current: int = 1
    success_streak: int = 0
    failure_streak: int = 0
    # Enhanced state
    provider_max_slots: int = 10
    provider_occupied_slots: int = 0
    latency_history: List[float] = field(default_factory=list)
    total_waves: int = 0
    total_tasks: int = 0
    total_succeeded: int = 0
    budget_limit: Optional[float] = None
    budget_used: float = 0.0
    audit_log: List[ConcurrencyAudit] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.max_parallel = max(1, int(self.max_parallel))
        self.current = max(1, min(int(self.current), self.max_parallel))
        self.provider_max_slots = max(1, int(self.provider_max_slots))

    def update_provider_slots(self, max_slots: int, occupied: int) -> None:
        """Update Provider slot availability from runtime."""
        self.provider_max_slots = max(1, int(max_slots))
        self.provider_occupied_slots = max(0, min(int(occupied), self.provider_max_slots))

    def update_budget(self, limit: Optional[float], used: float) -> None:
        """Update budget tracking."""
        self.budget_limit = limit
        self.budget_used = max(0.0, float(used))

    def _failure_rate(self) -> float:
        """Calculate overall failure rate."""
        if self.total_tasks == 0:
            return 0.0
        return 1.0 - (self.total_succeeded / self.total_tasks)

    def _latency_p95(self) -> float:
        """Calculate 95th percentile latency from recent history."""
        if not self.latency_history:
            return 0.0
        sorted_lat = sorted(self.latency_history[-100:])  # Last 100 observations
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def _budget_remaining_ratio(self) -> float:
        """Calculate remaining budget ratio."""
        if self.budget_limit is None or self.budget_limit == 0:
            return 1.0
        return max(0.0, 1.0 - (self.budget_used / self.budget_limit))

    def choose(
        self,
        ready_count: int,
        task_value_score: float = 0.5,
        wave_id: str = "",
    ) -> int:
        """Choose a bounded concurrency level for the next ready-wave.
        
        Integrates Provider slots, latency, failure rate, budget, and task value.
        """
        ready_count = max(0, int(ready_count))
        if ready_count == 0:
            return 1

        # Constraint 1: Provider slot availability
        provider_available = max(0, self.provider_max_slots - self.provider_occupied_slots)

        # A fresh controller has no evidence that warrants serial execution.
        # Start at the bounded ready-wave width, then let observed latency and
        # failures back off later waves. Serial cold start made a four-node
        # Ready-Wave execute one node at a time and prevented the system from
        # ever demonstrating parallel collaboration.
        if (
            self.total_waves == 0
            and self.total_tasks == 0
            and not self.latency_history
            and self.current == 1
        ):
            base = min(self.max_parallel, ready_count, provider_available or 1)
        else:
            base = self.current
        
        # Constraint 2: Failure rate penalty
        failure_rate = self._failure_rate()
        if failure_rate > 0.3:
            base = max(1, int(base * 0.7))
        elif failure_rate > 0.1:
            base = max(1, int(base * 0.9))

        # Latency alone is not pressure. Serializing independent slow provider
        # calls makes the wall-clock time worse. Back off only when latency is
        # accompanied by failures; provider slots remain the hard rate limit.
        latency_p95 = self._latency_p95()
        if latency_p95 > 60.0 and failure_rate > 0.1:
            base = max(1, int(base * 0.8))
        elif latency_p95 > 30.0 and failure_rate > 0.1:
            base = max(1, int(base * 0.9))

        # Constraint 4: Budget pressure
        budget_ratio = self._budget_remaining_ratio()
        if budget_ratio < 0.2:
            base = max(1, int(base * 0.6))
        elif budget_ratio < 0.5:
            base = max(1, int(base * 0.8))

        # Constraint 5: Task value boost (high-value tasks get more concurrency)
        if task_value_score > 0.8 and budget_ratio > 0.5:
            base = min(self.max_parallel, int(base * 1.1))

        # Final selection: min of all constraints
        selected = max(1, min(self.max_parallel, base, ready_count, provider_available or 1))
        # Persist the actual selected wave width. Previously a cold-start wave
        # selected four workers while ``current`` remained one; the next fully
        # successful wave therefore collapsed to two and then one under a
        # latency penalty.
        self.current = selected

        # Determine reason
        reason = self._determine_reason(
            base, selected, provider_available, failure_rate, 
            latency_p95, budget_ratio, task_value_score
        )

        # Record audit
        self._record_audit(
            wave_id=wave_id,
            selected=selected,
            ready_count=ready_count,
            provider_available=provider_available,
            failure_rate=failure_rate,
            latency_p95=latency_p95,
            budget_ratio=budget_ratio,
            task_value_score=task_value_score,
            reason=reason,
        )

        return selected

    def _determine_reason(
        self,
        base: int,
        selected: int,
        provider_available: int,
        failure_rate: float,
        latency_p95: float,
        budget_ratio: float,
        task_value_score: float,
    ) -> str:
        """Determine the primary reason for the concurrency decision."""
        reasons = []
        
        if selected < base:
            if provider_available < base:
                reasons.append(f"provider_slot_limit({provider_available})")
            if failure_rate > 0.1:
                reasons.append(f"high_failure_rate({failure_rate:.2f})")
            if latency_p95 > 30.0:
                reasons.append(f"high_latency({latency_p95:.1f}s)")
            if budget_ratio < 0.5:
                reasons.append(f"budget_pressure({budget_ratio:.2f})")
        
        if selected > base and task_value_score > 0.8:
            reasons.append(f"high_value_task({task_value_score:.2f})")
        
        if not reasons:
            if self.success_streak > 0:
                reasons.append("ramp_up_after_success")
            else:
                reasons.append("stable")
        
        return "; ".join(reasons)

    def _record_audit(
        self,
        wave_id: str,
        selected: int,
        ready_count: int,
        provider_available: int,
        failure_rate: float,
        latency_p95: float,
        budget_ratio: float,
        task_value_score: float,
        reason: str,
    ) -> None:
        """Record audit entry for this decision."""
        audit = ConcurrencyAudit(
            timestamp=time.time(),
            wave_id=wave_id,
            selected=selected,
            ready_count=ready_count,
            provider_slots_available=provider_available,
            provider_slots_occupied=self.provider_occupied_slots,
            failure_rate=failure_rate,
            latency_p95=latency_p95,
            budget_remaining_ratio=budget_ratio,
            task_value_score=task_value_score,
            reason=reason,
        )
        self.audit_log.append(audit)

    def observe(
        self,
        *,
        total: int,
        succeeded: int,
        elapsed_seconds: float,
        wave_id: str = "",
        actual_parallel: int = 0,
    ) -> Dict[str, Any]:
        """Update the controller after a wave and return an audit decision."""
        total = max(0, int(total))
        succeeded = max(0, min(total, int(succeeded)))
        failed = total - succeeded

        # ``selected`` in the observation is the width that was actually
        # submitted for this wave, not the next-wave recommendation.  The
        # controller may ramp ``current`` after observing success, but that
        # future decision must not corrupt the evidence for the completed wave.
        observed_parallel = max(0, int(actual_parallel or 0))
        if observed_parallel <= 0:
            observed_parallel = min(self.max_parallel, total) if total else 0

        # Update global stats
        self.total_waves += 1
        self.total_tasks += total
        self.total_succeeded += succeeded

        # Update latency history
        if total > 0 and elapsed_seconds > 0:
            avg_latency = elapsed_seconds / total
            self.latency_history.append(avg_latency)
            # Keep only last 1000 observations
            if len(self.latency_history) > 1000:
                self.latency_history = self.latency_history[-1000:]

        # Adjust concurrency based on success/failure
        if failed == 0 and total:
            self.success_streak += 1
            self.failure_streak = 0
            if self.success_streak >= 1:
                self.current = min(self.max_parallel, self.current + 1)
        elif failed:
            self.failure_streak += 1
            self.success_streak = 0
            if self.failure_streak >= 1:
                self.current = max(1, self.current - 1)

        # Update last audit entry with actual parallel window
        if self.audit_log and self.audit_log[-1].wave_id == wave_id:
            self.audit_log[-1].parallel_window_start = time.time() - elapsed_seconds
            self.audit_log[-1].parallel_window_end = time.time()
            self.audit_log[-1].actual_parallel = actual_parallel

        return {
            "selected": observed_parallel,
            "next_selected": self.current,
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 4),
            "success_streak": self.success_streak,
            "failure_streak": self.failure_streak,
            "failure_rate": round(self._failure_rate(), 4),
            "latency_p95": round(self._latency_p95(), 4),
            "budget_remaining_ratio": round(self._budget_remaining_ratio(), 4),
            "provider_slots_occupied": self.provider_occupied_slots,
            "reason": "ramp_up_after_success" if failed == 0 and total else "backoff_after_failure",
        }

    def get_audit_summary(self, last_n: int = 10) -> Dict[str, Any]:
        """Get summary of recent concurrency decisions."""
        recent = self.audit_log[-last_n:] if self.audit_log else []
        
        if not recent:
            return {
                "total_decisions": 0,
                "avg_selected": 0,
                "avg_failure_rate": 0,
                "avg_latency_p95": 0,
                "common_reasons": [],
            }
        
        return {
            "total_decisions": len(recent),
            "avg_selected": round(sum(a.selected for a in recent) / len(recent), 2),
            "avg_failure_rate": round(sum(a.failure_rate for a in recent) / len(recent), 4),
            "avg_latency_p95": round(sum(a.latency_p95 for a in recent) / len(recent), 4),
            "common_reasons": self._extract_common_reasons(recent),
        }

    def _extract_common_reasons(self, audits: List[ConcurrencyAudit]) -> List[Dict[str, int]]:
        """Extract common reason patterns from audit log."""
        reason_counts: Dict[str, int] = {}
        for audit in audits:
            for part in audit.reason.split("; "):
                key = part.split("(")[0]  # Extract reason type
                reason_counts[key] = reason_counts.get(key, 0) + 1
        
        return sorted(
            [{"reason": k, "count": v} for k, v in reason_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:5]
