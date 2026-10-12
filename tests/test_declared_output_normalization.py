"""起 A tightening: only an EXPLICIT field may relax a declared output.

doc b26 C1 + ``DESIGN_ARTIFACT_CONTRACT`` §4: 无法判定的条件按必需处理.  A prose
declaration therefore never exempts itself — natural language such as 若/如果
inside a goal sentence is not a contract (user m02366).  A conditional clause
that IS locatable in the prose must be normalized into structured fields by
``normalize_declared_output`` so the relaxation is explicit, auditable and
decidable downstream (交付契约 / 物化检查 / Verifier),
instead of being a silent, whole-sentence exemption.
"""
from __future__ import annotations

from AstralNexus.core.output_paths import (
    declared_output_is_required,
    normalize_declared_output,
)
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.trace import TraceRecorder

# Live r30 R-107 style declaration: prose, condition clause, single file claim.
R30_GOAL_CLAUSE = (
    "巡检浏览器流程；若发现缺陷则输出 deliverables/runs/browser/defects.json"
)
# Mixed long sentence: a REQUIRED artifact and a conditional one in one string.
MIXED_CLAUSE = (
    "必须生成 deliverables/app/product.py，"
    "若发现缺陷再生成 deliverables/runs/api/defects.json"
)

CONDITIONAL_ARTIFACT = {
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
REQUIRED_ARTIFACT = {
    "output_id": "O-01",
    "name": "required deliverable",
    "type": "file",
    "path": "deliverables/reports/defects.json",
    "required": True,
    "source": "explicit",
}


def _verifier(tmp_path, graph=None):
    return Verifier(
        graph or TaskGraph(), TraceRecorder(tmp_path),
        llm_client=None, workspace_root=tmp_path,
    )


# --- ① explicit relaxation stays possible -----------------------------------

def test_explicit_dict_required_false_is_relaxed():
    assert declared_output_is_required(CONDITIONAL_ARTIFACT) is False
    assert declared_output_is_required({"path": "x.json", "required": False}) is False
    assert declared_output_is_required({"path": "x.json", "required": "false"}) is False
    assert declared_output_is_required({"path": "x.json", "source": "optional"}) is False
    assert declared_output_is_required({"path": "x.json", "condition": {"kind": "no_defects"}}) is False


# --- ② explicit required stays hard, goal wording does not matter -----------

def test_explicit_dict_required_true_is_hard_even_in_a_conditional_goal():
    """The goal sentence 若发现缺陷 must not exempt an explicit required:True."""
    assert declared_output_is_required(REQUIRED_ARTIFACT) is True
    assert declared_output_is_required({"path": "x.json", "required": True, "source": "explicit"}) is True
    normalized = normalize_declared_output(REQUIRED_ARTIFACT)
    assert normalized is not None and normalized["required"] is True


def test_dict_without_any_explicit_signal_stays_required():
    assert declared_output_is_required({"path": "deliverables/app/x.py"}) is True
    assert declared_output_is_required({"path": "deliverables/app/x.py", "source": "explicit"}) is True


# --- ③ a locatable prose clause normalizes into an EXPLICIT condition -------

def test_prose_conditional_clause_normalizes_to_an_explicit_condition():
    """The evidence for this task: the r30 sentence no longer self-exempts, but
    its clause is normalizable into a decidable, auditable declaration."""
    # The raw prose is NOT silently relaxed …
    assert declared_output_is_required(R30_GOAL_CLAUSE) is True
    # … it is normalized instead.
    normalized = normalize_declared_output(R30_GOAL_CLAUSE)
    assert normalized is not None
    assert normalized["required"] is False
    assert normalized["source"] == "prose_conditional"
    assert normalized["path"] == "deliverables/runs/browser/defects.json"
    assert normalized["condition"] == {
        "kind": "prose_clause",
        "text": "若发现缺陷则输出 deliverables/runs/browser/defects.json",
        "when_false": "structured_empty_manifest",
    }
    # The normalized form is what the downstream gates can decide on.
    assert declared_output_is_required(normalized) is False


def test_english_and_clause_only_prose_forms_normalize():
    for text, clause in (
        ("若发现缺陷则输出 deliverables/runs/browser/defects.json",
         "若发现缺陷则输出 deliverables/runs/browser/defects.json"),
        ("若发现缺陷，则输出 deliverables/runs/browser/defects.json",
         "若发现缺陷，则输出 deliverables/runs/browser/defects.json"),
        ("if defects are found write deliverables/runs/browser/defects.json",
         "if defects are found write deliverables/runs/browser/defects.json"),
    ):
        normalized = normalize_declared_output(text)
        assert normalized is not None, text
        assert normalized["source"] == "prose_conditional"
        assert normalized["condition"]["text"] == clause


# --- ④ plain prose / no clause stays REQUIRED -------------------------------

def test_plain_prose_declaration_stays_required():
    for text in (
        "deliverables/app/requirements.txt",
        "deliverables/app/backend/ 下的后端源码与 schema.sql",
        "生成 deliverables/app/product.py",
    ):
        assert declared_output_is_required(text) is True, text
        assert normalize_declared_output(text) is None, text
    assert declared_output_is_required("") is True


# --- ⑤ a mixed sentence is never relaxed as a whole -------------------------

def test_mixed_sentence_is_never_relaxed_as_a_whole():
    """必须生成 A，若发现缺陷再生成 B: A is required and B cannot be attributed
    per-clause into ONE declaration, so the whole declaration stays required
    (the conservative reading of 无法判定即必需)."""
    assert declared_output_is_required(MIXED_CLAUSE) is True
    assert normalize_declared_output(MIXED_CLAUSE) is None


def test_clause_without_a_file_claim_is_not_relaxed():
    """A directory-only token cannot be turned into an artifact."""
    text = "巡检浏览器流程；若发现缺陷则输出 deliverables/runs/browser/"
    assert declared_output_is_required(text) is True
    assert normalize_declared_output(text) is None


def test_condition_word_inside_a_word_is_not_a_clause():
    """若干 must not read as the marker 若 opening a clause."""
    text = "生成 deliverables/app/product.py 与若干配置文件"
    assert declared_output_is_required(text) is True
    assert normalize_declared_output(text) is None


# --- ⑥ required prose still yields a hard verifier leaf ---------------------

def test_prose_required_declaration_still_produces_a_hard_leaf(tmp_path):
    node = TaskNode(
        node_id="N-402", goal="backend", role="coder",
        metadata={"expected_outputs": ["deliverables/app/missing.py"]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None and dim.passed is False
    leaves = [o for o in dim.observations if o["checked"] and o["holds"] is False]
    assert [o["reason"] for o in leaves] == ["FILE_NOT_FOUND"]
    assert [o["kind"] for o in leaves] == ["file_non_empty"]


def test_normalized_prose_condition_is_not_a_postcondition(tmp_path):
    """Once the normalized declaration is wired through, the absent conditional
    artifact no longer creates a failing leaf."""
    app = tmp_path / "deliverables" / "app"
    app.mkdir(parents=True)
    (app / "requirements.txt").write_text("flask\n", encoding="utf-8")
    node = TaskNode(
        node_id="N-403",
        goal="巡检浏览器流程；若发现缺陷则输出结构化缺陷单",
        role="coder",
        acceptance_criteria=["file:deliverables/app/requirements.txt non-empty"],
        metadata={"expected_outputs": [normalize_declared_output(R30_GOAL_CLAUSE)]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None and dim.passed is True
    assert [o for o in dim.observations if o["checked"] and o["holds"] is False] == []