from tests.publication_support import authorize_files
import json

from AstralNexus.core.delivery import DeliveryManager


def test_publish_always_writes_current_pointer(tmp_path):
    """Fix O (doc 18 attempt8): the pointer file used to be written only when
    symlinking failed.  After 2026-09-13 hosts that can create symlinks produced
    a published delivery WITHOUT current.json, so every consumer reading the
    pointer (acceptance harness, regression script) reported a successful
    publication as no_published_delivery."""
    run_dir = tmp_path / "run"
    manager = DeliveryManager(run_dir)
    staging = manager.begin()
    (staging / "delivery_response.md").write_text("done", encoding="utf-8")

    manifest = manager.publish(authorized=authorize_files(manager))
    assert manifest["status"] == "published"

    pointer = run_dir / "deliverables/current.json"
    assert pointer.is_file()
    assert json.loads(pointer.read_text(encoding="utf-8")) == {
        "current_version": 1,
        "path": "v1",
    }
    assert (run_dir / "deliverables/v1/delivery_response.md").is_file()

    link = run_dir / "deliverables/current"
    if link.exists() or link.is_symlink():
        assert link.resolve() == (run_dir / "deliverables/v1").resolve()


def test_unauthorized_publish_is_refused(tmp_path):
    run_dir = tmp_path / "run"
    manager = DeliveryManager(run_dir)
    staging = manager.begin()
    (staging / "delivery_response.md").write_text("done", encoding="utf-8")
    try:
        manager.publish(authorized=False)
    except PermissionError:
        pass
    else:  # pragma: no cover - explicit failure if the gate is bypassable
        raise AssertionError("publish must refuse an unauthorized delivery")
    assert not (run_dir / "deliverables/current.json").exists()


def test_publish_excludes_interpreter_and_probe_byproducts(tmp_path):
    """Fix Q (doc 18 2026-09-15 r13 audit P1): publish() copied the staging tree
    verbatim, so .pyc/.db/.log by-products were promoted into the immutable
    version and listed in delivery_manifest.json (live r13: ~640 KB of junk
    inside the required deliverables/app/ tree)."""
    run_dir = tmp_path / "run"
    manager = DeliveryManager(run_dir)
    staging = manager.begin()
    (staging / "app").mkdir(parents=True)
    (staging / "app/server.py").write_text("ok", encoding="utf-8")
    (staging / "app/__pycache__").mkdir(parents=True)
    (staging / "app/__pycache__/server.cpython-311.pyc").write_bytes(b"\x00\x01")
    (staging / "app/.probe").mkdir(parents=True)
    (staging / "app/.probe/tmp_check.db").write_bytes(b"db")
    (staging / "app/.probe/tmp_check.db-shm").write_bytes(b"shm")
    (staging / "app/.astralnexus_process_logs").mkdir(parents=True)
    (staging / "app/.astralnexus_process_logs/terminal_exec_1.log").write_text(
        "log", encoding="utf-8")

    manifest = manager.publish(authorized=authorize_files(manager))
    assert manifest["excluded_count"] == 4

    published = run_dir / "deliverables/v1"
    files = sorted(
        str(path.relative_to(published)).replace("\\", "/")
        for path in published.rglob("*") if path.is_file()
    )
    assert files == ["app/server.py", "delivery_manifest.json"]

    recorded = json.loads(
        (published / "delivery_manifest.json").read_text(encoding="utf-8"))
    assert [item["path"] for item in recorded["artifacts"]] == ["app/server.py"]
    assert recorded["excluded_count"] == 4
    assert "__pycache__" in recorded["exclusion_rules"]["parts"]