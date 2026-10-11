"""Fix r29 (doc 51 §16): guard x anchored-verification handoff.

Live r29 (2026-10-05/06, node ``N-SERVER-RECOVER-01``): the anchored command
``python server/smoke_check.py --strict`` exited 0 (``passed=36 failed=0``), the
worker then edited the two tracked files the execution contract declares
(``summary.json``, ``fix_log.json``) -- which the framework's own rule makes the
earlier pass STALE -- so the required re-verification was refused by the loop
guard (``tool loop guard halted repeated call``).  That refusal was recorded as a
receipt, and the gate's "last decisive match anchors" rule anchored the criterion
on the refusal: ``TEST_EXECUTION_FAILED`` on a command that had really passed.

The two sides must agree on one identity (``runtime.verification_identity``):
a refused call is not an execution result, and an execution result is only valid
for the premises it was produced under.  The three inversions below are the
required offline treatments:

(a) a tracked-file change voids the old evidence AND the guard admits exactly one
    re-verification of the original command;
(b) an unchanged world is still refused -- the guard is not weakened;
(c) a still-valid receipt is reused (no forced re-run, no false success), and a
    refusal-only history fails explicitly instead of passing or degrading.
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.tool_guardrails import ToolCallGuard
from AstralNexus.runtime.verification_identity import (
    GUARD_BLOCK_ERROR,
    premise_versions,
)

CMD = "python server/smoke_check.py --strict"
TRACKED = "deliverables/app/server/app.py"
PASSING_SUMMARY = "smoke_check: passed=36 failed=0"


def _call(arguments=None):
    """The exact r29 call shape: a constant guard fingerprint.

    ``server/smoke_check.py`` is resolved by the tool relative to ``cwd``
    (``deliverables/app``), which is NOT how the guard resolves referenced files,
    so nothing enters the fingerprint and only the repeat budget can decide.
    """
    return {
        "call_id": "call_smoke",
        "function": {
            "name": "terminal_exec",
            "arguments": arguments if arguments is not None else {
                "command": ["python", "server/smoke_check.py", "--strict"],
                "cwd": "deliverables/app",
                "timeout": 300,
            },
        },
    }


def _guard(tmp_path, path=TRACKED, tool="terminal_exec"):
    tracked = tmp_path / path
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v1", encoding="utf-8")
    guard = ToolCallGuard(
        workspace_root=str(tmp_path),
        premise_provider=lambda: premise_versions(tmp_path, ["deliverables/app/server/"]),
        verification_commands={CMD: "tests_passed:" + CMD},
    )
    return guard, tracked


def _run_attempt(guard, call, *, success=True):
    admitted, key = guard.admit(call)
    if admitted:
        guard.observe(call, {"status": "success" if success else "error", "returncode": 0 if success else 1})
    return admitted, key


def test_tracked_file_change_admits_one_reverification(tmp_path):
    """(a) the R29 re-run: 3 attempts, a tracked file moves, the 4th is allowed."""
    guard, tracked = _guard(tmp_path)
    call = _call()
    for _ in range(3):
        admitted, key = _run_attempt(guard, call)
        assert admitted is True

    tracked.write_text("v2", encoding="utf-8")
    admitted, key = guard.admit(call)
    assert admitted is True, "a re-verification after a tracked-file change must be admitted"

    notices = guard.take_notices()
    assert len(notices) == 1
    notice = notices[0]
    assert notice["reason"] == "premises_changed_since_last_execution"
    assert notice["changed_premises"] == [TRACKED]
    assert notice["criterion"] == "tests_passed:" + CMD
    assert notice["declared_verification"] is True
    assert notice["repeat_count"] == 4
    assert notice["verification_identity"]

    decision = guard.decision(call)
    assert decision["action"] == "retry_or_continue"
    assert decision["reason"] == "reverification_admitted"
    assert guard.snapshot()["reverification_grants"] == {key: 1}


def test_unchanged_world_still_refuses_the_identical_call(tmp_path):
    """(b) no change, no new evidence: the fourth identical call stays blocked."""
    guard, _ = _guard(tmp_path)
    call = _call()
    key = ""
    for _ in range(3):
        admitted, key = _run_attempt(guard, call)
        assert admitted is True

    admitted, reason = guard.admit(call)
    assert admitted is False, "an unchanged repeat with no new evidence must stay blocked"
    assert reason == "repeated_identical_tool_call"
    assert guard.take_notices() == []

    decision = guard.decision(call)
    assert decision["action"] == "halt"
    assert decision["reason"] == "repeated_identical_tool_call"

    # The refusal is the framework's own, so it must not burn the failure budget
    # that decides `switch_tool_or_replan` against the worker.
    guard.observe(call, {"status": "error", "returncode": None, "error": GUARD_BLOCK_ERROR})
    assert guard.snapshot()["guard_block_counts"] == {key: 1}
    assert decision and guard.decision(call)["failure_count"] == 0

    # A single grant per premise change: after the sanctioned re-run is observed,
    # the next identical call is refused again until the premises move once more.
    tracked_v2 = tmp_path / TRACKED
    tracked_v2.write_text("v3", encoding="utf-8")
    admitted, _ = guard.admit(call)
    assert admitted is True
    guard.observe(call, {"status": "success", "returncode": 0})
    admitted, _ = guard.admit(call)
    assert admitted is False


def _receipt(command, *, returncode=0, status="success", stdout_tail="",
             files=None, rid="rcpt", error=""):
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": files or {},
        "receipt_id": rid,
        "error": error,
        "arguments": json.dumps({"command": command}),
    }


def _blocked_receipt(command, rid="blocked"):
    """Exactly what the ledger records for a guard refusal."""
    return _receipt(command, returncode=None, status="error", rid=rid,
                    error=GUARD_BLOCK_ERROR)


def test_valid_receipt_is_reused_without_rerun(tmp_path):
    """(c) the receipt proves the claim; nothing asks the worker to run again."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v1", encoding="utf-8")
    versions = premise_versions(tmp_path, ["deliverables/app/server/"])
    assert versions[TRACKED]
    receipt = _receipt(CMD, stdout_tail=PASSING_SUMMARY, rid="real",
                       files={TRACKED: {"bytes": 2, "sha256": versions[TRACKED]}})

    observations = run_acceptance_checks(
        ["tests_passed:" + CMD],
        receipts=[receipt],
        execution_id="exec-reuse",
        premise_versions=versions,
        declared_premises=[TRACKED],
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation["checked"] is True
    assert observation["holds"] is True
    assert observation["receipt_ids"] == ["real"]
    verification = observation["verification"]
    assert verification["anchor_receipt_ids"] == ["real"]
    assert verification["guard_refused_receipt_ids"] == []
    assert verification["declared_premises"] == [TRACKED]
    assert verification["premise_versions"] == versions
    assert verification["verification_identity"]


def test_refusal_never_anchors_over_a_passing_receipt(tmp_path):
    """(c) the lost association is repaired from the valid receipt itself."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v1", encoding="utf-8")
    versions = premise_versions(tmp_path, ["deliverables/app/server/"])
    files = {TRACKED: {"bytes": 2, "sha256": versions[TRACKED]}}

    observations = run_acceptance_checks(
        ["tests_passed:" + CMD],
        receipts=[
            _receipt(CMD, stdout_tail=PASSING_SUMMARY, rid="real", files=files),
            _blocked_receipt(CMD),
        ],
        execution_id="exec-anchor",
        premise_versions=versions,
        declared_premises=[TRACKED],
    )
    observation = observations[0]
    assert observation["reason"] != "TEST_EXECUTION_FAILED"
    assert observation["checked"] is True
    assert observation["holds"] is True
    assert observation["receipt_ids"] == ["real"], "the refusal must not out-rank the real execution"
    assert observation["verification"]["guard_refused_receipt_ids"] == ["blocked"]


def test_refusal_only_history_fails_explicitly():
    """(c) only refusals: an explicit failure, never a pass and never not-found."""
    observations = run_acceptance_checks(
        ["tests_passed:" + CMD],
        receipts=[_blocked_receipt(CMD)],
        execution_id="exec-blocked",
        declared_premises=[CMD.split()[1]],
    )
    observation = observations[0]
    assert observation["reason"] == "TEST_EXECUTION_BLOCKED"
    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["receipt_ids"] == ["blocked"]
    assert "refused by the tool-loop guard" in observation["detail"]


def test_premise_change_makes_the_recorded_pass_stale(tmp_path):
    """(a) the other half: changed premises void the earlier pass, so the fresh
    re-run (admitted by the guard) is the only way back to holds=true."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v2", encoding="utf-8")
    current = premise_versions(tmp_path, ["deliverables/app/server/"])
    recorded = dict(current)
    recorded[TRACKED] = "0" * 64  # the version the anchored execution saw

    observations = run_acceptance_checks(
        ["tests_passed:" + CMD],
        receipts=[_receipt(CMD, stdout_tail=PASSING_SUMMARY, rid="real",
                           files={TRACKED: {"bytes": 2, "sha256": recorded[TRACKED]}})],
        execution_id="exec-stale",
        premise_versions=current,
        declared_premises=[TRACKED],
    )
    observation = observations[0]
    assert observation["checked"] is True
    assert observation["holds"] is False
    assert observation["reason"] == "TEST_EXECUTION_STALE"
    assert TRACKED in observation["detail"]


def _accept_case(tmp_path, receipts):
    """ExecutionStore.accept() end-to-end: the same plumbing ExecutionStore uses."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    if not tracked.exists():
        tracked.write_text("v1", encoding="utf-8")
    store = ExecutionStore(tmp_path)
    execution = store.begin("N-SERVER-RECOVER-01", {
        "owned_paths": ["deliverables/app/server/"],
        "upstream_artifacts": [],
        "acceptance_criteria": ["tests_passed:" + CMD],
    })
    for receipt in receipts:
        store.append(execution, receipt)
    store.finish(execution, "succeeded", {"owned_files": {}})
    return store, execution


def test_accept_reuses_the_passing_receipt_instead_of_the_refusal(tmp_path):
    """accept() must not fail the criterion on a refusal that out-ranked a real,
    still-valid execution -- the exact live r29 rejection."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v1", encoding="utf-8")
    versions = premise_versions(tmp_path, ["deliverables/app/server/"])
    files = {path: {"sha256": digest, "bytes": 2} for path, digest in versions.items()}
    store, execution = _accept_case(tmp_path, [
        _receipt(CMD, stdout_tail=PASSING_SUMMARY, rid="real", files=files),
        _blocked_receipt(CMD),
    ])
    store.accept(execution, {"approved": True})
    assert store.get(execution)["acceptance"]["approved"] is True


def test_accept_reports_a_refusal_only_history_explicitly(tmp_path):
    """No execution, only refusals: the gate must say so instead of passing or
    pretending the command was never declared."""
    store, execution = _accept_case(tmp_path, [_blocked_receipt(CMD)])
    with pytest.raises(ValueError, match="TEST_EXECUTION_BLOCKED"):
        store.accept(execution, {"approved": True})


def test_accept_treats_a_changed_premise_as_stale(tmp_path):
    """The declared premise set (owned paths, no criterion naming them) decides:
    a tracked file changed after the anchored run voids that run."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v1", encoding="utf-8")
    recorded = {"bytes": 2, "sha256": "0" * 64}
    store, execution = _accept_case(tmp_path, [
        _receipt(CMD, stdout_tail=PASSING_SUMMARY, rid="real",
                 files={TRACKED: recorded}),
        _blocked_receipt(CMD),
    ])
    with pytest.raises(ValueError, match="TEST_EXECUTION_STALE"):
        store.accept(execution, {"approved": True})


def test_receipt_command_variants_still_associate(tmp_path):
    """(c) the recorded receipt keeps its association even when the command text
    differs in shape: the r29 argv list (with ``cwd``) and a whitespace-heavy
    spelling are the same execution, so the gate reuses it instead of reporting
    ``TEST_EXECUTION_NOT_FOUND`` and asking for a pointless re-run."""
    tracked = tmp_path / TRACKED
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("v1", encoding="utf-8")
    versions = premise_versions(tmp_path, ["deliverables/app/server/"])
    files = {TRACKED: {"bytes": 2, "sha256": versions[TRACKED]}}
    variants = (
        ("argv-list", {"command": ["python", "server/smoke_check.py", "--strict"],
                       "cwd": "deliverables/app", "timeout": 300}),
        ("spacey", {"command": "python   server/smoke_check.py    --strict"}),
    )
    for label, arguments in variants:
        receipt = _receipt(CMD, stdout_tail=PASSING_SUMMARY, rid="real", files=files)
        receipt["arguments"] = json.dumps(arguments)
        observation = run_acceptance_checks(
            ["tests_passed:" + CMD],
            receipts=[receipt],
            execution_id="exec-" + label,
            premise_versions=versions,
            declared_premises=[TRACKED],
        )[0]
        assert observation["reason"] != "TEST_EXECUTION_NOT_FOUND", label
        assert observation["holds"] is True, label
        assert observation["receipt_ids"] == ["real"], label