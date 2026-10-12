"""Fail-closed filesystem and observability boundaries.

Behavioral cases are adapted from DeerFlow's MIT-licensed security tests and
rewritten against AstralNexus interfaces.
"""
from __future__ import annotations

import os

import pytest

from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
from AstralNexus.runtime.event_bus import RuntimeEventBus


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.txt",
        "../../outside.txt",
        "subdir/../../../outside.txt",
    ],
)
def test_acceptance_file_checks_reject_parent_traversal(tmp_path, unsafe_path):
    observation = run_acceptance_checks(
        [f"file:{unsafe_path} exists"], receipts=[], root=tmp_path
    )[0]

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"


def test_acceptance_file_check_rejects_an_absolute_path(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    observation = run_acceptance_checks(
        [f"file:{outside} exists"], receipts=[], root=tmp_path
    )[0]

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"


def test_acceptance_file_check_rejects_directories(tmp_path):
    (tmp_path / "report.md").mkdir()

    observation = run_acceptance_checks(
        ["file:report.md exists"], receipts=[], root=tmp_path
    )[0]

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"


def test_acceptance_file_check_follows_symlinks_before_scope_decision(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "report.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    observation = run_acceptance_checks(
        ["file:report.md exists"], receipts=[], root=tmp_path
    )[0]

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"


def test_binary_content_and_error_prefix_are_not_provider_errors(tmp_path):
    (tmp_path / "error.log").write_bytes(b"Error: a real log\n\xff\x00")

    observations = run_acceptance_checks(
        ["file:error.log exists", "file:error.log non-empty"],
        receipts=[],
        root=tmp_path,
    )

    assert all(item["checked"] is True and item["holds"] is True for item in observations)


def test_nested_private_reasoning_is_removed_without_mutating_input(tmp_path):
    bus = RuntimeEventBus(tmp_path)
    source = {
        "activity": {"label": "working", "type": "tool"},
        "details": {
            "reasoning": "private",
            "safe": [{"chain_of_thought": "private too", "value": 3}],
        },
    }

    event = bus.publish("agent.activity", source)

    assert source["details"]["reasoning"] == "private"
    assert "reasoning" not in event["payload"]["details"]
    assert event["payload"]["details"]["safe"] == [{"value": 3}]


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate path spelling")
def test_backslash_parent_traversal_is_rejected_on_windows(tmp_path):
    observation = run_acceptance_checks(
        [r"file:..\outside.txt exists"], receipts=[], root=tmp_path
    )[0]

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"

