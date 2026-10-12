# -*- coding: utf-8 -*-
"""Fix BD —— 重开一个已有运行目录时，run_id 必须沿用它自己声明的那个。

默认 run_id 原本是 `f"run-{int(time.time())}"`（秒级精度）。同一个运行目录被
第二次打开而调用方没有显式传 run_id 时，拿到的是**另一个**编号，运行状态随即自我拒绝：

    ValueError: runtime state belongs to run-1790357307, not run-1790357308

落不落在同一秒，纯看运气——`test_end_to_end_takeover_fences_old_executor`
就是因此时而通过时而失败。后果与 Fix AR 同族：一个运行无法恢复自己的状态。

运行目录本来就带着自己的身份（runtime_state.json 的 run_id），沿用它即可。
"""
from __future__ import annotations

import json

from AstralNexus.core.orchestrator import Orchestrator, _existing_run_id


def test_reopening_a_run_directory_adopts_its_declared_id(tmp_path):
    first = Orchestrator(tmp_path, max_iterations=1, run_id="run-declared")
    first._save_checkpoint()
    again = Orchestrator(tmp_path, max_iterations=1)     # 不传 run_id
    assert again.run_id == "run-declared"


def test_a_fresh_directory_still_mints_an_id(tmp_path):
    orch = Orchestrator(tmp_path / "fresh", max_iterations=1)
    assert orch.run_id.startswith("run-")


def test_an_explicit_matching_run_id_is_honoured(tmp_path):
    Orchestrator(tmp_path, max_iterations=1, run_id="run-declared")._save_checkpoint()
    assert Orchestrator(tmp_path, max_iterations=1, run_id="run-declared").run_id == "run-declared"


def test_a_conflicting_explicit_run_id_is_still_refused(tmp_path):
    """既有守卫不得被放宽：显式传入**另一个**运行的 id 仍然必须拒绝。

    本修复只改"调用方没给 id 时默认取哪个"，不改"给错了怎么办"。
    """
    import pytest

    Orchestrator(tmp_path, max_iterations=1, run_id="run-declared")._save_checkpoint()
    with pytest.raises(RuntimeError, match="runtime state belongs to"):
        Orchestrator(tmp_path, max_iterations=1, run_id="run-other")


# ---------------------------------------------------------------- 退化输入
def test_missing_state_file_yields_nothing(tmp_path):
    assert _existing_run_id(tmp_path) == ""


def test_unreadable_state_file_yields_nothing(tmp_path):
    (tmp_path / "runtime_state.json").write_text("{not json", encoding="utf-8")
    assert _existing_run_id(tmp_path) == ""


def test_state_without_run_id_yields_nothing(tmp_path):
    (tmp_path / "runtime_state.json").write_text(json.dumps({"iteration": 3}), encoding="utf-8")
    assert _existing_run_id(tmp_path) == ""


def test_non_object_state_yields_nothing(tmp_path):
    (tmp_path / "runtime_state.json").write_text("[1,2,3]", encoding="utf-8")
    assert _existing_run_id(tmp_path) == ""


def test_blank_run_id_yields_nothing(tmp_path):
    (tmp_path / "runtime_state.json").write_text(json.dumps({"run_id": "   "}), encoding="utf-8")
    assert _existing_run_id(tmp_path) == ""


def test_non_string_run_id_yields_nothing(tmp_path):
    (tmp_path / "runtime_state.json").write_text(json.dumps({"run_id": 42}), encoding="utf-8")
    assert _existing_run_id(tmp_path) == ""


def test_declared_id_is_returned_verbatim(tmp_path):
    (tmp_path / "runtime_state.json").write_text(
        json.dumps({"run_id": "run-1789438015"}), encoding="utf-8")
    assert _existing_run_id(tmp_path) == "run-1789438015"
