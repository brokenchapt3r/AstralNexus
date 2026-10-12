"""Phase-1 tool-result normalization (doc 17 第一批): programmatic judgment
before text heuristics; source attribution so a 403 from the application
under test is never confused with a provider auth failure."""
from __future__ import annotations

from AstralNexus.runtime.tool_outcome import ToolOutcome, normalize_tool_outcome


def test_successful_probe_403_stdout_is_data_not_auth_failure():
    """The measured 403 of a SUCCESSFUL probe is expected-permission evidence."""
    outcome = normalize_tool_outcome({
        "status": "success",
        "returncode": 0,
        "stdout": 'HTTP/1.1 403 Forbidden\n{"error": "permission denied"}',
    }, tool_name="terminal_exec")
    assert outcome.status == "success"
    assert outcome.error_type is None
    assert outcome.source == "tool_return"


def test_provider_auth_failure_classifies_from_error_channel():
    outcome = normalize_tool_outcome({"status": "error", "error": "403 invalid api key"})
    assert outcome == ToolOutcome("error", "auth", False, "stop", "tool_return")


def test_exception_classification_wins_with_exception_source():
    outcome = normalize_tool_outcome(
        {"status": "success"}, exception=TimeoutError("connection timed out")
    )
    assert outcome.status == "error"
    assert outcome.error_type == "transient"
    assert outcome.recommended_next_action == "try_alternative"
    assert outcome.source == "exception"


def test_write_scope_violation_is_deterministic_noncompliance_not_retryable():
    """fix-21 doctrine: ownership rejections get documented, not retried."""
    outcome = normalize_tool_outcome({
        "status": "error",
        "error": "file_write path is outside the Worker owned_paths contract",
        "failure_class": "write_scope_violation",
    })
    assert outcome.error_type == "permission"
    assert outcome.recoverable_by_model is False
    assert outcome.recommended_next_action == "summarize"
    assert outcome.source == "guard"


def test_guard_blocked_call_gets_guard_outcome():
    outcome = normalize_tool_outcome({
        "status": "error",
        "error": "tool loop guard halted repeated call",
        "guard_reason": "repeat_detected",
    })
    assert outcome.source == "guard"
    assert outcome.recoverable_by_model is False
    assert outcome.recommended_next_action == "stop"


def test_approval_pending_is_permission_stop():
    outcome = normalize_tool_outcome({"status": "approval_pending"})
    assert outcome == ToolOutcome("error", "permission", False, "stop", "guard")


def test_numeric_codes_use_word_boundaries():
    """Durations like "took 500ms" must not classify as internal errors."""
    outcome = normalize_tool_outcome({"status": "error", "error": "query took 500ms"})
    assert outcome.error_type != "internal"
    outcome2 = normalize_tool_outcome({"status": "error", "stderr": "server replied 500"})
    assert outcome2.error_type == "internal"


def test_partial_markers_downgrade_success_output():
    outcome = normalize_tool_outcome({"status": "success", "output": "partial results: 3 of 10"})
    assert outcome.status == "partial_success"
    assert outcome.source == "content_analysis"


def test_plain_success_is_tool_return_continue():
    outcome = normalize_tool_outcome({"status": "success", "path": "app.py", "stdout": "written"})
    assert outcome == ToolOutcome("success", None, True, "continue", "tool_return")
