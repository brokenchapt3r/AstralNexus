"""Offline tests for the workbench turn -> workspace delivery wiring.

Everything here is offline: no network, no LLM, no subprocess.  The delivery
helper and the turn-level reporter are exercised against real tmp_path trees
and the real ``runtime.workspace_publish`` module; ``main()`` itself is covered
by one thin test that stubs the AstralNexus imports (see the module comment on
``test_main_delivers_only_a_completed_turn``).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
from typing import Any

import pytest

import AstralNexus.runtime.workspace_publish as workspace_publish
from AstralNexus.runtime import workbench_chat_worker as worker
from AstralNexus.tests.publish_support import WORKBENCH_FILES, make_run, make_workspace as _ws


# --- fixtures ---------------------------------------------------------------


def _accept_run(run_dir: Path) -> None:
    """Publisher wiring fixture: seal an explicitly accepted synthetic version.

    This fixture tests publication plumbing, not autonomous business acceptance.
    """
    import shutil
    from AstralNexus.core.delivery import DeliveryManager
    from AstralNexus.tests.publication_support import authorize_files
    files = worker._deliverable_manifest(run_dir / "deliverables")["files"]
    manager = DeliveryManager(run_dir)
    staging = manager.begin()
    for relative in files:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run_dir / "deliverables" / relative, target)
    gate = {"overall_status": "pass", "delivery_authorized": True, "failed_tests": [], "unknown_tests": []}
    (staging / "delivery_verification.json").write_text(json.dumps(gate))
    (staging / "delivery_summary.json").write_text(json.dumps({"delivery_gate": gate}))
    contract = {"required_outputs": [{"path": "deliverables/" + path} for path in files]}
    manager.publish(authorized=authorize_files(manager))


def _make_run(tmp_path: Path, name: str = "run-1") -> Path:
    run = make_run(tmp_path, WORKBENCH_FILES, name=name)[0]
    _accept_run(run)
    return run


def _signature(path: Path) -> tuple[int, int, str]:
    info = path.stat()
    return (info.st_mtime_ns, info.st_size, hashlib.sha256(path.read_bytes()).hexdigest())


def _files_under(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


# --- ① happy path -----------------------------------------------------------


def test_happy_path_publishes_deliverables_and_excludes_framework_metadata(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)

    summary = worker.deliver_run_to_workspace(
        run_dir=run_dir, workspace_root=workspace, session="sess-1", turn="turn-9", run="run-1"
    )

    assert summary["status"] == "published"
    assert summary["reason"] == "published"
    assert summary["published"] == 2
    assert summary["skipped_identical"] == 0
    assert summary["conflicts"] == 0
    assert summary["refused"] == 0
    assert summary["rolled_back"] is False
    assert summary["resumed"] is False
    assert summary["delivery_id"].startswith("run-") and len(summary["delivery_id"]) == 24
    assert summary["manifest_sha256"] and len(summary["manifest_sha256"]) == 64

    published_root = Path(summary["published_root"])
    assert published_root == workspace / workspace_publish.DELIVERY_DIRNAME / summary["delivery_id"] / "published"
    assert (published_root / "app" / "x.py").read_bytes() == b"print(1)\n"
    assert (published_root / "README.md").read_bytes() == b"# run\n"
    assert not (published_root / "current.json").exists()
    assert not (published_root / "v1").exists()
    assert _files_under(published_root) == ["README.md", "app/x.py", "delivery_binding.json"]

    binding = workspace_publish.read_binding(workspace, summary["delivery_id"])
    assert binding["session"] == "sess-1"
    assert binding["turn"] == "turn-9"
    assert binding["run"] == "run-1"
    assert binding["manifest_sha256"] == summary["manifest_sha256"]
    assert sorted(binding["files"]) == ["README.md", "app/x.py"]

    # The summary is JSON-safe and never carries file contents.
    blob = json.dumps(summary, ensure_ascii=False)
    for secret in ("print(1)", "# run", "old package"):
        assert secret not in blob


# --- ② idempotent repeat ----------------------------------------------------


def test_repeat_publish_of_unchanged_run_is_skipped_identical(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)
    kwargs: dict[str, Any] = {"run_dir": run_dir, "workspace_root": workspace, "session": "s", "turn": "t", "run": "run-1"}

    first = worker.deliver_run_to_workspace(**kwargs)
    assert first["status"] == "published"
    published_root = Path(first["published_root"])
    before = {rel: _signature(published_root / rel) for rel in ("README.md", "app/x.py")}

    second = worker.deliver_run_to_workspace(**kwargs)

    assert second["status"] == "published"
    assert second["reason"] == "skipped_identical"
    assert second["published"] == 0
    assert second["skipped_identical"] == 2
    assert second["conflicts"] == 0
    assert second["delivery_id"] == first["delivery_id"]
    assert second["manifest_sha256"] == first["manifest_sha256"]
    after = {rel: _signature(published_root / rel) for rel in ("README.md", "app/x.py")}
    assert after == before  # identical bytes were not rewritten


# --- ③ conflicting change refuses the whole order ---------------------------


def test_conflicting_change_is_refused_and_target_is_untouched(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)
    kwargs: dict[str, Any] = {"run_dir": run_dir, "workspace_root": workspace, "session": "s", "turn": "t", "run": "run-1"}

    first = worker.deliver_run_to_workspace(**kwargs)
    assert first["status"] == "published"
    published_root = Path(first["published_root"])
    target = published_root / "app" / "x.py"
    target_before = _signature(target)

    (run_dir / "deliverables" / "app" / "x.py").write_bytes(b"print(2)\n")
    _accept_run(run_dir)
    second = worker.deliver_run_to_workspace(**kwargs)

    assert second["status"] == "refused"
    assert second["reason"] == "conflicts"
    assert second["conflicts"] == 1
    assert second["published"] == 0
    assert second["skipped_identical"] == 1
    assert second["delivery_id"] == first["delivery_id"]
    assert (target).read_bytes() == b"print(1)\n"
    assert _signature(target) == target_before


# --- ④ skipped reasons ------------------------------------------------------


def test_skips_have_stable_reasons_and_write_nothing(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)

    assert worker.deliver_run_to_workspace(run_dir=run_dir, workspace_root="") == {
        "status": "skipped", "reason": "no_workspace"}
    assert worker.deliver_run_to_workspace(run_dir="", workspace_root=workspace) == {
        "status": "skipped", "reason": "no_run_dir"}
    assert worker.deliver_run_to_workspace(run_dir=tmp_path / "gone", workspace_root=workspace) == {
        "status": "skipped", "reason": "no_run_dir"}

    bare = tmp_path / "bare-run"
    bare.mkdir()
    assert worker.deliver_run_to_workspace(run_dir=bare, workspace_root=workspace) == {
        "status": "skipped", "reason": "no_deliverables"}

    metadata_only = tmp_path / "meta-run" / "deliverables"
    (metadata_only / "v1").mkdir(parents=True)
    (metadata_only / "current.json").write_bytes(b"{}")
    (metadata_only / "v1" / "old.md").write_bytes(b"old")
    assert worker.deliver_run_to_workspace(run_dir=metadata_only.parent, workspace_root=workspace) == {
        "status": "skipped", "reason": "no_deliverables"}

    assert not (workspace / workspace_publish.DELIVERY_DIRNAME).exists()


# --- ⑤ internal failure is reported, never raised ---------------------------


def test_internal_publish_failure_is_reported_never_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)
    # The lazy accessor must resolve to the very module this test patches.
    assert worker._workspace_publish() is workspace_publish

    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("publisher exploded")

    monkeypatch.setattr(workspace_publish, "publish_deliverables", boom)

    summary = worker.deliver_run_to_workspace(
        run_dir=run_dir, workspace_root=workspace, session="s", turn="t", run="run-1")

    assert summary["status"] == "failed"
    assert summary["reason"].startswith("RuntimeError")
    assert "print(1)" not in json.dumps(summary)
    assert not (workspace / workspace_publish.DELIVERY_DIRNAME).exists()


# --- ⑥ negative control: nothing outside the delivery area ------------------


def test_nothing_is_written_outside_the_delivery_area(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)
    user_file = workspace / "README.md"
    user_file.write_bytes(b"user-owned\n")
    user_dir_file = workspace / "app" / "x.py"
    user_dir_file.parent.mkdir(parents=True)
    user_dir_file.write_bytes(b"user-owned-code\n")
    user_before = _signature(user_file)
    user_code_before = _signature(user_dir_file)
    before = _tree(workspace)

    summary = worker.deliver_run_to_workspace(
        run_dir=run_dir, workspace_root=workspace, session="s", turn="t", run="run-1")
    assert summary["status"] == "published"

    created = sorted(set(_tree(workspace)) - set(before))
    assert created, "the publish must have produced something"
    assert all(rel.split("/")[0] == workspace_publish.DELIVERY_DIRNAME for rel in created)
    assert _signature(user_file) == user_before
    assert _signature(user_dir_file) == user_code_before
    assert not (workspace / "v1").exists()
    assert not (workspace / "current.json").exists()


# --- ⑦ the turn-level reporter ----------------------------------------------


class _StatusController:
    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir

    def status(self) -> dict[str, Any]:
        return {"run_dir": str(self._run_dir)}


def test_deliver_completed_turn_reports_published_then_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, name="run-9")
    workspace = _ws(tmp_path)
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(workspace))
    monkeypatch.setenv("ASTRALNEXUS_CHAT_SESSION", "sess-1")
    events: list[tuple[str, dict[str, Any]]] = []
    controller = _StatusController(run_dir)
    request = {"home": str(tmp_path / "chat" / "sess-1" / "turn-9")}

    summary = worker.deliver_completed_turn(
        request=request, controller=controller, emit=lambda kind, payload: events.append((kind, payload)))

    assert summary["status"] == "published"
    assert summary["delivery_id"].startswith("run-") and len(summary["delivery_id"]) == 24
    kinds = [payload.get("type") for kind, payload in events if kind == "progress"]
    assert kinds == ["delivery.publish"]
    assert events[0][1]["status"] == "published"

    (run_dir / "deliverables" / "app" / "x.py").write_bytes(b"print(2)\n")
    _accept_run(run_dir)
    events.clear()
    second = worker.deliver_completed_turn(
        request=request, controller=controller, emit=lambda kind, payload: events.append((kind, payload)))

    assert second["status"] == "refused"
    assert [payload.get("type") for kind, payload in events if kind == "progress"] == [
        "delivery.publish", "delivery.publish.refused"]
    refusal = [payload for kind, payload in events if payload.get("type") == "delivery.publish.refused"]
    assert refusal == [{"type": "delivery.publish.refused", "reason": "conflicts"}]


def test_publish_uses_the_captured_session_workspace_not_the_mutated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live fix4: the run dir became the publish root.

    The host injects the session workspace into the worker
    (``runtime/workbench_chat.py:249``), but the agent runtime rewrites
    ``ASTRALNEXUS_WORKSPACE`` to the run dir because the tool layer uses it as
    the write boundary (``cli/main.py:1296`` -- correct for tools).  The
    publisher re-read that same variable, so 43 correct deliverables landed in
    ``<run_dir>/.astralnexus_delivery/<id>/published`` while the session
    workspace stayed empty and R1 reported no publication at all.
    """
    run_dir = _make_run(tmp_path, name="run-mut")
    workspace = _ws(tmp_path)
    session_workspace = str(workspace)          # what the host injected
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(run_dir))   # what the env holds now
    monkeypatch.setenv("ASTRALNEXUS_CHAT_SESSION", "sess-mut")

    summary = worker.deliver_completed_turn(
        request={"home": str(tmp_path / "chat" / "sess-mut" / "turn-mut")},
        controller=_StatusController(run_dir),
        emit=lambda kind, payload: None,
        workspace_root=session_workspace,
    )

    assert summary["status"] == "published"
    assert (workspace / ".astralnexus_delivery").is_dir()
    assert not (run_dir / ".astralnexus_delivery").exists()


def test_publish_falls_back_to_the_env_when_no_workspace_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backwards compatibility: the existing call shape still works."""
    run_dir = _make_run(tmp_path, name="run-env")
    workspace = _ws(tmp_path)
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(workspace))
    monkeypatch.setenv("ASTRALNEXUS_CHAT_SESSION", "sess-env")

    summary = worker.deliver_completed_turn(
        request={"home": str(tmp_path / "chat" / "sess-env" / "turn-env")},
        controller=_StatusController(run_dir),
        emit=lambda kind, payload: None,
    )

    assert summary["status"] == "published"
    assert (workspace / ".astralnexus_delivery").is_dir()


def test_deliver_completed_turn_survives_a_broken_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(_ws(tmp_path)))

    class _Broken:
        def status(self) -> dict[str, Any]:
            raise RuntimeError("no status")

    events: list[dict[str, Any]] = []
    summary = worker.deliver_completed_turn(
        request={"home": "x"}, controller=_Broken(), emit=lambda kind, payload: events.append(payload))

    assert summary["status"] == "skipped"
    assert summary["reason"] == "no_run_dir"
    assert events[0]["type"] == "delivery.publish"


# --- ⑧ main() calls the delivery path only for a completed turn -------------


def _stub_astralnexus(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> None:
    """Replace the heavy AstralNexus imports main() performs with inert stubs."""

    class _FakeController:
        def __init__(self, home: Path) -> None:
            self.home = home
            self.interrupts: list[str] = []

        def set_event_callback(self, callback: Any) -> None:
            pass

        def interrupt(self, reason: str) -> None:
            self.interrupts.append(reason)

        def status(self) -> dict[str, Any]:
            return {"run_dir": str(run_dir)}

    root = types.ModuleType("AstralNexus")
    root.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("AstralNexus.core")
    core.__path__ = []  # type: ignore[attr-defined]
    identity = types.ModuleType("AstralNexus.core.identity")
    setattr(identity, "bootstrap_environment", lambda: None)
    cli = types.ModuleType("AstralNexus.cli")
    cli.__path__ = []  # type: ignore[attr-defined]
    cli_main = types.ModuleType("AstralNexus.cli.main")
    setattr(cli_main, "_InteractiveRunController", _FakeController)
    setattr(cli_main, "_register_interactive_delegate_tool", lambda controller, home: None)
    setattr(cli_main, "_govern_interactive_turn", lambda *args, **kwargs: False)
    setattr(cli_main, "_interactive_chat_turn", lambda *args, **kwargs: None)
    tools = types.ModuleType("AstralNexus.tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    approval = types.ModuleType("AstralNexus.tools.approval")
    setattr(approval, "set_approval_provider", lambda provider: None)

    for name, module in {
        "AstralNexus": root,
        "AstralNexus.core": core,
        "AstralNexus.core.identity": identity,
        "AstralNexus.cli": cli,
        "AstralNexus.cli.main": cli_main,
        "AstralNexus.tools": tools,
        "AstralNexus.tools.approval": approval,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.mark.parametrize("status,expected_code,expected_calls", [("completed", 0, 1), ("failed", 1, 0)])
def test_main_delivers_only_a_completed_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_code: int,
    expected_calls: int,
) -> None:
    """Thin wiring test: main() stubs everything AstralNexus-heavy it imports.

    It proves the call site and its arguments (workspace captured BEFORE the
    turn, turn from the request home, run from the controller) and that a non
    ``completed`` turn never enters the delivery path.  The real publish itself
    is covered by the tests above.
    """
    run_dir = _make_run(tmp_path)
    home = tmp_path / "chat" / "sess-1" / "turn-9"
    home.mkdir(parents=True)
    _stub_astralnexus(monkeypatch, run_dir)

    inbound = io.StringIO(json.dumps({"message": "hi", "history": [], "home": str(home)}) + "\n")
    outbound = io.StringIO()
    monkeypatch.setattr(worker, "_open_channel", lambda: (inbound, outbound, None))

    def fake_execute_turn(*args: Any, **kwargs: Any) -> str:
        # The agent runtime rewrites ASTRALNEXUS_WORKSPACE for the tool write
        # boundary (cli/main.py:1296).  Delivery must not inherit that.
        os.environ["ASTRALNEXUS_WORKSPACE"] = str(run_dir)
        return status

    monkeypatch.setattr(worker, "execute_turn", fake_execute_turn)
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(_ws(tmp_path)))
    monkeypatch.setenv("ASTRALNEXUS_CHAT_SESSION", "sess-1")

    calls: list[dict[str, Any]] = []

    def recorder(*, request: dict[str, Any], controller: Any, emit: Any,
                 workspace_root: str = "") -> dict[str, Any]:
        calls.append({"request": request, "controller": controller, "emit": emit,
                      "workspace_root": workspace_root})
        return {"status": "published", "reason": "published"}

    monkeypatch.setattr(worker, "deliver_completed_turn", recorder)

    with pytest.raises(SystemExit) as exit_info:
        worker.main()

    assert exit_info.value.code == expected_code
    assert len(calls) == expected_calls
    if expected_calls:
        # The session workspace as injected, not the run dir the env now holds.
        assert calls[0]["workspace_root"] == str(_ws(tmp_path))
    events = [json.loads(line) for line in outbound.getvalue().splitlines() if line.strip()]
    assert {"kind": "turn.result", "payload": {"status": status}} in events
    if calls:
        assert calls[0]["request"]["home"] == str(home)
        assert calls[0]["controller"].status()["run_dir"] == str(run_dir)


# --- ⑨ cross-session isolation: a fresher delivery is not this acceptance ----


def test_other_sessions_fresher_delivery_is_never_selected(tmp_path: Path) -> None:
    """Scenario 7 reversal: only this run's own deliverables may be published.

    A newer, complete delivery already sitting in the same workspace (real
    binding, real bytes, strictly newer mtimes than this test's clock) belongs
    to another session and must be neither adopted as this run's acceptance nor
    rewritten by this run.
    """
    workspace = _ws(tmp_path)
    other_run = _make_run(tmp_path, name="run-b")
    (other_run / "deliverables" / "app" / "x.py").write_bytes(b"print('other-session')\n")
    _accept_run(other_run)
    other = worker.deliver_run_to_workspace(
        run_dir=other_run, workspace_root=workspace, session="sess-B", turn="turn-B", run="run-b"
    )
    assert other["status"] == "published"
    assert workspace_publish.read_binding(workspace, other["delivery_id"])["session"] == "sess-B"

    delivery_root = workspace / workspace_publish.DELIVERY_DIRNAME
    stamp = time.time() + 3600
    for path in sorted(delivery_root.rglob("*"), reverse=True):
        os.utime(path, (stamp, stamp))
    before = {rel: _signature(delivery_root / rel) for rel in _files_under(delivery_root)}
    assert before, "the other session's delivery must have real bytes"
    # Sensor: every file of the other session's delivery is newer than "now", so
    # any newest-wins selection would have to prefer it over this run's tree.
    assert min(signature[0] for signature in before.values()) > time.time_ns()

    bare = tmp_path / "run-a"
    bare.mkdir()
    summary = worker.deliver_run_to_workspace(
        run_dir=bare, workspace_root=workspace, session="sess-A", turn="turn-A", run="run-a"
    )

    assert summary["status"] == "skipped"
    assert summary["reason"] == "no_deliverables"
    after = {rel: _signature(delivery_root / rel) for rel in _files_under(delivery_root)}
    assert after == before
    assert sorted(path.name for path in delivery_root.iterdir() if path.is_dir()) == [
        other["delivery_id"]]


def test_another_session_of_the_same_run_never_shares_or_overwrites(tmp_path: Path) -> None:
    """Scenario 7 reversal: one run stem, two sessions, two separate areas."""
    run_dir = _make_run(tmp_path, name="run-1")
    workspace = _ws(tmp_path)
    first = worker.deliver_run_to_workspace(
        run_dir=run_dir, workspace_root=workspace, session="sess-A", turn="turn-1", run="run-1"
    )
    assert first["status"] == "published"
    root_a = Path(first["published_root"])

    (run_dir / "deliverables" / "app" / "x.py").write_bytes(b"print(2)\n")
    _accept_run(run_dir)
    second = worker.deliver_run_to_workspace(
        run_dir=run_dir, workspace_root=workspace, session="sess-B", turn="turn-2", run="run-1"
    )

    # Another session's newer bytes are a fresh delivery, never a conflict with
    # (and never an overwrite of) the acceptance another session already got.
    assert second["status"] == "published"
    assert second["reason"] == "published"
    assert second["delivery_id"] != first["delivery_id"]
    root_b = Path(second["published_root"])
    assert root_a != root_b
    assert (root_a / "app" / "x.py").read_bytes() == b"print(1)\n"
    assert (root_b / "app" / "x.py").read_bytes() == b"print(2)\n"
    binding_a = workspace_publish.read_binding(workspace, first["delivery_id"])
    binding_b = workspace_publish.read_binding(workspace, second["delivery_id"])
    assert (binding_a["session"], binding_a["turn"]) == ("sess-A", "turn-1")
    assert (binding_b["session"], binding_b["turn"]) == ("sess-B", "turn-2")
    assert binding_a["manifest_sha256"] != binding_b["manifest_sha256"]


# --- ⑩ a cancelled or interrupted turn never claims a delivery ---------------


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_main_never_delivers_a_cancelled_or_interrupted_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Scenario 8 reversal: only a real ``completed`` turn reaches delivery."""
    run_dir = _make_run(tmp_path)
    home = tmp_path / "chat" / "sess-1" / "turn-9"
    home.mkdir(parents=True)
    _stub_astralnexus(monkeypatch, run_dir)

    inbound = io.StringIO(json.dumps({"message": "hi", "history": [], "home": str(home)}) + "\n")
    outbound = io.StringIO()
    monkeypatch.setattr(worker, "_open_channel", lambda: (inbound, outbound, None))
    monkeypatch.setattr(worker, "execute_turn", lambda *args, **kwargs: status)
    workspace = _ws(tmp_path)
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(workspace))
    monkeypatch.setenv("ASTRALNEXUS_CHAT_SESSION", "sess-1")

    calls: list[dict[str, Any]] = []

    def _record_publish(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "published", "reason": "published"}

    monkeypatch.setattr(worker, "deliver_completed_turn", _record_publish)

    with pytest.raises(SystemExit) as exit_info:
        worker.main()

    assert exit_info.value.code == 1
    assert calls == []
    events = [json.loads(line) for line in outbound.getvalue().splitlines() if line.strip()]
    assert {"kind": "turn.result", "payload": {"status": status}} in events
    assert not [event for event in events if event["kind"].startswith("delivery")]
    assert not (workspace / workspace_publish.DELIVERY_DIRNAME).exists()


def test_publishing_spawns_no_child_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 8 reversal: an interrupted publish leaves no runaway child.

    Publishing is in-process.  A copy that shelled out (``subprocess``,
    ``os.system``, ``os.spawn*``) would both trip this and be exactly the
    uncontrolled helper an interrupted turn must never leave behind.
    """
    spawned: list[str] = []

    def _forbid(name: str) -> Any:
        def _called(*args: Any, **kwargs: Any) -> Any:
            spawned.append(name)
            raise AssertionError(f"publishing must not spawn a child process ({name})")

        return _called

    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen"))
    monkeypatch.setattr(os, "system", _forbid("os.system"))
    for name in ("posix_spawn", "posix_spawnp", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _forbid(f"os.{name}"))

    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)
    summary = worker.deliver_run_to_workspace(
        run_dir=run_dir, workspace_root=workspace, session="sess-1", turn="turn-9", run="run-1"
    )

    assert spawned == []
    assert summary["status"] == "published"
    assert summary["published"] == 2


def test_the_cancel_reader_cannot_outlive_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 8 reversal: a cancelled turn leaves no live reader behind."""
    run_dir = _make_run(tmp_path)
    home = tmp_path / "chat" / "sess-1" / "turn-9"
    home.mkdir(parents=True)
    _stub_astralnexus(monkeypatch, run_dir)

    inbound = io.StringIO(
        json.dumps({"message": "hi", "history": [], "home": str(home)}) + "\n"
        + json.dumps({"type": "cancel"}) + "\n"
    )
    outbound = io.StringIO()
    monkeypatch.setattr(worker, "_open_channel", lambda: (inbound, outbound, None))
    monkeypatch.setattr(worker, "execute_turn", lambda *args, **kwargs: "cancelled")
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(_ws(tmp_path)))

    real_thread = threading.Thread
    started: list[Any] = []

    class _RecordingThread(real_thread):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(worker.threading, "Thread", _RecordingThread)

    with pytest.raises(SystemExit):
        worker.main()

    assert started, "main must start the inbound reader"
    for thread in started:
        thread.join(timeout=5)
    assert [thread.daemon for thread in started] == [True] * len(started)
    assert [thread.is_alive() for thread in started] == [False] * len(started)


def test_an_interrupted_publish_is_reported_refused_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 8 reversal: a rolled-back publish is reported as a refusal.

    This pins the summary mapping: an interrupted (rolled-back) publish is
    reported as ``refused`` with reason ``rolled_back`` on the progress stream
    -- never as ``published``.
    """
    run_dir = _make_run(tmp_path)
    workspace = _ws(tmp_path)
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(workspace))
    monkeypatch.setenv("ASTRALNEXUS_CHAT_SESSION", "sess-1")
    delivery_id = "run-1-sess-1-turn-9"

    def interrupted(**kwargs: Any) -> Any:
        return workspace_publish.PublishResult(
            delivery_id=delivery_id,
            run_dir=str(run_dir),
            workspace_root=str(workspace),
            published_root=str(
                workspace / workspace_publish.DELIVERY_DIRNAME / delivery_id / "published"),
            published=(),
            skipped_identical=(),
            conflicts=(),
            refused=("app/x.py",),
            manifest_sha256="a" * 64,
            journal_path=str(
                workspace / workspace_publish.DELIVERY_DIRNAME
                / f"{workspace_publish.JOURNAL_PREFIX}{delivery_id}.json"),
            rolled_back=True,
            resumed=True,
        )

    monkeypatch.setattr(workspace_publish, "publish_deliverables", interrupted)

    events: list[tuple[str, dict[str, Any]]] = []
    summary = worker.deliver_completed_turn(
        request={"home": str(tmp_path / "chat" / "sess-1" / "turn-9")},
        controller=_StatusController(run_dir),
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    assert summary["status"] == "refused"
    assert summary["reason"] == "rolled_back"
    assert summary["rolled_back"] is True
    assert summary["published"] == 0
    assert summary["refused"] == 1
    assert [payload.get("type") for kind, payload in events if kind == "progress"] == [
        "delivery.publish", "delivery.publish.refused"]
    assert [payload for kind, payload in events if payload.get("type") == "delivery.publish.refused"] == [
        {"type": "delivery.publish.refused", "reason": "rolled_back"}]



def test_unaccepted_runtime_tree_cannot_be_published(tmp_path):
    run, _ = make_run(tmp_path, {"app/x.py": b"unverified"})
    result = worker.deliver_run_to_workspace(run_dir=run, workspace_root=_ws(tmp_path))
    assert result["status"] == "failed"
    assert result["reason"] == "no_accepted_publication"


def test_workspace_uses_accepted_version_not_extra_runtime_files(tmp_path):
    run = _make_run(tmp_path)
    (run / "deliverables" / "secret.txt").write_text("unverified old file")
    result = worker.deliver_run_to_workspace(run_dir=run, workspace_root=_ws(tmp_path))
    assert result["status"] == "published"
    assert not (Path(result["published_root"]) / "secret.txt").exists()


def test_delivery_directory_is_short_stable_and_scope_bound(tmp_path):
    from AstralNexus.runtime.workbench_chat_worker import _delivery_id
    root=tmp_path/("task_"+"x"*80)
    args={"run_dir":root,"session":"s"*32,"turn":"t"*32,"run":"r"*80}
    identity=_delivery_id(**args)
    assert len(identity)==24 and identity.startswith("run-")
    assert _delivery_id(**args)==identity
    assert _delivery_id(**{**args,"turn":"u"*32})!=identity
    assert _delivery_id(run_dir=root,run="a-b",session="c",turn="")!=_delivery_id(run_dir=root,run="a",session="b-c",turn="")
