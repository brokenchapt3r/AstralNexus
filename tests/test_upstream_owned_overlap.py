"""Fix AK: an upstream artifact the execution itself owns carries no signal.

Ownership is exclusive -- the contract grants this execution write authority
over ``owned_paths`` -- so a change there is authorized by construction and is
already judged by the owned-files check.  When a path is BOTH an upstream input
and owned, the upstream check cannot pass: doing the node's job is what changes
those bytes.

Live regression (doc 18, r13b generation 15).  ``PATCH-R006-RUNTIME`` took over
``PATCH-R005-RUNTIME`` through ``recovery_of``, inheriting its owned_paths while
also carrying its outputs as upstream artifacts.  It then did exactly what its
contract asked -- rewrote the launch recipe and startup log, cleaned up its
predecessor's scratch, wrote its own evidence files -- and was vetoed with::

    Execution evidence is stale; upstream input files changed since execution:
    added=6 [... _runtime/evidence_provenance.json, _runtime/freshness_manifest.json ...];
    removed=8 [... _runtime/selfcheck_run.db ...];
    changed=5 [... _runtime/launch_recipe.json, _runtime/startup_run1.log]

every path under ``deliverables/app/backend/_runtime/``, which it owns.  No
execution of that shape could ever have been accepted.

Upstream artifacts the execution does NOT own keep the unconditional hard gate.
"""
from __future__ import annotations

import pytest

from AstralNexus.runtime.execution_store import (
    ExecutionStore,
    _is_under,
    _run_relative,
    external_upstream_paths,
    snapshot_files,
)

APPROVED = {"approved": True, "score": 0.97, "verdict": "approved"}


# --- containment ------------------------------------------------------------

@pytest.mark.parametrize("candidate,root", [
    ("deliverables/app/backend/_runtime/launch_recipe.json", "deliverables/app/backend/"),
    ("deliverables/app/backend/_runtime/launch_recipe.json", "deliverables/app/backend"),
    ("deliverables/app/backend", "deliverables/app/backend/"),
    ("deliverables\\app\\backend\\db.py", "deliverables/app/backend"),
])
def test_paths_inside_an_owned_root_are_recognized(candidate, root):
    assert _is_under(candidate, root)


@pytest.mark.parametrize("candidate,root", [
    ("deliverables/app/backend-extra/x.py", "deliverables/app/backend"),
    ("deliverables/app/frontend/x.js", "deliverables/app/backend"),
    ("deliverables/app/backend", "deliverables/app/backend/_runtime"),
    ("", "deliverables/app/backend"),
    ("deliverables/app/backend", ""),
])
def test_sibling_and_empty_paths_are_not_contained(candidate, root):
    """A shared prefix is not containment -- ``backend-extra`` is not ``backend``."""
    assert not _is_under(candidate, root)


# --- selection --------------------------------------------------------------

def test_self_owned_upstream_is_dropped():
    contract = {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [
            {"path": "deliverables/app/backend/_runtime/launch_recipe.json"},
            {"path": "deliverables/app/backend/db.py"},
        ],
    }
    assert external_upstream_paths(contract) == []


def test_external_upstream_is_kept():
    contract = {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [
            {"path": "deliverables/app/schema.sql"},
            {"path": "deliverables/app/backend/db.py"},
            {"path": "deliverables/runs/api/core-api-calls.txt"},
        ],
    }
    assert external_upstream_paths(contract) == [
        "deliverables/app/schema.sql",
        "deliverables/runs/api/core-api-calls.txt",
    ]


def test_malformed_entries_are_ignored():
    contract = {"owned_paths": [], "upstream_artifacts": [
        {"path": "a/b.txt"}, {"path": ""}, {"nope": 1}, "a/b.txt", None, {"path": "a/b.txt"},
    ]}
    assert external_upstream_paths(contract) == ["a/b.txt"]


# --- end to end -------------------------------------------------------------

def _tree(tmp_path):
    runtime = tmp_path / "deliverables" / "app" / "backend" / "_runtime"
    runtime.mkdir(parents=True)
    (runtime / "launch_recipe.json").write_text('{"cmd": "old"}', encoding="utf-8")
    (runtime / "startup_run1.log").write_text("boot\n", encoding="utf-8")
    schema = tmp_path / "deliverables" / "app" / "schema.sql"
    schema.write_text("CREATE TABLE t(id INTEGER);\n", encoding="utf-8")
    return runtime, schema


def test_the_live_recovery_node_can_now_be_accepted(tmp_path):
    store = ExecutionStore(tmp_path)
    runtime, _ = _tree(tmp_path)
    execution_id = store.begin("PATCH-R006-RUNTIME", {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [
            {"path": "deliverables/app/backend/_runtime/launch_recipe.json"},
            {"path": "deliverables/app/backend/_runtime/startup_run1.log"},
        ],
    })
    # The node does its contracted job inside its own owned tree.
    (runtime / "launch_recipe.json").write_text('{"cmd": "new"}', encoding="utf-8")
    (runtime / "evidence_provenance.json").write_text("{}", encoding="utf-8")
    (runtime / "startup_run1.log").unlink()
    store.finish(execution_id, "succeeded", {"content": "runtime evidence captured"})
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    store.accept(execution_id, APPROVED, verified_files=basis)
    assert store.get(execution_id)["acceptance"]["approved"] is True


def test_a_genuine_external_upstream_change_still_hard_fails(tmp_path):
    store = ExecutionStore(tmp_path)
    _, schema = _tree(tmp_path)
    execution_id = store.begin("PATCH-R006-RUNTIME", {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [
            {"path": "deliverables/app/backend/_runtime/launch_recipe.json"},
            {"path": "deliverables/app/schema.sql"},
        ],
    })
    store.finish(execution_id, "succeeded", {"content": "ok"})
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    schema.write_text("DROP TABLE t;\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        store.accept(execution_id, APPROVED, verified_files=basis)
    message = str(excinfo.value)
    assert "upstream input files changed" in message
    assert "schema.sql" in message
    # The self-owned upstream must not pollute the report.
    assert "launch_recipe.json" not in message


def test_evidence_reports_no_upstream_change_for_self_owned_rewrites(tmp_path):
    store = ExecutionStore(tmp_path)
    runtime, _ = _tree(tmp_path)
    execution_id = store.begin("PATCH-R006-RUNTIME", {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [
            {"path": "deliverables/app/backend/_runtime/launch_recipe.json"},
        ],
    })
    (runtime / "launch_recipe.json").write_text('{"cmd": "rewritten"}', encoding="utf-8")
    store.finish(execution_id, "succeeded", {"content": "ok"})
    assert store.evidence(execution_id)["upstream_files_changed"] is False


# --- path spelling (the half Fix AK first missed) ----------------------------

def test_an_absolute_self_owned_upstream_is_also_dropped(tmp_path):
    """A contract registers the same artifact in several spellings.

    The live r13b contracts carry 58 upstream entries for roughly a dozen files,
    each present as a Windows absolute path AND as a run-relative one, often
    repeatedly.  Ownership is declared run-relative, so a prefix test against the
    raw string catches the relative spelling and misses every absolute one --
    the self-owned entry slips back through the upstream gate and re-creates the
    very false veto this fix removes.
    """
    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    absolute = str(backend / "_runtime" / "launch_recipe.json")
    contract = {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [
            {"path": absolute},
            {"path": "deliverables/app/backend/_runtime/launch_recipe.json"},
            {"path": str(tmp_path / "deliverables" / "app" / "schema.sql")},
        ],
    }
    assert external_upstream_paths(contract, tmp_path) == ["deliverables/app/schema.sql"]


def test_duplicate_spellings_collapse_to_one_entry(tmp_path):
    schema = tmp_path / "deliverables" / "app" / "schema.sql"
    schema.parent.mkdir(parents=True)
    contract = {"owned_paths": [], "upstream_artifacts": [
        {"path": str(schema)},
        {"path": "deliverables/app/schema.sql"},
        {"path": "deliverables\\app\\schema.sql"},
        {"path": str(schema)},
    ]}
    assert external_upstream_paths(contract, tmp_path) == ["deliverables/app/schema.sql"]


def test_a_path_outside_the_run_stays_external(tmp_path):
    """Normalization must not silently absorb something the run does not contain."""
    outside = tmp_path.parent / "elsewhere" / "config.toml"
    contract = {"owned_paths": ["deliverables/"], "upstream_artifacts": [{"path": str(outside)}]}
    assert external_upstream_paths(contract, tmp_path) == [str(outside).replace("\\", "/")]


def test_normalization_without_a_root_only_fixes_separators():
    assert _run_relative("deliverables\\app\\db.py", None) == "deliverables/app/db.py"
    assert _run_relative("", None) == ""
