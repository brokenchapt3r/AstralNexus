"""Base agent class for AstralNexus multi-agent system."""

from __future__ import annotations

import json
import hashlib
import importlib.util
import logging
import os
import time
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


@dataclass
class AgentContext:
    """Context passed to agents during execution."""
    
    node_id: str = ""
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionOutcome(str, Enum):
    """Result of one Worker execution, distinct from task lifecycle."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"


@dataclass
class AgentResponse:
    """Response from agent execution."""
    
    outcome: ExecutionOutcome = ExecutionOutcome.SUCCEEDED
    content: str = ""
    artifacts: list[str] = field(default_factory=list)
    next_actions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExecutionOutcome):
            self.outcome = ExecutionOutcome(str(self.outcome))


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

        tests_root = run_root / "tests"
        if tests_root.is_dir() and os.environ.get("ASTRALNEXUS_CODER_AUTO_TEST", "true").lower() not in {"0", "false", "no"}:
            target = os.environ.get("ASTRALNEXUS_CODER_TEST_TARGET", "tests")
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
            if not target:
                target = str(python_files[0])
            if importlib.util.find_spec("mypy") is None:
                # Type checking is optional.  Preserve the missing capability
                # in the validation evidence, but do not send a non-product
                # environment deficiency through the coder repair loop.
                result = {
                    "status": "skipped",
                    "tool": "mypy",
                    "returncode": 0,
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
                        f"stdout, stderr, status = api.run([{target!r}])\n"
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
            if str(result.get("failure_class", "")).lower() in {
                "optional_dependency_unavailable",
            }:
                continue
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
    ) -> AgentResponse | None:
        """Execute the role through the real LLM runtime.

        Offline heuristic fallback has been removed for competition delivery:
        every role execution must either return a real LLM result or an
        explicit failure that can be recovered by the orchestration layer.
        """
        from AstralNexus.runtime.model_selector import get_model_config

        validation_results: list[dict[str, Any]] = []
        validation_failures: list[dict[str, Any]] = []
        artifact_paths: set[str] = set()
        execution_started_ns = time.time_ns()
        context.metadata["execution_started_ns"] = execution_started_ns
        validation_rounds = 0
        rules = self._load_project_rules(context)
        if rules:
            context.metadata["project_rules"] = rules

        # Resolve the role allow-list before constructing the prompt so the
        # model sees exactly the same tools that will be sent on the wire.
        if isinstance(context.tools, list) and context.tools:
            from AstralNexus.tools.registry import discover_builtin_tools
            from AstralNexus.tools.tool_router import select_tools
            discover_builtin_tools()
            selected = select_tools(self.role, context.goal, context.tools)
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
        ) or model_config.get("model") or "qwen3.7-max"
        base_url = model_config.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        try:
            from AstralNexus.runtime.llm_client import LLMClient
            role_max_tokens = os.environ.get("ASTRALNEXUS_ROLE_MAX_TOKENS")
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

            def collect_artifacts() -> None:
                for raw_path in list(getattr(client, "last_tool_artifacts", []) or []):
                    path = Path(str(raw_path))
                    if path.is_file():
                        artifact_paths.add(str(path.resolve()))

            tool_definitions = []
            if isinstance(context.tools, list) and context.tools:
                from AstralNexus.tools.registry import discover_builtin_tools, registry
                from AstralNexus.tools.tool_router import select_tools
                discover_builtin_tools()
                selected_tools = list(context.tools)
                # Worker profiles may advertise the whole ecosystem, but a
                # node request must carry only semantically relevant schemas.
                # Unrelated GitHub/Skill/browser schemas add prompt weight and
                # make long tool histories more likely to produce malformed
                # function.arguments at compatible gateways.  This filter is
                # capability-neutral: it keeps the core file/execution tools
                # and only restores optional ecosystems when the node asks for
                # them explicitly.
                goal_text = f"{context.goal} {task_kind}".lower()
                optional_terms = {
                    "github_repo_contents": ("github", "repository", "repo", "仓库"),
                    "skills_list": ("skill", "技能"),
                    "skill_view": ("skill", "技能"),
                    "browser_fetch": ("browser", "网页", "浏览器"),
                    "web_search": ("web", "internet", "网络", "检索", "搜索"),
                }
                selected_tools = [
                    name for name in selected_tools
                    if name not in optional_terms
                    or any(term in goal_text for term in optional_terms[name])
                ]
                if context.metadata.get("expected_artifact_paths") and "file_write" in context.tools:
                    if "file_write" not in selected_tools:
                        selected_tools.insert(0, "file_write")
                context.metadata.setdefault("tool_routing", {})
                context.metadata["tool_routing"].update({
                    "role": self.role,
                    "candidate_count": len(context.tools),
                    "selected_tools": selected_tools,
                })
                tool_definitions = registry.get_definitions(set(selected_tools), quiet=True)
            content = client.stream_chat([
                {
                    "role": "system",
                    "content": (
                        "你是天枢 AstralNexus 的真实异构角色智能体。"
                        "必须直接完成节点任务，输出结构化、可验证、可继续传递给下游角色的结果。"
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
                completion_check=(
                    lambda: self._expected_artifacts_materialized(
                        context.metadata.get("run_dir"),
                        context.metadata.get("expected_artifact_paths", []),
                        not_before_ns=execution_started_ns,
                    )
                    if context.metadata.get("expected_artifact_paths")
                    else None
                ),
                cancel_check=context.metadata.get("_cancel_check"),
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
                        not_before_ns=execution_started_ns,
                    )
                ]
                if missing_paths:
                    from AstralNexus.tools.registry import registry
                    repair_tools = registry.get_definitions({"file_write"}, quiet=True)
                    repair_prompt = (
                        "上一轮已经完成分析，但没有满足节点的真实文件后置条件。"
                        "现在只执行文件落盘修复，不要继续检索、解释或调用其他工具。\n"
                        f"运行工作区：{run_root}\n"
                        "必须使用 file_write 工具逐一写入以下相对路径：\n"
                        + "\n".join(f"- {item}" for item in missing_paths)
                        + "\n文件内容必须直接完成节点目标，并满足验收标准。"
                        "工具返回成功后再用一句话报告已写入的路径；如果工具失败，明确报告失败。"
                    )
                    repair_content = client.stream_chat(
                        [
                            {"role": "system", "content": "你是天枢的文件交付修复器。只能使用 file_write。"},
                            {"role": "user", "content": repair_prompt},
                        ],
                        tools=repair_tools or None,
                        max_tool_rounds=(
                            None
                            if int(os.environ.get("ASTRALNEXUS_ARTIFACT_REPAIR_ROUNDS", "0")) <= 0
                            else int(os.environ["ASTRALNEXUS_ARTIFACT_REPAIR_ROUNDS"])
                        ),
                        workspace_root=context.metadata.get("run_dir"),
                        completion_check=(
                            lambda: self._expected_artifacts_materialized(
                                context.metadata.get("run_dir"),
                                missing_paths,
                                not_before_ns=execution_started_ns,
                            )
                        ),
                        cancel_check=context.metadata.get("_cancel_check"),
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
                    acceptance_criteria = []
                    if isinstance(task_contract, dict):
                        acceptance_criteria = task_contract.get("acceptance_criteria", []) or []
                    expected_paths_text = "\n".join(
                        f"- {item}" for item in (context.metadata.get("expected_artifact_paths", []) or [])
                    ) or "- 无"
                    repair_tools = registry.get_definitions(
                        {
                            "file_read", "file_write", "terminal_exec",
                            "python_exec", "pytest_run",
                        },
                        quiet=True,
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
                        "若环境没有该依赖，改用当前可用的语法检查、pytest 或运行时检查，并明确记录未执行的可选检查。"
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
                        cancel_check=context.metadata.get("_cancel_check"),
                    )
                    content = (content or "").rstrip() + "\n\n" + (repair_content or "").strip()
                    collect_artifacts()
                    validation_round += 1
            # Real workers may return an optional JSON envelope so the Router
            # can expand the graph from evidence discovered at runtime. Keep
            # plain-text answers valid for existing roles.
            next_actions = []
            public_reasoning = ""
            parsed = None
            candidate = (content or "").strip()
            if candidate.startswith("```"):
                candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                parsed = json.loads(candidate)
            except (TypeError, json.JSONDecodeError):
                parsed = None
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
                            next_actions.append({
                                "type": action_type,
                                "target": target,
                                "reason": reason,
                            })
                content = str(parsed.get("content") or parsed.get("result") or content)
        except Exception as exc:
            from AstralNexus.core.exceptions import InterruptionRequested
            if isinstance(exc, InterruptionRequested):
                self.log("Real LLM role execution cooperatively interrupted", "info")
                return AgentResponse(
                    outcome=ExecutionOutcome.INTERRUPTED,
                    content="Node execution interrupted; durable state is ready for resume.",
                    metrics={
                        "runtime_status": "interrupted",
                        "execution_path": "cooperative_interrupt",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            retry_events = list(getattr(locals().get("client"), "last_retry_events", []) or [])
            request_stats = dict(getattr(locals().get("client"), "last_request_stats", {}) or {})
            msg = f"Real LLM role execution failed: {exc}"
            self.log(msg, "warning")
            return AgentResponse(
                outcome=ExecutionOutcome.FAILED,
                content=msg,
                artifacts=[],
                metrics={
                    "runtime_status": "failed",
                    "execution_path": "runtime_llm_failed",
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
        task_contract = metadata.get("task_contract", {})
        input_contract = metadata.get("input_contract", "")
        output_contract = metadata.get("output_contract", "")
        context_policy = metadata.get("context_policy", {})
        project_rules = metadata.get("project_rules", "")
        expected_outputs = metadata.get("expected_outputs", []) or []
        expected_artifact_paths = metadata.get("expected_artifact_paths", []) or []
        from AstralNexus.core.skill_runtime import runtime_skill_context
        skill_context = runtime_skill_context(metadata, context.goal, self.role)

        return (
            f"你是天枢 AstralNexus 的异构角色智能体：{self.role}。\n"
            f"任务类型：{task_kind}\n"
            f"节点ID：{context.node_id}\n"
            f"目标：{context.goal}\n\n"
            f"约束：\n{constraints}\n\n"
            f"允许工具：\n{tools}\n\n"
            f"已有产物：\n{artifacts}\n\n"
            f"任务契约：{task_contract}\n\n"
            f"节点声明的交付输出：{expected_outputs or '无'}\n"
            f"必须落盘的运行内证据路径：{expected_artifact_paths or '无'}\n\n"
            f"Worker 输入契约：{input_contract}\n"
            f"Worker 输出契约：{output_contract}\n"
            f"上下文策略：{context_policy}\n\n"
            f"项目执行规则（必须遵守）：\n{project_rules or '无额外项目规则'}\n\n"
            f"{skill_context}\n\n"
            "请真实执行该角色职责，必要时调用可用工具。若允许工具中包含 file_write、terminal_exec、python_exec 或 pytest_run，"
                        "这是需要落地产物的实现/验证任务：必须先实际写入文件并运行验证，再报告结果；工具未返回 success 时不得声称已完成。"
                        "terminal_exec 的 command 必须是非空 argv 列表；不要调用 git、pip install、npm install 或其他外部依赖安装，"
                        "除非当前运行已明确授予包安装能力；优先使用已安装依赖或标准库，并在验证中记录实际结果。"
                        "当前运行环境是 Windows：terminal_exec 优先使用 dir、type、where、python -m 和 pytest，"
                        "不要使用 ls、cat、grep、bash 或未经验证的 Node/npm 命令；若 Node 命令出现权限/模块解析错误，"
                        "应立即改用 Python 标准库实现，不要重复同一个失败命令。"
                        "如果列出了必须落盘的运行内证据路径，必须对每条路径调用 file_write 写入真实内容；"
            "抽象输出标识对应的路径已经由运行时给出，不得自行改写路径或只在回答中描述。"
            "最终输出必须包含："
            "执行结论、关键证据、风险、后续动作建议。不要只输出系统运行报告。"
            "若证据暴露出当前图中缺失且确有必要的后续工作，请优先以 JSON 对象输出，"
            "字段为 content（正文）、public_reasoning（可向用户公开的阶段性执行依据摘要，"
            "只解释为何采取当前方法、依据哪些约束及下一步是什么，不得输出私有思维链）"
            "和 next_actions（对象数组，每项含 goal、role、"
            "dependencies、reason）；没有必要扩展时 next_actions 为空数组。"
        )
