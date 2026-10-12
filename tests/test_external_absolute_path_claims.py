"""Fix AW 回归：绝对系统路径的提及不是"对别人工作区的所有权主张"。

线上 r13b 实证（PATCH-R017-FINAL-ACCEPTANCE-REPAIR，round 97，2026-09-17 20:12）：

    python_exec references a workspace path outside the Worker owned_paths contract:
    C:\\Users\\Finitude\\AppData\\Local\\Programs\\Python\\Python310\\python.exe

工作者只是想在脚本里用某个具体解释器（那里有它需要的 flask 2.0.2）**并**写出自己的
验收产物，就被守卫整条拒绝。`_prepare_owned_process_scope` 的注释本来就写着
"The executable itself may be an absolute system path, so only paths rooted under
this run's workspace participate in ownership checks" —— 但实现把越界判定放在了
"是否位于工作区内"的过滤**之前**，于是注释承诺的豁免从未发生。

本修复只改"谁拥有工作区之外的候选路径"：
  - **绝对写法**（``C:\\...``）且解析到工作区之外 → 跳过（那里没有本守卫保护的东西）；
  - **相对写法**（``..\\..\\x``）→ 保持严格，因为 `../` 逃逸后依然是在指向工作区内的路径；
  - 绝对写法但**落在工作区内** → 保持严格（越界即拒），所有权语义不变。

因此这组用例同时断言"修好了"和"没有放松"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from AstralNexus.tools.context import tool_owned_paths, tool_workspace
from AstralNexus.tools.executor import ToolExecutor

OWNED = "deliverables/runs/final-freeze"
LIVE_INTERPRETER = r"C:\Users\Finitude\AppData\Local\Programs\Python\Python310\python.exe"


def guard(workspace: Path, code: str) -> str | None:
    """在给定的工作区/所有权上下文里跑一次 python_exec 守卫。"""
    with tool_workspace(workspace), tool_owned_paths([OWNED]):
        executor = ToolExecutor(workspace_root=workspace)
        return executor._prepare_owned_process_scope(
            "python_exec",
            {"code": code, "cwd": str(workspace / OWNED)},
        )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    (root / OWNED).mkdir(parents=True, exist_ok=True)
    (root / "other_worker").mkdir(parents=True, exist_ok=True)
    return root


def test_the_exact_live_refusal_no_longer_reproduces(workspace: Path) -> None:
    code = (
        "import subprocess, json, pathlib\n"
        f'subprocess.run([r"{LIVE_INTERPRETER}", "serve.py"])\n'
        "pathlib.Path('acceptance-rerun.json').write_text(json.dumps({}))\n"
    )
    assert guard(workspace, code) is None


@pytest.mark.parametrize(
    "interpreter",
    [
        r"C:\Python311\python.exe",
        "D:/toolchains/python/pythonw.exe",
        r"C:\ProgramData\anaconda3\python.exe",
        r"C:\Windows\System32\cmd.exe",
    ],
)
def test_absolute_program_paths_outside_the_workspace_are_allowed(
    workspace: Path, interpreter: str
) -> None:
    code = (
        f'subprocess.run([r"{interpreter}", "-V"])\n'
        "with open('out.txt', 'w') as fh:\n    fh.write('x')\n"
    )
    assert guard(workspace, code) is None


def test_absolute_path_inside_the_workspace_but_owned_by_another_worker_is_still_refused(
    workspace: Path,
) -> None:
    """所有权语义不变：工作区内的越界绝对路径仍必须拒绝。"""
    other = workspace / "other_worker" / "report.json"
    code = f'open(r"{other}", "w").write("x")\n'
    error = guard(workspace, code)
    assert error is not None
    assert "outside the Worker owned_paths contract" in error
    assert "other_worker" in error


def test_relative_traversal_escape_is_still_refused(workspace: Path) -> None:
    """`../` 逃逸保护不变（这正是原来的检查顺序要防的东西）。"""
    code = "open(r'..\\..\\..\\escaped.txt', 'w').write('x')\n"
    error = guard(workspace, code)
    assert error is not None
    assert "outside the Worker owned_paths contract" in error


def test_relative_traversal_escape_is_still_refused_when_spelled_with_slashes(
    workspace: Path,
) -> None:
    code = "p = '../../../../escaped2.txt'\nopen(p, 'w').write('x')\n"
    assert guard(workspace, code) is not None


def test_absolute_path_outside_workspace_without_writes_is_allowed(workspace: Path) -> None:
    """无写入时本来就允许（本次修复不应改变这一既有行为）。"""
    code = f'print(open(r"{LIVE_INTERPRETER}", "rb").read(4))\n'
    assert guard(workspace, code) is None


def test_localhost_url_is_still_not_a_path_claim(workspace: Path) -> None:
    """Fix AS 的保护不变：既写文件又打 localhost 的脚本必须放行。"""
    code = (
        "import urllib.request\n"
        "urllib.request.urlopen('http://127.0.0.1:8291/health')\n"
        "open('acceptance-rerun.log', 'a').write('ok')\n"
    )
    assert guard(workspace, code) is None


def test_owned_write_inside_the_contract_is_allowed(workspace: Path) -> None:
    target = workspace / OWNED / "acceptance-rerun.json"
    code = f'open(r"{target}", "w").write("{{}}")\n'
    assert guard(workspace, code) is None