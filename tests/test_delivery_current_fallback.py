"""Fix AX：``deliverables/current`` 无法建链时必须"可解释"，而不是静默消失。

实测（r13b 主机，2026-09-17）：

    Path.symlink_to(...)      -> OSError [WinError 1314] 客户端没有所需的特权
    _winapi.CreateJunction(..)-> PermissionError [WinError 5]（本沙箱内）
    → 工作区与 %TEMP% 表现一致

旧代码在这里是 ``except (OSError, NotImplementedError): pass``：链接建不成，
``deliverables/current`` 就不存在，而**任何地方都不记录原因**。
消费者的读法又分两派（读路径 vs 读 ``current.json``），于是"交付已发布"
与"交付不存在"的判据在主机之间不一致。

修复后：symlink → junction（无需该特权）→ file_only，并把**实际生效的模式与
失败原因**写进 manifest 的 ``current_pointer`` 字段。
"""
from __future__ import annotations
from tests.publication_support import authorize_files

import json
from pathlib import Path

import pytest

from AstralNexus.core.delivery import DeliveryManager


def _staged(run_dir: Path) -> DeliveryManager:
    manager = DeliveryManager(run_dir)
    staging = manager.begin()
    (staging / "app").mkdir(parents=True, exist_ok=True)
    (staging / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    (staging / "docs").mkdir(parents=True, exist_ok=True)
    (staging / "docs" / "readme.md").write_text("# d\n", encoding="utf-8")
    return manager


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    target = tmp_path / "run"
    target.mkdir()
    return target


# ------------------------------------------------------------------ 模式 1: symlink
def test_symlink_mode_is_preferred_and_reports_no_reason(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, bool]] = []

    def fake_symlink_to(self: Path, target, target_is_directory: bool = False) -> None:
        calls.append((str(target), target_is_directory))
        # 真实 publish() 在建链前已 mkdir(deliverables_dir)；这里单独测
        # _link_current，需自己保证父目录存在，否则写占位文件会抛
        # FileNotFoundError（属 OSError）而被误判成建链失败。
        self.parent.mkdir(parents=True, exist_ok=True)
        self.write_text("link-placeholder", encoding="utf-8")

    monkeypatch.setattr(Path, "symlink_to", fake_symlink_to)
    manager = _staged(run_dir)
    mode, reason = manager._link_current(manager.deliverables_dir / "v1")
    assert mode == "symlink"
    assert reason is None
    assert calls == [("v1", True)], "必须用相对目标名 + target_is_directory=True"


# ----------------------------------------------------------------- 模式 2: junction
def test_junction_fallback_when_symlink_is_denied(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path, target, target_is_directory: bool = False) -> None:
        raise OSError(1314, "客户端没有所需的特权。")

    monkeypatch.setattr(Path, "symlink_to", denied)
    import _winapi  # type: ignore[import-not-found]

    calls: list[tuple[str, str]] = []

    def fake_junction(src: str, dst: str) -> None:
        calls.append((src, dst))

    monkeypatch.setattr(_winapi, "CreateJunction", fake_junction, raising=False)
    manager = _staged(run_dir)
    version_dir = manager.deliverables_dir / "v1"
    mode, reason = manager._link_current(version_dir)
    assert mode == "junction"
    assert calls and calls[0][0] == str(version_dir.resolve()), "junction 源必须是绝对路径"
    assert "1314" in (reason or "") or "symlink" in (reason or ""), "应保留 symlink 失败原因"


# ---------------------------------------------------------------- 模式 3: file_only
def test_file_only_mode_records_both_reasons(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path, target, target_is_directory: bool = False) -> None:
        raise OSError(1314, "客户端没有所需的特权。")

    monkeypatch.setattr(Path, "symlink_to", denied)
    import _winapi  # type: ignore[import-not-found]

    def denied_junction(src: str, dst: str) -> None:
        raise PermissionError(5, "拒绝访问。")

    monkeypatch.setattr(_winapi, "CreateJunction", denied_junction, raising=False)
    manager = _staged(run_dir)
    mode, reason = manager._link_current(manager.deliverables_dir / "v1")
    assert mode == "file_only"
    assert "1314" in (reason or "") and "拒绝访问" in (reason or "")


# --------------------------------------------------------- 发布产物里的可审计事实
def test_publish_records_pointer_mode_in_published_manifest(run_dir: Path) -> None:
    """关键回归：manifest 必须在 **published 版本目录** 里被重写。

    temp_dir 在 ``os.rename`` 之后已不存在，若仍按旧路径写，就会在版本目录外
    凭空重建临时目录（或直接失败），发布出去的 manifest 也就不含该字段。
    """
    manager = _staged(run_dir)
    manifest = manager.publish(authorized=authorize_files(manager))
    version_dir = manager.deliverables_dir / f"v{manifest['version']}"

    pointer = manifest.get("current_pointer")
    assert isinstance(pointer, dict), "manifest 必须记录 current_pointer"
    assert pointer["mode"] in {"symlink", "junction", "file_only"}
    assert pointer["target"] == f"v{manifest['version']}"
    assert pointer["fallback_pointer_file"] == "current.json"

    published_manifest = version_dir / "delivery_manifest.json"
    assert published_manifest.is_file()
    on_disk = json.loads(published_manifest.read_text(encoding="utf-8"))
    assert on_disk["current_pointer"] == pointer, "发布出去的 manifest 必须与返回值一致"

    # current.json 恒写且可判定指向（Fix O 的契约不得被本次修改破坏）
    pointer_file = manager.deliverables_dir / "current.json"
    assert json.loads(pointer_file.read_text(encoding="utf-8"))["current_version"] == manifest["version"]

    # 链接成功时路径必须真的可读；失败时 mode 已如实记录，不得伪造
    current = manager.deliverables_dir / "current"
    if pointer["mode"] in {"symlink", "junction"}:
        assert current.exists()
    else:
        assert not current.exists()
        assert pointer["reason"], "未能建链时必须写明原因"


def test_file_only_delivery_is_still_readable_through_current_json(run_dir: Path) -> None:
    """无链接主机上的降级路径：交付不算失败，读取方走 current.json。"""
    manager = _staged(run_dir)
    manifest = manager.publish(authorized=authorize_files(manager))
    version_dir = manager.deliverables_dir / f"v{manifest['version']}"
    pointer_file = manager.deliverables_dir / "current.json"
    resolved = manager.deliverables_dir / json.loads(
        pointer_file.read_text(encoding="utf-8")
    )["path"]
    assert resolved == version_dir
    assert (resolved / "app" / "main.py").is_file()
    assert manifest["status"] == "published"


def test_by_products_are_still_excluded_and_counted(run_dir: Path) -> None:
    """Fix Q 契约不得被本次修改破坏。"""
    manager = DeliveryManager(run_dir)
    staging = manager.begin()
    (staging / "app").mkdir(parents=True)
    (staging / "app" / "main.py").write_text("x", encoding="utf-8")
    (staging / "app" / "__pycache__").mkdir(parents=True)
    (staging / "app" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00")
    manifest = manager.publish(authorized=authorize_files(manager))
    paths = {item["path"] for item in manifest["artifacts"]}
    assert paths == {"app/main.py"}
    assert manifest["excluded_count"] == 1