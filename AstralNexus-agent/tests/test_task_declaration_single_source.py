"""b33 S2 (declaration contraction): one structured task declaration.

These tests pin the two derivations that S2 collapsed into
``AstralNexus.core.task_declaration`` (the delivery-criterion id spelling and
the declared-output normalization), the two behaviour equivalences that made
the collapse legal, and the suffix table that used to be written twice.

They do not re-test framework features.  Each one fails if a *second* spelling,
a *second* normalization rule or a *second* copy of the suffix table comes
back — or if the collapse silently changed what an existing gate rejects.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from AstralNexus.core import orchestrator as orchestrator_module
from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.output_paths import identity_key
from AstralNexus.core.task_declaration import (
    criterion_id,
    criterion_leaf_id,
    declared_criterion_ids,
    declared_criterion_texts,
    declared_requirement,
)
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder


def _paths(raw):
    """Every candidate key of one declared entry (the gate's view)."""
    return declared_requirement(raw).paths


# --------------------------------------------------------------------------
# one spelling for a delivery criterion id (D2)
# --------------------------------------------------------------------------

def test_criterion_id_has_exactly_one_spelling():
    assert criterion_id("O-01", 3) == "O-01.C03"
    assert criterion_id("  O-01  ", 1) == "O-01.C01"
    assert criterion_id("O-01", 1, "EXPLICIT-9") == "EXPLICIT-9"
    assert criterion_id("O-01", 2, "   ") == "O-01.C02"
    # No output id and no explicit id: no derivable id at all — the same answer
    # the coverage gate gave before this landing.
    assert criterion_id("", 2) == ""
    assert criterion_id(None, 2) == ""


def test_criterion_leaf_id_is_the_single_spelling_for_every_family():
    """D2: all three leaf families are spelled by one formatter.

    ``core/verifier.py`` spelled ``<node>.AC<nn>`` and ``<node>.EO<nn>`` itself
    and ``runtime/acceptance_checks.py`` spelled the anonymous ``C<nn>`` label,
    so one criterion book had three id spellings.  These are the exact strings
    the three callers produced before the landing.
    """
    assert criterion_leaf_id("N-01", 2) == "N-01.C02"
    assert criterion_leaf_id("N-01", 2, "AC") == "N-01.AC02"
    assert criterion_leaf_id("N-01", 2, "EO") == "N-01.EO02"
    # An id-less entry still gets a label, but only from this one formatter.
    assert criterion_leaf_id("", 4) == "C04"
    assert criterion_leaf_id(None, 4) == "C04"
    # A declared id wins verbatim, in every family; a blank one is not an id.
    assert criterion_leaf_id("N-01", 2, "EO", "DECLARED-7") == "DECLARED-7"
    assert criterion_leaf_id("N-01", 2, "EO", "   ") == "N-01.EO02"


def test_declared_criterion_texts_read_through_the_single_spelling():
    """D2: the criterion-text reader is this module's, not the governor's own."""
    entries = [
        {"output_id": "O-01", "acceptance_criteria": ["first", "second"]},
        {
            "output_id": "O-02",
            "acceptance_criteria": ["third"],
            "acceptance_criterion_ids": ["DECLARED-3"],
        },
        {"acceptance_criteria": ["no output id"]},
        "not a dict",
    ]
    assert declared_criterion_texts(entries) == {
        "O-01.C01": "first",
        "O-01.C02": "second",
        "DECLARED-3": "third",
    }
    assert declared_criterion_texts(None) == {}


def test_declared_criterion_ids_keeps_declared_ids_verbatim():
    entry = {
        "output_id": "O-01",
        "acceptance_criteria": ["a", "b"],
        "acceptance_criterion_ids": ["X-1", "X-2"],
    }
    assert declared_criterion_ids(entry) == ["X-1", "X-2"]


def test_declared_criterion_ids_derives_from_the_leaves():
    entry = {"output_id": "O-02", "acceptance_criteria": ["a", "b", "c"]}
    assert declared_criterion_ids(entry) == ["O-02.C01", "O-02.C02", "O-02.C03"]
    # Nothing derivable, nothing invented.
    assert declared_criterion_ids({"acceptance_criteria": ["a"]}) == []
    assert declared_criterion_ids("not a dict") == []


# --------------------------------------------------------------------------
# one normalization for every declared-output shape (M1)
# --------------------------------------------------------------------------

def test_dict_and_str_declarations_normalize_to_one_key():
    spelling = "deliverables/app/server.py"
    assert _paths(spelling) == [identity_key(spelling)]
    assert _paths({"path": spelling}) == [identity_key(spelling)]
    assert _paths(
        {"path": spelling}
    ) == _paths(spelling)


@pytest.mark.parametrize(
    "spelling",
    [
        "deliverables/app/server.py",
        "Deliverables\\App\\Server.PY",
        "deliverables/app/./server.py",
        "deliverables//app/server.py",
    ],
)
def test_requirement_keys_live_on_the_identity_plane(spelling):
    """The module owns no normalization: every key IS an ``identity_key`` key.

    The verbatim spelling stays a candidate (the pre-landing gate compared on
    it), so an entry may yield more than one candidate — but the key for the
    spelled-out path is always among them and always produced by the single
    identity plane.
    """
    paths = _paths(spelling)
    assert identity_key(spelling) in paths
    assert paths == [identity_key(claim) for claim in paths]


def test_prose_tail_becomes_a_note_and_never_hides_the_path():
    raw = "deliverables/docs/contract.md（交付说明，勿裁撤）"
    requirement = declared_requirement(raw)
    # The declared file is readable even though prose follows it...
    assert identity_key("deliverables/docs/contract.md") in requirement.paths
    # ...and the prose tail is recorded, never re-parsed into a second path.
    assert requirement.note == raw
    assert identity_key(raw) in requirement.paths


def test_unreadable_declaration_keeps_its_verbatim_key():
    """A declaration with no readable path still reaches the old verdict.

    Its key is the folded verbatim text, which has no suffix, so the ownership
    gate's existing ``not Path(...).suffix`` skip still fires exactly as before.
    """
    text = "write the acceptance report"
    paths = _paths(text)
    assert paths == [identity_key(text)]
    assert not Path(paths[0]).suffix


def test_path_spelling_is_the_entrys_own_single_spelling():
    """The pre-landing comparison input is reproduced, not re-derived.

    A string entry is compared as its whole spelling (so its prose tail still
    decides whether the entry is a path at all); a dict entry is compared as the
    path its own field holds.  This is what lets the gate below keep its old
    skip rule without owning a fourth normalization.
    """
    text = "deliverables/docs/contract.md（交付说明）"
    assert declared_requirement(text).path_spelling == text
    assert declared_requirement({"path": "deliverables/docs/contract.md"}).path_spelling == (
        "deliverables/docs/contract.md"
    )
    # A dict that declares no path at all has no comparison input — the old gate
    # never entered its comparison loop for such an entry either.
    assert declared_requirement({"required": True}).path_spelling == ""


# --------------------------------------------------------------------------
# the ownership gate is not widened (M1 boundary)
# --------------------------------------------------------------------------

def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="running",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def test_ownership_gate_still_rejects_a_prose_tailed_out_of_bounds_declaration(tmp_path):
    """The strictest new case: normalizing the prose tail away must NOT let an
    undeclared path through.  The extracted path is still compared to
    ``owned_paths`` and still raises the same error."""
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="declared output is outside owned_paths"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "synthesis",
            "goal": "package the verified outputs",
            "owned_paths": ["deliverables/result.json"],
            "expected_outputs": ["deliverables/manifest.json（清单，另存）"],
        }]))
    assert graph.get_node("synthesis") is None


def test_ownership_gate_rejects_a_dict_declared_out_of_bounds_path(tmp_path):
    """The dict shape is normalized by the same rule, so it is rejected by the
    same gate — dict declarations never became a bypass."""
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="declared output is outside owned_paths"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "synthesis",
            "goal": "package the verified outputs",
            "owned_paths": ["deliverables/result.json"],
            "expected_outputs": [{"path": "deliverables/manifest.json"}],
        }]))
    assert graph.get_node("synthesis") is None


def test_ownership_gate_rejects_an_absolute_path_declaration(tmp_path):
    """Fail-closed on an unparseable spelling.

    A path the identity plane refuses (here: absolute) is not silently dropped:
    the declaration keeps its own spelling as the candidate, so the gate compares
    exactly the string it compared before this landing and still rejects it.
    """
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="declared output is outside owned_paths"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "synthesis",
            "goal": "package the verified outputs",
            "owned_paths": ["deliverables/result.json"],
            "expected_outputs": ["/etc/passwd.md"],
        }]))
    assert graph.get_node("synthesis") is None
    # The candidate really is the entry's own spelling, not a dropped entry.
    assert declared_requirement("/etc/passwd.md").paths == [
        identity_key("/etc/passwd.md")
    ]


def test_ownership_gate_accepts_a_prose_tailed_declaration_of_an_owned_path(tmp_path):
    """The shape that used to be mis-rejected: the declared path is owned, only
    the domain prose around it was not a path."""
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "report",
        "goal": "write the delivery contract",
        "owned_paths": ["deliverables/docs/"],
        "expected_outputs": ["deliverables/docs/contract.md（交付说明）"],
    }]))
    assert graph.get_node("report") is not None


def test_ownership_gate_rejects_a_mixed_declaration_of_several_paths(tmp_path):
    """b33 S4 §8vigesies (user 2026-10-06): the lenient side is now closed.

    原来钉住什么：一条声明同时列出多个路径时，只要任意一个候选落在该节点的
    ``owned_paths`` 内，整条声明就被放行（MIXED-DECLARATION-LENIENCY）。
    现在钉住什么：同一个输入在**准入阶段**被拒，错误点名节点、该条目的原始拼写与
    全部候选——"一条声明只能表达一个明确路径或一个明确目录范围"。
    为什么原来的行为正是要取消的：归属判定被"某一个候选"代为通过，声明里的另一个
    路径（可能与它同处一条声明却属于别人）就悄悄搭车进入交付；而"后续文件写入检查
    还能兜住"不能替代任务归属检查。
    """
    mixed = "deliverables/result.json deliverables/other-owner/x.md"
    # Three candidates: both file claims plus the entry's verbatim spelling.
    assert _paths(mixed) == [
        "deliverables/result.json",
        "deliverables/other-owner/x.md",
        identity_key(mixed),
    ]
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError) as caught:
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "synthesis",
            "goal": "package the verified outputs",
            "owned_paths": ["deliverables/result.json"],
            "expected_outputs": [mixed],
        }]))
    message = str(caught.value)
    assert "ambiguous declared output" in message
    assert "deliverables/result.json" in message
    assert "deliverables/other-owner/x.md" in message
    assert graph.get_node("synthesis") is None
    # The structured shape still reads field-first: a second field never enters
    # the comparison, so this remains one clear single-path declaration.
    assert _paths({
        "path": "deliverables/pack.md",
        "file": "deliverables/other-owner/x.md",
    }) == ["deliverables/pack.md"]


def test_ownership_gate_accepts_a_single_directory_scope_declaration(tmp_path):
    """The other legal shape, pinned as an ACCEPT: one clear directory scope.

    §8vigesies allows exactly two shapes — one definite path or one definite
    directory scope.  A directory entry is not a file claim, so the gate compares
    it by containment (``deliverables/docs/audit/`` sits inside the owned
    ``deliverables/docs/``) instead of by suffix.
    """
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "audit",
        "goal": "write the audit notes",
        "owned_paths": ["deliverables/docs/"],
        "expected_outputs": [{"path": "deliverables/docs/audit/"}],
    }]))
    assert graph.get_node("audit") is not None


def test_ownership_gate_keeps_the_legacy_skip_for_plain_prose(tmp_path):
    """An entry whose own spelling is not a path is skipped, exactly as before.

    Landing the extractor must not turn ordinary prose into a new rejection:
    the comparison input is still the entry's own spelling, and a sentence has
    no path suffix, so the gate never enters the comparison for it.
    """
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "summary",
        "goal": "summarize the work",
        "owned_paths": ["deliverables/summary.md"],
        "expected_outputs": ["a short summary of the delivered work"],
    }]))
    assert graph.get_node("summary") is not None


# --------------------------------------------------------------------------
# the self-lock judgement is still a plain set difference (D4, §8sexdecies #3)
# --------------------------------------------------------------------------

def _orchestrator_with_existing(paths=(), artifacts=()):
    orch = Orchestrator.__new__(Orchestrator)
    nodes = {}
    for index, path in enumerate(paths):
        nodes[f"N-{index:03d}"] = SimpleNamespace(
            owned_paths=[path], output_artifacts=list(artifacts) if index == 0 else []
        )
    if not paths:
        nodes["N-000"] = SimpleNamespace(owned_paths=[], output_artifacts=list(artifacts))
    orch.task_graph = SimpleNamespace(nodes=nodes)
    return orch


def test_self_lock_judgement_is_still_a_set_difference():
    orch = _orchestrator_with_existing(["deliverables/app/main.py"])
    # Re-claiming an existing path is not progress.
    assert orch._added_nodes_claim_new_evidence(
        [{"owned_paths": ["deliverables/app/main.py"]}]
    ) is False
    # Claiming one the graph does not have is progress.
    assert orch._added_nodes_claim_new_evidence(
        [{"owned_paths": ["deliverables/app/other.py"]}]
    ) is True


def test_claim_sets_are_identity_plane_keys():
    """Collecting the two inline normalizations into ``identity_key`` must not
    change the verdict: the same file spelled differently is the same file."""
    orch = _orchestrator_with_existing(["Deliverables\\App\\Main.PY"])
    existing_paths, _artifacts = orch._verification_claim_sets()
    assert existing_paths == {identity_key("deliverables/app/main.py")}
    assert orch._added_nodes_claim_new_evidence(
        [{"owned_paths": ["deliverables/app/main.py"]}]
    ) is False


# --------------------------------------------------------------------------
# the root-cause suffix lexer is declared once (D4, safe half)
# --------------------------------------------------------------------------

def test_root_cause_suffix_lexer_is_declared_once():
    """The legacy suffix set, unchanged and in one place.

    It used to be written twice inside ``_resolve_declared_root_causes`` (a
    tuple plus the same list again as a regex alternation).  A suffix silently
    disappearing would change which files a fatal verifier detail declares as
    root causes, so the set is pinned here.
    """
    suffixes = orchestrator_module.ROOT_CAUSE_TEXT_SUFFIXES
    assert suffixes == (
        ".py", ".sql", ".bat", ".md", ".json", ".js", ".ts", ".html",
        ".css", ".csv", ".txt", ".sh", ".ps1", ".yaml", ".yml", ".toml",
        ".log",
    )
    assert len(set(suffixes)) == len(suffixes)