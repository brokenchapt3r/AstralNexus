"""Contract tests for AstralNexus.runtime.native_command (dsh-native-command port).

Covers the shell-free runner (real spawns: capture, exit codes, ENOENT,
cancellation before/during, no-shell argv, large output), explorer_target
encoding (comma/equals escaping, non-ASCII decoded back, verbatim prefixes,
UNC), the LaunchServices plist parser, every open-intent dispatch branch
through the internals run seam (browser-first documents, Explorer delegated
exit 1, cancellation precedence, WSL translation, unsupported platforms),
XDG desktop-entry parsing and icon precedence, the GIO Linux catalog with a
real temporary XDG tree, metadata validation parsers, macOS dedupe/version
ordering, the aggregation layer (desktop_target, per-platform launch
validation), the PowerShell adapter script shape (base64 data, BOM, temp
cleanup), and — on this Windows host — the REAL C# COM adapter listing the
registered .txt handlers and rejecting an unregistered application id.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.runtime import native_command as nc


# ------------------------------------------------------------------- runner

class TestRunNativeCommand:
    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_captures_stdout_and_stderr(self) -> None:
        result = nc.run_native_command("cmd.exe", ["/c", "echo hello && echo oops 1>&2"])
        assert result["stdout"].strip() == "hello"
        assert result["stderr"].strip() == "oops"

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_nonzero_exit_carries_code_and_partial_capture(self) -> None:
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.run_native_command("cmd.exe", ["/c", "echo out && exit /b 3"])
        assert exc.value.code == 3
        assert exc.value.stdout.strip() == "out"
        assert exc.value.cancelled is False

    def test_missing_executable_is_enoent(self) -> None:
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.run_native_command("no-such-executable-xyz-42", [])
        assert exc.value.code == "ENOENT"

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_pre_cancelled_token_raises_before_wait(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.run_native_command("cmd.exe", ["/c", "echo x"], cancel)
        assert exc.value.code == "ABORT_ERR"
        assert exc.value.cancelled is True

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_cancel_during_run_terminates_the_child(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.6, cancel.set).start()
        start = time.monotonic()
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.run_native_command(
                "cmd.exe", ["/c", "echo started & ping -n 8 127.0.0.1 > nul"], cancel)
        elapsed = time.monotonic() - start
        assert exc.value.cancelled is True
        assert "started" in exc.value.stdout  # partial capture preserved
        assert elapsed < 5  # terminated, not waited out

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_large_output_does_not_deadlock(self) -> None:
        result = nc.run_native_command(
            "cmd.exe",
            ["/c", "for /L %i in (1,1,3000) do @echo line-%i-"
                   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
        assert result["stdout"].count("\n") == 3000

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_args_are_never_a_shell_string(self) -> None:
        # the ampersand arrives as literal argv data, not a command separator
        result = nc.run_native_command("cmd.exe", ["/c", "echo", "x & echo hacked"])
        assert "hacked" in result["stdout"]
        assert result["stdout"].count("\n") == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe host")
    def test_env_and_cwd_reach_the_child(self, tmp_path: Path) -> None:
        # `run_native_command` captures the child's bytes as UTF-8 -- that is the
        # module's documented contract ("utf8 capture"), so the runner is not
        # what fails here: cmd.exe's built-ins (`echo`, `cd`) emit the console/
        # OEM code page instead, and on a host whose temp directory contains
        # non-ASCII characters (…\pytest-of-断章\…) `cd` comes back as U+FFFD.
        # Measured, not assumed: `chcp 65001` first does not help either --
        # 02_运行产物/capability_migration/b33_contraction/probe_native_command_encoding.py
        # returns 'astral-probe-\ufffd\ufffd\ufffd\ufffd' for both variants.
        # Probe the cwd with an ASCII sentinel through `dir` instead: the name is
        # printed only if the child really resolved it relative to `tmp_path`.
        (tmp_path / "cwd-probe-ascii.txt").write_text("", encoding="utf-8")
        result = nc.run_native_command(
            "cmd.exe", ["/c", "echo %PROBE_MARK% & dir /b cwd-probe-ascii.txt"],
            env={**os.environ, "PROBE_MARK": "marked"}, cwd=str(tmp_path))
        assert "marked" in result["stdout"]
        assert "cwd-probe-ascii.txt" in result["stdout"]


# ----------------------------------------------------------- explorer_target

class TestExplorerTarget:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_commas_and_equals_are_escaped_spaces_percent20(self) -> None:
        target = nc.explorer_target(r"C:\a b\c,d=e.txt")
        assert target == "file:///C:/a%20b/c%2Cd%3De.txt"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_non_ascii_is_decoded_back_to_literal_characters(self) -> None:
        # Explorer rejects percent-encoded non-ASCII (opening Documents
        # instead) while resolving literal characters
        target = nc.explorer_target("C:\\temp\\中文.html")
        assert "中文" in target
        assert "%" not in target.split("file:///C:/temp/")[1]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_ascii_escapes_stand(self) -> None:
        assert "%20" in nc.explorer_target(r"C:\a b.txt")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_verbatim_drive_prefix_arrives_as_ordinary_uri(self) -> None:
        assert nc.explorer_target(r"\\?\C:\x.txt") == "file:///C:/x.txt"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_verbatim_unc_prefix_arrives_as_unc_uri(self) -> None:
        assert nc.explorer_target("\\\\?\\UNC\\server\\share\\x,y.txt") == \
            "file://server/share/x%2Cy.txt"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_unc_path(self) -> None:
        assert nc.explorer_target("\\\\server\\share\\x.txt") == \
            "file://server/share/x.txt"


# ------------------------------------------------------- mac https bundle

PLIST = """
{
LSHandlerRoleAll = com.old.browser;
LSHandlerURLScheme = http;
};
{
LSHandlerPreferredVersions = { LSHandlerRoleAll = "-1"; };
LSHandlerRoleAll = com.apple.Safari;
LSHandlerURLScheme = "https";
};
"""


class TestMacBundleForHttps:
    def test_finds_the_https_handler(self) -> None:
        assert nc.mac_bundle_for_https(PLIST) == "com.apple.Safari"

    def test_nested_version_dict_is_stripped_first(self) -> None:
        # the stripped block carries its own LSHandlerRoleAll = "-1"
        assert nc.mac_bundle_for_https(PLIST) != "-1"

    def test_no_https_block(self) -> None:
        assert nc.mac_bundle_for_https("LSHandlerURLScheme = http;") is None

    def test_block_without_role(self) -> None:
        assert nc.mac_bundle_for_https('{ LSHandlerURLScheme = https; }') is None


# --------------------------------------------------------- open dispatching

class RecordingRun:
    """A fake internals.run capturing every dispatch."""

    def __init__(self, results: Optional[Dict[str, Any]] = None,
                 error: Optional[nc.NativeCommandError] = None):
        self.calls: List[tuple] = []
        self.results = results or {}
        self.error = error

    def __call__(self, command: str, args: List[str], cancel: Any = None) -> Dict[str, str]:
        self.calls.append((command, tuple(args)))
        if self.error is not None and command == self.error_command:
            raise self.error
        return self.results.get(command, {"stdout": "", "stderr": ""})

    error_command = ""


class TestOpenNativePath:
    def windows(self, run):
        return {"platform": "win32", "run": run}

    def test_win32_hands_every_intent_to_explorer(self) -> None:
        run = RecordingRun()
        nc.open_native_path(r"C:\x.txt", None, self.windows(run))
        nc.open_native_text_file(r"C:\x.txt", None, self.windows(run))
        assert run.calls == [
            ("explorer.exe", ("file:///C:/x.txt",)),
            ("explorer.exe", ("file:///C:/x.txt",)),
        ]

    def test_explorer_delegate_exit_1_is_accepted(self) -> None:
        run = RecordingRun(error=nc.NativeCommandError("exit 1", code=1))
        run.error_command = "explorer.exe"
        nc.open_native_path(r"C:\x.txt", None, self.windows(run))  # no raise

    def test_other_explorer_failures_propagate(self) -> None:
        run = RecordingRun(error=nc.NativeCommandError("boom", code=2))
        run.error_command = "explorer.exe"
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.open_native_path(r"C:\x.txt", None, self.windows(run))
        assert exc.value.code == 2

    def test_cancellation_wins_over_delegate_exit_1(self) -> None:
        cancel = threading.Event()
        cancel.set()
        run = RecordingRun(error=nc.NativeCommandError("exit 1", code=1))
        run.error_command = "explorer.exe"
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.open_native_path(r"C:\x.txt", cancel, self.windows(run))
        assert exc.value.code == "ABORT_ERR"

    def test_darwin_default_and_text_editor_intents(self) -> None:
        run = RecordingRun()
        internals = {"platform": "darwin", "run": run}
        nc.open_native_path("/tmp/x.txt", None, internals)
        nc.open_native_text_file("/tmp/x.txt", None, internals)
        nc.open_native_associated_path("/tmp/x.txt", None, internals)
        assert run.calls == [("open", ("/tmp/x.txt",)), ("open", ("-t", "/tmp/x.txt")),
                             ("open", ("/tmp/x.txt",))]

    def test_darwin_browser_document_prefers_the_named_browser(self) -> None:
        run = RecordingRun(results={
            "defaults": {"stdout": PLIST, "stderr": ""}})
        nc.open_native_path("/tmp/x.html", None, {"platform": "darwin", "run": run})
        assert run.calls == [
            ("defaults", ("read", "com.apple.LaunchServices/com.apple.launchservices.secure")),
            ("open", ("-b", "com.apple.Safari", "/tmp/x.html")),
        ]

    def test_darwin_unnamed_browser_falls_back_to_open(self) -> None:
        run = RecordingRun(error=nc.NativeCommandError("no defaults", code=1))
        run.error_command = "defaults"
        nc.open_native_path("/tmp/x.svg", None, {"platform": "darwin", "run": run})
        assert run.calls[-1] == ("open", ("/tmp/x.svg",))

    def test_text_editor_intent_never_consults_the_browser(self) -> None:
        run = RecordingRun()
        nc.open_native_text_file("/tmp/x.html", None, {"platform": "darwin", "run": run})
        assert ("defaults", ("read",
                "com.apple.LaunchServices/com.apple.launchservices.secure")) not in run.calls
        assert run.calls == [("open", ("-t", "/tmp/x.html"))]

    def test_linux_browser_env_takes_browser_documents(self) -> None:
        run = RecordingRun()
        nc.open_native_path("/tmp/x.htm", None,
                            {"platform": "linux", "run": run,
                             "env": {"BROWSER": "firefox"}})
        assert run.calls == [("firefox", ("/tmp/x.htm",))]

    def test_linux_blank_browser_falls_back_to_xdg(self) -> None:
        run = RecordingRun()
        nc.open_native_path("/tmp/x.html", None,
                            {"platform": "linux", "run": run, "env": {"BROWSER": ""}})
        assert run.calls == [("xdg-open", ("/tmp/x.html",))]

    def test_linux_non_browser_document_uses_xdg(self) -> None:
        run = RecordingRun()
        nc.open_native_path("/tmp/x.pdf", None,
                            {"platform": "linux", "run": run, "env": {}})
        assert run.calls == [("xdg-open", ("/tmp/x.pdf",))]

    def test_wsl_translates_for_the_windows_desktop(self) -> None:
        run = RecordingRun(results={"wslpath": {"stdout": "C:\\mnt\\x.txt\r\n",
                                               "stderr": ""}})
        nc.open_native_path("/mnt/x.txt", None,
                            {"platform": "linux", "run": run,
                             "env": {"WSL_DISTRO_NAME": "Ubuntu"}})
        assert run.calls == [("wslpath", ("-w", "/mnt/x.txt")),
                             ("explorer.exe", ("file:///C:/mnt/x.txt",))]

    def test_wsl_empty_translation_is_an_error(self) -> None:
        run = RecordingRun(results={"wslpath": {"stdout": "\r\n", "stderr": ""}})
        with pytest.raises(RuntimeError, match="wslpath returned no Windows path"):
            nc.open_native_path("/mnt/x.txt", None,
                                {"platform": "linux", "run": run,
                                 "env": {"WSL_INTEROP": "/run/interop"}})

    def test_wsl_skips_the_browser_branch(self) -> None:
        run = RecordingRun(results={"wslpath": {"stdout": "C:\\x.html", "stderr": ""}})
        nc.open_native_path("/x.html", None,
                            {"platform": "linux", "run": run,
                             "env": {"WSL_DISTRO_NAME": "Ubuntu", "BROWSER": "firefox"}})
        assert run.calls[0][0] == "wslpath"

    def test_wsl_detected_via_kernel_release(self) -> None:
        run = RecordingRun(results={"wslpath": {"stdout": "C:\\x.txt", "stderr": ""}})
        nc.open_native_path("/x.txt", None,
                            {"platform": "linux", "run": run, "env": {},
                             "os_release": "5.15.153.1-microsoft-standard-WSL2"})
        assert run.calls[0][0] == "wslpath"

    def test_unsupported_platform(self) -> None:
        with pytest.raises(RuntimeError,
                           match="native path opener is unsupported on freebsd"):
            nc.open_native_path("/x.txt", None,
                                {"platform": "freebsd", "run": RecordingRun()})


class TestCanOpenAndManager:
    def test_can_open(self) -> None:
        assert nc.can_open_native_path({"platform": "darwin"}) is True
        assert nc.can_open_native_path({"platform": "win32"}) is True
        assert nc.can_open_native_path({"platform": "freebsd"}) is False
        assert nc.can_open_native_path({"platform": "linux", "env": {}}) is False
        assert nc.can_open_native_path({"platform": "linux",
                                        "env": {"DISPLAY": ":0"}}) is True
        assert nc.can_open_native_path({"platform": "linux",
                                        "env": {"WAYLAND_DISPLAY": "wayland-0"}}) is True
        assert nc.can_open_native_path({"platform": "linux",
                                        "env": {"WSL_DISTRO_NAME": "x"}}) is True

    def test_file_manager(self) -> None:
        assert nc.native_file_manager({"platform": "darwin"}) == "finder"
        assert nc.native_file_manager({"platform": "win32"}) == "explorer"
        assert nc.native_file_manager({"platform": "linux",
                                       "env": {"WSL_DISTRO_NAME": "x"}}) == "explorer"
        assert nc.native_file_manager({"platform": "linux", "env": {}}) == "directory"
        assert nc.native_file_manager({"platform": "freebsd"}) is None


class TestReveal:
    def test_finder(self) -> None:
        run = RecordingRun()
        nc.reveal_native_path("/tmp/x.txt", None, {"platform": "darwin", "run": run})
        assert run.calls == [("open", ("-R", "/tmp/x.txt"))]

    def test_explorer_select(self) -> None:
        run = RecordingRun()
        nc.reveal_native_path(r"C:\x,y.txt", None, {"platform": "win32", "run": run})
        assert run.calls == [("explorer.exe", ("/select,", "file:///C:/x%2Cy.txt"))]

    def test_wsl_select_translates(self) -> None:
        run = RecordingRun(results={"wslpath": {"stdout": "C:\\x.txt\n", "stderr": ""}})
        nc.reveal_native_path("/x.txt", None,
                              {"platform": "linux", "run": run,
                               "env": {"WSL_DISTRO_NAME": "u"}})
        assert run.calls == [("wslpath", ("-w", "/x.txt")),
                             ("explorer.exe", ("/select,", "file:///C:/x.txt"))]

    def test_linux_directory_fallback(self) -> None:
        run = RecordingRun()
        nc.reveal_native_path("/tmp/sub/x.txt", None,
                              {"platform": "linux", "run": run, "env": {}})
        assert run.calls == [("xdg-open", ("/tmp/sub",))]

    def test_unsupported(self) -> None:
        with pytest.raises(RuntimeError,
                           match="native file manager is unsupported on freebsd"):
            nc.reveal_native_path("/x", None,
                                  {"platform": "freebsd", "run": RecordingRun()})

    def test_precancelled(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.reveal_native_path("/x", cancel,
                                  {"platform": "darwin", "run": RecordingRun()})
        assert exc.value.cancelled is True


# ------------------------------------------------------------- desktop-entry

class TestDesktopEntry:
    def test_main_section_only_literal_fields(self) -> None:
        text = ("[Desktop Entry]\n"
                "# comment\n"
                "Name=App\n"
                "Name[zh_CN]=应用\n"
                "Exec=app %f\n"
                "[Desktop Action new]\n"
                "Name=NewWindow\n")
        fields = nc.desktop_entry_fields(text)
        assert fields == {"Name": "App", "Name[zh_CN]": "应用", "Exec": "app %f"}

    def test_later_key_wins(self) -> None:
        fields = nc.desktop_entry_fields("[Desktop Entry]\nA=1\nA=2\n")
        assert fields["A"] == "2"

    def test_data_directories_precedence(self) -> None:
        dirs = nc.desktop_data_directories(
            "/home/u", {"XDG_DATA_HOME": "/custom", "XDG_DATA_DIRS": "/a:/b"})
        assert dirs == ["/custom", "/a", "/b"]
        default = nc.desktop_data_directories("/home/u", {})
        assert default == [str(Path("/home/u") / ".local" / "share"),
                           "/usr/local/share", "/usr/share"]

    def test_read_icon_kinds(self, tmp_path: Path) -> None:
        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG")
        icon = nc.read_icon(str(png))
        assert icon is not None and icon["contentType"] == "image/png"
        assert icon["bytes"] == b"\x89PNG"
        svg = tmp_path / "a.svg"
        svg.write_text("<svg/>")
        svg_icon = nc.read_icon(str(svg))
        assert svg_icon is not None and svg_icon["contentType"] == "image/svg+xml"
        assert nc.read_icon(str(tmp_path / "a.ico")) is None
        assert nc.read_icon(str(tmp_path / "missing.png")) is None
        assert nc.read_icon(str(png / "sub.png")) is None  # under a file

    def test_icon_precedence_hicolor_then_scalable_then_pixmaps(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        small = root / "icons" / "hicolor" / "48x48" / "apps"
        small.mkdir(parents=True)
        (small / "app.png").write_bytes(b"48")
        scalable = root / "icons" / "hicolor" / "scalable" / "apps"
        scalable.mkdir(parents=True)
        (scalable / "app.svg").write_text("<svg/>")
        icon = nc.desktop_application_icon("app", [str(root)])
        # 512 missing → 256 missing → 128 → 64 → 48 hit (before scalable)
        assert icon is not None and icon["bytes"] == b"48"
        (small / "app.png").unlink()
        scalable_icon = nc.desktop_application_icon("app", [str(root)])
        assert scalable_icon is not None
        assert scalable_icon["contentType"] == "image/svg+xml"
        (scalable / "app.svg").unlink()
        pixmaps = root / "pixmaps"
        pixmaps.mkdir()
        (pixmaps / "app.png").write_bytes(b"pix")
        pixmap_icon = nc.desktop_application_icon("app", [str(root)])
        assert pixmap_icon is not None and pixmap_icon["bytes"] == b"pix"

    def test_absolute_icon_name(self, tmp_path: Path) -> None:
        absolute = tmp_path / "direct.png"
        absolute.write_bytes(b"d")
        icon = nc.desktop_application_icon(str(absolute), [])
        assert icon is not None and icon["bytes"] == b"d"


# ------------------------------------------------------------ linux via GIO

class TestLinuxFileApplications:
    def make_tree(self, tmp_path: Path) -> Dict[str, str]:
        """A real XDG data tree: data/applications/{a.desktop, nested/b.desktop}."""
        apps = tmp_path / "data" / "applications"
        apps.mkdir(parents=True)
        (apps / "a.desktop").write_text(
            "[Desktop Entry]\nName=App A\nName[zh_CN]=甲\nIcon=appicon\n",
            encoding="utf-8")
        (apps / "hidden.desktop").write_text(
            "[Desktop Entry]\nName=Hidden\nHidden=true\n", encoding="utf-8")
        nested = apps / "kde"
        nested.mkdir()
        (nested / "org.kde.b.desktop").write_text(
            "[Desktop Entry]\nName=App B\n", encoding="utf-8")
        icons = tmp_path / "data" / "icons" / "hicolor" / "48x48" / "apps"
        icons.mkdir(parents=True)
        (icons / "appicon.png").write_bytes(b"\x89PNG-data")
        return {"XDG_DATA_HOME": str(tmp_path / "data"), "HOME": str(tmp_path),
                "LC_ALL": "zh_CN.UTF-8"}

    def gio_run(self, tmp_path: Path, mime_out: str):
        def run(command: str, args: List[str], cancel: Any = None):
            if command == "gio" and args[0] == "info":
                return {"stdout": "attributes:\n  standard::content-type: text/plain\n",
                        "stderr": ""}
            if command == "env":
                return {"stdout": mime_out, "stderr": ""}
            raise AssertionError(f"unexpected {command} {args}")
        return run

    def test_catalog_with_preferred_first_locale_and_icons(self, tmp_path: Path) -> None:
        env = self.make_tree(tmp_path)
        mime_out = ("Default application for text/plain: a.desktop\n"
                    "Registered applications:\n"
                    "\tkde-org.kde.b.desktop\n"
                    "\ta.desktop\n"
                    "\thidden.desktop\n"
                    "\tmissing.desktop\n")
        apps = nc.linux_file_applications("/x/f.txt", None,
                                          self.gio_run(tmp_path, mime_out), env)
        # preferred first, ids deduped, hidden skipped, missing entry skipped
        assert [Path(a["id"]).name for a in apps] == ["a.desktop", "org.kde.b.desktop"]
        assert apps[0]["name"] == "甲"  # Name[zh_CN] wins under LC_ALL=zh_CN.UTF-8
        assert apps[0]["default"] is True
        assert apps[1]["default"] is False
        assert apps[0]["icon"] == "data:image/png;base64," + \
            base64.b64encode(b"\x89PNG-data").decode("ascii")
        assert apps[1]["icon"] is None

    def test_locale_falls_back_to_language_then_plain(self, tmp_path: Path) -> None:
        env = self.make_tree(tmp_path)
        env["LC_ALL"] = "de_DE.UTF-8"  # no Name[de_DE], no Name[de]
        mime_out = "Default application for text/plain: a.desktop\n"
        apps = nc.linux_file_applications("/x/f.txt", None,
                                          self.gio_run(tmp_path, mime_out), env)
        assert apps[0]["name"] == "App A"

    def test_missing_content_type_is_an_error(self, tmp_path: Path) -> None:
        def run(command, args, cancel=None):
            return {"stdout": "attributes:\n  standard::display-name: f.txt\n",
                    "stderr": ""}
        with pytest.raises(RuntimeError,
                           match="GIO did not identify the file content type"):
            nc.linux_file_applications("/x/f.txt", None, run, {})

    def test_cancel_between_entries(self, tmp_path: Path) -> None:
        env = self.make_tree(tmp_path)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.linux_file_applications("/x/f.txt", cancel,
                                       self.gio_run(tmp_path, "Default application for text/plain: a.desktop\n"), env)
        assert exc.value.cancelled is True

    def test_desktop_file_nested_id(self, tmp_path: Path) -> None:
        root = tmp_path / "apps"
        (root / "kde").mkdir(parents=True)
        (root / "kde" / "x.desktop").write_text("[Desktop Entry]\n")
        found = nc.desktop_file(str(root), "kde-x.desktop")
        assert found is not None and Path(found).name == "x.desktop"
        assert nc.desktop_file(str(root), "nope.desktop") is None


# -------------------------------------------------------------- validation

class TestParsers:
    GOOD = {"id": "a", "name": "A", "default": True, "icon": None}
    GOOD_ICON = {"id": "b", "name": "B", "default": False,
                 "icon": "data:image/png;base64,aGVsbG8="}

    def test_valid_list(self) -> None:
        assert nc.parse_native_file_applications([self.GOOD, self.GOOD_ICON]) == \
            [self.GOOD, self.GOOD_ICON]

    def test_not_a_list(self) -> None:
        with pytest.raises(ValueError, match="Invalid native application list"):
            nc.parse_native_file_applications({"id": "a"})

    @pytest.mark.parametrize("entry", [
        None, 42, {"id": "a"}, {"id": "", "name": "x", "default": True, "icon": None},
        {"id": "a", "name": 3, "default": True, "icon": None},
        {"id": "a", "name": "x", "default": "yes", "icon": None},
        {"id": "a", "name": "x", "default": True, "icon": "http://x/y.png"},
        {"id": "a", "name": "x", "default": True, "icon": "data:image/gif;base64,QQ=="},
        {"id": "a", "name": "x", "default": True},  # icon key missing
    ])
    def test_invalid_entries(self, entry) -> None:
        with pytest.raises(ValueError, match="Invalid native application entry"):
            nc.parse_native_file_applications([entry])

    def test_extra_fields_are_dropped(self) -> None:
        parsed = nc.parse_native_file_applications(
            [{**self.GOOD, "extra": 1}])
        assert parsed == [self.GOOD]

    def test_mac_fields(self) -> None:
        entry = {**self.GOOD, "bundle": "com.x", "version": ""}
        parsed = nc.parse_mac_applications([entry])[0]
        assert parsed["bundle"] == "com.x"
        assert parsed["version"] is None  # empty reads as null
        with pytest.raises(ValueError, match="Invalid native application entry"):
            nc.parse_mac_applications([{**self.GOOD, "bundle": 7}])
        absent = nc.parse_mac_applications([self.GOOD])[0]
        assert absent["bundle"] is None and absent["version"] is None

    def test_compare_versions(self) -> None:
        assert nc.compare_versions("1.2.3", "1.2.10") < 0
        assert nc.compare_versions("1.0", "1.0.0") == 0
        assert nc.compare_versions("1.x", "1.0") == 0  # non-numeric counts as 0
        assert nc.compare_versions(None, "1.0") < 0
        assert nc.compare_versions(None, None) == 0
        assert nc.compare_versions("2", None) > 0


class TestMacDedupe:
    def app(self, **kw):
        base = {"id": "/a", "name": "App", "default": False, "icon": None,
                "bundle": "com.app", "version": "1.0"}
        return {**base, **kw}

    def test_collapse_default_wins(self) -> None:
        apps = [self.app(id="/old", version="2.0"),
                self.app(id="/sys", default=True, version="1.0")]
        result = nc.dedupe_mac_applications(apps)
        assert len(result) == 1 and result[0]["id"] == "/sys"

    def test_collapse_highest_version_wins_at_first_position(self) -> None:
        apps = [self.app(id="/v1", version="1.0"), self.app(id="/v3", version="3.0"),
                self.app(id="/v2", version="2.0")]
        result = nc.dedupe_mac_applications(apps)
        assert result == [{"id": "/v3", "name": "App", "default": False, "icon": None}]

    def test_distinct_display_names_stay_split(self) -> None:
        apps = [self.app(id="/a", name="App"), self.app(id="/b", name="App Beta")]
        assert len(nc.dedupe_mac_applications(apps)) == 2

    def test_bundleless_entries_stay(self) -> None:
        apps = [self.app(id="/x", bundle=None), self.app(id="/y", bundle=None)]
        assert len(nc.dedupe_mac_applications(apps)) == 2

    def test_grouping_metadata_is_stripped(self) -> None:
        result = nc.dedupe_mac_applications([self.app()])
        assert set(result[0]) == {"id", "name", "default", "icon"}


# ------------------------------------------------------------- aggregation

MAC_OUT = json.dumps([
    {"id": "/old.app", "name": "App", "default": False, "icon": None,
     "bundle": "com.app", "version": "1.0"},
    {"id": "/new.app", "name": "App", "default": True, "icon": None,
     "bundle": "com.app", "version": "2.0"},
])


class TestAggregation:
    def test_desktop_target_plain(self) -> None:
        assert nc.desktop_target("/x", None, {"platform": "linux", "env": {}}) == \
            {"platform": "linux", "path": "/x"}

    def test_desktop_target_wsl(self) -> None:
        run = RecordingRun(results={"wslpath": {"stdout": "C:\\x\r\n", "stderr": ""}})
        assert nc.desktop_target("/x", None, {"platform": "linux", "run": run,
                                              "env": {"WSL_DISTRO_NAME": "u"}}) == \
            {"platform": "win32", "path": "C:\\x"}

    def test_darwin_display_collapses_and_asks_for_icons(self) -> None:
        run = RecordingRun(results={"osascript": {"stdout": MAC_OUT, "stderr": ""}})
        apps = nc.native_file_applications("/f.txt", None,
                                           {"platform": "darwin", "run": run})
        assert apps == [{"id": "/new.app", "name": "App", "default": True,
                         "icon": None}]
        assert run.calls[0][1][-1] == "icons"

    def test_unsupported_platform_is_empty(self) -> None:
        assert nc.native_file_applications("/f", None,
                                           {"platform": "freebsd",
                                            "run": RecordingRun()}) == []

    def test_darwin_launch_validates_the_complete_list(self) -> None:
        # the display list collapsed /old.app away, yet it stays openable
        run = RecordingRun(results={"osascript": {"stdout": MAC_OUT, "stderr": ""}})
        nc.open_native_file_application("/f.txt", "/old.app", None,
                                        {"platform": "darwin", "run": run})
        assert run.calls[-1] == ("open", ("-a", "/old.app", "/f.txt"))
        assert run.calls[-2][1][-1] == "handlers"  # validation skips icon rendering

    def test_darwin_launch_rejects_unregistered(self) -> None:
        run = RecordingRun(results={"osascript": {"stdout": MAC_OUT, "stderr": ""}})
        with pytest.raises(RuntimeError,
                           match="Application is not registered for this file"):
            nc.open_native_file_application("/f.txt", "/evil.app", None,
                                            {"platform": "darwin", "run": run})

    def test_linux_launch_uses_gio(self) -> None:
        calls: List[tuple] = []

        def run(command, args, cancel=None):
            calls.append((command, tuple(args)))
            if command == "gio" and args[0] == "info":
                return {"stdout": "standard::content-type: text/plain\n", "stderr": ""}
            if command == "env":
                return {"stdout": "Default application for text/plain: a.desktop\n",
                        "stderr": ""}
            return {"stdout": "", "stderr": ""}

        env = {"XDG_DATA_HOME": "/nonexistent-root", "HOME": "/nonexistent-home"}
        # with no desktop files found the registered list is empty → reject
        with pytest.raises(RuntimeError, match="not registered"):
            nc.open_native_file_application("/f.txt", "a.desktop", None,
                                            {"platform": "linux", "run": run,
                                             "env": env})

    def test_precancel(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.native_file_applications("/f", cancel,
                                        {"platform": "darwin", "run": RecordingRun()})
        assert exc.value.cancelled is True


# ------------------------------------------------------- PowerShell adapter

class TestWindowsAdapterScript:
    def test_script_shape_base64_data_bom_and_cleanup(self) -> None:
        seen: Dict[str, Any] = {}

        def run(command, args, cancel=None):
            seen["command"] = command
            seen["args"] = list(args)
            script_path = args[-1]
            seen["exists_during"] = os.path.isfile(script_path)
            with open(script_path, "rb") as handle:
                raw = handle.read()
            seen["bom"] = raw.startswith(b"\xef\xbb\xbf")
            seen["text"] = raw.decode("utf-8-sig")
            seen["directory"] = os.path.dirname(script_path)
            return {"stdout": "[]", "stderr": ""}

        out = nc.windows_file_applications("C:\\x,y\\文.txt", None, None, run)
        assert out == "[]"
        assert seen["command"] == "powershell.exe"
        assert seen["args"][:6] == ["-NoProfile", "-NonInteractive", "-STA",
                                    "-ExecutionPolicy", "Bypass", "-File"]
        assert seen["bom"] is True
        text = seen["text"]
        encoded = base64.b64encode("C:\\x,y\\文.txt".encode("utf-8")).decode("ascii")
        assert encoded in text  # the path travels as base64 data
        assert "C:\\x,y" not in text.replace(encoded, "")  # never a literal expression
        assert "[DshFileAssociations]::List($path)" in text
        assert seen["exists_during"] is True
        assert not os.path.exists(seen["directory"])  # temp removed once settled

    def test_open_mode_script(self) -> None:
        captured: Dict[str, str] = {}

        def run(command, args, cancel=None):
            with open(args[-1], encoding="utf-8-sig") as handle:
                captured["text"] = handle.read()
            return {"stdout": "", "stderr": ""}

        nc.windows_file_applications("C:\\x.txt", "C:\\app.exe", None, run)
        assert "[DshFileAssociations]::Open($path, $application)" in captured["text"]
        encoded = base64.b64encode(b"C:\\app.exe").decode("ascii")
        assert encoded in captured["text"]

    def test_temp_directory_removed_even_on_failure(self) -> None:
        state: Dict[str, str] = {}

        def run(command, args, cancel=None):
            state["directory"] = os.path.dirname(args[-1])
            raise nc.NativeCommandError("boom", code=1)

        with pytest.raises(nc.NativeCommandError):
            nc.windows_file_applications("C:\\x.txt", None, None, run)
        assert not os.path.exists(state["directory"])


# ----------------------------------------------------- real Windows host

@pytest.mark.skipif(sys.platform != "win32", reason="real Shell host")
class TestRealWindowsHost:
    def test_real_adapter_lists_registered_handlers(self, tmp_path: Path) -> None:
        target = tmp_path / "probe-file.txt"
        target.write_text("x", encoding="utf-8")
        apps = nc.native_file_applications(str(target))
        assert isinstance(apps, list)
        assert len(apps) >= 1  # .txt always has handlers on a desktop Windows
        for app in apps:
            assert set(app) == {"id", "name", "default", "icon"}
            assert app["name"] != ""
            assert app["icon"] is None or app["icon"].startswith(
                ("data:image/png;base64,", "data:image/svg+xml;base64,"))

    def test_real_adapter_rejects_unregistered_application(self, tmp_path: Path) -> None:
        target = tmp_path / "probe-file.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(nc.NativeCommandError) as exc:
            nc.open_native_file_application(
                str(target), "C:\\definitely\\not-registered-xyz.exe")
        assert exc.value.code == 1
        assert "not registered" in (exc.value.stderr + exc.value.stdout).lower()

    def test_real_explorer_open_is_not_attempted_here(self) -> None:
        # opening would pop real desktop windows; the dispatch is covered by
        # the run-seam tests and explorer_target by its encoding tests
        assert nc.can_open_native_path() is True
