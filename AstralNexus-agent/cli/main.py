#!/usr/bin/env python3
"""Main CLI entry point for the AstralNexus system."""

from __future__ import annotations

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
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from AstralNexus.cli.commands import (
    ARGPARSE_COMMANDS,
    SESSION_COMMANDS,
    process_command_args,
    resolve_command,
)
from AstralNexus.core.identity import CODE_NAME, PRODUCT_NAME, bootstrap_environment
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.output_paths import RunOutputLayout
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
    temporary = pointer.with_name(pointer.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(pointer)


def _stage_task_inputs(task_file: Path, run_dir: Path) -> list[str]:
    """Copy task-local inputs into the isolated run workspace.

    Workers must be able to read task data while all writes remain inside the
    run directory.  Staging keeps that boundary explicit and makes a run
    reproducible without hard-coding any domain or filename.
    """
    input_root = run_dir / "inputs"
    input_root.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    task_copy = input_root / task_file.name
    shutil.copy2(task_file, task_copy)
    staged.append(str(task_copy.relative_to(run_dir)))
    # A task directory is a bounded input bundle.  Patent, coding, and data
    # tasks often keep source documents beside the task prompt rather than in
    # a separate ``data`` directory.  Stage those files too, so workers never
    # need to rediscover them from the source tree or outside the run root.
    bundle_root = task_file.parent
    bundle_destination = input_root / "task_bundle"
    for source in sorted(path for path in bundle_root.rglob("*") if path.is_file()):
        if source.resolve() == task_file.resolve():
            continue
        relative_source = source.relative_to(bundle_root)
        if relative_source.parts and relative_source.parts[0].lower() == "data":
            continue
        # Avoid copying generated run/output folders accidentally included in
        # a task bundle.
        if any(part in {".astralnexus", ".astralnexus_deploy", "__pycache__"} for part in source.parts):
            continue
        destination = bundle_destination / relative_source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged.append(str(destination.relative_to(run_dir)))
    data_root = task_file.parent / "data"
    if data_root.exists():
        for source in sorted(path for path in data_root.rglob("*") if path.is_file()):
            destination = input_root / "data" / source.relative_to(data_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            staged.append(str(destination.relative_to(run_dir)))
    return staged


def _delivery_file_extension(output: dict) -> str:
    """Choose a file extension from the Governor output contract."""
    name = str(output.get("name", "")).lower()
    output_type = str(output.get("type", "text")).lower()
    for marker, extension in (
        (".json", ".json"),
        ("json", ".json"),
        (".csv", ".csv"),
        ("csv", ".csv"),
        (".html", ".html"),
        ("html", ".html"),
        (".py", ".py"),
        ("python", ".py"),
        ("javascript", ".js"),
        ("typescript", ".ts"),
        (".md", ".md"),
        ("markdown", ".md"),
    ):
        if marker in name or marker in output_type:
            return extension
    return ".md" if output_type in {"text", "markdown", "report"} else ".bin"


def _contract_output_relative_path(output: dict, index: int) -> Path:
    """Resolve a contract name while preserving a declared directory layout.

    ``src/calculator.py`` is a real relative artifact path, not a display
    label.  Older code sanitized the slash and then appended ``.py`` again,
    producing ``src_calculator.py.py`` and breaking the Delivery Gate.
    """
    raw = str(output.get("name") or output.get("output_id") or f"output_{index:02d}").strip()
    raw = raw.replace("\\", "/")
    parts: list[str] = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(f"contract output path escapes delivery root: {raw}")
        part = re.sub(r"[<>:\\|?*]", "_", part).strip(" ._")
        if part:
            parts.append(part)
    if not parts:
        parts = [f"output_{index:02d}"]
    relative = Path(*parts)
    extension = _delivery_file_extension(output)
    if relative.suffix.lower() != extension.lower():
        relative = relative.with_name(relative.name + extension)
    return relative


def _materialize_tool_contract_outputs(
    *,
    required_outputs: list,
    delivery_dir: Path,
    artifact_paths: list[str],
) -> tuple[list[str], list[str]]:
    """Bind files actually written by final ``file_write`` calls.

    Tool artifacts are authoritative only when their relative path exactly
    matches the declared contract path.  Flattened or renamed files are not
    silently accepted as a different artifact.
    """
    outputs = [
        item for item in required_outputs
        if isinstance(item, dict) and item.get("required", True)
    ]
    root = delivery_dir.resolve()
    available: set[str] = set()
    for raw_path in artifact_paths:
        path = Path(str(raw_path))
        try:
            relative = path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file():
            available.add(relative.as_posix().casefold())

    refs: list[str] = []
    missing: list[str] = []
    for index, output in enumerate(outputs, 1):
        relative = _contract_output_relative_path(output, index)
        key = relative.as_posix().casefold()
        candidate = root / relative
        if key not in available or not candidate.is_file():
            missing.append(str(output.get("name", "")))
            continue
        refs.append("deliverables/" + relative.as_posix())
    return refs, missing


def _stage_declared_delivery_directories(
    *,
    run_dir: Path,
    staging_dir: Path,
    required_outputs: list,
    required_sections: list,
) -> list[str]:
    """Stage real directory/file outputs declared by a task contract.

    Workers may write artifacts in the run workspace while the final
    synthesizer writes its response in ``.delivery_staging``.  Directory
    contracts such as ``deliverables/app/`` must be copied into the same
    atomic staging area before the Delivery Gate evaluates them.
    """
    contract_text = "\n".join(
        [str(item) for item in required_sections]
        + [
            str(output.get("name", "")) + " "
            + " ".join(str(item) for item in output.get("acceptance_criteria", []) or [])
            for output in required_outputs
            if isinstance(output, dict)
        ]
    )
    declared: set[str] = set()
    for match in re.finditer(
        # Capture a complete relative path, including nested directory
        # contracts.  Keep the boundary explicit so prose punctuation is not
        # copied into the filesystem path.
        r"deliverables/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)(?:/)?(?=目录(?:下)?|\s|$|[，。；;）)])",
        contract_text,
        flags=re.IGNORECASE,
    ):
        relative = match.group(1).replace("\\", "/").rstrip("/")
        if relative:
            declared.add(relative)
    refs: list[str] = []
    for relative_text in sorted(declared):
        relative = Path(*[part for part in relative_text.split("/") if part])
        source = run_dir / "deliverables" / relative
        target = staging_dir / relative
        try:
            source.resolve().relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            continue
        for file in target.rglob("*") if target.is_dir() else [target]:
            if file.is_file():
                refs.append("deliverables/" + file.relative_to(staging_dir).as_posix())
    return list(dict.fromkeys(refs))


def _normalize_delivery_heading(value: str) -> str:
    """Normalize contract and model headings without knowing the domain."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^\s*(?:\d+[.)、:]|[一二三四五六七八九十]+[、.)])\s*", "", text)
    text = re.sub(
        r"[（(]\s*(?:markdown|md|text|json|csv|html|文件|报告)\s*[）)]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _materialize_contract_outputs(
    *,
    response: str,
    required_outputs: list,
    delivery_dir: Path,
) -> tuple[list[str], list[str]]:
    """Split a multi-artifact response according to the runtime contract.

    The final model may return one readable Markdown package, but a contract
    can require independently consumable files.  This splitter is driven only
    by ``required_outputs`` and top-level headings; it contains no task-domain
    section names.  A single-output contract keeps the historical behavior.
    """
    outputs = [item for item in required_outputs if isinstance(item, dict) and item.get("required", True)]
    if not outputs:
        return [], []
    headings = list(re.finditer(r"^(#{1,3})\s+(.+?)\s*$", response, flags=re.MULTILINE))

    def heading_text(match: re.Match) -> str:
        return match.group(2).strip()

    output_matches: dict[int, re.Match] = {}
    for index, output in enumerate(outputs, 1):
        expected_normalized = _normalize_delivery_heading(str(output.get("name", "")))
        match = next(
            (
                item for item in headings
                if (
                    _normalize_delivery_heading(heading_text(item)) == expected_normalized
                    or _normalize_delivery_heading(heading_text(item)).startswith(expected_normalized)
                    or expected_normalized.startswith(_normalize_delivery_heading(heading_text(item)))
                )
            ),
            None,
        )
        if match is not None:
            output_matches[index] = match

    output_heading_starts = {match.start() for match in output_matches.values()}

    def block_for(match: re.Match) -> str:
        level = len(match.group(1))
        start = match.start()
        end = len(response)
        for next_heading in headings:
            if next_heading.start() <= start:
                continue
            # Only another required output starts a new artifact.  Internal
            # sections may use the same Markdown level and must stay with
            # their parent output.
            if next_heading.start() in output_heading_starts:
                end = next_heading.start()
                break
        return response[start:end].strip() + "\n"

    blocks: dict[int, str] = {}
    for index, output in enumerate(outputs, 1):
        expected = str(output.get("name", "")).strip()
        expected_normalized = _normalize_delivery_heading(expected)
        match = output_matches.get(index)
        if match is not None:
            blocks[index] = block_for(match)

    # Models sometimes omit an outer wrapper heading for the first aggregate
    # deliverable and begin directly with its internal sections.  If later
    # contract outputs were identified, the prefix before the first identified
    # output is an unambiguous candidate for that first aggregate output.  This
    # remains domain-neutral and never duplicates the whole response.
    if 1 not in blocks and blocks:
        first_matched_start = min(
            match.start()
            for index, output in enumerate(outputs, 1)
            for match in headings
            if index in blocks
            and (
                _normalize_delivery_heading(heading_text(match)) == _normalize_delivery_heading(str(output.get("name", "")))
                or _normalize_delivery_heading(heading_text(match)).startswith(
                    _normalize_delivery_heading(str(output.get("name", "")))
                )
            )
        )
        prefix = response[:first_matched_start].strip()
        if prefix:
            blocks[1] = prefix + "\n"

    # A single textual output is intentionally the whole response.  With
    # multiple outputs, silently duplicating the whole response would make a
    # false artifact look valid, so missing headings remain explicit.
    if len(outputs) == 1 and 1 not in blocks:
        blocks[1] = response.strip() + "\n"

    refs: list[str] = []
    missing: list[str] = []
    for index, output in enumerate(outputs, 1):
        content = blocks.get(index)
        match = output_matches.get(index)
        name = str(output.get("name") or output.get("output_id") or f"output_{index:02d}")
        if not content:
            missing.append(name)
            continue
        # A heading-only block is not an independently usable deliverable.
        # Keep the response in draft state so the final LLM quality retry can
        # fill the missing content instead of publishing a false artifact.
        if match is not None and len(content.strip()) <= len(heading_text(match).strip()) + 3:
            missing.append(name)
            continue
        relative = _contract_output_relative_path(output, index)
        path = delivery_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        refs.append(f"deliverables/{relative.as_posix()}")
    return refs, missing


def _missing_contract_output_names(response: str, required_outputs: list) -> list[str]:
    """Return required output names not represented by a top-level heading.

    Multi-output delivery is intentionally heading-driven so a single LLM
    response can become independently addressable files without duplicating
    the entire response into every artifact.  This helper is used before the
    final write, allowing the normal quality retry to repair omissions.
    """
    outputs = [
        item for item in required_outputs
        if isinstance(item, dict) and item.get("required", True)
    ]
    if len(outputs) <= 1:
        return []
    headings = [
        _normalize_delivery_heading(match.group(2))
        for match in re.finditer(r"^(#{1,3})\s+(.+?)\s*$", response or "", flags=re.MULTILINE)
    ]
    missing = []
    for output in outputs:
        expected = _normalize_delivery_heading(str(output.get("name", "")))
        if not expected:
            continue
        if not any(
            heading == expected
            or heading.startswith(expected)
            or expected.startswith(heading)
            for heading in headings
        ):
            missing.append(str(output.get("name", "")))
    return missing


def _unsupported_forbidden_claims(response: str, forbidden_markers: list[str]) -> list[str]:
    """Detect unsupported assertions without rejecting honest limitations.

    Governor forbidden claims are policy descriptions, not literal banned
    words. A final answer may legitimately say that a test, deployment, or
    evidence item is *not* complete. Treat a marker as a violation only when
    it appears without nearby negation or uncertainty language.
    """
    text = str(response or "")
    negations = (
        "未", "尚未", "尚无", "没有", "无", "缺少", "不足", "无法", "不能",
        "不得", "不应", "未能", "待", "计划", "假设", "如果", "仅建议",
        "not", "without", "pending", "planned", "not yet",
    )
    violations: list[str] = []
    for marker in forbidden_markers:
        marker = str(marker or "").strip()
        if not marker or marker not in text:
            continue
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            context = text[max(0, index - 80):index]
            if not any(token in context for token in negations):
                violations.append(marker)
                break
            start = index + len(marker)
    return violations


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


class _InteractiveRunController:
    """Own the one active background run for the interactive TUI.

    The controller never edits a TaskGraph.  It only exposes a cancellation
    token and a queue; the Orchestrator drains that queue at safe boundaries
    and sends requirement changes through Governor.
    """

    def __init__(self, home: Path):
        self.home = home
        self.interrupt_event = threading.Event()
        self.interrupt_queue: queue.Queue = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.last_result: Optional[int] = None
        self.run_dir: Optional[Path] = None
        self.event_callback = None
        self._lock = threading.Lock()
        self._route_governor = None

    def set_event_callback(self, callback) -> None:
        self.event_callback = callback

    def dispatch_command(
        self,
        command_line: str,
        *,
        history: Optional[list[dict]] = None,
    ) -> dict:
        """Handle commands that operate on the active interactive runtime.

        Frontends must delegate here instead of implementing their own
        background-run semantics. Commands unrelated to the active runtime are
        returned as ``handled=False`` for the canonical command registry.
        """
        from AstralNexus.cli.commands import resolve_command, split_command_line

        try:
            parts = split_command_line(command_line)
        except ValueError as exc:
            return {"handled": True, "ok": False, "code": 1, "output": str(exc)}
        if not parts:
            return {"handled": True, "ok": True, "code": 0, "output": ""}

        canonical = resolve_command(parts[0].lstrip("/").lower())
        if canonical is None:
            return {
                "handled": True,
                "ok": False,
                "code": 1,
                "output": f"未知命令: {parts[0]}",
            }
        if canonical not in SESSION_COMMANDS:
            return {"handled": False, "canonical": canonical}
        arguments = parts[1:]
        if canonical == "quit":
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "exit": True,
                "output": "正在退出天枢。",
            }
        if canonical == "run":
            if not arguments:
                return {"handled": True, "ok": False, "code": 1, "output": "用法: /run <task-file>"}
            started = self.start(arguments[0])
            return {
                "handled": True,
                "ok": started,
                "code": 0 if started else 1,
                "output": (
                    f"已在后台启动正式任务: {arguments[0]}"
                    if started else "任务未启动；已有任务运行或任务参数无效。"
                ),
            }
        if canonical == "resume":
            if not arguments:
                return {
                    "handled": True,
                    "ok": False,
                    "code": 1,
                    "output": "用法: /resume <run-id|run-dir> [task-file]",
                }
            started = self.start_resume(
                arguments[0],
                task=arguments[1] if len(arguments) > 1 else None,
            )
            return {
                "handled": True,
                "ok": started,
                "code": 0 if started else 1,
                "output": (
                    f"已在后台恢复运行: {arguments[0]}"
                    if started else f"无法恢复运行: {arguments[0]}"
                ),
            }
        if canonical == "new":
            goal = " ".join(arguments).strip()
            if not goal:
                return {"handled": True, "ok": False, "code": 1, "output": "用法: /new <任务目标>"}
            started = self.start_text(goal, history=history)
            return {
                "handled": True,
                "ok": started,
                "code": 0 if started else 1,
                "output": "已创建新任务并交给 Governor。" if started else "新任务未启动。",
            }
        if canonical == "interrupt":
            self.interrupt(" ".join(arguments).strip() or "user_interrupt")
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "output": "已请求协作式中断并保存 checkpoint。",
            }
        if canonical == "status":
            status = self.status()
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "status": status,
                "output": (
                    f"后台任务: {'运行中' if status['running'] else '空闲'}\n"
                    f"运行目录: {status.get('run_dir') or '(无)'}\n"
                    f"待处理注入: {status.get('queued_interrupts', 0)}\n"
                    f"最近退出码: {status.get('last_result')}"
                ),
            }
        if canonical == "inject-requirement" and self.status()["running"]:
            if not arguments:
                return {
                    "handled": True,
                    "ok": False,
                    "code": 1,
                    "output": "用法: /inject-requirement [run-id] <change.json>",
                }
            try:
                self.inject_requirement(arguments[-1])
            except Exception as exc:
                return {
                    "handled": True,
                    "ok": False,
                    "code": 1,
                    "output": f"需求变更注入失败: {exc}",
                }
            return {
                "handled": True,
                "ok": True,
                "code": 0,
                "output": "需求变更已进入 Governor 队列。",
            }
        return {"handled": False, "canonical": canonical}

    def _emit(self, event_type: str, text: str, **payload) -> None:
        event = {"event_type": event_type, "text": text, **payload}
        if self.event_callback is not None:
            self.event_callback(event)
        else:
            print(text, flush=True)

    def decide_route(self, text: str, history: list[dict]) -> object:
        """Ask the real-LLM Governor how an ordinary turn should execute."""
        from AstralNexus.core.governor import GlobalGovernor
        from AstralNexus.core.task_graph import TaskGraph
        from AstralNexus.runtime.llm_client import LLMClient
        from AstralNexus.runtime.trace import TraceRecorder

        if self._route_governor is None:
            control_dir = self.home / "interactive_control"
            trace = TraceRecorder(
                control_dir,
                default_node_id="interactive",
                event_callback=self.event_callback,
            )
            self._route_governor = GlobalGovernor(
                TaskGraph(),
                trace,
                llm_client=LLMClient(interactive=False, workspace_root=Path.cwd()),
            )
        else:
            self._route_governor.trace_recorder.event_callback = self.event_callback
        recent = "\n".join(
            f"{item.get('role')}: {str(item.get('content') or '')[:1000]}"
            for item in history[-8:]
            if item.get("role") in {"user", "assistant"}
        )
        return self._route_governor.decide_interactive_route(
            text,
            conversation_context=recent,
            active_run=self.status()["running"],
        )

    def start_text(
        self,
        text: str,
        *,
        history: Optional[list[dict]] = None,
        output: Optional[str] = None,
        route_decision: Optional[dict] = None,
    ) -> bool:
        """Materialize a conversational request as a reproducible task contract."""
        task_dir = self.home / "interactive_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_path = task_dir / f"task-{int(time.time() * 1000)}.txt"
        context_lines = []
        for item in (history or [])[-8:]:
            if item.get("role") in {"user", "assistant"}:
                context_lines.append(
                    f"{item.get('role')}: {str(item.get('content') or '')[:2000]}"
                )
        # The task file is the immutable user intent. Conversation context and
        # routing evidence are audit metadata, not hidden task requirements.
        task_path.write_text(text.strip(), encoding="utf-8")
        metadata = {
            "user_intent": text.strip(),
            "conversation_context": context_lines,
            "route_decision": route_decision or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        task_path.with_suffix(".context.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.start(str(task_path), output=output)

    def _bind(self, orchestrator, run_dir: Path) -> None:
        with self._lock:
            self.run_dir = run_dir
        orchestrator.attach_interrupt_control(self.interrupt_event, self.interrupt_queue)

    def start(self, task: str, *, output: Optional[str] = None) -> bool:
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                self._emit(
                    "interactive.run.rejected",
                    "已有任务正在运行；新需求应作为 TaskGraph Patch 注入当前任务。",
                )
                return False
            self.interrupt_event.clear()
            while True:
                try:
                    self.interrupt_queue.get_nowait()
                except queue.Empty:
                    break
            args = argparse.Namespace(
                task=task, output=output, config=None, model=None,
                provider=None, max_parallel=None,
            )
            self.last_result = None
            self.thread = threading.Thread(
                target=self._run,
                args=(args,),
                name="astralnexus-orchestrator",
                daemon=False,
            )
            self.thread.start()
        self._emit(
            "interactive.run.started",
            "Governor 已启动动态异构任务图；可继续输入新需求或使用 /status。",
            task=task,
            output=output,
        )
        return True

    def start_resume(self, run_dir: str, *, task: Optional[str] = None) -> bool:
        candidate = Path(run_dir).expanduser()
        if not candidate.is_dir():
            from AstralNexus.cli.commands import _resolve_run_dir
            resolved = _resolve_run_dir(run_dir, self.home)
            if resolved is None:
                print(f"找不到可恢复运行: {run_dir}")
                return False
            candidate = resolved
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                self._emit("interactive.run.rejected", "已有任务正在运行，请先中断或等待完成。")
                return False
            self.interrupt_event.clear()
            args = argparse.Namespace(
                run_dir=str(candidate), task=task, config=None, model=None,
                provider=None, max_parallel=None,
            )
            self.last_result = None
            self.thread = threading.Thread(
                target=self._resume,
                args=(args,),
                name="astralnexus-resume",
                daemon=False,
            )
            self.thread.start()
        self._emit(
            "interactive.run.resumed",
            "已在后台恢复天枢 Orchestrator；可继续输入新需求。",
            run_dir=str(candidate),
        )
        return True

    def _resume(self, args) -> None:
        try:
            self.last_result = _resume_task(
                args, self.home,
                interrupt_event=self.interrupt_event,
                interrupt_queue=self.interrupt_queue,
                on_orchestrator=self._bind,
                event_callback=self.event_callback,
            )
        finally:
            self._emit(
                "interactive.run.finished",
                f"后台恢复线程结束，退出码: {self.last_result}",
                result=self.last_result,
                run_dir=str(self.run_dir) if self.run_dir else None,
            )

    def _run(self, args) -> None:
        try:
            self.last_result = _run_task(
                args, self.home,
                interrupt_event=self.interrupt_event,
                interrupt_queue=self.interrupt_queue,
                on_orchestrator=self._bind,
                event_callback=self.event_callback,
            )
        finally:
            self._emit(
                "interactive.run.finished",
                f"后台任务线程结束，退出码: {self.last_result}",
                result=self.last_result,
                run_dir=str(self.run_dir) if self.run_dir else None,
            )

    def status(self) -> dict:
        with self._lock:
            thread = self.thread
            return {
                "running": bool(thread and thread.is_alive()),
                "run_dir": str(self.run_dir) if self.run_dir else None,
                "queued_interrupts": self.interrupt_queue.qsize(),
                "last_result": self.last_result,
            }

    def interrupt(self, message: str = "user_interrupt") -> None:
        self.interrupt_queue.put({"type": "interrupt", "message": message})
        self.interrupt_event.set()
        self._emit(
            "interactive.run.interrupt_requested",
            "已请求协作式中断；当前网络请求会在响应或重试边界退出并保存 checkpoint。",
        )

    def inject_requirement_text(self, description: str) -> None:
        self.interrupt_queue.put({
            "type": "requirement_change",
            "description": description,
            "affected_constraints": [],
            "payload": {"source": "interactive_governor"},
        })
        self.interrupt_event.set()
        self._emit(
            "interactive.requirement_change.queued",
            "新需求已进入 Governor 队列，将在安全边界生成 TaskGraph Patch。",
            description=description[:1000],
        )

    def inject_requirement(self, payload_path: str) -> None:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("需求变更文件必须是 JSON 对象")
        self.interrupt_queue.put({
            "type": "requirement_change",
            "description": payload.get("description") or payload.get("change") or json.dumps(payload, ensure_ascii=False),
            "affected_constraints": payload.get("affected_constraints", []),
            "change_kind": payload.get("change_kind"),
            "payload": payload,
        })
        self.interrupt_event.set()
        self._emit(
            "interactive.requirement_change.queued",
            "需求变更已进入队列，将由 Governor 在安全边界生成并应用 TaskGraph Patch。",
        )


def _resolve_interactive_task_reference(text: str) -> Optional[Path]:
    """Resolve a task file or bounded task bundle mentioned in a turn."""
    candidates = [
        Path(token.rstrip("'\"`"))
        for token in re.findall(r"[A-Za-z]:\\[^\s，。；;]+", text)
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


def _govern_interactive_turn(
    line: str,
    chat_history: list[dict],
    controller: _InteractiveRunController,
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

    if decision.mode == "direct_chat":
        return False

    chat_history.append({"role": "user", "content": line})
    if decision.mode == "requirement_change":
        controller.inject_requirement_text(line)
        response = "Governor 已将该输入识别为当前任务的需求变更，正在生成并应用增量任务图 Patch。"
    else:
        task_file = _resolve_interactive_task_reference(line)
        output_dir = _resolve_interactive_output_reference(line)
        if task_file is not None:
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
    try:
        # Read task file
        task_file = Path(args.task)
        if not task_file.exists():
            _runtime_notice(
                event_callback,
                "interactive.run.error",
                f"错误: 任务文件不存在: {task_file}",
            )
            return 1
        
        task_content = task_file.read_text(encoding="utf-8")
        
        # Setup run directory
        run_dir = Path(args.output) if args.output else home / "runs" / f"task_{task_file.stem}"
        run_dir.mkdir(parents=True, exist_ok=True)
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
        os.environ["ASTRALNEXUS_WORKSPACE"] = str(run_dir.resolve())
        
        # Create SQLite store for persistence
        graph_store_path = run_dir / "task_graph.sqlite"
        graph_store = SQLiteTaskGraphStore(graph_store_path)
        
        # Create and run orchestrator (enters main chain: TaskGraph → Router → Agents → StructuredMessage)
        max_parallel = args.max_parallel or int(os.environ.get("ASTRALNEXUS_MAX_PARALLEL", "4"))
        with Orchestrator(
            run_dir=run_dir,
            # Formal runs are evidence-driven.  No wall-clock or iteration
            # cap is applied; transport-level request timeouts still protect
            # individual sockets from hanging forever.
            max_iterations=0,
            graph_store=graph_store,
            max_parallel=max(1, max_parallel),
            event_callback=runtime_event_callback,
        ) as orchestrator:
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
            
            # Execute through orchestrator main chain
            result = orchestrator.run(goal=goal, constraints=constraints)

            # The execution report is an audit artifact, not the user-facing
            # answer.  Produce the requested domain deliverables through a final
            # real-LLM synthesis pass over the completed node outputs.
            delivery = None
            if result.get("status") == "completed":
                delivery = _generate_task_delivery(
                    task_content=task_content,
                    run_dir=run_dir,
                    orchestrator=orchestrator,
                    model_config=config,
                )
            
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
                final_summary["summary"],
                result=dict(result),
            )
            _runtime_notice(
                runtime_event_callback,
                "interactive.run.artifact",
                f"最终报告: {result['final_report']}",
                path=str(result["final_report"]),
            )
            if delivery:
                for label, key in (
                    ("主交付", "main_markdown"),
                    ("机器摘要", "summary_json"),
                    ("验收说明", "acceptance_report"),
                ):
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
            
            progress_reporter.close(final_status=result["status"])
            return 0 if result['status'] == "completed" else 1
    except Exception as e:
        _runtime_notice(
            progress_reporter.handle_event if progress_reporter is not None else event_callback,
            "interactive.run.error",
            f"错误: 执行任务时发生异常: {e}",
        )
        import traceback
        traceback.print_exc()
        return 1
    finally:
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
    runtime_state_path = run_dir / "runtime_state.json"
    graph_path = run_dir / "task_graph.sqlite"
    if not runtime_state_path.exists() or not graph_path.exists():
        _runtime_notice(
            event_callback,
            "interactive.resume.error",
            f"错误: 恢复需要 {runtime_state_path.name} 和 {graph_path.name}",
            run_dir=str(run_dir),
        )
        return 1
    try:
        from AstralNexus.cli.progress_reporter import RunProgressReporter

        progress_reporter = RunProgressReporter(
            run_dir=str(run_dir),
            sink=event_callback,
        )
        runtime_event_callback = progress_reporter.handle_event
        state_payload = json.loads(runtime_state_path.read_text(encoding="utf-8"))
        persisted_run_id = str(state_payload.get("run_id") or "").strip()
        if not persisted_run_id:
            raise ValueError("runtime_state.json is missing run_id")
        store = SQLiteTaskGraphStore(graph_path)
        max_parallel = args.max_parallel or int(os.environ.get("ASTRALNEXUS_MAX_PARALLEL", "4"))
        with Orchestrator(
            run_dir=run_dir,
            graph_store=store,
            run_id=persisted_run_id,
            max_iterations=0,
            max_parallel=max(1, max_parallel),
            event_callback=runtime_event_callback,
        ) as orchestrator:
            if interrupt_event is not None and interrupt_queue is not None:
                orchestrator.attach_interrupt_control(interrupt_event, interrupt_queue)
            if on_orchestrator is not None:
                on_orchestrator(orchestrator, run_dir)
            if not orchestrator.restore_checkpoint():
                return 1
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
            result = orchestrator.run(goal=goal)
            _runtime_notice(
                runtime_event_callback,
                "interactive.resume.completed",
                f"任务执行状态: {result.get('status')}",
                run_dir=str(run_dir),
                status=result.get("status"),
                final_report=result.get("final_report"),
            )
            if args.task:
                task_file = Path(args.task)
                task_content = task_file.read_text(encoding="utf-8")
                delivery = _generate_task_delivery(
                    task_content=task_content,
                    run_dir=run_dir,
                    orchestrator=orchestrator,
                    model_config=config,
                )
                _runtime_notice(
                    runtime_event_callback,
                    "interactive.resume.delivery",
                    f"主交付: {delivery['main_markdown']}",
                    run_dir=str(run_dir),
                    main_markdown=delivery["main_markdown"],
                    summary_json=delivery["summary_json"],
                    acceptance_report=delivery["acceptance_report"],
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
        return 1
    finally:
        if progress_reporter is not None:
            progress_reporter.close()


def _replan_from_global_verifier(
    *,
    task_content: str,
    run_dir: Path,
    orchestrator: Orchestrator,
    client,
    verifier: dict,
    attempt: int,
) -> bool:
    """Turn verifier coverage gaps into one bounded Governor graph patch.

    The verifier remains a hard gate.  This helper does not ask the final
    synthesizer to invent missing content; it asks Governor for the smallest
    affected subgraph, applies it atomically, and lets the normal Orchestrator
    ready-wave machinery execute the new nodes.
    """
    gaps = [
        dict(item)
        for item in verifier.get("coverage_gaps", [])
        if isinstance(item, dict) and str(item.get("gap") or "").strip()
    ]
    if not gaps:
        return False

    graph_summary = json.dumps([
        {
            "node_id": node.node_id,
            "goal": node.goal,
            "status": node.status,
            "dependencies": list(node.dependencies),
            "required_capabilities": node.metadata.get("required_capabilities", []),
        }
        for node in orchestrator.task_graph.nodes.values()
        if node.status not in {"cancelled", "superseded"}
    ], ensure_ascii=False)
    from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS

    capability_catalog = json.dumps([
        {
            "worker_type": profile.worker_type,
            "capabilities": profile.capabilities,
            "tools": profile.tools,
            "resource_tier": profile.resource_tier,
            "input_contract": profile.input_contract,
            "output_contract": profile.output_contract,
        }
        for profile in HETEROGENEOUS_WORKERS.values()
    ], ensure_ascii=False)
    gap_goal = (
        f"{task_content}\n\nGlobal Verifier 当前覆盖缺口（只补这些缺口，禁止重建无关节点）：\n"
        f"{json.dumps(gaps, ensure_ascii=False)}"
    )
    patch = orchestrator.governor.plan_task_graph_patch(
        gap_goal,
        client,
        graph_summary=graph_summary,
        capability_catalog=capability_catalog,
        resource_catalog="使用当前 Governor 资源目录；只选择可用资源。",
    )
    if not patch.add_nodes and not patch.update_nodes:
        orchestrator.trace_recorder.record("delivery.gap_replan.empty", {
            "attempt": attempt,
            "coverage_gaps": gaps,
            "reason_codes": patch.reason_codes,
        })
        return False

    # A delivery-gap repair is a fresh evidence path.  If the LLM attaches
    # it to a node that has already entered terminal failure, the patch would
    # be admitted only to be blocked immediately by the canonical lifecycle.
    # Rewire that dependency at the Governor boundary and record the reason;
    # the repair node remains accountable to the verifier gap and its own
    # acceptance contract.  Other patch types still reject terminal
    # dependencies in ``apply_task_graph_patch``.
    terminal_ids = {
        node.node_id
        for node in orchestrator.task_graph.nodes.values()
        if node.metadata.get("_terminal_failure")
    }
    rewired = []
    if terminal_ids:
        for item in patch.add_nodes:
            if not isinstance(item, dict):
                continue
            deps = [str(dep) for dep in item.get("dependencies", [])]
            removed = [dep for dep in deps if dep in terminal_ids]
            if removed:
                item["dependencies"] = [dep for dep in deps if dep not in terminal_ids]
                metadata = dict(item.get("metadata") or {})
                metadata["rewired_terminal_dependencies"] = removed
                metadata["replan_reason"] = "terminal_dependency_rewired_for_independent_evidence_repair"
                item["metadata"] = metadata
                rewired.append({"node_id": item.get("node_id"), "removed": removed})
        patch.reason_codes = list(dict.fromkeys(list(patch.reason_codes) + (["TERMINAL_DEPENDENCY_REWIRED"] if rewired else [])))
        if rewired:
            orchestrator.trace_recorder.record("delivery.gap_replan.terminal_dependency_rewired", {
                "attempt": attempt,
                "rewired": rewired,
            })

    applied = orchestrator.governor.apply_task_graph_patch(patch)
    orchestrator.trace_recorder.record("delivery.gap_replan.applied", {
        "attempt": attempt,
        "coverage_gaps": gaps,
        "patch": patch.as_dict(),
        "applied": applied,
    })
    if not applied.get("added") and not applied.get("updated"):
        return False

    # Reuse the canonical execution loop.  Because the existing graph is
    # preserved, completed nodes are not rerun; only newly ready or repaired
    # nodes are selected by Router and executed in subsequent ready-waves.
    root_goal = task_content.splitlines()[0].strip() or task_content
    orchestrator.run(goal=root_goal, constraints=[])
    return bool(applied.get("added") or applied.get("updated"))


def _generate_task_delivery(
    *,
    task_content: str,
    run_dir: Path,
    orchestrator: Orchestrator,
    model_config: Optional[dict] = None,
) -> dict[str, str]:
    """Synthesize the actual user-facing deliverables with the real LLM.

    ``final_report.md`` remains an execution audit.  This function creates
    the domain answer separately so a successful orchestration cannot be
    mistaken for a successful user-facing delivery.
    """
    from AstralNexus.runtime.llm_client import LLMClient
    from AstralNexus.runtime.model_selector import get_model_config
    from AstralNexus.core.layered_synthesis import build_layered_context

    layout = RunOutputLayout(run_dir)
    layout.ensure_runtime_dirs()
    # All candidate deliverables remain outside the final directory until
    # Delivery Gate authorizes publication.
    layout.begin_delivery_draft()
    config = model_config or get_model_config()
    synthesis_model = os.getenv("ASTRALNEXUS_SYNTHESIS_MODEL", "").strip() or config.get("model") or "qwen3.7-max"
    synthesis_timeout = max(30, int(os.getenv("ASTRALNEXUS_SYNTHESIS_TIMEOUT", "300")))
    client = LLMClient(
        model=synthesis_model,
        base_url=config.get("base_url"),
        timeout=synthesis_timeout,
        interactive=False,
        event_callback=lambda event: orchestrator.trace_recorder.record(
            event.get("event_type", "llm.event"), event
        ),
    )
    client.max_retries = max(0, int(os.getenv("ASTRALNEXUS_FINAL_RETRIES", "1")))
    
    # Build layered context. A failed Global Verifier triggers a Governor
    # repair cycle. There is no retry-count cap: termination is evidence-driven
    # and occurs only on verifier pass, no executable patch, or repeated gaps.
    # Do not stop on the first repeated gap. A repair may need to produce the
    # same semantic gap while changing its evidence or graph. Exact
    # fingerprints alone are insufficient because an LLM can paraphrase or
    # regroup the same missing evidence on every replan.
    gap_fingerprint_counts: dict[str, int] = {}
    previous_gap_obligations: set[str] = set()
    stagnant_gap_streak = 0
    layered = None
    replan_attempt = 0
    while True:
        try:
            layered = build_layered_context(
                task_content=task_content,
                run_dir=run_dir,
                client=client,
                governor=orchestrator.governor,
                reducer_workers=int(os.getenv("ASTRALNEXUS_REDUCER_WORKERS", "4")),
            )
            break
        except RuntimeError as e:
            if "No successful node outputs available" in str(e):
                logger.warning("No successful node outputs for layered synthesis; generating failure report")
                return _generate_failure_report(
                    task_content=task_content,
                    run_dir=run_dir,
                    orchestrator=orchestrator,
                    reason="capability_gap_or_execution_failure",
                    detail=str(e),
                )
            if "Global Verifier rejected reducer evidence" not in str(e):
                raise
            verifier_path = RunOutputLayout(run_dir).layered_dir / "global_verifier.json"
            try:
                verifier = json.loads(verifier_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise
            # Detect structural gaps that replan cannot fix
            missing_outputs = verifier.get("missing_outputs", [])
            if "structured_reducer_summaries" in missing_outputs:
                logger.warning("Global Verifier missing structured reducer summaries (structural gap); generating failure report")
                return _generate_failure_report(
                    task_content=task_content,
                    run_dir=run_dir,
                    orchestrator=orchestrator,
                    reason="structural_gap_no_reducer_groups",
                    detail=f"Global Verifier missing_outputs: {missing_outputs}",
                )
            gap_items = [
                {
                    "group_id": item.get("group_id", "") if isinstance(item, dict) else "",
                    "gap": item.get("gap", "") if isinstance(item, dict) else str(item),
                    "evidence_needed": item.get("evidence_needed", "") if isinstance(item, dict) else "",
                }
                for item in verifier.get("coverage_gaps", [])
                if isinstance(item, dict) or str(item).strip()
            ]
            def normalize_gap(value: str) -> str:
                return re.sub(r"[^\u4e00-\u9fffA-Za-z]+", "", str(value)).lower()[:800]
            semantic_gap_text = "|".join(sorted(
                normalize_gap(f"{item.get('gap', '')}{item.get('evidence_needed', '')}")
                for item in gap_items
            ))
            gap_fingerprint = json.dumps(gap_items, ensure_ascii=False, sort_keys=True)
            # LLM wording may change while the evidence obligation remains
            # the same.  Treat highly similar obligations as one gap so the
            # three-repeat termination policy cannot be evaded by paraphrase.
            for known_key in list(gap_fingerprint_counts):
                try:
                    known_items = json.loads(known_key)
                    known_text = "|".join(sorted(
                        normalize_gap(f"{item.get('gap', '')}{item.get('evidence_needed', '')}")
                        for item in known_items
                    ))
                except (TypeError, ValueError, AttributeError):
                    continue
                if semantic_gap_text and (
                    semantic_gap_text in known_text
                    or known_text in semantic_gap_text
                    or difflib.SequenceMatcher(None, semantic_gap_text, known_text).ratio() >= 0.78
                ):
                    gap_fingerprint = known_key
                    break
            gap_fingerprint_counts[gap_fingerprint] = gap_fingerprint_counts.get(gap_fingerprint, 0) + 1
            repeated_count = gap_fingerprint_counts[gap_fingerprint]
            current_gap_obligations = {
                normalize_gap(
                    f"{item.get('group_id', '')}|"
                    f"{item.get('evidence_needed', '')}|"
                    f"{item.get('gap', '')}"
                )
                for item in gap_items
                if normalize_gap(
                    f"{item.get('group_id', '')}|"
                    f"{item.get('evidence_needed', '')}|"
                    f"{item.get('gap', '')}"
                )
            }
            if previous_gap_obligations and current_gap_obligations:
                overlap = len(previous_gap_obligations & current_gap_obligations)
                union = len(previous_gap_obligations | current_gap_obligations)
                obligation_similarity = overlap / union if union else 1.0
                stagnant_gap_streak = (
                    stagnant_gap_streak + 1
                    if obligation_similarity >= 0.45
                    else 0
                )
            else:
                stagnant_gap_streak = 0
            previous_gap_obligations = current_gap_obligations
            if repeated_count > 1 or stagnant_gap_streak > 1:
                # Repetition is diagnostic evidence, never a hard round
                # limit.  Keep asking Governor for a changed execution path;
                # only a verifier pass or a genuinely empty/invalid patch
                # can end this repair cycle.
                orchestrator.trace_recorder.record("delivery.gap_replan.no_progress", {
                    "attempt": replan_attempt,
                    "coverage_gap_fingerprint": gap_fingerprint,
                    "repeat_count": repeated_count,
                    "stagnant_gap_streak": stagnant_gap_streak,
                    "reason": "same_hard_evidence_gaps_require_changed_plan",
                })
            replan_attempt += 1
            orchestrator.trace_recorder.record("delivery.gap_replan.review", {
                "attempt": replan_attempt,
                "coverage_gap_count": len(verifier.get("coverage_gaps", [])),
                "decision": "necessary_hard_evidence_gap",
                "termination_policy": "pass_or_three_identical_gap_repeats_only",
            })
            if not _replan_from_global_verifier(
                task_content=task_content,
                run_dir=run_dir,
                orchestrator=orchestrator,
                client=client,
                verifier=verifier,
                attempt=replan_attempt,
            ):
                # Governor may correctly decide that no executable patch is
                # justified.  That is a terminal replan decision, not a
                # Python exception.  Preserve the failed verifier report and
                # let the final synthesizer disclose the remaining gaps;
                # Delivery Gate still decides whether publication is allowed.
                orchestrator.trace_recorder.record("delivery.gap_replan.no_patch", {
                    "attempt": replan_attempt,
                    "reason": "governor_returned_no_executable_patch",
                    "final_synthesizer_must_disclose_gaps": True,
                })
                allow_partial_evidence = True
                continue
    if layered is None:
        raise RuntimeError("Layered synthesis did not produce a result")
    reducer_context = "\n\n---\n\n".join(
        f"[{group['group_id']} | {group['title']}]\n{json.dumps(layered['reducers'].get(group['group_id'], {}), ensure_ascii=False)[:12000]}"
        for group in layered["groups"]
    )
    evidence = (
        f"L3 Governor 批准的大纲：\n{layered['outline'][:12000]}\n\n"
        f"主题 reducer 证据：\n{reducer_context}\n\n"
        f"Global Verifier 报告（最终模型必须遵守）：\n{json.dumps(layered.get('global_verifier', {}), ensure_ascii=False, indent=2)}\n\n"
        f"Governor 批准的工作集：\n{json.dumps([p['node_id'] for p in layered['approved_packets']], ensure_ascii=False)}\n\n"
        f"证据包与原始产物引用：\n{json.dumps(layered['artifact_refs'], ensure_ascii=False)}"
    )
    contract = layered["delivery_contract"]
    required_sections = [str(item) for item in contract.get("required_sections", [])]
    forbidden_markers = [str(item) for item in contract.get("forbidden_claims", [])]
    required_outputs = [
        item for item in contract.get("required_outputs", [])
        if isinstance(item, dict) and item.get("required", True)
    ]
    has_file_outputs = any(
        str(item.get("type", "")).lower() in {"file", "files", "software", "data"}
        for item in required_outputs
    )
    delivery_strategy = str(contract.get("delivery_strategy") or "model_decided")
    summary_required = bool(contract.get("summary_required", True))
    artifact_first = delivery_strategy == "artifact_first" or (
        delivery_strategy == "model_decided" and has_file_outputs and not summary_required
    )
    # Artifact-first tasks still receive a short model response when the
    # Governor requests one, but the response is an index, not the delivery
    # itself.  No task domain is inferred here.
    response_required = summary_required or not has_file_outputs
    prompt = f"""你是最终交付执行器。请根据用户任务、Governor 生成的交付契约和批准的分层证据，直接生成用户需要的实际交付物。

任务书：
{task_content}

交付契约：
{json.dumps(contract, ensure_ascii=False, indent=2)}

Governor 批准的分层证据：
{evidence}

交付规则：
1. 直接完成任务契约，不要输出运行报告、节点列表、角色名称或提示词，除非契约明确要求这些内容。
2. 严格遵守 required_outputs、required_sections、acceptance_criteria 和 delivery_mode。
3. 只能把批准证据支持的内容写成已知事实；对缺失证据、计划或推断明确标注状态，不得编造外部结果。
4. 保留必要的来源或 artifact 引用；不要默认把未批准的原始节点全文重新注入。
5. 输出应是可直接交付给用户的内容，而不是对本次编排过程的解释。
6. 严格遵守契约中的 execution_evidence_policy：plan_only 任务必须给出可追踪的负责人、时间、依赖、状态和验证方式，但不得声称外部行动已发生；runtime_required 或 mixed 任务必须提供实际运行证据或明确标注未完成。

交付决策：
- delivery_strategy={delivery_strategy}
- summary_required={summary_required}
- artifact_first={artifact_first}
如果 artifact_first=true，优先使用工具真实创建、修改、运行和验证产物；不要把代码、数据、图片或其他产物全部复制进长篇文本，也不要把任务改写成论文。
如果 summary_required=false，最终文字只需给出简短的真实产物索引和状态，甚至可以为空；交付是否成功由实际 Artifact 和运行证据决定。

【强制约束】以下章节必须作为一级或二级标题出现，标题文字必须与列表完全一致（不得改名、合并或省略）：
{chr(10).join(f"- {s}" for s in required_sections) if required_sections else "（无强制章节）"}

每个标题必须独立成行，格式为 "## 标题" 或 "# 标题"，不得嵌入其他文字。

【交付物边界】
{("对于文本摘要契约，按契约要求组织独立章节；对于 artifact_first 交付，使用工具实际创建的产物作为边界，不要把每个文件复制成文本标题。" if artifact_first else ("如果 required_outputs 多于一个，分别输出独立章节，标题文字遵循契约；" + chr(10) + chr(10).join(f"- {str(item.get('name', ''))}" for item in required_outputs) if len(required_outputs) > 1 else "单一文本交付物，按契约生成。"))}
"""

    # Final synthesis consumes the Governor-approved working set only.  It is
    # a text transformation, so it must not receive discovery tools or enter
    # a tool-call loop; tool use belongs to the execution workers above.
    synthesis_client = client
    if hasattr(client, "api_key") and hasattr(client, "model"):
        synthesis_timeout = max(30, int(os.getenv("ASTRALNEXUS_FINAL_STAGE_TIMEOUT", str(synthesis_timeout))))
        synthesis_client = LLMClient(
            model=client.model,
            api_key=client.api_key,
            base_url=client.base_url,
            max_tokens=getattr(client, "max_tokens", None),
            temperature=getattr(client, "temperature", None),
            timeout=synthesis_timeout,
            interactive=False,
        )
        synthesis_client.max_retries = max(0, int(os.getenv("ASTRALNEXUS_FINAL_STAGE_RETRIES", "1")))
    # Directory-shaped contracts require real materialization.  Give the
    # final writer only the bounded delivery toolset; discovery, browser and
    # unrelated tools must never enter the final synthesis context.
    from AstralNexus.tools.registry import discover_builtin_tools
    from AstralNexus.tools.tool_router import discover_tools
    discover_builtin_tools()
    synthesis_tools = discover_tools(
        "coder",
        "materialize the contract-required delivery files and run their required checks",
        ["file_read", "file_write", "terminal_exec", "python_exec", "pytest_run"],
    )
    has_file_outputs = any(
        isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("type", "")).lower() in {"file", "files", "software"}
        for item in contract.get("required_outputs", [])
    )
    orchestrator.trace_recorder.record("delivery.synthesis.started", {
        "model": synthesis_model,
        "timeout_seconds": getattr(synthesis_client, "timeout", None),
        "tools_enabled": bool(synthesis_tools),
        "tool_names": [item.get("name") for item in synthesis_tools],
        "staging_dir": str(layout.delivery_staging_dir),
    })
    response = ""
    quality_issues: list[str] = []
    configured_quality_attempts = int(
        os.getenv("ASTRALNEXUS_FINAL_QUALITY_ATTEMPTS", "0")
    )
    final_quality_attempts = (
        None if configured_quality_attempts <= 0 else configured_quality_attempts
    )
    attempt = 0
    while True:
        attempt += 1
        retry_prompt = prompt
        if attempt > 1:
            retry_prompt += f"""

上一次输出未通过任务契约验收（第 {attempt - 1} 次尝试）。请重新完整输出，不要解释原因。
需要修复的问题：{json.dumps(quality_issues, ensure_ascii=False)}
以下是不得无证据作出的断言。它们是语义政策，不是要求你机械删除或逐字复述的字符串；
可以诚实说明“尚未完成/缺少证据”，但不要声称这些事情已经发生：
{chr(10).join(f"- {marker}" for marker in forbidden_markers) if forbidden_markers else "（无）"}
请改用可验证、有限度的表述，并保留任务要求的事实、限制与不确定性。
"""
        tool_instructions = ""
        if has_file_outputs:
            tool_instructions = f"""

【真实文件交付要求】
本任务包含目录型交付物。你必须使用已提供的 file_write 工具实际创建文件，不能只描述文件内容。
所有交付文件必须写入以下目录及其子目录，禁止写入源码目录或其他路径：
{layout.delivery_staging_dir}
目录映射：
- 源代码目录（deliverables/app/） -> {layout.delivery_staging_dir / 'app'}
- 文档目录（deliverables/docs/） -> {layout.delivery_staging_dir / 'docs'}
- 测试证据 -> {layout.delivery_staging_dir / 'evidence'}
            至少创建每个 required_outputs 对应目录下的真实文件，并用 terminal_exec/python_exec/pytest_run 执行可行的最小验证。
            file_write 的 path 必须是具体文件路径，不能把 deliverables/app 或 deliverables/docs 目录本身作为文件写入目标；terminal_exec 的 command 必须是非空 argv 数组。
            最终文字可以概述结果，但“已创建/已验证”只能基于工具返回的真实结果。
"""
        response = synthesis_client.chat(
            [
                {"role": "system", "content": "你是严谨的通用交付编辑，只输出最终用户交付内容。"},
                {"role": "user", "content": retry_prompt + tool_instructions},
            ],
            tools=synthesis_tools or None,
            max_tool_rounds=(
                (
                    None
                    if int(os.getenv("ASTRALNEXUS_FINAL_TOOL_ROUNDS", "0")) <= 0
                    else int(os.environ["ASTRALNEXUS_FINAL_TOOL_ROUNDS"])
                )
                if synthesis_tools else 0
            ),
            workspace_root=layout.delivery_staging_dir,
        ).strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        missing_sections = [title for title in required_sections if title not in response]
        forbidden_claims = _unsupported_forbidden_claims(response, forbidden_markers)
        missing_outputs = [] if artifact_first else _missing_contract_output_names(
            response, contract.get("required_outputs", []))
        quality_issues = []
        if missing_sections:
            quality_issues.append("missing_sections=" + ",".join(missing_sections))
        if forbidden_claims:
            quality_issues.append("forbidden_claims=" + ",".join(forbidden_claims))
        if missing_outputs:
            quality_issues.append("missing_outputs=" + ",".join(missing_outputs))
        if response and not quality_issues:
            break
        logger.warning(
            "Final LLM synthesis failed quality gate on attempt %s (%s chars; issues=%s)",
            attempt,
            len(response),
            quality_issues,
        )

    missing_sections = [title for title in required_sections if title not in response]
    forbidden_claims = _unsupported_forbidden_claims(response, forbidden_markers)
    missing_outputs = [] if artifact_first else _missing_contract_output_names(
        response, contract.get("required_outputs", []))
    has_real_tool_artifacts = bool(getattr(synthesis_client, "last_tool_artifacts", []) or [])
    if (response_required and not response) or (artifact_first and not response and not has_real_tool_artifacts) or missing_sections or forbidden_claims or missing_outputs:
        raise RuntimeError(
            "Final LLM synthesis failed the task-contract quality gate: "
            f"chars={len(response)}, missing_sections={missing_sections}, "
            f"missing_outputs={missing_outputs}, forbidden_claims={forbidden_claims}"
        )

    # Persist the synthesizer response under a domain-neutral name.  The
    # Governor contract, not the CLI, decides whether the task additionally
    # requires source files, reports, datasets, or other artifacts.
    main_path = layout.delivery("delivery_response.md")
    if response:
        main_path.write_text(response + "\n", encoding="utf-8")

    # Promote only files actually written by the final delivery tool calls
    # into public contract references.  A model-mentioned path outside the
    # staging directory is never accepted as evidence.
    synthesis_tool_refs: list[str] = []
    staging_root = layout.delivery_staging_dir.resolve()
    for raw_path in list(getattr(synthesis_client, "last_tool_artifacts", []) or []):
        try:
            candidate = Path(str(raw_path)).resolve()
            relative = candidate.relative_to(staging_root)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            synthesis_tool_refs.append(
                "deliverables/" + relative.as_posix()
            )
    synthesis_tool_refs = list(dict.fromkeys(synthesis_tool_refs))

    # Materialize independently addressable outputs before Delivery Gate. The
    # contract, not the task domain, decides whether one response or several
    # files are required.
    contract_has_declared_directories = any(
        "deliverables/" in (
            str(item.get("name", "")) + " "
            + " ".join(str(value) for value in item.get("acceptance_criteria", []) or [])
        ).lower()
        for item in contract.get("required_outputs", []) or []
        if isinstance(item, dict)
    )
    if artifact_first or contract_has_declared_directories:
        contract_output_refs, missing_contract_outputs = [], []
    else:
        contract_output_refs, missing_contract_outputs = _materialize_contract_outputs(
            response=response,
            required_outputs=contract.get("required_outputs", []),
            delivery_dir=layout.delivery_dir,
        )
        if missing_contract_outputs:
            tool_refs, tool_missing = _materialize_tool_contract_outputs(
                required_outputs=contract.get("required_outputs", []),
                delivery_dir=layout.delivery_dir,
                artifact_paths=list(getattr(synthesis_client, "last_tool_artifacts", []) or []),
            )
            if tool_refs:
                contract_output_refs = list(dict.fromkeys(contract_output_refs + tool_refs))
            missing_contract_outputs = tool_missing if tool_missing else []
    if missing_contract_outputs:
        raise RuntimeError(
            "Final LLM synthesis omitted required contract outputs: "
            + ", ".join(missing_contract_outputs)
        )

    # Generate structured JSON summary if required by contract
    # (must exist before Delivery Gate so artifact binding can find it)
    augmented_refs = list(layered["artifact_refs"])
    if response:
        augmented_refs.append("deliverables/delivery_response.md")
    augmented_refs.extend(contract_output_refs)
    augmented_refs.extend(synthesis_tool_refs)
    needs_json_summary = any(
        isinstance(out, dict)
        and (
            out.get("type") == "data"
            or "JSON" in str(out.get("name", "")).upper()
        )
        for out in required_outputs
    )
    if needs_json_summary:
        section_titles_for_json = re.findall(r"^#{1,3}\s+(.+)$", response, flags=re.MULTILINE)
        json_summary = {
            "title": section_titles_for_json[0] if section_titles_for_json else task_content.splitlines()[0].strip(),
            "sections": section_titles_for_json,
            "content_length": len(response),
            "model": synthesis_model,
            "node_count": len(orchestrator.task_graph.nodes),
            "completed_nodes": sum(n.status == "completed" for n in orchestrator.task_graph.nodes.values()),
        }
        json_summary_path = layout.delivery("summary.json")
        json_summary_path.write_text(
            json.dumps(json_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        augmented_refs.append("deliverables/summary.json")

    # Bind the explicit path-declaration output before Delivery Gate.  The
    # declaration in the paper is user-facing content; this manifest is the
    # independently addressable artifact required by the delivery contract.
    path_manifest_path = layout.delivery("delivery_path_manifest.md")
    required_names = [
        str(item.get("name", ""))
        for item in required_outputs
        if isinstance(item, dict) and item.get("required", True)
    ]
    path_manifest = (
        "# 交付物路径声明\n\n"
        "本清单由运行时生成，交付形态由 Governor 的任务契约决定。\n\n"
        + (f"- 用户摘要：`deliverables/{main_path.name}`\n" if response else "")
        + "- 机器可读运行摘要：`deliverables/summary.json`（如契约要求）\n"
        "- 验收说明：`deliverables/acceptance_report.md`\n"
        "- 契约要求的交付物：\n"
        + ("\n".join(
            f"  - {name}: `{ref}`"
            for name, ref in zip(required_names, contract_output_refs)
        ) if contract_output_refs else (
            "\n".join(f"  - {name}" for name in required_names)
            if required_names else "  - 无额外文件交付物"
        ))
        + "\n"
    )
    path_manifest_path.write_text(path_manifest, encoding="utf-8")
    if response:
        response = response.rstrip() + "\n\n" + path_manifest
        main_path.write_text(response + "\n", encoding="utf-8")
    augmented_refs.append("deliverables/delivery_path_manifest.md")

    # Keep the acceptance/reading guide as an independently addressable
    # deliverable before the Gate evaluates the contract.  It records actual
    # runtime evidence without replacing the user-facing answer.
    acceptance_path = layout.delivery("acceptance_report.md")
    completed_nodes = sum(n.status == "completed" for n in orchestrator.task_graph.nodes.values())
    failed_nodes = sum(n.status == "failed" for n in orchestrator.task_graph.nodes.values())
    acceptance_path.write_text(
        "# 评测/验收说明或运行手册\n\n"
        f"- 任务：{task_content.splitlines()[0].strip()}\n"
        f"- 模型：{synthesis_model}\n"
        f"- 任务图节点：{len(orchestrator.task_graph.nodes)}\n"
        f"- 已完成节点：{completed_nodes}\n"
        f"- 失败节点：{failed_nodes}\n"
        "- 证据链：`layered/global_verifier.json`、`layered/outline.md`、`trace.jsonl`\n"
        + (f"- 用户摘要：`deliverables/{main_path.name}`\n" if response else "- 用户摘要：本任务未要求额外文本摘要。\n")
        + "- JSON 摘要：`deliverables/summary.json`（如契约要求）\n"
        "- 路径清单：`deliverables/delivery_path_manifest.md`\n"
        "\n本文件用于说明本次运行的验收入口和产物位置；最终业务答案以主交付文件为准。\n",
        encoding="utf-8",
    )
    augmented_refs.append("deliverables/acceptance_report.md")

    staged_contract_refs = _stage_declared_delivery_directories(
        run_dir=run_dir,
        staging_dir=layout.delivery_staging_dir,
        required_outputs=required_outputs,
        required_sections=contract.get("required_sections", []) or [],
    )
    augmented_refs.extend(staged_contract_refs)

    # Run Delivery Gate with augmented artifact refs.  A rejected draft is a
    # repair signal, not immediately a terminal task failure: the final writer
    # receives the structured Gate findings and must rewrite unsupported or
    # placeholder claims before the next verification.
    delivery_gate = None
    configured_delivery_repairs = int(
        os.getenv("ASTRALNEXUS_DELIVERY_REPAIR_ATTEMPTS", "0")
    )
    delivery_repair_attempts = (
        None if configured_delivery_repairs <= 0 else configured_delivery_repairs
    )
    delivery_attempt = 0
    while True:
        delivery_attempt += 1
        delivery_gate = orchestrator.governor.run_delivery_gate(
            contract,
            content=response,
            artifact_refs=augmented_refs,
            run_dir=run_dir,
            client=client,
        )
        if delivery_gate["delivery_authorized"]:
            break
        if (
            delivery_repair_attempts is not None
            and delivery_attempt >= delivery_repair_attempts + 1
        ):
            break
        orchestrator.trace_recorder.record("delivery.gate.repair_requested", {
            "attempt": delivery_attempt + 1,
            "failed_tests": delivery_gate.get("failed_tests", []),
            "reason": "final_draft_failed_contract_verification",
        })
        repair_prompt = prompt + f"""

上一版最终稿未通过 Delivery Gate。请在不改变任务目标和必需章节的前提下完整重写最终稿。
Gate 报告：
{json.dumps(delivery_gate, ensure_ascii=False, indent=2)}

追加真实性约束：
- 删除申请人、论文、专利、数据集、仓库等无法由批准证据中的明确标识支持的内容；
- 删除或改写没有可追溯来源的具体百分比、性能数值、FLOPs、延迟和提升幅度；
- 不得使用“某高校/科研机构”等占位符冒充真实来源；
- 缺少真实证据时明确写“未验证/待实验”，只能提出实验指标和验证方法，不能写成已发生结果；
- 不得把 EvidencePacket、Governor 或节点编号本身当作外部事实来源。
"""
        response = synthesis_client.chat([
            {"role": "system", "content": "你是严谨的最终交付修复编辑，只输出修复后的用户交付内容。"},
            {"role": "user", "content": repair_prompt + tool_instructions},
        ], tools=synthesis_tools or None, max_tool_rounds=(
            (
                None
                if int(os.getenv("ASTRALNEXUS_FINAL_TOOL_ROUNDS", "0")) <= 0
                else int(os.environ["ASTRALNEXUS_FINAL_TOOL_ROUNDS"])
            )
            if synthesis_tools else 0
        ), workspace_root=layout.delivery_staging_dir).strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if artifact_first or contract_has_declared_directories:
            # Directory contracts are satisfied by real files already written
            # under the run workspace.  The final editor must repair prose or
            # evidence references, not recreate software files in a Markdown
            # response.  Re-stage the directories after each repair so the
            # Gate sees the same physical artifacts on every attempt.
            repaired_refs, repaired_missing = [], []
            repaired_refs.extend(_stage_declared_delivery_directories(
                run_dir=run_dir,
                staging_dir=layout.delivery_staging_dir,
                required_outputs=required_outputs,
                required_sections=contract.get("required_sections", []) or [],
            ))
        else:
            repaired_refs, repaired_missing = _materialize_contract_outputs(
                response=response,
                required_outputs=contract.get("required_outputs", []),
                delivery_dir=layout.delivery_dir,
            )
            if repaired_missing:
                tool_refs, tool_missing = _materialize_tool_contract_outputs(
                    required_outputs=contract.get("required_outputs", []),
                    delivery_dir=layout.delivery_dir,
                    artifact_paths=list(getattr(synthesis_client, "last_tool_artifacts", []) or []),
                )
                repaired_refs = list(dict.fromkeys(repaired_refs + tool_refs))
                repaired_missing = tool_missing if tool_missing else []
        if repaired_missing:
            raise RuntimeError(
                "Delivery Gate repair omitted required contract outputs: "
                + ", ".join(repaired_missing)
            )
        augmented_refs = [ref for ref in augmented_refs if ref not in contract_output_refs]
        augmented_refs.extend(repaired_refs)
        repaired_tool_refs: list[str] = []
        for raw_path in list(getattr(synthesis_client, "last_tool_artifacts", []) or []):
            try:
                candidate = Path(str(raw_path)).resolve()
                relative = candidate.relative_to(staging_root)
            except (OSError, ValueError):
                continue
            if candidate.is_file():
                repaired_tool_refs.append("deliverables/" + relative.as_posix())
        augmented_refs.extend(list(dict.fromkeys(repaired_tool_refs)))
        contract_output_refs = repaired_refs
        response = response.rstrip() + "\n\n" + path_manifest
        main_path.write_text(response + "\n", encoding="utf-8")
    layout.delivery("delivery_verification.json").write_text(
        json.dumps(delivery_gate, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not delivery_gate["delivery_authorized"]:
        raise RuntimeError(
            "Delivery Gate rejected final output: "
            f"status={delivery_gate['overall_status']}, "
            f"failed={delivery_gate['failed_tests']}, "
            f"unknown={delivery_gate.get('unknown_tests', [])}"
        )

    orchestrator.trace_recorder.record("delivery.synthesis.completed", {
        "chars": len(response),
        "delivery_gate": delivery_gate.get("overall_status"),
        "tools_enabled": False,
    })

    # Complete the entire public delivery package in staging first.  Writing
    # summary files after publish used to split one delivery across the
    # version directory and the deliverables root.
    main_path = layout.delivery("delivery_response.md")
    section_titles = re.findall(r"^#{1,3}\s+(.+)$", response, flags=re.MULTILINE)
    summary = {
        "product": "天枢",
        "code_name": "AstralNexus",
        "goal": task_content.splitlines()[0].strip(),
        "model": synthesis_model,
        "node_count": len(orchestrator.task_graph.nodes),
        "completed_nodes": sum(n.status == "completed" for n in orchestrator.task_graph.nodes.values()),
        "section_titles": section_titles,
        "main_delivery": "deliverables/delivery_response.md" if response else None,
        "evidence_node_outputs": len(layered["packets"]),
        "approved_working_set": len(layered["approved_packets"]),
        "deferred_packet_ids": layered["deferred_packet_ids"],
        "delivery_contract": contract,
        "delivery_gate": delivery_gate,
        "layered_groups": len(layered["groups"]),
        "layered_artifacts": layered["artifact_refs"],
        "delivery_artifacts": [
            "deliverables/delivery_verification.json",
            "deliverables/delivery_summary.json",
            "deliverables/acceptance_report.md",
        ],
    }
    summary_path = layout.delivery("delivery_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    acceptance_path = layout.delivery("acceptance_report.md")
    acceptance_path.write_text(
        "# 交付验收说明\n\n"
        + (f"- 用户摘要：`{main_path.name}`\n" if response else "- 用户摘要：未要求。\n")
        + f"- 机器摘要：`{summary_path.name}`\n"
        f"- 有效角色输出：{len(layered['packets'])}\n"
        f"- Governor 批准工作集：{len(layered['approved_packets'])} 个节点\n"
        f"- Governor 延后节点：{len(layered['deferred_packet_ids'])} 个\n"
        f"- 分层 reducer：{len(layered['groups'])} 个\n"
        f"- 任务图节点：{len(orchestrator.task_graph.nodes)}\n"
        f"- 已完成节点：{summary['completed_nodes']}\n\n"
        f"- Delivery Gate：{delivery_gate['overall_status']}\n"
        "- 全局验收证据：`deliverables/delivery_verification.json`\n"
        "主文由真实 LLM 基于任务书与节点证据生成；运行审计另见 `reports/final_report.md`，两者不混用。\n",
        encoding="utf-8",
    )
    publish_manifest = layout.publish_delivery(
        authorized=True,
        source_refs=[p.get("node_id", "") for p in layered.get("approved_packets", [])],
        dependencies=layered.get("artifact_refs", []),
    )
    public_dir = layout.run_dir / "deliverables" / f"v{publish_manifest['version']}"
    return {
        "main_markdown": str(public_dir / main_path.name),
        "summary_json": str(public_dir / summary_path.name),
        "acceptance_report": str(public_dir / acceptance_path.name),
    }


def _generate_failure_report(
    *,
    task_content: str,
    run_dir: Path,
    orchestrator: Orchestrator,
    reason: str,
    detail: str,
) -> dict[str, str]:
    """Generate a minimal auditable failure report when no successful node outputs exist.

    This is the graceful degradation path: instead of crashing the CLI,
    write structured failure artifacts so the run remains auditable.
    """
    layout = RunOutputLayout(run_dir)
    layout.ensure_runtime_dirs()

    nodes = orchestrator.task_graph.nodes
    node_summary = {
        "total": len(nodes),
        "completed": sum(1 for n in nodes.values() if n.status == "completed"),
        "failed": sum(1 for n in nodes.values() if n.status == "failed"),
        "ready": sum(1 for n in nodes.values() if n.status == "ready"),
        "waiting": sum(1 for n in nodes.values() if n.status in ("waiting", "waiting_dependencies")),
        "terminal_failures": [
            {"node_id": nid, "reason": n.metadata.get("_terminal_reason", "unknown")}
            for nid, n in nodes.items() if n.metadata.get("_terminal_failure")
        ],
    }

    goal = task_content.splitlines()[0].strip() if task_content else "Execute task"

    # Main failure report
    main_path = layout.delivery("delivery_response.md")
    main_path.write_text(
        f"# 任务执行失败报告\n\n"
        f"## 任务目标\n\n{goal}\n\n"
        f"## 失败原因\n\n- 类型：{reason}\n- 详情：{detail}\n\n"
        f"## 任务图状态\n\n"
        f"- 总节点数：{node_summary['total']}\n"
        f"- 已完成：{node_summary['completed']}\n"
        f"- 已失败：{node_summary['failed']}\n"
        f"- 就绪：{node_summary['ready']}\n"
        f"- 等待中：{node_summary['waiting']}\n\n"
        f"## 终止失败节点\n\n"
        + (
            "\n".join(
                f"- `{t['node_id']}`: {t['reason']}"
                for t in node_summary["terminal_failures"]
            )
            if node_summary["terminal_failures"]
            else "（无终止失败节点）"
        )
        + "\n\n## 结论\n\n"
        f"本次运行因能力缺口或执行故障未能产生成功节点输出，"
        f"分层汇总和最终交付无法执行。请检查任务图设计、Worker Profile 配置和所需能力匹配。\n",
        encoding="utf-8",
    )

    # Summary JSON
    summary_path = layout.delivery("delivery_summary.json")
    summary_path.write_text(
        json.dumps(
            {
                "product": "天枢",
                "code_name": "AstralNexus",
                "goal": goal,
                "status": "failed",
                "failure_reason": reason,
                "failure_detail": detail,
                "node_summary": node_summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Acceptance report
    acceptance_path = layout.delivery("acceptance_report.md")
    acceptance_path.write_text(
        f"# 交付验收说明（失败）\n\n"
        f"- 主文：`{main_path.name}`\n"
        f"- 机器摘要：`{summary_path.name}`\n"
        f"- 状态：**未通过** — 无成功节点输出\n"
        f"- 运行审计另见 `reports/final_report.md`\n",
        encoding="utf-8",
    )

    return {
        "main_markdown": str(main_path),
        "summary_json": str(summary_path),
        "acceptance_report": str(acceptance_path),
    }


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
        
        orchestrator = Orchestrator(run_dir=run_dir)
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
        phase_counts = {}
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
        
        status_counts = {}
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
    except Exception as e:
        print(f"错误: 运行评估时发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if 'orchestrator' in locals():
            orchestrator.shutdown()



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
            f"**开始时间**: {run_info.get('start_time', 'N/A')}",
            f"**结束时间**: {run_info.get('end_time', 'N/A')}",
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
        status_counts = {}
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
                f"- **执行者**: {node.executor or 'N/A'}",
                f"- **迭代次数**: {node.iteration_count}",
                f"- **重试次数**: {node.retry_count}",
                f"- **创建时间**: {node.created_at or 'N/A'}",
                f"- **完成时间**: {node.completed_at or 'N/A'}",
                f"- **依赖**: {', '.join(node.dependencies) if node.dependencies else '无'}",
                f"",
            ])
            
            if node.error_message:
                report_lines.extend([
                    f"**错误信息**:",
                    f"```",
                    f"{node.error_message}",
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
                report_lines.append(f"- **{fact_id}**: {fact['content'][:100]}...")
            
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
                report_lines.append(f"- **{artifact_id}**: {artifact['path']}")
            
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
                graph_store = SQLiteTaskGraphStore(candidate / "task_graph.sqlite")
                run_info = graph_store.get_run_info(run_id)
                if run_info:
                    run_dir = candidate
                    break
        
        if not run_dir:
            print(f"错误: 找不到运行ID: {run_id}")
            return 1
        
        print(f"找到运行目录: {run_dir}")
        
        graph_store = SQLiteTaskGraphStore(run_dir / "task_graph.sqlite")
        orchestrator = Orchestrator(
            run_dir,
            graph_store=graph_store,
            run_id=run_id,
        )
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
        target_node = orchestrator.task_graph.get_node(target_node.node_id)
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
            f"- **执行者**: {target_node.executor or 'N/A'}",
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
        
    except Exception as e:
        print(f"错误: 注入故障时发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


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
        planned_run_dir = Path(args.get("output")).expanduser() if args.get("output") else (
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
                    "你已获得当前请求筛选后的真实工具契约；工具调用结果是唯一事实来源。"
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
        local_request = any(
            marker in user_input.lower()
            for marker in (
                "代码", "源码", "工作区", "文件", "项目", "仓库", "读取", "阅读",
                "审视", "审查", "检查", "修改", "运行测试",
            )
        )
        external_request = any(
            marker in user_input.lower()
            for marker in (
                "网上", "互联网", "公开渠道", "网页", "网站", "在线",
                "中标", "招标", "采购公告", "成交公告", "标书",
                "新闻", "最新", "web", "online", "internet",
            )
        )
        required_tools = ["delegate_task"]
        if local_request:
            required_tools.extend(["search_files", "file_read"])
        if external_request:
            required_tools.extend(["web_search", "http_get"])
        candidates = None
        if external_request and not local_request:
            candidates = [
                name for name in ("web_search", "http_get", "browser_fetch")
                if registry.get_entry(name) is not None
            ]
        selected_names = select_tools(
            "interactive",
            user_input,
            candidates=candidates,
            required_tools=required_tools,
            max_tools=12,
        )
        tools = registry.get_definitions(set(selected_names))

        streamed_answer = {"started": False, "text": []}

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
