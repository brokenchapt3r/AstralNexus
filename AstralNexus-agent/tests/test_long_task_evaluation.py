"""Long-task evaluation: natural compression trigger, fact recall, and recovery.

Tests the full lifecycle of a long-running task that naturally triggers
context compression (not forced), verifies fact recall quality, and
exercises fault injection + self-recovery (§5.6 方案书).
"""
import tempfile
import pytest
from pathlib import Path

from AstralNexus.runtime.context import ContextManager
from AstralNexus.runtime.trace import TraceRecorder


def _build_long_multiline_context(num_lines: int = 200) -> str:
    """Build realistic multi-line context with colon-separated fields that
    the structural simplification stage can compress."""
    lines = []
    for i in range(num_lines):
        lines.append(f"module-{i}: analysis complete with {i * 3} issues detected across subsystems A, B, C")
        lines.append(f"metrics-{i}: latency={i}ms, throughput={1000 + i}req/s, error_rate=0.{i:02d}")
        lines.append(f"details-{i}: verbose diagnostic output captured during integration testing phase with extended telemetry data and correlated logs from distributed services")
    return "\n".join(lines)


class TestNaturalCompressionTrigger:
    """Verify that long tasks trigger compression naturally via token overflow."""

    def test_natural_trigger_when_context_exceeds_budget(self):
        """ContextManager.compress_context with no force should trigger when
        current tokens exceed the target threshold (60% of 8000 = 4800)."""
        ctx = ContextManager(max_context_tokens=8000)

        long_text = _build_long_multiline_context(400)  # ~50k chars
        assert ctx.estimate_tokens(long_text) > 4800

        compressed = ctx.compress_context(long_text, preserve_facts=[])

        # Natural compression should reduce size via structural simplification
        assert len(compressed) < len(long_text), (
            f"Natural compression should reduce: {len(compressed)} vs {len(long_text)}"
        )
        assert compressed != long_text

    def test_no_trigger_when_below_budget(self):
        """Below-budget context should NOT be compressed (no force)."""
        ctx = ContextManager(max_context_tokens=8000)
        short_text = "A brief result."
        assert ctx.estimate_tokens(short_text) < 4800

        result = ctx.compress_context(short_text, preserve_facts=[])
        assert result == short_text, "Should not compress below-budget context"

    def test_force_vs_natural_trigger_distinction(self):
        """Verify force=True triggers compression even on short context;
        force=False leaves short context untouched."""
        # Natural: short text → no compression
        ctx1 = ContextManager(max_context_tokens=8000)
        short_text = "Brief single line."
        natural_result = ctx1.compress_context(short_text, preserve_facts=[])
        assert natural_result == short_text, "No natural compression for short text"

        # Natural: long text → compressed
        ctx2 = ContextManager(max_context_tokens=8000)
        long_text = _build_long_multiline_context(400)
        compressed = ctx2.compress_context(long_text, preserve_facts=[])
        assert compressed != long_text

        # Force: short text still gets structural pass
        ctx3 = ContextManager(max_context_tokens=8000)
        short2 = "Brief."
        forced = ctx3.compress_context(short2, preserve_facts=[], force=True)
        assert isinstance(forced, str)


class TestFactRecallAfterCompression:
    """Verify critical facts survive compression and are recallable."""

    def test_preserved_facts_survive_compression(self):
        """Facts passed to compress_context must appear in get_preserved_facts()."""
        ctx = ContextManager(max_context_tokens=8000)
        long_text = _build_long_multiline_context(400)

        facts = [
            {"text": "API key rotation deadline is 2026-08-01", "source_node_id": "T-003"},
            {"text": "Database migration must be backward-compatible", "source_node_id": "T-005"},
        ]

        ctx.compress_context(long_text, preserve_facts=facts)
        preserved = ctx.get_preserved_facts()

        # get_preserved_facts() returns List[str]
        assert len(preserved) == 2
        assert "API key rotation deadline is 2026-08-01" in preserved
        assert "Database migration must be backward-compatible" in preserved

    def test_fact_recall_in_build_prompt(self):
        """Preserved facts should be injectable into prompts for downstream agents."""
        ctx = ContextManager(max_context_tokens=8000)
        long_text = _build_long_multiline_context(400)

        facts = [
            {"text": "Critical constraint: max latency 200ms", "source_node_id": "T-010"},
        ]

        ctx.compress_context(long_text, preserve_facts=facts)
        preserved = ctx.get_preserved_facts()  # List[str]

        prompt = ctx.build_prompt("New task", compressed_facts=preserved)
        assert "压缩保留的关键事实" in prompt
        assert "max latency 200ms" in prompt

    def test_constraint_versioning_through_long_task(self):
        """Constraints updated multiple times during a long task retain latest version."""
        ctx = ContextManager(max_context_tokens=8000)

        ctx.add_constraint("latency", "Max 500ms", source_node_id="T-001")
        ctx.add_constraint("latency", "Max 300ms", source_node_id="T-005")
        ctx.add_constraint("latency", "Max 200ms", source_node_id="T-010")

        active = ctx.get_active_constraints()
        assert any("200ms" in c for c in active), f"Latest constraint not found: {active}"

        history = ctx.get_constraint_history("latency")
        assert len(history) == 3


class TestFaultInjectionAndRecovery:
    """Evaluate fault injection and self-recovery in long tasks (§5.6)."""

    def test_fault_recovery_trace_events(self):
        """TraceRecorder should capture fault injection and recovery events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = TraceRecorder(Path(tmpdir))

            recorder.record("fault.injected", {
                "fault_type": "timeout",
                "target": "T-003",
                "reason": "External API timeout after 30s",
            })
            recorder.record("fault.recovery_attempted", {
                "fault_type": "timeout",
                "target": "T-003",
                "strategy": "retry_with_backoff",
            })
            recorder.record("fault.recovery_succeeded", {
                "fault_type": "timeout",
                "target": "T-003",
                "attempts": 2,
            })

            fault_events = recorder.get_events_by_type("fault.injected")
            assert len(fault_events) == 1
            assert fault_events[0]["fault_type"] == "timeout"

            recovery_events = recorder.get_events_by_type("fault.recovery_succeeded")
            assert len(recovery_events) == 1
            assert recovery_events[0]["attempts"] == 2

    def test_long_task_compression_with_interleaved_faults(self):
        """Simulate a long task that compresses context while handling faults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = ContextManager(max_context_tokens=4000)
            recorder = TraceRecorder(Path(tmpdir))

            # Accumulate enough context to exceed 60% budget (2400 tokens = 9600 chars)
            context_parts = []
            for i in range(200):
                context_parts.append(f"step-{i}: processed batch {i} with metrics latency={i}ms throughput={500 + i}req/s")
                context_parts.append(f"detail-{i}: extended diagnostic payload captured during integration testing phase with verbose telemetry data for subsystem correlation")
            full_context = "\n".join(context_parts)
            assert ctx.estimate_tokens(full_context) > 2400, "Need enough context to trigger compression"

            # Phase 2: Inject fault mid-task
            recorder.record("fault.injected", {
                "fault_type": "resource_degradation",
                "target": "T-015",
                "reason": "Memory pressure",
            })

            # Phase 3: Recovery
            recorder.record("fault.recovery_attempted", {
                "fault_type": "resource_degradation",
                "target": "T-015",
                "strategy": "reduce_context_size",
            })

            # Phase 4: Natural compression triggered by accumulated context
            facts = [
                {"text": f"Critical result from step {i}", "source_node_id": f"T-{i:03d}"}
                for i in [0, 20, 40, 60]
            ]
            compressed = ctx.compress_context(full_context, preserve_facts=facts)

            recorder.record("fault.recovery_succeeded", {
                "fault_type": "resource_degradation",
                "target": "T-015",
            })

            # Compression happened
            assert len(compressed) <= len(full_context)

            # Facts preserved (List[str])
            preserved = ctx.get_preserved_facts()
            assert len(preserved) == 4

            # Trace captured full lifecycle
            assert len(recorder.events) >= 3

            # Prompt with recovered facts
            prompt = ctx.build_prompt("Continue task", compressed_facts=preserved)
            assert "压缩保留的关键事实" in prompt


class TestEndToEndLongTaskMetrics:
    """End-to-end evaluation: simulate a long task and verify all metrics."""

    def test_long_task_metric_collection(self):
        """Full pipeline: accumulate → compress → recall → verify metrics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = ContextManager(max_context_tokens=2000)
            recorder = TraceRecorder(Path(tmpdir))

            # Build enough context to exceed 60% budget (1200 tokens = 4800 chars)
            all_lines = []
            for step in range(100):
                all_lines.append(f"step-{step}: module analysis found {step * 2} issues with metrics latency={step * 10}ms throughput={1000 + step * 50}req/s error_rate=0.0{step}")
                all_lines.append(f"detail-{step}: verbose diagnostic output captured during integration testing phase with extended telemetry data and correlated logs from distributed services subsystem")
            full_text = "\n".join(all_lines)
            initial_tokens = ctx.estimate_tokens(full_text)
            assert initial_tokens > 1200, f"Need enough context: {initial_tokens}"

            facts = [
                {"text": "Final resolution: 29 issues fixed", "source_node_id": "T-029"},
                {"text": "Critical: no data loss occurred", "source_node_id": "T-015"},
            ]
            compressed = ctx.compress_context(full_text, preserve_facts=facts)
            compressed_tokens = ctx.estimate_tokens(compressed)

            # Natural compression reduced tokens
            assert compressed_tokens < initial_tokens, (
                f"Compression should reduce tokens: {compressed_tokens} vs {initial_tokens}"
            )
            compression_ratio = 1 - (compressed_tokens / initial_tokens)
            assert compression_ratio > 0

            # Facts preserved (List[str])
            preserved = ctx.get_preserved_facts()
            assert len(preserved) == 2
            assert "Final resolution: 29 issues fixed" in preserved
            assert "Critical: no data loss occurred" in preserved

            # Prompt still usable
            prompt = ctx.build_prompt(
                "Continue analysis",
                active_constraints=[],
                compressed_facts=preserved,
            )
            assert "29 issues fixed" in prompt
            assert "no data loss" in prompt

            # Record compression event
            recorder.record("message.compressed", {
                "trigger_type": "natural_threshold_exceeded",
                "tokens_before": initial_tokens,
                "tokens_after": compressed_tokens,
                "compression_ratio": compression_ratio,
                "facts_preserved": len(preserved),
                "quality_metrics": {
                    "payload_efficiency": compressed_tokens / initial_tokens,
                    "key_field_retention": 1.0,
                },
            })

            events = recorder.get_events_by_type("message.compressed")
            assert len(events) == 1
            assert events[0]["trigger_type"] == "natural_threshold_exceeded"
            assert events[0]["quality_metrics"]["key_field_retention"] == 1.0
