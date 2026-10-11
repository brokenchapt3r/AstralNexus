"""Frozen model-visible receipt sets (doc 17 第一批⑤): citations resolve
against the request's own frozen set, so compaction or a shifted projection
window can never cross wires (压缩后引用不串号)."""
from __future__ import annotations

import json

from AstralNexus.runtime.execution_ledger import ExecutionLedger
from AstralNexus.runtime.execution_store import ExecutionStore


def _payload(message):
    return json.loads(message["content"].rsplit("\n", 1)[-1])


def _observe(ledger, call_id, command, stdout="ok"):
    ledger.observe(
        {"id": call_id, "function": {
            "name": "terminal_exec", "arguments": json.dumps({"command": command}),
        }},
        {"status": "success", "returncode": 0, "stdout": stdout},
    )


def test_frozen_sets_resolve_per_request(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-1", {"owned_paths": ["app.py"]})
    ledger = ExecutionLedger(tmp_path, ["app.py"], execution)

    _observe(ledger, "c1", "pytest tests", "3 passed")
    first_receipt = store.get(execution)["receipts"][0]["receipt_id"]
    payload1 = _payload(ledger.message(request_id="req-1"))
    assert first_receipt in payload1["visible_receipt_ids"]

    # Projection window shift: newer executions push the first receipt out.
    for index in range(5):
        _observe(ledger, f"c{index + 2}", f"cmd-{index}")
    payload2 = _payload(ledger.message(request_id="req-2"))
    assert first_receipt not in payload2["visible_receipt_ids"]

    # References resolve against each request's OWN frozen set.
    assert store.resolve_receipt_reference("req-1", first_receipt) == "visible"
    assert store.resolve_receipt_reference("req-2", first_receipt) == "unknown_to_request"
    assert store.resolve_receipt_reference("req-9", first_receipt) == "unknown_request"
    assert store.resolve_receipt_reference(
        "req-2", payload2["visible_receipt_ids"][0]
    ) == "visible"


def test_freeze_is_durable_across_store_reload(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-2", {"owned_paths": []})
    ledger = ExecutionLedger(tmp_path, [], execution)
    _observe(ledger, "c1", "pytest tests", "1 passed")
    payload = _payload(ledger.message(request_id="req-a"))
    reloaded = ExecutionStore(tmp_path)
    assert reloaded.request_receipts("req-a") == payload["visible_receipt_ids"]


def test_message_without_request_id_does_not_freeze(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-3", {"owned_paths": []})
    ledger = ExecutionLedger(tmp_path, [], execution)
    _observe(ledger, "c1", "pytest tests", "1 passed")
    payload = _payload(ledger.message())
    assert payload["visible_receipt_ids"]
    assert store.request_receipts("anything") == []


def test_projection_documents_citation_rule(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("coder-4", {"owned_paths": []})
    ledger = ExecutionLedger(tmp_path, [], execution)
    message = ledger.message(request_id="req-doc")
    assert "visible_receipt_ids" in message["content"]
    assert "must be reverified" in message["content"]
