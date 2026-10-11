"""起 A wiring: a prose-conditional declaration becomes a STRUCTURED entry.

``normalize_declared_output`` (core/output_paths.py) already classified a
locatable prose condition, but nothing called it on the live path: the
Governor's ``declared_output_items`` string branch (core/governor.py) kept every
non-dict declaration as a plain string, so ``declared_output_is_required`` found
no field to read and the conditional artifact stayed a HARD postcondition (live
r30 R-107: an otherwise green execution was failed and rerun for a correctly
absent ``defects.json``).

These cases drive the real admission entry point — ``GlobalGovernor.
apply_task_graph_patch`` reaches ``declared_output_items`` — and then check what
the Verifier reads back from the admitted node.  Pure and offline: no network,
no subprocess, no LLM.  The classification rules in ``core/output_paths.py`` are
frozen and unchanged; only the string branch of ``declared_output_items`` now
asks the normalizer first.
"""
from __future__ import annotations

from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.output_paths import declared_output_is_required
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.trace import TraceRecorder

# Live r30 R-107 shape: prose, a locatable conditional clause, one file claim.
PROSE_CONDITIONAL = (
    "巡检浏览器流程；若发现缺陷则输出 deliverables/runs/browser/defects.json"
)
# Mixed long sentence naming a REQUIRED artifact and a conditional one.
MIXED_SENTENCE = (
    "必须生成 deliverables/app/product.py，"
    "若发现缺陷再生成 deliverables/runs/api/defects.json"
)


def _governor(tmp_path):
    """Same admission harness the ownership/contract suites already use."""
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="running",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def _expected_outputs(graph, node_id):
    return graph.get_node(node_id).metadata["expected_outputs"]


def _verifier(graph, tmp_path):
    return Verifier(
        graph, TraceRecorder(tmp_path), llm_client=None, workspace_root=tmp_path,
    )


# --- ① prose-conditional in ⇒ structured dict out ---------------------------

def test_prose_conditional_declaration_is_admitted_as_a_structured_entry(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "browser-scan",
        "goal": "巡检浏览器流程",
        "owned_paths": ["deliverables/runs/browser/"],
        "expected_outputs": [PROSE_CONDITIONAL],
    }]))
    outputs = _expected_outputs(graph, "browser-scan")
    assert len(outputs) == 1
    entry = outputs[0]
    assert isinstance(entry, dict)
    assert entry["required"] is False
    assert entry["source"] == "prose_conditional"
    assert entry["path"] == "deliverables/runs/browser/defects.json"
    # The relaxation is auditable: the clause is quoted verbatim and carries a
    # decidable when_false, instead of being a silent whole-sentence exemption.
    assert entry["condition"]["kind"] == "prose_clause"
    assert entry["condition"]["text"] == (
        "若发现缺陷则输出 deliverables/runs/browser/defects.json"
    )
    assert entry["condition"]["when_false"] == "structured_empty_manifest"
    assert declared_output_is_required(entry) is False


# --- ② regression guard: an unconditional plain path is untouched ----------

def test_unconditional_plain_path_stays_a_plain_string(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "backend",
        "goal": "implement the backend service",
        "owned_paths": ["deliverables/app/backend/"],
        "expected_outputs": ["deliverables/app/backend/schema.sql"],
    }]))
    outputs = _expected_outputs(graph, "backend")
    assert outputs == ["deliverables/app/backend/schema.sql"]
    assert isinstance(outputs[0], str)
    assert declared_output_is_required(outputs[0]) is True


# --- ③ documented boundary: no per-clause splitting -------------------------

def test_mixed_two_path_sentence_is_not_split_and_stays_required(tmp_path):
    """必须生成 A，若发现缺陷再生成 B names TWO files.  ``normalize_declared_output``
    refuses it (one declaration, two claims), so the string branch keeps it
    verbatim and the declaration stays REQUIRED — the conservative reading of
    无法判定即必需.  The test only asserts containment: the Governor's existing
    path resolution may re-root a prose string, which this wiring does not touch.
    """
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "product",
        "goal": "build the product and the conditional defect report",
        "expected_outputs": [MIXED_SENTENCE],
    }]))
    outputs = _expected_outputs(graph, "product")
    assert len(outputs) == 1
    assert isinstance(outputs[0], str)
    assert "deliverables/app/product.py" in outputs[0]
    assert "deliverables/runs/api/defects.json" in outputs[0]
    assert declared_output_is_required(outputs[0]) is True


# --- ④ dedupe tolerates the mixed shapes the new branch produces -----------

def test_dedupe_tolerates_a_mix_of_structured_and_plain_declarations(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "browser-scan",
        "goal": "巡检浏览器流程",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/runs/browser/"],
        "expected_outputs": ["deliverables/runs/browser/report.md"],
    }]))
    for status in ("ready", "running", "failed"):
        graph.update_node_status("browser-scan", status)
    # A recovery successor inherits the predecessor's declarations and dedupes
    # the union (core/governor.py: dedupe_declared_outputs call site).  The mix
    # here is one plain string plus one dict from the new prose branch.
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "browser-scan-recovery",
        "goal": "巡检浏览器流程（重试）",
        "dependencies": ["root"],
        "recovery_of": "browser-scan",
        "owned_paths": ["deliverables/runs/browser/"],
        "expected_outputs": [PROSE_CONDITIONAL],
    }]))
    outputs = _expected_outputs(graph, "browser-scan-recovery")
    assert [type(item).__name__ for item in outputs] == ["str", "dict"]
    assert outputs[0] == "deliverables/runs/browser/report.md"
    assert outputs[1]["source"] == "prose_conditional"
    assert outputs[1]["required"] is False


# --- ⑤ structured-in/structured-out through the Verifier -------------------

def test_governed_optional_declaration_yields_an_optional_verifier_leaf(tmp_path):
    """The dict the Governor now admits is read field-first: an absent
    conditional artifact keeps an UNVERIFIED audit leaf and is never a checked
    ``file:`` gap."""
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "browser-scan",
        "goal": "巡检浏览器流程",
        "owned_paths": ["deliverables/runs/browser/"],
        "expected_outputs": [PROSE_CONDITIONAL],
    }]))
    observations = _verifier(graph, tmp_path).deterministic_observations(
        graph.get_node("browser-scan")
    )
    assert [o for o in observations if o["kind"] == "file_non_empty"] == []
    optional = [
        o for o in observations
        if str(o["criterion"]).startswith("optional_artifact:")
    ]
    assert len(optional) == 1
    assert optional[0]["criterion"] == (
        "optional_artifact:deliverables/runs/browser/defects.json"
    )
    assert optional[0]["checked"] is False
    assert optional[0]["holds"] is None
    assert optional[0]["reason"] == "UNDECIDABLE_IN_CODE"


def test_governed_required_declaration_still_yields_a_hard_leaf(tmp_path):
    """The other direction of ⑤: wiring the normalizer must not soften a plain
    required declaration, which keeps its checked/holds=False file leaf."""
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "backend",
        "goal": "implement the backend service",
        "owned_paths": ["deliverables/app/backend/"],
        "expected_outputs": ["deliverables/app/backend/missing.py"],
    }]))
    dim = _verifier(graph, tmp_path)._verify_deterministic_acceptance(
        graph.get_node("backend")
    )
    assert dim is not None and dim.passed is False
    leaves = [o for o in dim.observations if o["checked"] and o["holds"] is False]
    assert [o["reason"] for o in leaves] == ["FILE_NOT_FOUND"]
    assert [o["kind"] for o in leaves] == ["file_non_empty"]