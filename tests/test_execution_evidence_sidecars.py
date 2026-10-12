"""Fix AG：SQLite sidecar 不得计入执行完整性快照（否则检查自我失效）。

实测来源（doc 18, r13b, 2026-09-15）：PATCH-R004-RUNTIME 以 overall_score=0.9917、
六个维度全部 >= 0.95 通过验收，**一秒后**被
`ExecutionStore.accept()` 以 "Execution evidence is stale; files changed since
execution" 否决，节点被打回 repair_pending 并重跑，代价是一整轮执行加一份 churn。

因果链：accept() 先对 owned_paths 下每个 .db 跑只读 quick_check / foreign_key_check；
SQLite 即使只读打开也会写 -wal / -shm。随后 evidence() 拍快照与执行结束时比对，
发现这些 sidecar 变了 —— 也就是**验收自己改动的字节把验收判成了失效**。
该节点快照中确有 `_runtime/probe_run.db-shm|-wal`、`selfcheck_run.db-shm|-wal`。

.db 主文件仍然参与哈希，真实的完整性保证不受影响。
"""
from __future__ import annotations

import sqlite3

from AstralNexus.runtime.execution_store import snapshot_files


def _make_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t(v) VALUES ('x')")
    con.commit()
    con.close()


def test_sqlite_sidecars_are_not_part_of_the_snapshot(tmp_path):
    owned = tmp_path / "app" / "backend"
    _make_db(owned / "run.db")
    (owned / "run.db-wal").write_bytes(b"wal-bytes")
    (owned / "run.db-shm").write_bytes(b"shm-bytes")
    (owned / "run.db-journal").write_bytes(b"journal-bytes")
    (owned / "app.py").write_text("print('x')\n", encoding="utf-8")

    snap = snapshot_files(tmp_path, ["app/backend"])

    assert "app/backend/run.db" in snap, "数据库主文件必须仍被哈希"
    assert "app/backend/app.py" in snap
    for sidecar in ("run.db-wal", "run.db-shm", "run.db-journal"):
        assert f"app/backend/{sidecar}" not in snap, sidecar


def test_opening_the_database_read_only_no_longer_changes_the_snapshot(tmp_path):
    """线上因果链的最小复现：只读打开即触碰 sidecar。"""
    owned = tmp_path / "app" / "backend"
    _make_db(owned / "run.db")

    before = snapshot_files(tmp_path, ["app/backend"])

    con = sqlite3.connect(owned / "run.db")
    con.execute("PRAGMA quick_check").fetchall()
    con.execute("PRAGMA foreign_key_check").fetchall()
    con.execute("SELECT count(*) FROM t").fetchall()
    con.close()

    after = snapshot_files(tmp_path, ["app/backend"])
    assert after == before, "只读校验不得使执行证据失效"


def test_real_content_change_is_still_detected(tmp_path):
    """Fix AG 不得削弱完整性保证。"""
    owned = tmp_path / "app" / "backend"
    _make_db(owned / "run.db")
    (owned / "app.py").write_text("print('v1')\n", encoding="utf-8")
    before = snapshot_files(tmp_path, ["app/backend"])

    (owned / "app.py").write_text("print('v2')\n", encoding="utf-8")
    assert snapshot_files(tmp_path, ["app/backend"]) != before

    after_src = snapshot_files(tmp_path, ["app/backend"])
    con = sqlite3.connect(owned / "run.db")
    con.execute("INSERT INTO t(v) VALUES ('y')")
    con.commit()
    con.close()
    assert snapshot_files(tmp_path, ["app/backend"]) != after_src, (
        "数据库主文件的真实写入仍必须被发现"
    )
