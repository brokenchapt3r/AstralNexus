"""User requirement m02366 ③: a write receipt must carry its own change facts.

Operation, path, before/after hash and bytes, ``changed``, requested-vs-landed
hash, and "a failed write modified nothing" are all decidable from the durable
receipt alone; the requested content itself is never recorded.
"""
import hashlib
import json

from AstralNexus.runtime.execution_ledger import ExecutionLedger
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.file_write_facts import (
    inventory_version,
    relative_receipt_path,
    request_facts,
    write_receipt_facts,
)


def _version(text: str) -> dict:
    encoded = text.encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def test_overwrite_records_request_and_landed_hashes_without_content(tmp_path):
    target = tmp_path / "app.py"
    before = {"app.py": _version("old")}
    after = {"app.py": _version("MARKER42")}
    facts = write_receipt_facts(
        json.dumps({"path": str(target), "content": "MARKER42", "mode": "overwrite"}),
        before,
        after,
        relative_path="app.py",
        path=str(target),
        status="success",
        returncode=0,
    )
    assert facts["operation"] == "overwrite"
    assert facts["changed"] is True
    assert facts["landed_matches_request"] is True
    assert facts["previous_sha256"] == before["app.py"]["sha256"]
    assert facts["landed_sha256"] == after["app.py"]["sha256"]
    assert facts["request_bytes"] == 8
    assert facts["landed_bytes"] == 8
    assert facts["failed_write_modified"] is False
    assert facts["content_recorded"] is False
    assert "MARKER42" not in json.dumps(facts)
    assert "content" not in facts


def test_identical_overwrite_reports_no_change(tmp_path):
    same = {"app.py": _version("same")}
    facts = write_receipt_facts(
        {"path": "app.py", "content": "same"},
        {"app.py": _version("same")},
        same,
        relative_path="app.py",
        status="success",
    )
    assert facts["changed"] is False
    assert facts["landed_matches_request"] is True


def test_append_reports_size_consistency_not_a_landed_match(tmp_path):
    facts = write_receipt_facts(
        {"path": "app.py", "content": "add", "mode": "append", "expected_bytes": 4},
        {"app.py": _version("base")},
        {"app.py": _version("baseadd")},
        relative_path="app.py",
        status="success",
    )
    assert facts["operation"] == "append"
    assert facts["landed_matches_request"] is None
    assert facts["landed_bytes_consistent"] is True
    assert facts["expected_previous_bytes"] == 4

    inconsistent = write_receipt_facts(
        {"path": "app.py", "content": "add", "mode": "append", "expected_bytes": 4},
        {"app.py": _version("base")},
        {"app.py": _version("other")},
        relative_path="app.py",
        status="success",
    )
    assert inconsistent["landed_bytes_consistent"] is False


def test_replace_records_old_text_size_and_expected_version(tmp_path):
    facts = write_receipt_facts(
        {"path": "app.py", "content": "x", "mode": "replace", "old_text": "old-value",
         "expected_sha256": "abc123"},
        {"app.py": _version("before")},
        {"app.py": _version("affter")},
        relative_path="app.py",
        status="success",
    )
    assert facts["operation"] == "replace"
    assert facts["replace_old_text_bytes"] == 9
    assert facts["expected_previous_sha256"] == "abc123"
    assert facts["landed_matches_request"] is None
    assert facts["changed"] is True


def test_failed_write_that_modified_nothing_is_distinguishable():
    unchanged = write_receipt_facts(
        {"path": "app.py", "content": "x", "mode": "replace", "old_text": "missing",
         "expected_sha256": "dead"},
        {"app.py": _version("kept")},
        {"app.py": _version("kept")},
        relative_path="app.py",
        status="error",
        returncode=None,
    )
    assert unchanged["changed"] is False
    assert unchanged["failed_write_modified"] is False

    modified = write_receipt_facts(
        {"path": "app.py", "content": "x"},
        {"app.py": _version("kept")},
        {"app.py": _version("touched")},
        relative_path="app.py",
        status="error",
    )
    assert modified["changed"] is True
    assert modified["failed_write_modified"] is True


def test_unknown_baseline_never_claims_a_change():
    facts = write_receipt_facts(
        {"path": "app.py", "content": "x"},
        {},
        {"app.py": _version("x")},
        relative_path="app.py",
        status="success",
    )
    assert facts["changed"] is None
    assert facts["failed_write_modified"] is False


def test_new_and_removed_paths_are_decidable_against_a_known_baseline():
    added = write_receipt_facts(
        {"path": "new.py", "content": "x"},
        {"app.py": _version("kept")},
        {"app.py": _version("kept"), "new.py": _version("x")},
        relative_path="new.py",
        status="success",
    )
    assert added["change_kind"] == "added"
    assert added["changed"] is True
    assert added["previous_sha256"] == ""
    assert added["landed_bytes"] == 1

    removed = write_receipt_facts(
        {"path": "gone.py", "content": ""},
        {"gone.py": _version("x")},
        {"app.py": _version("kept")},
        relative_path="gone.py",
        status="success",
    )
    assert removed["change_kind"] == "removed"
    assert removed["changed"] is True
    assert removed["landed_sha256"] == ""


def test_unparsable_arguments_do_not_raise_or_leak_content():
    facts = write_receipt_facts("not json at all", {}, {}, status="error")
    assert facts["parsable"] is False
    assert facts["operation"] == "unknown"
    assert facts["changed"] is None
    assert "not json at all" not in json.dumps(facts)
    assert request_facts(None)["parsable"] is False
    assert request_facts({"path": "a.py", "content": "MARKER42"})["content_recorded"] is False


def test_inventory_and_relative_path_helpers(tmp_path):
    assert inventory_version(None, "a.py") == {}
    assert inventory_version({"a.py": "not-a-dict"}, "a.py") == {}
    assert inventory_version({"a.py": {"sha256": "h", "bytes": 1}}, "b.py", "a.py") == {
        "sha256": "h", "bytes": 1,
    }
    inside = tmp_path / "sub" / "a.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("x", encoding="utf-8")
    assert relative_receipt_path(str(inside), tmp_path) == "sub/a.py"
    assert relative_receipt_path(str(tmp_path.parent / "outside.py"), tmp_path) == ""
    assert relative_receipt_path("", tmp_path) == ""


def test_ledger_write_receipt_carries_facts_and_never_the_content(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old", encoding="utf-8")
    execution = ExecutionStore(tmp_path).begin("coder", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    ledger.observe(
        {"id": "check", "function": {"name": "python_exec", "arguments": "check"}},
        {"status": "success", "returncode": 0},
    )
    source.write_text("MARKER42", encoding="utf-8")
    ledger.observe(
        {"id": "write", "function": {"name": "file_write", "arguments": json.dumps(
            {"path": str(source), "content": "MARKER42", "mode": "overwrite"})}},
        {"status": "success", "returncode": 0, "path": str(source), "bytes": 8},
    )
    receipt = ledger.observations[-1]
    assert "arguments" not in receipt
    assert "MARKER42" not in json.dumps(receipt)
    facts = receipt["write"]
    assert facts["operation"] == "overwrite"
    assert facts["changed"] is True
    assert facts["landed_matches_request"] is True
    assert facts["landed_bytes"] == 8

    projected = json.loads(ledger.message()["content"].split("\n")[-1])
    writes = [item for item in projected["recent_observations"] if item.get("tool") == "file_write"]
    assert writes and writes[-1]["write"]["changed"] is True
    assert "MARKER42" not in json.dumps(projected)


def test_ledger_failed_write_keeps_the_fact_that_nothing_changed(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("kept", encoding="utf-8")
    execution = ExecutionStore(tmp_path).begin("coder", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    ledger.observe(
        {"id": "read", "function": {"name": "file_read"}},
        {"status": "success", "returncode": 0, "path": str(source)},
    )
    ledger.observe(
        {"id": "write", "function": {"name": "file_write", "arguments": json.dumps(
            {"path": str(source), "content": "new", "mode": "replace",
             "old_text": "missing", "expected_sha256": "dead"})}},
        {"status": "error", "returncode": None, "path": str(source), "error": "replace requires exactly one old_text match"},
    )
    facts = ledger.observations[-1]["write"]
    assert facts["changed"] is False
    assert facts["failed_write_modified"] is False
    assert facts["status"] == "error"