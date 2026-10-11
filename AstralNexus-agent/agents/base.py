"""Base agent class for AstralNexus multi-agent system."""

from __future__ import annotations
from AstralNexus.agents.contracts import AgentContext, AgentResponse, ExecutionOutcome
from AstralNexus.tools.assembly import assemble_tools, capability_guidance

import json
import glob
import hashlib
import importlib.util
import logging
import os
import re
import time
from pathlib import Path
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any








# Fix AQ —— 允许带进"文件落盘修复 pass"的执行类工具。
# 修复 pass 的用意是停止探索、把交付落地；但对实测型节点，"落地"本身要求先执行。
# 只保留执行与取证能力，不带回检索类工具（search_files / web_search / browser_fetch），
# 因为把上一轮预算耗光的正是检索与通读。实际授予的是本集合与节点自身已授权工具的交集。
_ARTIFACT_REPAIR_EVIDENCE_TOOLS = frozenset({
    "python_exec", "terminal_exec", "pytest_run", "http_get",
})


def artifact_repair_toolset(authorized_tools: Any) -> set[str]:
    """落盘修复 pass 实际授予的工具集合。

    始终含 file_write（这一 pass 的目的就是把交付写下来），再并上节点自身
    已授权的执行类工具。不含任何检索类工具。
    """
    authorized = {str(item) for item in (authorized_tools or [])}
    return {"file_write"} | (_ARTIFACT_REPAIR_EVIDENCE_TOOLS & authorized)


def artifact_repair_directive(repair_tool_names: Any) -> str:
    """修复 pass 的指令前缀：先测后写，测不到就如实标注。"""
    granted = sorted({str(item) for item in (repair_tool_names or [])} - {"file_write"})
    text = ("上一轮已经完成分析，但没有满足节点的真实文件后置条件。"
            "现在只做交付收口：不要再做面上的检索与通读。\n")
    if granted:
        text += ("本次除 file_write 外还授权了执行类工具：" + "、".join(granted)
                 + "。若验收要求的字段必须来自真实执行（启动服务、读取就绪哨兵、"
                   "发起 HTTP 请求、执行测试、记录退出码与前后状态），"
                   "必须先用这些工具真实执行一次、取到实测值，再把实测值写进文件。\n")
    text += ("严禁为了让文件存在而写入空值、占位值或未经执行的结论；"
             "确实执行不出来的条目，必须在文件中如实标注 failed/unverified 并写明原因。\n")
    return text


def _extract_response_envelope(content: str | None) -> dict[str, Any] | None:
    """Extract the optional JSON envelope from a real worker response.

    Workers are instructed to answer with a JSON envelope, but in practice
    they frequently emit free-form prose followed by a fenced ``json`` block
    (observed live: root-cause escalations with ``next_actions`` were lost
    because the whole-content ``json.loads`` failed on the prose prefix).
    Extraction order:

    1. whole content fenced with ``` (legacy strict shape);
    2. whole content as a JSON object (legacy strict shape);
    3. fenced blocks, last first — accepted only when the object carries an
       envelope key (``next_actions`` / ``public_reasoning`` / ``content`` /
       ``result``) so tool-output-like JSON is never misread as an envelope;
    4. line-initial ``{`` positions scanned with ``raw_decode`` (tolerates
       trailing text), same envelope-key requirement, last match wins.

    Returns the envelope dict, or None when the response is plain text.
    """
    candidate = (content or "").strip()
    if not candidate:
        return None

    def _loads(text: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(text)
        except (TypeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    if candidate.startswith("```"):
        obj = _loads(candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip())
        if obj is not None:
            return obj
    obj = _loads(candidate)
    if obj is not None:
        return obj

    envelope_keys = ("next_actions", "public_reasoning", "content", "result")

    def _is_envelope(obj: dict[str, Any]) -> bool:
        return any(key in obj for key in envelope_keys)

    for block in reversed(re.findall(r"```(?:json)?[ \t]*\r?\n(.*?)```", candidate, flags=re.DOTALL)):
        obj = _loads(block.strip())
        if obj is not None and _is_envelope(obj):
            return obj
    decoder = json.JSONDecoder()
    starts = [m.end() - 1 for m in re.finditer(r"^[ \t]*\{", candidate, flags=re.MULTILINE)]
    for idx in reversed(starts):
        try:
            obj, _end = decoder.raw_decode(candidate, idx)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and _is_envelope(obj):
            return obj
    return None


def _workspace_delta_for_exit(
    context: "AgentContext",
    owned_baseline: dict[str, Any] | None,
    baseline_root: Path | None,
    baseline_owned: list[str],
    budget: Any,
) -> dict[str, Any] | None:
    """P1 (DeerFlow capped-semantics port): objective owned-path delta at exit.

    Recomputes the owned-files snapshot and classifies the attempt against
    its start baseline: ``capped_with_output`` / ``capped_zero_output`` when
    a budget dimension fired, ``with_output`` / ``zero_output`` otherwise.
    The payload is stored on ``context.metadata`` (same dict object the
    orchestrator handed in) so the node record and recovery routing can read
    it, and returned for metrics attachment.  Any scan failure degrades to
    ``None`` — delta evidence must never become the reason an exit fails.
    """
    if owned_baseline is None or baseline_root is None:
        return None
    from AstralNexus.runtime.execution_store import snapshot_files
    from AstralNexus.runtime.workspace_delta import build_workspace_delta

    try:
        after = snapshot_files(baseline_root, baseline_owned)
    except OSError:
        return None
    reason = None
    if budget is not None:
        try:
            reason = budget.exhaustion_reason()
        except Exception:
            reason = None
    payload = build_workspace_delta(
        owned_baseline, after, exhausted_reason=reason, node_id=context.node_id,
    )
    try:
        context.metadata["workspace_delta"] = payload
    except Exception:
        pass
    return payload


def validation_repair_block_reason(task_execution_budget: Any) -> str | None:
    """P5#13 guard: why coder validation repair rounds cannot possibly succeed.

    Returns the budget exhaustion reason (``max_tool_calls``/``max_seconds``)
    when the execution budget is spent — a repair round would call stream_chat
    with all tools withdrawn at entry, so the model cannot modify the
    workspace while re-validation keeps failing; with drifting failure
    fingerprints the repeated-failure stall guard never trips and the default
    unlimited validation budget spins forever (r15 N-105 exec-22: 697
    exhausted events / 7.7h hang).  ``None`` means repair rounds still have
    real tool capacity and the loop may continue.
    """
    if task_execution_budget is None:
        return None
    try:
        reason = task_execution_budget.exhaustion_reason()
    except Exception:
        return None
    return str(reason) if reason else None


class BaseAgent(ABC):
    """Base class for all agents in AstralNexus."""
    
    def __init__(
        self,
        role: str = "base",
        capabilities: list[str] | None = None,
    ):
        self.role = role
        self.capabilities = capabilities or []
        self.artifacts: list[str] = []
        self.worker_profile: dict[str, Any] = {}
        self.default_tools: list[str] = []
        self.input_contract = ""
        self.output_contract = ""
        self.model_preference = ""
        self.reasoning_level = "medium"
        self.selected_model = ""
        self.context_policy: dict[str, Any] = {}
        self.model_profile: dict[str, Any] = {}
        self.logger = logging.getLogger(f"AstralNexus.agents.{role}")
    
    def log(self, message: str, level: str = "info") -> None:
        """Log a message with timestamp.
        
        Args:
            message: Message to log
            level: Log level (debug, info, warning, error)
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        full_message = f"[{timestamp}] {message}"
        
        log_func = getattr(self.logger, level, self.logger.info)
        log_func(full_message)
    
    @abstractmethod
    def execute(self, context: AgentContext) -> AgentResponse:
        """Execute the agent's task.
        
        Args:
            context: Execution context
            
        Returns:
            AgentResponse with results
        """
        pass
    
    def can_handle(self, role: str) -> bool:
        """Check if this agent can handle a specific role.
        
        Args:
            role: Role to check
            
        Returns:
            True if agent can handle this role
        """
        return self.role == role

    @staticmethod
    def _record_runtime_llm_event(context: AgentContext, event: dict[str, Any]) -> None:
        """Forward live LLM events without putting a recorder in persisted input.

        The orchestrator installs this private callback only after node input
        has been serialized.  This keeps node I/O JSON portable while making
        an in-flight provider request observable in the same trace stream.
        """
        callback = context.metadata.get("_runtime_event_callback")
        if not callable(callback):
            return
        payload = dict(event)
        payload.setdefault("node_id", context.node_id)
        callback(payload)

    @staticmethod
    def _load_project_rules(context: AgentContext) -> str:
        """Load the nearest project instructions before a coding turn.

        Rules are data supplied by the delivery workspace, not a task-specific
        prompt branch. The search is bounded to the run workspace and its
        parents. The AstralNexus source checkout may be the process cwd while a
        worker is building a different project; importing cwd rules would make
        framework checks leak into that delivery.
        """
        run_root = Path(str(context.metadata.get("run_dir", ""))).resolve()
        roots: list[Path] = []
        if run_root:
            roots.extend([run_root, *run_root.parents[:4]])
        seen: set[Path] = set()
        chunks: list[str] = []
        for root in roots:
            if root in seen:
                continue
            seen.add(root)
            for name in ("AGENTS.md", ".astralnexus.md"):
                path = root / name
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")[:50_000]
                except OSError:
                    continue
                chunks.append(f"[{path}]\n{text}")
        return "\n\n".join(chunks)

    @staticmethod
    def _run_coder_validation(
        *,
        run_root: Path,
        artifact_paths: list[str],
        rules: str,
    ) -> list[dict[str, Any]]:
        """Run deterministic post-write checks through the shared tool API."""
        from AstralNexus.tools.executor import ToolExecutor

        executor = ToolExecutor(workspace_root=run_root)
        results: list[dict[str, Any]] = []
        python_files = [
            Path(item) for item in artifact_paths
            if str(item).lower().endswith(".py")
        ]
        for path in python_files:
            relative = str(path.relative_to(run_root)) if path.is_absolute() and path.is_relative_to(run_root) else str(path)
            result = executor.execute_tool("python_exec", {
                "code": (
                    "import py_compile; "
                    f"py_compile.compile({relative!r}, doraise=True)"
                ),
                "cwd": ".",
            })
            results.append({"check": "python_compile", "target": relative, "result": result})

        if os.environ.get("ASTRALNEXUS_CODER_AUTO_TEST", "true").lower() not in {"0", "false", "no"}:
            configured_target = os.environ.get("ASTRALNEXUS_CODER_TEST_TARGET")
            targets = [configured_target] if configured_target else [
                relative for relative in ("tests", "app/tests", "deliverables/app/tests")
                if (run_root / relative).is_dir()
            ]
            for target in targets:
                result = executor.execute_tool("pytest_run", {"target": target, "cwd": "."})
                results.append({"check": "pytest", "target": target, "result": result})

        # mypy is optional: run it only when explicitly enabled or when the
        # project rules/configuration request it.  Missing mypy is reported as
        # unavailable rather than misrepresented as a passing type check.
        mypy_requested = (
            os.environ.get("ASTRALNEXUS_CODER_RUN_MYPY", "false").lower() in {"1", "true", "yes"}
            or "mypy" in rules.lower()
            or (run_root / "mypy.ini").exists()
            or (
                (run_root / "pyproject.toml").exists()
                and "mypy" in (run_root / "pyproject.toml").read_text(
                    encoding="utf-8", errors="ignore"
                ).lower()
            )
        )
        if mypy_requested and python_files:
            target = os.environ.get("ASTRALNEXUS_CODER_MYPY_TARGET", "")
            mypy_targets = [target] if target else list(dict.fromkeys(
                str(path.relative_to(run_root))
                if path.is_absolute() and path.is_relative_to(run_root) else str(path)
                for path in python_files
            ))
            target = target or ", ".join(mypy_targets)
            if importlib.util.find_spec("mypy") is None:
                # Once requested, an unavailable checker is a failed gate.
                # Recovery may repair the environment; it cannot claim a pass.
                result = {
                    "status": "failed",
                    "tool": "mypy",
                    "returncode": 1,
                    "failure_class": "optional_dependency_unavailable",
                    "stderr": "mypy is not installed in the active runtime",
                    "next_action": "install_or_enable_optional_checker",
                }
            else:
                # This is a framework-owned deterministic check, not a model
                # requested shell command. Run it through the bounded Python
                # executor so an approval policy cannot be mistaken for a code
                # defect and fed back into the coder repair loop.
                result = executor.execute_tool("python_exec", {
                    "code": (
                        "from mypy import api\n"
                        f"stdout, stderr, status = api.run({mypy_targets!r})\n"
                        "print(stdout, end='')\n"
                        "if stderr:\n    print(stderr, end='')\n"
                        "raise SystemExit(status)\n"
                    ),
                    "cwd": ".",
                })
            results.append({"check": "mypy", "target": target, "result": result})
        return results

    @staticmethod
    def _validation_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        failures = []
        for item in results:
            result = item.get("result") or {}
            if item.get("check") == "mypy":
                diagnostic = "\n".join(
                    str(result.get(key) or "") for key in ("stdout", "stderr")
                ).lower()
                # Classify environment failures for recovery without turning
                # a nonzero validation command into an accepted worker result.
                error_lines = [
                    line for line in diagnostic.splitlines()
                    if ": error:" in line
                ]
                if error_lines and all(
                    "library stubs not installed" in line
                    or "cannot find implementation or library stub" in line
                    or "skipping analyzing" in line
                    for line in error_lines
                ):
                    result["failure_class"] = "optional_dependency_unavailable"
                    result["next_action"] = "install_type_stubs_or_configure_mypy"
            if str(result.get("status", "")).lower() not in {"success", "passed", "skipped"} or int(result.get("returncode", 0) or 0) != 0:
                failures.append(item)
        return failures

    @staticmethod
    def _expected_artifacts_materialized(
        run_dir: str | Path | None,
        expected_paths: list[str] | tuple[str, ...] | None,
        *,
        not_before_ns: int | None = None,
    ) -> bool:
        """Return whether every declared physical output exists.

        This predicate is intentionally contract-driven and domain-neutral.
        It is used by the LLM transport to stop accepting redundant tool
        calls once a worker has already produced its required evidence.
        Globs are supported for contracts such as ``reports/*.json``.
        """
        if not run_dir or not expected_paths:
            return False
        root = Path(run_dir).resolve()
        def is_fresh_file(path: Path) -> bool:
            if not path.is_file():
                return False
            if not_before_ns is None:
                return True
            try:
                return path.stat().st_mtime_ns >= not_before_ns
            except OSError:
                return False

        for raw in expected_paths:
            value = str(raw).strip()
            if not value:
                continue
            path = Path(value)
            directory_contract = value.endswith(("/", "\\"))
            if path.is_absolute():
                candidates = [path]
            else:
                candidates = [root / path, root / ".delivery_staging" / path]
            if directory_contract:
                if any(
                    candidate.is_dir()
                    and any(is_fresh_file(item) for item in candidate.rglob("*"))
                    for candidate in candidates
                ):
                    continue
                return False
            if any(is_fresh_file(candidate) for candidate in candidates):
                continue
            if any(char in value for char in "*?["):
                if any(
                    is_fresh_file(Path(match))
                    for candidate in candidates
                    for match in glob.glob(str(candidate), recursive=True)
                ):
                    continue
            return False
        return True

    def try_execute_with_runtime(
        self,
        context: AgentContext,
        *,
        task_kind: str,
    ) -> AgentResponse:
        """Execute the role through the real LLM runtime.

        Offline heuristic fallback has been removed for competition delivery:
        every role execution must either return a real LLM result or an
        explicit failure that can be recovered by the orchestration layer.
        """
        from AstralNexus.runtime.model_selector import DEFAULT_MODEL, get_model_config

        validation_results: list[dict[str, Any]] = []
        validation_failures: list[dict[str, Any]] = []
        artifact_paths: set[str] = set()
        execution_started_ns = time.time_ns()
        context.metadata["execution_started_ns"] = execution_started_ns
        validation_rounds = 0
        task_execution_budget = None
        # P1 (DeerFlow workspace_changes port, doc 30): owned-path baseline
        # captured before the first tool round, so every exit (success,
        # honest failure, interruption) can book an objective delta and a
        # capped_with_output / capped_zero_output classification.
        owned_baseline: dict[str, Any] | None = None
        baseline_root: Path | None = None
        baseline_owned: list[str] = []
        rules = self._load_project_rules(context)
        if rules:
            context.metadata["project_rules"] = rules

        # Resolve the role allow-list before constructing the prompt so the
        # model sees exactly the same tools that will be sent on the wire.
        if isinstance(context.tools, list) and context.tools:
            from AstralNexus.tools.toolsets import resolve_toolset
            candidates = set(context.tools)
            role_scope = set(resolve_toolset(self.role))
            if role_scope and not context.metadata.get("worker_profile"):
                candidates &= role_scope
            selected = [entry["function"]["name"] for entry in assemble_tools(candidates=candidates, lead=False)]
            context.metadata.setdefault("tool_routing", {})
            context.metadata["tool_routing"].update({
                "role": self.role,
                "candidate_count": len(context.tools),
                "selected_tools": selected,
            })
            context.tools = selected
        prompt = self._build_runtime_prompt(context, task_kind)

        model_config = get_model_config()
        model = (
            str(context.metadata.get("model", "")).strip()
            if isinstance(context.metadata, dict)
            else ""
        ) or model_config.get("model") or DEFAULT_MODEL
        base_url = model_config.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        try:
            from AstralNexus.runtime.llm_client import LLMClient
            # Fix AA (doc 18, 2026-09-15 r13b): no output cap by default.
            # The old 8192 default was OUR ceiling, not the provider's, and it
            # was fatal rather than advisory: a worker whose answer ran long was
            # cut at finish_reason="length", the node failed outright, the
            # Governor superseded it into a new generation, and each generation
            # burned recovery churn.  Live r13b PATCH-R003-RUNTIME died this way
            # twice in a row; 25 trace events carried the truncation message.
            # Empty/unset now means the max_tokens field is omitted from the
            # request entirely, so the provider applies its own maximum.
            # NOTE: this does not make truncation impossible — the provider
            # still has a ceiling — which is why the truncation nudge (Fix Z)
            # stays.  Set ASTRALNEXUS_ROLE_MAX_TOKENS explicitly to re-impose a
            # cap; large artifacts should still be split across tool calls.
            role_max_tokens = os.environ.get("ASTRALNEXUS_ROLE_MAX_TOKENS", "").strip()
            role_temperature = os.environ.get("ASTRALNEXUS_ROLE_TEMPERATURE")
            context_policy = context.metadata.get("context_policy", {}) or {}
            is_recovery = (
                str(context.metadata.get("trigger", "")).lower() == "exception_fault"
                or task_kind == "fault_recovery_and_subgraph_replanning"
            )
            recovery_budget = context_policy.get("budget") if isinstance(context_policy, dict) else None
            if is_recovery and not recovery_budget:
                # Recovery only needs the failure fingerprint, checkpoint and
                # a bounded evidence slice.  Keep its tool-loop context below
                # the normal worker budget so repeated diagnosis cannot grow
                # into a second full task history.
                recovery_budget = 16000
            client = LLMClient(
                model=model,
                base_url=base_url,
                max_tokens=int(role_max_tokens) if role_max_tokens else None,
                temperature=float(role_temperature) if role_temperature else None,
                timeout=int(
                    os.environ.get(
                        "ASTRALNEXUS_ROLE_TIMEOUT",
                        os.environ.get("ASTRALNEXUS_LLM_TIMEOUT", "600"),
                    )
                ),
                interactive=False,
                event_callback=lambda event: self._record_runtime_llm_event(
                    context, event
                ),
                workspace_root=context.metadata.get("run_dir"),
                context_budget=int(recovery_budget) if recovery_budget else None,
            )

            client.runtime_execution_id = context.metadata.get("runtime_execution_id")
            task_budget_config = context.metadata.get("task_budget", {})
            task_execution_budget = None
            if isinstance(task_budget_config, dict):
                max_task_tool_calls = int(task_budget_config.get("max_tool_calls") or 0)
                max_task_seconds = float(task_budget_config.get("max_seconds") or 0)
                if max_task_tool_calls > 0 or max_task_seconds > 0:
                    from AstralNexus.runtime.task_execution_budget import TaskExecutionBudget

                    task_execution_budget = TaskExecutionBudget(
                        max_tool_calls=max_task_tool_calls,
                        max_seconds=max_task_seconds,
                    )

            # P1: pin the owned-path baseline before the first tool round.
            run_dir_raw = str(context.metadata.get("run_dir") or "")
            owned_declared = [
                str(item) for item in (context.metadata.get("owned_paths") or [])
                if str(item).strip()
            ]
            if run_dir_raw and owned_declared:
                from AstralNexus.runtime.execution_store import snapshot_files

                baseline_root = Path(run_dir_raw).resolve()
                baseline_owned = owned_declared
                try:
                    owned_baseline = snapshot_files(baseline_root, baseline_owned)
                except OSError:
                    owned_baseline = None

            def collect_artifacts() -> None:
                root = Path(str(context.metadata.get("run_dir", ""))).resolve()
                # Reused component files must reach validation and handoff too.
                for owned in context.metadata.get("owned_paths", []) or []:
                    candidate = (root / str(owned)).resolve()
                    if not candidate.is_relative_to(root):
                        continue
                    candidates = candidate.rglob("*") if candidate.is_dir() else [candidate]
                    for item in candidates:
                        if (item.is_file() and item.resolve().is_relative_to(root)
                                and not any(part in {"node_modules", ".git", ".venv", "__pycache__"} for part in item.parts)):
                            artifact_paths.add(str(item.resolve()))
                for raw_path in list(getattr(client, "last_tool_artifacts", []) or []):
                    path = Path(str(raw_path))
                    if path.is_file():
                        artifact_paths.add(str(path.resolve()))

            tool_definitions = []
            if isinstance(context.tools, list) and context.tools:
                tool_definitions = assemble_tools(candidates=context.tools, lead=False)
                context.metadata.setdefault("tool_routing", {})
                context.metadata["tool_routing"].update({
                    "role": self.role, "candidate_count": len(context.tools),
                    "selected_tools": [entry["function"]["name"] for entry in tool_definitions],
                })

            content = client.stream_chat([
                {
                    "role": "system",
                    "content": (
                        capability_guidance(tool_definitions) + "你是天枢 AstralNexus 的真实异构角色智能体。"
                        "必须直接完成节点任务，输出结构化、可验证、可继续传递给下游角色的结果。"
                        "严格按节点目标与验收条目实现，不增加未要求的接口、报表、历史查询组合或功能。"
                        "先完成最小完整实现并实际验证，再修复缺口。接口文档复用公共Schema，避免重复展开。"
                        "单次响应有长度限制；每次写入一个紧凑完整的文件，大文件使用 file_write mode=append 和 expected_bytes 顺序追加，"
                        "不要在单次工具调用里输出整个项目或冗长规范。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
                tools=tool_definitions or None,
                max_tool_rounds=(
                    None
                    if int(os.environ.get("ASTRALNEXUS_MAX_TOOL_ROUNDS", "0")) <= 0
                    else int(os.environ["ASTRALNEXUS_MAX_TOOL_ROUNDS"])
                ),
                workspace_root=context.metadata.get("run_dir"),
                owned_paths=context.metadata.get("owned_paths", []),
                # File existence is not completion: workers still need their
                # tools to execute tests, start services and gather evidence.
                completion_check=None,
                cancel_check=context.metadata.get("_cancel_check"),
                task_execution_budget=task_execution_budget,
            )
            collect_artifacts()
            # A model may spend its normal tool budget on discovery and then
            # return a useful narrative without satisfying the declared file
            # postcondition.  Give the same real model a narrow repair pass
            # with only file_write instead of silently accepting text as an
            # implementation artifact.  This is domain-neutral and works for
            # documents, code, data, tests, and any other declared file.
            expected_artifact_paths = [
                str(item).strip()
                for item in (context.metadata.get("expected_artifact_paths", []) or [])
                if str(item).strip()
            ]
            if expected_artifact_paths:
                existing_paths = {
                    str(Path(str(item)).resolve())
                    for item in list(getattr(client, "last_tool_artifacts", []) or [])
                    if Path(str(item)).is_file()
                }
                run_root = Path(str(context.metadata.get("run_dir", ""))).resolve()
                missing_paths = [
                    item
                    for item in expected_artifact_paths
                    if str((run_root / item).resolve()) not in existing_paths
                    and not self._expected_artifacts_materialized(
                        run_root,
                        [item],
                    )
                ]
                if missing_paths:
                    from AstralNexus.tools.registry import registry
                    if context.metadata.get("worker_profile") and "file_write" not in context.tools:
                        raise RuntimeError("Missing artifacts cannot be repaired: selected profile does not authorize file_write")
                    # Fix AQ —— 落盘修复 pass 过去只授予 file_write。
                    # 对"产物内容必须来自真实执行"的节点（启动服务、发起 HTTP、
                    # 执行 pytest、记录退出码），只给写工具意味着这一 pass 在结构上
                    # 不可能成功：它唯一能做的，就是写出一个形状正确、实测字段全空的
                    # 交付文件，让"文件存在"这个后置条件通过，而验收条款必然不通过。
                    # 线上实证 PATCH-R016-FINAL-ACCEPTANCE-RERUN 正是如此：三个文件
                    # 真实落盘并逐条登记 sha256，(a)–(g) 实测项全部 unverified。
                    # 因此这里保留节点自身已授权的执行类工具，只剥离检索类工具
                    # （检索正是上一轮把预算耗光的原因），并在提示里把"先测后写、
                    # 测不到就如实标 failed"写成硬要求。
                    repair_tool_names = artifact_repair_toolset(context.tools)
                    repair_tools = assemble_tools(candidates=repair_tool_names, lead=False)
                    repair_prompt = (
                        artifact_repair_directive(repair_tool_names)
                        + f"原始任务与验收要求：\n{prompt}\n"
                        f"上一轮执行结果：\n{content or ''}\n"
                        f"运行工作区：{run_root}\n"
                        "必须使用 file_write 工具逐一写入以下相对路径：\n"
                        + "\n".join(f"- {item}" for item in missing_paths)
                        + "\n文件内容必须直接完成节点目标，并满足验收标准。"
                        "工具返回成功后再用一句话报告已写入的路径；如果工具失败，明确报告失败。"
                    )
                    repair_content = client.stream_chat(
                        [
                            {"role": "system", "content": (
                                "你是天枢的文件交付修复器。目标是产出真实、可验证的交付文件本身，"
                                "不是让文件存在。凡是验收要求实测值的字段，先执行再落盘；"
                                "拿不到实测值就如实标 failed/unverified，不得编造或留空充数。"
                            )},
                            {"role": "user", "content": repair_prompt},
                        ],
                        tools=repair_tools or None,
                        max_tool_rounds=(
                            None
                            if int(os.environ.get("ASTRALNEXUS_ARTIFACT_REPAIR_ROUNDS", "0")) <= 0
                            else int(os.environ["ASTRALNEXUS_ARTIFACT_REPAIR_ROUNDS"])
                        ),
                        workspace_root=context.metadata.get("run_dir"),
                        owned_paths=context.metadata.get("owned_paths", []),
                        completion_check=(
                            lambda: self._expected_artifacts_materialized(
                                context.metadata.get("run_dir"),
                                missing_paths,
                                    )
                        ),
                        cancel_check=context.metadata.get("_cancel_check"),
                        task_execution_budget=task_execution_budget,
                    )
                    content = (content or "").rstrip() + "\n\n" + (repair_content or "").strip()
                    collect_artifacts()

            # Coder has a deterministic post-write gate.  The model may choose
            # tools, but it cannot choose whether code is syntactically valid
            # or whether the project's tests pass.  Failed checks are sent
            # back to the real model for repair, then the checks are repeated.
            if self.role == "coder":
                # Router contracts live in node metadata.  Keep the validation
                # repair loop independent from prompt construction so a coder
                # post-write failure cannot raise NameError after real files
                # have already been created.
                task_contract = context.metadata.get("task_contract", {})
                run_root = Path(str(context.metadata.get("run_dir", ""))).resolve()
                from AstralNexus.tools.registry import registry
                # Include declared/pre-existing Python artifacts as well as
                # files reported by file_write.  Otherwise a coder could
                # modify an existing module without the post-write gate
                # compiling that module.
                declared_paths = list(context.artifacts or []) + list(
                    context.metadata.get("expected_artifact_paths", []) or []
                )
                for raw_path in declared_paths:
                    path = Path(str(raw_path))
                    if not path.is_absolute():
                        path = run_root / path
                    if path.is_file() and path.suffix.lower() == ".py":
                        artifact_paths.add(str(path.resolve()))
                configured_validation_rounds = int(
                    os.environ.get("ASTRALNEXUS_CODER_VALIDATION_ROUNDS", "0")
                )
                max_validation_rounds = (
                    None if configured_validation_rounds <= 0 else configured_validation_rounds
                )
                validation_round = 0
                previous_failure_fingerprint = ""
                repeated_failure_count = 0
                repeated_failure_limit = max(
                    1,
                    int(os.environ.get(
                        "ASTRALNEXUS_CODER_REPEATED_FAILURE_LIMIT",
                        "3",
                    )),
                )
                while True:
                    validation_rounds += 1
                    validation_results = self._run_coder_validation(
                        run_root=run_root,
                        artifact_paths=sorted(artifact_paths),
                        rules=rules,
                    )
                    from AstralNexus.runtime.data_checks import check_sqlite_artifacts
                    validation_results.extend(check_sqlite_artifacts(
                        run_root, list(context.metadata.get("owned_paths", [])) or sorted(artifact_paths),
                    ))
                    if context.metadata.get("runtime_execution_id"):
                        from AstralNexus.runtime.execution_ledger import ExecutionLedger
                        validation_ledger = ExecutionLedger(run_root, list(context.metadata.get("owned_paths", [])), context.metadata["runtime_execution_id"])
                        for check in validation_results:
                            validation_ledger.observe({"function": {"name": "validation", "arguments": {
                                "check": check.get("check"), "target": check.get("target"),
                            }}}, check.get("result") or {})
                    validation_failures = self._validation_failures(validation_results)
                    if not validation_failures:
                        break
                    self._record_runtime_llm_event(
                        context,
                        {
                            "event_type": "coder.validation_failed",
                            "validation_round": validation_round + 1,
                            "failures": validation_failures,
                            "results": validation_results,
                        },
                    )
                    if any(
                        (item.get("result") or {}).get("failure_class")
                        == "optional_dependency_unavailable"
                        for item in validation_failures
                    ):
                        raise RuntimeError(
                            "required validation environment unavailable; "
                            "install the checker/stubs before retrying this worker"
                        )
                    # Fix P5#13 (r15 postmortem, 2026-10-02): budget
                    # exhaustion is a terminal stall for the repair loop;
                    # see validation_repair_block_reason() for the anatomy.
                    # Escalate to orchestrator recovery immediately, which
                    # re-dispatches with a fresh execution budget.
                    exhaustion_reason = validation_repair_block_reason(task_execution_budget)
                    if exhaustion_reason:
                        self._record_runtime_llm_event(
                            context,
                            {
                                "event_type": "coder.validation_stalled",
                                "validation_round": validation_round + 1,
                                "failure_fingerprint": "",
                                "repeat_count": repeated_failure_count,
                                "action": "return_to_orchestrator_recovery",
                                "reason": (
                                    "task execution budget exhausted "
                                    f"({exhaustion_reason}); repair rounds "
                                    "cannot execute tools"
                                ),
                            },
                        )
                        raise RuntimeError(
                            "coder validation cannot repair: task execution "
                            f"budget exhausted ({exhaustion_reason})"
                        )
                    if max_validation_rounds is not None and validation_round >= max_validation_rounds:
                        raise RuntimeError("coder validation repair budget exhausted")
                    failure_text = json.dumps(
                        validation_failures,
                        ensure_ascii=False,
                        default=str,
                    )[:30_000]
                    failure_fingerprint = hashlib.sha256(
                        failure_text.encode("utf-8", errors="replace")
                    ).hexdigest()[:16]
                    if failure_fingerprint == previous_failure_fingerprint:
                        repeated_failure_count += 1
                    else:
                        repeated_failure_count = 1
                    if repeated_failure_count >= repeated_failure_limit:
                        self._record_runtime_llm_event(
                            context,
                            {
                                "event_type": "coder.validation_stalled",
                                "validation_round": validation_round + 1,
                                "failure_fingerprint": failure_fingerprint,
                                "repeat_count": repeated_failure_count,
                                "action": "return_to_orchestrator_recovery",
                            },
                        )
                        raise RuntimeError(
                            "coder validation made no semantic progress after "
                            f"{repeated_failure_count} identical failures "
                            f"({failure_fingerprint})"
                        )
                    repeated_failure_instruction = (
                        "这是与上一轮相同的失败指纹。禁止再次只重写同一文件或重复运行同一诊断；"
                        "必须重新读取失败测试和实现，明确指出契约冲突，采用不同的实现方案并实际修改代码。"
                        if failure_fingerprint == previous_failure_fingerprint
                        else "先读取失败测试和相关实现，定位根因后进行实质修改。"
                    )
                    previous_failure_fingerprint = failure_fingerprint
                    acceptance_criteria: list[str] = []
                    if isinstance(task_contract, dict):
                        acceptance_criteria = task_contract.get("acceptance_criteria", []) or []
                    expected_paths_text = "\n".join(
                        f"- {item}" for item in (context.metadata.get("expected_artifact_paths", []) or [])
                    ) or "- 无"
                    repair_tools = assemble_tools(
                        candidates={
                            "file_read", "file_write", "terminal_exec",
                            "python_exec", "pytest_run",
                        },
                        lead=False,
                    )
                    repair_prompt = (
                        "代码验收未通过。你必须修复真实工作区中的代码，然后让验证重新通过。\n"
                        f"验证轮次：{validation_round + 1}/"
                        f"{'unlimited' if max_validation_rounds is None else max_validation_rounds}\n"
                        f"工作区：{run_root}\n"
                        f"项目规则：{rules or '无额外规则'}\n"
                        f"当前节点验收标准：{acceptance_criteria or '无额外标准'}\n"
                        "当前节点必须落盘的相对路径：\n"
                        f"{expected_paths_text}\n"
                        f"失败检查：{failure_text}\n"
                        f"失败指纹：{failure_fingerprint}\n"
                        f"修复进展要求：{repeated_failure_instruction}\n"
                        "先读取相关文件和错误输出，使用 file_write/terminal_exec/python_exec/pytest_run "
                        "完成修复和验证。不要只解释原因；修复后必须实际运行检查。\n"
                        "节点验收标准优先于模型临时生成的示例断言。若测试断言与节点验收标准冲突，"
                        "先识别并修正冲突来源，不能在产品代码和测试之间来回振荡；不得为了掩盖真实缺陷而放宽断言。\n"
                        "验证规则：工具返回失败就是失败证据，不得在报告中声称成功；不要反复安装可选依赖（例如 mypy），"
                        "若明确要求的检查不可用，保留失败并交由恢复流程修复环境，不得用其他检查替代后宣称通过。"
                        "HTML/JSON/SQL 等结构检查必须使用解析器、DOM/正则或实际运行验证，不要用带反斜杠的转义字符串"
                        "制造脆弱的字面量断言，也不得修改测试来掩盖产品缺陷。"
                        "当前运行环境是 Windows：使用 dir、type、where、python -m 和 pytest；"
                        "不要使用 ls、cat、grep、bash 或未经验证的 Node/npm 命令。"
                    )
                    repair_content = client.stream_chat(
                        [
                            {"role": "system", "content": "你是天枢 Coder 的代码修复回路。"},
                            {"role": "user", "content": repair_prompt},
                        ],
                        tools=repair_tools or None,
                        max_tool_rounds=(
                            None
                            if int(os.environ.get("ASTRALNEXUS_CODER_REPAIR_TOOL_ROUNDS", "0")) <= 0
                            else int(os.environ["ASTRALNEXUS_CODER_REPAIR_TOOL_ROUNDS"])
                        ),
                        workspace_root=context.metadata.get("run_dir"),
                        owned_paths=context.metadata.get("owned_paths", []),
                        cancel_check=context.metadata.get("_cancel_check"),
                        task_execution_budget=task_execution_budget,
                    )
                    content = (content or "").rstrip() + "\n\n" + (repair_content or "").strip()
                    collect_artifacts()
                    validation_round += 1
            # Real workers may return an optional JSON envelope so the Router
            # can expand the graph from evidence discovered at runtime. Keep
            # plain-text answers valid for existing roles. The envelope may be
            # embedded after free-form prose (fenced block); extraction is
            # conservative and requires envelope keys for embedded shapes.
            next_actions = []
            public_reasoning = ""
            parsed = _extract_response_envelope(content)
            if isinstance(parsed, dict):
                # This is an explicitly public execution explanation for
                # progress reporting.  It is not provider reasoning_content
                # and must never contain a private chain of thought.
                public_reasoning = str(
                    parsed.get("public_reasoning")
                    or parsed.get("rationale")
                    or parsed.get("reasoning")
                    or ""
                ).strip()[:4000]
                proposed = parsed.get("next_actions", [])
                if isinstance(proposed, list):
                    for item in proposed:
                        if not isinstance(item, dict):
                            continue
                        action_type = str(item.get("type", "spawn")).strip()
                        target = str(
                            item.get("target") or item.get("goal") or ""
                        ).strip()
                        reason = str(item.get("reason") or "runtime evidence").strip()
                        if action_type not in {"spawn", "revise", "verify", "finish"}:
                            action_type = "spawn"
                        if target:
                            entry: dict[str, Any] = {
                                "type": action_type,
                                "target": target,
                                "goal": str(item.get("goal") or target).strip(),
                                "reason": reason,
                            }
                            # Preserve the routing fields Governor admission
                            # needs to derive an actionable repair node:
                            # role selects the worker profile, dependencies
                            # wire the graph, owned_paths/expected_outputs
                            # give the repair node write authority over the
                            # root-cause files (still gated by the Governor
                            # ownership validation).
                            role = str(item.get("role") or "").strip()
                            if role:
                                entry["role"] = role[:64]
                            deps = [
                                str(dep).strip()
                                for dep in (item.get("dependencies") or [])
                                if str(dep).strip()
                            ]
                            if deps:
                                entry["dependencies"] = deps[:16]
                            for extra_key in (
                                "acceptance_criteria",
                                "owned_paths",
                                "expected_outputs",
                            ):
                                extra = item.get(extra_key)
                                if isinstance(extra, list):
                                    cleaned = [
                                        str(value).strip()
                                        for value in extra
                                        if str(value).strip()
                                    ]
                                    if cleaned:
                                        entry[extra_key] = cleaned[:32]
                            next_actions.append(entry)
                content = str(parsed.get("content") or parsed.get("result") or content)
        except Exception as exc:
            failed_budget = task_execution_budget.snapshot() if task_execution_budget is not None else None
            # P1: book the objective owned-path delta on EVERY exception exit
            # before classifying — an interrupted or failed attempt that had
            # already landed bytes must stay visible as capped_with_output /
            # with_output for recovery routing.
            workspace_delta = _workspace_delta_for_exit(
                context, owned_baseline, baseline_root, baseline_owned,
                task_execution_budget,
            )
            if workspace_delta is not None:
                self._record_runtime_llm_event(
                    context, {"event_type": "task.workspace_delta", **workspace_delta},
                )
            from AstralNexus.core.exceptions import InterruptionRequested
            if isinstance(exc, InterruptionRequested):
                self.log("Real LLM role execution cooperatively interrupted", "info")
                return AgentResponse(
                    outcome=ExecutionOutcome.INTERRUPTED,
                    content="Node execution interrupted; durable state is ready for resume.",
                    metrics={
                        "runtime_status": "interrupted",
                        "execution_path": "cooperative_interrupt",
                        "task_execution_budget": failed_budget,
                        "workspace_delta": workspace_delta,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            retry_events = list(getattr(locals().get("client"), "last_retry_events", []) or [])
            request_stats = dict(getattr(locals().get("client"), "last_request_stats", {}) or {})
            # Fix AI (doc 18, 2026-09-15 r13b): this handler flattened EVERY
            # escaping exception into FAILED, so a transport cut and "the model
            # got the task wrong" became the same verdict.  A node killed this
            # way burns a recovery generation and recovery churn, and when the
            # provider is flaky the identical transport failure recurs until
            # the no-progress breaker fires -- which is what ended both
            # generation 13 (SSL: UNEXPECTED_EOF_WHILE_READING) and generation
            # 14 (SSL EOF, then a read timeout) on PATCH-R005-RUNTIME.
            #
            # A transport cut is not evidence about the node's work.  Every
            # bounded retry below this point (stream reconnect, then turn
            # retry) is already spent, so the honest classification is
            # INTERRUPTED: the Orchestrator returns the node to ready without
            # manufacturing a recovery branch.  It is NOT unbounded -- the
            # Orchestrator counts these per node and converts to FAILED once
            # the budget is spent, so a genuine outage still reaches the
            # breaker instead of being papered over.
            # A circuit-open rejection belongs in the same class, and more
            # clearly so: the gate refuses BEFORE the request leaves this
            # machine, so the node never had a turn at all.  Live gen 14:
            # "provider circuit open for ...; retry in 26.0s" was recorded as
            # PATCH-R005-RUNTIME failing, on a request that was never sent.
            # Fix AM: classify through the __cause__ chain -- a retry loop that
            # re-raises (LLMClientError wrapping ProviderCircuitOpenError) would
            # otherwise defeat the isinstance test.
            from AstralNexus.runtime.llm_client import is_transport_interruption
            if is_transport_interruption(exc):
                message = f"Real LLM role execution interrupted by transport failure: {exc}"
                self.log(message, "warning")
                return AgentResponse(
                    outcome=ExecutionOutcome.INTERRUPTED,
                    content=message,
                    metrics={
                        "runtime_status": "interrupted",
                        "execution_path": "runtime_transport_interrupted",
                        "transport_interrupted": True,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "retry_events": retry_events,
                        "request_stats": request_stats,
                        "task_execution_budget": failed_budget,
                        "workspace_delta": workspace_delta,
                    },
                )
            msg = f"Real LLM role execution failed: {exc}"
            self.log(msg, "warning")
            return AgentResponse(
                outcome=ExecutionOutcome.FAILED,
                content=msg,
                artifacts=[],
                metrics={
                    "runtime_status": "failed",
                    "execution_path": "runtime_llm_failed",
                    "task_execution_budget": failed_budget,
                    "workspace_delta": workspace_delta,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "retry_events": retry_events,
                    "request_stats": request_stats,
                },
                next_actions=[],
            )

        artifacts = []
        for raw_path in sorted(artifact_paths):
            path = Path(str(raw_path))
            run_dir = Path(str(context.metadata.get("run_dir", ""))) if context.metadata.get("run_dir") else None
            if run_dir and path.is_absolute():
                try:
                    path = path.resolve().relative_to(run_dir.resolve())
                except (ValueError, OSError):
                    pass
            artifacts.append(str(path))
        validation_metrics = {
            "rounds": validation_rounds,
            "results": validation_results,
            "failures": validation_failures,
        }
        task_budget_metrics = (
            task_execution_budget.snapshot()
            if task_execution_budget is not None else None
        )
        # P1: the success/validation-failure exit gets the same objective
        # delta booking as the exception exits — a budget-capped attempt
        # that still produced a submittable answer lands here, and its
        # capped_with_output classification is exactly what recovery
        # routing needs to continue instead of restarting.
        workspace_delta = _workspace_delta_for_exit(
            context, owned_baseline, baseline_root, baseline_owned,
            task_execution_budget,
        )
        if workspace_delta is not None:
            self._record_runtime_llm_event(
                context, {"event_type": "task.workspace_delta", **workspace_delta},
            )
        final_outcome = (
            ExecutionOutcome.FAILED
            if validation_failures
            else ExecutionOutcome.SUCCEEDED
        )
        return AgentResponse(
            outcome=final_outcome,
            content=content,
            artifacts=list(dict.fromkeys(artifacts)),
            metrics={
                "runtime_status": "succeeded",
                "final_response_chars": len(content or ""),
                "execution_path": "runtime_llm_direct",
                "model": model,
                "request_stats": dict(getattr(client, "last_request_stats", {}) or {}),
                "task_execution_budget": task_budget_metrics,
                "workspace_delta": workspace_delta,
                "code_validation": validation_metrics,
                "public_reasoning": public_reasoning,
            },
            next_actions=next_actions,
        )

    def _build_runtime_prompt(self, context: AgentContext, task_kind: str) -> str:
        """Build a structured role prompt for the runtime backend."""
        constraints = "\n".join(f"- {item}" for item in context.constraints) or "- 无"
        tools = "\n".join(f"- {item}" for item in context.tools) or "- 无"
        artifacts = "\n".join(f"- {item}" for item in context.artifacts) or "- 无"
        metadata = context.metadata or {}
        from AstralNexus.runtime.worker_reference_view import project_worker_references
        metadata = project_worker_references(
            metadata, Path(str(metadata.get("run_dir") or ".")).resolve(),
        )
        task_contract = metadata.get("task_contract", {})
        from AstralNexus.runtime.acceptance_checks import render_execution_contract
        test_execution_contract = render_execution_contract(
            metadata.get("acceptance_criteria")
            or (task_contract.get("acceptance_criteria", []) if isinstance(task_contract, dict) else []),
            declared=list(metadata.get("owned_paths") or [])
            + list(context.resources.get("upstream_artifacts") or []),
            root=Path(str(metadata.get("run_dir") or ".")).resolve(),
        )
        input_contract = metadata.get("input_contract", "")
        output_contract = metadata.get("output_contract", "")
        context_policy = metadata.get("context_policy", {})
        project_rules = metadata.get("project_rules", "")
        expected_outputs = metadata.get("expected_outputs", []) or []
        expected_artifact_paths = metadata.get("expected_artifact_paths", []) or []
        run_root = Path(str(metadata.get("run_dir") or ".")).resolve()
        owned_paths = metadata.get("owned_paths", []) or []
        process_cwd = run_root
        from AstralNexus.core.skill_runtime import runtime_skill_context
        skill_context = runtime_skill_context(metadata, context.goal, self.role)

        # Re-dispatch anti-loop feedback (live r11 resume #10): three
        # identical no-progress-guard deaths of a backend takeover node
        # proved a re-armed worker that cannot see its predecessor's
        # failure receipt repeats the same dead loop.  Surface the receipt
        # plus explicit change-of-approach directives, including service
        # ownership: acceptance criteria demanding live endpoint probes
        # mean THIS worker must start the service (background mode) before
        # probing; old terminal logs never prove a service is up now.
        previous_failure = metadata.get("previous_attempt_failure") or {}
        rework_block = ""
        if isinstance(previous_failure, dict) and str(previous_failure.get("message") or ""):
            rework_block = (
                "上一次尝试失败回执（本节点为重试/接管再派发，必读）：\n"
                f"- 回执来源节点：{previous_failure.get('source_node_id') or context.node_id}\n"
                f"- 失败信息：{previous_failure.get('message')}\n"
                f"- 错误类型：{previous_failure.get('error_type') or '未知'}\n"
                f"- 执行路径：{previous_failure.get('execution_path') or '未知'}\n"
                "反循环强制指令：上次执行因重复相同动作且证据无变化被运行时守卫终止。"
                "禁止原样重复上次失败的动作序列；先根据失败回执补齐缺失的前置条件，再重新探测。"
                "若失败信息显示对服务端点的探测被拒绝（连接失败/无监听），说明当前没有服务在运行："
                "验收条款要求的服务由你负责启动——用 terminal_exec 以后台方式（background=true）"
                "启动服务进程（或运行项目启动脚本），轮询确认端口监听成功后再执行实测并保留输出回执。"
                "历史终端日志与进程注册表仅供参考，不代表当前有服务在运行；一切以当前实测为准。\n"
                "前提再验证（fix 16）：动手执行修复类目标前，先用一次实测核实目标所述故障前提在当前环境是否仍然成立。"
                "若实测证明环境已满足目标状态（例如目标要求'修复服务启动失败'，但健康端点探测已返回正常响应、端口已在监听），"
                "则前提已失效：不要重复修复动作，也不要反复执行同一探测等待不同结果；"
                "立即把本次新鲜实测输出（命令、时间戳、响应码与响应体）写入节点要求的证据路径，"
                "在总结中说明'前提已失效：环境已满足目标状态'并附最新回执，然后提交结果。"
                "前提失效加当前真实回执是合法完成路径，验证器将按新鲜实测证据判定；"
                "若前提仍成立，按上述反循环指令更换方案执行修复。\n"
                "确定性不合规（fix 21）：若对验证/测试类目标的多轮实测返回确定性地相同、"
                "且不符合验收预期的结果（例如预期 403/400/409 实际稳定返回 500，或预期字段确定不存在），"
                "这不是靠重试能改变的——这是被测对象不合规的证据。不要重复同一探测等待不同结果；"
                "立即把实测原样写入回执路径（命令、时间戳、实际状态码与响应体），"
                "在总结中如实说明'确定性不合规'及具体差距，然后提交结果，"
                "由验证者把根因路由给上游责任人。写出不合规回执并如实提交是合法完成路径；"
                "反复探测只会触发无进展强杀，让根因无法进入上游修复。\n\n"
            )

        # P3 (DeerFlow durable-context port, doc 30): digest-stamped pointers
        # to the lineage's persisted submission receipts. Live r13c: four
        # successive recovery workers each rebuilt their predecessors'
        # diagnostics from scratch (N-126: 104 minutes / 198 calls
        # re-verifying findings already on disk, then died at the wall).
        # The ledger makes citing cheaper than re-deriving.
        ledger_block = ""
        diagnostic_ledger = metadata.get("diagnostic_ledger") or []
        if diagnostic_ledger:
            from AstralNexus.runtime.diagnostic_ledger import (
                render_diagnostic_ledger_block,
            )
            ledger_block = render_diagnostic_ledger_block(diagnostic_ledger)

        acceptance_scope_prompt = (
            "本轮判定范围是执行与呈现。模型生成的节点业务条款用于诊断，不是另一个完整业务验收门。"
            "执行必要的真实命令，提交实际结果和未满足项；不要为条款占位符拼写或报告措辞反复调查。"
            "完整业务目标由显式 Goal 独立判断。不得声称未验证成功，失败的实际验证必须修复或如实上报。"
            "声明产物必须落盘，写入归属、执行预算和历史保全要求仍然生效。\n"
            if metadata.get("acceptance_scope") == "execution" else ""
        )
        return (
            acceptance_scope_prompt + f"你是天枢 AstralNexus 的异构角色智能体：{self.role}。\n"
            f"任务类型：{task_kind}\n"
            f"节点ID：{context.node_id}\n"
            f"目标：{context.goal}\n\n"
            f"约束：\n{constraints}\n\n"
            f"允许工具：\n{tools}\n\n"
            f"运行工作区（file_read/file_write 相对路径基准）：{run_root.as_posix()}\n"
            f"允许写入路径（相对于运行工作区）：{owned_paths}\n"
            f"python_exec/terminal_exec 默认工作目录：{process_cwd.as_posix()}\n"
            "所有工具省略cwd时采用同一运行根目录。若显式设置cwd为应用子目录，命令内路径应相对于该子目录，不要重复添加目录前缀。"
            "读取上游文件可先使用 file_read，Python 解析读取到的真实内容。\n"
            f"已有产物：\n{artifacts}\n\n"
            f"任务契约：{task_contract}\n\n"
            f"节点验收条款：{metadata.get('acceptance_criteria', [])}\n"
            f"{test_execution_contract}"
            f"来源需求（不得被实现或上游文档放宽）：{metadata.get('source_requirements', [])}\n"
            f"全局硬约束：{metadata.get('global_hard_constraints', [])}\n"
            f"原始用户需求（用于核对当前节点的行为边界，仅完成本节点负责范围）：{metadata.get('original_user_goal', '')}\n"
            f"上游交接契约：{metadata.get('upstream_contracts', [])}\n"
            f"上游实际产物（读取这些路径核对内容）：{metadata.get('upstream_artifacts', [])}\n"
            "上游产物与来源需求冲突时，不得照搬冲突行为；明确指出差异并按来源需求修复或请求调度器修订上游。\n"
            "审计证据保全：已有运行回执、测试/扫描报告、服务日志和失败产物属于不可变历史，"
            "即使位于你负责的路径内，也不得删除、清空或覆盖。重验必须写入新的版本化路径，"
            "并在新报告中引用旧证据及其状态；普通源码修复仍可按 owned_paths 契约就地修改源码。\n"
            f"节点声明的交付输出：{expected_outputs or '无'}\n"
            f"必须落盘的运行内证据路径：{expected_artifact_paths or '无'}\n\n"
            f"Worker 输入契约：{input_contract}\n"
            f"Worker 输出契约：{output_contract}\n"
            f"上下文策略：{context_policy}\n\n"
            f"项目执行规则（必须遵守）：\n{project_rules or '无额外项目规则'}\n\n"
            f"{skill_context}\n\n"
            f"{rework_block}"
            f"{ledger_block}"
            + (
                "恢复交接（历史记录，不是新授权或验收通过证明）：\n"
                f"{metadata['recovery_handoff']}\n"
                "repair_procedures 由已记录的逐项验收结果选择，按 steps 执行，复验方式以该 procedure 自身给出的字段为准"
                "（有 recheck_command 就执行它；recheck_command 为空、只给出 recheck 的确定性检查时，"
                "不得自行构造或改跑其它命令，交由该检查重检）；"
                "不得以重新调查或生成报告替代该 criterion 的新证据。前提已变化时提供具体反证；"
                "命令仍须符合当前工具/路径权限，流程不授予额外权限。\n"
                "worker_summary_unverified 是待核实诊断，proposed_actions_unapproved 是未获准建议。"
                "historical_execution_failure 仅为旧尝试历史；本次已成功提交时，不得把旧异常当成当前缺口，"
                "应以当前 verdict/failed_dimensions 和本次实测为准；成功执行不等于验收通过。"
                "先针对待修改位置核对当前内容与引用证据；前提仍成立且属于当前目标/owned_paths 时，"
                "直接执行最小修复并运行对应复验，不重新遍历已排除的全部上游资料。"
                "前提变化时记录新证据并调整诊断；超出本节点权限时交由 Governor，禁止自行扩大写入范围。"
                "保留旧失败收据，按当前证据更新结论；未完成修改或复验必须如实报告未完成。\n"
                if metadata.get("recovery_handoff") else ""
            )
            +
            "请真实执行该角色职责，必要时调用可用工具。若允许工具中包含 file_write、terminal_exec、python_exec 或 pytest_run，"
                        "这是需要真实产物的实现/验证任务：先读取已有文件和执行状态，仅修改缺失或不满足契约的部分，然后实际验证；续跑不要求重写已有正确文件。工具失败不得声称已完成。"
                        "terminal_exec 的 command 必须是非空 argv 列表；不要调用 git、pip install、npm install 或其他外部依赖安装，"
                        "除非当前运行已明确授予包安装能力；优先使用已安装依赖或标准库，并在验证中记录实际结果。"
                        "terminal_exec 直接执行 argv，不隐式启动 shell；目录和文件操作优先使用 file_read 或 python_exec，"
                        "环境或模块错误时先核对 cwd、可执行文件路径和依赖声明，"
                        "依据具体错误修复环境；不得仅因一次环境错误重写技术栈或改变已约定的接口。"
                        "列出的运行内证据路径必须有真实内容；已有文件可以复用，但必须核对当前契约，运行证据必须对应当前实现；"
            "抽象输出标识对应的路径已经由运行时给出，不得自行改写路径或只在回答中描述。"
            "最终输出必须包含："
            "执行结论、关键证据、风险、后续动作建议。不要只输出系统运行报告。"
            "若证据暴露出当前图中缺失且确有必要的后续工作，请优先以 JSON 对象输出，"
            "字段为 content（正文）、public_reasoning（可向用户公开的阶段性执行依据摘要，"
            "只解释为何采取当前方法、依据哪些约束及下一步是什么，不得输出私有思维链）"
            "和 next_actions（对象数组，每项含 goal、role、"
            "dependencies、reason）；没有必要扩展时 next_actions 为空数组。"
        )

