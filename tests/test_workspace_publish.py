"""Offline unit tests for runtime.workspace_publish."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from AstralNexus.runtime import workspace_publish as wp
from tests.publish_support import (
    make_run as _make_run, make_workspace as _ws, temporary_host,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ① fresh publish of two files ------------------------------------------------


def test_fresh_publish_two_files(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha", "sub/b.bin": b"\x00\x01\x02"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")

    result = wp.publish_deliverables(
        run_dir=run_dir,
        workspace_root=workspace,
        session="s1",
        turn="t1",
        run="r1",
        delivery_id="dtest1",
        manifest=manifest,
    )
    assert result.rolled_back is False
    assert result.resumed is False
    assert result.refused == ()
    assert result.conflicts == ()
    assert set(result.published) == set(payload)
    assert result.skipped_identical == ()

    published_root = workspace / wp.DELIVERY_DIRNAME / "dtest1" / "published"
    assert Path(result.published_root) == published_root
    for rel, data in payload.items():
        written = published_root / rel
        assert written.read_bytes() == data
        assert _sha(written.read_bytes()) == manifest["files"][rel]["sha256"]

    binding = wp.read_binding(workspace, "dtest1")
    assert binding["delivery_id"] == "dtest1"
    assert binding["session"] == "s1"
    assert binding["turn"] == "t1"
    assert binding["run"] == "r1"
    assert binding["manifest_sha256"] == manifest["manifest_sha256"]
    assert sorted(binding["files"]) == sorted(payload)
    assert isinstance(binding["published_at"], str)

    # nothing was written outside the delivery area
    assert not (workspace / "a.txt").exists()
    assert not (workspace / "sub").exists()


# ② repeat publish hits skipped_identical -------------------------------------


def test_repeat_publish_is_skipped_identical(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha", "sub/b.bin": b"beta"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")

    first = wp.publish_deliverables(
        run_dir=run_dir, workspace_root=workspace, delivery_id="drepeat", manifest=manifest
    )
    assert set(first.published) == set(payload)

    published_root = workspace / wp.DELIVERY_DIRNAME / "drepeat" / "published"
    before = {rel: (published_root / rel).stat().st_mtime_ns for rel in payload}
    time.sleep(0.01)

    second = wp.publish_deliverables(
        run_dir=run_dir, workspace_root=workspace, delivery_id="drepeat", manifest=manifest
    )
    assert second.published == ()
    assert set(second.skipped_identical) == set(payload)
    assert second.conflicts == ()
    for rel, data in payload.items():
        assert (published_root / rel).read_bytes() == data
        assert (published_root / rel).stat().st_mtime_ns == before[rel]


# ③ conflicting content refuses the whole order -------------------------------


def test_conflict_refuses_whole_order_by_default(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")

    published_root = workspace / wp.DELIVERY_DIRNAME / "dconflict" / "published"
    (published_root / "b.txt").parent.mkdir(parents=True, exist_ok=True)
    (published_root / "b.txt").write_bytes(b"USER-OWNED")

    result = wp.publish_deliverables(
        run_dir=run_dir, workspace_root=workspace, delivery_id="dconflict", manifest=manifest
    )
    assert result.conflicts == ("b.txt",)
    assert result.published == ()
    assert (published_root / "b.txt").read_bytes() == b"USER-OWNED"
    assert not (published_root / "a.txt").exists()


# ④ explicit overwrite ---------------------------------------------------------


def test_overwrite_on_conflict_replaces_target(tmp_path: Path) -> None:
    payload = {"b.txt": b"beta-v2"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")

    published_root = workspace / wp.DELIVERY_DIRNAME / "doverwrite" / "published"
    (published_root / "b.txt").parent.mkdir(parents=True, exist_ok=True)
    (published_root / "b.txt").write_bytes(b"old-bytes")

    result = wp.publish_deliverables(
        run_dir=run_dir,
        workspace_root=workspace,
        delivery_id="doverwrite",
        manifest=manifest,
        overwrite_on_conflict=True,
    )
    assert result.published == ("b.txt",)
    assert result.conflicts == ()
    assert (published_root / "b.txt").read_bytes() == b"beta-v2"
    assert _sha((published_root / "b.txt").read_bytes()) == manifest["files"]["b.txt"]["sha256"]


# ⑤ partial copy failure rolls back -------------------------------------------


def test_partial_copy_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")
    delivery_id = "dpartial"

    real_publish = wp._publish_file
    calls = {"n": 0}

    def boom(staged: Path, target: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated copy failure")
        real_publish(staged, target)

    monkeypatch.setattr(wp, "_publish_file", boom)

    with pytest.raises(OSError):
        wp.publish_deliverables(
            run_dir=run_dir,
            workspace_root=workspace,
            delivery_id=delivery_id,
            manifest=manifest,
        )

    published_root = workspace / wp.DELIVERY_DIRNAME / delivery_id / "published"
    assert not (published_root / "a.txt").exists()
    assert not (published_root / "b.txt").exists()
    journal_path = workspace / wp.DELIVERY_DIRNAME / f"{wp.JOURNAL_PREFIX}{delivery_id}.json"
    journal: dict[str, Any] = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "rolled_back"


# ⑥ restart recovery -----------------------------------------------------------


def _handmade_staging(
    tmp_path: Path,
    *,
    delivery_id: str,
    payload: dict[str, bytes],
    published_first: bool,
    corrupt: bool = False,
) -> tuple[Path, Path]:
    workspace = _ws(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    delivery_root = workspace / wp.DELIVERY_DIRNAME / delivery_id
    staging = delivery_root / "staging"
    published = delivery_root / "published"
    staging.mkdir(parents=True, exist_ok=True)
    published.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict[str, Any]] = {}
    for index, rel in enumerate(sorted(payload)):
        data = payload[rel]
        if published_first and index == 0:
            target = published / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[rel] = {"sha256": _sha(data), "bytes": len(data), "state": "published"}
        else:
            staged = staging / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(b"CORRUPT" if corrupt else data)
            files[rel] = {"sha256": _sha(data), "bytes": len(data), "state": "staged"}

    journal_path = workspace / wp.DELIVERY_DIRNAME / f"{wp.JOURNAL_PREFIX}{delivery_id}.json"
    journal = {
        "delivery_id": delivery_id,
        "run_dir": str(run_dir),
        "workspace_root": str(workspace),
        "session": "s-recover",
        "turn": "t-recover",
        "run": "r-recover",
        "manifest_sha256": wp._journal_files_digest(files),
        "files": files,
        "phase": "publishing",
        "updated_at": "1970-01-01T00:00:00+00:00",
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    return workspace, published


def test_recover_resumes_remaining_files(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, published = _handmade_staging(
        tmp_path, delivery_id="dresume", payload=payload, published_first=True
    )

    result = wp.recover_pending(workspace_root=workspace, delivery_id="dresume")

    assert result.resumed is True
    assert result.rolled_back is False
    assert result.published == ("b.txt",)
    assert (published / "a.txt").read_bytes() == b"alpha"
    assert (published / "b.txt").read_bytes() == b"beta"

    journal_path = workspace / wp.DELIVERY_DIRNAME / f"{wp.JOURNAL_PREFIX}dresume.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "done"
    binding = wp.read_binding(workspace, "dresume")
    assert binding["session"] == "s-recover"

    # recover again on a done journal is a no-op with resumed=True
    again = wp.recover_pending(workspace_root=workspace, delivery_id="dresume")
    assert again.resumed is True
    assert again.published == ()
    assert again.rolled_back is False


def test_recover_rolls_back_on_tampered_staging(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, published = _handmade_staging(
        tmp_path, delivery_id="dtamper", payload=payload, published_first=True, corrupt=True
    )

    result = wp.recover_pending(workspace_root=workspace, delivery_id="dtamper")

    assert result.rolled_back is True
    assert result.resumed is True
    assert result.refused != ()
    assert not (published / "a.txt").exists()
    assert not (published / "b.txt").exists()
    journal_path = workspace / wp.DELIVERY_DIRNAME / f"{wp.JOURNAL_PREFIX}dtamper.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "rolled_back"


def test_recover_missing_journal_raises(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    with pytest.raises(ValueError):
        wp.recover_pending(workspace_root=workspace, delivery_id="nope")


def test_recover_rejects_manifest_sha256_mismatch(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha"}
    workspace, _ = _handmade_staging(
        tmp_path, delivery_id="dmismatch", payload=payload, published_first=False
    )
    journal_path = workspace / wp.DELIVERY_DIRNAME / f"{wp.JOURNAL_PREFIX}dmismatch.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["manifest_sha256"] = "0" * 64
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(ValueError):
        wp.recover_pending(workspace_root=workspace, delivery_id="dmismatch")


# ⑦ validate_manifest ----------------------------------------------------------


def _valid_files() -> dict[str, dict[str, Any]]:
    data = b"alpha"
    return {"a.txt": {"sha256": _sha(data), "bytes": len(data)}}


def test_validate_manifest_accepts_valid() -> None:
    files = _valid_files()
    manifest = {"files": files, "manifest_sha256": wp._manifest_digest(files)}
    assert wp.validate_manifest(manifest) == ()


def test_validate_manifest_rejects_empty_files() -> None:
    reasons = wp.validate_manifest({"files": {}})
    assert reasons != ()


def test_validate_manifest_rejects_parent_escape() -> None:
    files = {"../escape.py": {"sha256": "a" * 64, "bytes": 1}}
    reasons = wp.validate_manifest({"files": files})
    assert reasons != ()
    assert any(".." in reason for reason in reasons)


def test_validate_manifest_rejects_absolute_path() -> None:
    files = {"/etc/passwd": {"sha256": "a" * 64, "bytes": 1}}
    reasons = wp.validate_manifest({"files": files})
    assert reasons != ()


def test_validate_manifest_rejects_missing_sha256() -> None:
    files = {"a.txt": {"bytes": 1}}
    reasons = wp.validate_manifest({"files": files})
    assert reasons != ()
    assert any("sha256" in reason for reason in reasons)


def test_validate_manifest_rejects_short_sha256() -> None:
    files = {"a.txt": {"sha256": "abc", "bytes": 1}}
    reasons = wp.validate_manifest({"files": files})
    assert any("sha256" in reason for reason in reasons)


def test_validate_manifest_rejects_bad_bytes() -> None:
    files = {"a.txt": {"sha256": "a" * 64, "bytes": -1}}
    reasons = wp.validate_manifest({"files": files})
    assert any("bytes" in reason for reason in reasons)
    assert wp.validate_manifest({"files": {"a.txt": {"sha256": "a" * 64, "bytes": "1"}}}) != ()


def test_validate_manifest_rejects_manifest_sha256_mismatch() -> None:
    files = _valid_files()
    reasons = wp.validate_manifest({"files": files, "manifest_sha256": "f" * 64})
    assert any("manifest_sha256" in reason for reason in reasons)


# ⑧ stale source refused at publish time ---------------------------------------


def test_stale_source_is_refused_at_publish(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    run_dir, deliverables = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(deliverables)

    (deliverables / "b.txt").write_bytes(b"beta-EDITED-AFTER-MANIFEST")

    result = wp.publish_deliverables(
        run_dir=run_dir, workspace_root=workspace, delivery_id="dstale", manifest=manifest
    )
    assert result.refused != ()
    assert any("b.txt" in reason for reason in result.refused)
    assert result.published == ()

    published_root = workspace / wp.DELIVERY_DIRNAME / "dstale" / "published"
    assert not (published_root / "a.txt").exists()
    assert not (published_root / "b.txt").exists()


def test_corrupt_manifest_entry_refuses_whole_order(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")
    manifest["files"]["a.txt"]["bytes"] = 999

    result = wp.publish_deliverables(
        run_dir=run_dir, workspace_root=workspace, delivery_id="dbadbytes", manifest=manifest
    )
    assert result.published == ()
    assert result.refused != ()


def test_empty_run_dir_manifest_is_invalid(tmp_path: Path) -> None:
    run_dir, _ = _make_run(tmp_path, {})
    workspace = _ws(tmp_path)
    manifest = wp.build_manifest(run_dir / "deliverables")
    assert manifest["files"] == {}
    assert wp.validate_manifest(manifest) != ()
    with pytest.raises(ValueError):
        wp.publish_deliverables(
            run_dir=run_dir, workspace_root=workspace, delivery_id="dempty", manifest=manifest
        )
    published_root = workspace / wp.DELIVERY_DIRNAME / "dempty" / "published"
    assert not published_root.exists()


def test_build_manifest_skips_pycache_and_pyc(tmp_path: Path) -> None:
    run_dir, deliverables = _make_run(
        tmp_path,
        {
            "keep.py": b"print('hi')\n",
            "pkg/mod.py": b"x = 1\n",
            "pkg/__pycache__/mod.cpython-311.pyc": b"\x00compiled",
            ".DS_Store": b"junk",
        },
    )
    manifest = wp.build_manifest(deliverables)
    assert sorted(manifest["files"]) == ["keep.py", "pkg/mod.py"]


def test_default_delivery_id_prefix(tmp_path: Path) -> None:
    payload = {"a.txt": b"alpha"}
    run_dir, _ = _make_run(tmp_path, payload)
    workspace = _ws(tmp_path)
    result = wp.publish_deliverables(run_dir=run_dir, workspace_root=workspace)
    assert result.delivery_id.startswith("d")
    assert result.delivery_id[1:].isdigit()


# ⑨ recovery is scoped to the delivery that was asked for ---------------------


def test_recover_is_scoped_to_the_asked_delivery_only(tmp_path: Path) -> None:
    """Scenario 7 reversal: another session's newer pending delivery is untouched.

    Recovery takes an explicit delivery id; a newer pending delivery sitting in
    the same workspace must not be picked instead of (or in addition to) the one
    asked for, and it must not be rolled back or resumed by that call.
    """
    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, published = _handmade_staging(
        tmp_path, delivery_id="dasked", payload=payload, published_first=True
    )
    other_workspace, _ = _handmade_staging(
        tmp_path, delivery_id="dother", payload={"c.txt": b"gamma"}, published_first=False
    )
    assert other_workspace == workspace

    delivery_root = workspace / wp.DELIVERY_DIRNAME
    other_journal = delivery_root / f"{wp.JOURNAL_PREFIX}dother.json"
    other_staged = delivery_root / "dother" / "staging" / "c.txt"
    stamp = time.time() + 3600
    for path in sorted(delivery_root.rglob("*"), reverse=True):
        os.utime(path, (stamp, stamp))
    journal_before = other_journal.read_bytes()
    # Sensor: the other delivery is the newest thing on disk, so any newest-wins
    # recovery would have to pick it.
    assert other_staged.stat().st_mtime_ns > time.time_ns()

    result = wp.recover_pending(workspace_root=workspace, delivery_id="dasked")

    assert result.resumed is True
    assert result.rolled_back is False
    assert result.published == ("b.txt",)
    assert (published / "b.txt").read_bytes() == b"beta"
    assert other_journal.read_bytes() == journal_before
    assert json.loads(other_journal.read_text(encoding="utf-8"))["phase"] == "publishing"
    assert other_staged.read_bytes() == b"gamma"
    assert not (delivery_root / "dother" / "published" / "c.txt").exists()


# ⑦ orphan sweep + binding verification: the un-wired recovery, wired --------
#
# ``recover_pending`` and ``read_binding`` shipped with the delivery contract
# and had NO production caller.  Consequences, both observed live: an
# interrupted publish was resumed by nobody (fix4 published 43 correct
# deliverables into the wrong root and only a human reading the journal found
# out), and a publication the worker *reported* was never checked against what
# landed on disk.  DeerFlow calls the first orphan recovery and gives it a named
# stop reason; these tests pin the same thing for our journals.


def test_sweep_resumes_an_interrupted_publish(tmp_path: Path) -> None:
    from AstralNexus.runtime import delivery_recovery as dr

    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, published = _handmade_staging(
        tmp_path, delivery_id="dsweep", payload=payload, published_first=True
    )

    outcomes = dr.sweep(tmp_path / "host", [{"workspace": str(workspace)}])

    assert [o["outcome"] for o in outcomes] == [dr.RESUMED]
    assert outcomes[0]["delivery_id"] == "dsweep"
    assert outcomes[0]["published"] == 1
    assert (published / "b.txt").read_bytes() == b"beta"


def test_sweep_rolls_back_a_tampered_publish(tmp_path: Path) -> None:
    from AstralNexus.runtime import delivery_recovery as dr

    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, published = _handmade_staging(
        tmp_path, delivery_id="dsweeptamper", payload=payload,
        published_first=True, corrupt=True,
    )

    outcomes = dr.sweep(tmp_path / "host", [{"workspace": str(workspace)}])

    assert [o["outcome"] for o in outcomes] == [dr.ROLLED_BACK]
    assert not (published / "a.txt").exists()


def test_sweep_survives_an_unreadable_journal(tmp_path: Path) -> None:
    """One broken journal is a verdict, not a reason to stop the sweep."""
    from AstralNexus.runtime import delivery_recovery as dr

    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, published = _handmade_staging(
        tmp_path, delivery_id="dsweepgood", payload=payload, published_first=True
    )
    bogus = workspace / wp.DELIVERY_DIRNAME / f"{wp.JOURNAL_PREFIX}bogus.json"
    bogus.write_text("{ not json", encoding="utf-8")

    outcomes = dr.sweep(tmp_path / "host", [{"workspace": str(workspace)}])

    by_id = {o["delivery_id"]: o for o in outcomes}
    assert set(by_id) == {"bogus", "dsweepgood"}
    assert by_id["bogus"]["outcome"] == dr.UNREADABLE
    assert by_id["bogus"]["error"]
    assert by_id["dsweepgood"]["outcome"] == dr.RESUMED
    assert (published / "b.txt").read_bytes() == b"beta"


def test_candidate_roots_cover_home_workspace_and_run_dirs(tmp_path: Path) -> None:
    """All three roots, because the publish root has legitimately been all
    three: the session workspace (correct, after the 2026-10-07 fix), the host
    home, and individual run dirs (pre-fix behaviour -- fix4's 43 files are
    under one of those)."""
    from AstralNexus.runtime import delivery_recovery as dr

    home = tmp_path / "host"
    run_dir = home / "chat" / "s" / "t" / "runs" / "task_task-1"
    run_dir.mkdir(parents=True)
    workspace = _ws(tmp_path)

    roots = dr.candidate_roots(home, [{"workspace": str(workspace), "run_dir": ""}])

    assert home.resolve() in roots
    assert workspace.resolve() in roots
    assert run_dir.resolve() in roots


def test_reported_publication_is_verified_against_the_binding(tmp_path: Path) -> None:
    """A claim of publication is checked against the workspace, not believed."""
    from AstralNexus.runtime import delivery_recovery as dr

    payload = {"a.txt": b"alpha", "b.txt": b"beta"}
    workspace, _ = _handmade_staging(
        tmp_path, delivery_id="dverify", payload=payload, published_first=True
    )
    wp.recover_pending(workspace_root=workspace, delivery_id="dverify")

    verified = dr.verify_published(workspace_root=workspace, delivery_id="dverify")
    assert verified["outcome"] == dr.BINDING_VERIFIED
    assert verified["files"] == 2

    mismatch = dr.verify_published(
        workspace_root=workspace, delivery_id="dverify",
        expect_manifest_sha256="0" * 64,
    )
    assert mismatch["outcome"] == dr.BINDING_UNVERIFIED
    assert "!=" in mismatch["reason"]

    missing = dr.verify_published(workspace_root=workspace, delivery_id="dnope")
    assert missing["outcome"] == dr.BINDING_UNVERIFIED


def test_host_startup_calls_the_delivery_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temporary_host) -> None:
    """The wiring itself is the fix: without the call, ``recover_pending``
    stays a library function that nobody runs."""
    from AstralNexus.runtime import delivery_recovery as dr

    calls: list[tuple[Path, list[Any]]] = []

    def fake_sweep(home: Path, sessions: Any = ()) -> list[Any]:
        calls.append((Path(home), list(sessions or ())))
        return []

    monkeypatch.setattr(dr, "sweep", fake_sweep)
    with temporary_host(tmp_path / 'host') as host:
        assert len(calls) == 1
        assert calls[0][0] == (tmp_path / "host").resolve()
#    因此交付文档里若还写 ``deliverables/...``，那个路径在发布树里指向不存在的东西。
#    实测：README 写 ``python deliverables/app/server.py``，验收器执行
#    ``<published>/app/deliverables/app/server.py`` → 服务起不来。


def _published(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "published"
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def test_content_base_mismatch_finds_the_pre_publish_prefix(tmp_path: Path) -> None:
    root = _published(tmp_path, {"README.md": "start:\n\npython deliverables/app/server.py\n"})

    found = wp.content_base_mismatches(root, ["README.md"])

    assert [item["rel"] for item in found] == ["README.md"]
    assert found[0]["line"] == 3
    assert "deliverables/app/server.py" in found[0]["excerpt"]


def test_content_base_mismatch_accepts_the_published_base(tmp_path: Path) -> None:
    root = _published(tmp_path, {"README.md": "python app/server.py\n"})

    assert wp.content_base_mismatches(root, ["README.md"]) == []


def test_content_base_mismatch_ignores_deep_oversized_and_non_docs(tmp_path: Path) -> None:
    root = _published(tmp_path, {
        "a/b/c/deep.md": "deliverables/app/server.py\n",              # 深度 > 2，跳过
        "big.md": "deliverables/app/server.py\n" + "x" * 300000,      # 超 256 KB，跳过
        "note.json": '{"path": "deliverables/app/server.py"}\n',      # 非文档后缀，跳过
    })

    found = wp.content_base_mismatches(root, ["a/b/c/deep.md", "big.md", "note.json"])

    assert found == []


def test_content_base_mismatch_is_bounded(tmp_path: Path) -> None:
    files = {"doc%d.md" % index: "deliverables/app/server.py\n" for index in range(12)}
    root = _published(tmp_path, files)

    found = wp.content_base_mismatches(root, sorted(files), limit=3)

    assert len(found) == 3


def test_delivery_summary_reports_content_base_mismatches(tmp_path: Path) -> None:
    """The diagnostic rides on ``delivery.publish``, so the turn row keeps it."""
    from AstralNexus.runtime import workbench_chat_worker as wcw

    root = _published(tmp_path, {"README.md": "python deliverables/app/server.py\n"})
    result = wp.PublishResult(
        delivery_id="d1", run_dir=str(tmp_path), workspace_root=str(tmp_path),
        published_root=str(root), published=("README.md",), skipped_identical=(),
        conflicts=(), refused=(), manifest_sha256="abc",
        journal_path=str(tmp_path / "journal_d1.json"),
    )

    summary = wcw._delivery_summary(result)

    assert summary["status"] == "published"
    assert [item["rel"] for item in summary["content_base"]["mismatches"]] == ["README.md"]