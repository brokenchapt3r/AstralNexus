"""Keep checkout layout and the TUI child import namespace consistent."""
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

from AstralNexus.cli import native_tui


def test_src_checkout_finds_sibling_tui_and_child_can_import(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    assert native_tui.source_root() == root
    assert native_tui.bundle_path() == root / 'tui/dist/entry.js'
    captured = {}
    monkeypatch.setattr(native_tui, 'resolve_node_executable', lambda: 'node')
    monkeypatch.setattr(native_tui, 'ensure_bundle_current', lambda node: None)
    monkeypatch.setattr(native_tui.subprocess, 'run', lambda argv, **kwargs:
                        captured.update(kwargs) or SimpleNamespace(returncode=0))
    assert native_tui.launch_native_tui() == 0
    assert captured['env']['ASTRALNEXUS_PYTHON_SRC_ROOT'] == str(root / 'src')


def test_checkout_metadata_and_runtime_assets_have_one_location():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'setup.py').is_file() and (root / 'pyproject.toml').is_file()
    assert (root / 'src/AstralNexus/dashboard/workbench.html').is_file()
    assert (root / 'src/AstralNexus/contracts/subagent_status_contract.json').is_file()
    assert not (root / 'AstralNexus-agent').exists()
    assert not (root / 'apps').exists()
