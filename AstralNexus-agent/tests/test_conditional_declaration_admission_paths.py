"""Admission-path regression: a conditional declared artifact must stay optional.

``tests/test_declared_output_normalization_wiring.py`` covers the declaration
path that goes straight into ``GlobalGovernor.apply_task_graph_patch``.  This
file covers the OTHER live entry point: ``GlobalGovernor.admit_worker_subtasks``
— the path a Worker-discovered subtask takes.

``core/governor.py`` rebuilds ``expected_outputs`` with ``strings_from`` inside
``admit_worker_subtasks`` before the patch, so a STRUCTURED conditional
declaration (``required: False``) is replaced by its own ``repr`` string and is
read back as REQUIRED (无法判定即必需).  That is exactly the 起 A hardening: live
r30 R-107 recorded ``result PASS 53 / 53 failed []`` and was still failed and
rerun for a correctly absent ``deliverables/runs/browser/defects.json``.

Pure and offline: no network, no subprocess, no LLM.  The classification rules
in ``core/output_paths.py`` are frozen and are not changed here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.output_paths import declared_output_is_required
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.trace import TraceRecorder

GREEN_CMD = "python -m pytest tests -q"
PASSING_SUMMARY = "===== 12 passed in 0.42s ====="
DEFECTS_PATH = "deliverables/runs/browser/defects.json"
PROSE_CONDITIONAL = (
    "巡检浏览器流程；若发现缺陷则输出 deliverables/runs/browser/defects.json"
)
REQUIRED_PATH = "deliverables/app/backend/schema.sql"
# Live r30 shape: a structured declaration that the contract itself marks optional.
CONDITIONAL_ENTRY: Dict[str, Any] = {
    "output_id": "O-02",
    "path": DEFECTS_PATH,
    "required": False,
    "source": "optional",
    "condition": {"kind": "no_defects", "when_false": "structured_empty_manifest"},
}


def _governor(tmp_path: Path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="completed",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def _admit(
    governor: GlobalGovernor,
    expected_outputs: List[Any],
    owned_paths: Optional[List[str]] = None,
):
    """Admit one Worker-discovered subtask and return the node it created."""
    result = governor.admit_worker_subtasks(
        parent_node_id="root",
        proposals=[{
            "goal": "repair the browser defect sheet",
            "role": "coder",
            "owned_paths": list(owned_paths or ["deliverables/runs/browser/"]),
            "expected_outputs": list(expected_outputs),
            "information_gain": 1.0,
            "risk_reduction": 1.0,
            "parallel_benefit": 1.0,
            "estimated_cost": 1,
        }],
    )
    admitted = [
        node for node in governor.task_graph.nodes.values() if node.node_id != "root"
    ]
    assert admitted, f"the Worker proposal was not admitted: {result!r}"
    return admitted[0], result


def _expected_outputs(node: TaskNode) -> List[Any]:
    return list(node.metadata.get("expected_outputs") or [])


def _green_receipt(receipt_id: str) -> Dict[str, Any]:
    return {
        "tool": "terminal_exec",
        "call_id": "call-conditional-1",
        "status": "success",
        "returncode": 0,
        "stdout_tail": PASSING_SUMMARY,
        "files": {},
        "receipt_id": receipt_id,
        "arguments": json.dumps({"command": GREEN_CMD}),
    }


# --- ① a STRUCTURED conditional declaration must survive admission ----------


def test_structured_conditional_declaration_stays_optional(tmp_path: Path) -> None:
    graph, governor = _governor(tmp_path)
    node, result = _admit(governor, [CONDITIONAL_ENTRY])
    outputs = _expected_outputs(node)
    assert outputs, f"no expected_outputs on the admitted node: {result!r}"
    first = outputs[0]
    assert isinstance(first, dict), (
        "the structured conditional declaration was collapsed to "
        f"{first!r}; a repr string has no readable required field and is read "
        "back as REQUIRED, re-hardening the r30 R-107 postcondition"
    )
    assert declared_output_is_required(first) is False, (
        f"an explicitly optional declaration was hardened: {first!r}"
    )


# --- ② the prose-conditional form must survive the same path ----------------


def test_prose_conditional_declaration_stays_optional(tmp_path: Path) -> None:
    graph, governor = _governor(tmp_path)
    node, result = _admit(governor, [PROSE_CONDITIONAL])
    outputs = _expected_outputs(node)
    assert outputs, f"no expected_outputs on the admitted node: {result!r}"
    first = outputs[0]
    assert isinstance(first, dict), (
        f"the prose-conditional declaration stayed a bare string: {first!r}"
    )
    assert first.get("source") == "prose_conditional", (
        f"unexpected normalization source: {first!r}"
    )
    assert declared_output_is_required(first) is False


# --- ③ CONTROL: a required declaration must stay required -------------------


def test_required_declaration_stays_required(tmp_path: Path) -> None:
    graph, governor = _governor(tmp_path)
    node, _ = _admit(
        governor, [REQUIRED_PATH], owned_paths=["deliverables/app/backend/"]
    )
    outputs = _expected_outputs(node)
    assert outputs, "no expected_outputs on the admitted node"
    assert declared_output_is_required(outputs[0]) is True, (
        f"a required declaration was relaxed: {outputs[0]!r}"
    )


# --- ④ end to end: green execution + absent conditional artifact → pass -----


def test_green_execution_with_an_absent_conditional_artifact_passes(
    tmp_path: Path,
) -> None:
    """The 起 A closure composed through the Worker-discovery path."""
    graph, governor = _governor(tmp_path)
    node, _ = _admit(governor, [PROSE_CONDITIONAL])
    store = ExecutionStore(tmp_path)
    execution = store.begin(
        node.node_id, {"owned_paths": [], "upstream_artifacts": []}
    )
    store.append(execution, _green_receipt("rcpt-conditional-1"))
    store.finish(execution, "succeeded", {"summary": "full green run"})
    node.metadata["runtime_execution_id"] = execution
    node.acceptance_criteria = [f"tests_passed:{GREEN_CMD}"]

    verifier = Verifier(
        graph, TraceRecorder(tmp_path), llm_client=None, workspace_root=tmp_path
    )
    dimension = verifier._verify_deterministic_acceptance(node)
    assert dimension is not None
    failing = [
        o for o in dimension.observations if o["checked"] and o["holds"] is False
    ]
    assert failing == [], (
        "a conditional/optional artifact was hardened into a hard postcondition "
        f"on the Worker-discovery path: "
        f"{[(o['criterion_id'], o['reason'], o['detail']) for o in dimension.observations]!r}"
    )
    assert dimension.passed is True