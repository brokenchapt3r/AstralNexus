"""Fix AV 回归：交付验证器的证据视图必须与"实际会发布的内容"一致。

线上 r13b 实测（2026-09-17）：``_delivery_progress_snapshot()`` 用裸 ``rglob("*")``
收集 ``deliverables/**``，不排除任何副产物。结果是 290 个文件里 83 个构建/进程副产物
（``.pytest_cache/README.md``、``.astralnexus_process_logs/*.stdout.log``、``*.pyc`` …）
把 220000 字符的有界证据预算整个吃光（只有前 14 个文件的内容被采样），
而真正封存的验收证据 ``runs/final-freeze/acceptance-rerun.json`` 排在第 175 位、
内容从未进入验证器；同时验证器看到的恰是**发布时会被 Fix Q 丢掉的东西**。

本用例覆盖三件事：
  1. 副产物被排除（且与发布层共用同一份排除清单，不会各自漂移）；
  2. 同优先级内按 mtime 倒序——重跑产物优先于历史产物；
  3. 饥饿回归：即使存在足够多的历史证据把预算吃满，封存验收证据仍必须被采样。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from AstralNexus.core import delivery as delivery_module
from AstralNexus.core import orchestrator as orchestrator_module
from AstralNexus.core.orchestrator import Orchestrator, _delivery_evidence_visible

BUDGET = 220_000
PER_FILE = 16_000


class _SnapshotHost:
    """只提供 ``_delivery_progress_snapshot`` 真正用到的两个属性。"""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.task_graph = SimpleNamespace(artifacts={}, nodes={})


def snapshot(run_dir: Path) -> tuple[str, list[str]]:
    # 故意用鸭子类型 self：该方法只依赖 run_dir 与 task_graph.artifacts/nodes，
    # 装配整个 Orchestrator 只会让用例变慢且更脆。
    return Orchestrator._delivery_progress_snapshot(cast(Any, _SnapshotHost(run_dir)))


def write(path: Path, text: str, *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.mark.parametrize(
    "relative",
    [
        "deliverables/app/__pycache__/app.cpython-311.pyc",
        "deliverables/app/.pytest_cache/README.md",
        "deliverables/app/.mypy_cache/3.11/os.data.json",
        "deliverables/app/backend/.astralnexus_process_logs/pytest_run_1.stdout.log",
        "deliverables/app/backend/.probe/probe.db",
        "deliverables/app/node_modules/pkg/index.js",
        "deliverables/app/tests/_evidence/a-wal",
        "deliverables/app/tests/_evidence/a-shm",
        "deliverables/app/backend/legacy.pyo",
    ],
)
def test_byproducts_are_not_evidence(relative: str) -> None:
    assert _delivery_evidence_visible(Path(relative)) is False


@pytest.mark.parametrize(
    "relative",
    [
        "deliverables/runs/final-freeze/acceptance-rerun.json",
        "deliverables/runs/tests/test-report.json",
        "deliverables/docs/architecture.md",
        "deliverables/app/backend/app.py",
        # 名字里带 cache 但不是副产物目录，不得误伤
        "deliverables/runs/cache_report.json",
    ],
)
def test_real_evidence_is_visible(relative: str) -> None:
    assert _delivery_evidence_visible(Path(relative)) is True


def test_exclusion_lists_are_shared_with_publish_layer() -> None:
    """同一份清单（对象同一性），避免验证器与发布层各自漂移。"""
    assert orchestrator_module._PUBLISH_EXCLUDE_PARTS is delivery_module._PUBLISH_EXCLUDE_PARTS
    assert orchestrator_module._PUBLISH_EXCLUDE_SUFFIXES == delivery_module._PUBLISH_EXCLUDE_SUFFIXES


def test_snapshot_excludes_byproducts_from_refs(tmp_path: Path) -> None:
    write(tmp_path / "deliverables/app/backend/app.py", "print(1)", mtime=1000)
    write(tmp_path / "deliverables/app/.pytest_cache/README.md", "junk", mtime=9999)
    write(tmp_path / "deliverables/app/backend/__pycache__/app.cpython-311.pyc", "bytecode", mtime=9999)
    write(tmp_path / "deliverables/runs/tests/test-report.json", "{}", mtime=2000)

    content, refs = snapshot(tmp_path)

    assert "deliverables/runs/tests/test-report.json" in refs
    assert "deliverables/app/backend/app.py" in refs
    assert not [ref for ref in refs if ".pytest_cache" in ref or "__pycache__" in ref], refs
    assert "junk" not in content


def test_sealed_evidence_survives_an_exhausted_budget(tmp_path: Path) -> None:
    """饥饿回归：历史证据足够吃满预算时，最新封存证据仍必须被采样。"""
    # 14 个历史文件各 16000 字符（含标记词）足以吃满 220000 预算
    for index in range(15):
        write(
            tmp_path / f"deliverables/runs/app_cleanup/legacy_{index:02d}_evidence.json",
            "H" * PER_FILE,
            mtime=1_000 + index,
        )
    # 与旧规则下的"元凶"同名：标记词 test 命中 ".pytest_cache"
    write(tmp_path / "deliverables/app/.pytest_cache/README.md", "J" * PER_FILE, mtime=9_000)

    sealed = tmp_path / "deliverables/runs/final-freeze/acceptance-rerun.json"
    write(sealed, '{"selfcheck_verdict": "sealed-acceptance-marker"}', mtime=5_000)

    content, _ = snapshot(tmp_path)

    assert "sealed-acceptance-marker" in content, "封存验收证据被历史证据饿死"
    # 采样是按内容累计的，确认预算确实被用满过（否则本用例证明力不足）
    assert content.count("H") >= BUDGET - PER_FILE * 2
    assert "J" not in content, "副产物仍进入采样"


def test_newest_evidence_is_sampled_first(tmp_path: Path) -> None:
    """同优先级内 mtime 倒序：重跑产物优先于历史产物。"""
    old = tmp_path / "deliverables/runs/final-check/goal-convergence.json"
    new = tmp_path / "deliverables/runs/final-freeze/acceptance-rerun.json"
    write(old, "OLD-EVIDENCE", mtime=1_000)
    write(new, "NEW-EVIDENCE", mtime=8_000)

    content, _ = snapshot(tmp_path)

    assert content.index("NEW-EVIDENCE") < content.index("OLD-EVIDENCE")


def test_ordering_is_deterministic_for_equal_mtime(tmp_path: Path) -> None:
    write(tmp_path / "deliverables/runs/tests/b_report.json", "B", mtime=4_000)
    write(tmp_path / "deliverables/runs/tests/a_report.json", "A", mtime=4_000)

    first, _ = snapshot(tmp_path)
    second, _ = snapshot(tmp_path)

    assert first == second
    assert first.index("### Artifact: deliverables/runs/tests/a_report.json") < first.index(
        "### Artifact: deliverables/runs/tests/b_report.json"
    )


def test_missing_declared_artifact_does_not_raise(tmp_path: Path) -> None:
    """已消失的声明产物只应排到最后，不能让快照抛错。

    声明产物不受 ``rglob`` 约束（可能已被清理），因此它是 ``_freshness``
    唯一可能 stat 失败的真实入口。
    """
    write(tmp_path / "deliverables/runs/tests/test-report.json", "{}", mtime=1_000)
    host = _SnapshotHost(tmp_path)
    host.task_graph.artifacts = {
        "a1": SimpleNamespace(path="deliverables/runs/gone/missing.json")
    }

    content, refs = Orchestrator._delivery_progress_snapshot(cast(Any, host))

    assert "deliverables/runs/gone/missing.json" in refs, "声明产物仍应被列出"
    assert "### Artifact: deliverables/runs/gone/missing.json" not in content
    assert "test-report.json" in content