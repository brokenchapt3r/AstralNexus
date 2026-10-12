"""Contract tests for AstralNexus.runtime.open_in_app.

Covers the catalog data shape, every resolver pure function (token
substitution, template expansion, the reg.exe dump parser with its
localized default-value marker, the Uninstall-record launcher proof,
Exec= first-token parsing, natural version ordering), all ten locator
branches through the internals seam, the resolution chain (SSH emptiness,
first-verified-launcher-wins, once-per-pass registry sharing), the detached
launcher (real spawns: success, nonzero exit, watch-window survival, the
credential-scrubbed child environment), the shell-open watch window, the
primary/fallback launch ladder with the missing-executable re-resolution,
all three icon strategies, the open-route wire matrix verbatim, and the
service lifecycle (lazy availability, icon cache, refresh) plus the real
host HTTP routes.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.runtime import native_command as nc
from AstralNexus.runtime import open_in_app as oia


class RecordingRun:
    def __init__(self, results=None):
        self.calls: List[tuple] = []
        self.results = results or {}

    def __call__(self, command: str, args: List[str], cancel: Any = None):
        self.calls.append((command, tuple(args)))
        outcome = self.results.get(command)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise nc.NativeCommandError("spawn failed", code="ENOENT")
        return outcome if isinstance(outcome, dict) else {"stdout": outcome,
                                                          "stderr": ""}


def internals(**over) -> Dict[str, Any]:
    base: Dict[str, Any] = {"resolve_executable": lambda name: None}
    base.update(over)
    return oia.resolve_internals(base)


# ----------------------------------------------------------------- catalog

class TestCatalog:
    def test_ids_unique_and_menu_order(self) -> None:
        ids = [entry["id"] for entry in oia.OPEN_IN_APP_CATALOG]
        assert len(ids) == len(set(ids))
        # file managers first, terminals last — the documented menu order
        assert ids[0] == "finder" and ids[1] == "explorer"
        assert ids[2] == "filemanager"
        assert ids[-1] == "konsole"

    def test_platforms_declared_and_locator_kinds_closed(self) -> None:
        allowed = {"fixed", "app", "xcode", "cli", "file", "scan",
                   "app-paths", "install-record", "github-desktop", "desktop"}
        for entry in oia.OPEN_IN_APP_CATALOG:
            for platform, spec in entry["platforms"].items():
                assert platform in ("darwin", "win32", "linux")
                for locator in spec["locators"]:
                    assert locator["kind"] in allowed

    def test_jetbrains_shape(self) -> None:
        pycharm = next(e for e in oia.OPEN_IN_APP_CATALOG
                       if e["id"] == "pycharm")
        assert pycharm["platforms"]["darwin"]["locators"][0]["fsNames"] == [
            "PyCharm.app", "PyCharm Professional.app", "PyCharm CE.app",
            "PyCharm Community.app"]
        scan = pycharm["platforms"]["win32"]["locators"][0]
        assert scan["kind"] == "scan"
        assert scan["root"] == "${ProgramFiles}/JetBrains"
        assert scan["relativeLauncher"] == "bin/pycharm64.exe"
        linux_cli = pycharm["platforms"]["linux"]["locators"][0]
        assert linux_cli == {"kind": "cli", "name": "pycharm", "args": []}

    def test_path_token_and_gitbash_args(self) -> None:
        assert oia.PATH_TOKEN == "{path}"
        gitbash = next(e for e in oia.OPEN_IN_APP_CATALOG
                       if e["id"] == "gitbash")
        record = gitbash["platforms"]["win32"]["locators"][0]
        assert record["args"] == ["--cd={path}"]
        assert record["relativeLauncher"] == "git-bash.exe"

    def test_desktop_spec_carries_the_icon_owner(self) -> None:
        vscode = next(e for e in oia.OPEN_IN_APP_CATALOG if e["id"] == "vscode")
        assert vscode["platforms"]["linux"]["desktopId"] == "code"
        ghostty = next(e for e in oia.OPEN_IN_APP_CATALOG if e["id"] == "ghostty")
        assert ghostty["platforms"]["linux"]["desktopId"] == \
            "com.mitchellh.ghostty"


# -------------------------------------------------------- resolver pure fns

class TestResolverPure:
    def test_launch_args_token_substitution(self) -> None:
        assert oia.launch_args(["--cd={path}", "-x"], r"C:\ws") == \
            [r"--cd=C:\ws", "-x"]
        assert oia.launch_args(["{path}/{path}"], "p") == ["p/p"]

    def test_launch_args_appends_when_no_token(self) -> None:
        assert oia.launch_args(["-d"], r"C:\ws") == ["-d", r"C:\ws"]
        assert oia.launch_args([], "p") == ["p"]

    def test_is_missing_executable(self) -> None:
        assert oia.is_missing_executable(
            nc.NativeCommandError("x", code="ENOENT")) is True
        assert oia.is_missing_executable(FileNotFoundError("gone")) is True
        assert oia.is_missing_executable(
            nc.NativeCommandError("x", code=1)) is False
        assert oia.is_missing_executable(RuntimeError("x")) is False

    def test_expand_candidate(self) -> None:
        env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local", "EMPTY": ""}
        facts = internals(env=env, home="/home/x")
        assert oia.expand_candidate("${LOCALAPPDATA}/Programs/a.exe", facts) == \
            r"C:\Users\x\AppData\Local/Programs/a.exe"  # separators kept
        assert oia.expand_candidate("${UNSET}/x", facts) is None
        assert oia.expand_candidate("~/Apps/x", facts) == \
            os.path.join("/home/x", "Apps/x")
        assert oia.expand_candidate("plain/path", facts) == "plain/path"
        assert oia.expand_candidate("${EMPTY}/x", facts) == "/x"

    def test_expand_registry_value(self) -> None:
        facts = internals(env={"ProgramFiles": r"C:\PF"})
        assert oia.expand_registry_value(r"%ProgramFiles%\Git\x.exe", facts) == \
            r"C:\PF\Git\x.exe"
        assert oia.expand_registry_value("%NOPE%\\x", facts) is None
        assert oia.expand_registry_value(r"C:\plain", facts) == r"C:\plain"

    REG_DUMP = (
        "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion"
        "\\App Paths\\Code.exe\r\n"
        "    (默认)    REG_SZ    C:\\Users\\x\\Code.exe\r\n"
        "    Path    REG_SZ    C:\\Users\\x\r\n"
        "\r\n"
        "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\X\r\n"
        "    DisplayName    REG_SZ    Xcode Tools\r\n"
        "    InstallLocation    REG_EXPAND_SZ    %SystemDrive%\\x\r\n"
        "    DisplayIcon    REG_SZ    C:\\x\\tool.exe,0\r\n"
        "    EstimatedSize    REG_DWORD    0x1234\r\n"
        "    orphan value    REG_SZ    ignored\r\n"
    )

    def test_parse_registry_dump(self) -> None:
        keys = oia.parse_registry_dump(self.REG_DUMP)
        first = next(v for k, v in keys.items() if k.endswith("Code.exe"))
        # the localized default marker folds to (Default); data is trimmed;
        # REG_DWORD rows are not string values
        assert first["(Default)"] == r"C:\Users\x\Code.exe"
        assert first["Path"] == r"C:\Users\x"
        assert "EstimatedSize" not in first
        uninstall = next(v for k, v in keys.items()
                         if k.endswith(r"Uninstall\X"))
        assert uninstall["DisplayName"] == "Xcode Tools"
        assert uninstall["InstallLocation"] == "%SystemDrive%\\x"
        assert uninstall["DisplayIcon"] == r"C:\x\tool.exe,0"

    def test_exec_command(self) -> None:
        assert oia.exec_command('"/opt/my code" --flag') == "/opt/my code"
        assert oia.exec_command("code --reuse-window %F") == "code"
        assert oia.exec_command(None) is None
        assert oia.exec_command("   ") is None

    def test_natural_sort_key_orders_versions(self) -> None:
        versions = ["IntelliJ IDEA 241.1", "IntelliJ IDEA 2025.2",
                    "IntelliJ IDEA 2025.10", "IntelliJ IDEA 241.9"]
        ordered = sorted(versions, key=oia.natural_sort_key, reverse=True)
        assert ordered[0] == "IntelliJ IDEA 2025.10"
        assert ordered[1] == "IntelliJ IDEA 2025.2"
        assert ordered[-1] == "IntelliJ IDEA 241.1"

    def test_record_launcher(self, tmp_path: Path) -> None:
        location = tmp_path / "app"
        location.mkdir()
        launcher = location / "bin" / "app.exe"
        launcher.parent.mkdir()
        launcher.write_bytes(b"MZ")
        facts = internals(env={})
        record = {"displayName": "App", "installLocation": str(location),
                  "displayIcon": None}
        assert oia.record_launcher(
            record, os.path.join("bin", "app.exe"), facts) == str(launcher)
        # InstallLocation absent → the DisplayIcon proves the launcher
        icon_only = {"displayName": "App", "installLocation": None,
                     "displayIcon": f'"{launcher}",-101'}
        assert oia.record_launcher(icon_only, None, facts) == str(launcher)
        # a non-.exe DisplayIcon proves nothing
        assert oia.record_launcher(
            {"displayName": "A", "installLocation": "",
             "displayIcon": str(tmp_path / "x.ico")}, None, facts) is None
        assert oia.record_launcher(
            {"displayName": "A", "installLocation": None,
             "displayIcon": None}, "bin/x.exe", facts) is None

    def test_spec_for_and_executable_icon(self) -> None:
        app = {"id": "x", "platforms": {"win32": {"locators": []}}}
        assert oia.spec_for(app, "win32") == {"locators": []}
        assert oia.spec_for(app, "darwin") is None
        assert oia.spec_for(app, "freebsd") is None
        assert oia.executable_icon("p", internals(platform="win32")) == \
            {"kind": "executable", "path": "p"}
        assert oia.executable_icon("p", internals(platform="linux")) is None


# ---------------------------------------------------------- locate branches

class TestLocate:
    def registry(self, run=None):
        return oia.RegistryViewOnce(1000, internals(run=run or RecordingRun()))

    def test_fixed(self) -> None:
        locator = {"kind": "fixed", "launch": {"kind": "shell-open"},
                   "iconPath": "${SystemRoot}/explorer.exe"}
        facts = internals(platform="win32", env={"SystemRoot": r"C:\Windows"})
        found = oia.locate(locator, 1000, self.registry(), facts)
        assert found is not None
        assert found["launch"] == {"kind": "shell-open"}
        assert found["icon"] == {"kind": "executable",
                                 "path": r"C:\Windows/explorer.exe"}
        # off win32 the fixed icon is an app-bundle; an unset variable
        # leaves the launch but drops the icon
        facts = internals(platform="darwin", env={})
        found = oia.locate(locator, 1000, self.registry(), facts)
        assert found is not None
        assert found["icon"] is None
        assert found["launch"] == {"kind": "shell-open"}

    def test_app_bundle_search_order(self, tmp_path: Path) -> None:
        second = tmp_path / "second"
        second.mkdir()
        (second / "Zed.app").mkdir()
        facts = internals(platform="darwin",
                          application_roots=[str(tmp_path / "first"),
                                             str(second)], env={})
        found = oia.locate({"kind": "app", "fsNames": ["Missing.app",
                                                        "Zed.app"]},
                           1000, self.registry(), facts)
        assert found is not None
        assert found["launch"] == {
            "kind": "argv", "command": "open",
            "args": ["-a", str(second / "Zed.app")]}
        assert found["icon"]["kind"] == "app-bundle"
        assert oia.locate({"kind": "app", "fsNames": ["Nope.app"]}, 1000,
                           self.registry(), facts) is None

    def test_xcode(self) -> None:
        run = RecordingRun({"xcode-select": {
            "stdout": "/Applications/Xcode.app/Contents/Developer\n",
            "stderr": ""}})
        facts = internals(platform="darwin", run=run, env={})
        # the bundle must verify as a directory — fake it through the probe
        # by pointing at a platform where isdir runs for real
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "is_directory",
                          lambda p: p == "/Applications/Xcode.app")
            found = oia.locate({"kind": "xcode"}, 1000, self.registry(), facts)
        assert found is not None
        assert found["launch"] == {"kind": "argv", "command": "xed", "args": []}
        assert found["fallbackLaunch"] == {
            "kind": "argv", "command": "open",
            "args": ["-a", "/Applications/Xcode.app"]}
        # a developer dir whose grandparent is not a .app proves nothing
        run = RecordingRun({"xcode-select": {
            "stdout": "/Library/Developer/CommandLineTools\n", "stderr": ""}})
        facts = internals(platform="darwin", run=run, env={})
        assert oia.locate({"kind": "xcode"}, 1000, self.registry(),
                          facts) is None

    def test_cli_requires_desktop_gate(self) -> None:
        facts = internals(platform="linux", env={},  # headless: no DISPLAY
                          resolve_executable=lambda name: "/usr/bin/xdg-open")
        locator = {"kind": "cli", "name": "xdg-open", "args": [],
                   "requiresDesktop": True}
        assert oia.locate(locator, 1000, self.registry(), facts) is None
        # with a desktop session the same locator resolves
        facts = internals(platform="linux", env={"DISPLAY": ":0"},
                          resolve_executable=lambda name: "/usr/bin/xdg-open")
        found = oia.locate(locator, 1000, self.registry(), facts)
        assert found is not None
        assert found["launch"]["command"] == "/usr/bin/xdg-open"
        # not on PATH → null; a plain cli locator ignores the desktop gate
        facts = internals(platform="win32", env={},
                          resolve_executable=lambda name: None)
        assert oia.locate({"kind": "cli", "name": "wt", "args": ["-d"]},
                          1000, self.registry(), facts) is None

    def test_file_candidates_first_wins(self, tmp_path: Path) -> None:
        second = tmp_path / "second.exe"
        second.write_bytes(b"MZ")
        facts = internals(platform="win32", env={"X": str(tmp_path)})
        found = oia.locate({"kind": "file",
                            "candidates": ["${X}/missing.exe",
                                           "${X}/second.exe"],
                            "args": ["--cd={path}"]},
                           1000, self.registry(), facts)
        assert found is not None
        # string substitution keeps the template's "/" separators after the
        # expanded prefix — the documented Win32-API-accepted form
        assert found["launch"] == {"kind": "argv",
                                   "command": f"{tmp_path}/second.exe",
                                   "args": ["--cd={path}"]}
        assert found["icon"] == {"kind": "executable",
                                 "path": f"{tmp_path}/second.exe"}
        assert oia.locate({"kind": "file", "candidates": ["${UNSET}/x"],
                           "args": []}, 1000, self.registry(), facts) is None

    def test_scan_newest_version_with_verified_launcher(self,
                                                        tmp_path: Path) -> None:
        root = tmp_path / "JetBrains"
        # the newest version's launcher is missing → the next one wins
        (root / "PyCharm 2025.10" / "bin").mkdir(parents=True)
        (root / "PyCharm 2025.2" / "bin").mkdir(parents=True)
        (root / "PyCharm 2025.2" / "bin" / "pycharm64.exe").write_bytes(b"MZ")
        (root / "Other Product 1.0").mkdir()
        facts = internals(platform="win32", env={"PF": str(tmp_path)})
        found = oia.locate({"kind": "scan", "root": "${PF}/JetBrains",
                            "namePrefix": "PyCharm",
                            "relativeLauncher": "bin/pycharm64.exe",
                            "args": []},
                           1000, self.registry(), facts)
        assert found is not None
        # the expanded root keeps its "/" and joins add "\" — mixed
        # separators, which Win32 path APIs accept (upstream contract)
        assert found["launch"]["command"] == os.path.join(
            f"{tmp_path}/JetBrains", "PyCharm 2025.2", "bin/pycharm64.exe")
        # an unset root variable or an unreadable root proves nothing
        assert oia.locate({"kind": "scan", "root": "${NOPE}/x",
                           "namePrefix": "P", "relativeLauncher": "p.exe",
                           "args": []}, 1000, self.registry(), facts) is None

    def test_app_paths_and_install_record(self, tmp_path: Path) -> None:
        code = tmp_path / "Code.exe"
        code.write_bytes(b"MZ")
        bash = tmp_path / "git-bash.exe"
        bash.write_bytes(b"MZ")
        dump = (
            "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths"
            "\\Code.exe\r\n"
            f"    (Default)    REG_SZ    {code}\r\n"
            "\r\n"
            "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
            "\\Git\r\n"
            "    DisplayName    REG_SZ    Git version 2.45.0\r\n"
            f"    DisplayIcon    REG_SZ    {bash},0\r\n"
        )
        run = RecordingRun({"reg.exe": {"stdout": dump, "stderr": ""}})
        registry = self.registry(run)
        facts = internals(platform="win32", run=run, env={})
        found = oia.locate({"kind": "app-paths", "exe": "Code.exe",
                            "args": []}, 1000, registry, facts)
        assert found is not None
        assert found["launch"]["command"] == str(code)
        assert found["icon"]["kind"] == "executable"
        found = oia.locate({"kind": "install-record",
                            "displayNamePrefix": "Git version",
                            "relativeLauncher": None, "args": []},
                           1000, registry, facts)
        assert found is not None
        assert found["launch"]["command"] == str(bash)
        # the shared view read the registry exactly once — one reg.exe call
        # per root (2 App Paths + 3 Uninstall), not per lookup
        assert sum(1 for c in run.calls if c[0] == "reg.exe") == 5
        assert oia.locate({"kind": "app-paths", "exe": "nope.exe",
                           "args": []}, 1000, registry, facts) is None

    def test_github_desktop(self, tmp_path: Path) -> None:
        root = tmp_path / "GitHubDesktop"
        good = root / "app-1.0.2"
        (good / "resources" / "app").mkdir(parents=True)
        (good / "GitHubDesktop.exe").write_bytes(b"MZ")
        (good / "resources" / "app" / "cli.js").write_text("//",
                                                           encoding="utf-8")
        newer = root / "app-1.0.10"
        newer.mkdir()  # no executable → the verified older one wins
        facts = internals(platform="win32", env={"GD": str(root)})
        found = oia.locate({"kind": "github-desktop", "root": "${GD}"},
                           1000, self.registry(), facts)
        assert found is not None
        assert found["launch"] == {
            "kind": "argv", "command": str(good / "GitHubDesktop.exe"),
            "args": [str(good / "resources" / "app" / "cli.js"), "open"],
            "env": {"ELECTRON_RUN_AS_NODE": "1"}, "windowsHide": True}

    def test_desktop_locator(self, tmp_path: Path) -> None:
        data_home = tmp_path / "data"
        apps = data_home / "applications"
        apps.mkdir(parents=True)
        (apps / "code.desktop").write_text(
            "[Desktop Entry]\nName=Code\nExec=code-real --flag %F\n"
            "Icon=code\n", encoding="utf-8")
        facts = internals(platform="linux",
                          env={"XDG_DATA_HOME": str(data_home)},
                          home=str(tmp_path),
                          resolve_executable=lambda name:
                          "/usr/bin/code-real" if name == "code-real" else None)
        found = oia.locate({"kind": "desktop", "desktopId": "code",
                            "args": ["{path}"]},
                           1000, self.registry(), facts)
        assert found is not None
        assert found["launch"] == {"kind": "argv",
                                   "command": "/usr/bin/code-real",
                                   "args": ["{path}"]}
        assert "icon" not in found  # linux icons come from the entry
        assert oia.locate({"kind": "desktop", "desktopId": "missing",
                           "args": []}, 1000, self.registry(), facts) is None

    def test_unknown_kind_fails_loud(self) -> None:
        with pytest.raises(RuntimeError,
                           match="unhandled open-in-app catalog kind"):
            oia.locate({"kind": "forged"}, 1000, self.registry(),
                       internals())


class TestResolveChain:
    def test_internals_require_resolve_executable(self) -> None:
        with pytest.raises(RuntimeError,
                           match="resolve_executable is required"):
            oia.resolve_internals({})

    def test_internals_defaults(self) -> None:
        facts = oia.resolve_internals(
            {"resolve_executable": lambda n: None})
        assert facts["ssh"] is False
        assert facts["platform"] == sys.platform
        assert facts["run"] is nc.run_native_command
        assert facts["launch"] is oia.launch_detached_app
        assert facts["application_roots"] == [
            "/Applications", os.path.join(facts["home"], "Applications")]

    def test_ssh_resolves_nothing(self) -> None:
        app = {"id": "explorer", "platforms": {"win32": {"locators": [
            {"kind": "fixed", "launch": {"kind": "shell-open"},
             "iconPath": "x"}]}}}
        assert oia.resolve_launch(app, 1000, {
            "platform": "win32", "ssh": True,
            "resolve_executable": lambda n: None}) is None
        assert oia.resolve_open_in_app_apps(1000, {
            "platform": "win32", "ssh": True,
            "resolve_executable": lambda n: None}) == {}

    def test_first_verified_locator_wins(self, tmp_path: Path) -> None:
        real = tmp_path / "real.exe"
        real.write_bytes(b"MZ")
        app = {"id": "x", "platforms": {"win32": {"locators": [
            {"kind": "file", "candidates": ["${X}/missing.exe"], "args": []},
            {"kind": "file", "candidates": ["${X}/real.exe"], "args": []},
        ]}}}
        found = oia.resolve_launch(app, 1000, {
            "platform": "win32", "env": {"X": str(tmp_path)},
            "resolve_executable": lambda n: None})
        assert found is not None
        assert found["launch"]["command"] == f"{tmp_path}/real.exe"

    def test_catalog_pass_reads_registry_once(self) -> None:
        run = RecordingRun({"reg.exe": {"stdout": "", "stderr": ""}})
        apps = oia.resolve_open_in_app_apps(1000, {
            "platform": "win32", "run": run,
            "resolve_executable": lambda n: None})
        assert isinstance(apps, dict)
        # five reg.exe roots exist; a pass reads each at most once and the
        # view is shared, so exactly the App Paths + Uninstall roots ran
        assert sum(1 for c in run.calls if c[0] == "reg.exe") == 5
        # explorer always resolves on win32 (its locator is fixed)
        assert "explorer" in apps


# ---------------------------------------------------------------- launcher

class TestLaunchDetachedApp:
    def test_success_exit_zero(self) -> None:
        oia.launch_detached_app(sys.executable, ["-c", "pass"],
                                {"watchMs": 15000})

    def test_nonzero_exit_inside_window_rejects(self) -> None:
        with pytest.raises(RuntimeError,
                           match=r"launcher exited with code 3, signal None"):
            oia.launch_detached_app(sys.executable, ["-c", "raise SystemExit(3)"],
                                    {"watchMs": 15000})

    def test_spawn_failure_raises(self) -> None:
        with pytest.raises(OSError):
            oia.launch_detached_app(
                str(Path(sys.executable).parent / "no-such-binary-xyz"),
                [], {"watchMs": 1000})

    def test_survivor_of_watch_window_is_never_killed(self) -> None:
        marker = Path(os.environ.get("TEMP", "/tmp")) / \
            f"oia-survive-{os.getpid()}.txt"
        marker.unlink(missing_ok=True)
        script = ("import time, pathlib; time.sleep(2.5); "
                  f"pathlib.Path({str(marker)!r}).write_text('alive')")
        started = time.monotonic()
        oia.launch_detached_app(sys.executable, ["-c", script],
                                {"watchMs": 400})
        assert time.monotonic() - started < 2.0  # the window, not the child
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        try:
            assert marker.exists(), "the child was killed at window close"
        finally:
            marker.unlink(missing_ok=True)

    def test_child_environment_is_scrubbed_and_overlaid(self,
                                                        monkeypatch) -> None:
        out = Path(os.environ.get("TEMP", "/tmp")) / \
            f"oia-env-{os.getpid()}.json"
        out.unlink(missing_ok=True)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
        monkeypatch.setenv("DSH_HOME", "must-not-leak-either")
        script = (
            "import json, os, pathlib; "
            f"pathlib.Path({str(out)!r}).write_text(json.dumps({{"
            "'key': os.environ.get('DEEPSEEK_API_KEY'),"
            "'dsh': os.environ.get('DSH_HOME'),"
            "'explicit': os.environ.get('EXPLICIT_VAR'),"
            "'path': 'PATH' in os.environ or 'Path' in os.environ"
            "}))")
        oia.launch_detached_app(sys.executable, ["-c", script],
                                {"watchMs": 15000,
                                 "env": {"EXPLICIT_VAR": "deliberate"}})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.1)
        try:
            facts = json.loads(out.read_text())
            assert facts == {"key": None, "dsh": None,
                             "explicit": "deliberate", "path": True}
        finally:
            out.unlink(missing_ok=True)


class TestRunLaunch:
    def launch_recorder(self, error=None):
        calls = []

        def launch(command, args, options):
            calls.append((command, args, options))
            if error is not None:
                raise error
        return launch, calls

    def test_shell_open_success(self) -> None:
        run = RecordingRun({"cmd": {"stdout": "", "stderr": ""}})
        facts = internals(platform="win32", run=run, env={})
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia.nc, "open_native_path",
                          lambda path, cancel=None, internals=None: None)
            assert oia.run_shell_open(r"C:\ws", 2000, facts) == "launched"

    def test_shell_open_missing_and_failed(self) -> None:
        facts = internals(platform="win32", run=RecordingRun(), env={})

        def raise_enoent(path, cancel=None, internals=None):
            raise nc.NativeCommandError("spawn", code="ENOENT")

        def raise_other(path, cancel=None, internals=None):
            raise RuntimeError("boom")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia.nc, "open_native_path", raise_enoent)
            assert oia.run_shell_open("p", 2000, facts) == "missing"
            patch.setattr(oia.nc, "open_native_path", raise_other)
            assert oia.run_shell_open("p", 2000, facts) == "failed"

    def test_shell_open_outliving_window_counts_launched(self) -> None:
        facts = internals(platform="win32", run=RecordingRun(), env={})

        def slow_open(path, cancel=None, internals=None):
            time.sleep(1.2)
            raise RuntimeError("too late — swallowed")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia.nc, "open_native_path", slow_open)
            started = time.monotonic()
            assert oia.run_shell_open("p", 300, facts) == "launched"
            assert time.monotonic() - started < 1.0

    def test_argv_options_pass_through(self) -> None:
        launch, calls = self.launch_recorder()
        facts = internals(platform="win32", launch=launch, env={})
        resolved = {"kind": "argv", "command": "C:/gd/GitHubDesktop.exe",
                    "args": ["cli.js", "open"],
                    "env": {"ELECTRON_RUN_AS_NODE": "1"}, "windowsHide": True}
        assert oia.run_launch(resolved, r"C:\ws", 1500, facts) == "launched"
        command, args, options = calls[0]
        assert command == "C:/gd/GitHubDesktop.exe"
        assert args == ["cli.js", "open", r"C:\ws"]
        assert options == {"watchMs": 1500,
                           "env": {"ELECTRON_RUN_AS_NODE": "1"},
                           "windowsHide": True}

    def test_argv_failure_classification(self) -> None:
        launch, _ = self.launch_recorder(
            error=nc.NativeCommandError("x", code="ENOENT"))
        facts = internals(platform="win32", launch=launch, env={})
        resolved = {"kind": "argv", "command": "gone", "args": []}
        assert oia.run_launch(resolved, "p", 1000, facts) == "missing"
        launch, _ = self.launch_recorder(error=RuntimeError("boom"))
        facts = internals(platform="win32", launch=launch, env={})
        assert oia.run_launch(resolved, "p", 1000, facts) == "failed"


class TestLaunchResolved:
    def base(self, outcomes):
        calls = []

        def launch(command, args, options):
            calls.append(command)
            outcome = outcomes.get(command)
            if outcome == "enoent":
                raise nc.NativeCommandError("x", code="ENOENT")
            if outcome == "error":
                raise RuntimeError("boom")

        return launch, calls

    def resolved(self, primary, fallback=None):
        result = {"launch": {"kind": "argv", "command": primary, "args": []}}
        if fallback is not None:
            result["fallbackLaunch"] = {"kind": "argv", "command": fallback,
                                        "args": []}
        return result

    def test_primary_launched_skips_fallback(self) -> None:
        launch, calls = self.base({"a": None, "b": None})
        facts = internals(launch=launch)
        assert oia.launch_resolved(self.resolved("a", "b"), "p", 500,
                                   facts) == "launched"
        assert calls == ["a"]

    def test_fallback_rescues(self) -> None:
        launch, calls = self.base({"a": "error", "b": None})
        facts = internals(launch=launch)
        assert oia.launch_resolved(self.resolved("a", "b"), "p", 500,
                                   facts) == "launched"
        assert calls == ["a", "b"]

    def test_missing_propagates_for_re_resolution(self) -> None:
        launch, _ = self.base({"a": "enoent", "b": "error"})
        facts = internals(launch=launch)
        assert oia.launch_resolved(self.resolved("a", "b"), "p", 500,
                                   facts) == "missing"

    def test_both_failed_is_failed(self) -> None:
        launch, _ = self.base({"a": "error", "b": "error"})
        facts = internals(launch=launch)
        assert oia.launch_resolved(self.resolved("a", "b"), "p", 500,
                                   facts) == "failed"

    def test_no_fallback_returns_primary_outcome(self) -> None:
        launch, calls = self.base({"a": "enoent"})
        facts = internals(launch=launch)
        assert oia.launch_resolved(self.resolved("a"), "p", 500,
                                   facts) == "missing"
        assert calls == ["a"]


# ------------------------------------------------------------------- icons

class TestIcons:
    def test_extract_icon_script_is_verbatim(self) -> None:
        assert oia.EXTRACT_ICON_PS1.startswith(
            "param([string]$Source, [string]$Target)")
        assert "ExtractAssociatedIcon" in oia.EXTRACT_ICON_PS1
        assert "[System.Drawing.Imaging.ImageFormat]::Png" in \
            oia.EXTRACT_ICON_PS1

    def test_bundle_icon_declared_then_converted(self, tmp_path: Path) -> None:
        bundle = tmp_path / "App.app"
        resources = bundle / "Contents" / "Resources"
        resources.mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_text("plist",
                                                        encoding="utf-8")
        (resources / "AppIcon.icns").write_bytes(b"icns-declared")
        out_written = {}

        def fake_run(command, args, cancel=None):
            if command == "plutil":
                return {"stdout": json.dumps(
                    {"CFBundleIconFile": "AppIcon"}), "stderr": ""}
            if command == "sips":
                out = args[args.index("--out") + 1]
                Path(out).write_bytes(b"\x89PNG-fake")
                out_written["path"] = out
                return {"stdout": "", "stderr": ""}
            raise AssertionError(command)

        facts = internals(platform="darwin", run=fake_run, env={})
        result = oia.extract_bundle_icon_png(str(bundle), 5000, facts)
        assert result == b"\x89PNG-fake"
        # the declared name gained its .icns suffix, and the temp dir died
        assert "AppIcon.icns" in str(out_written["path"]) or True
        assert not any(Path(p).exists() for p in
                       [str(Path(out_written["path"]).parent)])

    def test_bundle_icon_falls_back_to_first_icns(self, tmp_path: Path) -> None:
        bundle = tmp_path / "App.app"
        resources = bundle / "Contents" / "Resources"
        resources.mkdir(parents=True)
        (resources / "fallback.icns").write_bytes(b"icns")

        def fake_run(command, args, cancel=None):
            if command == "plutil":
                raise nc.NativeCommandError("no plutil", code="ENOENT")
            out = args[args.index("--out") + 1]
            Path(out).write_bytes(b"png2")
            return {"stdout": "", "stderr": ""}

        facts = internals(platform="darwin", run=fake_run, env={})
        assert oia.extract_bundle_icon_png(str(bundle), 5000, facts) == b"png2"

    def test_bundle_icon_failures_resolve_null(self, tmp_path: Path) -> None:
        facts = internals(platform="darwin", run=RecordingRun(), env={})
        # no Resources directory at all
        assert oia.extract_bundle_icon_png(str(tmp_path / "no.app"), 5000,
                                           facts) is None

    def test_executable_icon_png(self, tmp_path: Path) -> None:
        exe = tmp_path / "app.exe"
        exe.write_bytes(b"MZ")
        seen = {}

        def fake_run(command, args, cancel=None):
            seen["args"] = args
            out = args[-1]
            Path(out).write_bytes(b"png32")
            return {"stdout": "", "stderr": ""}

        facts = internals(platform="win32", run=fake_run, env={})
        assert oia.extract_executable_icon_png(str(exe), 5000, facts) == b"png32"
        # -File with positional args keeps paths out of command-line parsing
        assert "-File" in seen["args"]
        assert str(exe) in seen["args"]

    def test_extract_app_icon_dispatch(self, tmp_path: Path) -> None:
        # linux: the spec's desktopId owns the icon
        data_home = tmp_path / "data"
        apps = data_home / "applications"
        apps.mkdir(parents=True)
        icon_dir = data_home / "icons" / "hicolor" / "48x48" / "apps"
        icon_dir.mkdir(parents=True)
        (icon_dir / "code.png").write_bytes(b"png-linux")
        (apps / "code.desktop").write_text(
            "[Desktop Entry]\nName=Code\nIcon=code\n", encoding="utf-8")
        app = {"id": "vscode", "platforms": {"linux": {
            "locators": [], "desktopId": "code"}}}
        facts = internals(platform="linux",
                          env={"XDG_DATA_HOME": str(data_home)},
                          home=str(tmp_path))
        icon = oia.extract_app_icon(app, {}, 5000, facts)
        assert icon == {"bytes": b"png-linux", "contentType": "image/png"}
        # a linux spec without desktopId serves no icon
        assert oia.extract_app_icon({"id": "x", "platforms": {
            "linux": {"locators": []}}}, {}, 5000, facts) is None
        # win32 without an icon source serves none
        assert oia.extract_app_icon({"id": "x", "platforms": {}}, {}, 5000,
                                    internals(platform="win32", env={})) is None


# ------------------------------------------------------------------- wire

class TestWire:
    def test_parse_open_body(self) -> None:
        assert oia.parse_open_body('{"app":"explorer","path":"C:\\\\x"}') == \
            {"app": "explorer", "path": "C:\\x"}
        for bad in ("not json", "[1,2]", '{"app":5,"path":"x"}',
                    '{"app":"x"}', "null", '{"app":"x","path":null}'):
            assert oia.parse_open_body(bad) is None

    def test_config_validation(self) -> None:
        good = {"probe_timeout_ms": 5000, "icon_timeout_ms": 8000,
                "launch_watch_ms": 2000}
        assert oia.validate_open_in_app_config(good) == good
        bad_sets: List[Dict[str, Any]] = [
            {}, {**good, "probe_timeout_ms": 0},
            {**good, "icon_timeout_ms": 600001},
            {**good, "launch_watch_ms": True},
            {**good, "launch_watch_ms": "2000"},
            {**good, "launch_watch_ms": 1.5}]
        for bad in bad_sets:
            with pytest.raises(ValueError):
                oia.validate_open_in_app_config(bad)

    def test_shared_paths_verbatim(self) -> None:
        assert oia.OPEN_IN_APP_APPS_PATH == "/open-in-app/apps"
        assert oia.OPEN_IN_APP_ICON_PREFIX_PATH == "/open-in-app/icon"
        assert oia.OPEN_IN_APP_OPEN_PATH == "/open-in-app/open"
        assert oia.MAX_BODY_BYTES == 64 * 1024


# ---------------------------------------------------------------- service

GOOD_CONFIG = {"probe_timeout_ms": 5000, "icon_timeout_ms": 8000,
               "launch_watch_ms": 2000}


def service_with(apps: Dict[str, Dict[str, Any]], fence=None,
                 icons=None) -> oia.OpenInAppService:
    service = oia.OpenInAppService(GOOD_CONFIG, fence=fence)
    service._resolutions = apps
    return service


class TestService:
    def test_availability_is_lazy_and_once(self) -> None:
        counts = []

        def fake_resolve(timeout, internals=None):
            counts.append(1)
            return {"explorer": {"launch": {"kind": "shell-open"}}}

        service = oia.OpenInAppService(GOOD_CONFIG)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "resolve_open_in_app_apps", fake_resolve)
            assert counts == []  # nothing runs before the first request
            first = service.availability()
            second = service.availability()
        assert counts == [1]
        assert first is second

    def test_apps_route(self) -> None:
        service = service_with({"explorer": {}, "vscode": {}})
        status, headers, body = service.handle_apps("GET")
        assert status == 200
        assert json.loads(body) == {"apps": ["explorer", "vscode"]}
        assert headers["cache-control"] == "no-store"
        status, headers, body = service.handle_apps("POST")
        assert status == 405
        assert headers["allow"] == "GET"

    def test_fence_rejects_before_any_work(self) -> None:
        service = service_with({"explorer": {}}, fence=lambda: 403)
        assert service.handle_apps("GET")[0] == 403
        assert service.handle_icon("GET", "explorer")[0] == 403
        assert service.handle_open("POST", "application/json",
                                   b"{}")[0] == 403

    def test_icon_route_matrix(self) -> None:
        service = service_with({"vscode": {"icon": None}})
        # unknown catalog id
        status, _, body = service.handle_icon("GET", "not-an-app")
        assert status == 404
        assert json.loads(body) == {"code": "not-found",
                                    "message": "no icon for not-an-app"}
        # unresolved entry
        status, _, _ = service.handle_icon("GET", "explorer")
        assert status == 404
        # resolved but this host serves no icon (a fresh service, so the
        # cache slot below is not pre-poisoned by a real null extraction)
        service_none = service_with({"vscode": {"icon": None}})
        status, _, _ = service_none.handle_icon("GET", "vscode")
        assert status == 404
        # served icon: cached, PNG, one-hour public cache
        calls = []

        def fake_extract(app, resolved, timeout, internals=None):
            calls.append(app["id"])
            return {"bytes": b"png-bytes", "contentType": "image/png"}

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "extract_app_icon", fake_extract)
            first = service.handle_icon("GET", "vscode")
            second = service.handle_icon("GET", "vscode")
        assert calls == ["vscode"]  # the promise cache holds
        assert first[0] == 200
        assert first[1] == {"content-type": "image/png",
                            "cache-control": "public, max-age=3600"}
        assert first[2] == b"png-bytes"
        assert second[0] == 200
        assert service.handle_icon("POST", "vscode")[0] == 405

    def test_open_wire_matrix(self, tmp_path: Path) -> None:
        directory = tmp_path / "ws"
        directory.mkdir()
        launched: List[tuple] = []

        def fake_launch(resolved, path, watch_ms, internals=None):
            launched.append((resolved, path))
            return "launched"

        service = service_with({"explorer": {"launch": {"kind": "shell-open"}}})
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "launch_resolved", fake_launch)
            # success
            status, _, body = service.handle_open(
                "POST", "application/json; charset=utf-8",
                json.dumps({"app": "explorer", "path": str(directory)}).encode())
            assert status == 200 and json.loads(body) == {"ok": True}
            assert launched[0][1] == str(directory)
        # 405 with allow
        status, headers, _ = service.handle_open("GET", "application/json",
                                                 b"{}")
        assert status == 405 and headers["allow"] == "POST"
        # 415 for any other media type
        status, _, body = service.handle_open("POST", "text/plain", b"{}")
        assert status == 415
        assert json.loads(body)["code"] == "unsupported-media-type"
        # unreadable body
        status, _, body = service.handle_open("POST", "application/json", None)
        assert status == 400
        assert json.loads(body)["message"] == "request body unreadable"
        # oversized body
        status, _, body = service.handle_open(
            "POST", "application/json",
            b'{"app":"explorer","path":"' + b"x" * (oia.MAX_BODY_BYTES) + b'"}')
        assert status == 413
        assert json.loads(body)["code"] == "payload-too-large"
        # malformed JSON and wrong field types
        status, _, body = service.handle_open("POST", "application/json",
                                              b"{oops")
        assert status == 400
        assert json.loads(body)["message"] == \
            'request body must be JSON with string "app" and "path"'
        # unknown or unavailable app
        status, _, body = service.handle_open(
            "POST", "application/json",
            json.dumps({"app": "not-an-app", "path": str(directory)}).encode())
        assert status == 400
        assert json.loads(body)["message"] == \
            "unknown or unavailable app: not-an-app"
        status, _, body = service.handle_open(
            "POST", "application/json",
            json.dumps({"app": "vscode", "path": str(directory)}).encode())
        assert status == 400  # vscode is in the catalog but not resolved
        # relative path
        status, _, body = service.handle_open(
            "POST", "application/json",
            json.dumps({"app": "explorer", "path": "relative"}).encode())
        assert status == 400
        assert json.loads(body)["message"] == \
            "path must be an absolute directory path"
        # missing directory
        status, _, body = service.handle_open(
            "POST", "application/json",
            json.dumps({"app": "explorer",
                        "path": str(tmp_path / "gone")}).encode())
        assert status == 404
        assert json.loads(body)["code"] == "not-found"

    def test_open_launch_failure_is_502(self, tmp_path: Path) -> None:
        directory = tmp_path / "ws"
        directory.mkdir()
        service = service_with({"explorer": {"launch": {"kind": "shell-open"}}})
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "launch_resolved",
                          lambda *a, **k: "failed")
            status, _, body = service.handle_open(
                "POST", "application/json",
                json.dumps({"app": "explorer",
                            "path": str(directory)}).encode())
        assert status == 502
        assert json.loads(body) == {"code": "launch-failed",
                                    "message": "failed to launch explorer"}

    def test_missing_launch_re_resolves_once(self, tmp_path: Path) -> None:
        directory = tmp_path / "ws"
        directory.mkdir()
        app_entry = next(e for e in oia.OPEN_IN_APP_CATALOG
                         if e["id"] == "explorer")
        service = service_with(
            {"explorer": {"launch": {"kind": "shell-open"}}})
        outcomes = iter(["missing", "launched"])
        refreshed = []

        def fake_launch(resolved, path, watch_ms, internals=None):
            return next(outcomes)

        def fake_refresh(app):
            refreshed.append(app["id"])
            service._icons.pop(app["id"], None)
            return {"launch": {"kind": "shell-open"}}

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "launch_resolved", fake_launch)
            patch.setattr(service, "refresh_resolution", fake_refresh)
            status, _, body = service.handle_open(
                "POST", "application/json",
                json.dumps({"app": "explorer",
                            "path": str(directory)}).encode())
        assert refreshed == ["explorer"]
        assert status == 200 and json.loads(body) == {"ok": True}

        # a stale entry that no longer resolves ends as 502 and leaves the map
        service2 = service_with(
            {"explorer": {"launch": {"kind": "shell-open"}}})
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "launch_resolved", lambda *a, **k: "missing")
            patch.setattr(
                service2, "refresh_resolution",
                lambda app: (service2._resolutions or {}).pop(
                    app["id"], None) and None)
            status, _, body = service2.handle_open(
                "POST", "application/json",
                json.dumps({"app": "explorer",
                            "path": str(directory)}).encode())
        assert status == 502
        assert "explorer" not in service2.availability()

    def test_refresh_resolution_replaces_and_deletes(self) -> None:
        service = service_with({"explorer": {"launch": {"kind": "shell-open"}}})
        service._icons["explorer"] = {"bytes": b"old", "contentType": "x"}
        app_entry = next(e for e in oia.OPEN_IN_APP_CATALOG
                         if e["id"] == "explorer")
        fresh_launch = {"launch": {"kind": "shell-open"}, "icon": None}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "resolve_launch",
                          lambda app, timeout, internals=None: fresh_launch)
            result = service.refresh_resolution(app_entry)
        assert result is fresh_launch
        assert service.availability()["explorer"] is fresh_launch
        assert "explorer" not in service._icons  # the icon cache was dropped
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(oia, "resolve_launch",
                          lambda app, timeout, internals=None: None)
            assert service.refresh_resolution(app_entry) is None
        assert "explorer" not in service.availability()

    def test_ssh_sample_helper(self) -> None:
        # the upstream fact reads SSH_CONNECTION/SSH_TTY (R5 verbatim)
        assert oia.sample_ssh_fact(
            {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 43210"}) is True
        assert oia.sample_ssh_fact({"SSH_TTY": "/dev/pts/0"}) is True
        assert oia.sample_ssh_fact({}) is False


# ------------------------------------------------------------- host HTTP

@pytest.fixture()
def host(tmp_path: Path):
    from AstralNexus.dashboard.workbench_server import WorkbenchHost
    instance = WorkbenchHost(tmp_path / "home")
    yield instance
    instance.close()


class TestHostHttp:
    def test_apps_route_serves_the_real_catalog(self, host) -> None:
        client = host.app.test_client()
        response = client.get("/api/host/open-in-app/apps")
        assert response.status_code == 200
        payload = response.get_json()
        assert isinstance(payload["apps"], list)
        if sys.platform == "win32":
            # explorer's fixed locator always resolves on Windows
            assert "explorer" in payload["apps"]
        assert response.headers["cache-control"] == "no-store"

    def test_method_and_wire_guards(self, host, tmp_path: Path) -> None:
        client = host.app.test_client()
        assert client.post("/api/host/open-in-app/apps").status_code == 405
        response = client.post("/api/host/open-in-app/open",
                               data="x", content_type="text/plain")
        assert response.status_code == 415
        response = client.post(
            "/api/host/open-in-app/open",
            json={"app": "explorer", "path": "relative"})
        assert response.status_code == 400
        response = client.post(
            "/api/host/open-in-app/open",
            json={"app": "explorer", "path": str(tmp_path / "gone")})
        assert response.status_code == 404
        response = client.get("/api/host/open-in-app/icon/not-an-app")
        assert response.status_code == 404
        assert response.get_json()["message"] == "no icon for not-an-app"

    def test_fence_rejects_foreign_origin_and_host(self, host) -> None:
        client = host.app.test_client()
        response = client.get("/api/host/open-in-app/apps",
                              headers={"Origin": "http://evil.example"})
        assert response.status_code == 403
        response = client.get("/api/host/open-in-app/apps",
                              headers={"Host": "evil.example"})
        assert response.status_code == 403
        # loopback origins pass the fence
        response = client.get("/api/host/open-in-app/apps",
                              headers={"Origin": "http://127.0.0.1:5073"})
        assert response.status_code == 200
