"""LLM client for AstralNexus role agents.

Provides a lightweight, standalone LLM client that calls the DashScope
API (OpenAI-compatible) directly. Used by role agents for real task execution.

Integrates with model_selector for interactive model selection at startup.
"""

from __future__ import annotations

import json
import http.client
import logging
import os
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .model_selector import (
    get_model_config,
    get_selected_model,
    load_env_file,
    prompt_model_selection,
    validate_model_config,
    AVAILABLE_MODELS,
)
from .request_sanitizer import prepare_request
from .context_compressor import ContextCompressor
from .reliability import classify_http, classify_network
from .provider_request_gate import provider_request_gate, ProviderCircuitOpenError
from AstralNexus.core.exceptions import InterruptionRequested

logger = logging.getLogger(__name__)


def _normalize_tool_calls(raw_calls: Any) -> tuple[List[Dict[str, Any]], List[str]]:
    """Keep provider tool-call messages valid before echoing them back."""
    normalized: List[Dict[str, Any]] = []
    invalid_ids: List[str] = []
    for index, raw_call in enumerate(raw_calls or []):
        call = dict(raw_call) if isinstance(raw_call, dict) else {}
        function = dict(call.get("function") or {})
        call_id = str(call.get("id") or f"tool_{index}")
        raw_arguments = function.get("arguments", {})
        valid = True
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments or "{}")
                valid = isinstance(parsed, dict)
                function["arguments"] = json.dumps(parsed if valid else {}, ensure_ascii=False)
            except (TypeError, json.JSONDecodeError):
                valid = False
                function["arguments"] = "{}"
        elif isinstance(raw_arguments, dict):
            function["arguments"] = json.dumps(raw_arguments, ensure_ascii=False)
        else:
            valid = False
            function["arguments"] = "{}"
        call["id"] = call_id
        call["function"] = function
        if not valid:
            invalid_ids.append(call_id)
        normalized.append(call)
    return normalized, invalid_ids


class LLMClient:
    """Lightweight LLM client for DashScope (Qwen) API with model selection.
    
    Calls the OpenAI-compatible endpoint directly via urllib,
    requiring no additional dependencies.
    
    Usage:
        client = LLMClient()
        response = client.chat([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"}
        ])
        print(response)
    """
    
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: int = 120,
        interactive: bool = True,
        enable_compression: bool = True,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        workspace_root: Optional[str | Path] = None,
        context_budget: Optional[int] = None,
    ):
        """Initialize LLM client.
        
        Args:
            model: Model ID (if None, uses config or prompts selection)
            api_key: API key (if None, uses ASTRALNEXUS_API_KEY env var)
            base_url: API base URL
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            timeout: Request timeout in seconds
            interactive: Whether to prompt for model selection if not configured
        """
        config = get_model_config()
        load_env_file()

        # Get API key using unified resolver
        from AstralNexus.runtime.provider_utils import resolve_api_key
        self.api_key, _ = resolve_api_key(api_key)
        
        # Get model
        if model:
            self.model = model
        else:
            # Try to get from config
            config_path = config.get("config_path")
            self.model = get_selected_model(config_path)
            
            # If not valid, prompt for selection
            if not validate_model_config(self.model):
                if interactive:
                    print("\n需要配置模型。")
                    self.model = prompt_model_selection()
                    if not self.model:
                        raise ValueError("No model selected.")
                else:
                    raise ValueError(f"Invalid model configuration: {self.model}")
        
        from AstralNexus.runtime.provider_utils import resolve_base_url
        self.base_url = resolve_base_url(base_url, config)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max(0, int(os.environ.get("ASTRALNEXUS_LLM_RETRIES", "3")))
        self.retry_backoff = max(0.1, float(os.environ.get("ASTRALNEXUS_LLM_RETRY_BACKOFF", "2")))
        self._total_tokens_used = 0
        self._total_calls = 0
        self.last_request_stats: dict[str, Any] = {}
        self.last_tool_artifacts: list[str] = []
        self.last_compression_stats: dict[str, Any] = {}
        self.last_retry_events: list[dict[str, Any]] = []
        self._enable_compression = enable_compression
        self.context_budget = context_budget
        self._event_callback = event_callback
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None and str(workspace_root).strip()
            else None
        )
        self._context_compressor: Optional[ContextCompressor] = None
        self.provider_key = self.base_url.rstrip("/")

    def _emit_event(self, event: Dict[str, Any]) -> None:
        """Publish observability events without coupling the client to TraceRecorder."""
        callback = self._event_callback
        if callback is None:
            return
        try:
            callback(dict(event))
        except Exception:
            logger.debug("LLM event callback failed", exc_info=True)

    def _get_context_compressor(self) -> ContextCompressor:
        if self._context_compressor is None:
            self._context_compressor = ContextCompressor(
                summary_client=self._build_auxiliary_client(),
            )
        return self._context_compressor

    def _build_auxiliary_client(self) -> "LLMClient":
        """Build a separate, tool-free client for historical summaries."""
        aux_model = os.environ.get("ASTRALNEXUS_AUX_MODEL", "qwen-flash")
        return LLMClient(
            model=aux_model,
            api_key=self.api_key,
            base_url=self.base_url,
            max_tokens=int(os.environ.get("ASTRALNEXUS_AUX_MAX_TOKENS", "4000")),
            temperature=0.0,
            timeout=min(self.timeout, 120),
            interactive=False,
            enable_compression=False,
        )

    def _maybe_compress_conversation(
        self,
        conversation: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        if not self._enable_compression:
            self.last_compression_stats = {"compressed": False, "disabled": True}
            return conversation
        context_length = self._effective_context_length()
        tool_chars = len(json.dumps(tools or [], ensure_ascii=False, separators=(",", ":")))
        compressor = self._get_context_compressor()
        compacted, stats = compressor.compress(
            conversation,
            context_length,
            extra_chars=tool_chars,
        )
        self.last_compression_stats = stats
        if stats.get("compressed"):
            logger.info(
                "Context compressed with auxiliary model=%s: %s",
                getattr(compressor.summary_client, "model", None), stats,
            )
            # Record context overflow event for trace
            from AstralNexus.core.governor import EventType
            if hasattr(self, '_trace_recorder') and self._trace_recorder:
                self._trace_recorder.record(EventType.CONTEXT_OVERFLOW, {
                    "before_tokens": stats.get("before_tokens_estimate"),
                    "after_tokens": stats.get("after_tokens_estimate"),
                    "compression_ratio": stats.get("compression_ratio", stats.get("reduction_ratio")),
                    "reason": "当前请求上下文达到有效压缩阈值，需要保留约束和最近工具结果并压缩历史中段",
                    "reason_codes": ["CONTEXT_COMPRESSION_TRIGGERED"],
                })
        return compacted

    def _effective_context_length(self) -> int:
        """Return the single context budget used by compression and the TUI.

        A model's provider limit and an optional local operating budget are
        different concepts.  The local budget is opt-in; a hidden 32k default
        made the UI report 4% while the compressor had already fired.
        """
        model_capacity = int(
            os.environ.get(
                "ASTRALNEXUS_CONTEXT_LENGTH",
                str(AVAILABLE_MODELS.get(self.model, {}).get("context_length", 1_000_000)),
            )
        )
        configured = self.context_budget
        if configured is None:
            raw_budget = os.environ.get("ASTRALNEXUS_CONTEXT_BUDGET")
            configured = int(raw_budget) if raw_budget else None
        if configured is not None and configured > 0:
            return min(model_capacity, configured)
        return model_capacity
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_tool_rounds: Optional[int] = None,
        workspace_root: Optional[str | Path] = None,
        completion_check: Optional[Callable[[], bool]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Send a chat completion request and return the response text.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            max_tokens: Override default max_tokens
            temperature: Override default temperature
            response_format: Optional format hint ("json" or "text")
            
        Returns:
            Response content as string
            
        Raises:
            LLMClientError: If the API call fails
        """
        url = f"{self.base_url}/chat/completions"
        if max_tool_rounds is None:
            # ``0`` means unlimited tool turns.  The product must not stop a
            # real worker merely because coding, testing, and repair took more
            # turns than an arbitrary benchmark budget.  The independent tool
            # loop guard still blocks an identical unsafe call forever.
            configured_rounds = int(
                os.environ.get("ASTRALNEXUS_MAX_TOOL_ROUNDS", "0")
            )
            max_tool_rounds = None if configured_rounds <= 0 else configured_rounds
        try:
            conversation, normalized_tools, request_stats = prepare_request(messages, tools)
        except ValueError as exc:
            raise LLMClientError(f"LLM request rejected before send: {exc}") from exc
        self.last_request_stats = request_stats
        self.last_retry_events = []
        self.last_tool_artifacts = []
        tools = normalized_tools or None
        conversation = self._maybe_compress_conversation(conversation, tools)
        # Compression changes the message set, so recompute the preflight
        # stats before constructing the wire payload.
        conversation, normalized_tools, request_stats = prepare_request(conversation, tools)
        tools = normalized_tools or None
        self.last_request_stats = {**request_stats, "compression": self.last_compression_stats}
        tool_round = 0
        consecutive_tool_failures = 0
        repair_attempted = False
        tool_round_repair_attempted = False
        completion_confirmation_requested = False
        from AstralNexus.runtime.tool_guardrails import ToolCallGuard
        tool_guard = ToolCallGuard()

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": conversation,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        effective_temperature = temperature if temperature is not None else self.temperature
        if effective_max_tokens is not None:
            payload["max_tokens"] = effective_max_tokens
        if effective_temperature is not None:
            payload["temperature"] = effective_temperature
        
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        logger.info("LLM request prepared: model=%s stats=%s", self.model, request_stats)
        # Tool rounds are successful model responses, not retry attempts.  Keep
        # their budget separate so a normal tool-using worker cannot end with
        # the misleading "retry budget exhausted" error.
        retry_attempt = 0
        # Reserve one final text-only request for a model that keeps asking
        # for tools until the round budget is exhausted.  AstralNexus-style
        # recovery asks the model to summarize from the accumulated evidence;
        # it must never turn the unfinished loop into an empty success.
        max_requests = (
            None
            if max_tool_rounds is None
            else (self.max_retries + 1) * (max(0, max_tool_rounds) + 1) + 1
        )
        request_started = time.monotonic()
        # ``timeout`` is the timeout of one provider request.  A previous
        # implementation also used it as the cumulative deadline of the whole
        # tool-using node, so successful long-running coding work eventually
        # received only a few seconds for its final model call.  Node lifetime
        # is now unlimited by default and can be bounded explicitly when an
        # operator needs a deployment SLA.
        configured_node_timeout = float(
            os.environ.get("ASTRALNEXUS_NODE_TIMEOUT", "0") or 0
        )
        deadline = (
            request_started + configured_node_timeout
            if configured_node_timeout > 0
            else None
        )
        self.last_request_stats.update({
            "model": self.model,
            "provider_key": self.provider_key,
            "request_timeout_seconds": self.timeout,
            "node_timeout_seconds": configured_node_timeout or None,
            "tool_count": len(tools or []),
            "tool_rounds": 0,
            "request_attempts": [],
            "network_errors": [],
        })
        request_index = 0
        while True:
            if cancel_check is not None and cancel_check():
                self._emit_event({
                    "event_type": "llm.interrupted",
                    "request_index": request_index,
                    "tool_round": tool_round,
                    "phase": "before_request",
                })
                raise InterruptionRequested("LLM request cancelled by interactive controller")
            request_index += 1
            request = Request(
                url,
                data=request_data,
                headers={**headers, "Connection": "close"},
                method="POST",
            )
            start_time = time.time()
            self._total_calls += 1
            node_remaining = (
                deadline - time.monotonic() if deadline is not None else None
            )
            if node_remaining is not None and node_remaining <= 0:
                self._emit_event({
                    "event_type": "llm.timeout",
                    "request_index": request_index + 1,
                    "tool_round": tool_round,
                    "timeout_seconds": configured_node_timeout,
                    "scope": "node",
                })
                raise LLMClientError(
                    f"node timeout after {configured_node_timeout}s; "
                    f"request_stats={self.last_request_stats}"
                )
            request_timeout = max(
                1.0,
                min(float(self.timeout), node_remaining)
                if node_remaining is not None
                else float(self.timeout),
            )
            try:
                self._emit_event({
                    "event_type": "llm.request",
                    "request_index": request_index + 1,
                    "tool_round": tool_round,
                    "request_timeout_seconds": round(request_timeout, 3),
                    "node_remaining_seconds": (
                        round(node_remaining, 3)
                        if node_remaining is not None
                        else None
                    ),
                    "prompt_tokens_estimate": request_stats.get("estimated_input_tokens", 0),
                    "tool_count": len(tools or []),
                    "reason": "根据当前任务上下文、节点状态和上一轮结果继续请求模型",
                    "reason_codes": ["CONTINUE_CURRENT_NODE"],
                })
                with provider_request_gate.request_slot(self.provider_key):
                    with urlopen(request, timeout=request_timeout) as response:
                        response_data = json.loads(response.read().decode("utf-8"))
                if cancel_check is not None and cancel_check():
                    self._emit_event({
                        "event_type": "llm.interrupted",
                        "request_index": request_index,
                        "tool_round": tool_round,
                        "phase": "after_response",
                    })
                    raise InterruptionRequested("LLM request cancelled after provider response")
                self.last_request_stats["request_attempts"].append({
                    "request_index": request_index + 1,
                    "elapsed_seconds": round(time.time() - start_time, 3),
                    "tool_round": tool_round,
                    "status": "response_received",
                })
                provider_request_gate.record_success(self.provider_key)
                self.last_request_stats["provider_request_gate"] = provider_request_gate.snapshot().get(
                    self.provider_key, {}
                )
                # Process successful response
                elapsed = time.time() - start_time
                choices = response_data.get("choices", [])
                if not choices:
                    raise LLMClientError("No choices in API response")
                message = choices[0].get("message", {})
                tool_calls, invalid_tool_call_ids = _normalize_tool_calls(
                    message.get("tool_calls") or []
                )
                if invalid_tool_call_ids:
                    # Do not echo malformed provider arguments into the next
                    # request; keep a protocol-valid assistant message.
                    message = dict(message)
                    message["tool_calls"] = tool_calls
                    self.last_request_stats["invalid_tool_call_ids"] = invalid_tool_call_ids
                usage = response_data.get("usage", {}) or {}
                current_attempt = self.last_request_stats["request_attempts"][-1]
                current_attempt.update({
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "tool_call_count": len(tool_calls),
                })
                self._emit_event({
                    "event_type": "llm.response",
                    "request_index": request_index + 1,
                    "tool_round": tool_round,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "tool_call_count": len(tool_calls),
                })
                if (
                    tool_calls
                    and tools
                    and (max_tool_rounds is None or tool_round < max(0, max_tool_rounds))
                ):
                    if cancel_check is not None and cancel_check():
                        raise InterruptionRequested("tool round cancelled by interactive controller")
                    from AstralNexus.tools.executor import ToolExecutor

                    conversation.append(message)
                    executor = ToolExecutor(
                        workspace_root=workspace_root or self.workspace_root,
                    )
                    admitted_calls = []
                    blocked_results = {}
                    for call in tool_calls:
                        allowed, key_or_reason = tool_guard.admit(call)
                        call_id = call.get("id", f"tool_{len(blocked_results)}")
                        if allowed:
                            admitted_calls.append(call)
                        else:
                            blocked_results[call_id] = {
                                "status": "error",
                                "error": "tool loop guard halted repeated call",
                                "guard_reason": key_or_reason,
                            }
                    executed_results = executor.execute_tool_calls(admitted_calls)
                    self._emit_event({
                        "event_type": "llm.tool_batch",
                        "request_index": request_index + 1,
                        "tool_round": tool_round,
                        "calls": [
                            {
                                "name": str((call.get("function") or {}).get("name", "")),
                                "call_id": call.get("id", ""),
                                "status": (
                                    result.get("status")
                                    if isinstance(result, dict)
                                    else "unknown"
                                ),
                                "failure_class": (
                                    result.get("failure_class")
                                    if isinstance(result, dict)
                                    else None
                                ),
                                "error": (
                                    str(result.get("error", ""))[:500]
                                    if isinstance(result, dict)
                                    else None
                                ),
                                "returncode": (
                                    result.get("returncode")
                                    if isinstance(result, dict)
                                    else None
                                ),
                                "stdout_tail": (
                                    str(result.get("stdout", ""))[-1000:]
                                    if isinstance(result, dict)
                                    else None
                                ),
                                "stderr_tail": (
                                    str(result.get("stderr", ""))[-1000:]
                                    if isinstance(result, dict)
                                    else None
                                ),
                                "path": (
                                    str(result.get("path", ""))
                                    if isinstance(result, dict) and result.get("path")
                                    else None
                                ),
                                "run_dir": (
                                    str(result.get("run_dir", ""))
                                    if isinstance(result, dict) and result.get("run_dir")
                                    else None
                                ),
                            }
                            for call, result in zip(admitted_calls, executed_results)
                        ],
                        "reason": "模型返回了可执行的工具调用，运行时按当前节点契约执行并收集证据",
                        "reason_codes": ["MODEL_SELECTED_TOOLS", "COLLECT_EXECUTION_EVIDENCE"],
                    })
                    for result in executed_results:
                        if (
                            isinstance(result, dict)
                            and result.get("status") == "success"
                            and result.get("artifact_created") is True
                        ):
                            path = result.get("path")
                            if path:
                                self.last_tool_artifacts.append(str(path))
                    executed_by_id = {
                        call.get("id", f"tool_{index}"): result
                        for index, (call, result) in enumerate(zip(admitted_calls, executed_results))
                    }
                    tool_results = []
                    force_text_only = False
                    for index, call in enumerate(tool_calls):
                        call_id = call.get("id", f"tool_{index}")
                        result = blocked_results.get(call_id, executed_by_id.get(call_id, {"status": "error", "error": "tool result missing"}))
                        tool_guard.observe(call, result)
                        decision = tool_guard.decision(call)
                        if decision["action"] != "retry_or_continue":
                            result = dict(result)
                            result.setdefault("recovery", decision)
                        # A repeated call that the guard has halted must not
                        # be sent back into the same tool-enabled loop.  Keep
                        # the evidence in the conversation, then force a
                        # text-only decision so the worker can report the
                        # blocker or choose the normal recovery path.  This
                        # is a state transition, not an artificial round cap.
                        if decision["action"] == "halt":
                            force_text_only = True
                        tool_results.append(result)
                    successful_results = [
                        result for result in tool_results
                        if isinstance(result, dict)
                        and str(result.get("status", "")).lower() == "success"
                    ]
                    failed_results = [
                        result for result in tool_results
                        if isinstance(result, dict)
                        and (
                            str(result.get("status", "")).lower() in {"error", "failed", "failure"}
                            or bool(result.get("error"))
                        )
                    ]
                    # A mixed batch can contain an optional bad command while
                    # still making real progress (for example four file
                    # writes plus one malformed terminal call).  Count a
                    # failure streak only when the whole batch made no
                    # successful progress; otherwise recovery should continue
                    # from the newly materialized evidence.
                    if successful_results:
                        consecutive_tool_failures = 0
                    elif failed_results:
                        consecutive_tool_failures += 1
                    else:
                        consecutive_tool_failures = 0
                    self.last_request_stats["consecutive_tool_failures"] = consecutive_tool_failures
                    # Do not impose a tool-round failure cap.  Different
                    # malformed commands are recoverable evidence: keep the
                    # errors in the conversation and ask the model to change
                    # the command, path, or tool.  The Governor remains free
                    # to reassign/replan, but the transport loop itself must
                    # not terminate merely because a fixed streak was hit.
                    failure_feedback = False
                    if consecutive_tool_failures >= 3:
                        failure_feedback = True
                        self.last_request_stats["tool_failure_feedback"] = consecutive_tool_failures
                        consecutive_tool_failures = 0
                    self.last_request_stats["tool_guard"] = tool_guard.snapshot()
                    for call, result in zip(tool_calls, tool_results):
                        function = call.get("function", {})
                        name = str(function.get("name", ""))
                        result_text = json.dumps(result, ensure_ascii=False)
                        max_tool_result = max(1000, int(os.environ.get("ASTRALNEXUS_TOOL_RESULT_MAX_CHARS", "24000")))
                        if len(result_text) > max_tool_result:
                            result_text = result_text[:max_tool_result] + "\n[tool result truncated; full result should be read from the artifact]"
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": call.get("id", f"tool_{tool_round}"),
                            "name": name,
                            "content": result_text,
                        })
                    if failure_feedback:
                        conversation.append({
                            "role": "user",
                            "content": (
                                "连续工具调用没有产生有效进展。请立即改变策略："
                                "只使用当前工作区中已经确认存在的路径；不要读取猜测文件，"
                                "不要调用空命令或 Windows 不兼容的命令；若目标是生成文件，"
                                "直接使用 file_write 写入最小可运行版本，然后用实际存在的测试/启动命令验证。"
                            ),
                        })
                    if invalid_tool_call_ids:
                        conversation.append({
                            "role": "user",
                            "content": (
                                "上一个工具调用的参数不是合法 JSON，系统已阻止该调用。"
                                "请停止工具调用，基于当前证据输出结果；未验证部分请明确列出。"
                            ),
                        })
                        tools = None
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        self.last_request_stats["invalid_tool_call_recovery"] = "text_only"
                    if force_text_only:
                        conversation.append({
                            "role": "user",
                            "content": (
                                "同一工具调用已被重复调用保护机制阻断。请停止继续调用该工具，"
                                "基于已经返回的证据给出真实结果；如果任务仍未完成，明确指出阻塞原因和"
                                "下一步需要由上层 Governor 重新规划的动作，不得声称未验证的文件或测试已完成。"
                            ),
                        })
                        tools = None
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        self.last_request_stats["tool_guard_recovery"] = "text_only_after_halt"
                    # A Worker-owned physical delivery contract is a stronger
                    # completion signal than the model's next tool choice.
                    # Once all declared artifacts exist, stop accepting more
                    # discovery/repair calls and request one plain-text
                    # confirmation.  This is progress-based convergence, not
                    # a round limit: it works for any domain with a predicate
                    # supplied by the caller and prevents endless re-reading
                    # after the real work is already complete.
                    if (
                        completion_check is not None
                        and not completion_confirmation_requested
                    ):
                        try:
                            contract_satisfied = bool(completion_check())
                        except Exception as exc:
                            contract_satisfied = False
                            self.last_request_stats["completion_check_error"] = str(exc)
                        if contract_satisfied:
                            completion_confirmation_requested = True
                            conversation.append({
                                "role": "user",
                                "content": (
                                    "节点的声明交付物已经全部真实落盘。"
                                    "请停止所有工具调用，只返回简短的完成确认和已验证证据；"
                                    "不要再次读取、重写或重复运行同一命令。"
                                ),
                            })
                            tools = None
                            payload.pop("tools", None)
                            payload.pop("tool_choice", None)
                            self.last_request_stats["completion_contract"] = "satisfied_text_confirmation"
                    tool_round += 1
                    retry_attempt = 0
                    self.last_request_stats["tool_rounds"] = tool_round
                    conversation = self._maybe_compress_conversation(conversation, tools)
                    conversation, normalized_tools, request_stats = prepare_request(conversation, tools)
                    tools = normalized_tools or None
                    self.last_request_stats.update({
                        **request_stats,
                        "compression": self.last_compression_stats,
                        "model": self.model,
                        "provider_key": self.provider_key,
                        "tool_count": len(tools or []),
                        "tool_rounds": tool_round,
                    })
                    request_data = json.dumps({**payload, "messages": conversation}, ensure_ascii=False).encode("utf-8")
                    continue
                if tool_calls:
                    if not tool_round_repair_attempted:
                        conversation.append(message)
                        for index, call in enumerate(tool_calls):
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": call.get("id", f"tool_{index}"),
                                "name": call.get("function", {}).get("name", "unknown"),
                                "content": json.dumps({
                                    "status": "error",
                                    "error": "tool round budget reached; no further tool calls are allowed",
                                }, ensure_ascii=False),
                            })
                        conversation.append({
                            "role": "user",
                            "content": (
                                "工具轮次已达到上限。请停止调用工具，基于已有证据直接输出最终结果；"
                                "如果证据不足，请明确标注未验证项和后续动作，不得返回空内容。"
                            ),
                        })
                        tool_round_repair_attempted = True
                        tools = None
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        request_data = json.dumps(
                            {**payload, "messages": conversation}, ensure_ascii=False
                        ).encode("utf-8")
                        self.last_request_stats["tool_round_repair"] = "text_only_summary"
                        continue
                    # Never turn an unfinished tool loop into a successful
                    # empty assistant response.  The caller must enter its
                    # normal recovery/replan path so a node cannot claim that
                    # files or tests were completed without a final result.
                    self.last_request_stats["retry_events"] = list(self.last_retry_events)
                    raise LLMClientError(
                        "model exhausted tool-round budget without a final response; "
                        f"tool_rounds={tool_round}, tool_calls={len(tool_calls)}, "
                        f"request_stats={self.last_request_stats}"
                    )
                content = message.get("content", "") or ""
                if not str(content).strip():
                    self.last_request_stats["retry_events"] = list(self.last_retry_events)
                    raise LLMClientError(
                        "model returned an empty final response; "
                        f"tool_rounds={tool_round}, request_stats={self.last_request_stats}"
                    )
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                self._total_tokens_used += prompt_tokens + completion_tokens
                logger.info(
                    "LLM call completed: model=%s, attempt=%s, prompt_tokens=%s, "
                    "completion_tokens=%s, elapsed=%.1fs",
                    self.model, request_index + 1, prompt_tokens, completion_tokens, elapsed,
                )
                self.last_request_stats["retry_events"] = list(self.last_retry_events)
                self.last_request_stats["actual_prompt_tokens"] = prompt_tokens
                self.last_request_stats["actual_completion_tokens"] = completion_tokens
                self.last_request_stats["elapsed_seconds"] = round(time.monotonic() - request_started, 3)
                return content
            except ProviderCircuitOpenError as e:
                # Convert provider circuit open to structured error with retry_after
                error_msg = str(e)
                retry_after = None
                if "retry in" in error_msg:
                    try:
                        retry_after = float(error_msg.split("retry in")[1].strip().rstrip("s"))
                    except (IndexError, ValueError):
                        pass
                self.last_request_stats["network_errors"].append({
                    "category": "provider_circuit_open",
                    "retryable": True,
                    "detail": error_msg,
                    "retry_after": retry_after,
                })
                provider_request_gate.record_failure(self.provider_key, "provider_circuit_open", True)
                if retry_attempt >= self.max_retries:
                    raise LLMClientError(
                        f"provider circuit open after {self.max_retries} retries: {error_msg}; "
                        f"request_stats={self.last_request_stats}"
                    ) from e
                retry_attempt += 1
                if retry_after:
                    time.sleep(min(retry_after, self.retry_backoff * (2 ** max(0, retry_attempt - 1))))
                else:
                    time.sleep(self.retry_backoff * (2 ** max(0, retry_attempt - 1)))
                continue

            except HTTPError as e:
                elapsed = time.time() - start_time
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8")
                except Exception:
                    pass
                invalid_parameter = e.code == 400 and any(
                    marker in error_body for marker in ("InvalidParameter", "invalid_parameter", "input length")
                )
                classification = classify_http(e.code, error_body)
                if invalid_parameter and not repair_attempted:
                    repair_attempted = True
                    # Match the mature AstralNexus recovery behavior: retain the
                    # semantic request but remove the optional schema surface
                    # first; if history is the cause, keep system + latest
                    # user turn and send a clean text-only request.
                    if tools:
                        tools = None
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                    else:
                        system = next((item for item in conversation if item.get("role") == "system"), None)
                        latest_user = next((item for item in reversed(conversation) if item.get("role") == "user"), None)
                        conversation = [item for item in (system, latest_user) if item]
                    request_data = json.dumps({**payload, "messages": conversation}, ensure_ascii=False).encode("utf-8")
                    logger.warning("Provider rejected request parameters; sent one sanitized repair request: %s", error_body[:500])
                    continue
                retryable = classification.retryable
                self.last_retry_events.append({
                    "attempt": retry_attempt + 1,
                    "category": classification.category,
                    "status": e.code,
                    "retryable": retryable,
                    "detail": error_body[:500],
                })
                provider_request_gate.record_failure(self.provider_key, classification.category, retryable)
                self.last_request_stats["provider_request_gate"] = provider_request_gate.snapshot().get(
                    self.provider_key, {}
                )
                self.last_request_stats["network_errors"].append({
                    "category": classification.category,
                    "status": e.code,
                    "detail": error_body[:500],
                })
                if not retryable or retry_attempt >= self.max_retries:
                    self._emit_event({
                        "event_type": "llm.error",
                        "request_index": request_index + 1,
                        "tool_round": tool_round,
                        "category": classification.category,
                        "error": str(e)[:500],
                        "retryable": retryable,
                    })
                    raise LLMClientError(
                        f"HTTP {e.code} from LLM API after {elapsed:.1f}s: {error_body}; "
                        f"error_category={classification.category}; request_stats={self.last_request_stats}"
                    ) from e
                self._emit_event({
                    "event_type": "llm.retry",
                    "request_index": request_index + 1,
                    "tool_round": tool_round,
                    "category": classification.category,
                    "error": str(e)[:500],
                    "attempt": retry_attempt + 1,
                    "max_retries": self.max_retries,
                })
                logger.warning("Transient HTTP %s from LLM API; retrying (%s/%s)", e.code, retry_attempt + 1, self.max_retries)
                retry_attempt += 1
            except (
                URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionError,
                http.client.RemoteDisconnected,
                json.JSONDecodeError,
            ) as e:
                elapsed = time.time() - start_time
                classification = classify_network(e)
                self.last_retry_events.append({
                    "attempt": retry_attempt + 1,
                    "category": classification.category,
                    "retryable": True,
                    "detail": str(e)[:500],
                })
                provider_request_gate.record_failure(self.provider_key, classification.category, True)
                self.last_request_stats["provider_request_gate"] = provider_request_gate.snapshot().get(
                    self.provider_key, {}
                )
                self.last_request_stats["network_errors"].append({
                    "category": classification.category,
                    "detail": str(e)[:500],
                })
                if retry_attempt >= self.max_retries:
                    detail = getattr(e, "reason", str(e))
                    self._emit_event({
                        "event_type": "llm.error",
                        "request_index": request_index + 1,
                        "tool_round": tool_round,
                        "category": classification.category,
                        "error": str(e)[:500],
                        "retryable": False,
                    })
                    raise LLMClientError(
                        f"Network error from LLM API after {elapsed:.1f}s: {detail}"
                    ) from e
                self._emit_event({
                    "event_type": "llm.retry",
                    "request_index": request_index + 1,
                    "tool_round": tool_round,
                    "category": classification.category,
                    "error": str(e)[:500],
                    "attempt": retry_attempt + 1,
                    "max_retries": self.max_retries,
                })
                logger.warning(
                    "Transient network error from LLM API (%s); retrying (%s/%s)",
                    e, retry_attempt + 1, self.max_retries,
                )
                retry_attempt += 1
            except (KeyError, LLMClientError):
                raise

            time.sleep(self.retry_backoff * (2 ** max(0, retry_attempt - 1)))

        self.last_request_stats["retry_events"] = list(self.last_retry_events)
        raise LLMClientError(
            "LLM request exhausted retry budget: "
            f"tool_rounds={tool_round}; retry_events={self.last_retry_events}; "
            f"request_stats={self.last_request_stats}"
        )
    
    def stream(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        emit_metrics: bool = False,
    ):
        """Yield OpenAI-compatible SSE delta events.

        Streaming is intentionally a separate API from ``chat``.  Tool-loop
        orchestration remains owned by ``chat`` until a caller explicitly
        chooses an event-driven runtime.
        """
        url = f"{self.base_url}/chat/completions"
        conversation, normalized_tools, request_stats = prepare_request(messages, tools)
        conversation = self._maybe_compress_conversation(conversation, normalized_tools)
        conversation, normalized_tools, request_stats = prepare_request(conversation, normalized_tools)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": conversation,
            "stream": True,
        }
        if normalized_tools:
            payload["tools"] = normalized_tools
            payload["tool_choice"] = "auto"
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        effective_temperature = temperature if temperature is not None else self.temperature
        if effective_max_tokens is not None:
            payload["max_tokens"] = effective_max_tokens
        if effective_temperature is not None:
            payload["temperature"] = effective_temperature
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Connection": "close",
            },
            method="POST",
        )
        self.last_request_stats = {
            **request_stats,
            "model": self.model,
            "stream": True,
            "tool_count": len(normalized_tools or []),
        }
        def emit(event: Dict[str, Any]) -> None:
            callback = event_callback or self._event_callback
            if callback is not None:
                try:
                    callback(dict(event))
                except Exception:
                    logger.debug("LLM stream event callback failed", exc_info=True)

        emit({
            "event_type": "llm.stream.started",
            "model": self.model,
            "provider_key": self.provider_key,
            "tool_count": len(normalized_tools or []),
        })
        context_total = self._effective_context_length()
        if emit_metrics:
            emit({
                "event_type": "llm.context",
                "estimated_input_tokens": request_stats.get("estimated_input_tokens", 0),
                "context_total": context_total,
                "compressed": bool(self.last_compression_stats.get("compressed")),
                "compression": self.last_compression_stats,
                "effective_context_budget": context_total,
                "reason": self.last_compression_stats.get(
                    "reason", "当前请求尚未达到上下文压缩阈值"
                ),
            })
        max_stream_retries = max(0, int(os.environ.get("ASTRALNEXUS_STREAM_RETRIES", "2")))
        stream_attempt = 0
        stream_usage: Dict[str, Any] = {}
        while True:
            try:
                with provider_request_gate.request_slot(self.provider_key):
                    with urlopen(request, timeout=self.timeout) as response:
                        for raw_line in response:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                self.last_request_stats["stream_usage"] = dict(stream_usage)
                                emit({
                                    "event_type": "llm.stream.completed",
                                    "model": self.model,
                                    "attempt": stream_attempt + 1,
                                })
                                return
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            usage = event.get("usage") or {}
                            if usage:
                                stream_usage.update(usage)
                                self.last_request_stats.update({
                                    "actual_prompt_tokens": usage.get("prompt_tokens", 0),
                                    "actual_completion_tokens": usage.get("completion_tokens", 0),
                                })
                                if emit_metrics:
                                    emit({
                                        "event_type": "llm.usage",
                                        "prompt_tokens": usage.get("prompt_tokens", 0),
                                        "completion_tokens": usage.get("completion_tokens", 0),
                                    })
                            choices = event.get("choices", [])
                            delta = choices[0].get("delta", {}) if choices else {}
                            if delta.get("tool_calls"):
                                emit({
                                    "event_type": "llm.stream.tool_call_delta",
                                    "model": self.model,
                                    "tool_calls": delta["tool_calls"],
                                })
                            # The parsed SSE event is yielded to stream_chat,
                            # which emits the user-visible text delta. Mirroring
                            # every raw provider envelope into trace duplicates
                            # the stream and floods the control-plane timeline.
                            yield event
                provider_request_gate.record_success(self.provider_key)
                return
            except Exception as exc:
                provider_request_gate.record_failure(self.provider_key, "network", True)
                if stream_attempt >= max_stream_retries:
                    emit({
                        "event_type": "llm.stream.failed",
                        "model": self.model,
                        "error": str(exc)[:500],
                        "attempts": stream_attempt + 1,
                    })
                    raise
                stream_attempt += 1
                emit({
                    "event_type": "llm.stream.reconnected",
                    "model": self.model,
                    "attempt": stream_attempt + 1,
                    "error": str(exc)[:500],
                })
                time.sleep(self.retry_backoff * (2 ** (stream_attempt - 1)))

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_tool_rounds: Optional[int] = None,
        workspace_root: Optional[str | Path] = None,
        completion_check: Optional[Callable[[], bool]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Run a streaming chat/tool loop and return the complete answer.

        ``stream()`` is the transport primitive: it yields provider SSE
        events for one request.  AstralNexus-style interactive behavior needs one
        layer above that primitive because a streamed assistant message can
        finish with tool calls.  This method owns that continuation protocol:

        assistant deltas -> completed tool calls -> ToolExecutor -> tool
        messages -> next streamed request.

        Keeping this separate from ``chat()`` preserves the existing
        synchronous orchestrator path while giving the interactive CLI a
        real token stream and real tool execution rather than status-only
        printing.
        """
        from AstralNexus.tools.executor import ToolExecutor
        from AstralNexus.runtime.tool_guardrails import ToolCallGuard

        conversation: List[Dict[str, Any]] = [dict(message) for message in messages]
        working_tools = tools
        tool_round = 0
        full_text: List[str] = []
        tool_guard = ToolCallGuard()
        completion_confirmation_requested = False
        self.last_tool_artifacts = []

        def emit(event: Dict[str, Any]) -> None:
            callback = event_callback or self._event_callback
            if callback is not None:
                try:
                    callback(dict(event))
                except Exception:
                    logger.debug("LLM stream-chat event callback failed", exc_info=True)

        while True:
            if cancel_check is not None and cancel_check():
                emit({
                    "event_type": "llm.interrupted",
                    "phase": "before_stream_request",
                    "tool_round": tool_round,
                })
                raise InterruptionRequested("LLM stream chat cancelled")

            streamed_text: List[str] = []
            pending: Dict[int, Dict[str, Any]] = {}
            for event in self.stream(
                conversation,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                tools=working_tools,
                event_callback=event_callback,
                emit_metrics=True,
            ):
                choices = event.get("choices", [])
                delta = choices[0].get("delta", {}) if choices else {}
                content = delta.get("content")
                if content:
                    text = str(content)
                    streamed_text.append(text)
                    full_text.append(text)
                    emit({
                        "event_type": "llm.text.delta",
                        "text": text,
                        "tool_round": tool_round,
                    })
                for raw_call in delta.get("tool_calls") or []:
                    index = int(raw_call.get("index", len(pending)))
                    call = pending.setdefault(index, {
                        "id": f"stream_tool_{index}",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if raw_call.get("id"):
                        call["id"] = raw_call["id"]
                    function = raw_call.get("function") or {}
                    if function.get("name"):
                        call["function"]["name"] += str(function["name"])
                    if function.get("arguments"):
                        call["function"]["arguments"] += str(function["arguments"])

            tool_calls, invalid_ids = _normalize_tool_calls(
                [pending[index] for index in sorted(pending)]
            )
            if not tool_calls:
                answer = "".join(full_text).strip()
                if not answer:
                    raise LLMClientError(
                        "model returned an empty streamed response; "
                        f"tool_rounds={tool_round}; request_stats={self.last_request_stats}"
                    )
                return answer

            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": "".join(streamed_text) or None,
                "tool_calls": tool_calls,
            }
            conversation.append(assistant_message)
            executor = ToolExecutor(workspace_root=workspace_root or self.workspace_root)
            admitted = []
            blocked: Dict[str, Dict[str, Any]] = {}
            for index, call in enumerate(tool_calls):
                allowed, reason = tool_guard.admit(call)
                call_id = call.get("id", f"stream_tool_{index}")
                if allowed:
                    admitted.append(call)
                else:
                    blocked[call_id] = {
                        "status": "error",
                        "error": "tool loop guard halted repeated call",
                        "guard_reason": reason,
                    }
            emit({
                "event_type": "llm.tool_execution.start",
                "tool_round": tool_round,
                "reason": "模型选择这些工具来推进当前节点，并取得下一步所需的文件、命令或外部证据",
                "reason_codes": ["MODEL_SELECTED_TOOLS", "ADVANCE_NODE"],
                "calls": [
                    {
                        "name": str((call.get("function") or {}).get("name", "")),
                        "call_id": str(call.get("id") or ""),
                        "arguments": (call.get("function") or {}).get("arguments", {}),
                    }
                    for call in admitted
                ],
            })
            executed = executor.execute_tool_calls(admitted)
            executed_by_id = {
                call.get("id", f"stream_tool_{index}"): result
                for index, (call, result) in enumerate(zip(admitted, executed))
            }
            calls_for_event = []
            force_text_only = False
            for index, call in enumerate(tool_calls):
                call_id = call.get("id", f"stream_tool_{index}")
                result = blocked.get(call_id, executed_by_id.get(call_id, {
                    "status": "error",
                    "error": "tool result missing",
                }))
                result = result if isinstance(result, dict) else {"output": str(result)}
                tool_guard.observe(call, result)
                decision = tool_guard.decision(call)
                if decision["action"] == "halt":
                    force_text_only = True
                calls_for_event.append({
                    "name": str((call.get("function") or {}).get("name", "")),
                    "call_id": call.get("id", ""),
                    "status": result.get("status", "unknown"),
                    "error": str(result.get("error", ""))[:500] if result.get("error") else None,
                    "path": str(result.get("path", "")) if result.get("path") else None,
                    "returncode": result.get("returncode"),
                    "stdout_tail": str(
                        result.get("stdout") or result.get("output") or ""
                    )[-2000:],
                    "stderr_tail": str(result.get("stderr") or "")[-1000:],
                })
                if (
                    result.get("status") == "success"
                    and result.get("path")
                    and result.get("artifact_created") is True
                ):
                    self.last_tool_artifacts.append(str(result["path"]))
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": str((call.get("function") or {}).get("name", "")),
                    "content": json.dumps(result, ensure_ascii=False),
                })
            emit({
                "event_type": "llm.tool_execution.done",
                "tool_round": tool_round,
                "calls": calls_for_event,
            })
            emit({
                "event_type": "llm.tool_batch",
                "tool_round": tool_round,
                "calls": calls_for_event,
            })
            if completion_check is not None and not completion_confirmation_requested:
                try:
                    contract_satisfied = bool(completion_check())
                except Exception as exc:
                    contract_satisfied = False
                    self.last_request_stats["completion_check_error"] = str(exc)
                if contract_satisfied:
                    completion_confirmation_requested = True
                    conversation.append({
                        "role": "user",
                        "content": (
                            "节点声明的交付物已经全部真实落盘。请停止工具调用，"
                            "只返回面向用户的简短完成结论与已验证证据。"
                        ),
                    })
                    working_tools = None
                    self.last_request_stats["completion_contract"] = (
                        "satisfied_text_confirmation"
                    )
            if invalid_ids:
                conversation.append({
                    "role": "user",
                    "content": "工具参数不是合法 JSON；请停止调用工具，基于已有证据直接回答。",
                })
                working_tools = None
            if force_text_only:
                conversation.append({
                    "role": "user",
                    "content": (
                        "重复工具调用已被保护机制阻断。请停止调用工具，基于已经返回的证据直接回答；"
                        "未验证部分必须明确说明。"
                    ),
                })
                working_tools = None
            tool_round += 1
            self.last_request_stats["tool_rounds"] = tool_round
            if max_tool_rounds is not None and tool_round >= max(0, max_tool_rounds):
                conversation.append({
                    "role": "user",
                    "content": "工具轮次已达到调用方设置的上限，请停止工具调用并直接输出当前结果。",
                })
                working_tools = None

    @property
    def total_tokens_used(self) -> int:
        """Total tokens used across all calls."""
        return self._total_tokens_used
    
    @property
    def total_calls(self) -> int:
        """Total number of API calls made."""
        return self._total_calls


class LLMClientError(Exception):
    """Error from LLM API calls."""
    pass
