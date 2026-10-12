"""Path identity (Fix S1, b32_path_identity): one root, one identity, one decision.

The framework used to hold several definitions of the same fact: the goal text,
``expected_outputs``, ``expected_artifact_paths``, ``owned_paths`` and the
acceptance criteria each derived their own path string, so any small difference in
model output manufactured a conflict that was not a real failure.  The live B+
run ``task-1791256754557`` is the witness: node ``C-01`` owned
``deliverables/docs/contract.md`` while its two criterion leaves said
``docs/contract.md``; the 40 006-byte deliverable was on disk, the leaves were
resolved literally, both reported ``FILE_NOT_FOUND``, ``C-01`` was failed, an
``R-C-01`` recovery node was added, seven downstream nodes were rewired
(``recovery_churn`` 14, zero delivered nodes).

This file pins the contraction: one canonicalization function in
``core/output_paths.py``, relocation only inside a node's declared path set, and
an explicit loud rejection instead of a filesystem search for a same-named file.
"""

import inspect
from pathlib import Path

import pytest

from AstralNexus.core import output_paths
from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.output_paths import (
    PathIdentityError,
    canonical_relative,
    declared_relative,
    identity_key,
    workspace_identity,
    workspace_relative,
)
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
from AstralNexus.runtime.acceptance_checks import bind_criterion_paths
from AstralNexus.runtime.file_write_facts import relative_receipt_path
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.runtime.verification_identity import (
    declared_path_identity,
    path_tokens,
    premise_versions,
    verification_identity,
)

HERE = Path(__file__).resolve().parent
ASTRA = Path(str(__import__('AstralNexus').__file__)).resolve().parent


def test_live_b73_command_path_binds_before_receipt_matching(tmp_path):
    """The admitted script path and the recorded argv name one artifact."""
    import json

    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "adder", "goal": "implement adder", "role": "coder",
        "owned_paths": ["main.py"], "expected_outputs": ["main.py"],
        "acceptance_criteria": ["tests_passed:python main.py 2 3"],
    }]))
    criteria = graph.get_node("adder").acceptance_criteria
    assert criteria == ["tests_passed:python deliverables/app/main.py 2 3"]
    receipt = {"tool": "terminal_exec", "status": "success", "returncode": 0,
               "stdout_tail": "5\n", "receipt_id": "actual-command",
               "arguments": json.dumps({"command": ["python", "deliverables/app/main.py", "2", "3"]})}
    observation = run_acceptance_checks(criteria, receipts=[receipt])[0]
    # Path binding must not weaken the separate test-summary requirement.
    assert observation["reason"] == "TEST_SUMMARY_NOT_RECOGNIZED"
    assert observation["receipt_ids"] == ["actual-command"]
    assert observation["holds"] is None


@pytest.mark.parametrize("command,expected", [
    ("python main.py 2 3", "python deliverables/app/main.py 2 3"),
    ("python main.py main.py", "python deliverables/app/main.py main.py"),
    ("python -m pytest tests/test_main.py::test_add -q",
     "python -m pytest deliverables/app/tests/test_main.py::test_add -q"),
    ("python -m unittest discover -s tests -v",
     "python -m unittest discover -s deliverables/app/tests -v"),
    ('python -c "print(\'main.py\')"', 'python -c "print(\'main.py\')"'),
    ('echo "python main.py 2 3"', 'echo "python main.py 2 3"'),
    ("python elsewhere/main.py 2 3", "python elsewhere/main.py 2 3"),
])
def test_command_binding_only_relocates_declared_execution_targets(command, expected):
    bound, _ = bind_criterion_paths(["tests_passed:" + command], declared=[
        "deliverables/app/main.py", "deliverables/app/tests",
        "deliverables/app/tests/test_main.py",
    ])
    assert bound == ["tests_passed:" + expected]


def test_command_binding_rejects_ambiguity_and_escape():
    for command, declared in [
        ("python check.py", ["deliverables/a/check.py", "deliverables/b/check.py"]),
        ("python ../check.py", ["deliverables/app/check.py"]),
    ]:
        with pytest.raises(PathIdentityError):
            bind_criterion_paths(["tests_passed:" + command], declared=declared)
        assert bind_criterion_paths(["tests_passed:" + command], declared=declared,
                                    strict=False)[0] == ["tests_passed:" + command]


def test_runtime_output_does_not_turn_coverage_prose_into_a_file():
    from AstralNexus.core.output_paths import runtime_output_claims
    assert runtime_output_claims("positive/negative/zero", ["deliverables/app/test_main.py"]) == []
    assert runtime_output_claims("覆盖 positive/negative/zero 情况") == []
    assert runtime_output_claims("deliverables/app/test_main.py") == ["deliverables/app/test_main.py"]
    assert runtime_output_claims("实现 deliverables/app/test_main.py 覆盖 positive/negative/zero") == ["deliverables/app/test_main.py"]
    assert runtime_output_claims("deliverables/app/") == ["deliverables/app/"]
    assert runtime_output_claims("docs/output", ["docs/output"]) == ["docs/output"]
    assert runtime_output_claims("README") == ["README"]

#: The one file the live C-01 node actually delivered (40 006 bytes there).
DECLARED = "deliverables/docs/contract.md"


def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="running",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def _c01_node(**overrides):
    """The live C-01 declaration: owned name plus the two criteria that failed."""
    node = {
        "node_id": "C-01",
        "goal": "write the frozen delivery contract",
        "role": "coder",
        "task_type": "implementation",
        "owned_paths": [DECLARED],
        "acceptance_criteria": [
            "file_written:docs/contract.md",
            "file:docs/contract.md non-empty",
        ],
    }
    node.update(overrides)
    return node


def _c01_patch(**overrides):
    return TaskGraphPatch(add_nodes=[_c01_node(**overrides)])


def _observed(criteria, root):
    """``(criterion, holds, reason)`` for each criterion leaf, code-decided."""
    observations = run_acceptance_checks(
        list(criteria), receipts=[], execution_id="e-c01", root=Path(root),
    )
    return [(item["criterion"], item["holds"], item["reason"]) for item in observations]


def _write(root, relative, text="payload"):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ① one canonicalization function -------------------------------------------------


def test_one_file_six_spellings_is_one_identity(tmp_path):
    """Every spelling of one declared file canonicalizes to the same identity."""
    spellings = [
        "deliverables/docs/contract.md",
        "./deliverables/docs/contract.md",
        ".\\deliverables/docs/contract.md",
        "deliverables\\docs\\contract.md",
        "deliverables/docs/contract.md/",
        "DELIVERABLES/Docs/Contract.MD",
        "./deliverables//docs/./contract.md",
    ]
    bound = [
        workspace_identity(str(tmp_path), spelling, declared=[DECLARED])
        for spelling in spellings
    ]
    assert [item.rel for item in bound] == [DECLARED] * len(spellings)
    assert {item.key for item in bound} == {DECLARED}
    assert {item.status for item in bound} == {"exact"}
    # declared_directory is a property of the spelling, not a second identity
    assert [item.declared_directory for item in bound] == [
        False, False, False, False, True, False, False,
    ]
    # the escape/comparison helpers agree, and every identity sits under ONE root
    assert {identity_key(spelling) for spelling in spellings} == {DECLARED}
    # canonical_relative keeps the spelling's case; only the comparison key folds it
    # (which is why identity_key -- not the display form -- is what gets compared).
    assert {canonical_relative(spelling, root=str(tmp_path)).casefold()
            for spelling in spellings} == {DECLARED}
    assert canonical_relative("./deliverables//docs/./contract.md", root=str(tmp_path)) == DECLARED
    assert all(item.as_path().is_relative_to(tmp_path) for item in bound)
    assert declared_relative("deliverables\\docs\\contract.md", root=str(tmp_path)) == DECLARED


# ② the C-01 live case, before and after admission ---------------------------------


def test_c01_live_criteria_fail_before_binding_and_hold_after_admission(tmp_path):
    _write(tmp_path, DECLARED, "c" * 40006)
    live = list(_c01_node()["acceptance_criteria"])

    # BEFORE: the criteria name a path that the identity plane still reads literally.
    assert _observed(live, tmp_path) == [
        ("file_written:docs/contract.md", False, "FILE_NOT_FOUND"),
        ("file:docs/contract.md non-empty", False, "FILE_NOT_FOUND"),
    ]

    # ADMISSION: the declared set relocates both leaves to the file C-01 owns.
    graph, governor = _governor(tmp_path)
    result = governor.apply_task_graph_patch(_c01_patch())

    node = graph.get_node("C-01")
    assert list(node.acceptance_criteria) == [
        "file_written:deliverables/docs/contract.md",
        "file:deliverables/docs/contract.md non-empty",
    ]
    assert list(node.owned_paths) == [DECLARED]
    assert result["ownership_normalizations"] == [
        {"from": "docs/contract.md", "to": DECLARED, "reason": "criterion_path_relocated"},
        {"from": "docs/contract.md", "to": DECLARED, "reason": "criterion_path_relocated"},
    ]
    # the relocation is recorded inside the existing event payload (no new event type)
    recorded = [
        event for event in governor.trace_recorder.events
        if event["event_type"] == "governor.graph_patch.applied"
    ][-1]
    assert recorded["payload"]["ownership_normalizations"] == result["ownership_normalizations"]

    # AFTER: the very same two leaves now decide on the file that exists.
    assert _observed(node.acceptance_criteria, tmp_path) == [
        ("file_written:deliverables/docs/contract.md", True, ""),
        ("file:deliverables/docs/contract.md non-empty", True, ""),
    ]


def test_relocation_never_invents_existence(tmp_path):
    """A relocated identity is still checked on disk: absence stays FILE_NOT_FOUND."""
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(_c01_patch())
    bound = list(graph.get_node("C-01").acceptance_criteria)
    assert [item[1:] for item in _observed(bound, tmp_path)] == [
        (False, "FILE_NOT_FOUND"),
        (False, "FILE_NOT_FOUND"),
    ]


def test_admission_binds_declared_output_claims_as_well(tmp_path):
    """Expected outputs go through the same identity as the criteria."""
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[_c01_node(
        owned_paths=[DECLARED],
        expected_outputs=[{"output_id": "O-01", "kind": "document",
                           "path": "docs/contract.md", "required": True}],
        acceptance_criteria=["file_written:docs/contract.md"],
    )]))
    node = graph.get_node("C-01")
    assert list(node.acceptance_criteria) == ["file_written:deliverables/docs/contract.md"]
    assert list(node.owned_paths) == [DECLARED]


# ③ loud rejection instead of guessing --------------------------------------------


def test_ambiguous_declared_suffix_is_rejected_with_candidates_and_root(tmp_path):
    declared = ["deliverables/a/report.md", "deliverables/b/report.md"]
    with pytest.raises(PathIdentityError) as error:
        workspace_identity(str(tmp_path), "report.md", declared=declared)
    assert error.value.reason == "ambiguous_declared_suffix"
    assert error.value.raw == "report.md"
    assert list(error.value.candidates) == declared
    # the message is a repr-formatted diagnosis: raw path, root and every candidate
    message = str(error.value)
    assert repr("report.md") in message
    assert repr(str(tmp_path)) in message
    assert "against workspace root" in message
    assert "deliverables/a/report.md" in message and "deliverables/b/report.md" in message


@pytest.mark.parametrize(("raw", "reason"), [
    ("../outside.md", "escapes_workspace_root"),
    ("deliverables/../../outside.md", "escapes_workspace_root"),
    ("/etc/passwd", "absolute_path"),
    ("~/outside.md", "absolute_path"),
    ("C:\\Windows\\system32\\drivers\\etc\\hosts", "absolute_path"),
])
def test_out_of_bounds_paths_are_rejected_with_the_raw_path(tmp_path, raw, reason):
    with pytest.raises(PathIdentityError) as error:
        canonical_relative(raw, root=str(tmp_path))
    assert error.value.reason == reason
    assert error.value.raw == raw
    message = str(error.value)
    assert repr(raw) in message
    assert repr(str(tmp_path)) in message


def test_admission_refuses_the_whole_patch_on_an_escaping_owned_path(tmp_path):
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError) as error:
        governor.apply_task_graph_patch(_c01_patch(owned_paths=["../outside.md"]))
    assert "../outside.md" in str(error.value)
    assert graph.get_node("C-01") is None


def test_an_undeclared_fresh_path_never_rejects_the_patch(tmp_path):
    """status="unbound" is a normal state: new files are declared, not refused."""
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(_c01_patch(
        owned_paths=["deliverables/app/serve.py"],
        acceptance_criteria=["file:deliverables/app/serve.py exists"],
    ))
    node = graph.get_node("C-01")
    assert list(node.owned_paths) == ["deliverables/app/serve.py"]
    # Nothing is declared for a bare name in a *different* node: it stays itself.
    assert workspace_identity(str(tmp_path), "serve.py").status == "unbound"


# ④ no filesystem same-name search, no disk preference ----------------------------


def test_a_same_named_file_elsewhere_never_satisfies_the_criterion(tmp_path):
    """The decoy exists on disk; the identity plane still refuses to adopt it."""
    _write(tmp_path, "tmp/contract.md", "decoy")
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(_c01_patch())
    bound = list(graph.get_node("C-01").acceptance_criteria)
    assert [item[1:] for item in _observed(bound, tmp_path)] == [
        (False, "FILE_NOT_FOUND"),
        (False, "FILE_NOT_FOUND"),
    ]


def test_relocation_uses_the_declared_set_not_the_shallowest_match(tmp_path):
    _write(tmp_path, "deliverables/docs/notes.md", "declared")
    _write(tmp_path, "deliverables/other/docs/notes.md", "sibling")
    bound = workspace_identity(
        str(tmp_path), "docs/notes.md",
        declared=["deliverables/other/docs/notes.md", "deliverables/docs/notes.md"],
    )
    # ``docs/notes.md`` is already the namespace form of a declared file, so the
    # exact/namespace rule answers before suffix matching ever runs.
    assert bound.rel == "deliverables/docs/notes.md"
    assert bound.reason == "declared identity of the path"
    assert bound.status in {"exact", "relocated"}

    # A spelling that is NOT the namespace form reaches the suffix rule, which is
    # restricted to the declared set: ``deliverables/docs/`` is the only candidate.
    pure_suffix = workspace_identity(
        str(tmp_path), "shared/report.md",
        declared=["deliverables/docs/shared/report.md", "deliverables/other/notes.md"],
    )
    assert pure_suffix.rel == "deliverables/docs/shared/report.md"
    assert pure_suffix.reason == "unique suffix match among the declared paths"

    # Two declared files sharing the suffix make it ambiguous rather than a guess.
    with pytest.raises(PathIdentityError):
        workspace_identity(
            str(tmp_path), "notes.md",
            declared=["deliverables/docs/notes.md", "deliverables/other/docs/notes.md"],
        )


def test_the_identity_plane_decides_without_touching_the_filesystem():
    """The relocation helpers are graph-declaration-only: no glob, no exists()."""
    forbidden = ("rglob(", ".glob(", "iterdir(", "os.walk", "exists()", "is_file()", ".stat(")
    for name in (
        "canonical_parts", "canonical_relative", "declared_relative", "identity_key",
        "same_or_under", "paths_overlap", "declared_members", "declared_tree_member",
        "workspace_identity",
    ):
        body = inspect.getsource(getattr(output_paths, name))
        assert not [token for token in forbidden if token in body], name

    source = "\n".join(path.read_text(encoding="utf-8-sig")
                         for path in (ASTRA / "core" / "orchestration").glob("*.py"))
    # Comments explain why the old fallback was removed and therefore quote it; the
    # rule is about executable lines, so they are stripped before asserting.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "rglob(Path(token).name)" not in code
    assert "deliverables_root.rglob" not in code
    assert "Path(token).name" not in code

    # Independent fence: within the whole core package, directory walking stays where
    # it is already justified (publish/staging enumeration, "a declared directory is
    # non-empty", declared wildcard patterns, delivery progress) -- no *new* module
    # may start globbing for an undeclared file.  The classification is enumerated
    # in FIX_S1_PATH_IDENTITY.md §5.
    allowed = {
        "delivery.py", "delivery_snapshot.py", "delivery_acceptance.py", "output_paths.py", "governor.py", "orchestrator.py",
        "verifier.py", "skill_runtime.py", "layered_synthesis.py",
        # Existing Orchestrator enumeration moved into these three modules.
        "completion.py", "execution.py", "verification.py",
        # Existing CLI staging enumeration moved to the core delivery service.
        # Its roots still come from declared outputs, not basename discovery.
        "delivery_materialization.py", "delivery_pipeline.py",  # Explicit working/output enumeration, no basename search.
        "delivery_snapshot.py",
    }
    found = set()
    for path in sorted((ASTRA / "core").rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if any(token in line for token in
                   ("rglob(", ".glob(", "iterdir(", "os.walk")):
                found.add(f"{path.name}:{number}")
    assert found, "the enumeration must be reproducible, not empty"
    assert {hit.split(":")[0] for hit in found} <= allowed, sorted(found)


# ⑤ four-way reuse: coverage, prompt fields, receipts, verification identity -------


def test_owned_path_coverage_shares_the_identity_and_has_no_basename_fallback():
    assert GlobalGovernor._clause_path_covered(DECLARED, [DECLARED]) is True
    assert GlobalGovernor._clause_path_covered("deliverables/docs/deep/x.md", ["deliverables/docs"]) is True
    assert GlobalGovernor._clause_path_covered("contract.md", ["deliverables/other/contract.md"]) is False
    assert GlobalGovernor._clause_path_covered(
        DECLARED, ["deliverables/other/contract.md"]
    ) is False


def test_worker_prompt_fields_are_identities_after_admission(tmp_path):
    """agents/base.py:1297/:1303 print exactly these two node fields."""
    graph, governor = _governor(tmp_path)
    # A patch that carries the criteria ONLY in metadata: a real model often puts
    # them there, and the top-level key may simply be absent (never None -- the
    # committer iterates it).
    node_payload = _c01_node()
    del node_payload["acceptance_criteria"]
    node_payload["metadata"] = {"acceptance_criteria": ["file:docs/contract.md non-empty"]}
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[node_payload]))
    node = graph.get_node("C-01")
    prompt_owned = list(node.metadata.get("owned_paths") or [])
    prompt_criteria = list(node.metadata.get("acceptance_criteria") or [])
    assert prompt_owned == [DECLARED]
    assert prompt_criteria == ["file:deliverables/docs/contract.md non-empty"]
    assert all(
        path_tokens(criterion) == [DECLARED] for criterion in prompt_criteria
    )


def test_receipt_hashing_and_verification_identity_use_the_same_identity(tmp_path):
    _write(tmp_path, DECLARED, "c" * 40)
    assert relative_receipt_path("deliverables\\docs\\contract.md", tmp_path) == DECLARED
    assert relative_receipt_path("./deliverables/docs/contract.md", tmp_path) == DECLARED
    assert relative_receipt_path(DECLARED, tmp_path) == DECLARED
    assert relative_receipt_path("../outside.md", tmp_path) == ""
    assert workspace_relative(tmp_path, "deliverables\\docs\\contract.md") == DECLARED

    assert premise_versions(tmp_path, [DECLARED]) == premise_versions(
        tmp_path, ["./deliverables\\docs\\contract.md"]
    )
    assert list(premise_versions(tmp_path, [DECLARED])) == [DECLARED]

    left = verification_identity("pytest -q tests/x.py", {DECLARED: "sha256:abc"})
    right = verification_identity("pytest -q tests/x.py", {".\\deliverables\\docs\\Contract.MD": "sha256:abc"})
    assert left == right
    assert verification_identity("pytest -q tests/x.py", {DECLARED: "sha256:other"}) != left


def test_declared_path_identity_and_leaf_keywords_are_stripped_together():
    assert declared_path_identity("docs/contract.md", [DECLARED]) == DECLARED
    assert declared_path_identity("contract.md", [DECLARED]) == DECLARED
    assert declared_path_identity("report.md", ["deliverables/a/report.md", "deliverables/b/report.md"]) == "report.md"
    assert path_tokens("file_written:docs/contract.md") == ["docs/contract.md"]
    assert path_tokens("file:deliverables/docs/contract.md non-empty") == [DECLARED]
    assert path_tokens(
        "report_not_stale:deliverables/report.md differs_from:deliverables/baseline.json"
    ) == ["deliverables/report.md", "deliverables/baseline.json"]
