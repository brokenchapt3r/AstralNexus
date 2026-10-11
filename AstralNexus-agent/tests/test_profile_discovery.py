"""Tests for core.profile_discovery — multi-source Worker Profile discovery."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from AstralNexus.core.profile_discovery import (
    DiscoveryResult,
    ProfileDiscovery,
    ProfileSource,
    WorkerProfileSpec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_profile(name: str, **overrides) -> WorkerProfileSpec:
    defaults: dict[str, Any] = dict(
        name=name,
        description=f"Test profile {name}",
        capabilities=["generic"],
        tools=["file_read"],
        input_contract="task",
        output_contract="result",
        risk_level="low",
        model_preferences={"model": "default"},
    )
    defaults.update(overrides)
    return WorkerProfileSpec(**defaults)


def _write_json(directory: str, filename: str, data: dict) -> str:
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _valid_profile_dict(name: str = "test_worker", **overrides) -> dict:
    d = dict(
        name=name,
        description="A test profile",
        capabilities=["coding"],
        tools=["file_read", "file_write"],
        input_contract="code requirements",
        output_contract="modified code",
        risk_level="low",
        model_preferences={"model": "qwen-max"},
    )
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# WorkerProfileSpec tests
# ---------------------------------------------------------------------------

class TestWorkerProfileSpec:
    def test_valid_construction(self):
        spec = _make_profile("alpha")
        assert spec.name == "alpha"
        assert spec.risk_level == "low"
        assert spec.source == ProfileSource.BUILTIN

    def test_invalid_risk_level_raises(self):
        with pytest.raises(ValueError, match="risk_level"):
            WorkerProfileSpec(name="bad", description="bad", risk_level="critical")

    def test_risk_level_normalized(self):
        spec = _make_profile("x", risk_level="HIGH")
        assert spec.risk_level == "high"

    def test_to_dict_roundtrip(self):
        spec = _make_profile("round", capabilities=["a", "b"], tools=["t1"])
        d = spec.to_dict()
        assert d["name"] == "round"
        assert d["capabilities"] == ["a", "b"]
        restored = WorkerProfileSpec.from_dict(d)
        assert restored.name == "round"
        assert restored.tools == ["t1"]

    def test_from_dict_missing_required(self):
        with pytest.raises(ValueError, match="missing"):
            WorkerProfileSpec.from_dict({"name": "incomplete"})

    def test_source_preserved(self):
        spec = _make_profile("p", source=ProfileSource.PROJECT, source_path="/x.json")
        assert spec.source == ProfileSource.PROJECT
        assert spec.source_path == "/x.json"


# ---------------------------------------------------------------------------
# ProfileDiscovery tests
# ---------------------------------------------------------------------------

class TestProfileDiscovery:
    def test_builtin_only(self):
        builtins = [_make_profile("a"), _make_profile("b")]
        disc = ProfileDiscovery(builtins, user_dir="/nonexistent", project_dir="/nonexistent")
        result = disc.discover()
        assert set(result.list_names()) == {"a", "b"}

    def test_user_overrides_builtin(self):
        builtin = _make_profile("worker", description="builtin version")
        with tempfile.TemporaryDirectory() as user_dir:
            _write_json(user_dir, "worker.json",
                        _valid_profile_dict("worker", description="user version"))
            disc = ProfileDiscovery([builtin], user_dir=user_dir, project_dir="/nonexistent")
            result = disc.discover()
            assert result.get("worker").description == "user version"
            assert result.get("worker").source == ProfileSource.USER

    def test_project_overrides_user(self):
        with tempfile.TemporaryDirectory() as user_dir, \
             tempfile.TemporaryDirectory() as proj_dir:
            _write_json(user_dir, "w.json",
                        _valid_profile_dict("w", description="user"))
            _write_json(proj_dir, "w.json",
                        _valid_profile_dict("w", description="project"))
            disc = ProfileDiscovery([], user_dir=user_dir, project_dir=proj_dir)
            result = disc.discover()
            assert result.get("w").description == "project"
            assert result.get("w").source == ProfileSource.PROJECT

    def test_task_overrides_all(self):
        builtin = _make_profile("w", description="builtin")
        disc = ProfileDiscovery([builtin], user_dir="/nonexistent", project_dir="/nonexistent")
        task_spec = _make_profile("w", description="task-scoped")
        disc.register_task_profile(task_spec)
        result = disc.discover()
        assert result.get("w").description == "task-scoped"
        assert result.get("w").source == ProfileSource.TASK

    def test_project_high_risk_denied_without_approval(self):
        """Project-level profiles declaring risk_level=high must be downgraded."""
        with tempfile.TemporaryDirectory() as proj_dir:
            _write_json(proj_dir, "dangerous.json",
                        _valid_profile_dict("dangerous", risk_level="high"))
            disc = ProfileDiscovery([], user_dir="/nonexistent", project_dir=proj_dir)
            result = disc.discover()
            # Downgraded to medium
            assert result.get("dangerous").risk_level == "medium"
            assert len(result.rejected) == 1
            assert "high_risk" in result.rejected[0]["reason"]

    def test_project_high_risk_approved_by_governor(self):
        """Governor-approved high-risk project profiles keep their risk_level."""
        with tempfile.TemporaryDirectory() as proj_dir:
            _write_json(proj_dir, "approved.json",
                        _valid_profile_dict("approved", risk_level="high"))
            disc = ProfileDiscovery(
                [], user_dir="/nonexistent", project_dir=proj_dir,
                governor_approved_high_risk={"approved"},
            )
            result = disc.discover()
            assert result.get("approved").risk_level == "high"
            assert len(result.rejected) == 0

    def test_invalid_json_rejected(self):
        with tempfile.TemporaryDirectory() as proj_dir:
            bad_path = os.path.join(proj_dir, "bad.json")
            with open(bad_path, "w") as f:
                f.write("{invalid json content")
            disc = ProfileDiscovery([], user_dir="/nonexistent", project_dir=proj_dir)
            result = disc.discover()
            assert len(result.rejected) == 1
            assert "parse_error" in result.rejected[0]["reason"]

    def test_missing_required_fields_rejected(self):
        with tempfile.TemporaryDirectory() as proj_dir:
            _write_json(proj_dir, "incomplete.json", {"name": "x"})
            disc = ProfileDiscovery([], user_dir="/nonexistent", project_dir=proj_dir)
            result = disc.discover()
            assert len(result.rejected) == 1
            assert "missing_fields" in result.rejected[0]["reason"]

    def test_non_json_files_ignored(self):
        with tempfile.TemporaryDirectory() as proj_dir:
            # Write a .txt file — should be skipped
            with open(os.path.join(proj_dir, "readme.txt"), "w") as f:
                f.write("not a profile")
            _write_json(proj_dir, "valid.json", _valid_profile_dict("valid"))
            disc = ProfileDiscovery([], user_dir="/nonexistent", project_dir=proj_dir)
            result = disc.discover()
            assert "valid" in result.list_names()
            assert len(result.rejected) == 0

    def test_discovery_summary(self):
        builtins = [_make_profile("a"), _make_profile("b")]
        disc = ProfileDiscovery(builtins, user_dir="/nonexistent", project_dir="/nonexistent")
        result = disc.discover()
        summary = result.to_summary()
        assert summary["total_profiles"] == 2
        assert summary["by_source"]["builtin"] == 2

    def test_unregister_task_profile(self):
        disc = ProfileDiscovery([], user_dir="/nonexistent", project_dir="/nonexistent")
        disc.register_task_profile(_make_profile("tmp"))
        assert disc.discover().get("tmp") is not None
        disc.unregister_task_profile("tmp")
        assert disc.discover().get("tmp") is None

    def test_empty_directories_no_error(self):
        with tempfile.TemporaryDirectory() as user_dir, \
             tempfile.TemporaryDirectory() as proj_dir:
            disc = ProfileDiscovery([], user_dir=user_dir, project_dir=proj_dir)
            result = disc.discover()
            assert result.to_summary()["total_profiles"] == 0

    def test_multiple_profiles_from_directory(self):
        with tempfile.TemporaryDirectory() as user_dir:
            _write_json(user_dir, "coder.json", _valid_profile_dict("coder"))
            _write_json(user_dir, "researcher.json", _valid_profile_dict("researcher"))
            _write_json(user_dir, "verifier.json", _valid_profile_dict("verifier"))
            disc = ProfileDiscovery([], user_dir=user_dir, project_dir="/nonexistent")
            result = disc.discover()
            assert set(result.list_names()) == {"coder", "researcher", "verifier"}

    def test_user_high_risk_allowed(self):
        """User-level profiles CAN declare high-risk (only project is gated)."""
        with tempfile.TemporaryDirectory() as user_dir:
            _write_json(user_dir, "power.json",
                        _valid_profile_dict("power", risk_level="high"))
            disc = ProfileDiscovery([], user_dir=user_dir, project_dir="/nonexistent")
            result = disc.discover()
            assert result.get("power").risk_level == "high"
            assert len(result.rejected) == 0
