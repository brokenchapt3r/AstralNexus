#!/usr/bin/env python3
"""Main CLI entry point for the AstralNexus system."""

from __future__ import annotations
from AstralNexus.runtime.recovery_operation import recovery_operation

import argparse
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import shlex
import sys
import threading
import queue
import time
import uuid
from AstralNexus.runtime.run_lifecycle import _start_lease_heartbeat, _stop_lease_heartbeat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, TYPE_CHECKING, TypedDict, Protocol

if TYPE_CHECKING:
    from AstralNexus.core.governor import GlobalGovernor, InteractiveRouteDecision


class StreamedAnswer(TypedDict):
    started: bool
    text: list[str]


class NodeSummary(TypedDict):
    total: int
    completed: int
    failed: int
    ready: int
    waiting: int
    terminal_failures: list[dict[str, str]]

from AstralNexus.cli.commands import (
    ARGPARSE_COMMANDS,
    SESSION_COMMANDS,
    process_command_args,
    resolve_command,
)
from AstralNexus.core.identity import CODE_NAME, PRODUCT_NAME, bootstrap_environment
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.output_paths import RunOutputLayout
from AstralNexus.runtime.model_selector import DEFAULT_MODEL
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

logger = logging.getLogger(__name__)


def _runtime_notice(event_callback, event_type: str, text: str, **payload) -> None:
    """Report progress without writing non-JSON text into the native gateway."""
    if event_callback is not None:
        event_callback({"event_type": event_type, "text": text, **payload})
    else:
        print(text, flush=True)


def _write_current_run_pointer(home: Path, run_dir: Path) -> None:
    """Publish the run selected by this CLI process for live observers."""
    pointer = Path(home).expanduser() / "current_run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": Path(run_dir).name,
        "run_dir": str(Path(run_dir).expanduser().resolve()),
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    temporary = pointer.with_name(pointer.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(8):
            try:
                temporary.replace(pointer)
                break
            except PermissionError:
                # Windows readers/another replace can briefly hold the name.
                # Bound retries; persistent access failures remain failures.
                if attempt == 7:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)


def _stage_task_inputs(task_file: Path, run_dir: Path) -> list[str]:
    """Copy task-local inputs into the isolated run workspace.

    Workers must be able to read task data while all writes remain inside the
    run directory.  Staging keeps that boundary explicit and makes a run
    reproducible without hard-coding any domain or filename.
    """
    from .input_staging import stage_task_inputs
    return stage_task_inputs(task_file, run_dir)






def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for the AstralNexus CLI.
    
    Args:
        argv: Command line arguments (defaults to sys.argv)
        
    Returns:
        Exit code (0 for success, non-zero for error)
    """
    # Bootstrap environment
    home = bootstrap_environment()
    
    # Create argument parser
    parser = argparse.ArgumentParser(
        prog=CODE_NAME,
        description=f"{PRODUCT_NAME} - 面向超长程复杂任务的动态异构群体智能协同推理引擎"
    )
    
    # Add subcommands
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # Add run command
    run_parser = subparsers.add_parser("run", help="运行任务")
    run_parser.add_argument("--task", "-t", required=True, help="任务文件路径")
    run_parser.add_argument("--config", "-c", help="配置文件路径")
    run_parser.add_argument("--model", "-m", help="模型名称")
    run_parser.add_argument("--provider", "-p", help="提供商名称")
    run_parser.add_argument("--output", "-o", help="输出目录")
    run_parser.add_argument(
        "--max-parallel", type=int,
        help="ready-wave 最大并发数（默认读取 ASTRALNEXUS_MAX_PARALLEL，未设置时为 4）",
    )

    resume_parser = subparsers.add_parser("resume", help="从运行目录 checkpoint 恢复任务")
    resume_parser.add_argument(
        "--user-resume",
        action="store_true",
        help="显式用户恢复：paused 的 run 只有此旗标可恢复；监控/自动续跑不得携带",
    )
    resume_parser.add_argument("--run-dir", "-r", required=True, help="已有运行目录")
    resume_parser.add_argument("--task", "-t", help="原任务文件，用于恢复后的正式交付")
    resume_parser.add_argument("--model", "-m", help="模型名称")
    resume_parser.add_argument("--provider", "-p", help="提供商名称")
    resume_parser.add_argument(
        "--max-parallel", type=int,
        help="恢复任务的 ready-wave 最大并发数（默认读取 ASTRALNEXUS_MAX_PARALLEL）",
    )
    
    # Add eval command
    eval_parser = subparsers.add_parser("eval", help="运行评估基准")
    eval_parser.add_argument("--scenario", "-s", default="long_research", help="评测场景名称")
    eval_parser.add_argument("--nodes", "-n", type=int, default=120, help="任务图节点数")
    eval_parser.add_argument("--seed", type=int, help="随机种子（用于可复现性）")
    eval_parser.add_argument("--strict-runtime", action="store_true", help="启用严格运行时模式")
    eval_parser.add_argument("--output", "-o", help="输出目录")
    
    # Add report command
    report_parser = subparsers.add_parser("report", help="生成报告")
    report_parser.add_argument("--run-id", "-r", required=True, help="运行ID")
    report_parser.add_argument("--output", "-o", help="输出文件路径")

    verify_delivery_parser = subparsers.add_parser(
        "verify-delivery", help="不重跑 Agent/LLM，离线复验已有交付结果"
    )
    verify_delivery_parser.add_argument(
        "--run-dir", "-r", required=True, help="包含 checkpoint 和 deliverables 的运行目录"
    )
    verify_delivery_parser.add_argument(
        "--candidate", "-c", help="显式候选目录；默认依次选择 staging、current、最新版本、旧式根目录"
    )
    verify_delivery_parser.add_argument(
        "--output", "-o", help="可选 JSON 验收报告输出路径；省略时只输出到 stdout"
    )
    
    # Add dashboard command
    dashboard_parser = subparsers.add_parser("dashboard", help="启动仪表板")
    dashboard_parser.add_argument("--run-id", "-r", help="运行ID")
    dashboard_parser.add_argument("--port", "-p", type=int, default=8080, help="端口号")

    stream_parser = subparsers.add_parser("stream", help="以流式方式调用模型")
    stream_parser.add_argument("--prompt", required=True, help="用户提示词")
    stream_parser.add_argument("--model", "-m", help="模型名称")
    
    # Add inject-fault command
    fault_parser = subparsers.add_parser("inject-fault", help="注入故障")
    fault_parser.add_argument("--run-id", "-r", required=True, help="运行ID")
    fault_parser.add_argument("--type", "-t", required=True, help="故障类型")
    fault_parser.add_argument("--payload", "-p", help="故障载荷文件")
    
    # Add help command
    subparsers.add_parser("help", help="显示帮助信息")
    
    # With no arguments, start the native AstralNexus interactive shell.
    # Explicit argv remains non-interactive so scripts and tests stay deterministic.
    if argv is None and len(sys.argv) == 1:
        return _interactive_loop(home)

    # Commands whose contracts are represented by argparse retain their
    # script-friendly flags. Every other registered command uses the same
    # dispatcher as the interactive slash interface, so ``AstralNexus model``
    # and ``/model`` cannot drift into separate implementations.
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv:
        canonical = resolve_command(effective_argv[0].lstrip("/").lower())
        if canonical is not None and canonical not in ARGPARSE_COMMANDS:
            return process_command_args(effective_argv, home=home)

    # Parse arguments
    if effective_argv:
        canonical = resolve_command(effective_argv[0])
        if canonical in ARGPARSE_COMMANDS:
            effective_argv = [canonical, *effective_argv[1:]]
    args = parser.parse_args(effective_argv)
    
    # Process command
    if args.command == "run":
        return _run_task(args, home)
    elif args.command == "resume":
        return _resume_task(args, home)
    elif args.command == "eval":
        return _run_evaluation(args, home)
    elif args.command == "report":
        return _generate_report(args, home)
    elif args.command == "verify-delivery":
        return _verify_existing_delivery(args)
    elif args.command == "dashboard":
        return _start_dashboard(args, home)
    elif args.command == "stream":
        return _stream_prompt(args, home)
    elif args.command == "inject-fault":
        return _inject_fault(args, home)
    elif args.command == "help":
        parser.print_help()
        return 0
    parser.print_help()
    return 1


def _verify_existing_delivery(args) -> int:
    """Run the standalone, read-only delivery gate for a previous result."""
    from AstralNexus.core.delivery_acceptance import (
        evaluate_delivery,
        write_acceptance_report,
    )

    try:
        report = evaluate_delivery(
            Path(args.run_dir),
            candidate=Path(args.candidate) if args.candidate else None,
        )
        if args.output:
            report["report_path"] = str(Path(args.output).expanduser().resolve())
            write_acceptance_report(report, args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0 if report["delivery_authorized"] else 1
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(json.dumps({
            "status": "error",
            "delivery_authorized": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, ensure_ascii=False, indent=2), flush=True)
        return 2


from AstralNexus.cli.session_controller import _InteractiveRunController



def _resolve_interactive_task_reference(text: str) -> Optional[Path]:
    """Resolve a task file or bounded task bundle mentioned in a turn."""
    candidates = [
        Path(token.rstrip("'\"`"))
        for token in ([match.group(2) for match in re.finditer(r"(['\"`])([A-Za-z]:[\\/].+?)\1", text)]
                      + re.findall(r"[A-Za-z]:\\[^\s，。；;]+", text))
    ]
    for candidate in candidates:
        options = [candidate]
        if not candidate.suffix:
            options.append(candidate.with_suffix(".txt"))
        for option in options:
            if option.is_file():
                return option.resolve()
            if option.is_dir():
                named = option / f"{option.name}.txt"
                if named.is_file():
                    return named.resolve()
                prompts = sorted(option.glob("*.txt"))
                if len(prompts) == 1:
                    return prompts[0].resolve()
    return None


def _resolve_interactive_output_reference(text: str) -> Optional[Path]:
    """Extract an explicitly requested Windows delivery directory."""
    marker = re.search(
        r"(?:输出(?:的)?文件(?:放在|保存到)|交付目录(?:为|是)?|输出目录(?:为|是)?)\s*",
        text,
    )
    if not marker:
        return None
    match = re.search(r"[A-Za-z]:\\[^\s，。；;]+", text[marker.end():])
    if not match:
        return None
    return Path(match.group(0).rstrip("'\"`")).resolve()


class InteractiveRoutingController(Protocol):
    def set_event_callback(self, callback) -> None: ...
    def decide_route(self, text: str, history: list[dict]) -> InteractiveRouteDecision: ...
    def inject_requirement_text(self, description: str) -> None: ...
    def start(self, task: str, *, output: Optional[str] = None) -> bool: ...
    def start_text(self, text: str, *, history: Optional[list[dict]] = None,
                   output: Optional[str] = None, route_decision: Optional[dict] = None) -> bool: ...


def _govern_interactive_turn(
    line: str,
    chat_history: list[dict],
    controller: InteractiveRoutingController,
    *,
    output=None,
) -> bool:
    """Run the mandatory Governor routing boundary for ordinary text.

    Returns ``True`` when the turn was consumed by a governed run or a
    requirement-change patch.  ``False`` means the Governor explicitly chose
    the direct conversational Agent.
    """
    if output is not None:
        controller.set_event_callback(output.event)
    if line.strip().startswith("/goal "):
        started = controller.start_text(line.strip(), history=chat_history)
        response = "显式 Goal 已启用；最多自动继续 8 次，连续 2 次没有新进展时停止。" if started else "已有运行，不能同时启动新 Goal。"
        if output is not None:
            output.write_response(response)
        else:
            print(response, flush=True)
        return True
    try:
        decision = controller.decide_route(line, chat_history)
    except Exception as exc:
        message = f"Governor 路由失败，当前请求尚未执行: {exc}"
        if output is not None:
            output.write(message, error=True)
        else:
            print(message, file=sys.stderr)
        return True

    decision_data = decision.as_dict()
    if output is None:
        print(
            f"[Governor] 路由={decision.mode} 复杂度={decision.complexity_score:.2f} "
            f"并行收益={decision.parallel_benefit:.2f} 节点估计={decision.estimated_nodes}",
            flush=True,
        )
        if decision.reasoning:
            print(f"[Governor] 理由: {decision.reasoning}", flush=True)

    if decision.mode == "direct_chat" and not line.strip().startswith("/goal "):
        return False

    chat_history.append({"role": "user", "content": line})
    if decision.mode == "requirement_change":
        controller.inject_requirement_text(line)
        response = "Governor 已将该输入识别为当前任务的需求变更，正在生成并应用增量任务图 Patch。"
    else:
        task_file = _resolve_interactive_task_reference(line)
        output_dir = _resolve_interactive_output_reference(line)
        if task_file is not None and not line.strip().startswith("/goal "):
            started = controller.start(
                str(task_file),
                output=str(output_dir) if output_dir else None,
            )
        else:
            started = controller.start_text(
                line,
                history=chat_history[:-1],
                output=str(output_dir) if output_dir else None,
                route_decision=decision_data,
            )
        if started:
            response = (
                "Governor 已创建正式动态任务图并启动异构 Worker。"
                f"预计节点数约 {decision.estimated_nodes}；"
                f"能力范围：{', '.join(decision.required_capabilities) or '由 Router 动态匹配'}。"
            )
        else:
            response = "Governor 未能启动新任务；当前已有运行，请将该输入作为需求变更提交。"
    chat_history.append({"role": "assistant", "content": response})
    if output is not None:
        output.write_response(response)
    else:
        print(f"天枢: {response}", flush=True)
    return True


def _interactive_loop(home: Path) -> int:
    """Start the sole interactive entry: the native AstralNexus Ink TUI."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("AstralNexus 交互入口必须运行在支持原生 Ink TUI 的终端中")
    from AstralNexus.cli.native_tui import launch_native_tui, native_tui_available

    if not native_tui_available():
        raise RuntimeError("AstralNexus 原生 TUI 不可用：请安装 Node.js 并构建 tui/dist/entry.js")
    return launch_native_tui()


def _stream_prompt(args, home: Path) -> int:
    """Render a real model response incrementally in the terminal."""
    try:
        from AstralNexus.runtime.llm_client import LLMClient
        from AstralNexus.runtime.stream_renderer import stream_to_cli
        client = LLMClient(model=args.model, interactive=False)
        stream_to_cli(client, [{"role": "user", "content": args.prompt}])
        print()
        return 0
    except Exception as exc:
        print(f"流式调用失败: {exc}", file=sys.stderr)
        return 1


def _sweep_run_services(run_dir: Path, stage: str) -> None:
    """doc 18 修复G：run 级服务清理硬边界。

    attempt3/4/5 实证：worker 启动的后台服务在节点会话/run 结束后泄漏，
    占住端口 → 后续代 worker 启动失败 → 重复动作 → runtime_no_progress
    空转（恢复链代际全灭），甚至跨 attempt 泄漏（attempt4 进程污染
    attempt5 环境）。两个调用边界：①恢复接管启动前（清崩溃代际遗留）；
    ②run 收尾 finally（所有终态路径，含 blocked/host_exception）。
    清理失败不致命——只打印警告，绝不改写主停机语义。"""
    try:
        from AstralNexus.tools.builtin_tools import stop_all_registered_services

        stopped = stop_all_registered_services(run_dir)
        if stopped:
            print(f"[run-services] {stage}: stopped {stopped} leaked background service(s)")
    except Exception as exc:  # pragma: no cover - 卫生边界不致命
        print(f"[run-services] {stage}: sweep failed: {exc}", file=sys.stderr)


def _acquire_run_ownership(
    run_dir: Path,
    *,
    user_resume: bool,
    notice_sink,
    notice_event: str,
    run_id: str = "",
    authorization_source: str = "",
):
    """Doc 17 第三批：统一运行入口。CLI/web/测试套件/监控共用同一准入边界；
    被拒绝的双启动在任何运行状态写入之前退出（不触碰 run_dir 状态）。"""
    from AstralNexus.runtime.run_coordinator import RunCoordinator, TERMINAL_STATUSES
    import socket
    import uuid as _uuid

    coordinator = RunCoordinator(run_dir, run_id=run_id or run_dir.name)
    owner_id = f"{socket.gethostname()}:{os.getpid()}:{_uuid.uuid4().hex[:8]}"
    lease = coordinator.acquire(
        owner_id, user_resume=user_resume, authorization_source=authorization_source,
    )
    if lease is not None:
        return coordinator, lease
    record = coordinator.current() or {}
    status = str(record.get("status") or "")
    if status in TERMINAL_STATUSES:
        reason = f"run已处于终态 {status}，不再被接管"
    elif int(record.get("paused_by_user") or 0) or status == "paused":
        reason = (
            "run已被用户暂停，监控/自动续跑不得重启"
            "（用户显式恢复需 --user-resume）"
        )
    else:
        reason = (
            "另一存活执行者持有租约"
            f"（owner={record.get('owner_id')}，"
            f"lease_expires_at={record.get('lease_expires_at')}）"
        )
    _runtime_notice(
        notice_sink,
        notice_event,
        f"拒绝启动: {reason}",
        run_dir=str(run_dir),
        reason=reason,
    )
    return None, None






def _parallel_limit(value: Optional[int]) -> int:
    selected = int(os.environ.get("ASTRALNEXUS_MAX_PARALLEL", "4")) if value is None else int(value)
    if selected <= 0:
        raise ValueError("max-parallel must be positive")
    return selected


def _sweep_owned_run(coordinator, holder, run_dir: Optional[Path], stage: str) -> None:
    if coordinator is None or holder is None or run_dir is None:
        return
    try:
        record = coordinator.current() or {}
        if int(record.get("generation", -1)) == holder["lease"].generation:
            _sweep_run_services(run_dir, stage)
    except Exception as error:
        logger.warning("cleanup ownership unavailable; skipping sweep: %s", error)


def _entry_exception_disposition(error: Exception, result: dict | None = None) -> tuple[str, str, str]:
    from AstralNexus.core.exceptions import PlanningRejectedError
    if isinstance(error, PlanningRejectedError):
        return "failed", "planning_failed", str(error)[:200]
    if isinstance(result, dict) and result.get("status") in {"blocked", "interrupted"}:
        return "blocked", _derive_stop_code(str(result["status"]), "blocked", str(result.get("stop_code") or "")), (str(result.get("stop_reason") or "") + " | finalize_host_error: " + str(error))[:200]
    return "blocked", "host_exception", str(error)[:200]


def _derive_stop_code(result_status: str, final_status: str, result_stop_code: str = "") -> str:
    """闭合停机码枚举映射（doc 17 第三批第二轮修正①）。

    机器码驱动自动恢复策略；未知/缺失的 blocked 原因一律 unknown_blocked
    ——默认要求人工核查，不得猜测为崩溃类。展示文字（stop_reason）另存。
    """
    code = str(result_stop_code or "").strip()
    if code:
        return code
    mapped = {
        "completed": "completed",
        "interrupted": "user_interrupt",
    }.get(str(result_status or ""))
    if mapped:
        return mapped
    if final_status == "blocked":
        return "unknown_blocked"
    return str(result_status or "unknown") or "unknown"


def _run_task(
    args,
    home: Path,
    *,
    interrupt_event=None,
    interrupt_queue=None,
    on_orchestrator=None,
    event_callback=None,
) -> int:
    """Run a task using the Orchestrator main chain.
    
    Args:
        args: Parsed command line arguments
        home: AstralNexus home directory
        
    Returns:
        Exit code
    """
    progress_reporter = None
    coordinator = None
    lease = None
    holder = None
    hb_stop = None
    _hb_thread = None
    run_dir: Optional[Path] = None
    result: dict[str, Any] | None = None
    try:
        # Read task file
        task_file = Path(args.task)
        if not task_file.is_file():
            _runtime_notice(
                event_callback,
                "interactive.run.error",
                f"错误: 任务文件不存在: {task_file}",
            )
            return 1
        
        task_content = task_file.read_text(encoding="utf-8")
        
        max_parallel = _parallel_limit(getattr(args, "max_parallel", None))
        # Setup run directory
        run_dir = Path(args.output) if args.output else home / "runs" / f"task_{task_file.stem}_{uuid.uuid4().hex[:12]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        # 第三批统一入口：先取运行所有权，被拒则在触碰任何运行状态前退出。
        coordinator, lease = _acquire_run_ownership(
            run_dir,
            user_resume=False,
            notice_sink=event_callback,
            notice_event="interactive.run.ownership_rejected",
        )
        if lease is None:
            return 2
        holder, hb_stop, _hb_thread, _ownership_lost = _start_lease_heartbeat(
            coordinator, lease, interrupt_event=interrupt_event,
            interrupt_queue=interrupt_queue,
        )
        from AstralNexus.cli.progress_reporter import RunProgressReporter
        progress_reporter = RunProgressReporter(
            run_dir=str(run_dir),
            sink=event_callback,
        )
        runtime_event_callback = progress_reporter.handle_event
        _write_current_run_pointer(home, run_dir)
        staged_inputs = _stage_task_inputs(task_file, run_dir)
        # Formal workers operate in the isolated run workspace.  Input data is
        # staged there; file/terminal tools cannot escape this boundary.
        # Worker execution already receives an explicit workspace root; do
        # not mutate the process-wide fallback for concurrent sessions.
        
        # Create SQLite store for persistence
        graph_store_path = run_dir / "task_graph.sqlite"
        graph_store = SQLiteTaskGraphStore(graph_store_path)
        
        # Create and run orchestrator (enters main chain: TaskGraph → Router → Agents → StructuredMessage)
        from AstralNexus.core.run_kernel import RunKernel
        from AstralNexus.runtime.run_context import current_run_context
        inherited_context = current_run_context()
        delivery = None
        def operate_owned(orchestrator):
            nonlocal result, delivery
            if interrupt_event is not None and interrupt_queue is not None:
                orchestrator.attach_interrupt_control(interrupt_event, interrupt_queue)
            if on_orchestrator is not None:
                on_orchestrator(orchestrator, run_dir)
            from AstralNexus.runtime.model_selector import get_model_config
            config = get_model_config(args.config, overrides={
                "model": args.model,
                "provider": args.provider,
            })
            config["_model_explicit_override"] = bool(args.model)

            # Worker Profile selection is a real Governor control-plane decision,
            # not a fixed role map.  Enable it for formal product runs only.
            orchestrator.enable_real_llm_worker_matching(config)
            orchestrator.register_staged_inputs(staged_inputs)
            
            # Parse the complete task contract.  The delivery task files contain
            # prose and numbered acceptance requirements, not only dash bullets;
            # dropping those lines would make the LLM roles solve a different task.
            # The accepted goal is the complete user task contract.  Treating
            # the first line as the goal made URL-first task files appear as if
            # the user had only asked to visit one URL, while the actual
            # requirements were hidden in a constraint blob.
            goal = task_content.strip() or "Execute task"
            task_requirements = ""
            task_requirements += (
                "\n\n运行输入工作区（已由天枢 staging，不得读取工作区外路径）：\n"
                + "\n".join(f"- {item}" for item in staged_inputs)
            )
            constraints = [task_requirements] if task_requirements else []

            # Derive the delivery contract before the first DAG planning call.
            # Otherwise the Governor cannot see explicit file/software outputs
            # and may plan only analysis nodes, leaving the final synthesizer
            # unable to produce real source, documentation, or test artifacts.
            from AstralNexus.runtime.llm_client import LLMClient
            if not getattr(args, "goal", None):
                planning_client = LLMClient(
                    model=config.get("model") or args.model or "qwen-flash",
                    base_url=config.get("base_url"),
                    interactive=False,
                )
                orchestrator.governor.derive_delivery_contract(
                    task_content,
                    planning_client,
                    environment_summary="\n".join(staged_inputs),
                    permission_constraints=task_requirements,
                )

                # Persist task provenance before any LLM planning.  This makes the
                # accepted task unambiguous when several similarly named task files
                # exist and lets an audit distinguish input mismatch from model drift.
            orchestrator.trace_recorder.record("task.source.loaded", {
                "path": str(task_file.resolve()),
                "sha256": hashlib.sha256(task_content.encode("utf-8")).hexdigest(),
                "goal": goal,
                "original_request": task_content.strip(),
                "content_chars": len(task_content),
                "constraint_count": len(constraints),
                "staged_inputs": staged_inputs,
            })
            
            _runtime_notice(
                runtime_event_callback,
                "interactive.run.progress",
                f"开始执行任务: {goal}",
                run_dir=str(run_dir),
            )
            _runtime_notice(
                runtime_event_callback,
                "interactive.run.progress",
                f"约束条件: {len(constraints)} 个",
                constraint_count=len(constraints),
            )
            _runtime_notice(
                runtime_event_callback,
                "interactive.run.progress",
                f"运行目录: {run_dir}",
                run_dir=str(run_dir),
            )
            
            # One application entry owns execution, presentation and optional Goal.
            from AstralNexus.core.run_application import RunApplicationService
            explicit_goal = getattr(args, "goal", None)
            application = RunApplicationService(orchestrator).execute(
                task_content=("/goal " + explicit_goal) if explicit_goal else task_content,
                constraints=constraints, run_dir=run_dir, model_config=config)
            result = {"iterations": 0, "completed_tasks": 0, "failed_tasks": 0, "elapsed_seconds": 0.0,
                      **dict(application.get("execution") or {}), "status": application["status"],
                      "execution_status": application.get("execution_status"), "delivery": application}
            result["elapsed_seconds"] = application.get("elapsed_seconds", result["elapsed_seconds"])
            delivery = application.get("artifacts", {}) or None
            if result["status"] != "completed":
                result["stop_code"] = application.get("stop_code", result.get("stop_code", "delivery_failed"))
                result["stop_reason"] = application.get("stop_reason", result.get("stop_reason", ""))

            final_summary = {
                "status": result.get("status"),
                "summary": (
                    f"任务执行状态: {result['status']}；迭代 {result['iterations']}；"
                    f"完成 {result['completed_tasks']}；失败 {result['failed_tasks']}；"
                    f"运行 {result['elapsed_seconds']:.2f} 秒"
                ),
                "total_nodes": int(result.get("completed_tasks", 0))
                + int(result.get("failed_tasks", 0)),
                "successful_nodes": int(result.get("completed_tasks", 0)),
                "failed_nodes": int(result.get("failed_tasks", 0)),
                "output_dir": str(run_dir),
                "result": dict(result),
            }
            orchestrator.trace_recorder.record(
                "interactive.run.summary", final_summary
            )
            _runtime_notice(
                runtime_event_callback,
                "interactive.run.summary",
                str(final_summary["summary"]),
                result=dict(result),
            )
            if result.get("final_report"):
                _runtime_notice(
                    runtime_event_callback,
                    "interactive.run.artifact",
                    f"最终报告: {result.get('final_report')}",
                    path=str(result.get("final_report")),
                )
            if delivery:
                for label, key in (
                    ("机器摘要", "summary_json"),
                    ("呈现说明", "acceptance_report"),
                ):
                    if not delivery.get(key):
                        continue
                    _runtime_notice(
                        runtime_event_callback,
                        "interactive.run.artifact",
                        f"{label}: {delivery[key]}",
                        path=str(delivery[key]),
                        artifact_kind=key,
                    )
            elif result.get("status") == "interrupted":
                _runtime_notice(
                    runtime_event_callback,
                    "interactive.run.interrupted",
                    f"任务已中断，运行状态已保存: {run_dir / 'runtime_state.json'}",
                )
            
            # 第三批：围栏终态写入——只有当前租约持有者的写入生效；
            # 失去所有权后旧管理者的迟到写入被拒（finish 返回 False）。
            final_status = {
                "completed": "completed",
                "blocked": "blocked",
                "interrupted": "blocked",
            }.get(str(result.get("status")), "failed")
            coordinator.record_checkpoint(holder["lease"], {
                "phase": "run_returned",
                "status": result.get("status"),
                "iterations": result.get("iterations"),
            })
            coordinator.finish(
                holder["lease"], final_status,
                stop_reason=str(result.get("stop_reason") or final_status),
                stop_code=_derive_stop_code(
                    str(result.get("status") or ""), final_status,
                    str(result.get("stop_code") or ""),
                ),
            )
            progress_reporter.close(final_status=result["status"])
            return 0 if result['status'] == "completed" else 1
        with RunKernel(run_dir, engine_factory=lambda: Orchestrator(
            run_dir=run_dir,
            # Formal runs are evidence-driven.  No wall-clock or iteration
            # cap is applied; transport-level request timeouts still protect
            # individual sockets from hanging forever.
            max_iterations=0,
            graph_store=graph_store,
            max_parallel=max(1, max_parallel),
            event_callback=runtime_event_callback,
            checkpoint_sink=lambda payload: coordinator.record_checkpoint(
                holder["lease"], payload
            ),
            ownership_probe=lambda: coordinator.owns(holder["lease"]),
            fence_token_provider=lambda: coordinator.fence_token(holder["lease"]),
        ),
            settings=inherited_context.settings if inherited_context else None,
            interrupt_event=interrupt_event, interrupt_queue=interrupt_queue) as kernel:
            return int(kernel._run_entry(operate_owned).result())
    except Exception as e:
        _runtime_notice(
            progress_reporter.handle_event if progress_reporter is not None else event_callback,
            "interactive.run.error",
            f"错误: 执行任务时发生异常: {e}",
        )
        if coordinator is not None and holder is not None:
            # 第五轮实验修正：宿主异常（网络中断/提供方熔断/进程级错误）
            # 是崩溃类，必须落 blocked+host_exception（可恢复）——failed 是
            # 终态永不重新准入，一次瞬断网络不能把 run 永久判死。
            # 只有任务真实失败结果（result.status=failed）才走终态。
            # doc 18 attempt3：收尾阶段的宿主异常不得改写主停机原因——
            # run() 已返回 blocked/interrupted（如无进展熔断）时，按结果
            # 自身的闭合停机码落库，宿主错误降为 stop_reason 附加信息。
            status, code, reason = _entry_exception_disposition(e, result)
            coordinator.finish(holder["lease"], status, stop_reason=reason, stop_code=code)

        import traceback
        traceback.print_exc()
        return 1
    finally:
        _stop_lease_heartbeat(hb_stop, _hb_thread)
        _sweep_owned_run(coordinator, holder, run_dir, "run-finalize")
        if progress_reporter is not None:
            progress_reporter.close()


def _resume_task(
    args,
    home: Path,
    *,
    interrupt_event=None,
    interrupt_queue=None,
    on_orchestrator=None,
    event_callback=None,
) -> int:
    """Resume an existing run from RuntimeState and SQLite graph truth."""
    run_dir = Path(args.run_dir)
    progress_reporter = None
    coordinator = None
    lease = None
    holder = None
    hb_stop = None
    _hb_thread = None
    result: dict[str, Any] | None = None
    runtime_state_path = run_dir / "runtime_state.json"
    graph_path = run_dir / "task_graph.sqlite"
    if not graph_path.exists():
        _runtime_notice(
            event_callback,
            "interactive.resume.error",
            f"错误: 恢复需要 {runtime_state_path.name} 和 {graph_path.name}",
            run_dir=str(run_dir),
        )
        return 1
    try:
        from AstralNexus.cli.progress_reporter import RunProgressReporter

        # 第三批统一入口（gap③修正）：--resume 默认是自动续跑语义
        # （user_resume=False）——paused 的 run 结构性拒绝；只有显式
        # --user-resume（用户亲手执行）才能恢复用户暂停的 run。
        user_resume = bool(getattr(args, "user_resume", False))
        coordinator, lease = _acquire_run_ownership(
            run_dir,
            user_resume=user_resume,
            notice_sink=event_callback,
            notice_event="interactive.resume.ownership_rejected",
            authorization_source=(getattr(args, "authorization_source", "") or "cli:--user-resume") if user_resume else "",
        )
        if lease is None:
            return 2
        holder, hb_stop, _hb_thread, _ownership_lost = _start_lease_heartbeat(
            coordinator, lease, interrupt_event=interrupt_event,
            interrupt_queue=interrupt_queue,
        )
        _write_current_run_pointer(home, run_dir)
        # doc 18 修复G：取得租约后、派发前清掉崩溃代际遗留的后台服务
        # （注册表条目所属进程已随上一代宿主死亡，或在占端口毒化本代）。
        _sweep_run_services(run_dir, "resume-takeover")

        progress_reporter = RunProgressReporter(
            run_dir=str(run_dir),
            sink=event_callback,
        )
        runtime_event_callback = progress_reporter.handle_event
        from AstralNexus.core.runtime_state import read_authoritative_cursor
        authoritative = read_authoritative_cursor(run_dir)
        if not authoritative or authoritative.get("source") == "db_read_failed":
            raise ValueError("authoritative checkpoint unavailable; refusing cache-only recovery after a database error")
        state_payload = authoritative.get("cursor") or {}
        persisted_run_id = str(state_payload.get("run_id") or "").strip()
        if not persisted_run_id:
            raise ValueError("authoritative checkpoint is missing run_id")
        store = SQLiteTaskGraphStore(graph_path)
        max_parallel = _parallel_limit(args.max_parallel)
        from AstralNexus.core.run_kernel import RunKernel
        from AstralNexus.runtime.run_context import current_run_context
        inherited_context = current_run_context()
        delivery = None
        def operate_owned(orchestrator):
            nonlocal result, delivery
            if interrupt_event is not None and interrupt_queue is not None:
                orchestrator.attach_interrupt_control(interrupt_event, interrupt_queue)
            if on_orchestrator is not None:
                on_orchestrator(orchestrator, run_dir)
            # 第三批第二轮修正③ + 第五轮修正：权威游标以联合提交内的 DB
            # 载荷为准重建；runtime_state.json 只是可能被迟到写入覆盖的
            # 展示缓存。缓存不得否决 DB 载荷（含虚高序号）——异议只记异常。
            fence_row = store.read_commit_fence(persisted_run_id)
            if fence_row is not None and fence_row.get("cursor"):
                cache_seq = int(authoritative.get("cache_commit_seq") or 0)
                if cache_seq > int(fence_row["commit_seq"]):
                    orchestrator.trace_recorder.record(
                        "resume.cursor_anomaly", {
                            "anomaly": "cache_seq_ahead_of_authoritative",
                            "cache_seq": cache_seq,
                            "authoritative_seq": int(fence_row["commit_seq"]),
                            "note": "json 缓存虚高序号只记异常，不否决 DB 权威载荷",
                        }
                    )
                orchestrator.runtime_state_manager.restore_from_payload(
                    fence_row["cursor"]
                )
            if not orchestrator.restore_checkpoint():
                # halted，不是终态：保留显式 user_resume 再恢复的权利。
                coordinator.finish(
                    holder["lease"], "blocked",
                    stop_reason="checkpoint_restore_failed",
                    stop_code="checkpoint_restore_failed",
                )
                return 1
            if bool(getattr(args, "user_resume", False)):
                # 人工恢复审计入账：授权来源、原停机码、代数变迁；恢复
                # 不清除失败历史/恢复指纹/验收缺口，也不代表验收通过。
                resume_audit = coordinator.last_resume_audit()
                # doc 18：显式人工核查通过 = 授予新一轮恢复 churn 预算
                # （对齐 DeerFlow human-in-the-loop 门）；自动恢复不清零。
                prior_churn = int(
                    orchestrator.runtime_state_manager.state.recovery_churn_total or 0
                )
                # F12：熔断现在挂在"无进展"计数上，单调账本保留作审计；人工
                # 恢复必须同时清零两者，否则授权后仍会立刻再次停机。
                prior_no_progress = int(
                    orchestrator.runtime_state_manager.state.recovery_no_progress_churn
                    or 0
                )
                orchestrator.runtime_state_manager.state.recovery_churn_total = 0
                orchestrator.runtime_state_manager.state.recovery_no_progress_churn = 0
                orchestrator._recovery_budget_exhausted = False
                orchestrator.trace_recorder.record("run.user_resume.audit", {
                    "audit": resume_audit,
                    "run_dir": str(run_dir),
                    "churn_budget_reset": {
                        "prior_total": prior_churn,
                        "prior_no_progress": prior_no_progress,
                        "budget": orchestrator.recovery_churn_budget,
                        "semantics": "人工核查授权新预算；自动恢复不清零",
                    },
                })
                _runtime_notice(
                    runtime_event_callback,
                    "interactive.resume.user_authorized",
                    "用户显式恢复：仅授权再次运行，不代表验收通过，"
                    "熔断依据与失败历史保留",
                    run_dir=str(run_dir),
                    audit=resume_audit,
                )
            if bool(getattr(args, "user_resume", False)):
                if not orchestrator._persist_graph_checkpoint(stage="user_resume_budget_reset"):
                    return 1
            # 第三批 gap③修正：恢复对账用共同提交标识（commit_seq），不再用
            # 版本大小猜测一致性。写入次序不变量 S_graph >= S_state >= S_ref
            # （联合提交先落，游标次之，确认引用最后）。
            from AstralNexus.runtime.run_coordinator import reconcile_commit_identities

            reference = coordinator.last_checkpoint()
            fence_row = store.read_commit_fence(persisted_run_id)
            state_commit = int(
                orchestrator.runtime_state_manager.state.last_commit_seq or 0
            )
            graph_commit = int(fence_row["commit_seq"]) if fence_row else 0
            ref_commit = int((reference or {}).get("commit_seq") or 0)
            verdict = reconcile_commit_identities(
                graph_commit, state_commit, ref_commit
            )
            orchestrator.trace_recorder.record("resume.version_reference", {
                "protocol": "commit_seq",
                "graph_commit_seq": graph_commit,
                "state_commit_seq": state_commit,
                "reference_commit_seq": ref_commit,
                "reference": reference,
                "verdict": verdict,
            })
            if verdict.startswith("reject_"):
                _runtime_notice(
                    runtime_event_callback,
                    "interactive.resume.version_mismatch",
                    f"拒绝恢复：提交标识对账 {verdict}"
                    f"（graph={graph_commit}, state={state_commit}, "
                    f"ref={ref_commit}），库间撕裂或外部改写，需人工核查",
                    run_dir=str(run_dir),
                    verdict=verdict,
                    graph_commit_seq=graph_commit,
                    state_commit_seq=state_commit,
                    reference_commit_seq=ref_commit,
                )
                # blocked + 人工核查停机码：自动恢复规则不得再次接管。
                coordinator.finish(
                    holder["lease"], "blocked",
                    stop_reason="torn_commit_detected",
                    stop_code="torn_commit_detected",
                )
                return 1
            if verdict == "torn_window_continue":
                _runtime_notice(
                    runtime_event_callback,
                    "interactive.resume.torn_window",
                    f"图存储已联合提交至 commit_seq={graph_commit}，游标/确认"
                    f"停在 {state_commit}/{ref_commit}：崩溃发生在两次持久化之间，"
                    "任务图为权威，按显式对账协议续跑",
                    run_dir=str(run_dir),
                    verdict=verdict,
                )
            from AstralNexus.runtime.model_selector import get_model_config
            config = get_model_config(overrides={"model": args.model, "provider": args.provider})
            config["_model_explicit_override"] = bool(args.model)
            orchestrator.enable_real_llm_worker_matching(config)
            first = next(iter(orchestrator.task_graph.nodes.values()), None)
            goal = first.goal if first else "Resume the persisted AstralNexus task"
            _runtime_notice(
                runtime_event_callback,
                "interactive.resume.started",
                f"恢复任务: {goal}",
                run_dir=str(run_dir),
                goal=goal,
            )
            from AstralNexus.core.run_application import RunApplicationService
            task_content = Path(args.task).read_text(encoding="utf-8") if args.task else goal
            saved_goal = orchestrator.runtime_state_manager.state.goal_state
            active_goal = saved_goal.get("objective") if saved_goal.get("active") else None
            application = RunApplicationService(orchestrator).execute(
                task_content=("/goal " + str(active_goal)) if active_goal else task_content,
                run_dir=run_dir, model_config=config)
            result = {**dict(application.get("execution") or {}), "status": application["status"],
                      "execution_status": application.get("execution_status"), "delivery": application}
            if result["status"] != "completed":
                result["stop_code"] = application.get("stop_code", result.get("stop_code", "delivery_failed"))
                result["stop_reason"] = application.get("stop_reason", result.get("stop_reason", ""))
            _runtime_notice(runtime_event_callback, "interactive.resume.completed",
                f"任务执行状态: {result.get('status')}", run_dir=str(run_dir), status=result.get("status"),
                final_report=result.get("final_report"))
            artifacts = application.get("artifacts", {})
            if artifacts:
                _runtime_notice(runtime_event_callback, "interactive.resume.delivery",
                    f"交付状态: {application['delivery_status']}", run_dir=str(run_dir),
                    summary_json=artifacts.get("summary_json"), acceptance_report=artifacts.get("acceptance_report"))
            return result
        with RunKernel(run_dir, engine_factory=lambda: Orchestrator(
            run_dir=run_dir,
            graph_store=store,
            run_id=persisted_run_id,
            max_iterations=0,
            max_parallel=max(1, max_parallel),
            event_callback=runtime_event_callback,
            checkpoint_sink=lambda payload: coordinator.record_checkpoint(
                holder["lease"], payload
            ),
            ownership_probe=lambda: coordinator.owns(holder["lease"]),
            fence_token_provider=lambda: coordinator.fence_token(holder["lease"]),
        ),
            settings=inherited_context.settings if inherited_context else None,
            interrupt_event=interrupt_event, interrupt_queue=interrupt_queue) as kernel:
            entry_result = kernel._run_entry(operate_owned).result()
            if isinstance(entry_result, int): return entry_result
            result = dict(entry_result)
        # 第三批：围栏终态写入（与 _run_task 同一契约）。
        final_status = {
            "completed": "completed",
            "blocked": "blocked",
            "interrupted": "blocked",
        }.get(str(result.get("status")), "failed")
        coordinator.record_checkpoint(holder["lease"], {
            "phase": "run_returned",
            "status": result.get("status"),
            "iterations": result.get("iterations"),
        })
        coordinator.finish(
            holder["lease"], final_status,
            stop_reason=str(result.get("stop_reason") or final_status),
            stop_code=_derive_stop_code(
                str(result.get("status") or ""), final_status,
                str(result.get("stop_code") or ""),
            ),
        )
        progress_reporter.close(final_status=str(result.get("status") or "finished"))
        return 0 if result.get("status") == "completed" else 1
    except Exception as exc:
        import traceback
        _runtime_notice(
            progress_reporter.handle_event if progress_reporter is not None else event_callback,
            "interactive.resume.error",
            f"恢复失败: {exc}",
            run_dir=str(run_dir),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
        )
        if coordinator is not None and holder is not None:
            # 同 _run_task：宿主异常 = 崩溃类 → blocked+host_exception（可恢复）；
            # run() 已返回 blocked/interrupted 时主停机码优先，宿主错误降为附加信息。
            status, code, reason = _entry_exception_disposition(exc, result)
            coordinator.finish(holder["lease"], status, stop_reason=reason, stop_code=code)

        return 1
    finally:
        _stop_lease_heartbeat(hb_stop, _hb_thread)
        _sweep_owned_run(coordinator, holder, run_dir, "resume-finalize")
        if progress_reporter is not None:
            progress_reporter.close()



def _delivery_pipeline(orchestrator):
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.delivery_ports import adapt_delivery_runtime
    return FinalDeliveryPipeline(adapt_delivery_runtime(orchestrator), hooks={
        "synthesize": _generate_task_delivery_inner, "failure": _generate_failure_report,
    })

def _generate_task_delivery(*, task_content, run_dir, orchestrator, model_config=None):
    return _delivery_pipeline(orchestrator).execute(task_content=task_content, run_dir=run_dir, model_config=model_config)

def _generate_task_delivery_inner(*, task_content, run_dir, orchestrator, model_config=None):
    return _delivery_pipeline(orchestrator).stage("synthesize", task_content=task_content, run_dir=run_dir, model_config=model_config)

def _generate_failure_report(*, task_content, run_dir, orchestrator, reason, detail):
    return _delivery_pipeline(orchestrator).stage("failure", task_content=task_content, run_dir=run_dir, reason=reason, detail=detail)

def _run_evaluation(args, home: Path) -> int:
    """Run Phase 4 end-to-end long-task evaluation.
    
    Generates a 100+ node semantic task graph, executes it through the full
    Orchestrator chain (Router → ready-wave → compression → recovery),
    injects 4 fault types, aggregates 12-dimension metrics, and outputs
    a complete delivery package.
    
    Args:
        args: Parsed command line arguments
        home: AstralNexus home directory
        
    Returns:
        Exit code
    """
    try:
        import time
        from datetime import datetime
        from AstralNexus.eval.scenario_generator import generate_seed_scenario
        from AstralNexus.eval.metrics_aggregator import MetricsAggregator
        from AstralNexus.eval.fault_injection_runner import FaultInjectionRunner
        from AstralNexus.eval.dashboard_generator import generate_dashboard
        from AstralNexus.core.orchestrator import Orchestrator
        from AstralNexus.runtime.trace import TraceRecorder
        
        # Parse arguments
        scenario = args.scenario
        node_count = args.nodes
        seed = args.seed
        strict_runtime = args.strict_runtime
        output_dir = Path(args.output) if args.output else home / "eval" / datetime.now().strftime("%Y%m%d_%H%M%S")
        
        print(f"{'='*70}")
        print(f"AstralNexus Phase 4 Evaluation")
        print(f"{'='*70}")
        print(f"Scenario: {scenario}")
        print(f"Node Count: {node_count}")
        print(f"Seed: {seed if seed else 'random'}")
        print(f"Strict Runtime: {strict_runtime}")
        print(f"Output: {output_dir}")
        print(f"{'='*70}\n")
        
        # Enable strict runtime if requested
        if strict_runtime:
            import os
            os.environ["ASTRALNEXUS_STRICT_RUNTIME"] = "1"
            print("[OK] Strict runtime mode enabled\n")
        
        # Step 1: Generate scenario
        print("[1/6] Generating semantic task graph...")
        start_time = time.time()
        # Strict real-LLM evaluation starts from a root-only graph.  The
        # requested node count is an evaluation budget, not a fixture size.
        seed_node_count = 0 if strict_runtime else node_count
        task_graph = generate_seed_scenario(
            scenario=scenario,
            node_count=seed_node_count,
            seed=seed,
        )
        print(f"[OK] Generated root seed ({len(task_graph.nodes)} node), dynamic budget={node_count}\n")
        
        # Step 2: Initialize orchestrator
        print("[2/6] Initializing Orchestrator...")
        run_dir = output_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_current_run_pointer(home, run_dir)
        layout = RunOutputLayout(run_dir)
        layout.ensure_runtime_dirs()
        
        from AstralNexus.core.run_kernel import RunKernel
        def evaluate_owned(orchestrator):
            # Inject the generated task graph and trace recorder
            orchestrator.rebind_runtime(
                task_graph=task_graph,
                trace_recorder=TraceRecorder(run_dir=run_dir),
            )
            print(f"[OK] Orchestrator initialized with {len(task_graph.nodes)} nodes\n")
        
            # Step 3: Plan fault injections
            print("[3/6] Planning fault injections...")
            fault_runner = FaultInjectionRunner(orchestrator=orchestrator, seed=seed)
            injections = fault_runner.plan_injections(task_graph, expected_node_count=node_count)
            print(f"[OK] Planned {len(injections)} fault injections\n")
        
            # Step 4: Execute with fault injection
            print("[4/6] Executing long-task evaluation...")
            print(f"Running {node_count} nodes through full Orchestrator chain...\n")
        
            exec_start = time.time()
            completed_before_injection = 0
            injections_triggered = 0

            def _inject_after_wave(wave_results, _iteration):
                nonlocal completed_before_injection, injections_triggered
                completed_before_injection += sum(1 for ok in wave_results.values() if ok)
                injection_result = fault_runner.check_and_inject(
                    injections, completed_before_injection
                )
                if injection_result:
                    injections_triggered += 1

            run_result = orchestrator.run(
                goal=next(iter(task_graph.nodes.values())).goal if task_graph.nodes else None,
                wave_hook=_inject_after_wave,
            )
            exec_elapsed = time.time() - exec_start
            iterations = run_result.get("iterations", 0)
            completed_tasks = run_result.get("completed_tasks", completed_before_injection)
            failed_tasks = run_result.get("failed_tasks", 0)
            recovery_attempts = run_result.get("recovery_attempts", 0)
        
            # If we hit max iterations without completion
            if run_result.get("status") != "completed":
                print(f"\n[WARN] Orchestrator ended with status: {run_result.get('status')}")
        
            print(f"\n[OK] Execution completed in {exec_elapsed:.2f}s")
            final_node_count = len(task_graph.nodes)
            unique_completed = sum(1 for node in task_graph.nodes.values() if node.status == "completed")
            print(f"  Completed: {unique_completed}/{final_node_count}")
            print(f"  Failed: {failed_tasks}")
            print(f"  Iterations: {iterations}")
            print(f"  Recovery attempts: {recovery_attempts}")
            print(f"  Faults injected: {injections_triggered}\n")
        
            # Step 5: Aggregate metrics
            print("[5/6] Aggregating metrics (12 dimensions)...")
            execution_stats = {
                "elapsed_seconds": exec_elapsed,
                "iterations": iterations,
                "completed_tasks": completed_tasks,
                "failed_tasks": failed_tasks,
                "recovery_attempts": recovery_attempts,
                "faults_injected": injections_triggered,
            }
        
            aggregator = MetricsAggregator(
                trace_recorder=orchestrator.trace_recorder,
                task_graph=task_graph,
                run_dir=run_dir,
            )
        
            metrics = aggregator.aggregate_all(execution_stats)
            metrics_path = aggregator.save_metrics(metrics)
            print(f"[OK] Metrics saved to {metrics_path}\n")
        
            # Generate metrics table
            metrics_table = aggregator.generate_metrics_table(metrics)
            metrics_table_path = layout.report("metrics_table.md")
            metrics_table_path.write_text(metrics_table, encoding="utf-8")
        
            # Step 6: Generate deliverables
            print("[6/6] Generating delivery package...")
        
            # 6.1 Final report
            final_report_path = orchestrator.generate_final_report()
            print(f"  [OK] final_report.md: {final_report_path}")
        
            # 6.2 Task graph report
            task_graph_report_path = layout.report("task_graph_report.md")
            task_graph_report_lines = [
                "# Task Graph Report",
                "",
                f"**Scenario**: {scenario}",
                f"**Node Count**: {len(task_graph.nodes)}",
                f"**Edge Count**: {len(task_graph.edges)}",
                f"**Seed**: {seed if seed else 'random'}",
                "",
                "## Phase Distribution",
                "",
            ]
        
            # Group nodes by phase
            phase_counts: dict[str, int] = {}
            for node in task_graph.nodes.values():
                phase = node.metadata.get("phase", "unknown")
                phase_counts[phase] = phase_counts.get(phase, 0) + 1
        
            for phase, count in sorted(phase_counts.items(), key=lambda item: str(item[0])):
                task_graph_report_lines.append(f"- **{phase}**: {count} nodes")
        
            task_graph_report_lines.extend([
                "",
                "## Status Distribution",
                "",
            ])
        
            status_counts: dict[str, int] = {}
            for node in task_graph.nodes.values():
                status_counts[node.status] = status_counts.get(node.status, 0) + 1
        
            for status, count in sorted(status_counts.items(), key=lambda item: str(item[0])):
                task_graph_report_lines.append(f"- **{status}**: {count}")
        
            task_graph_report_path.write_text("\n".join(task_graph_report_lines), encoding="utf-8")
            print(f"  [OK] task_graph_report.md: {task_graph_report_path}")
        
            # 6.3 Fault injection report
            fault_report_path = layout.report("fault_injection_report.md")
            fault_report = fault_runner.generate_injection_report()
            fault_report_path.write_text(fault_report, encoding="utf-8")
            print(f"  [OK] fault_injection_report.md: {fault_report_path}")
        
            # 6.4 Dashboard
            dashboard_path = generate_dashboard(metrics=metrics, run_dir=run_dir)
            print(f"  [OK] dashboard.html: {dashboard_path}")
        
            # 6.5 Trace already saved by TraceRecorder
            trace_path = run_dir / "trace.jsonl"
            print(f"  [OK] trace.jsonl: {trace_path}")
        
            # 6.6 Final output summary
            final_output_path = layout.delivery("evaluation_output.md")
            final_output_lines = [
                "# AstralNexus Phase 4 Evaluation - Final Output",
                "",
                f"**Generated**: {datetime.now().isoformat()}",
                f"**Scenario**: {scenario}",
                f"**Node Count**: {node_count}",
                f"**Strict Runtime**: {strict_runtime}",
                "",
                "## Execution Summary",
                "",
                f"- Total Elapsed: {exec_elapsed:.2f}s",
                f"- Total Iterations: {iterations}",
                f"- Completed Tasks: {completed_tasks}/{node_count}",
                f"- Failed Tasks: {failed_tasks}",
                f"- Recovery Attempts: {recovery_attempts}",
                "",
                "## Deliverables",
                "",
                f"- [final_report.md](../reports/{final_report_path.name})",
                f"- [task_graph_report.md](../reports/{task_graph_report_path.name})",
                f"- [fault_injection_report.md](../reports/{fault_report_path.name})",
                f"- [metrics.json](../reports/{metrics_path.name})",
                f"- [metrics_table.md](../reports/{metrics_table_path.name})",
                f"- [dashboard.html](../reports/{dashboard_path.name})",
                f"- [trace.jsonl](../trace.jsonl)",
                "",
                "## Key Metrics",
                "",
                f"- Task Graph: {len(task_graph.nodes)} nodes, {len(task_graph.edges)} edges",
                f"- Router Sparsity: {metrics['router_metrics'].get('avg_sparsity', 0):.4f}",
                f"- Ready-Waves: {metrics['ready_wave_metrics'].get('total_waves', 0)}",
                f"- Compression Events: {metrics['compression_metrics'].get('compression_count', 0)}",
                f"- Fault Injections: {metrics['fault_recovery_metrics'].get('total_injections', 0)}",
                f"- Recovery Success Rate: {metrics['fault_recovery_metrics'].get('recovery_success_rate', 0):.2%}",
                "",
            ]
        
            final_output_path.write_text("\n".join(final_output_lines), encoding="utf-8")
            print(f"  [OK] final_output.md: {final_output_path}")
        
            # Final summary
            print(f"\n{'='*70}")
            print(f"Evaluation Complete!")
            print(f"{'='*70}")
            print(f"Output Directory: {run_dir}")
            print(f"Total Time: {time.time() - start_time:.2f}s")
            print(f"Success Rate: {unique_completed/max(1, final_node_count):.2%}")
            print(f"Throughput: {completed_tasks/max(0.001, exec_elapsed):.2f} tasks/s")
            print(f"{'='*70}\n")
        
            return 0 if completed_tasks >= node_count * 0.8 else 1
        with RunKernel(run_dir) as kernel:
            return int(kernel._run_entry(evaluate_owned).result())
    except Exception as e:
        print(f"错误: 运行评估时发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1



def _generate_report(args, home: Path) -> int:
    """Generate a report from a run.
    
    Args:
        args: Parsed command line arguments
        home: AstralNexus home directory
        
    Returns:
        Exit code
    """
    try:
        import json
        from datetime import datetime
        
        run_id = args.run_id
        print(f"生成报告，运行ID: {run_id}")
        
        # Find run directory
        runs_dir = home / "runs"
        run_dir = None
        
        for candidate in runs_dir.glob(f"*"):
            if candidate.is_dir() and (candidate / "task_graph.sqlite").exists():
                # Check if this is the run we're looking for
                graph_store = SQLiteTaskGraphStore(candidate / "task_graph.sqlite")
                runs = graph_store.list_runs()
                for run in runs:
                    if run['run_id'] == run_id:
                        run_dir = candidate
                        break
            if run_dir:
                break
        
        if not run_dir:
            print(f"错误: 找不到运行ID: {run_id}")
            print(f"请确认 {runs_dir} 目录下存在该运行")
            return 1
        
        print(f"找到运行目录: {run_dir}")
        
        # Load task graph
        graph_store_path = run_dir / "task_graph.sqlite"
        graph_store = SQLiteTaskGraphStore(graph_store_path)
        
        # Get run information
        runs = graph_store.list_runs()
        run_info = None
        for run in runs:
            if run['run_id'] == run_id:
                run_info = run
                break
        
        if not run_info:
            print(f"错误: 在数据库中找不到运行ID: {run_id}")
            return 1
        
        # Load task graph
        task_graph = graph_store.load_task_graph(run_id)
        if not task_graph:
            print(f"错误: 无法加载任务图")
            return 1
        
        # Generate report
        report_lines = [
            f"# AstralNexus 运行报告",
            f"",
            f"**运行ID**: {run_id}",
            f"**开始时间**: {run_info.get('started_at', 'N/A')}",
            f"**结束时间**: {run_info.get('ended_at', 'N/A')}",
            f"**状态**: {run_info.get('status', 'N/A')}",
            f"",
            f"## 任务图概览",
            f"",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 总节点数 | {len(task_graph.nodes)} |",
            f"| 总边数 | {len(task_graph.edges)} |",
            f"| 事实数 | {len(task_graph.facts)} |",
            f"| 工件数 | {len(task_graph.artifacts)} |",
            f"",
        ]
        
        # Add node status summary
        status_counts: dict[str, int] = {}
        for node in task_graph.nodes.values():
            status = node.status
            status_counts[status] = status_counts.get(status, 0) + 1
        
        report_lines.extend([
            f"## 节点状态统计",
            f"",
            f"| 状态 | 数量 | 百分比 |",
            f"|------|------|--------|",
        ])
        
        total_nodes = len(task_graph.nodes)
        for status, count in sorted(status_counts.items()):
            percentage = (count / total_nodes * 100) if total_nodes > 0 else 0
            report_lines.append(f"| {status} | {count} | {percentage:.1f}% |")
        
        report_lines.extend([
            f"",
            f"## 任务详情",
            f"",
        ])
        
        # Add task details
        for node in task_graph.nodes.values():
            report_lines.extend([
                f"### {node.node_id}: {node.goal[:80]}",
                f"",
                f"- **状态**: {node.status}",
                f"- **执行者**: {node.metadata.get('worker_agent_id') or node.role}",
                f"- **迭代次数**: {node.metadata.get('iteration_count', 'N/A')}",
                f"- **重试次数**: {node.retry_count}",
                f"- **创建时间**: {node.created_at or 'N/A'}",
                f"- **完成时间**: {node.updated_at if node.status == 'completed' else 'N/A'}",
                f"- **依赖**: {', '.join(node.dependencies) if node.dependencies else '无'}",
                f"",
            ])
            
            error_message = node.metadata.get("error") or (node.metadata.get("last_failure") or {}).get("message")
            if error_message:
                report_lines.extend([
                    f"**错误信息**:",
                    f"```",
                    f"{error_message}",
                    f"```",
                    f"",
                ])
        
        # Add facts summary
        if task_graph.facts:
            report_lines.extend([
                f"## 事实摘要",
                f"",
            ])
            for fact_id, fact in list(task_graph.facts.items())[:10]:  # Show first 10
                report_lines.append(f"- **{fact_id}**: {fact.text[:100]}...")
            
            if len(task_graph.facts) > 10:
                report_lines.append(f"- ... (共 {len(task_graph.facts)} 条事实)")
            report_lines.append("")
        
        # Add artifacts summary
        if task_graph.artifacts:
            report_lines.extend([
                f"## 工件摘要",
                f"",
            ])
            for artifact_id, artifact in list(task_graph.artifacts.items())[:10]:  # Show first 10
                report_lines.append(f"- **{artifact_id}**: {artifact.path}")
            
            if len(task_graph.artifacts) > 10:
                report_lines.append(f"- ... (共 {len(task_graph.artifacts)} 个工件)")
            report_lines.append("")
        
        # Save report
        output_path = Path(args.output) if args.output else run_dir / f"report_{run_id}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        
        print(f"报告已生成: {output_path}")
        return 0
        
    except Exception as e:
        print(f"错误: 生成报告时发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _start_dashboard(args, home: Path) -> int:
    """Start a web-based dashboard for monitoring runs.
    
    Args:
        args: Parsed command line arguments
        home: AstralNexus home directory
        
    Returns:
        Exit code
    """
    # The live dashboard tails the append-only trace file, so it works both
    # during an active run and after a run has completed.
    try:
        from AstralNexus.dashboard.live_server import run_dashboard

        return run_dashboard(
            home=home,
            run_id=getattr(args, "run_id", None),
            host=getattr(args, "host", "127.0.0.1"),
            port=int(getattr(args, "port", 8080)),
        )
    except ImportError as exc:
        print(f"实时仪表板依赖不可用: {exc}")
        return 1

def _inject_fault(args, home: Path) -> int:
    """Inject a fault into a running task to test recovery mechanisms.
    
    Args:
        args: Parsed command line arguments
        home: AstralNexus home directory
        
    Returns:
        Exit code
    """
    coordinator = None
    holder = None
    hb_stop = None
    hb_thread = None
    original_stop_code = ""
    original_stop_reason = ""
    try:
        import json
        from datetime import datetime
        from AstralNexus.core.orchestrator import Orchestrator
        
        run_id = args.run_id
        fault_type_str = args.type
        payload_path = args.payload
        
        print(f"注入故障，运行ID: {run_id}, 类型: {fault_type_str}")
        
        valid_types = {
            "requirement_change",
            "node_failure",
            "tool_failure",
            "memory_loss",
            "resource_degradation",
        }
        fault_type = str(fault_type_str).strip().lower()
        if fault_type not in valid_types:
            print(f"错误: 无效的故障类型: {fault_type_str}")
            print(f"支持的类型: {', '.join(sorted(valid_types))}")
            return 1
        
        # Load payload if provided
        payload = None
        if payload_path:
            payload_file = Path(payload_path)
            if not payload_file.exists():
                print(f"错误: 载荷文件不存在: {payload_file}")
                return 1
            
            with open(payload_file, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            print(f"加载载荷: {payload_file}")
        
        # Find run directory
        runs_dir = home / "runs"
        run_dir = None
        
        for candidate in runs_dir.iterdir():
            if candidate.is_dir() and (candidate / "task_graph.sqlite").exists():
                import sqlite3
                from contextlib import closing
                with closing(sqlite3.connect((candidate / "task_graph.sqlite").resolve().as_uri() + "?mode=ro", uri=True)) as database:
                    run_info = database.execute("SELECT run_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run_info:
                    run_dir = candidate
                    break
        
        if not run_dir:
            print(f"错误: 找不到运行ID: {run_id}")
            return 1
        
        print(f"找到运行目录: {run_dir}")
        
        from AstralNexus.runtime.run_coordinator import RunCoordinator
        probe = RunCoordinator(run_dir, run_id=run_dir.name)
        record = probe.current() or {}
        if record.get("status") in {"running", "pending"} and record.get("owner_id"):
            print("错误: 当前执行器仍持有运行身份；独立 inject-fault 不创建第二执行器，请先正常停止运行。")
            return 2
        coordinator, lease = _acquire_run_ownership(run_dir, user_resume=False,
            notice_sink=None, notice_event="fault_injection.ownership_rejected")
        if lease is None:
            return 2
        record = coordinator.current() or {}
        original_stop_code = str(record.get("stop_code") or "")
        original_stop_reason = str(record.get("stop_reason") or "")
        holder, hb_stop, hb_thread, _lost = _start_lease_heartbeat(coordinator, lease)
        graph_store = SQLiteTaskGraphStore(run_dir / "task_graph.sqlite")
        from AstralNexus.core.run_kernel import RunKernel
        def edit_owned(orchestrator):
            task_graph = orchestrator.load_authoritative_graph()

            # Select target node (first running or pending node)
            target_node = None
            for node in task_graph.nodes.values():
                if node.status in ['running', 'admitted', 'waiting_dependencies', 'ready']:
                    target_node = node
                    break
            
            if not target_node:
                print(f"错误: 找不到可注入故障的节点（需要运行中、待运行或就绪状态的节点）")
                return 1
            
            print(f"目标节点: {target_node.node_id} ({target_node.goal[:50]}...)")
            
            result = orchestrator.inject_fault(
                fault_type,
                target_node.node_id,
                {
                    "error_message": f"手动注入的故障: {fault_type_str}",
                    **dict(payload or {}),
                },
            )
            target_node = orchestrator.task_graph.get_node(target_node.node_id) or target_node
            fault_id = str(result.get("fault_id") or f"{fault_type}:{target_node.node_id}")
            print(f"故障已通过 Orchestrator 注入并持久化: {fault_id}")
            
            # Generate fault injection report
            report_lines = [
                f"# 故障注入报告",
                f"",
                f"**运行ID**: {run_id}",
                f"**注入时间**: {datetime.now().isoformat()}",
                f"",
                f"## 故障详情",
                f"",
                f"| 属性 | 值 |",
                f"|------|------|",
                f"| 故障ID | {fault_id} |",
                f"| 故障类型 | {fault_type} |",
                f"| 目标节点 | {target_node.node_id} |",
                f"| 恢复成功 | {result.get('recovery_success')} |",
                f"",
            ]
            
            if payload:
                report_lines.extend([
                    f"## 载荷数据",
                    f"",
                    f"```json",
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    f"```",
                    f"",
                ])
            
            report_lines.extend([
                f"## 目标节点状态",
                f"",
                f"- **节点ID**: {target_node.node_id}",
                f"- **目标**: {target_node.goal}",
                f"- **状态**: {target_node.status}",
                f"- **执行者**: {target_node.metadata.get('worker_agent_id') or target_node.role}",
                f"",
                f"## 下一步",
                f"",
                f"任务图已更新，故障已注入。系统将在下次调度时检测到故障并触发恢复机制。",
                f"",
                f"你可以：",
                f"1. 继续运行任务以观察恢复行为",
                f"2. 查看任务图状态: `python -m AstralNexus.cli.main report --run-id {run_id}`",
                f"3. 启动仪表板监控: `python -m AstralNexus.cli.main dashboard --port 8080`",
                f"",
            ])
            
            # Save report
            safe_fault_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", fault_id)
            report_path = run_dir / f"fault_injection_{safe_fault_id}.md"
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            
            print(f"故障注入报告已生成: {report_path}")
            print(f"故障注入完成！")
            return 0
        with RunKernel(run_dir, engine_factory=lambda: Orchestrator(run_dir, graph_store=graph_store, run_id=run_id,
            checkpoint_sink=lambda payload: coordinator.record_checkpoint(holder["lease"], payload),
            ownership_probe=lambda: coordinator.owns(holder["lease"]),
            fence_token_provider=lambda: coordinator.fence_token(holder["lease"]))) as kernel:
            return int(kernel._run_entry(edit_owned).result())

    except Exception as e:
        print(f"错误: 注入故障时发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        _stop_lease_heartbeat(hb_stop, hb_thread)
        if coordinator is not None and holder is not None:
            coordinator.finish(holder["lease"], "paused",
                stop_code=original_stop_code or "user_pause",
                stop_reason=(original_stop_reason + " | fault injection edit complete; explicit resume required")[:500])



def _register_interactive_delegate_tool(controller: _InteractiveRunController, home: Path) -> None:
    """Expose governed background delegation to the interactive model."""
    from AstralNexus.tools.registry import registry

    def delegate_task(args: dict) -> dict:
        task = str(args.get("task") or args.get("task_path") or "").strip()
        if not task:
            return {"status": "error", "error": "task or task_path is required"}
        candidate = Path(task).expanduser()
        if not candidate.is_file():
            task_dir = home / "interactive_tasks"
            task_dir.mkdir(parents=True, exist_ok=True)
            candidate = task_dir / f"task-{int(time.time() * 1000)}.txt"
            candidate.write_text(task, encoding="utf-8")
        started = controller.start(str(candidate), output=args.get("output"))
        output_path = args.get("output")
        planned_run_dir = Path(output_path).expanduser() if output_path else (
            home / "runs" / f"task_{candidate.stem}"
        )
        return {
            "status": "started" if started else "rejected",
            "task": str(candidate),
            "run_dir": str(planned_run_dir),
            "background": True,
            "reason": "interactive controller accepted the task" if started else "another task is running",
        }

    registry.register(
        name="delegate_task",
        toolset="interactive",
        schema={
            "name": "delegate_task",
            "description": "在后台启动一个由天枢 Governor 管理的复杂任务；参数 task 可以是任务文件路径或任务描述文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "任务文件路径或任务描述"},
                    "task_path": {"type": "string", "description": "可选任务文件路径"},
                    "output": {"type": "string", "description": "可选运行输出目录"},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
        handler=delegate_task,
        description="启动受 Governor 管理的后台任务",
        override=True,
    )


def _interactive_chat_turn(
    user_input: str,
    chat_history: list[dict],
    *,
    output=None,
) -> None:
    """Send one ordinary CLI turn to the real streaming model session.

    Command-shaped input is handled before this function.  Everything else is
    conversational input, so it must not be converted into a fake command or
    rejected merely because it is not a task request.
    """
    from AstralNexus.runtime.llm_client import LLMClient
    from AstralNexus.tools.registry import discover_builtin_tools, registry
    from AstralNexus.tools.tool_router import select_tools

    if not chat_history:
        chat_history.append(
            {
                "role": "system",
                "content": (
                    "你是天枢 AstralNexus 的交互式 Agent。你必须根据用户请求决定下一步："
                    "直接回答、读取或搜索工作区文件、调用工具、修改文件、运行验证，或委派后台任务。"
                    f"当前真实工作区是：{Path.cwd().resolve()}。"
                    "你已获得当前启用的真实工具契约；工具调用结果是唯一事实来源。"
                    "当用户要求阅读、审查、分析或修改代码时，先用 search_files 发现实际路径，"
                    "再用 file_read 读取真实内容；不得凭空声称没有工作区、没有工具或无法访问。"
                    "除非用户明确要求远程仓库，否则不要调用 github_repo_contents。"
                    "当用户要求在网上、公开渠道查找公告、招标、中标、采购、新闻或其他外部资料时，"
                    "必须优先调用 web_search，再按搜索结果调用 http_get 或 browser_fetch；"
                    "search_files 只能搜索本地工作区，绝不能用它代替互联网检索。"
                    "不要伪称自己是其他模型或产品；你是 AstralNexus 的运行时 Agent。"
                    "若工具返回错误，必须原样说明错误并尝试合理替代路径，不得编造已读取的内容。"
                ),
            }
        )
    chat_history.append({"role": "user", "content": user_input})
    try:
        if output is None:
            print("\n[天枢] 正在分析请求并选择下一步...", flush=True)
        discover_builtin_tools()
        from AstralNexus.tools.assembly import assemble_tools, capability_guidance
        tools = assemble_tools()
        guidance = capability_guidance(tools)
        if chat_history and chat_history[0].get("role") == "system" and guidance not in str(chat_history[0].get("content", "")):
            chat_history[0]["content"] = str(chat_history[0]["content"]) + guidance

        streamed_answer: StreamedAnswer = {"started": False, "text": []}

        def on_event(event: dict) -> None:
            if output is not None:
                # The TUI already renders every text delta.  Keep a local
                # completion marker so the final response is not written a
                # second time after the stream closes.
                if event.get("event_type") == "llm.text.delta":
                    streamed_answer["started"] = True
                    streamed_answer["text"].append(str(event.get("text", "")))
                output.event(event)
                return
            event_type = event.get("event_type", "")
            if event_type == "llm.stream.started":
                print(
                    f"[天枢] 正在连接 {event.get('model', '模型')}，工具数: {event.get('tool_count', len(tools))}...",
                    flush=True,
                )
            elif event_type == "llm.text.delta":
                if not streamed_answer["started"]:
                    print("\n天枢: ", end="", flush=True)
                    streamed_answer["started"] = True
                text = str(event.get("text", ""))
                streamed_answer["text"].append(text)
                print(text, end="", flush=True)
            elif event_type == "llm.request":
                print(
                    f"[天枢] 正在调用模型，工具数: {len(tools)}，"
                    f"第 {event.get('request_index', 1)} 轮",
                    flush=True,
                )
            elif event_type == "llm.tool_batch":
                calls = [item.get("name", "unknown") for item in event.get("calls", [])]
                if calls:
                    print(f"\n[天枢] LLM 调用工具: {', '.join(calls)}", flush=True)
            elif event_type == "llm.response":
                print("[天枢] 已收到模型响应。", flush=True)

        client = LLMClient(
            interactive=False,
            workspace_root=Path.cwd(),
            event_callback=on_event,
        )
        answer = client.stream_chat(
            chat_history,
            tools=tools,
            workspace_root=Path.cwd(),
            max_tool_rounds=None,
            cancel_check=(getattr(output, "cancel_requested", None) if output is not None else None),
            event_callback=on_event,
        )
        if answer and answer.strip():
            if streamed_answer["started"]:
                if output is None:
                    print("\n", flush=True)
            else:
                if output is not None:
                    output.write_response(answer)
                else:
                    print("\n天枢: ", end="", flush=True)
                    print(answer, flush=True)
            chat_history.append({"role": "assistant", "content": answer})
        else:
            stats = getattr(client, "last_request_stats", {})
            print(
                "\n[天枢] 模型没有返回文本。"
                f" 请求统计: {json.dumps(stats, ensure_ascii=False)[:1000]}",
                file=sys.stderr,
                flush=True,
            )
            chat_history.pop()
    except Exception as exc:
        chat_history.pop()
        if output is not None:
            output.write(f"聊天调用失败: {exc}", error=True)
        else:
            print(f"聊天调用失败: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
