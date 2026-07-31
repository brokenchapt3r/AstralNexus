from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shlex
from pathlib import Path
from typing import List, Optional


def _default_home() -> Path:
    """Return the canonical runtime home selected by product bootstrap."""
    from AstralNexus.core.identity import bootstrap_environment

    return bootstrap_environment()


def _find_run_dirs(home: Optional[Path]) -> list[Path]:
    runs_dir = (home or _default_home()) / "runs"
    if not runs_dir.exists():
        return []
    return sorted(
        [p for p in runs_dir.iterdir() if p.is_dir() and (p / "task_graph.sqlite").exists()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _resolve_run_dir(session_id: str, home: Optional[Path]) -> Optional[Path]:
    candidate = Path(session_id).expanduser()
    if candidate.is_dir() and (candidate / "task_graph.sqlite").exists():
        return candidate
    for run_dir in _find_run_dirs(home):
        try:
            from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
            if any(item.get("run_id") == session_id for item in SQLiteTaskGraphStore(run_dir / "task_graph.sqlite").list_runs()):
                return run_dir
        except Exception:
            continue
    return None


def _args(**values):
    from types import SimpleNamespace
    return SimpleNamespace(**values)


def _write_config_value(path: Path, key: str, value: str) -> None:
    import yaml
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if key == "model":
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        model["default"] = value
        data["model"] = model
    elif key == "provider":
        provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
        provider["default"] = value
        data["provider"] = provider
    else:
        data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@dataclass(frozen=True)
class CommandDef:
    """Definition of a CLI command."""
    
    # Command name (without slash)
    name: str
    
    # Command description
    description: str
    
    # Category for help organization
    category: str
    
    # Alternative names for the command
    aliases: tuple = ()
    
    # Argument hint for help display
    args_hint: str = ""
    
    # Commands with stable script flags are parsed by argparse. Other system
    # commands use the same handler table as slash commands.
    argparse_owned: bool = False

    # Commands that need the live background controller are intercepted by
    # the interactive session. Their non-interactive handlers remain explicit
    # entry points and must never pretend to affect an active session.
    session_owned: bool = False


# Command registry
COMMAND_REGISTRY: List[CommandDef] = [
    # Session commands
    CommandDef("new", "开始新的任务会话", "Session", aliases=("n",), args_hint="[goal]", session_owned=True),
    CommandDef("resume", "恢复之前的任务会话", "Session", aliases=("r",), args_hint="<session-id>", argparse_owned=True, session_owned=True),
    CommandDef("list", "列出最近的任务会话", "Session", aliases=("ls",)),
    CommandDef("status", "显示当前任务状态", "Session", session_owned=True),
    CommandDef("interrupt", "协作式中断当前后台任务", "Session", args_hint="[message]", session_owned=True),
    
    # Configuration commands
    CommandDef("config", "管理系统配置", "Configuration", args_hint="<get|set|list> [key] [value]"),
    CommandDef("tools", "管理工具集", "Configuration", args_hint="<enable|disable|list> [toolset]"),
    CommandDef("skills", "搜索、审计和管理技能", "Configuration", args_hint="<find|install|audit|list|update> [skill/query]"),
    CommandDef("models", "管理模型配置", "Configuration", aliases=("model",), args_hint="<list|set> [model]"),
    
    # Execution commands
    CommandDef("run", "运行任务", "Execution", args_hint="<task-file>", argparse_owned=True, session_owned=True),
    CommandDef("stream", "流式调用当前模型", "Execution", args_hint="<prompt>", argparse_owned=True),
    CommandDef("eval", "运行评估基准", "Execution", args_hint="<benchmark>", argparse_owned=True),
    CommandDef("report", "生成报告", "Execution", args_hint="<run-id>", argparse_owned=True),
    CommandDef("dashboard", "启动仪表板", "Execution", args_hint="[run-id]", argparse_owned=True),
    
    # Fault injection commands
    CommandDef("inject-fault", "注入故障", "Fault Injection", args_hint="<run-id> <type> [payload.json]", argparse_owned=True),
    CommandDef("inject-requirement", "注入需求变更", "Fault Injection", args_hint="<run-id> <change.json>", session_owned=True),
    
    # Information commands
    CommandDef("help", "显示帮助信息", "Info", aliases=("?",), argparse_owned=True),
    CommandDef("version", "显示版本信息", "Info"),
    CommandDef("info", "显示系统信息", "Info"),
    
    # Exit commands
    CommandDef("quit", "退出程序", "Exit", aliases=("exit",), session_owned=True),
    CommandDef("reset", "重置系统状态", "Exit"),
]


ARGPARSE_COMMANDS = frozenset(
    command.name for command in COMMAND_REGISTRY if command.argparse_owned
)
SESSION_COMMANDS = frozenset(
    command.name for command in COMMAND_REGISTRY if command.session_owned
)


def resolve_command(command_name: str) -> Optional[str]:
    """Resolve a command name or alias to its canonical name.
    
    Args:
        command_name: The command name or alias to resolve
        
    Returns:
        The canonical command name, or None if not found
    """
    # Remove leading slash if present
    if command_name.startswith("/"):
        command_name = command_name[1:]
    
    # Check for exact match
    for cmd in COMMAND_REGISTRY:
        if cmd.name == command_name:
            return cmd.name
    
    # Check for alias match
    for cmd in COMMAND_REGISTRY:
        if command_name in cmd.aliases:
            return cmd.name
    
    return None


def split_command_line(command_line: str) -> list[str]:
    """Parse one command while preserving quoted Windows paths."""
    try:
        parts = shlex.split(str(command_line or "").strip(), posix=False)
    except ValueError as exc:
        raise ValueError(f"命令解析失败: {exc}") from exc
    normalized = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'}:
            part = part[1:-1]
        normalized.append(part)
    return normalized


def command_catalog() -> dict:
    """Build the one command catalog consumed by Python and Ink frontends."""
    pairs = [[f"/{cmd.name}", cmd.description] for cmd in COMMAND_REGISTRY]
    canon: dict[str, str] = {}
    categories: dict[str, list[list[str]]] = {}
    for cmd in COMMAND_REGISTRY:
        canonical = f"/{cmd.name}"
        canon[canonical] = canonical
        for alias in cmd.aliases:
            canon[f"/{alias}"] = canonical
        categories.setdefault(cmd.category, []).append(
            [canonical, cmd.description]
        )
    return {
        "pairs": pairs,
        "canon": canon,
        "categories": [
            {"name": name, "pairs": pairs}
            for name, pairs in categories.items()
        ],
        "sub": {},
        "skill_count": 0,
    }


def _command_handlers() -> dict:
    """Return the canonical execution table for registered commands."""
    return {
        "help": _handle_help,
        "version": _handle_version,
        "info": _handle_info,
        "new": _handle_new,
        "resume": _handle_resume,
        "status": _handle_status,
        "interrupt": _handle_interrupt,
        "list": _handle_list,
        "config": _handle_config,
        "tools": _handle_tools,
        "skills": _handle_skills,
        "models": _handle_models,
        "run": _handle_run,
        "stream": _handle_stream,
        "eval": _handle_eval,
        "report": _handle_report,
        "dashboard": _handle_dashboard,
        "inject-fault": _handle_inject_fault,
        "inject-requirement": _handle_inject_requirement,
        "reset": _handle_reset,
        "quit": _handle_quit,
    }


def process_command(command_line: str, *, home: Optional[Path] = None) -> int:
    """Parse and dispatch one textual slash command."""
    try:
        parts = split_command_line(command_line)
    except ValueError as exc:
        print(str(exc))
        return 1
    return process_command_args(parts, home=home)


def process_command_args(parts: List[str], *, home: Optional[Path] = None) -> int:
    """Dispatch pre-tokenized CLI arguments through the canonical registry.

    System command entry points already receive an argv array. Re-parsing it as
    a shell string corrupts quoted Windows paths, so both CLI and TUI converge
    here after their respective tokenization boundary.
    """
    parts = list(parts or [])
    if not parts:
        return _handle_help([], home=home)
    command_name = parts[0].lstrip("/").lower()
    canonical = resolve_command(command_name)
    if canonical is None:
        print(f"未知命令: {parts[0]}")
        return 1
    handler = _command_handlers().get(canonical)
    if handler is None:
        print(f"命令尚未实现: /{canonical}")
        return 1
    return int(handler(parts[1:], home=home) or 0)
def _handle_help(args: List[str], **kwargs) -> int:
    """显示帮助信息"""
    print("\n可用命令:")
    print("=" * 60)
    
    categories = {}
    for cmd in COMMAND_REGISTRY:
        if cmd.category not in categories:
            categories[cmd.category] = []
        categories[cmd.category].append(cmd)
    
    for category, commands in sorted(categories.items()):
        print(f"\n{category}:")
        for cmd in commands:
            aliases = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
            args_hint = f" {cmd.args_hint}" if cmd.args_hint else ""
            print(f"  /{cmd.name}{aliases}{args_hint}")
            print(f"    {cmd.description}")
    
    print()
    return 0


def _handle_interrupt(args: List[str], **kwargs) -> int:
    """Document the TUI-owned interrupt path for non-interactive callers."""
    print("/interrupt 只能在 AstralNexus 交互会话中取消后台运行；请先启动交互模式。")
    return 1


def _handle_version(args: List[str], **kwargs) -> int:
    """显示版本信息"""
    from AstralNexus.core.identity import VERSION

    print(f"天枢 AstralNexus v{VERSION}")
    print("动态异构群体智能协同推理引擎")
    return 0


def _handle_info(args: List[str], **kwargs) -> int:
    """显示系统信息"""
    import os
    from pathlib import Path
    
    home = kwargs.get("home") or _default_home()
    print("\n系统信息:")
    print(f"  工作目录: {Path.cwd()}")
    print(f"  配置目录: {home}")
    print(f"  配置文件: {home / 'config.yaml'}")
    print(f"  环境变量:")
    for key in ["ASTRALNEXUS_HOME", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
        value = os.environ.get(key)
        if value:
            masked = value[:8] + "..." if len(value) > 8 else "***"
            print(f"    {key} = {masked}")
        else:
            print(f"    {key} = (未设置)")
    print()
    return 0


def _handle_new(args: List[str], **kwargs) -> int:
    """开始新的任务会话"""
    if not args:
        print("用法: /new <任务目标>")
        return 1
    from datetime import datetime
    from AstralNexus.cli import main as cli_main
    home = kwargs.get("home") or _default_home()
    task_file = home / "tasks" / f"interactive_{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(" ".join(args), encoding="utf-8")
    return cli_main._run_task(_args(
        task=str(task_file), output=None, config=None, model=None, provider=None,
        max_parallel=None,
    ), home)


def _handle_status(args: List[str], *, home: Optional[Path] = None) -> int:
    """显示当前任务状态"""
    runs = _find_run_dirs(home)
    if not runs:
        print("没有找到已创建的运行会话。")
        return 0
    run_dir = runs[0]
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
    store = SQLiteTaskGraphStore(run_dir / "task_graph.sqlite")
    records = store.list_runs()
    record = records[-1] if records else {}
    counts = store.counts()
    print("\n当前任务状态:")
    print(f"  运行目录: {run_dir}")
    print(f"  运行 ID: {record.get('run_id', 'unknown')}")
    print(f"  状态: {record.get('status', 'unknown')}")
    print(f"  节点: {counts.get('nodes', 0)}，边: {counts.get('edges', 0)}")
    print(f"  消息: {counts.get('messages', 0)}，事实: {counts.get('facts', 0)}")
    return 0


def _handle_list(args: List[str], *, home: Optional[Path] = None) -> int:
    """列出最近的任务会话"""
    print("\n最近的任务会话:")
    for run_dir in _find_run_dirs(home):
        print(f"  {run_dir.name}: {run_dir}")
    if not _find_run_dirs(home):
        print("  (无历史记录)")
    return 0


def _handle_config(args: List[str], **kwargs) -> int:
    """管理系统配置"""
    if not args:
        print("用法: /config <get|set|list> [key] [value]")
        return 1
    
    action = args[0]
    from AstralNexus.runtime.model_selector import get_model_config, get_config_path
    config_path = get_config_path()
    
    if action == "list":
        config = get_model_config()
        print("\n配置项:")
        for key in ("model", "provider", "base_url", "config_path", "api_key_env"):
            print(f"  {key}: {config.get(key, '')}")
        return 0
    elif action == "get" and len(args) >= 2:
        key = args[1]
        config = get_model_config()
        print(f"\n{key} = {config.get(key, '')}")
        return 0
    elif action == "set" and len(args) >= 3:
        key, value = args[1], args[2]
        if key not in {"model", "provider", "base_url"}:
            print(f"不支持直接写入的配置项: {key}")
            return 1
        _write_config_value(config_path, key, value)
        print(f"\n已写入 {config_path}: {key} = {value}")
        return 0
    else:
        print("用法: /config <get|set|list> [key] [value]")
        return 1


def _handle_tools(args: List[str], **kwargs) -> int:
    """管理工具集"""
    from AstralNexus.tools.toolsets import get_toolset_names, resolve_toolset
    
    if not args:
        print("\n可用工具集:")
        for name in get_toolset_names():
            tools = resolve_toolset(name)
            print(f"  {name}: {len(tools)} 个工具")
        return 0
    
    action = args[0]
    
    if action == "list":
        print("\n所有工具集:")
        for name in get_toolset_names():
            tools = resolve_toolset(name)
            print(f"  {name}:")
            for tool in tools[:5]:  # Show first 5 tools
                print(f"    - {tool}")
            if len(tools) > 5:
                print(f"    ... 还有 {len(tools) - 5} 个工具")
        return 0
    elif action == "show" and len(args) >= 2:
        toolset_name = args[1]
        tools = resolve_toolset(toolset_name)
        print(f"\n工具集 '{toolset_name}':")
        for tool in tools:
            print(f"  - {tool}")
        return 0
    else:
        print("用法: /tools [list|show] [toolset_name]")
        return 1


def _handle_resume(args: List[str], *, home: Optional[Path] = None) -> int:
    """恢复之前的任务会话"""
    if not args:
        print("用法: /resume <session-id>")
        return 1
    
    session_id = args[0]
    run_dir = _resolve_run_dir(session_id, home)
    if run_dir is None:
        print(f"找不到运行会话: {session_id}")
        return 1
    from AstralNexus.cli import main as cli_main

    return cli_main._resume_task(
        _args(
            run_dir=str(run_dir),
            task=None,
            config=None,
            model=None,
            provider=None,
            max_parallel=None,
        ),
        home or _default_home(),
    )


def _handle_skills(args: List[str], **kwargs) -> int:
    """管理技能"""
    skills_dir = (kwargs.get("home") or _default_home()) / "skills"
    if not args:
        print("用法: /skills <find|list|view|install|audit|enable|disable|update> [skill/query]")
        return 1
    
    action = args[0]
    
    if action == "find" and len(args) >= 2:
        from AstralNexus.tools.skill_manager import _find_skills
        result = json.loads(_find_skills({"query": " ".join(args[1:]), "limit": 10}))
        if result.get("status") != "success":
            print(f"技能搜索失败: {result.get('error', result)}")
            return 1
        print(f"\nSkill 候选（{result.get('source')}）:")
        for item in result.get("candidates", []):
            print(f"  {item['name']} | {item.get('package') or '无包标识'}")
            print(f"    {item.get('description', '')[:160]}")
            print(f"    来源: {item.get('source_url') or '未知'}")
            print(f"    安装: {item.get('install_command') or '需人工确认来源'}")
        return 0
    if action == "list":
        from AstralNexus.core.skill_runtime import skill_manifest
        installed = skill_manifest(skills_dir.parent, limit=200)
        print("\n运行时 Skill 目录（仅元数据）:")
        for skill in installed:
            print(f"  {skill['name']} | {skill['category']} | {skill['path']}")
        if not installed:
            print("  (无可用技能)")
        return 0
    elif action == "view" and len(args) >= 2:
        from AstralNexus.tools.skill_manager import _skill_view
        result = json.loads(_skill_view({"name": args[1], "relative_path": args[2] if len(args) >= 3 else "SKILL.md",
                                          "home": str(skills_dir.parent)}))
        if result.get("status") != "success":
            print(f"技能读取失败: {result.get('error', result)}")
            return 1
        print(result.get("content", ""))
        return 0
    elif action == "install" and len(args) >= 2:
        from AstralNexus.tools.skill_manager import _skill_install
        result = json.loads(_skill_install({"package": args[1], "home": str(skills_dir.parent), "approved_by_governor": True}))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"installed", "review_required", "blocked_by_policy"} else 1
    elif action == "audit" and len(args) >= 2:
        from AstralNexus.tools.skill_manager import _skill_audit
        result = json.loads(_skill_audit({"name": args[1], "home": str(skills_dir.parent)}))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    elif action == "update":
        from AstralNexus.tools.skill_manager import _skill_update
        result = json.loads(_skill_update({"approved_by_governor": True}))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    elif action in {"enable", "disable"} and len(args) >= 2:
        from AstralNexus.tools.skill_manager import _set_skill_enabled
        result = json.loads(_set_skill_enabled({"name": args[1], "home": str(skills_dir.parent),
                                                "approved_by_governor": True}, action == "enable"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    elif action == "remove" and len(args) >= 2:
        print("为避免误删运行资产，/skills remove 暂未开放文件删除操作。")
        return 1
    else:
        print("用法: /skills <find|list|view|install|audit|enable|disable|update> [skill/query]")
        return 1


def _handle_models(args: List[str], **kwargs) -> int:
    """管理模型配置"""
    if not args:
        print("用法: /models <list|set> [model]")
        return 1
    
    action = args[0]
    
    if action == "list":
        from AstralNexus.runtime.model_selector import AVAILABLE_MODELS, get_model_config
        current = get_model_config().get("model")
        print("\n可用模型:")
        for model, info in AVAILABLE_MODELS.items():
            marker = " (当前)" if model == current else ""
            print(f"  {model}{marker} - {info.get('description', '')}")
        return 0
    elif action == "set" and len(args) >= 2:
        model = args[1]
        from AstralNexus.runtime.model_selector import AVAILABLE_MODELS, get_config_path
        if model not in AVAILABLE_MODELS:
            print(f"未知模型: {model}")
            return 1
        _write_config_value(get_config_path(), "model", model)
        print(f"\n已设置默认模型: {model}")
        return 0
    else:
        print("用法: /models <list|set> [model]")
        return 1


def _handle_run(args: List[str], *, home: Optional[Path] = None) -> int:
    """运行任务"""
    if not args:
        print("用法: /run <task-file>")
        return 1
    
    task_file = args[0]
    from AstralNexus.cli import main as cli_main
    return cli_main._run_task(_args(
        task=task_file, output=None, config=None, model=None, provider=None,
        max_parallel=None,
    ), home or _default_home())


def _handle_stream(args: List[str], *, home: Optional[Path] = None) -> int:
    """Stream one prompt through the configured real model."""
    if not args:
        print("用法: /stream <prompt>")
        return 1
    from AstralNexus.cli import main as cli_main
    return cli_main._stream_prompt(
        _args(prompt=" ".join(args), model=None),
        home or _default_home(),
    )


def _handle_eval(args: List[str], *, home: Optional[Path] = None) -> int:
    """运行评估基准"""
    if not args:
        print("用法: /eval <benchmark>")
        return 1
    
    benchmark = args[0]
    from AstralNexus.cli import main as cli_main
    return cli_main._run_evaluation(_args(scenario=benchmark, nodes=120, seed=None, strict_runtime=True, output=None), home or _default_home())


def _handle_report(args: List[str], *, home: Optional[Path] = None) -> int:
    """生成报告"""
    if not args:
        print("用法: /report <run-id>")
        return 1
    
    run_id = args[0]
    from AstralNexus.cli import main as cli_main
    return cli_main._generate_report(_args(run_id=run_id, output=None), home or _default_home())


def _handle_dashboard(args: List[str], *, home: Optional[Path] = None) -> int:
    """启动仪表板"""
    run_id = args[0] if args else "latest"
    from AstralNexus.cli import main as cli_main
    return cli_main._start_dashboard(_args(run_id=None if run_id == "latest" else run_id, port=8080), home or _default_home())


def _handle_inject_fault(args: List[str], *, home: Optional[Path] = None) -> int:
    """注入故障"""
    if len(args) < 2:
        print("用法: /inject-fault <run-id> <type> [payload.json]")
        print("  故障类型: requirement_change, node_failure, tool_failure, memory_loss, resource_degradation")
        return 1
    
    run_id = args[0]
    fault_type = args[1]
    
    valid_types = ["requirement_change", "node_failure", "tool_failure", "memory_loss", "resource_degradation"]
    if fault_type not in valid_types:
        print(f"无效的故障类型: {fault_type}")
        print(f"可用类型: {', '.join(valid_types)}")
        return 1
    
    from AstralNexus.cli import main as cli_main
    payload = args[2] if len(args) >= 3 else None
    return cli_main._inject_fault(_args(run_id=run_id, type=fault_type, payload=payload), home or _default_home())


def _handle_inject_requirement(args: List[str], *, home: Optional[Path] = None) -> int:
    """Inject a governed requirement change from a JSON payload."""
    if len(args) < 2:
        print("用法: /inject-requirement <run-id> <change.json>")
        print("载荷至少包含 new_constraints 或 change，可选 affected_nodes、acceptance_criteria。")
        return 1
    from AstralNexus.cli import main as cli_main
    return cli_main._inject_fault(
        _args(run_id=args[0], type="requirement_change", payload=args[1]),
        home or _default_home(),
    )


def _handle_reset(args: List[str], *, home: Optional[Path] = None) -> int:
    """Reset the interactive context without deleting persisted evidence."""
    target = args[0] if args else "latest"
    run_dir = _resolve_run_dir(target, home) if target != "latest" else (
        _find_run_dirs(home)[0] if _find_run_dirs(home) else None
    )
    from AstralNexus.runtime.session_reset import SessionResetManager
    reset_manager = SessionResetManager(home_dir=home or _default_home())
    record = reset_manager.reset_session(
        target,
        action="context_reset_only",
        preserve_runs=True,
        metadata={"run_dir": str(run_dir) if run_dir else None},
    )
    reset_record = {
        **record.to_dict(),
        "target": target,
        "run_dir": str(run_dir) if run_dir else None,
        "action": "context_reset_only",
        "preserved": ["task_graph.sqlite", "trace.jsonl", "node_io", "artifacts", "checkpoints"],
    }
    reset_path = (run_dir or (home or _default_home())) / "session_reset.json"
    reset_path.parent.mkdir(parents=True, exist_ok=True)
    reset_path.write_text(json.dumps(reset_record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已重置当前交互上下文，运行证据已保留: {reset_path}")
    return 0


def _handle_quit(args: List[str], **kwargs) -> int:
    """退出程序"""
    print("\n再见!")
    raise SystemExit(0)
