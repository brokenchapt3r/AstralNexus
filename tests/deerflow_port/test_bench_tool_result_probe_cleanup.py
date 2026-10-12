"""移植自 deer-flow@2814bd5d498a backend/tests/test_bench_tool_result_probe_cleanup.py（移植器生成，断言逐字保留）。

被依赖的实现经 `deerflow_compat` 垫片替换为 AstralNexus 的等价入口；
缺失能力以 skip 标注，契约保留，不假装通过。
"""
from __future__ import annotations

# Explicit compatibility bindings; no sys.path or sys.modules mutation.
import pytest
from tests.deerflow_port import deerflow_compat as _compat


"""The probe may only clean directories it owns (review finding on #5255).

``--outputs-dir`` is user-supplied and may pre-exist with unrelated files:
on success or failure, the probe must remove only its own per-run
``probe-run-*`` child (plus its unique temp directory), never the requested
directory itself or anything beside it.
"""


import importlib.util
from types import ModuleType
import sys
from pathlib import Path

import pytest


def _load_probe_module() -> ModuleType:
    from scripts import tool_output_probe
    return tool_output_probe


def test_successful_run_leaves_unrelated_files_alone(tmp_path: Path) -> None:
    module = _load_probe_module()
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    (outputs_dir / "user-file.txt").write_text("keep me", encoding="utf-8")
    tmp_dir = tmp_path / "scratch"

    report = module.run_probe(20_000, outputs_dir, tmp_dir)

    assert report["verdict"]["raw_content_is_full"] is True
    assert report["verdict"]["wrapped_content_stays_small"] is True
    assert (outputs_dir / "user-file.txt").read_text(encoding="utf-8") == "keep me", "unrelated content must survive"
    assert not list(outputs_dir.glob("probe-run-*")), "the owned child must be removed"
    assert not tmp_dir.exists(), "the owned temp directory must be removed with the run"


def test_failing_run_still_leaves_unrelated_files_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_probe_module()
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    (outputs_dir / "user-file.txt").write_text("keep me", encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(module, "_main", boom)
    with pytest.raises(RuntimeError):
        module.run_probe(20_000, outputs_dir, tmp_path / "scratch")

    assert (outputs_dir / "user-file.txt").read_text(encoding="utf-8") == "keep me", "cleanup on failure must still be scoped to the owned child"
    assert not list(outputs_dir.glob("probe-run-*"))


def test_preexisting_scratch_files_survive(tmp_path):
    module = _load_probe_module()
    scratch = tmp_path/"scratch"; scratch.mkdir()
    (scratch/"user.txt").write_text("keep")
    module.run_probe(20000, tmp_path/"outputs", scratch)
    assert (scratch/"user.txt").read_text()=="keep"
    assert not list(scratch.glob("probe-tmp-*"))
