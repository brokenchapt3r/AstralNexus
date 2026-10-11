"""Verifier deterministic acceptance dimension (doc 17 第一批④): code-decidable
facts become an independent hard gate, and the semantic judge receives the
observations so it never re-guesses what code already decided."""
from __future__ import annotations

import json

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier, VerificationVerdict, VerificationDimension
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.trace import TraceRecorder


def _verifier(tmp_path, graph=None, client=None):
    return Verifier(
        graph or TaskGraph(), TraceRecorder(tmp_path),
        llm_client=client, workspace_root=tmp_path,
    )


def test_live_constraint_dimension_uses_file_fact_not_summary_words(tmp_path):
    (tmp_path / "proof.txt").write_text("real", encoding="utf-8")
    node = TaskNode("proof", "create proof", "coder", acceptance_criteria=["file:proof.txt non-empty"],
                    metadata={"evidence_packet": {"summary": "完全不同的措辞"}})
    dim = _verifier(tmp_path, client=object())._verify_constraint_satisfaction(node)
    assert dim.passed and dim.score == 1.0
    (tmp_path / "proof.txt").unlink()
    semantic = VerificationDimension("goal_coverage", 1.0, True)
    dim = _verifier(tmp_path, client=object())._verify_constraint_satisfaction(
        node, semantic_dimension=semantic)
    assert not dim.passed and dim.score == 0.0


def test_unknown_constraint_reuses_independent_semantic_decision_without_extra_call(tmp_path):
    node = TaskNode("doc", "write documentation", "coder", acceptance_criteria=["文档解释准确"])
    verifier = _verifier(tmp_path, client=object())
    for passed, score in [(True, 0.98), (False, 0.2)]:
        semantic = VerificationDimension("goal_coverage", score, passed, "independent evidence")
        dim = verifier._verify_constraint_satisfaction(node, semantic_dimension=semantic)
        assert dim.passed is passed and dim.score == score


def test_materialized_expected_output_holds(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "design.md").write_text("# design", encoding="utf-8")
    node = TaskNode(
        node_id="D-1", goal="write design", role="coder",
        metadata={"expected_outputs": ["docs/design.md", "artifact:design"]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None and dim.score == 1.0 and dim.passed
    # artifact: ids are not file leaves; only the path output is checked
    assert [o["criterion_id"] for o in dim.observations] == ["D-1.EO01"]
    assert dim.observations[0]["kind"] == "file_non_empty"


def test_missing_expected_output_is_hard_gate(tmp_path):
    """A declared output that never materialized outvotes a high semantic
    score — code-decided facts are an independent delivery gate."""
    graph = TaskGraph()
    node = TaskNode(
        node_id="D-2", goal="write design", role="coder", status="result_submitted",
        metadata={
            "expected_outputs": ["docs/design.md"],
            "evidence_packet": {"summary": "design written; all passed"},
        },
    )
    graph.add_node(node)

    class Client:
        def chat(self, *args, **kwargs):
            return json.dumps({"score": 0.95, "details": "looks complete", "blocking_gaps": []})

    verifier = _verifier(tmp_path, graph, Client())
    result = verifier.verify_node("D-2")
    dim = next(d for d in result.dimensions if d.name == "deterministic_acceptance")
    assert dim.score == 0.0
    assert dim.observations[0]["reason"] == "FILE_NOT_FOUND"
    assert result.verdict == VerificationVerdict.REPLAN_REQUIRED


def test_undecidable_only_criteria_do_not_add_dimension(tmp_path):
    graph = TaskGraph()
    node = TaskNode(
        node_id="A-1", goal="design", role="architect", status="result_submitted",
        acceptance_criteria=["状态迁移表完整准确"],
        metadata={"evidence_packet": {"summary": "done"}},
    )
    graph.add_node(node)

    class Client:
        def chat(self, *args, **kwargs):
            return json.dumps({"score": 0.9, "details": "ok", "blocking_gaps": []})

    verifier = _verifier(tmp_path, graph, Client())
    assert verifier._verify_deterministic_acceptance(node) is None
    result = verifier.verify_node("A-1")
    assert "deterministic_acceptance" not in {d.name for d in result.dimensions}


def test_tests_passed_criterion_anchors_to_recorded_execution(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin("T-1", {"owned_paths": []})
    store.append(execution, {
        "tool": "terminal_exec", "call_id": "c1", "status": "success", "returncode": 0,
        "stdout_tail": "===== 5 passed in 1.2s =====", "files": {},
        "arguments": json.dumps({"command": "pytest tests"}),
    })
    store.finish(execution, "succeeded", {"summary": "tests ran"})
    node = TaskNode(
        node_id="T-1", goal="make tests pass", role="tester",
        acceptance_criteria=["tests_passed:pytest tests"],
        metadata={"runtime_execution_id": execution},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None and dim.score == 1.0
    observation = dim.observations[0]
    assert observation["checked"] is True and observation["holds"] is True
    assert observation["receipt_ids"]


def test_claimed_pass_without_execution_is_unverified_not_holds(tmp_path):
    """退出码/口头声明不能替代执行：no recorded execution -> UNVERIFIED,
    and an UNVERIFIED-only node adds no dimension (semantic layer judges)."""
    node = TaskNode(
        node_id="T-2", goal="make tests pass", role="tester",
        acceptance_criteria=["tests_passed:pytest tests"],
        metadata={"evidence_packet": {"summary": "pytest tests: 5 passed"}},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is None
    observations = _verifier(tmp_path)._deterministic_observations(node)
    assert observations[0]["checked"] is False
    assert observations[0]["reason"] == "TEST_EXECUTION_NOT_FOUND"


def test_semantic_payload_carries_deterministic_observations(tmp_path):
    (tmp_path / "report.md").write_text("x", encoding="utf-8")
    captured = {}

    class Client:
        def chat(self, messages, **kwargs):
            captured["payload"] = json.loads(messages[-1]["content"])
            return json.dumps({"score": 0.8, "details": "ok", "blocking_gaps": []})

    node = TaskNode(
        node_id="R-1", goal="report", role="coder",
        acceptance_criteria=["file:report.md exists", "结论正确"],
        metadata={"evidence_packet": {"summary": "written"}},
    )
    verifier = _verifier(tmp_path, client=Client())
    verifier._verify_goal_coverage(node)
    payload = captured["payload"]
    by_id = {item["criterion_id"]: item for item in payload["deterministic_observations"]}
    assert by_id["R-1.AC01"]["checked"] is True and by_id["R-1.AC01"]["holds"] is True
    assert by_id["R-1.AC02"]["reason"] == "UNDECIDABLE_IN_CODE"
    assert "blocking_gaps" in payload["deterministic_rule"]
    assert "不得推翻" in payload["deterministic_rule"]
