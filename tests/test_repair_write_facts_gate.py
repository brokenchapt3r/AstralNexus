"""R5 (DESIGN_R5_WRITE_FACTS): a repair round that claims a fix but modified
nothing must not be able to reach ``completed``.

Offline only.  Every receipt below is produced by the real ``ExecutionStore`` +
``ExecutionLedger`` write-fact path (``runtime/file_write_facts.py``) and every
observation by the real deterministic acceptance layer; there is no live tool
call, no network and no LLM (the semantic judge is a stub that always scores
0.95, so a blocking verdict can only come from the deterministic layer).

Honesty boundary inside the fixture: what a test *simulates* is only what a tool
REPORTS (``status`` / ``returncode``); whether ``changed`` is ``True`` / ``False``
/ ``None`` is decided by the ledger's own SHA-256 inventory comparison, and a
test that wants "the write changed nothing" simply does not apply the write to
disk.  That is exactly the fraud shape R5 must catch, and it is built through the
real receipt code, not by hand-writing facts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier, VerificationVerdict
from AstralNexus.runtime.acceptance_checks import (
    KIND_WRITE_LANDED,
    REASONS,
    is_test_class_path,
    run_acceptance_checks,
    write_change_observations,
)
from AstralNexus.runtime.execution_ledger import ExecutionLedger
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.file_write_facts import write_receipt_facts
from AstralNexus.runtime.trace import TraceRecorder

WRITE_REASONS = frozenset({
    "WRITE_NO_PRODUCT_CHANGE",
    "WRITE_CHANGE_UNKNOWN",
    "WRITE_FAILED_BUT_MODIFIED",
    "WRITE_ONLY_TEST_CHANGE",
    "WRITE_ONLY_REPORT_CHANGE",
    "WRITE_LANDED",
})
BLOCKING_WRITE_REASONS = frozenset({
    "WRITE_NO_PRODUCT_CHANGE",
    "WRITE_FAILED_BUT_MODIFIED",
})


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _client():
    class Client:
        def chat(self, *args, **kwargs):
            return json.dumps({
                "score": 0.95,
                "details": "the repair looks complete",
                "blocking_gaps": [],
            })
    return Client()


def _verifier(tmp_path, graph=None, client=None):
    return Verifier(
        graph or TaskGraph(), TraceRecorder(tmp_path),
        llm_client=client, workspace_root=tmp_path,
    )


def _version(text: str) -> Dict[str, Any]:
    encoded = text.encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def _observe_baseline(ledger: ExecutionLedger) -> None:
    """A non-write observation first: it is what makes ``changed`` decidable.

    ``runtime/execution_ledger.py:62`` takes the PREVIOUS observation's ``files``
    as the baseline of a write, so this call is the only reason the next write
    can answer ``changed`` at all.  Without it the write is the first observation
    and ``changed`` stays ``None`` — the real shape T2 covers.
    """
    ledger.observe(
        {"id": "check", "function": {"name": "python_exec", "arguments": "check"}},
        {"status": "success", "returncode": 0},
    )


def _record_write(
    ledger: ExecutionLedger, arguments: Dict[str, Any], result: Dict[str, Any],
) -> None:
    ledger.observe(
        {"id": "write", "function": {"name": "file_write", "arguments": json.dumps(arguments)}},
        result,
    )


def _receipts(root: Path, execution: str) -> List[Dict[str, Any]]:
    return list(ExecutionStore(root).get(execution).get("receipts", []))


def _write_facts(receipts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item["write"] for item in receipts if isinstance(item.get("write"), dict)]


def _repair_node(
    *,
    node_id: str = "R-1",
    execution: str = "",
    repair: bool = True,
    criteria: tuple = ("修复说明合理",),
) -> TaskNode:
    metadata: Dict[str, Any] = {
        "runtime_execution_id": execution,
        "evidence_packet": {"summary": "the fix landed and every check passed"},
    }
    if repair:
        # The repair-round predicate core/verifier.py:628-637 reads.
        metadata["recovery_of"] = "O-1"
        metadata["_is_repair_node"] = True
        # This suite explicitly tests product-mutation repairs, not reverification.
        metadata["task_contract"] = {"requires_product_change": True}
    return TaskNode(
        node_id=node_id,
        goal="repair the still-missing fix",
        role="coder",
        status="result_submitted",
        acceptance_criteria=list(criteria),
        metadata=metadata,
    )


def _dimension(result):
    return next(d for d in result.dimensions if d.name == "deterministic_acceptance")


def _no_op_execution(tmp_path: Path, *, owned: str = "app.py") -> str:
    """A finished execution whose single product write changed NOTHING."""
    source = tmp_path / owned
    source.write_text("value = 0\n", encoding="utf-8")
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-1", {"owned_paths": [owned]})
    ledger = ExecutionLedger(tmp_path, [owned], execution)
    _observe_baseline(ledger)
    _record_write(
        ledger,
        {"path": str(source), "content": "value = 1\n", "mode": "replace",
         "old_text": "value = 0\n"},
        {"status": "success", "returncode": 0, "path": str(source)},
    )
    store.finish(execution, "succeeded", {"summary": "fixed the defect"})
    return execution


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #
def test_write_reason_vocabulary_is_closed_and_uppercase():
    assert WRITE_REASONS <= REASONS
    assert {reason for reason in REASONS if reason.startswith("WRITE_")} == WRITE_REASONS
    for reason in WRITE_REASONS:
        assert reason == reason.upper()


def test_test_class_whitelist_is_explicit():
    assert is_test_class_path("tests/test_app.py")
    assert is_test_class_path("app/tests/helper.py")
    assert is_test_class_path("src/fixtures/data.json")
    assert is_test_class_path("conftest.py")
    assert is_test_class_path("test_app.py")
    assert is_test_class_path("app_test.py")
    # never by substring: product code that merely contains the letters
    assert not is_test_class_path("contest.py")
    assert not is_test_class_path("tests_utils.py")
    assert not is_test_class_path("app.py")
    assert not is_test_class_path("deliverables/app/backend/main.py")
    assert not is_test_class_path("")


# --------------------------------------------------------------------------- #
# T1 — the blocking no-op write
# --------------------------------------------------------------------------- #
def test_T1_no_op_repair_write_blocks_the_node(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 0\n", encoding="utf-8")
    execution = _no_op_execution(tmp_path)

    facts = _write_facts(_receipts(tmp_path, execution))
    assert len(facts) == 1
    assert facts[0]["changed"] is False
    assert facts[0]["change_kind"] == "unchanged"
    assert facts[0]["failed_write_modified"] is False
    assert facts[0]["operation"] == "replace"

    graph = TaskGraph()
    graph.add_node(_repair_node(execution=execution))
    result = _verifier(tmp_path, graph, _client()).verify_node("R-1")
    dim = _dimension(result)
    assert [o["criterion_id"] for o in dim.observations] == ["R-1.AC01", "R-1.WR01"]
    blocking = dim.observations[1]
    assert blocking["reason"] == "WRITE_NO_PRODUCT_CHANGE"
    assert blocking["kind"] == KIND_WRITE_LANDED
    assert blocking["checked"] is True and blocking["holds"] is False
    assert len(blocking["receipt_ids"]) == 1 and blocking["receipt_ids"][0]
    assert "unchanged" in blocking["detail"]
    assert dim.passed is False and dim.score == 0.0
    assert result.verdict is not VerificationVerdict.APPROVED
    # The write leaf is an ADDITIONAL failure, so report-only-gap reuse cannot
    # launder this node (core/verifier.py:766-782 -> report_gap.py:120-121).
    assert result.report_gap is None


def test_T1_no_op_reason_is_not_the_unknown_reason(tmp_path):
    """None and False must never be merged: one blocks, the other audits."""
    execution = _no_op_execution(tmp_path)
    node = _repair_node(execution=execution)
    leaves = write_change_observations(
        receipts=_receipts(tmp_path, execution), node_id="R-1", repair_round=True,
        root=tmp_path,
    )
    assert [leaf["reason"] for leaf in leaves] == ["WRITE_NO_PRODUCT_CHANGE"]
    assert leaves[0]["holds"] is False
    assert "WRITE_CHANGE_UNKNOWN" not in {leaf["reason"] for leaf in leaves}


# --------------------------------------------------------------------------- #
# T2 — the undecidable first write must stay auditable, never blocking
# --------------------------------------------------------------------------- #
def test_T2_undecidable_first_write_is_auditable_and_not_blocking(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-1", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    # The write is the FIRST observation: no baseline exists yet, so the ledger
    # cannot decide whether the bytes moved.  In a repair execution this first
    # write is usually the fix itself.
    _record_write(
        ledger,
        {"path": str(source), "content": "value = 1\n", "mode": "overwrite"},
        {"status": "success", "returncode": 0, "path": str(source)},
    )
    store.finish(execution, "succeeded", {"summary": "fixed"})

    facts = _write_facts(_receipts(tmp_path, execution))
    assert facts[0]["changed"] is None and facts[0]["change_kind"] == "unknown"

    graph = TaskGraph()
    graph.add_node(_repair_node(execution=execution, criteria=("file:app.py non-empty",)))
    result = _verifier(tmp_path, graph, _client()).verify_node("R-1")
    dim = _dimension(result)
    assert [o["reason"] for o in dim.observations] == [
        "", "WRITE_CHANGE_UNKNOWN",
    ]
    unknown = dim.observations[1]
    assert unknown["checked"] is False and unknown["holds"] is None
    assert unknown["criterion_id"] == "R-1.WR01"
    assert unknown["kind"] == KIND_WRITE_LANDED
    # non-blocking: the holding criterion is the only decided leaf
    assert dim.passed is True and dim.score == 1.0
    assert not [o for o in dim.observations if o["holds"] is False]
    assert result.verdict is not VerificationVerdict.REPLAN_REQUIRED

    false_reason = "WRITE_NO_PRODUCT_CHANGE"
    assert unknown["reason"] != false_reason
    assert unknown["reason"] != dim.observations[0]["reason"]


def test_T2_none_and_false_reasons_are_distinct(tmp_path):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    undecidable = write_receipt_facts(
        {"path": "app.py", "content": "x", "mode": "overwrite"},
        {}, {"app.py": _version("x")}, relative_path="app.py", status="success",
    )
    no_op = write_receipt_facts(
        {"path": "app.py", "content": "y", "mode": "overwrite"},
        {"app.py": _version("x")}, {"app.py": _version("x")},
        relative_path="app.py", status="success",
    )
    assert undecidable["changed"] is None and no_op["changed"] is False
    none_leaves = write_change_observations(
        receipts=[{"receipt_id": "n1", "write": undecidable}],
        node_id="R-2", repair_round=True,
    )
    false_leaves = write_change_observations(
        receipts=[{"receipt_id": "f1", "write": no_op}],
        node_id="R-2", repair_round=True,
    )
    assert [leaf["reason"] for leaf in none_leaves] == ["WRITE_CHANGE_UNKNOWN"]
    assert [leaf["reason"] for leaf in false_leaves] == ["WRITE_NO_PRODUCT_CHANGE"]
    assert none_leaves[0]["reason"] != false_leaves[0]["reason"]
    assert none_leaves[0]["holds"] is None and false_leaves[0]["holds"] is False


# --------------------------------------------------------------------------- #
# T3 — a failed write that modified bytes has its own leaf
# --------------------------------------------------------------------------- #
def test_T3_failed_write_that_modified_gets_its_own_leaf(tmp_path):
    facts = write_receipt_facts(
        {"path": "app.py", "content": "new value\n", "mode": "overwrite"},
        {"app.py": _version("old value\n")},
        {"app.py": _version("new value\n")},
        relative_path="app.py", status="error", returncode=1,
    )
    assert facts["failed_write_modified"] is True and facts["changed"] is True

    leaves = write_change_observations(
        receipts=[{"receipt_id": "r9", "write": facts}],
        node_id="R-3", repair_round=True,
    )
    reasons = [leaf["reason"] for leaf in leaves]
    assert "WRITE_FAILED_BUT_MODIFIED" in reasons
    blocking = next(l for l in leaves if l["reason"] == "WRITE_FAILED_BUT_MODIFIED")
    assert blocking["checked"] is True and blocking["holds"] is False
    assert blocking["receipt_ids"] == ["r9"]
    assert "a failed write" in blocking["detail"]
    # the same receipt also proves the bytes landed: a different, non-blocking fact
    landed = next(l for l in leaves if l["reason"] == "WRITE_LANDED")
    assert landed["holds"] is True


# --------------------------------------------------------------------------- #
# T4 — no content leaks into observations or the model-visible projection
# --------------------------------------------------------------------------- #
def test_T4_write_content_never_leaks(tmp_path):
    sentinel = "R5_SENTINEL_9f2c41_LEAK_CANARY"
    source = tmp_path / "app.py"
    source.write_text("before\n", encoding="utf-8")
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-1", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    _observe_baseline(ledger)
    source.write_text(sentinel + "\n", encoding="utf-8")
    _record_write(
        ledger,
        {"path": str(source), "content": sentinel + "\n", "mode": "overwrite"},
        {"status": "success", "returncode": 0, "path": str(source)},
    )
    projection = json.dumps(ledger.message("m-1"), ensure_ascii=False)
    store.finish(execution, "succeeded", {"summary": "fixed"})

    # the sentinel really is in the file and really is what was requested
    assert sentinel in source.read_text(encoding="utf-8")
    receipts = _receipts(tmp_path, execution)
    facts = _write_facts(receipts)
    assert facts[0]["changed"] is True
    assert facts[0]["content_recorded"] is False
    assert sentinel not in json.dumps(receipts, ensure_ascii=False)

    node = _repair_node(execution=execution, criteria=("file:app.py non-empty",))
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    write_leaf = next(o for o in dim.observations if o["reason"] in WRITE_REASONS)
    assert sentinel not in json.dumps(dim.observations, ensure_ascii=False)
    assert sentinel not in json.dumps(write_leaf, ensure_ascii=False)
    # path / digest / byte count only
    assert sentinel not in write_leaf["detail"]
    assert "app.py" in write_leaf["detail"]
    assert sentinel not in projection


# --------------------------------------------------------------------------- #
# T5 — only test-class / only report-class writes are non-blocking audits
# --------------------------------------------------------------------------- #
def test_T5_only_test_class_paths_are_a_non_blocking_audit():
    facts = write_receipt_facts(
        {"path": "tests/test_app.py", "content": "assert True\n", "mode": "overwrite"},
        {"tests/test_app.py": _version("old\n")},
        {"tests/test_app.py": _version("assert True\n")},
        relative_path="tests/test_app.py", status="success",
    )
    assert facts["changed"] is True
    leaves = write_change_observations(
        receipts=[{"receipt_id": "t1", "write": facts}],
        node_id="R-5", repair_round=True,
    )
    assert [leaf["reason"] for leaf in leaves] == ["WRITE_ONLY_TEST_CHANGE"]
    assert leaves[0]["checked"] is False and leaves[0]["holds"] is None
    assert not [leaf for leaf in leaves if leaf["holds"] is False]


def test_T5_only_report_class_paths_are_a_non_blocking_audit():
    facts = write_receipt_facts(
        {"path": "reports/summary.md", "content": "# done\n", "mode": "overwrite"},
        {"reports/summary.md": _version("# draft\n")},
        {"reports/summary.md": _version("# done\n")},
        relative_path="reports/summary.md", status="success",
    )
    leaves = write_change_observations(
        receipts=[{"receipt_id": "p1", "write": facts}],
        node_id="R-5", repair_round=True,
    )
    assert [leaf["reason"] for leaf in leaves] == ["WRITE_ONLY_REPORT_CHANGE"]
    assert leaves[0]["checked"] is False and leaves[0]["holds"] is None
    assert not [leaf for leaf in leaves if leaf["holds"] is False]


def test_T5_test_and_report_together_stay_undecidable():
    test_facts = write_receipt_facts(
        {"path": "tests/test_app.py", "content": "assert True\n", "mode": "overwrite"},
        {"tests/test_app.py": _version("old\n")},
        {"tests/test_app.py": _version("assert True\n")},
        relative_path="tests/test_app.py", status="success",
    )
    report_facts = write_receipt_facts(
        {"path": "reports/summary.md", "content": "# done\n", "mode": "overwrite"},
        {"reports/summary.md": _version("# draft\n")},
        {"reports/summary.md": _version("# done\n")},
        relative_path="reports/summary.md", status="success",
    )
    leaves = write_change_observations(
        receipts=[
            {"receipt_id": "t1", "write": test_facts},
            {"receipt_id": "p1", "write": report_facts},
        ],
        node_id="R-5", repair_round=True,
    )
    assert [leaf["reason"] for leaf in leaves] == ["WRITE_CHANGE_UNKNOWN"]
    assert leaves[0]["receipt_ids"] == ["t1", "p1"]


# --------------------------------------------------------------------------- #
# T6 — a product write that landed
# --------------------------------------------------------------------------- #
def test_T6_landed_product_write_is_audited_as_landed(tmp_path):
    source = tmp_path / "app.py"
    # byte-exact fixtures: ``write_text`` would translate "\n" to "\r\n" on
    # Windows and silently break the requested-bytes-equal-landed-bytes claim
    source.write_bytes(b"broken\n")
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-1", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)
    _observe_baseline(ledger)
    source.write_bytes(b"fixed\n")
    _record_write(
        ledger,
        {"path": str(source), "content": "fixed\n", "mode": "overwrite"},
        {"status": "success", "returncode": 0, "path": str(source)},
    )
    store.finish(execution, "succeeded", {"summary": "fixed"})

    facts = _write_facts(_receipts(tmp_path, execution))
    assert facts[0]["changed"] is True
    assert facts[0]["landed_matches_request"] is True

    graph = TaskGraph()
    graph.add_node(_repair_node(execution=execution, criteria=("file:app.py non-empty",)))
    result = _verifier(tmp_path, graph, _client()).verify_node("R-1")
    dim = _dimension(result)
    reasons = [o["reason"] for o in dim.observations]
    assert "WRITE_LANDED" in reasons
    landed = next(o for o in dim.observations if o["reason"] == "WRITE_LANDED")
    assert landed["checked"] is True and landed["holds"] is True
    assert landed["kind"] == KIND_WRITE_LANDED
    assert not [o for o in dim.observations if o["holds"] is False]
    assert dim.passed is True and dim.score == 1.0
    # a landed repair is not pushed back by this layer
    assert result.verdict is not VerificationVerdict.REPLAN_REQUIRED


# --------------------------------------------------------------------------- #
# absence must never be evidence
# --------------------------------------------------------------------------- #
def test_receipts_without_a_write_key_are_never_a_failed_write():
    receipts = [
        {"receipt_id": "r1", "tool": "file_write", "status": "error", "path": "app.py"},
        {"receipt_id": "r2", "tool": "terminal_exec", "status": "error"},
        {"receipt_id": "r3", "tool": "python_exec", "status": "success"},
    ]
    leaves = write_change_observations(
        receipts=receipts, node_id="R-0", repair_round=True,
    )
    assert [leaf["reason"] for leaf in leaves] == ["WRITE_CHANGE_UNKNOWN"]
    assert leaves[0]["checked"] is False and leaves[0]["holds"] is None
    assert leaves[0]["receipt_ids"] == []
    assert "no file_write fact" in leaves[0]["detail"]
    assert not [leaf for leaf in leaves if leaf["holds"] is False]


def test_receipts_without_a_write_key_produce_no_leaves_for_a_non_repair_round():
    assert write_change_observations(
        receipts=[{"receipt_id": "r1", "status": "error"}],
        node_id="N-0", repair_round=False,
    ) == []


# --------------------------------------------------------------------------- #
# T7 — a non-repair node keeps its exact former observation set
# --------------------------------------------------------------------------- #
def test_T7_non_repair_node_with_the_same_receipts_gets_no_write_leaves(tmp_path):
    execution = _no_op_execution(tmp_path)
    criteria = ("file:app.py non-empty",)
    node = _repair_node(node_id="N-9", execution=execution, repair=False, criteria=criteria)
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert [o["criterion_id"] for o in dim.observations] == ["N-9.AC01"]
    assert not [o for o in dim.observations if o["reason"] in WRITE_REASONS]
    # the same receipts through the same node, stamped as a repair, do produce it
    repair_dim = _verifier(tmp_path)._verify_deterministic_acceptance(
        _repair_node(node_id="N-9", execution=execution, repair=True, criteria=criteria)
    )
    assert [o["criterion_id"] for o in repair_dim.observations] == [
        "N-9.AC01", "N-9.WR01",
    ]
    assert repair_dim.observations[-1]["reason"] == "WRITE_NO_PRODUCT_CHANGE"
    # the pre-existing leaf is identical in both cases
    assert dim.observations[0] == repair_dim.observations[0]


def test_T7_non_repair_node_with_only_undecidable_criteria_keeps_no_dimension(tmp_path):
    execution = _no_op_execution(tmp_path)
    node = _repair_node(node_id="N-9", execution=execution, repair=False)
    assert _verifier(tmp_path)._verify_deterministic_acceptance(node) is None


# --------------------------------------------------------------------------- #
# T8 — the default path is byte-identical for every existing caller
# --------------------------------------------------------------------------- #
def test_T8_default_call_is_unchanged(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    execution = _no_op_execution(tmp_path)
    receipts = _receipts(tmp_path, execution)
    criteria = [("R-8.AC01", "file:app.py non-empty"), "修复说明合理"]

    default = run_acceptance_checks(
        criteria, receipts=receipts, execution_id="ex-8", root=tmp_path,
    )
    explicit_off = run_acceptance_checks(
        criteria, receipts=receipts, execution_id="ex-8", root=tmp_path,
        write_facts_enabled=False,
    )
    assert default == explicit_off
    assert [o["criterion_id"] for o in default] == ["R-8.AC01", "C02"]
    assert not [o for o in default if o["reason"] in WRITE_REASONS]

    enabled = run_acceptance_checks(
        criteria, receipts=receipts, execution_id="ex-8", root=tmp_path,
        write_facts_enabled=True,
    )
    assert [o["criterion_id"] for o in enabled] == ["R-8.AC01", "C02", "R-8.WR01"]
    assert enabled[-1]["reason"] == "WRITE_NO_PRODUCT_CHANGE"
    assert enabled[-1]["execution_id"] == "ex-8"
    # the pre-existing leaves are untouched by the switch
    assert enabled[:2] == default


def test_T8_criterion_ids_without_a_node_prefix_do_not_invent_one(tmp_path):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    execution = _no_op_execution(tmp_path)
    enabled = run_acceptance_checks(
        ["file:app.py non-empty"], receipts=_receipts(tmp_path, execution),
        root=tmp_path, write_facts_enabled=True,
    )
    assert [o["criterion_id"] for o in enabled] == ["C01", "WR01"]