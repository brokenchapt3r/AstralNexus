from pathlib import Path
import shutil
import pytest
from AstralNexus.core.delivery import DeliveryManager
from AstralNexus.core.delivery_snapshot import authorize_snapshot, DeliveryRejected
from AstralNexus.core.run_application import RunApplicationService
from AstralNexus.tests.publication_support import authorize_files


def make_package(tmp_path):
    manager = DeliveryManager(tmp_path)
    stage = manager.begin()
    (stage / "a.txt").write_text("new", encoding="utf-8")
    return manager, {"task_version": 2, "required_outputs": [{"path": "deliverables/a.txt"}]}


def test_old_runtime_file_cannot_satisfy_missing_staged_output(tmp_path):
    manager, contract = make_package(tmp_path)
    (manager.staging_dir / "a.txt").unlink()
    manager.deliverables_dir.mkdir()
    (manager.deliverables_dir / "a.txt").write_text("old")
    with pytest.raises(DeliveryRejected):
        authorize_snapshot(manager.staging_dir, contract, {"overall_status": "pass"})


def test_legacy_boolean_verdict_cannot_authorize_new_publication(tmp_path):
    manager, contract = make_package(tmp_path)
    (manager.staging_dir / "undeclared.txt").write_text("extra")
    with pytest.raises(DeliveryRejected, match="legacy_delivery_authorization_retired"):
        authorize_snapshot(manager.staging_dir, contract, {"overall_status": "pass"})


@pytest.mark.parametrize("change", ["modify", "add", "delete"])
def test_authorization_expires_on_snapshot_change(tmp_path, change):
    manager, contract = make_package(tmp_path)
    permit = authorize_files(manager)
    if change == "modify":
        (manager.staging_dir / "a.txt").write_text("changed")
    elif change == "add":
        (manager.staging_dir / "b.txt").write_text("extra")
    else:
        (manager.staging_dir / "a.txt").unlink()
    with pytest.raises(PermissionError, match="changed"):
        manager.publish(authorized=permit)
    assert not (manager.deliverables_dir / "current.json").exists()


def test_bare_true_and_other_run_authorization_are_rejected(tmp_path):
    manager, contract = make_package(tmp_path / "one")
    other, _ = make_package(tmp_path / "two")
    permit = authorize_files(manager)
    for authorization in (True, permit):
        with pytest.raises(PermissionError):
            other.publish(authorized=authorization)


def test_copy_mutation_is_rejected_before_version_commit(tmp_path, monkeypatch):
    manager, contract = make_package(tmp_path)
    permit = authorize_files(manager)
    original = shutil.copy2
    def tamper(source, dest):
        result = original(source, dest)
        Path(dest).write_text("tampered")
        return result
    monkeypatch.setattr(shutil, "copy2", tamper)
    with pytest.raises(PermissionError, match="Copied"):
        manager.publish(authorized=permit)
    assert not (manager.deliverables_dir / "v1").exists()


@pytest.mark.parametrize("status", ["fail", "partial", "inconclusive"])
def test_failed_independent_verdict_cannot_publish(tmp_path, status):
    manager, contract = make_package(tmp_path)
    with pytest.raises(DeliveryRejected):
        authorize_snapshot(manager.staging_dir, contract, {"overall_status": status})


def test_service_translates_acceptance_rejection_without_changing_execution(tmp_path):
    class Pipeline:
        def execute(self, **kwargs):
            raise DeliveryRejected({"overall_status": "fail"})
    execution = {"status": "completed"}
    outcome = RunApplicationService(object(), pipeline=Pipeline()).complete(execution=execution, task_content="task", run_dir=tmp_path)
    assert outcome["status"] == "delivery_failed"
    assert outcome["execution_status"] == "completed"
    assert not outcome["delivery_authorized"]
    assert execution == {"status": "completed"}









@pytest.mark.parametrize("flag,expected", [(False, False), ("false", False), ("true", True)])
def test_persisted_contract_has_one_summary_default(flag, expected):
    from AstralNexus.core.delivery_contract_policy import normalize_delivery_contract
    original = {"summary_required": flag, "task_version": 7}
    migrated = normalize_delivery_contract(original)
    assert migrated["summary_required"] is expected
    assert migrated["contract_schema_version"] == 2
    assert migrated["task_version"] == 7
    assert "contract_schema_version" not in original
    assert normalize_delivery_contract({"task_version": 7})["summary_required"] is False


def test_legacy_conditional_output_is_normalized_once():
    from AstralNexus.core.delivery_contract_policy import normalize_delivery_contract
    value = normalize_delivery_contract({"required_outputs": [{"path": "optional.txt", "source": "conditional", "condition": "only if enabled"}]})
    assert value["required_outputs"][0]["required"] is False
    assert normalize_delivery_contract(value) == value




def test_new_draft_cannot_reuse_previous_candidate_and_preserves_it(tmp_path):
    from AstralNexus.core.output_paths import RunOutputLayout
    layout = RunOutputLayout(tmp_path)
    first = layout.begin_delivery_draft()
    (first / "old.txt").write_text("failed candidate")
    second = layout.begin_delivery_draft()
    assert not list(second.iterdir())
    assert len(list((tmp_path / ".delivery_drafts").glob("*/old.txt"))) == 1



def test_failed_current_pointer_commit_preserves_previous_pointer(tmp_path, monkeypatch):
    import os
    import json
    manager, contract = make_package(tmp_path)
    manager.publish(authorized=authorize_files(manager))
    manager.begin()
    (manager.staging_dir / "a.txt").write_text("next")
    permit = authorize_files(manager)
    original = os.replace
    def fail_pointer(src, dst):
        if Path(dst).name == "current.json":
            raise OSError("pointer commit failed")
        return original(src, dst)
    monkeypatch.setattr(os, "replace", fail_pointer)
    with pytest.raises(OSError, match="pointer commit"):
        manager.publish(authorized=permit)
    assert json.loads((manager.deliverables_dir / "current.json").read_text())["current_version"] == 1
    # A fully written but uncommitted version is preserved for audit.
    assert (manager.deliverables_dir / "v2" / "delivery_manifest.json").is_file()



def test_recorded_gate_must_be_the_authorized_verdict(tmp_path):
    import json
    manager, contract = make_package(tmp_path)
    (manager.staging_dir / "delivery_verification.json").write_text(json.dumps({"overall_status": "fail"}))
    with pytest.raises(DeliveryRejected, match="recorded_gate"):
        authorize_snapshot(manager.staging_dir, contract, {"overall_status": "pass"})



def test_stale_generation_cannot_use_an_accepted_snapshot(tmp_path):
    manager, contract = make_package(tmp_path)
    permit = authorize_files(manager, ownership_identity=("run", "owner", 1))
    with pytest.raises(PermissionError, match="generation"):
        manager.publish(authorized=permit, fence_provider=lambda: ("run", "owner", 2))
    assert not (manager.deliverables_dir / "current.json").exists()



