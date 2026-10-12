"""Contract tests for AstralNexus.runtime.directory_picker (the four-package
directory-picking seam: base + native + browse + auto).

Covers the typed error, the abstract service, every native dispatch branch
through the run/dialog-runner seams (darwin osascript with its user-canceled
shape, the win32 dialog driver's cancel/abort/failure paths, linux
zenity→kdialog fallback with the loud install-hint failure, cancellation
always preceding a cancel-shaped exit code), the browse pure functions
(crumbs, the fully-qualified rails including drive-less rooted and
incomplete-UNC refusals, the O(keep) bounded window, symlink-following rows),
the browse backend against real temporary filesystems (ordering, truncation,
hidden flagged-but-returned, broken-link skipping, typed failures), the auto
resolver's complete decision matrix, the boot-time mount, and the host HTTP
surface (projection, browse routes, wrong-kind refusals, typed 400s).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.runtime import directory_picker as dp
from AstralNexus.runtime import native_command as nc


class RecordingRun:
    def __init__(self, results=None):
        self.calls: List[tuple] = []
        self.results = results or {}

    def __call__(self, command: str, args: List[str], cancel: Any = None):
        self.calls.append((command, tuple(args)))
        outcome = self.results.get(command)
        if isinstance(outcome, nc.NativeCommandError):
            raise outcome
        return outcome or {"stdout": "", "stderr": ""}


# -------------------------------------------------------------------- base

class TestBase:
    def test_error_carries_code_path_message(self) -> None:
        error = dp.DirectoryPickerError("directory-exists", "/x", "boom")
        assert (error.code, error.path, error.name) == \
            ("directory-exists", "/x", "DirectoryPickerError")
        assert str(error) == "boom"

    def test_abstract_service_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            dp.DirectoryPicker()  # type: ignore[abstract]


# ------------------------------------------------------------------ native

class TestNativeDispatch:
    def darwin(self, run):
        return {"platform": "darwin", "run": run}

    def test_darwin_returns_the_posix_path(self) -> None:
        run = RecordingRun({"osascript": {"stdout": "/Users/x/ws\n", "stderr": ""}})
        assert dp.pick_native_directory(None, self.darwin(run)) == "/Users/x/ws"
        assert run.calls[0][0] == "osascript"
        assert any("choose folder" in arg for arg in run.calls[0][1])

    def test_darwin_user_canceled_shape_is_none(self) -> None:
        run = RecordingRun({"osascript": nc.NativeCommandError(
            "canceled", code=1, stderr="execution error: User canceled. (-128)")})
        assert dp.pick_native_directory(None, self.darwin(run)) is None

    def test_darwin_bare_minus_128_is_none(self) -> None:
        run = RecordingRun({"osascript": nc.NativeCommandError(
            "canceled", code=1, stderr="osascript: -128")})
        assert dp.pick_native_directory(None, self.darwin(run)) is None

    def test_darwin_other_exit_one_rethrows(self) -> None:
        error = nc.NativeCommandError("boom", code=1, stderr="syntax error")
        run = RecordingRun({"osascript": error})
        with pytest.raises(nc.NativeCommandError) as exc:
            dp.pick_native_directory(None, self.darwin(run))
        assert exc.value is error

    def test_darwin_non_one_code_rethrows(self) -> None:
        run = RecordingRun({"osascript": nc.NativeCommandError(
            "x", code=2, stderr="User canceled. (-128)")})
        with pytest.raises(nc.NativeCommandError):
            dp.pick_native_directory(None, self.darwin(run))

    def test_cancelled_token_wins_over_cancel_shaped_exit(self) -> None:
        cancel = threading.Event()
        cancel.set()
        run = RecordingRun({"osascript": nc.NativeCommandError(
            "canceled", code=1, stderr="User canceled. (-128)")})
        with pytest.raises(nc.NativeCommandError) as exc:
            dp.pick_native_directory(cancel, self.darwin(run))
        assert exc.value.code == "ABORT_ERR"

    def test_win32_dispatches_to_the_dialog_driver(self) -> None:
        seen: Dict[str, Any] = {}

        def runner(cancel):
            seen["cancel"] = cancel
            return {"stdout": "C:\\chosen\r\n", "stderr": ""}

        result = dp.pick_native_directory(None, {"platform": "win32",
                                                 "dialog_runner": runner})
        assert result == "C:\\chosen"

    def test_unsupported_platform(self) -> None:
        with pytest.raises(RuntimeError,
                           match="native directory picker is unsupported on freebsd"):
            dp.pick_native_directory(None, {"platform": "freebsd",
                                            "run": RecordingRun()})


class TestWin32DialogDriver:
    def test_pre_cancelled_aborts_before_spawning(self) -> None:
        spawned: List[int] = []

        def runner(cancel):
            spawned.append(1)
            return {"stdout": "", "stderr": ""}

        cancel = threading.Event()
        cancel.set()
        with pytest.raises(nc.NativeCommandError) as exc:
            dp.pick_win32_directory(cancel, {"dialog_runner": runner})
        assert exc.value.code == "ABORT_ERR"
        assert spawned == []

    def test_empty_stdout_is_a_dialog_cancel(self) -> None:
        def runner(cancel):
            return {"stdout": "\r\n", "stderr": ""}

        assert dp.pick_win32_directory(None, {"dialog_runner": runner}) is None

    def test_runner_cancel_becomes_the_abort(self) -> None:
        def runner(cancel):
            raise nc.NativeCommandError("cancelled", code="ABORT_ERR",
                                        cancelled=True)

        with pytest.raises(RuntimeError, match="native directory picker aborted"):
            dp.pick_win32_directory(None, {"dialog_runner": runner})

    def test_runner_failure_names_the_dialog(self) -> None:
        def runner(cancel):
            raise nc.NativeCommandError("dialog crashed", code=1, stderr="boom")

        with pytest.raises(RuntimeError,
                           match="win32 folder dialog failed: dialog crashed"):
            dp.pick_win32_directory(None, {"dialog_runner": runner})


class TestLinuxDispatch:
    def linux(self, run):
        return {"platform": "linux", "run": run}

    def test_zenity_success(self) -> None:
        run = RecordingRun({"zenity": {"stdout": "/home/x/ws\n", "stderr": ""}})
        assert dp.pick_native_directory(None, self.linux(run)) == "/home/x/ws"
        assert run.calls[0][1][:2] == ("--file-selection", "--directory")

    def test_zenity_cancel_is_none(self) -> None:
        run = RecordingRun({"zenity": nc.NativeCommandError("", code=1)})
        assert dp.pick_native_directory(None, self.linux(run)) is None

    def test_zenity_error_code_rethrows(self) -> None:
        run = RecordingRun({"zenity": nc.NativeCommandError("", code=2)})
        with pytest.raises(nc.NativeCommandError):
            dp.pick_native_directory(None, self.linux(run))

    def test_kdialog_fallback_when_zenity_missing(self) -> None:
        run = RecordingRun({
            "zenity": nc.NativeCommandError("spawn", code="ENOENT"),
            "kdialog": {"stdout": "/home/x/ws\n", "stderr": ""}})
        assert dp.pick_native_directory(None, self.linux(run)) == "/home/x/ws"
        assert run.calls[1][0] == "kdialog"
        assert run.calls[1][1] == ("--getexistingdirectory", ".", "--title",
                                   "Select Workspace Directory")

    def test_kdialog_cancel_is_none(self) -> None:
        run = RecordingRun({
            "zenity": nc.NativeCommandError("spawn", code="ENOENT"),
            "kdialog": nc.NativeCommandError("", code=1)})
        assert dp.pick_native_directory(None, self.linux(run)) is None

    def test_both_missing_is_the_install_hint(self) -> None:
        run = RecordingRun({
            "zenity": nc.NativeCommandError("spawn", code="ENOENT"),
            "kdialog": nc.NativeCommandError("spawn", code="ENOENT")})
        with pytest.raises(RuntimeError,
                           match=r"no supported native directory picker found "
                                 r"\(install zenity or kdialog\)"):
            dp.pick_native_directory(None, self.linux(run))

    def test_kdialog_other_failure_rethrows(self) -> None:
        error = nc.NativeCommandError("boom", code=3)
        run = RecordingRun({
            "zenity": nc.NativeCommandError("spawn", code="ENOENT"),
            "kdialog": error})
        with pytest.raises(nc.NativeCommandError) as exc:
            dp.pick_native_directory(None, self.linux(run))
        assert exc.value is error


class TestNativeService:
    def test_capability_is_stable_and_native_kind(self) -> None:
        service = dp.NativeDirectoryPicker({"platform": "freebsd"})
        first = service.capability()
        assert first is service.capability()
        assert first["kind"] == "native"
        assert callable(first["pick"])

    def test_pick_forwards_to_the_chooser(self) -> None:
        run = RecordingRun({"zenity": {"stdout": "/ws\n", "stderr": ""}})
        service = dp.NativeDirectoryPicker({"platform": "linux", "run": run})
        assert service.capability()["pick"]() == "/ws"

    def test_is_a_directory_picker(self) -> None:
        assert isinstance(dp.NativeDirectoryPicker(), dp.DirectoryPicker)


# ------------------------------------------------------------------ browse

class TestBrowsePure:
    def test_ancestry_crumbs_root_to_target(self) -> None:
        target = os.path.join(os.path.abspath(os.sep), "a", "b")
        crumbs = dp.ancestry_crumbs(target)
        assert crumbs[-1] == {"name": "b", "path": target, "hidden": False}
        assert crumbs[0]["path"] == os.path.abspath(os.sep)
        # the root crumb names its full path (parent === current)
        assert crumbs[0]["name"] == crumbs[0]["path"]
        assert [c["name"] for c in crumbs] == [crumbs[0]["name"], "a", "b"]

    @pytest.mark.parametrize("path,expected", [
        (r"C:\x", True), (r"C:/x", True), (r"c:\x", True),
        (r"\\server\share\x", True),
        # the verbatim prefix matches the complete-UNC shape (the regex reads
        # "?" as the server segment) — upstream's rule, kept verbatim
        (r"\\?\C:\x", True),
        (r"\foo", False), ("/foo", False), (r"\\", False),
        (r"\\server", False), (r"relative\x", False), ("", False),
    ])
    def test_fully_qualified_win32(self, path: str, expected: bool) -> None:
        assert dp.fully_qualified(path, "win32") is expected

    def test_fully_qualified_posix(self) -> None:
        assert dp.fully_qualified("/x", "linux") is True
        assert dp.fully_qualified("x", "linux") is False
        # the platform argument decides the rule, not the running OS
        assert dp.fully_qualified(r"C:\x", "linux") is False

    def test_bounded_insert_sorts_and_evicts(self) -> None:
        window: List[Dict[str, Any]] = []
        for name in ("m", "a", "z"):
            assert dp.bounded_insert(window, {"name": name}, 3) is False
        assert [c["name"] for c in window] == ["a", "m", "z"]
        # the fourth candidate inserts in order and evicts the name-largest
        assert dp.bounded_insert(window, {"name": "b"}, 3) is True
        assert [c["name"] for c in window] == ["a", "b", "m"]
        assert dp.bounded_insert(window, {"name": "c"}, 3) is True
        assert [c["name"] for c in window] == ["a", "b", "c"]
        # a full window rejects a candidate >= its last without inserting
        assert dp.bounded_insert(window, {"name": "d"}, 3) is True
        assert dp.bounded_insert(window, {"name": "zz"}, 3) is True
        assert [c["name"] for c in window] == ["a", "b", "c"]

    def test_directory_row_symlink_following(self, monkeypatch,
                                             tmp_path: Path) -> None:
        # this machine lacks SeCreateSymbolicLink, so the symlink branch
        # is driven through the stat call the row itself makes
        real_isdir = os.path.isdir
        target = tmp_path / "target-dir"
        target.mkdir()
        link = str(tmp_path / "link")
        monkeypatch.setattr(dp.os.path, "isdir",
                            lambda path: path == link or real_isdir(path))
        row = dp.directory_row(str(tmp_path), "link", False, True, None)
        assert row == {"name": "link", "path": link, "hidden": False,
                       "isDirectory": True}
        # a broken or cyclic link is silently skipped
        monkeypatch.setattr(dp.os.path, "isdir",
                            lambda path: False if path == link else real_isdir(path))
        assert dp.directory_row(str(tmp_path), "link", False, True, None) is None
        # plain directories pass through
        plain = dp.directory_row(str(tmp_path), ".cfg", True, False, None)
        assert plain == {"name": ".cfg", "path": str(tmp_path / ".cfg"),
                         "hidden": True, "isDirectory": True}

    def test_validate_config(self) -> None:
        assert dp.validate_browse_config(None) == {"max_entries": 1000}
        assert dp.validate_browse_config({}) == {"max_entries": 1000}
        assert dp.validate_browse_config({"max_entries": 5, "junk": 1}) == \
            {"max_entries": 5}
        for bad in (0, -1, True, "5", 1.5):
            with pytest.raises(ValueError):
                dp.validate_browse_config({"max_entries": bad})


class TestBrowseBackend:
    def make_tree(self, tmp_path: Path, with_symlinks: bool = False) -> Path:
        root = tmp_path / "root"
        (root / "zeta").mkdir(parents=True)
        (root / "alpha").mkdir()
        (root / ".hidden").mkdir()
        (root / "file.txt").write_text("x")
        if with_symlinks:
            os.symlink(root / "zeta", str(root / "link-dir"),
                       target_is_directory=True)
            os.symlink(root / "nowhere", str(root / "link-broken"),
                       target_is_directory=True)
        return root

    def picker(self, **config):
        return dp.BrowseDirectoryPicker(config or None)

    def test_list_real_directory(self, tmp_path: Path) -> None:
        root = self.make_tree(tmp_path)
        result = self.picker().list(str(root))
        names = [entry["name"] for entry in result["entries"]]
        # directories only, name-ascending, hidden FLAGGED BUT RETURNED, the
        # file excluded
        assert names == [".hidden", "alpha", "zeta"]
        by_name = {entry["name"]: entry for entry in result["entries"]}
        assert by_name[".hidden"]["hidden"] is True
        assert by_name["alpha"]["hidden"] is False
        assert by_name["alpha"]["isDirectory"] is True
        assert by_name["alpha"]["path"] == str(root / "alpha")
        assert result["path"] == str(root)
        assert result["truncated"] is False
        assert result["crumbs"][-1]["path"] == str(root)
        assert "home" in result

    def test_list_follows_symlinks_and_skips_broken(self, tmp_path: Path) -> None:
        try:
            root = self.make_tree(tmp_path, with_symlinks=True)
        except OSError as error:
            pytest.skip(f"this machine cannot create symlinks: {error}")
        result = self.picker().list(str(root))
        names = [entry["name"] for entry in result["entries"]]
        # the symlink to a directory is followed and listed; the broken link
        # is silently skipped
        assert names == [".hidden", "alpha", "link-dir", "zeta"]
        by_name = {entry["name"]: entry for entry in result["entries"]}
        assert by_name["link-dir"]["isDirectory"] is True

    def test_list_default_is_home(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        (fake_home / "sub").mkdir(parents=True)
        picker = dp.BrowseDirectoryPicker(None, home=str(fake_home))
        result = picker.list()
        assert result["path"] == str(fake_home)
        assert result["home"] == str(fake_home)
        assert [e["name"] for e in result["entries"]] == ["sub"]

    def test_list_relative_path_is_refused(self) -> None:
        with pytest.raises(dp.DirectoryPickerError) as exc:
            self.picker().list("relative/path")
        assert exc.value.code == "directory-unreadable"
        assert exc.value.path == "relative/path"
        assert str(exc.value) == 'cannot list "relative/path": not a fully qualified path'

    def test_list_missing_directory_is_typed(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope")
        with pytest.raises(dp.DirectoryPickerError) as exc:
            self.picker().list(missing)
        assert exc.value.code == "directory-unreadable"
        assert str(exc.value).startswith(f"cannot list {missing}: ")

    def test_list_truncates_at_max_entries(self, tmp_path: Path) -> None:
        root = tmp_path / "many"
        root.mkdir()
        for index in range(5):
            (root / f"d{index}").mkdir()
        result = dp.BrowseDirectoryPicker({"max_entries": 2}).list(str(root))
        assert [e["name"] for e in result["entries"]] == ["d0", "d1"]
        assert result["truncated"] is True

    def test_truncation_false_when_window_evicted_but_no_rows_survive(
            self, monkeypatch, tmp_path: Path) -> None:
        # upstream reads `truncated` from the entry phase alone: a full
        # window whose every row drops (broken symlinks upstream; every row
        # here) lists nothing and reports NOT truncated — the evicted flag
        # never reaches it
        root = tmp_path / "evicted"
        root.mkdir()
        for index in range(3):
            (root / f"d{index}").mkdir()
        monkeypatch.setattr(dp, "directory_row",
                            lambda *args: None)
        result = dp.BrowseDirectoryPicker({"max_entries": 2}).list(str(root))
        assert result["entries"] == []
        assert result["truncated"] is False

    def test_list_cancelled_token(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            self.picker().list(str(tmp_path), cancel)

    def test_create_directory(self, tmp_path: Path) -> None:
        created = self.picker().create_directory(str(tmp_path), "child")
        assert created == str(tmp_path / "child")
        assert (tmp_path / "child").is_dir()

    def test_create_existing_is_directory_exists(self, tmp_path: Path) -> None:
        (tmp_path / "child").mkdir()
        with pytest.raises(dp.DirectoryPickerError) as exc:
            self.picker().create_directory(str(tmp_path), "child")
        assert exc.value.code == "directory-exists"
        assert str(exc.value) == f"{tmp_path / 'child'} already exists"

    @pytest.mark.parametrize("name", ["", "  ", ".", "..", "a/b", "a\\b"])
    def test_create_rejects_non_single_segments(self, tmp_path: Path,
                                                name: str) -> None:
        with pytest.raises(dp.DirectoryPickerError) as exc:
            self.picker().create_directory(str(tmp_path), name)
        assert exc.value.code == "directory-create-failed"
        assert "is not a single path segment" in str(exc.value)

    def test_create_under_relative_parent_is_refused(self) -> None:
        with pytest.raises(dp.DirectoryPickerError) as exc:
            self.picker().create_directory("rel/parent", "child")
        assert exc.value.code == "directory-create-failed"
        assert "not a fully qualified parent path" in str(exc.value)

    def test_create_failure_is_typed(self) -> None:
        # a qualified path that cannot hold children (a file as parent)
        with pytest.raises(dp.DirectoryPickerError) as exc:
            self.picker().create_directory(
                os.path.abspath(__file__), "child")
        assert exc.value.code == "directory-create-failed"
        assert str(exc.value).startswith("cannot create ")

    def test_capability_is_stable_browse_kind(self) -> None:
        picker = self.picker()
        capability = picker.capability()
        assert capability is picker.capability()
        assert capability["kind"] == "browse"
        assert callable(capability["list"])
        assert callable(capability["createDirectory"])


# -------------------------------------------------------------------- auto

class TestResolver:
    def facts(self, **overrides):
        base = {"bind_host": "127.0.0.1", "platform": "win32", "ssh": False,
                "env": {}, "linux_chooser": False}
        return {**base, **overrides}

    def test_loopback_desktop_windows_is_native(self) -> None:
        assert dp.resolve_directory_picker_backend(self.facts()) == "native"

    def test_all_interfaces_bind_is_browse(self) -> None:
        assert dp.resolve_directory_picker_backend(
            self.facts(bind_host="0.0.0.0")) == "browse"

    def test_ssh_launch_is_browse(self) -> None:
        assert dp.resolve_directory_picker_backend(self.facts(ssh=True)) == "browse"

    def test_darwin_assumes_a_display(self) -> None:
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="darwin")) == "native"

    def test_linux_needs_chooser_and_display(self) -> None:
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="linux")) == "browse"  # no chooser
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="linux", linux_chooser=True)) == "browse"  # no DISPLAY
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="linux", linux_chooser=True,
                       env={"DISPLAY": ":0"})) == "native"
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="linux", linux_chooser=True,
                       env={"WAYLAND_DISPLAY": "wayland-0"})) == "native"
        # a blank export is "unset" by shell convention
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="linux", linux_chooser=True,
                       env={"DISPLAY": ""})) == "browse"

    def test_other_platforms_are_never_native(self) -> None:
        assert dp.resolve_directory_picker_backend(
            self.facts(platform="freebsd", linux_chooser=True,
                       env={"DISPLAY": ":0"})) == "browse"

    def test_package_vocabulary_is_upstream_verbatim(self) -> None:
        assert dp.BACKEND_PACKAGES == {
            "native": "@deepseek-ai/dsh-host-directory-picker-native",
            "browse": "@deepseek-ai/dsh-host-directory-picker-browse"}
        assert dp.SURFACE_PACKAGES == {
            "native": "@deepseek-ai/dsh-client-ui-directory-picker-native",
            "browse": "@deepseek-ai/dsh-client-ui-directory-picker-browse"}


class TestProbe:
    def test_has_linux_chooser_binary_scans_path(self, tmp_path: Path) -> None:
        binary_dir = tmp_path / "bin"
        binary_dir.mkdir()
        (binary_dir / "zenity").write_text("#!/bin/sh\n")
        path_value = f"{tmp_path / 'empty'}{os.pathsep}{binary_dir}{os.pathsep}"
        assert dp.has_linux_chooser_binary(path_value,
                                           lambda candidate: candidate.endswith(
                                               os.path.join("bin", "zenity"))) is True
        assert dp.has_linux_chooser_binary(
            path_value, lambda candidate: False) is False
        assert dp.has_linux_chooser_binary(None, lambda candidate: True) is False
        assert dp.has_linux_chooser_binary("", lambda candidate: True) is False

    def test_can_execute_real_files(self, tmp_path: Path) -> None:
        existing = tmp_path / "tool"
        existing.write_text("x")
        assert dp.can_execute(str(existing)) is True
        assert dp.can_execute(str(tmp_path / "missing")) is False

    def test_mount_resolves_once_per_boot(self) -> None:
        picker, kind = dp.mount_directory_picker(
            "127.0.0.1", ssh=False, env={}, platform="win32")
        assert kind == "native"
        assert isinstance(picker, dp.NativeDirectoryPicker)
        picker, kind = dp.mount_directory_picker(
            "127.0.0.1", ssh=True, env={}, platform="win32")
        assert kind == "browse"
        assert isinstance(picker, dp.BrowseDirectoryPicker)
        picker, kind = dp.mount_directory_picker(
            "0.0.0.0", ssh=False, env={}, platform="darwin")
        assert kind == "browse"


# --------------------------------------------------------------- host HTTP

@pytest.fixture()
def host(tmp_path: Path):
    from AstralNexus.dashboard.workbench_server import WorkbenchHost
    instance = WorkbenchHost(tmp_path / "home")
    yield instance
    instance.close()


class TestHostHttp:
    def test_mounted_native_on_windows_desktop(self, host) -> None:
        # this migration machine is a loopback-bound win32 desktop: the
        # resolver's own matrix says native
        if sys.platform == "win32":
            assert host.directory_picker_kind == "native"
        assert host.plugins.services["directory_picker"] is host.directory_picker

    def test_projection_and_snapshot(self, host) -> None:
        client = host.app.test_client()
        response = client.get("/api/host/directory-picker")
        assert response.status_code == 200
        assert response.get_json() == {"kind": host.directory_picker_kind}
        snapshot = client.get("/api/host").get_json()
        assert snapshot["directory_picker"] == {"kind": host.directory_picker_kind}

    def test_wrong_kind_routes_refuse(self, host) -> None:
        client = host.app.test_client()
        # force the browse face onto a native-mounted host to exercise the
        # discriminated refusals in both directions
        if host.directory_picker_kind == "native":
            response = client.get("/api/host/directory-picker/list")
            assert response.status_code == 400
            assert "does not serve listing" in response.get_json()["error"]
            response = client.post("/api/host/directory-picker/create-directory",
                                   json={"path": str(Path.cwd()), "name": "x"})
            assert response.status_code == 400
            host.directory_picker = dp.BrowseDirectoryPicker()
            host.directory_picker_kind = "browse"
        response = client.post("/api/host/directory-picker/pick")
        assert response.status_code == 400
        assert "does not serve picking" in response.get_json()["error"]

    def test_browse_routes_over_http(self, host, tmp_path: Path) -> None:
        host.directory_picker = dp.BrowseDirectoryPicker()
        host.directory_picker_kind = "browse"
        client = host.app.test_client()
        listing_root = tmp_path / "listing"
        (listing_root / "child").mkdir(parents=True)
        response = client.get("/api/host/directory-picker/list",
                              query_string={"path": str(listing_root)})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["path"] == str(listing_root)
        assert [e["name"] for e in payload["entries"]] == ["child"]
        # a relative path answers the typed 400 with code and path
        response = client.get("/api/host/directory-picker/list",
                              query_string={"path": "relative"})
        assert response.status_code == 400
        body = response.get_json()
        assert body["code"] == "directory-unreadable"
        assert body["path"] == "relative"
        # creation over HTTP
        response = client.post("/api/host/directory-picker/create-directory",
                               json={"path": str(listing_root), "name": "made"})
        assert response.status_code == 200
        assert response.get_json()["path"] == str(listing_root / "made")
        assert (listing_root / "made").is_dir()
        response = client.post("/api/host/directory-picker/create-directory",
                               json={"path": str(listing_root), "name": "made"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "directory-exists"

    def test_list_without_path_uses_home(self, host) -> None:
        home = os.path.expanduser("~")
        host.directory_picker = dp.BrowseDirectoryPicker()
        host.directory_picker_kind = "browse"
        client = host.app.test_client()
        response = client.get("/api/host/directory-picker/list")
        assert response.status_code == 200
        assert response.get_json()["path"] == os.path.abspath(home)
