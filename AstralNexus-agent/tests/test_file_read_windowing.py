# -*- coding: utf-8 -*-
"""Fix AP —— file_read 必须能定址文件的任意区域。

背景（线上证据）：PATCH-R016-FINAL-ACCEPTANCE-RERUN 单个节点里，
40903 字节的 app.py 被读了 77 次（71 次 python_exec 切片 + 6 次 file_read），
213 次读取里 162 次是 python_exec 的 `print(Path(...).read_text()[a:b])`。
原因是 file_read 只返回 text[:max_chars]：它会告诉工作者 truncated=True，
却不提供任何取到后半段的手段，于是唯一出路就是用 python_exec 自己切片，
读取循环吃光整个节点预算，最后被 tool loop guard 拦下，实测项全部为空。

这组用例锁住三件事：
1) 默认行为不变（读头部）；
2) 能按 offset 分页，且 next_offset 让分页可终止；
3) 能按 search_files 给出的 1 基行号直接定址区域。
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.tools import builtin_tools as bt


@pytest.fixture()
def sample(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    target = tmp_path / "sample.py"
    # 显式写字节：Windows 的 write_text 会把 \n 翻成 \r\n，而 _file_read 是
    # read_bytes + decode，断言必须对着磁盘上真实的字节，而不是我想象的字节。
    target.write_bytes("".join(f"line{i:03d}\n" for i in range(1, 201)).encode("utf-8"))
    return target


@pytest.fixture()
def crlf_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE", str(tmp_path))
    target = tmp_path / "crlf.py"
    target.write_bytes("".join(f"line{i:03d}\r\n" for i in range(1, 51)).encode("utf-8"))
    return target


def _read(target, **kw):
    return json.loads(bt._file_read({"path": str(target), **kw}))


# ---------------------------------------------------------------- 1. 默认行为
def test_default_read_is_unchanged_head(sample):
    out = _read(sample)
    assert out["status"] == "success"
    assert out["content"].startswith("line001\n")
    assert out["eof"] is True
    assert out["truncated"] is False
    assert out["offset"] == 0
    assert out["window_mode"] == "head"


def test_default_head_is_truncated_when_over_max_chars(sample):
    out = _read(sample, max_chars=40)
    assert out["truncated"] is True
    assert out["eof"] is False
    assert out["returned_chars"] == 40
    assert out["next_offset"] == 40


def test_truncated_result_tells_the_caller_how_to_continue(sample):
    out = _read(sample, max_chars=40)
    assert "next_offset" in out["continuation"] or "offset=" in out["continuation"]
    assert "start_line" in out["continuation"]


# ---------------------------------------------------------------- 2. offset 分页
def test_offset_window_returns_the_requested_region(sample):
    head = _read(sample, max_chars=40)
    nxt = _read(sample, max_chars=40, offset=head["next_offset"])
    assert nxt["offset"] == 40
    assert nxt["content"] == sample.read_text(encoding="utf-8")[40:80]
    assert nxt["window_mode"] == "offset"


def test_offset_paging_terminates_and_reconstructs_the_file(sample):
    text = sample.read_text(encoding="utf-8")
    parts, offset, guard = [], 0, 0
    while True:
        guard += 1
        assert guard < 200, "分页没有终止 —— next_offset 必须单调推进"
        out = _read(sample, max_chars=97, offset=offset)
        parts.append(out["content"])
        if out["eof"]:
            assert out["next_offset"] is None
            break
        assert out["next_offset"] > offset
        offset = out["next_offset"]
    assert "".join(parts) == text


def test_offset_past_end_is_empty_and_eof(sample):
    total = len(sample.read_text(encoding="utf-8"))
    out = _read(sample, offset=total + 500)
    assert out["content"] == ""
    assert out["eof"] is True
    assert out["next_offset"] is None


def test_offset_reports_the_line_it_landed_in(sample):
    out = _read(sample, offset=8, max_chars=8)   # 第 2 行起
    assert out["start_line"] == 2
    assert out["content"] == "line002\n"


# ---------------------------------------------------------------- 3. 行号定址
def test_line_range_addresses_the_region_search_files_points_at(sample):
    out = _read(sample, start_line=100, end_line=102)
    assert out["content"] == "line100\nline101\nline102\n"
    assert out["start_line"] == 100
    assert out["end_line"] == 102
    assert out["total_lines"] == 200
    assert out["window_mode"] == "lines"


def test_start_line_without_end_line_runs_to_eof(sample):
    out = _read(sample, start_line=198)
    assert out["content"] == "line198\nline199\nline200\n"
    assert out["eof"] is True


def test_end_line_beyond_eof_is_clamped(sample):
    out = _read(sample, start_line=199, end_line=9999)
    assert out["end_line"] == 200
    assert out["eof"] is True


def test_start_line_beyond_eof_is_empty_not_an_error(sample):
    out = _read(sample, start_line=9999)
    assert out["status"] == "success"
    assert out["content"] == ""


def test_line_window_clipped_by_max_chars_reports_the_lines_it_actually_returned(sample):
    out = _read(sample, start_line=1, end_line=200, max_chars=24)
    assert out["content"] == "line001\nline002\nline003\n"
    assert out["start_line"] == 1
    assert out["end_line"] == 3
    assert out["eof"] is False
    assert out["next_offset"] == 24


def test_offset_and_line_range_together_are_rejected(sample):
    out = _read(sample, offset=10, start_line=3)
    assert out["status"] == "error"
    assert "start_line" in out.get("error", "") or "offset" in out.get("error", "")


# ---------------------------------------------------------------- 4. 完整性引用不变
def test_sha256_and_bytes_always_describe_the_whole_file_not_the_window(sample):
    import hashlib
    whole = hashlib.sha256(sample.read_bytes()).hexdigest()
    for kw in ({}, {"offset": 40, "max_chars": 20}, {"start_line": 100, "end_line": 101}):
        out = _read(sample, **kw)
        assert out["sha256"] == whole
        assert out["bytes"] == sample.stat().st_size
        assert out["total_chars"] == len(sample.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 5. 工具契约
def test_schema_advertises_the_addressing_parameters():
    spec = bt.registry.get_entry("file_read").schema
    props = spec["parameters"]["properties"]
    assert {"offset", "start_line", "end_line"} <= set(props)
    assert "python_exec" in spec["description"]


# ---------------------------------------------------------------- 6. CRLF（Windows 真实源文件）
def test_line_addressing_works_on_crlf_files(crlf_sample):
    out = _read(crlf_sample, start_line=10, end_line=12)
    assert out["content"] == "line010\r\nline011\r\nline012\r\n"
    assert (out["start_line"], out["end_line"]) == (10, 12)
    assert out["total_lines"] == 50


def test_crlf_offset_paging_reconstructs_the_file(crlf_sample):
    raw = crlf_sample.read_bytes().decode("utf-8")
    parts, offset, guard = [], 0, 0
    while True:
        guard += 1
        assert guard < 100
        out = _read(crlf_sample, max_chars=37, offset=offset)
        parts.append(out["content"])
        if out["eof"]:
            break
        offset = out["next_offset"]
    assert "".join(parts) == raw
