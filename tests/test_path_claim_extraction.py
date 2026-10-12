# -*- coding: utf-8 -*-
"""Fix AS —— 所有权守卫不得把 URL 当成文件路径。

线上现场（r13b，PATCH-R017-FINAL-ACCEPTANCE-REPAIR，回执 seq 2940）：
工作者终于发起了真正的验收运行——subprocess 拉起 serve.py、复制独立 DB、
写 stop-file、用 urllib 探 /health——整条调用被拒：

    python_exec references a workspace path outside the Worker owned_paths
    contract: p:\\127.0.0.1:8291\\health

守卫的路径模式是 ``[A-Z]:[\\/]``，它匹配上了 URL scheme 的尾字母：
``http://127.0.0.1:8291/health`` 里含有 ``p://127.0.0.1:8291/health``，
``Path()`` 在 Windows 上把它规格化成 ``p:\\127.0.0.1:8291\\health``。

后果不是"少拦了一次"，而是**一个契约就是"把服务拉起来并用 HTTP 打它"的节点，
在结构上无法完成自己的工作**：任何既写文件又提到 localhost URL 的脚本都被拒。
R016 与 R017 两代都因此交出实测字段为空的验收报告。

seq 2972 是同一族的第二个误判：``R:\\n``——源码里 ``print("R:\\n")`` 的转义，
被当成 R 盘下的路径。

守卫本身保留；只修正它对"这段文本是文件路径"的判断。
"""
from __future__ import annotations

import pytest

from AstralNexus.tools.executor import _extract_path_claims


# ---------------------------------------------------------------- 1. 线上误判
def test_localhost_url_is_not_a_path_claim():
    code = (
        "import urllib.request\n"
        "with urllib.request.urlopen('http://127.0.0.1:8291/health', timeout=5) as r:\n"
        "    body = r.read()\n"
    )
    assert _extract_path_claims(code) == []


def test_the_exact_live_refusal_no_longer_reproduces():
    """seq 2940 的真实形状：既写文件又打 HTTP。"""
    code = (
        "import pathlib, subprocess, sys, os, tempfile, shutil, urllib.request\n"
        "work = pathlib.Path(tempfile.mkdtemp(prefix='r017-pilot-'))\n"
        "stop = work/'stop.flag'\n"
        "stop.write_text('stop', encoding='utf-8')\n"
        "with urllib.request.urlopen('http://127.0.0.1:8291/health') as r: print(r.status)\n"
    )
    assert all("127.0.0.1" not in claim for claim in _extract_path_claims(code))


def test_escape_sequence_is_not_a_drive_path():
    """seq 2972：print(\"R:\\n\") 里的 R:\\n。"""
    assert _extract_path_claims('print("R:\\n", R)') == []


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:5000/api/health",
    "https://example.com/a/b",
    "ftp://host/path",
    "ws://127.0.0.1:9222/devtools",
    "http://localhost:8123/api/work-orders/WO-001/transitions",
])
def test_no_url_scheme_is_mistaken_for_a_drive(url):
    assert _extract_path_claims(f"u = '{url}'") == []


def test_a_drive_letter_must_not_follow_a_word_character():
    assert _extract_path_claims("value = fooC:/bar/baz") == []


def test_host_port_is_not_a_path_component():
    """Windows 文件名不允许冒号，127.0.0.1:8291 不可能是路径分量。"""
    assert _extract_path_claims("x = 'D:/127.0.0.1:8291/health'") == []


# ---------------------------------------------------------------- 2. 守卫未被放宽
def test_real_absolute_windows_path_is_still_a_claim():
    code = "open('C:/Users/Finitude/secret.txt', 'w').write('x')"
    claims = _extract_path_claims(code)
    assert claims and any("Users" in c for c in claims)


def test_real_backslash_windows_path_is_still_a_claim():
    claims = _extract_path_claims(r"p = 'D:\runs\other-worker\out.json'")
    assert claims and any("other-worker" in c for c in claims)


def test_parent_traversal_is_still_a_claim():
    claims = _extract_path_claims("open('../../other/tree/file.txt','w')")
    assert claims and any(".." in c for c in claims)


def test_multi_level_traversal_is_still_a_claim():
    claims = _extract_path_claims(r"shutil.copy2('..\..\..\etc\shadow', dst)")
    assert claims


def test_a_path_and_a_url_in_the_same_payload_keeps_the_path():
    code = (
        "urllib.request.urlopen('http://127.0.0.1:8291/health')\n"
        "open('C:/Users/Finitude/evil.txt','w').write('x')\n"
    )
    claims = _extract_path_claims(code)
    assert any("evil.txt" in c for c in claims)
    assert all("8291" not in c for c in claims)


def test_drive_letter_at_start_of_payload_is_a_claim():
    claims = _extract_path_claims(r"C:\work\run\deliverables\x.json")
    assert claims


# ---------------------------------------------------------------- 3. 退化输入
@pytest.mark.parametrize("payload", ["", "   ", "print('hello')", "x = 1"])
def test_payloads_without_paths_yield_nothing(payload):
    assert _extract_path_claims(payload) == []


def test_none_payload_does_not_crash():
    assert _extract_path_claims(None) == []


def test_extraction_is_used_by_the_guard():
    import inspect

    from AstralNexus.tools import executor as mod

    src = inspect.getsource(mod)
    assert "_extract_path_claims(str(payload))" in src
    assert 'path_pattern = re.compile' not in src


# ---------------------------------------------------------------- 4. 历史模式的反向对照
# 不做"把修复摘掉再跑"的临时改码（线上有运行在跑同一份源码），
# 改为把**修复前的原始正则**原样钉在这里：它必须复现线上那条逐字错误，
# 而现在的提取器必须不复现。任何人日后放宽提取器，这组对照会立刻失衡。
import re as _re
from pathlib import Path as _Path

_HISTORICAL_PATTERN = _re.compile(r"(?i)(?:[A-Z]:[\\/][^\"'\r\n;|]+|(?:\.\.[\\/])+[^\"'\r\n;| ]+)")


def test_historical_pattern_did_produce_the_live_false_positive():
    """修复前：http://127.0.0.1:8291/health → p:\\127.0.0.1:8291\\health。"""
    payload = "urllib.request.urlopen('http://127.0.0.1:8291/health')"
    matches = _HISTORICAL_PATTERN.findall(payload)
    assert matches, "历史模式必须在此 URL 上命中——这正是线上被拒的原因"
    assert any(match.lower().startswith("p://127.0.0.1:8291") for match in matches), matches
    # Path() 规格化后就是错误信息里逐字出现的那个"路径"
    assert _Path(matches[0]).as_posix().lower().startswith("p:/127.0.0.1:8291")


def test_historical_pattern_also_hit_the_escape_sequence():
    assert _HISTORICAL_PATTERN.findall('print("R:\\n", R)')


def test_current_extractor_rejects_both_historical_false_positives():
    assert _extract_path_claims("urllib.request.urlopen('http://127.0.0.1:8291/health')") == []
    assert _extract_path_claims('print("R:\\n", R)') == []


def test_current_extractor_still_agrees_with_history_on_real_paths():
    """真路径上两者必须一致——修复只去掉误判，不去掉拦截。"""
    for payload in (
        "open('C:/Users/Finitude/secret.txt','w')",
        r"p = 'D:\runs\other-worker\out.json'",
        "open('../../other/tree/file.txt','w')",
    ):
        assert _HISTORICAL_PATTERN.findall(payload), payload
        assert _extract_path_claims(payload), payload
