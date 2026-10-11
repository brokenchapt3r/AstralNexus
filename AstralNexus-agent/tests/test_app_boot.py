"""Contract tests for AstralNexus.runtime.app_boot (dsh-app-boot remaining
face: .env loading, fail-loud guards, snapshot-aware config resolution, and
the startup audit).

parse_env is pinned against the real Electron-host Node v24.17.0
util.parseEnv probe (28 cases, evidence in the wave probe directory); the
layered loading asserts the inherited > project > home precedence, the
bootstrap-only refusal with its remedy text, and the home-layer proxy
exemption; install_fail_loud asserts the latch, the release timeout, the
assembled exemption window, and the diagnostic shape; the audit face is
driven through a fake PluginHost inventory.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import pytest

from AstralNexus.runtime import app_boot
from AstralNexus.runtime.compat import ExceptionGroup


# ------------------------------------------------------------- parse_env

class TestParseEnv:
    """Every case below was probed against real Node v24.17.0 parseEnv."""

    @pytest.mark.parametrize("text,expected", [
        ("A=1\nB=two\n", {"A": "1", "B": "two"}),
        ("# c\n\nA=1\n", {"A": "1"}),
        ('A="spaced"\nB=\'sq\'\nC="unbalanced\n',
         {"A": "spaced", "B": "sq", "C": '"unbalanced'}),
        ("export A=1\n", {"A": "1"}),
        ("A=1\nA=2\n", {"A": "2"}),  # later wins, no error
        ("  A  =  1  \n", {"A": "1"}),
        ("A=1=2\n", {"A": "1=2"}),  # first = splits
        ("A=\n", {"A": ""}),
        ("A=1", {"A": "1"}),  # no trailing newline
        ("A=1\r\nB=2\r\n", {"A": "1", "B": "2"}),
        ("A=1 # note\n", {"A": "1"}),  # inline comment stripped
        ('A="1 # note"\n', {"A": "1 # note"}),  # quoted keeps it
        ("A=$B\n", {"A": "$B"}),  # never expanded
        ("my.app-x_1=v\n", {"my.app-x_1": "v"}),
        ('A="line1\nline2"\n', {"A": "line1\nline2"}),  # multiline quoted
        ("\ufeffA=1\n", {"\ufeffA": "1"}),  # BOM joins the key
        ("A=1#x\n", {"A": "1"}),  # tight hash still cuts
        ("A=#x\n", {"A": ""}),
        ('A="q"#tail\n', {"A": "q"}),  # rest of line after close ignored
        ("A='v # c'\n", {"A": "v # c"}),
        ("A= # c\n", {"A": ""}),
        ("A\n", {}),  # no assignment ignored
        ("=1\n", {}),  # empty key ignored
        ('A=C:\\path\\x\n', {"A": "C:\\path\\x"}),  # backslashes literal
        ('A="it\'s"\n', {"A": "it's"}),
        ("A\t=\t1\n", {"A": "1"}),
        ("A = = \n", {"A": "="}),
        ('A="say \\"hi\\"\n', {"A": "say \\"}),  # no backslash escapes
    ])
    def test_probed_semantics(self, text: str, expected: Dict[str, str]) -> None:
        assert app_boot.parse_env(text) == expected

    def test_empty_text(self) -> None:
        assert app_boot.parse_env("") == {}

    def test_comment_line_with_leading_space(self) -> None:
        assert app_boot.parse_env("   # indented comment\nA=1\n") == {"A": "1"}

    def test_export_tab_prefix(self) -> None:
        assert app_boot.parse_env("export\tA=1\n") == {"A": "1"}


# ------------------------------------------------------ resolveConfigPath

class TestResolveConfigPath:
    def test_replay_swaps_basename(self, tmp_path: Path) -> None:
        assert app_boot.resolve_config_path(
            "cordis.yml", "replay", str(tmp_path)) == \
            str(tmp_path / "cordis.snapshot.yml")
        assert app_boot.resolve_config_path(
            "cordis.yaml", "replay", str(tmp_path)) == \
            str(tmp_path / "cordis.snapshot.yml")

    def test_other_modes_keep_the_path(self, tmp_path: Path) -> None:
        for mode in ("live", None, "record", ""):
            assert app_boot.resolve_config_path(
                "cordis.yml", mode, str(tmp_path)) == str(tmp_path / "cordis.yml")

    def test_non_matching_basename_kept_in_replay(self, tmp_path: Path) -> None:
        assert app_boot.resolve_config_path(
            "other.yml", "replay", str(tmp_path)) == str(tmp_path / "other.yml")

    def test_absolute_path_and_default_cwd(self, tmp_path: Path) -> None:
        absolute = str(tmp_path / "deep" / "cordis.yml")
        assert app_boot.resolve_config_path(absolute, "replay") == \
            str(tmp_path / "deep" / "cordis.snapshot.yml")
        relative = app_boot.resolve_config_path("cordis.yml", None)
        assert os.path.isabs(relative)


# ------------------------------------------------------- bootstrap policy

class TestBootstrapPolicy:
    def test_exact_names_are_case_insensitive(self) -> None:
        for name in ("PATH", "path", "Path", "NODE_OPTIONS", "GIT_SSH_COMMAND",
                     "NODE_TLS_REJECT_UNAUTHORIZED", "DEEPSEEK_BASE_URL"):
            assert app_boot.is_bootstrap_only(name) is True

    def test_prefixes(self) -> None:
        for name in ("DSH_HOME", "dsh_x", "XDG_DATA_HOME", "DYLD_INSERT_LIBRARIES",
                     "BASH_FUNC_foo"):
            assert app_boot.is_bootstrap_only(name) is True

    def test_ordinary_names_pass(self) -> None:
        for name in ("MY_VAR", "DEEPSEEK_API_KEY", "PATHISH", "XDG", "EDITORX"):
            assert app_boot.is_bootstrap_only(name) is False

    def test_sets_are_verbatim(self) -> None:
        assert len(app_boot.BOOTSTRAP_NAMES) == 49
        assert app_boot.BOOTSTRAP_PREFIXES == ("DSH_", "XDG_", "DYLD_",
                                               "BASH_FUNC_")
        assert app_boot.HOME_LAYER_PROXY_NAMES == {
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


class TestReadEnvLayer:
    def test_parses_without_applying(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("APP_VAR=layer-value\n", encoding="utf-8")
        layer = app_boot.read_env_layer("bin", str(tmp_path),
                                        home=str(tmp_path / "home"))
        assert layer == {"path": str(tmp_path / ".env"),
                         "values": {"APP_VAR": "layer-value"}}
        assert "APP_VAR" not in os.environ  # parsing alone changes nothing

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert app_boot.read_env_layer("bin", str(tmp_path),
                                       home=str(tmp_path)) is None

    def test_unreadable_file_warns(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        real_open = open

        def broken_open(file, *args, **kwargs):
            if str(file).endswith(".env"):
                raise PermissionError("denied")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", broken_open)
        warnings: List[str] = []
        assert app_boot.read_env_layer("bin", str(tmp_path), warnings.append,
                                       home=str(tmp_path)) is None
        assert warnings == ["bin: failed to load .env: denied\n"]

    def test_bootstrap_name_rejects_with_remedy(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("MY_VAR=ok\nPATH=/evil\n",
                                       encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        with pytest.raises(RuntimeError) as info:
            app_boot.read_env_layer("bin", str(tmp_path), home=str(home))
        message = str(info.value)
        assert '.env sets "PATH"' in message
        assert "only the launching environment may set" in message
        # a non-proxy name's remedy is the export form only (the home-file
        # alternative belongs to the proxy branch, asserted separately)
        assert "export PATH instead of putting it in a .env file" in message

    def test_proxy_names_rejected_in_project_layer(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("HTTP_PROXY=http://clone-proxy\n",
                                       encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        with pytest.raises(RuntimeError) as info:
            app_boot.read_env_layer("bin", str(tmp_path), home=str(home))
        message = str(info.value)
        assert "export HTTP_PROXY" in message
        assert f"put it in {home / '.env'}" in message  # proxy remedy differs

    def test_proxy_names_exempt_in_home_layer(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".env").write_text(
            "HTTP_PROXY=http://user-proxy\nNO_PROXY=local\n", encoding="utf-8")
        layer = app_boot.read_env_layer("bin", str(home), home=str(home))
        assert layer is not None
        assert layer["values"] == {"HTTP_PROXY": "http://user-proxy",
                                   "NO_PROXY": "local"}

    def test_ca_names_stay_refused_in_home_layer(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".env").write_text("SSL_CERT_FILE=/evil.pem\n",
                                   encoding="utf-8")
        with pytest.raises(RuntimeError, match="SSL_CERT_FILE"):
            app_boot.read_env_layer("bin", str(home), home=str(home))

    def test_dsh_home_cannot_be_relocated(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".env").write_text("DSH_HOME=/elsewhere\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="DSH_HOME"):
            app_boot.read_env_layer("bin", str(home), home=str(home))


class TestLoadEnv:
    def test_applies_without_replacing_existing(self, tmp_path: Path,
                                                monkeypatch) -> None:
        (tmp_path / ".env").write_text(
            "OIA_PRE_EXISTING=from-file\nOIA_NEW_VAR=from-file\n",
            encoding="utf-8")
        monkeypatch.setenv("OIA_PRE_EXISTING", "from-process")
        monkeypatch.delenv("OIA_NEW_VAR", raising=False)
        app_boot.load_env("bin", str(tmp_path))
        assert os.environ["OIA_PRE_EXISTING"] == "from-process"
        assert os.environ["OIA_NEW_VAR"] == "from-file"

    def test_missing_file_is_silent(self, tmp_path: Path) -> None:
        warnings: List[str] = []
        app_boot.load_env("bin", str(tmp_path), warnings.append)
        assert warnings == []

    def test_read_failure_warns(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
        real_open = open

        def broken_open(file, *args, **kwargs):
            if str(file).endswith(".env"):
                raise PermissionError("denied")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", broken_open)
        warnings: List[str] = []
        app_boot.load_env("bin", str(tmp_path), warnings.append)
        assert warnings == ["bin: failed to load .env: denied\n"]


class TestLoadLayeredEnv:
    @pytest.fixture()
    def layered(self, tmp_path: Path, monkeypatch):
        project = tmp_path / "project"
        home = tmp_path / "home"
        project.mkdir()
        home.mkdir()
        (project / ".env").write_text(
            "OIA_SHARED=project\nOIA_PROJECT=project-only\n",
            encoding="utf-8")
        (home / ".env").write_text(
            "OIA_SHARED=home\nOIA_HOME=home-only\n", encoding="utf-8")
        monkeypatch.setenv("OIA_SHARED", "process")
        monkeypatch.setenv("DSH_HOME", str(home))
        monkeypatch.delenv("OIA_PROJECT", raising=False)
        monkeypatch.delenv("OIA_HOME", raising=False)
        return project, home

    def test_layer_precedence_and_snapshot(self, layered) -> None:
        project, home = layered
        snapshot = app_boot.load_layered_env("bin", cwd=str(project))
        # inherited beats both files; project beats home; both apply
        assert os.environ["OIA_SHARED"] == "process"
        assert os.environ["OIA_PROJECT"] == "project-only"
        assert os.environ["OIA_HOME"] == "home-only"
        # the snapshot preserves which layer supplied each value
        assert snapshot.get_from("OIA_SHARED", ["process", "project-env",
                                                "user-env"]).value == "process"
        assert snapshot.get_from("OIA_PROJECT", ["process", "project-env",
                                                 "user-env"]).value == \
            "project-only"
        sources = [layer.source for layer in snapshot.layers]
        assert sources == ["process", "project-env", "user-env"]

    def test_same_directory_reads_once(self, tmp_path: Path,
                                       monkeypatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".env").write_text("OIA_SOLO=1\n", encoding="utf-8")
        monkeypatch.setenv("DSH_HOME", str(home))
        monkeypatch.delenv("OIA_SOLO", raising=False)
        snapshot = app_boot.load_layered_env("bin", cwd=str(home))
        sources = [layer.source for layer in snapshot.layers]
        assert sources == ["process", "project-env"]  # no duplicate user layer
        assert os.environ["OIA_SOLO"] == "1"

    def test_both_files_checked_before_applying(self, tmp_path: Path,
                                                monkeypatch) -> None:
        project = tmp_path / "project"
        home = tmp_path / "home"
        project.mkdir()
        home.mkdir()
        (project / ".env").write_text("OIA_OK=applied?\n", encoding="utf-8")
        (home / ".env").write_text("PATH=/evil\n", encoding="utf-8")
        monkeypatch.setenv("DSH_HOME", str(home))
        monkeypatch.delenv("OIA_OK", raising=False)
        with pytest.raises(RuntimeError, match="PATH"):
            app_boot.load_layered_env("bin", cwd=str(project))
        assert "OIA_OK" not in os.environ  # nothing applied before the throw

    def test_absent_files_yield_process_only_snapshot(self, tmp_path: Path,
                                                      monkeypatch) -> None:
        monkeypatch.setenv("DSH_HOME", str(tmp_path / "nohome"))
        snapshot = app_boot.load_layered_env("bin", cwd=str(tmp_path))
        assert [layer.source for layer in snapshot.layers] == ["process"]


# ---------------------------------------------------------- fail-loud

def fake_proc(lines: List[str], exits: List[int]) -> Dict[str, Any]:
    return {"write_stderr": lines.append, "exit": exits.append}


class TestInstallFailLoud:
    def hook(self):
        return sys.excepthook

    def test_diagnostic_and_exit(self) -> None:
        lines: List[str] = []
        exits: List[int] = []
        uninstall = app_boot.install_fail_loud(
            "bin", fake_proc(lines, exits))
        try:
            error = ValueError("boom")
            sys.excepthook(type(error), error, None)
        finally:
            uninstall()
        assert exits == [1]
        assert lines[0].startswith("bin: fatal uncaught exception: ")
        assert "ValueError('boom')" in lines[0]

    def test_latch_keeps_first_failure(self) -> None:
        lines: List[str] = []
        exits: List[int] = []
        uninstall = app_boot.install_fail_loud("bin", fake_proc(lines, exits))
        try:
            first = ValueError("first")
            second = ValueError("second")
            sys.excepthook(type(first), first, None)
            sys.excepthook(type(second), second, None)
        finally:
            uninstall()
        assert exits == [1]
        assert len(lines) == 1
        assert "first" in lines[0]

    def test_cause_chain_and_attributes(self) -> None:
        lines: List[str] = []
        exits: List[int] = []
        uninstall = app_boot.install_fail_loud("bin", fake_proc(lines, exits))
        try:
            try:
                raise KeyError("inner")
            except KeyError as inner:
                # constructor-set filename lands in the C-level slots the
                # diagnostic reads through getattr (the fs-error rationale)
                raise OSError(2, "no such file", "/some/path") from inner
        except OSError as captured:
            sys.excepthook(type(captured), captured, None)
        finally:
            uninstall()
        assert "no such file" in lines[0]
        assert "KeyError('inner')" in lines[0]
        assert "/some/path" in lines[0]  # the enumerable-attribute rationale

    def test_release_runs_before_exit(self) -> None:
        lines: List[str] = []
        exits: List[int] = []
        order: List[str] = []
        uninstall = app_boot.install_fail_loud(
            "bin", fake_proc(lines, exits),
            release=lambda: order.append("released"))
        try:
            error = ValueError("x")
            sys.excepthook(type(error), error, None)
        finally:
            uninstall()
        assert order == ["released"]
        assert exits == [1]

    def test_release_failure_is_swallowed(self) -> None:
        lines: List[str] = []
        exits: List[int] = []

        def broken_release():
            raise RuntimeError("release failed")

        uninstall = app_boot.install_fail_loud(
            "bin", fake_proc(lines, exits), release=broken_release)
        try:
            error = ValueError("x")
            sys.excepthook(type(error), error, None)
        finally:
            uninstall()
        assert exits == [1]  # the pending fatal exit owns the outcome

    def test_release_timeout_still_exits(self) -> None:
        lines: List[str] = []
        exits: List[int] = []
        started = time.monotonic()
        uninstall = app_boot.install_fail_loud(
            "bin", fake_proc(lines, exits), release=lambda: time.sleep(10))
        try:
            error = ValueError("x")
            sys.excepthook(type(error), error, None)
        finally:
            uninstall()
        elapsed = time.monotonic() - started
        assert exits == [1]
        # a wedged disposer delays the fatal exit, never cancels it
        assert 1.9 <= elapsed < 5.0

    def test_assembled_exemption_window(self) -> None:
        lines: List[str] = []
        exits: List[int] = []
        uninstall = app_boot.install_fail_loud("bin", fake_proc(lines, exits))
        try:
            rejection = ValueError("already reported by the audit")
            app_boot.retain_assembled_rejection(rejection)
            try:
                sys.excepthook(type(rejection), rejection, None)
            finally:
                app_boot.release_assembled_rejection(rejection)
            assert exits == []  # exempt during the checkpoint
            # after the release the same shape is fatal again
            other = ValueError("fresh")
            sys.excepthook(type(other), other, None)
            assert exits == [1]
        finally:
            uninstall()

    def test_threading_hook_is_installed_and_removed(self) -> None:
        saved_thread_hook = threading.excepthook
        saved_hook = sys.excepthook
        uninstall = app_boot.install_fail_loud("bin", fake_proc([], []))
        try:
            assert sys.excepthook is not saved_hook
            assert threading.excepthook is not saved_thread_hook

            class Args:
                exc_value = ValueError("thread boom")

            lines: List[str] = []
            exits: List[int] = []
            # re-install with a recording proc to observe the thread path
            uninstall()
            uninstall = app_boot.install_fail_loud("bin", fake_proc(lines, exits))

            class Args2:
                exc_value = ValueError("thread boom")

            threading.excepthook(cast(threading.ExceptHookArgs, Args2()))
            assert exits == [1]
            assert "thread boom" in lines[0]
        finally:
            uninstall()
        assert sys.excepthook is saved_hook
        assert threading.excepthook is saved_thread_hook

    def test_observe_checkpoint_releases(self) -> None:
        reason = ValueError("r")
        app_boot.observe_loader_rejection_checkpoint([reason])
        assert id(reason) not in app_boot._assembled_activation_rejections


# ------------------------------------------------------------ audit face

class FakePluginHost:
    """The inventory face inactive_entries consumes."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def inventory(self) -> List[Dict[str, Any]]:
        return list(self._rows)


def row(ident: str, phase: str, desired: str = "enabled",
        last_error: Optional[str] = None,
        dependencies: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"id": ident, "name": f"{ident}-pkg", "phase": phase,
            "desired": desired, "last_error": last_error,
            "dependencies": dependencies or []}


class TestInactiveEntries:
    def test_active_and_disabled_skip(self) -> None:
        host = FakePluginHost([row("a", "active"), row("b", "disposed",
                                                       desired="disabled")])
        assert app_boot.inactive_entries(host) == []

    def test_failed_carries_last_error(self) -> None:
        host = FakePluginHost([row("a", "failed", last_error="boom detail")])
        failures = app_boot.inactive_entries(host)
        assert failures[0]["outcome"] == {"kind": "failed",
                                          "error": "boom detail"}

    def test_failed_without_error_uses_default(self) -> None:
        host = FakePluginHost([row("a", "failed", last_error=None)])
        assert app_boot.inactive_entries(host)[0]["outcome"]["error"] == \
            "failed to activate"

    def test_pending_lists_unsatisfied_dependencies(self) -> None:
        host = FakePluginHost([
            row("app", "pending", dependencies=["dep-a", "dep-b"]),
            row("dep-a", "active"),
        ])
        failures = app_boot.inactive_entries(host)
        pending = [f for f in failures if f["outcome"]["kind"] == "pending"]
        assert pending[0]["outcome"]["missing"] == ["dep-b"]

    def test_disposed_but_enabled_is_reported_failed(self) -> None:
        host = FakePluginHost([row("a", "disposed", last_error="crashed")])
        failures = app_boot.inactive_entries(host)
        assert failures[0]["outcome"] == {"kind": "failed",
                                          "error": "crashed"}


class TestDiagnostics:
    def test_inactive_diagnostic_shapes(self) -> None:
        failed = {"entry": row("a", "failed"),
                  "outcome": {"kind": "failed", "error": "boom"}}
        assert app_boot.inactive_diagnostic(failed) == \
            "a (a-pkg): boom"
        pending = {"entry": row("b", "pending"),
                   "outcome": {"kind": "pending", "missing": ["svc-x"]}}
        assert app_boot.inactive_diagnostic(pending) == \
            "b (b-pkg): pending (waiting for service: svc-x)"
        unknown = {"entry": row("c", "pending"),
                   "outcome": {"kind": "pending", "missing": []}}
        assert "waiting for services: unknown" in \
            app_boot.inactive_diagnostic(unknown)

    def test_activation_diagnostic_singular_and_plural(self) -> None:
        one = [{"entry": row("a", "failed"),
                "outcome": {"kind": "failed", "error": "x"}}]
        text = app_boot.activation_diagnostic("bin", one)
        assert text.startswith("bin: warning: 1 entry did not activate\n")
        two = one + [{"entry": row("b", "failed"),
                      "outcome": {"kind": "failed", "error": "y"}}]
        text = app_boot.activation_diagnostic("bin", two)
        assert "2 entries did not activate" in text
        assert "a (a-pkg): x" in text and "b (b-pkg): y" in text

    def test_startup_diagnostic_groups_and_marks_required(self) -> None:
        failures = [
            {"entry": row("webserver", "failed", last_error="port taken"),
             "outcome": {"kind": "failed", "error": "port taken"}},
            {"entry": row("app", "pending", dependencies=["webserver"]),
             "outcome": {"kind": "pending", "missing": ["webserver"]}},
        ]
        text = app_boot.startup_diagnostic(
            "bin", failures, ["webserver", "app"])
        assert "bin: startup failed: 2 required plugins did not activate" in text
        assert "Failed plugins (1):" in text
        assert "webserver (required)" in text
        assert "port taken" in text
        assert "Plugins waiting for services (1):" in text
        assert "app (required)" in text
        assert "webserver" in text.split("Missing services")[-1]

    def test_startup_diagnostic_singular(self) -> None:
        failures = [{"entry": row("a", "failed"),
                     "outcome": {"kind": "failed", "error": "x"}}]
        text = app_boot.startup_diagnostic("bin", failures, ["a"])
        assert "1 required plugin did not activate" in text


class TestAudit:
    def test_rejecting_face_throws_startup_error(self) -> None:
        host = FakePluginHost([row("a", "failed", last_error="boom")])
        with pytest.raises(app_boot.StartupError) as info:
            app_boot.audit_startup_entries(host, "bin")
        error = info.value
        assert "bin: startup failed" in str(error)
        assert error.entries[0]["id"] == "a"
        assert error.entries[0]["required"] is True
        assert isinstance(error.__cause__, ExceptionGroup)
        assert error.startup is None  # the boot owner attaches it later

    def test_rejecting_face_passes_when_settled(self) -> None:
        host = FakePluginHost([row("a", "active")])
        app_boot.audit_startup_entries(host, "bin")  # no throw

    def test_pending_entry_carries_synthetic_failure(self) -> None:
        host = FakePluginHost([row("app", "pending", dependencies=["dep"])])
        with pytest.raises(app_boot.StartupError) as info:
            app_boot.audit_startup_entries(host, "bin")
        entry = info.value.entries[0]
        assert isinstance(entry["failure"], RuntimeError)
        assert "waiting for service: dep" in str(entry["failure"])

    def test_warning_face_renders_and_returns(self) -> None:
        host = FakePluginHost([row("a", "failed", last_error="boom")])
        warnings: List[str] = []
        rendered = app_boot.audit_startup_warnings(host, "bin",
                                                   warnings.append)
        assert len(warnings) == 1
        assert "warning: 1 entry did not activate" in warnings[0]
        assert rendered == ["a (a-pkg): boom"]

    def test_warning_face_silent_when_settled(self) -> None:
        host = FakePluginHost([row("a", "active")])
        warnings: List[str] = []
        assert app_boot.audit_startup_warnings(host, "bin",
                                               warnings.append) == []
        assert warnings == []


class TestStartupErrorAndFormatting:
    def test_format_activation_error_chain(self) -> None:
        try:
            try:
                raise KeyError("inner")
            except KeyError as inner:
                raise OSError("outer") from inner
        except OSError as captured:
            text = app_boot.format_activation_error(captured)
        assert "KeyError" in text and "OSError" in text

    def test_format_activation_error_group_members(self) -> None:
        group = ExceptionGroup("g", [ValueError("m1"), TypeError("m2")])
        text = app_boot.format_activation_error(group)
        assert "m1" in text and "m2" in text

    def test_format_activation_error_dedupes_cycles(self) -> None:
        error = RuntimeError("self")
        error.__cause__ = error  # a cycle the renderer must survive
        text = app_boot.format_activation_error(error)
        assert text.count("self") >= 1

    def test_format_activation_error_non_exception(self) -> None:
        assert app_boot.format_activation_error("plain string") == \
            "plain string"


class TestHarnessSourceSection:
    def test_no_prompt_is_a_no_op(self) -> None:
        assert app_boot.add_harness_source_section(None, "/root") is None

    def test_section_text_is_verbatim(self) -> None:
        calls: List[Dict[str, Any]] = []

        class FakePrompt:
            def get_section_order(self, name):
                return 42

            def section(self, payload):
                calls.append(payload)
                return lambda: None

        disposer = app_boot.add_harness_source_section(
            FakePrompt(), "/checkout/root")
        assert callable(disposer)
        payload = calls[0]
        assert payload["name"] == "harness:source"
        assert payload["order"] == 42
        assert payload["text"] == (
            "The DeepSeek Harness implementation checkout is at "
            "/checkout/root. The checkout location and current working "
            "directory are separate values and may differ; never infer the "
            "working directory from this path. Use pwd to determine the "
            "current working directory. Use this checkout only to inspect "
            "or extend DSH itself.")
