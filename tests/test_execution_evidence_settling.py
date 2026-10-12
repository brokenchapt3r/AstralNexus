"""Fix AH: a staleness veto must be about the bytes the verdict was formed on.

Live regression (doc 18, r13b PATCH-R004-RUNTIME).  The node's contracted job
is to start the backend and capture runtime evidence.  ``finish()`` snapshots
``owned_files`` the moment the worker returns, but the service it launched is
still shutting down: the log file is still being flushed, the shutdown
handshake writes its stop-flag, and the SQLite database is checkpointed on
clean close.  The Verifier then reads the SETTLED tree and forms a verdict on
it -- three times a real score, once 0.9917 with all six dimensions >= 0.95.
``accept()`` compared accept-time bytes against the FINISH-time snapshot, saw
the settling, and raised "Execution evidence is stale", rewriting each verdict
to inconclusive/0.0.  The gate vetoed a correct verdict using a reference point
the verdict was never about.

Fix AH keeps the guarantee and moves the reference point: drift *after* the
Verifier read the tree is still a hard failure, upstream drift is still a hard
failure, and pre-verification settling is recorded rather than silently
forgiven.  Callers that supply no basis keep the original, stricter behaviour.
"""
from __future__ import annotations

import pytest

from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files


def _store(tmp_path):
    return ExecutionStore(tmp_path)


def _runtime_execution(tmp_path, store):
    """The r13b PATCH-R004-RUNTIME shape: a node that owns a live service tree."""
    app = tmp_path / "deliverables" / "app" / "backend" / "_runtime"
    app.mkdir(parents=True)
    (app / "startup_run1.log").write_text("boot\n", encoding="utf-8")
    (app / "launch_recipe.json").write_text('{"cmd": "python app.py"}', encoding="utf-8")
    execution_id = store.begin("PATCH-R004-RUNTIME", {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [],
    })
    store.append(execution_id, {
        "command": "python run_runtime_selfcheck.py", "status": "success", "returncode": 0,
        "summary": "selfcheck ok",
    })
    store.finish(execution_id, "succeeded", {"content": "runtime evidence captured"})
    return execution_id, app


def _settle(app):
    """What the launched service does while the Verifier is still thinking."""
    with (app / "startup_run1.log").open("a", encoding="utf-8") as handle:
        handle.write("graceful shutdown complete\n")
    (app / "selfcheck_stop.flag").write_text("1", encoding="utf-8")


APPROVED = {"approved": True, "score": 0.9917, "verdict": "approved"}


# --- the live defect ---------------------------------------------------------

def test_post_finish_settling_no_longer_vetoes_the_verdict(tmp_path):
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    _settle(app)                                   # service finishes shutting down
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])  # what the Verifier reads
    store.accept(execution_id, APPROVED, verified_files=basis)
    assert store.get(execution_id)["acceptance"]["approved"] is True
    assert store.get(execution_id)["acceptance"]["score"] == pytest.approx(0.9917)


def test_settling_is_recorded_not_forgiven(tmp_path):
    """The drift stays auditable in the journal."""
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    _settle(app)
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    store.accept(execution_id, APPROVED, verified_files=basis)
    drift = store.get(execution_id)["acceptance"]["pre_verification_drift"]
    assert drift["added"] == ["deliverables/app/backend/_runtime/selfcheck_stop.flag"]
    assert drift["changed"] == ["deliverables/app/backend/_runtime/startup_run1.log"]


def test_quiet_tree_records_no_drift(tmp_path):
    store = _store(tmp_path)
    execution_id, _ = _runtime_execution(tmp_path, store)
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    store.accept(execution_id, APPROVED, verified_files=basis)
    assert "pre_verification_drift" not in store.get(execution_id)["acceptance"]


# --- the guarantee that must survive ----------------------------------------

def test_change_after_the_verifier_read_still_hard_fails(tmp_path):
    """The bytes the verdict is ABOUT may not move under it."""
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    (app / "launch_recipe.json").write_text('{"cmd": "FORGED"}', encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        store.accept(execution_id, APPROVED, verified_files=basis)
    assert "after the Verifier read them" in str(excinfo.value)
    assert store.get(execution_id)["acceptance"] is None


def test_file_deleted_after_the_verifier_read_still_hard_fails(tmp_path):
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    (app / "launch_recipe.json").unlink()
    with pytest.raises(ValueError):
        store.accept(execution_id, APPROVED, verified_files=basis)


def test_upstream_change_still_hard_fails(tmp_path):
    """Upstream inputs are unconditional -- a basis never relaxes them."""
    store = _store(tmp_path)
    upstream = tmp_path / "deliverables" / "app" / "schema.sql"
    upstream.parent.mkdir(parents=True, exist_ok=True)
    upstream.write_text("CREATE TABLE t(id INTEGER);\n", encoding="utf-8")
    backend = tmp_path / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "app.py").write_text("print('ok')\n", encoding="utf-8")
    execution_id = store.begin("PATCH-R004-RUNTIME", {
        "owned_paths": ["deliverables/app/backend/"],
        "upstream_artifacts": [{"path": "deliverables/app/schema.sql"}],
    })
    store.finish(execution_id, "succeeded", {"content": "ok"})
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    upstream.write_text("DROP TABLE t;\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        store.accept(execution_id, APPROVED, verified_files=basis)
    assert "upstream input files changed" in str(excinfo.value)


def test_no_basis_keeps_the_original_stricter_gate(tmp_path):
    """Backward compatibility: every existing caller is unaffected."""
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    _settle(app)
    with pytest.raises(ValueError) as excinfo:
        store.accept(execution_id, APPROVED)
    assert "since execution finished" in str(excinfo.value)


def test_rejection_is_never_blocked_by_staleness(tmp_path):
    """A non-approval must always be recordable, however the tree moved."""
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    (app / "launch_recipe.json").write_text("{}", encoding="utf-8")
    store.accept(execution_id, {"approved": False, "score": 0.2, "verdict": "repair_required"})
    assert store.get(execution_id)["acceptance"]["approved"] is False


# --- the veto must name what it vetoed on ------------------------------------

def test_veto_names_the_offending_files(tmp_path):
    """The live failure said only 'files changed since execution'; the cause had
    to be excavated from executions.sqlite by hand."""
    store = _store(tmp_path)
    execution_id, app = _runtime_execution(tmp_path, store)
    basis = snapshot_files(tmp_path, ["deliverables/app/backend/"])
    (app / "launch_recipe.json").write_text('{"cmd": "FORGED"}', encoding="utf-8")
    (app / "extra.log").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        store.accept(execution_id, APPROVED, verified_files=basis)
    message = str(excinfo.value)
    assert "launch_recipe.json" in message and "extra.log" in message
    assert "changed=1" in message and "added=1" in message
