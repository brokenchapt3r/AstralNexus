# -*- coding: utf-8 -*-
"""Fix AQ —— 落盘修复 pass 不能是一条"结构上不可能成功"的路。

线上实证（r13b gen 20，PATCH-R016-FINAL-ACCEPTANCE-RERUN）：

  主 pass 用 216 次工具调用做检索式通读（仅 app.py 一个文件就被读了 77 次），
  被 tool loop guard 拦下，没有产出任何声明文件；
  框架随即进入 expected_artifact_paths 的"文件落盘修复 pass"，
  而该 pass 过去**只授予 file_write**；
  于是它写下了 run_acceptance.py / acceptance-rerun.log / acceptance-rerun.json 三个文件，
  形状完全正确、逐条登记了 bytes 与 sha256，
  但节点验收要求的 (a)–(g) 全部是必须"跑一次才拿得到"的实测值，
  只给写工具就注定拿不到 —— 全部 unverified，verdict=failed，0.8333 被独立验证者拒绝。

这不是工作者说谎（它如实标了 failed），而是框架给了一条永远走不通的修复路，
白白消耗一个验证轮次与一格 recovery churn。

修复的边界很窄，两条都要守住：
  1) 修复 pass 保留节点自身**已授权**的执行类工具（不是新授权），使实测成为可能；
  2) 绝不带回检索类工具 —— 检索正是上一轮把预算耗光的原因。
并且提示必须把"先测后写、测不到如实标 failed"写成硬要求，
不得退化成"为了让文件存在而写空值"。
"""
from __future__ import annotations

import pytest

from AstralNexus.agents.base import (
    _ARTIFACT_REPAIR_EVIDENCE_TOOLS,
    artifact_repair_directive,
    artifact_repair_toolset,
)

# PATCH-R016 的真实 worker_profile.tools
R016_TESTING_PROFILE_TOOLS = [
    "file_read", "search_files", "terminal_exec", "python_exec",
    "pytest_run", "http_get", "file_write",
]

DISCOVERY_TOOLS = {"search_files", "web_search", "browser_fetch",
                   "openalex_search", "crossref_search", "arxiv_search",
                   "github_repo_contents"}


# ---------------------------------------------------------------- 1. 实测成为可能
def test_repair_pass_keeps_the_execution_tools_the_node_already_had():
    """R016 的档案里有 terminal_exec/python_exec/pytest_run/http_get，修复 pass 必须留住。"""
    tools = artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS)
    assert {"terminal_exec", "python_exec", "pytest_run", "http_get"} <= tools


def test_file_write_is_always_present():
    assert "file_write" in artifact_repair_toolset([])
    assert "file_write" in artifact_repair_toolset(None)
    assert "file_write" in artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS)


def test_the_old_behaviour_is_what_this_fix_replaces():
    """回归锚点：修复前这里恒等于 {'file_write'}，实测节点因此必然失败。"""
    assert artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS) != {"file_write"}


# ---------------------------------------------------------------- 2. 不得扩权
def test_repair_pass_never_grants_a_tool_the_node_did_not_have():
    """修复 pass 是保留，不是授权。档案没给的执行工具不能凭空出现。"""
    writer_only = ["file_read", "file_write"]
    assert artifact_repair_toolset(writer_only) == {"file_write"}
    doc_profile = ["file_read", "search_files", "file_write"]
    assert artifact_repair_toolset(doc_profile) == {"file_write"}


@pytest.mark.parametrize("tool", sorted(DISCOVERY_TOOLS))
def test_discovery_tools_are_never_carried_into_the_repair_pass(tool):
    """检索是上一轮烧光预算的原因；修复 pass 不把它带回来。"""
    assert tool not in artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS + [tool])
    assert tool not in _ARTIFACT_REPAIR_EVIDENCE_TOOLS


def test_file_read_stays_out_so_the_repair_pass_cannot_resume_reading():
    assert "file_read" not in artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS)


def test_empty_and_none_profiles_do_not_crash():
    assert artifact_repair_toolset([]) == {"file_write"}
    assert artifact_repair_toolset(None) == {"file_write"}
    assert artifact_repair_toolset(set()) == {"file_write"}


# ---------------------------------------------------------------- 3. 指令：先测后写
def test_directive_names_the_execution_tools_it_granted():
    text = artifact_repair_directive(artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS))
    for name in ("terminal_exec", "python_exec", "pytest_run", "http_get"):
        assert name in text


def test_directive_requires_measuring_before_writing():
    text = artifact_repair_directive(artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS))
    assert "真实执行" in text
    assert "再把实测值写进文件" in text


def test_directive_forbids_placeholder_values_in_every_case():
    """无论有没有执行工具，都不许为了让文件存在而写空值。"""
    for tools in ({"file_write"}, artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS)):
        text = artifact_repair_directive(tools)
        assert "严禁" in text and "空值" in text
        assert "failed/unverified" in text


def test_directive_without_execution_tools_does_not_promise_execution():
    text = artifact_repair_directive({"file_write"})
    assert "还授权了执行类工具" not in text
    assert "failed/unverified" in text


def test_directive_no_longer_forbids_all_other_tools():
    """旧提示写的是"不要继续检索、解释或调用其他工具"，正是它锁死了实测。"""
    text = artifact_repair_directive(artifact_repair_toolset(R016_TESTING_PROFILE_TOOLS))
    assert "调用其他工具" not in text


# ---------------------------------------------------------------- 4. 调用点确实用了这两个函数
def test_call_site_uses_the_helpers_not_a_hardcoded_file_write_set():
    import inspect

    from AstralNexus.agents import base as base_mod

    src = inspect.getsource(base_mod)
    assert "artifact_repair_toolset(context.tools)" in src
    assert "artifact_repair_directive(repair_tool_names)" in src
    assert 'registry.get_definitions({"file_write"}, quiet=True)' not in src


def test_repair_system_prompt_no_longer_says_only_file_write():
    import inspect

    from AstralNexus.agents import base as base_mod

    src = inspect.getsource(base_mod)
    assert "只能使用 file_write" not in src
