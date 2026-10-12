from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from AstralNexus.cli import native_tui


def test_installed_bundle_is_used_without_nonexistent_source_files(monkeypatch, tmp_path):
    package = tmp_path / 'site' / 'AstralNexus'
    bundle = package / 'tui_assets' / 'entry.mjs'
    bundle.parent.mkdir(parents=True)
    bundle.write_text('console.log("packaged")', encoding='utf-8')
    monkeypatch.setattr(native_tui, '__file__', str(package / 'cli' / 'native_tui.py'))
    monkeypatch.setattr(native_tui, 'source_root', lambda: tmp_path / 'site')
    assert native_tui.bundle_path() == bundle
    assert native_tui._tui_source_paths() == []
    assert not native_tui.bundle_is_stale()


def test_resolve_node_prefers_explicit_astralnexus_node(monkeypatch, tmp_path: Path) -> None:
    node = tmp_path / "node.exe"
    node.write_bytes(b"node")
    monkeypatch.setenv("ASTRALNEXUS_NODE", str(node))
    monkeypatch.setenv("ASTRALNEXUS_NODE", str(node))

    assert native_tui.resolve_node_executable() == str(node.resolve())


def test_resolve_node_uses_system_path_when_unconfigured(monkeypatch, tmp_path: Path) -> None:
    node = tmp_path / "node.exe"
    node.write_bytes(b"node")
    monkeypatch.delenv("ASTRALNEXUS_NODE", raising=False)
    monkeypatch.setattr(native_tui.shutil, "which", lambda name: str(node))
    monkeypatch.setattr(native_tui.sys, "platform", "win32")

    assert native_tui.resolve_node_executable() == str(node.resolve())


def test_bundle_is_stale_when_source_is_newer(monkeypatch, tmp_path: Path) -> None:
    bundle = tmp_path / "entry.js"
    source = tmp_path / "entry.tsx"
    bundle.write_text("old", encoding="utf-8")
    source.write_text("new", encoding="utf-8")
    bundle.touch()
    source.touch()
    source_mtime = bundle.stat().st_mtime_ns + 1_000_000
    import os

    os.utime(source, ns=(source_mtime, source_mtime))
    monkeypatch.setattr(native_tui, "bundle_path", lambda: bundle)
    monkeypatch.setattr(native_tui, "_tui_source_paths", lambda: [source])

    assert native_tui.bundle_is_stale() is True


def test_launch_uses_primary_buffer_and_native_mouse(monkeypatch, tmp_path: Path) -> None:
    node = tmp_path / "node.exe"
    bundle = tmp_path / "entry.js"
    node.write_bytes(b"node")
    bundle.write_text("// bundle", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(native_tui, "resolve_node_executable", lambda: str(node))
    monkeypatch.setattr(native_tui, "bundle_path", lambda: bundle)
    monkeypatch.setattr(native_tui, "ensure_bundle_current", lambda _node: None)

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native_tui.subprocess, "run", fake_run)

    assert native_tui.launch_native_tui() == 0
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["ASTRALNEXUS_TUI_INLINE"] == "1"
    assert env["ASTRALNEXUS_TUI_DISABLE_MOUSE"] == "1"
    assert env["ASTRALNEXUS_TUI_MOUSE_TRACKING"] == "off"


def test_launch_clears_inline_viewport_after_child_exit(monkeypatch, tmp_path: Path) -> None:
    def _capture_callback(value):
        writes.append(value)
        return len(value)

    node = tmp_path / "node.exe"
    bundle = tmp_path / "entry.js"
    node.write_bytes(b"node")
    bundle.write_text("// bundle", encoding="utf-8")
    writes: list[str] = []

    monkeypatch.setattr(native_tui, "resolve_node_executable", lambda: str(node))
    monkeypatch.setattr(native_tui, "bundle_path", lambda: bundle)
    monkeypatch.setattr(native_tui, "ensure_bundle_current", lambda _node: None)
    monkeypatch.setattr(
        native_tui.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(native_tui.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        native_tui.sys.stdout,
        "write",
        _capture_callback,
    )
    monkeypatch.setattr(native_tui.sys.stdout, "flush", lambda: None)

    assert native_tui.launch_native_tui() == 0
    assert writes == ["\x1b[H\x1b[2J\x1b[3J\x1b[H"]
