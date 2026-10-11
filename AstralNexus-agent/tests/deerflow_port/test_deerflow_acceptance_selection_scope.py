"""移植草稿（第 2 阶段批次 1）：验收门与证据防伪 —— 选择范围与 shell 清洗。

**出处**：``deer-flow@2814bd5d498a`` → ``backend/tests/test_acceptance_checks.py``。
原文件 2602 行 / 243 个测试；本批只取"**选择范围收窄**"与"**shell 清洗**"两族中
**我方尚无等价覆盖**的 12 条，逐条忠实转写（期望值取自上游原文，未放宽）。

**移植方式 = ADAPT**（不是 PORT）：上游 API 为
``check_acceptance_criteria([criterion], bash_executions=[_bash_execution(cmd, output_tail=…)])``
返回 ``{"leaves": [{"checked","holds","detail"}]}``；我方为
``run_acceptance_checks([criterion], receipts=[_receipt(cmd, stdout_tail=…)])``
返回 ``[{"criterion_id","kind","checked","holds","reason","detail"}]``。
**字段逐个对齐**（上游 ``leaves`` ↔ 我方返回列表；``detail`` 两边同名，无需改名）。

**纪律**：本文件只增测试，**不改动被断言的生产代码**。若某条在现实现上失败，
即为发现的生产缺陷/能力缺口，须单独记录并交裁决——**不得放宽断言以迁就实现**。

**位置**：本文件写在``02_运行产物/capability_migration/deerflow_test_port/drafts/``，
待写窗口 OPEN 后整文件移入 ``01_源码/AstralNexus/tests/``（不改内容）。
"""
from __future__ import annotations

import json

import pytest

from AstralNexus.runtime.acceptance_checks import run_acceptance_checks

UNPROVABLE_REASON = "TEST_COMMAND_MATCH_UNVERIFIED"


def _receipt(command: str, *, stdout_tail: str = "", returncode: int = 0,
             status: str = "success") -> dict:
    """我方 fixture：终局收据形状（与 tests/test_acceptance_zero_and_negation.py 一致）。"""
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": {},
        "receipt_id": "rc-" + command[:12].replace(" ", "").replace("\n", "N"),
        "arguments": json.dumps({"command": command}),
    }


def _one(command: str, stdout_tail: str, *, criterion: str) -> dict:
    return run_acceptance_checks(
        [criterion], receipts=[_receipt(command, stdout_tail=stdout_tail)]
    )[0]


# ---------------------------------------------------------------- 换行与操作符


def test_continuation_line_or_operator_cannot_launder_a_skipped_run():
    """上游自审：``cd backend\\n|| pytest tests/`` —— 换行后的 ``||`` 在成功命令之后
    会**整段跳过** runner 却仍退出 0；把换行当 ``;`` 解析就会把一次**从未发生**的执行
    记成无条件执行。

    出处：``test_continuation_line_or_operator_cannot_launder_a_skipped_run``
    """
    observation = _one(
        "cd backend\n|| pytest tests/", "", criterion="tests_passed:pytest tests/"
    )

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == UNPROVABLE_REASON


def test_trailing_criterion_background_operator_is_unprovable():
    """判据 ``make test &`` 要求后台化；span 必须止于**最后一个已执行段**，
    因此除 ``;`` 之外的尾随操作符永远无法被保留。

    出处：``test_trailing_criterion_background_operator_is_unprovable``
    """
    observation = _one("make test", "3 passed", criterion="tests_passed:make test &")

    assert observation["checked"] is False
    assert observation["holds"] is None
    assert observation["reason"] == UNPROVABLE_REASON


def test_trailing_criterion_semicolon_still_matches():
    """``tests_passed:make test;`` 是合法 shell 写法 —— 尾随 ``;`` 只让判据比 span
    多一个操作符，**不得因此抛异常**（异常会被任务工具整体丢弃，绕过所有判据）。

    出处：``test_trailing_criterion_semicolon_still_matches``
    """
    observation = _one("make test", "3 passed", criterion="tests_passed:make test;")

    assert observation["holds"] is True


# ---------------------------------------------------------------- 零测试否决


@pytest.mark.parametrize(
    "output",
    [
        "0 passed in 1.0s",
        "test result: ok. 0 passed; 0 failed",
        "ok  \tgithub.com/example/pkg\t0.5s [no test files]",
        "Ran 0 tests\n\nOK",
    ],
)
def test_zero_passing_tests_is_not_a_pass(output):
    """成功命令但**跑了零个测试**必须保持 UNVERIFIED，而不是 holds。

    出处：``test_zero_passing_tests_is_not_a_pass``（参数原样）
    """
    observation = _one("run tests", output, criterion="tests_passed:run tests")

    assert observation["holds"] is False, output
    assert observation["checked"] is True, output
    assert observation["reason"] == "TEST_SUMMARY_ZERO_TESTS", output


def test_zero_errors_does_not_veto_a_pass():
    """``0 errors`` 是干净运行，**不是**失败记录 —— 带计数的错误形状必须与
    failed/passed 形状一样保持"零不算失败"。

    出处：``test_zero_errors_does_not_veto_a_pass``
    """
    observation = _one(
        "pytest tests",
        "===== 4 passed, 0 errors in 0.12s =====",
        criterion="tests_passed:pytest tests",
    )

    assert observation["holds"] is True


# ------------------------------------------------------- 否定选项收窄选择范围


@pytest.mark.parametrize(
    ("command", "criterion"),
    (
        ("pytest --ignore tests/security", "pytest"),
        ("pytest --deselect tests/test_slow.py", "pytest"),
        ("python -m pytest --ignore tests/security", "python -m pytest"),
    ),
)
def test_bare_criterion_any_negating_option_is_unprovable(command, criterion):
    """**裸判据**代表 runner 的默认选择；任何否定选项都会收窄它，而裸判据里
    没有任何被消费的判据 token 可供重叠检查捕捉。

    出处：``test_bare_criterion_any_negating_option_is_unprovable``（参数原样）
    """
    observation = _one(command, "7 passed", criterion=f"tests_passed:{criterion}")

    assert observation["checked"] is False, command
    assert observation["reason"] == UNPROVABLE_REASON, command


def test_deselected_sub_path_of_the_target_is_unprovable():
    """``pytest tests --deselect tests/unit/test_auth.py`` —— 被取消选择的测试从未运行，
    故 ``3 passed`` 覆盖不了判据里的 ``tests`` 选择（真退出码、真摘要，无需伪造）。

    出处：``test_deselected_sub_path_of_the_target_is_unprovable``
    """
    observation = _one(
        "pytest tests --deselect tests/unit/test_auth.py", "3 passed",
        criterion="tests_passed:pytest tests",
    )

    assert observation["checked"] is False


def test_glued_deselect_of_a_sub_path_is_unprovable():
    """``--deselect=`` 的**粘连形式**同样收窄选择范围。

    出处：``test_glued_deselect_of_a_sub_path_is_unprovable``
    """
    observation = _one(
        "pytest tests --deselect=tests/unit/test_auth.py", "3 passed",
        criterion="tests_passed:pytest tests",
    )

    assert observation["checked"] is False


def test_nodeid_deselect_inside_the_target_is_unprovable():
    """取消选择的是**判据目标内部的 nodeid**（``tests/x.py::test_flaky``）。

    出处：``test_nodeid_deselect_inside_the_target_is_unprovable``
    """
    observation = _one(
        "pytest tests/x.py --deselect tests/x.py::test_flaky", "3 passed",
        criterion="tests_passed:pytest tests/x.py",
    )

    assert observation["checked"] is False


def test_negating_option_with_equals_form():
    """否定选项写在**目标之前**且用等号形式。

    出处：``test_negating_option_with_equals_form``
    """
    observation = _one(
        "pytest --deselect=tests/x.py tests", "3 passed",
        criterion="tests_passed:pytest tests/x.py",
    )

    assert observation["checked"] is False


def test_narrowing_positional_after_bare_criterion_is_unprovable():
    """``python -m unittest pkg.OneTest`` 把 unittest 发现范围收窄到单个测试 ——
    OK 行证明不了"全量发现"。

    出处：``test_narrowing_positional_after_bare_criterion_is_unprovable``
    """
    observation = _one(
        "python -m unittest pkg.OneTest",
        ".\n----------------------------------------------------------------------\nRan 1 test\n\nOK",
        criterion="tests_passed:python -m unittest",
    )

    assert observation["checked"] is False
    assert observation["reason"] == UNPROVABLE_REASON


# --------------------------------------------------- 不收窄选择范围 ⇒ 仍可锚定


@pytest.mark.parametrize(
    "command",
    (
        "pytest tests/x.py -q",
        "pytest -v --tb=short tests/x.py",
        "pytest tests/x.py -n4 --dist=worksteal",
        "pytest tests/x.py --maxfail=2 -rA",
    ),
)
def test_selection_preserving_extra_flags_still_match(command):
    """纯输出/并发/失败上限类开关**不收窄**选择范围 ⇒ 仍应锚定成功。

    出处：``test_selection_preserving_extra_flags_still_match``（参数原样）
    """
    observation = _one(command, "3 passed", criterion="tests_passed:pytest tests/x.py")

    assert observation["holds"] is True, command


def test_extra_positional_targets_widen_selection_and_still_match():
    """**超集**运行（目标比判据要求的多）仍然跑到了判据的测试，整体通过覆盖它们。

    出处：``test_extra_positional_targets_widen_selection_and_still_match``
    """
    observation = _one(
        "pytest tests/security tests/unit", "9 passed",
        criterion="tests_passed:pytest tests/security",
    )

    assert observation["holds"] is True