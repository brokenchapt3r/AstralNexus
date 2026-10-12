"""b28 minimal inversion tests for the r30 artifact-contract defects (offline).

Authority for every "target contract" asserted below:

* ``02_运行产物/capability_migration/b26_fullstack_audit/RECONCILIATION.md``
  (起 A conditional artifact hardened / 起 B self-inflicted stale / 起 C ownership)
* ``02_运行产物/capability_migration/b25_artifact_contract/DESIGN_ARTIFACT_CONTRACT.md``
  (§4 contract table, §8 seven implementation boundaries, §9 recovery payload)
* ``FINDINGS_TOOLCHAIN.md`` P0-2 (execution-store staleness) and P0-2 (budget)
* ``FINDINGS_ARCHITECTURE.md`` P0-2 (required/optional dropped by normalization)

REVERSAL tests assert the TARGET contract and are EXPECTED TO FAIL on the current
framework; that failure is the required evidence that the framework is still broken.
CONTROL tests pin behaviour that is already correct and must stay green so a fix
cannot overshoot.  There is deliberately no xfail/skip and no relaxed assertion.

Offline only: tmp dirs, injected receipts, no network, no subprocess, no browser,
no orchestration run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.core.output_paths import RunOutputLayout
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files
from AstralNexus.runtime.task_execution_budget import (
    TaskExecutionBudget,
    default_max_tool_calls,
    ensure_task_budget,
)
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.runtime.verification_identity import (
    changed_premises,
    declared_premise_paths,
    premise_versions,
)

TEST_CMD = "python -m pytest tests/test_product.py -q"
GREEN_CMD = "python -m pytest tests -q"
PASSING_SUMMARY = "===== 12 passed in 0.42s ====="


# --------------------------------------------------------------------------- helpers


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _receipt(
    command: str,
    files: Optional[Dict[str, Any]] = None,
    receipt_id: str = "rcpt-inversion-1",
) -> Dict[str, Any]:
    """A recorded successful test execution, same shape as the store writes."""
    return {
        "tool": "terminal_exec",
        "call_id": "call-inversion-1",
        "status": "success",
        "returncode": 0,
        "stdout_tail": PASSING_SUMMARY,
        "files": dict(files or {}),
        "receipt_id": receipt_id,
        "arguments": json.dumps({"command": command}),
    }


def _verifier(root: Path, graph: Optional[TaskGraph] = None) -> Verifier:
    return Verifier(
        graph or TaskGraph(), TraceRecorder(root), llm_client=None, workspace_root=root
    )


# =========================================================== 起 B: self-inflicted stale
# RECONCILIATION.md C1 起 B / FINDINGS_TOOLCHAIN.md P0-2:
# a node whose ``owned_paths`` live INSIDE one of its ``upstream_paths`` directories
# invalidates its own evidence, because runtime/execution_store.py
# ``external_upstream_paths`` (:103-135) only removes "upstream ⊆ owned" and never
# "owned ⊆ upstream directory".


def _own_output_contract(upstream: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "owned_paths": ["app/summary.md"],
        "upstream_artifacts": upstream,
        "acceptance_criteria": ["file:app/summary.md exists"],
    }


def test_owned_output_inside_upstream_directory_is_not_stale(tmp_path: Path) -> None:
    """REVERSAL (起 B): writing your own deliverable must not stale your own evidence.

    Product paths: core/orchestrator.py:3503-3509, core/verifier.py:565-572 are the
    postcondition side; the stale raise is runtime/execution_store.py:386-391 fed by
    :103-135.  r30 N-202 produced exactly
    ``Execution evidence is stale; upstream input files changed since execution:
    added=1 [deliverables/app/收尾摘要.md]``.
    """
    app = tmp_path / "app"
    _write(app / "README.md", "upstream readme\n")

    store = ExecutionStore(tmp_path)
    execution = store.begin("N-202", _own_output_contract([{"path": "app"}]))
    _write(app / "summary.md", "the node's own deliverable\n")
    store.finish(execution, "succeeded", {"summary": "wrote app/summary.md"})

    store.accept(execution, {"approved": True})

    assert store.get(execution)["acceptance"]["approved"] is True


def test_changed_external_upstream_input_is_still_stale(tmp_path: Path) -> None:
    """CONTROL: a genuinely external input change must keep staling the evidence.

    The fix for the case above must not blanket-ignore upstream directories.
    """
    app = tmp_path / "app"
    _write(app / "config.json", '{"threshold": 1}\n')

    store = ExecutionStore(tmp_path)
    execution = store.begin("N-203", _own_output_contract([{"path": "app/config.json"}]))
    _write(app / "summary.md", "own deliverable\n")
    _write(app / "config.json", '{"threshold": 2}\n')  # real external edit, after begin()
    store.finish(execution, "succeeded", {"summary": "wrote app/summary.md"})

    with pytest.raises(ValueError, match="Execution evidence is stale"):
        store.accept(execution, {"approved": True})


# ================================================= 报告类产物 vs 功能证据前提集 (DESIGN §8 ③)
# DESIGN_ARTIFACT_CONTRACT §8 boundary ③: only product code / test scripts / test
# inputs invalidate functional evidence; a report update must NOT.  The named
# implementation point is runtime/verification_identity.py ``declared_premise_paths``.

PRODUCT = "deliverables/app/product.py"
TEST_SCRIPT = "tests/test_product.py"
REPORT = "deliverables/reports/N-301-summary.md"


PREMISE_PATHS = [PRODUCT, TEST_SCRIPT, REPORT]


def _seed_premise_workspace(root: Path) -> tuple[Dict[str, Any], Dict[str, str]]:
    """Create the three premise files.

    Returns the two shapes the two sides of the gate use: the receipt-shaped
    inventory (``{path: {bytes, sha256}}``, exactly what ExecutionStore writes into
    ``receipt["files"]``) and the flat ``{path: sha256}`` premise version map that
    ``verification_identity.premise_versions`` produces.
    """
    _write(root / PRODUCT, "def add(a, b):\n    return a + b\n")
    _write(root / TEST_SCRIPT, "def test_add():\n    assert True\n")
    _write(root / REPORT, "# report\n")
    return snapshot_files(root, PREMISE_PATHS), premise_versions(root, PREMISE_PATHS)


def _premise_declared() -> List[str]:
    return declared_premise_paths(
        acceptance_criteria=[f"tests_passed:{TEST_CMD}"],
        upstream_artifacts=[],
        owned_paths=[PRODUCT, TEST_SCRIPT, REPORT],
    )


def _functional_observation(
    root: Path, receipt: Dict[str, Any], premise_now: Dict[str, str]
) -> Dict[str, Any]:
    observations = run_acceptance_checks(
        [("N-301.AC01", f"tests_passed:{TEST_CMD}")],
        receipts=[receipt],
        execution_id="exec-inversion-301",
        root=root,
        latest_files=None,
        premise_versions=premise_now,
        declared_premises=_premise_declared(),
    )
    assert len(observations) == 1
    return observations[0]


def test_report_only_change_does_not_stale_the_functional_receipt(tmp_path: Path) -> None:
    """REVERSAL: backfilling a report must not invalidate a green test receipt.

    This is the loop DESIGN §8 ③ exists to prevent: if the report artifact is a
    functional premise, the backfill that repairs 起 A immediately creates
    ``TEST_EXECUTION_STALE`` for the very evidence it is meant to preserve.
    """
    inventory, flat_before = _seed_premise_workspace(tmp_path)
    receipt = _receipt(TEST_CMD, inventory)
    _write(tmp_path / REPORT, "# report v2 (report-only edit)\n")
    premise_now = premise_versions(tmp_path, _premise_declared())

    # The edit is real: the raw digest did change.
    assert REPORT in changed_premises(flat_before, premise_now)

    observation = _functional_observation(tmp_path, receipt, premise_now)
    assert observation["holds"] is True and observation["reason"] != "TEST_EXECUTION_STALE", (
        "a report-only edit invalidated the functional test receipt: "
        f"reason={observation['reason']!r} detail={observation['detail']!r} "
        f"premise_versions={premise_now!r} "
        f"artifact_versions={observation['artifact_versions']!r}"
    )


def test_report_class_path_is_not_a_functional_premise(tmp_path: Path) -> None:
    """REVERSAL (mechanism level): the report path must not enter the premise set."""
    _seed_premise_workspace(tmp_path)
    declared = _premise_declared()
    premise_now = premise_versions(tmp_path, declared)
    assert REPORT not in premise_now, (
        f"report-class artifact {REPORT!r} is registered as a functional premise: "
        f"declared_premise_paths={declared!r} premise_versions={sorted(premise_now)!r}"
    )


@pytest.mark.parametrize("changed_path", [PRODUCT, TEST_SCRIPT])
def test_product_and_test_script_change_stales_the_functional_receipt(
    tmp_path: Path, changed_path: str
) -> None:
    """CONTROL: product code / test script changes must keep invalidating evidence."""
    inventory, flat_before = _seed_premise_workspace(tmp_path)
    receipt = _receipt(TEST_CMD, inventory)
    _write(tmp_path / changed_path, "# changed after the anchored execution\n")
    premise_now = premise_versions(tmp_path, _premise_declared())

    assert changed_path in changed_premises(flat_before, premise_now)

    observation = _functional_observation(tmp_path, receipt, premise_now)
    assert observation["reason"] == "TEST_EXECUTION_STALE"
    assert observation["holds"] is False


# ==================================================== 起 A: conditional artifact hardening
# RECONCILIATION.md C1 起 A / FINDINGS_ARCHITECTURE.md P0-2:
# core/governor.py:1386 normalizes the delivery contract's required/source flags away
# (contract built with them at :3341-3356), and core/orchestrator.py:3503-3509 +
# core/verifier.py:565-572 then treat every declared path as a hard postcondition.
#
# Fallback layer, stated explicitly for the report: the hardening itself lives inside
# the ~10000-line Orchestrator method (core/orchestrator.py:3528-3619) and the flag loss
# inside Governor.apply_task_graph_patch's inline normalization loop
# (core/governor.py:1145-1444); neither has a seam a test can drive offline without a
# full orchestration run.  The minimal testable surface is therefore the CONSUMER of
# that contract: core/verifier.py's criterion generation
# (Verifier._deterministic_observations / _verify_deterministic_acceptance), which today
# hardens a non-required declaration into a failing ``file:<path> non-empty`` leaf.
# The target contract is NOT weakened: a *required* declaration must keep failing.

CONDITIONAL_ARTIFACT: Dict[str, Any] = {
    "output_id": "O-02",
    "name": "defect report (structured empty manifest when there are no defects)",
    "type": "file",
    "path": "deliverables/reports/defects.json",
    "required": False,
    "source": "optional",
    "condition": {
        "kind": "no_defects",
        "source": "artifact:functional-test-receipts",
        "when_false": "structured_empty_manifest",
    },
}
REQUIRED_ARTIFACT: Dict[str, Any] = {
    "output_id": "O-01",
    "name": "required deliverable",
    "type": "file",
    "path": "deliverables/reports/defects.json",
    "required": True,
    "source": "explicit",
}


def _record_green_execution(root: Path, node_id: str) -> str:
    store = ExecutionStore(root)
    execution = store.begin(node_id, {"owned_paths": [], "upstream_artifacts": []})
    store.append(execution, _receipt(GREEN_CMD, {}, receipt_id=f"rcpt-{node_id}"))
    store.finish(execution, "succeeded", {"summary": "full green run"})
    return execution


def test_conditional_artifact_absence_does_not_fail_a_green_node(tmp_path: Path) -> None:
    """REVERSAL (起 A): a conditional/optional artifact must not be a hard postcondition.

    r30 R-107: ``result PASS 53/53 failed []`` and yet the node was failed with
    ``Worker did not materialize declared output files: deliverables/runs/browser/
    defects.json`` because the sentence "if defects are found, output a defect sheet"
    was taken as an unconditional file postcondition.
    """
    execution = _record_green_execution(tmp_path, "N-401")
    node = TaskNode(
        node_id="N-401",
        goal="巡检浏览器流程；若发现缺陷则输出结构化缺陷单",
        role="coder",
        acceptance_criteria=[f"tests_passed:{GREEN_CMD}"],
        metadata={
            "runtime_execution_id": execution,
            "expected_outputs": [CONDITIONAL_ARTIFACT],
        },
    )
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dimension is not None
    failing = [o for o in dimension.observations if o["checked"] and o["holds"] is False]
    assert failing == [], (
        "a conditional/optional artifact was hardened into a hard postcondition: "
        f"{[(o['criterion_id'], o['reason'], o['detail']) for o in dimension.observations]!r}"
    )
    assert dimension.passed is True


def test_required_artifact_absence_still_fails_the_node(tmp_path: Path) -> None:
    """CONTROL: a genuinely required artifact that never materialized must keep failing."""
    execution = _record_green_execution(tmp_path, "N-402")
    node = TaskNode(
        node_id="N-402",
        goal="产出必需交付物",
        role="coder",
        acceptance_criteria=[f"tests_passed:{GREEN_CMD}"],
        metadata={
            "runtime_execution_id": execution,
            "expected_outputs": [REQUIRED_ARTIFACT],
        },
    )
    dimension = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dimension is not None
    assert dimension.passed is False
    assert any(o["reason"] == "FILE_NOT_FOUND" for o in dimension.observations), (
        f"no FILE_NOT_FOUND leaf for a required, missing artifact: {dimension.observations!r}"
    )


def test_missing_required_artifact_blocks_final_delivery(tmp_path: Path) -> None:
    """CONTROL: the final delivery gate still refuses an unfinished delivery.

    Offline surface: core/output_paths.py RunOutputLayout.begin_delivery_draft /
    publish_delivery -> core/delivery.py DeliveryManager.publish (:89-99).  The
    required_outputs authorization itself (core/governor.py:4936-4962) is inside the
    governor's acceptance-criteria builder and is not offline-drivable in isolation,
    so this control pins the publish refusal that gate feeds.
    """
    layout = RunOutputLayout(tmp_path / "run")
    layout.begin_delivery_draft()  # required artifact never materialized -> empty staging

    with pytest.raises(PermissionError, match="snapshot-bound"):
        layout.publish_delivery(authorized=False)
    with pytest.raises(PermissionError, match="snapshot-bound"):
        layout.publish_delivery(authorized=True)


# ================================================= budget floor / repair round (TOOLCHAIN P0-2)
# FINDINGS_TOOLCHAIN.md P0-2 + doc 51 §22 adjudication: a *planned* attempt keeps its declared
# ``max_tool_calls`` verbatim (runtime/task_execution_budget.py:122-126, pinned by
# tests/test_task_budget_defaults.py:44-48), so an undersized plan stays visible as
# ``budget_exhausted`` instead of being silently raised.  The floor applies only to
# framework-issued *repair* rounds (``recovery_of`` successors): trace.jsonl:23736 showed
# the r30 N-103 repair round re-adopting the spent ``max_tool_calls: 40`` and resuming at
# ``tool_calls_used: 40, tool_round: 0``, so it could not even write a file.


def test_declared_quota_passes_through_on_the_planned_attempt() -> None:
    """CONTROL: a planned attempt keeps its declared quota verbatim.

    Overriding a planner's declared cap would break the documented contract and
    hide planner undersizing; the repair-round floor must not leak into a normal
    dispatch.
    """
    budget, defaulted = ensure_task_budget(
        {"max_tool_calls": 1}, task_type="coder", execution_target="cloud"
    )
    assert budget["max_tool_calls"] == 1, (
        "a planned attempt must keep its declared cap: "
        f"budget={budget!r} (coder/cloud default is {default_max_tool_calls('coder', 'cloud')})"
    )
    assert defaulted is False


def test_repair_round_does_not_inherit_an_exhausted_quota() -> None:
    """REVERSAL: a framework-issued repair round must not re-adopt the spent small cap.

    Model note: the orchestrator's live repair wiring is not offline-drivable, so this
    models re-entry as "the same declared budget dict is fed to ensure_task_budget again
    with repair_round=True" — the flag wired at core/orchestrator.py:3033,
    core/governor.py:1368 and core/governor.py:3633 — and asserts the fresh cap is the
    role default, not the spent declared value.
    """
    declared = {"max_tool_calls": 1}
    first, _ = ensure_task_budget(declared, task_type="coder", execution_target="cloud")
    cap = first["max_tool_calls"]
    assert isinstance(cap, int)
    spent = TaskExecutionBudget(max_tool_calls=cap, max_seconds=0)
    for _ in range(cap):
        assert spent.admit_tool_call()[0] is True
    admitted, reason = spent.admit_tool_call()
    assert admitted is False and reason == "max_tool_calls"

    repair_budget, _ = ensure_task_budget(
        declared, task_type="coder", execution_target="cloud", repair_round=True
    )
    repair_cap = repair_budget["max_tool_calls"]
    assert isinstance(repair_cap, int)
    repair = TaskExecutionBudget(max_tool_calls=repair_cap, max_seconds=0)
    assert repair.admit_tool_call()[0] is True
    assert repair.max_tool_calls == 240, (
        "the repair round inherited the already-exhausted declared quota: "
        f"repair_budget={repair_budget!r} (coder/cloud default is 240)"
    )


def test_declared_quota_above_the_default_is_still_honoured() -> None:
    """CONTROL: max(declared, default) must not cap a deliberately generous declaration."""
    budget, _ = ensure_task_budget(
        {"max_tool_calls": 400, "max_seconds": 900},
        task_type="coder",
        execution_target="cloud",
    )
    assert budget["max_tool_calls"] == 400
    assert budget["max_seconds"] == 900