"""LLM client for AstralNexus role agents.

Provides a lightweight, standalone LLM client that calls the DashScope
API (OpenAI-compatible) directly. Used by role agents for real task execution.

Integrates with model_selector for interactive model selection at startup.
"""

from __future__ import annotations
from AstralNexus.runtime.run_context import get_env

import json
import http.client
import logging
import os
import re
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import ProxyHandler, Request, build_opener, urlopen
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


# --- Fix AJ (doc 18, 2026-09-15 r13b): provider egress must be explicit ------
# ``urlopen`` uses urllib's default opener, which silently inherits HTTP_PROXY /
# HTTPS_PROXY from the environment.  The provider endpoint is region-specific
# (dashscope is an Alibaba Cloud gateway in China), so an unrelated system proxy
# in another region routes every token of every stream through it.  That is not
# a slow link, it is a link that breaks: generation 14 produced four dead calls
# in forty minutes -- SSL: UNEXPECTED_EOF_WHILE_READING, a read timeout, then
# the circuit opening -- none of which say anything about the model or the task.
# The proxy a run uses for LLM egress is therefore a declared setting, not
# whatever happened to be exported into the shell.
#
#   ASTRALNEXUS_LLM_PROXY unset  -> inherit the environment (unchanged default)
#   ASTRALNEXUS_LLM_PROXY=direct -> bypass every proxy ("none"/"off"/"no" too)
#   ASTRALNEXUS_LLM_PROXY=<url>  -> use exactly that proxy
_PROXY_OPENER_CACHE: dict[str, Any] = {}
_PROXY_CHOICE_LOGGED: set[str] = set()
_DIRECT_TOKENS = {"direct", "none", "off", "no", "0", "false"}


def llm_proxy_setting() -> str:
    return str(get_env("ASTRALNEXUS_LLM_PROXY", "")).strip()


def _llm_opener(setting: str):
    """An opener for the declared egress path, or None to keep urllib's default."""
    if not setting:
        return None
    if setting in _PROXY_OPENER_CACHE:
        return _PROXY_OPENER_CACHE[setting]
    if setting.lower() in _DIRECT_TOKENS:
        opener = build_opener(ProxyHandler({}))
    else:
        opener = build_opener(ProxyHandler({"http": setting, "https": setting}))
    _PROXY_OPENER_CACHE[setting] = opener
    return opener


def llm_urlopen(request, timeout=None):
    """Open a provider request over the declared egress path (Fix AJ)."""
    setting = llm_proxy_setting()
    if setting and setting not in _PROXY_CHOICE_LOGGED:
        _PROXY_CHOICE_LOGGED.add(setting)
        logger.info(
            "LLM egress: %s",
            "direct (system proxy bypassed)" if setting.lower() in _DIRECT_TOKENS else setting,
        )
    opener = _llm_opener(setting)
    if opener is None:
        return urlopen(request, timeout=timeout)
    return opener.open(request, timeout=timeout)



# --------------------------------------------------------------- Fix Y (doc 22)
# Fix A gave the *recovery* path a bounded circuit wait; the main node path had
# none, so a short cooldown was fatal.  Live r13b: PATCH-R002-E2E died of a 5m10s
# silent stream, and PATCH-R002-SEC died 21s later with
#   "provider circuit open for https://dashscope...; retry in 25.99s"
# — its retry budget had already been spent on 2s/4s backoff sleeps, so it hit
# the still-open breaker on the last attempt and the node was written off.  A
# 26s cooldown is a wait, not a provider failure; the node should never die of it.


#: Fix X 后续修正（doc 18, 2026-09-15）：流式中断的判据必须与错误文案解耦。
#: Fix L 的轮次重试原本按字面匹配 "stream ended without [DONE]"；Fix X 为了说明
#: 真实原因把该文案改成 "stream closed by peer before [DONE]" 后，那条匹配恒为假，
#: 轮次重试对「对端中途关闭」整类故障静默失效——改一句错误文案就关掉了一条恢复
#: 路径，而 Fix L 的既有测试是直接伪造异常文本的，抓不到这种脱钩。两种措辞在此
#: 集中判定；新增措辞只改这里。
_INCOMPLETE_STREAM_MARKERS = (
    "stream closed by peer before [DONE]",
    "stream ended without [DONE]",
)


def is_mid_turn_transport_error(exc: BaseException) -> bool:
    """True for a transport-layer cut that happened before any tool ran.

    Fix AB (doc 18, 2026-09-15 r13b): Fix L reissues a round when the stream is
    cut mid-flight, on the grounds that tools execute only AFTER the stream
    completes, so nothing has happened yet and the reissue is idempotent.  That
    reasoning is about WHEN the cut happens, not about which exception class
    carries it — but the handler only caught LLMClientError, so a raw URLError /
    SSLError / socket timeout propagated straight out and killed the node.
    Live PATCH-R004-RUNTIME: 9 streams succeeded and 2 were cut by
    "SSL: UNEXPECTED_EOF_WHILE_READING"; one of those cuts ended the node, which
    cost a Governor generation and recovery churn for a transient drop.
    The stream-level reconnect budget is exhausted before this point, so this is
    the last bounded chance, and it is bounded by ASTRALNEXUS_TURN_RETRIES.
    """
    return isinstance(
        exc,
        (URLError, socket.timeout, TimeoutError, ssl.SSLError,
         http.client.HTTPException, ConnectionError),
    ) and not isinstance(exc, HTTPError)


def is_incomplete_stream_error(exc: BaseException) -> bool:
    """True when a stream ended before the provider sent [DONE]."""
    text = str(exc)
    return any(marker in text for marker in _INCOMPLETE_STREAM_MARKERS)


def is_transport_interruption(exc: BaseException) -> bool:
    """True when a failure is transport, not a statement about the work.

    Fix AM (doc 18, 2026-09-15 r13b gen 17): Fix AI and Fix AL both classified
    by ``isinstance``, which is defeated the moment a retry loop RE-RAISES.  When
    the circuit-open budget runs out this module raises a plain
    ``LLMClientError`` whose message still says "provider circuit open after 3
    retries" but whose type no longer does::

        Verifier failed for PATCH-R009-APP-RUNTIME-CLEANUP:
        provider circuit open after 3 retries: provider circuit open for
        https://dashscope.aliyuncs.com/compatible-mode/v1; retry in 21.97s

    Both fixes missed it and the node was judged on a request that never left
    the machine.  The re-raise uses ``from e``, so the original exception is on
    the ``__cause__`` chain: walking it recovers the true category without
    matching on message text, which would break the moment the wording changes.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ProviderCircuitOpenError):
            return True
        if is_mid_turn_transport_error(current) or is_incomplete_stream_error(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _circuit_wait_budget_seconds() -> float:
    """Longest cooldown a call may wait out instead of failing (0 disables)."""
    try:
        return max(0.0, float(get_env("ASTRALNEXUS_CIRCUIT_WAIT_SECONDS", "45")))
    except (TypeError, ValueError):
        return 45.0


def _circuit_max_waits() -> int:
    """How many distinct cooldowns one stream call may absorb."""
    try:
        return max(0, int(get_env("ASTRALNEXUS_CIRCUIT_MAX_WAITS", "2")))
    except (TypeError, ValueError):
        return 2


def _circuit_retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Read the cooldown the gate reported (``... ; retry in 25.99s``)."""
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(exc))
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _sleep_out_circuit(
    seconds: float,
    cancel_check: Optional[Callable[[], bool]] = None,
    slice_seconds: float = 0.5,
) -> bool:
    """Sleep out a cooldown in slices; ``False`` means the caller asked to stop.

    Slicing keeps a stop request from being blocked for the whole cooldown.  The
    remaining time is decremented per slice rather than read from a monotonic
    clock, so the loop cannot spin when ``time.sleep`` is patched out in tests.
    """
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if cancel_check is not None and cancel_check():
            return False
        chunk = min(slice_seconds, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return True

# Fix 21 (live r11 resume #32 boundary-test churn): the tester probed a
# backend that deterministically answered 500 where 403/400 were required;
# every retry produced identical evidence, the no-progress guard killed the
# run BEFORE the noncompliance could be written down, and recovery kept
# re-deriving the same messenger-killing probe loop.  Deterministic
# noncompliance is EVIDENCE, not spinning: the first stalled-feedback
# message must offer the document-and-submit exit explicitly.
PROGRESS_STALLED_FEEDBACK = (
    "运行时发现相同动作和证据形成循环，责任文件没有变化。读取成功不代表任务进展。"
    "请根据已有证据识别未完成的交付或验证项，执行能改变该状态的下一步；"
    "若无法继续，明确提交阻塞原因交给调度器。不要重复同一轮检查，也不要虚报完成。"
    "特别地：若循环中的相同动作是对环境的实测/探测，且结果确定性地相同、又不符合任务预期"
    "（例如预期 403/400/409 实际稳定返回 500，或预期字段确定不存在），"
    "这不是靠重试能改变的——这是被测环境不合规的证据。立即停止重试，"
    "把本轮实测原样写入节点要求的回执路径（含命令、时间戳、实际状态码与响应体），"
    "在总结中如实说明『确定性不合规』并附实测，然后提交结果，"
    "由验证者与规划器把根因路由给上游责任人。"
    "写出不合规回执并如实提交是合法完成路径；反复探测等待不同结果会触发无进展强杀。"
)


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
        self.runtime_execution_id: str | None = None
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
        self.max_retries = max(0, int(get_env("ASTRALNEXUS_LLM_RETRIES", "3")))
        self.retry_backoff = max(0.1, float(get_env("ASTRALNEXUS_LLM_RETRY_BACKOFF", "2")))
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

    #: Fix BB: tokens of input that earn one extra multiple of the base timeout.
    TIMEOUT_SCALE_TOKENS = 32_768

    def _size_aware_timeout(self, request_stats: Optional[Dict[str, Any]] = None) -> float:
        """Read timeout for one request, scaled by that request's own input size.

        ``self.timeout`` stays the floor, so every small request behaves exactly as
        before.  A large request earns proportionally more time, capped by
        ``ASTRALNEXUS_LLM_MAX_REQUEST_TIMEOUT`` (default 600s).  The caller still
        clamps the result to the node's remaining budget.
        """
        base = float(self.timeout)
        try:
            ceiling = float(get_env("ASTRALNEXUS_LLM_MAX_REQUEST_TIMEOUT", "600"))
        except (TypeError, ValueError):
            ceiling = 600.0
        ceiling = max(base, ceiling)
        try:
            scale_tokens = float(get_env(
                "ASTRALNEXUS_LLM_TIMEOUT_SCALE_TOKENS", str(self.TIMEOUT_SCALE_TOKENS)
            ))
        except (TypeError, ValueError):
            scale_tokens = float(self.TIMEOUT_SCALE_TOKENS)
        if scale_tokens <= 0:
            return base
        stats = request_stats if isinstance(request_stats, dict) else self.last_request_stats
        tokens = 0.0
        if isinstance(stats, dict):
            raw = stats.get("estimated_input_tokens", 0)
            if isinstance(raw, bool):
                raw = 0
            if isinstance(raw, (int, float)) and raw > 0:
                tokens = float(raw)
        if tokens <= 0:
            return base
        return min(ceiling, base * (1.0 + tokens / scale_tokens))

    def _build_auxiliary_client(self) -> "LLMClient":
        """Build a separate, tool-free client for historical summaries."""
        aux_model = get_env("ASTRALNEXUS_AUX_MODEL", "qwen-flash")
        return LLMClient(
            model=aux_model,
            api_key=self.api_key,
            base_url=self.base_url,
            max_tokens=int(get_env("ASTRALNEXUS_AUX_MAX_TOKENS", "4000")),
            temperature=0.0,
            timeout=min(self.timeout, 120),
            interactive=False,
            enable_compression=False,
        )

    def _maybe_compress_conversation(
        self,
        conversation: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        extra_chars: int = 0,
    ) -> List[Dict[str, Any]]:
        if not self._enable_compression:
            self.last_compression_stats = {"compressed": False, "disabled": True}
            return conversation
        context_length = self._effective_context_length()
        tool_chars = len(json.dumps(tools or [], ensure_ascii=False, separators=(",", ":")))
        tool_chars += max(0, int(extra_chars))
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
            from AstralNexus.core.governance_types import EventType
            if hasattr(self, '_trace_recorder') and self._trace_recorder:
                self._trace_recorder.record(EventType.CONTEXT_OVERFLOW, {
                    "before_tokens": stats.get("before_tokens_estimate"),
                    "after_tokens": stats.get("after_tokens_estimate"),
                    "compression_ratio": stats.get("compression_ratio", stats.get("reduction_ratio")),
                    "reason": "当前请求上下文达到有效压缩阈值，需要保留约束和最近工具结果并压缩历史中段",
                    "reason_codes": ["CONTEXT_COMPRESSION_TRIGGERED"],
                })
        return compacted

    def _prepare_stream_context(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        current_state: Optional[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        """Prepare the exact stream context before compression and admission."""
        conversation, normalized_tools, _ = prepare_request(messages, tools)
        # Fix BC: the execution-state view is the freshest, most authoritative
        # context the worker has -- the current file digest / ledger.  It used to
        # be appended BEFORE compression so that the compressor's budget would
        # account for its size (live r13b: 59k compressor tokens vs 66k preflight).
        # That intent is right, but putting the state INTO the compressible list
        # lets the compressor summarize it away or move it: the worker then acts
        # on a stale picture of what changed, and the state can also land in the
        # retained history, where a stale digest accumulates forever.  This is the
        # Fix AH / AO family again -- the framework handing a worker the wrong view
        # of the world.
        #
        # Both intents are satisfiable: charge the state's size to the compression
        # budget via ``extra_chars``, but append the state itself AFTER compression
        # so it stays last and never enters the compressible middle or the history.
        state_chars = (
            len(json.dumps(current_state, ensure_ascii=False, separators=(",", ":")))
            if current_state is not None
            else 0
        )
        conversation = self._maybe_compress_conversation(
            conversation, normalized_tools or None, extra_chars=state_chars,
        )
        if current_state is not None:
            conversation = [*conversation, current_state]
        conversation, normalized_tools, stats = prepare_request(
            conversation, normalized_tools,
        )
        return conversation, normalized_tools or None, stats

    def _effective_context_length(self) -> int:
        """Return the single context budget used by compression and the TUI.

        A model's provider limit and an optional local operating budget are
        different concepts.  The local budget is opt-in; a hidden 32k default
        made the UI report 4% while the compressor had already fired.
        """
        model_capacity = int(
            get_env(
                "ASTRALNEXUS_CONTEXT_LENGTH",
                str(AVAILABLE_MODELS.get(self.model, {}).get("context_length", 1_000_000)),
            )
        )
        configured = self.context_budget
        if configured is None:
            raw_budget = get_env("ASTRALNEXUS_CONTEXT_BUDGET")
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
        owned_paths: Optional[List[str]] = None,
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
        from AstralNexus.runtime.llm_loop_budget import LLMLoopBudget, LLMLoopBudgetExceeded
        loop_budget = LLMLoopBudget.from_environment(max_tool_rounds, self.max_retries)
        max_tool_rounds = loop_budget.max_rounds
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
        ledger_root = workspace_root or self.workspace_root
        from AstralNexus.runtime.execution_ledger import ExecutionLedger
        ledger = ExecutionLedger.for_context(ledger_root, owned_paths, self.runtime_execution_id)
        tool_guard = ToolCallGuard(workspace_root=ledger_root)

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
        request_started = time.monotonic()
        # ``timeout`` is the timeout of one provider request.  A previous
        # implementation also used it as the cumulative deadline of the whole
        # tool-using node, so successful long-running coding work eventually
        # received only a few seconds for its final model call.  Node lifetime
        # is separate from one-request timeout, but finite for every session.
        deadline = loop_budget.deadline
        configured_node_timeout = deadline - request_started
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
            try:
                loop_budget.admit_request(tool_round)
            except LLMLoopBudgetExceeded as exc:
                self._emit_event({"event_type": "llm.task_budget.exhausted", "reason_code": str(exc)})
                raise LLMClientError(str(exc)) from exc
            if cancel_check is not None and cancel_check():
                self._emit_event({
                    "event_type": "llm.interrupted",
                    "request_index": request_index,
                    "tool_round": tool_round,
                    "phase": "before_request",
                })
                raise InterruptionRequested("LLM request cancelled by interactive controller")
            request_index += 1
            if ledger is not None:
                state_request_id = uuid.uuid4().hex
                state = ledger.message(request_id=state_request_id)
                # The receipt projection is request-local, never appended to
                # conversation history or repeatedly summarized by the model.
                wire_messages, _, request_stats = prepare_request(conversation + [state], tools)
                request_data = json.dumps({**payload, "messages": wire_messages}, ensure_ascii=False).encode("utf-8")
                self._emit_event({
                    "event_type": "llm.state_view.frozen", "tool_round": tool_round,
                    "state_request_id": state_request_id,
                })
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
            # Fix BB: the read timeout was a flat constant (default 120s) no matter
            # how large the request was, so a big request was guillotined at the same
            # wall every time and an identical retry could only hit it again.
            #
            # Live r13b: the independent Verifier sends ONE 2-message request carrying
            # the whole evidence packet (observed 195,692-198,845 chars,
            # estimated_input_tokens 65,232-66,283). Every failure reads
            # "Network error from LLM API after 120.5s: The read operation timed out"
            # -- always AT the timeout, never a connection error. The compressor cannot
            # help either: it requires len(messages) >= 4 and the verifier request has 2.
            # PATCH-R023 and PATCH-R026 each burned all 4 verification attempts on the
            # same 120s wall and were forced to inconclusive/0.0.
            #
            # Retrying harder is the wrong answer (项目规则 不能增加次数掩盖问题); the
            # attempt budget is untouched. What changes is that a request gets a read
            # window proportional to its own measured size, still bounded by an absolute
            # ceiling and still never exceeding the node's remaining time.
            request_timeout = max(
                1.0,
                min(self._size_aware_timeout(request_stats), node_remaining)
                if node_remaining is not None
                else self._size_aware_timeout(request_stats),
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
                    with llm_urlopen(request, timeout=request_timeout) as response:
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
                try:
                    loop_budget.consume_usage(usage)
                except LLMLoopBudgetExceeded as exc:
                    raise LLMClientError(str(exc)) from exc
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
                        owned_paths=owned_paths,
                        allowed_tools={str(item.get("function", item).get("name")) for item in (tools or []) if item.get("function", item).get("name")} or None,
                    )
                    executor.result_observer = ledger.observe if ledger is not None else None
                    blocked_results: dict[str, Any] = {}
                    for call in tool_calls:
                        allowed, key_or_reason = tool_guard.admit(call)
                        call_id = call.get("id", f"tool_{len(blocked_results)}")
                        if not allowed:
                            blocked_results[call_id] = {
                                "status": "error",
                                "error": "tool loop guard halted repeated call",
                                "guard_reason": key_or_reason,
                            }
                    executed_results = executor.execute_model_calls(tool_calls, blocked_results)
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
                                "review_coverage": result.get("review_coverage") if isinstance(result, dict) else None,
                                "review_warning": result.get("review_warning") if isinstance(result, dict) else None,
                                "change_ids": result.get("change_ids", []) if isinstance(result, dict) else [],
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
                            for call, result in zip(tool_calls, executed_results)
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
                    tool_results = []
                    force_text_only = False
                    for call, result in zip(tool_calls, executed_results):
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
                        max_tool_result = max(1000, int(get_env("ASTRALNEXUS_TOOL_RESULT_MAX_CHARS", "24000")))
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
                # Fix Y: `min(retry_after, backoff * 2**n)` capped a 26s cooldown at
                # 2/4/8s of waiting, so the retry budget ran out while the breaker
                # was still open and the caller failed for a purely temporary
                # condition.  Wait out a short cooldown in full; keep the capped
                # backoff only when the cooldown exceeds the wait budget.
                if retry_after and retry_after <= _circuit_wait_budget_seconds():
                    if not _sleep_out_circuit(retry_after + 2.0, cancel_check):
                        raise LLMClientError(
                            f"provider circuit open; interrupted while waiting out "
                            f"{retry_after:.2f}s cooldown: {error_msg}; "
                            f"request_stats={self.last_request_stats}"
                        ) from e
                elif retry_after:
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
        prepared_messages_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        current_state: Optional[Dict[str, Any]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        """Yield OpenAI-compatible SSE delta events.

        Streaming is intentionally a separate API from ``chat``.  Tool-loop
        orchestration remains owned by ``chat`` until a caller explicitly
        chooses an event-driven runtime.
        """
        url = f"{self.base_url}/chat/completions"
        # The execution ledger is part of the request's model-visible context;
        # include it in the same compression/admission pass (live r13b showed
        # 59k compressor tokens vs 66k preflight after state was appended).
        conversation, normalized_tools, request_stats = self._prepare_stream_context(
            messages, tools, current_state,
        )
        if prepared_messages_callback is not None:
            # Fix BC: the execution-state view is ephemeral -- it is rebuilt fresh for
            # every request.  It must reach the model (last message) but must NOT be
            # retained as history, or a stale digest accumulates there forever.
            retained = conversation
            if current_state is not None and retained and retained[-1] == current_state:
                retained = retained[:-1]
            prepared_messages_callback([dict(message) for message in retained])
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
        stream_request_id = uuid.uuid4().hex
        def emit(event: Dict[str, Any]) -> None:
            callback = event_callback or self._event_callback
            if callback is not None:
                try:
                    callback({"request_id": stream_request_id, **event})
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
        max_stream_retries = max(0, int(get_env("ASTRALNEXUS_STREAM_RETRIES", "2")))
        # Fix X (doc 18, 2026-09-15 r13b): urlopen(timeout=) is a PER-SOCKET-READ
        # timeout, so it is already a chunk watchdog — it was just set to the whole
        # request budget (self.timeout, default 600s), which makes it useless.  Live
        # N-008: the provider accepted the request, sent nothing, and closed ~5
        # minutes later; the round-level retry then burned a second 5 minutes, all
        # of it pure dead wait that no code noticed.  Bound a silent stream to a
        # chunk interval instead.  Note the non-streaming path already narrows its
        # timeout with the node budget (request_timeout); stream() has no deadline
        # parameter, so self.timeout was its ONLY bound.
        chunk_timeout = min(
            float(self.timeout),
            max(5.0, float(get_env("ASTRALNEXUS_STREAM_CHUNK_TIMEOUT", "180"))),
        )
        stream_attempt = 0
        # Fix Y: cooldowns absorbed by this call.  Counted separately from
        # stream_attempt on purpose — waiting for a rejected request to become
        # requestable is not a re-connection attempt and must not consume the
        # stream retry budget.
        circuit_waits = 0
        stream_usage: Dict[str, Any] = {}
        while True:
            if cancel_check is not None and cancel_check():
                emit({
                    "event_type": "llm.interrupted",
                    "phase": "before_stream_request",
                    "attempt": stream_attempt + 1,
                })
                raise InterruptionRequested("LLM stream cancelled before request")
            attempt_has_output = False
            stream_events = 0
            cancel_event = threading.Event()
            try:
                with provider_request_gate.request_slot(self.provider_key):
                    with llm_urlopen(request, timeout=chunk_timeout) as response:
                        watcher_stop = threading.Event()

                        def close_on_cancel() -> None:
                            while not watcher_stop.wait(0.1):
                                if cancel_check is not None and cancel_check():
                                    cancel_event.set()
                                    try:
                                        response.close()
                                    except Exception:
                                        logger.debug(
                                            "Could not close cancelled LLM stream",
                                            exc_info=True,
                                        )
                                    return

                        watcher = None
                        if cancel_check is not None:
                            watcher = threading.Thread(
                                target=close_on_cancel,
                                name="astralnexus-llm-stream-cancel",
                                daemon=True,
                            )
                            watcher.start()
                        try:
                            for raw_line in response:
                                if cancel_event.is_set() or (
                                    cancel_check is not None and cancel_check()
                                ):
                                    cancel_event.set()
                                    emit({
                                        "event_type": "llm.interrupted",
                                        "phase": "during_stream",
                                        "attempt": stream_attempt + 1,
                                        "stream_events": stream_events,
                                    })
                                    raise InterruptionRequested(
                                        "LLM stream cancelled while reading provider response"
                                    )
                                line = raw_line.decode("utf-8", errors="replace").strip()
                                if not line or not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    provider_request_gate.record_success(self.provider_key)
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
                                            "model": self.model,
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
                                attempt_has_output = True
                                stream_events += 1
                                yield event
                        finally:
                            watcher_stop.set()
                            if watcher is not None:
                                watcher.join(timeout=0.25)
                        if cancel_event.is_set():
                            emit({
                                "event_type": "llm.interrupted",
                                "phase": "during_stream",
                                "attempt": stream_attempt + 1,
                                "stream_events": stream_events,
                            })
                            raise InterruptionRequested(
                                "LLM stream cancelled while waiting for provider response"
                            )
                # Fix X: the SSE loop ends silently when the peer closes the
                # connection — no exception is raised — so this line used to report
                # a TLS/network drop as "protocol incomplete".  That mislabel is the
                # same class of defect as Fix W: the system described its own
                # infrastructure failure as something else, and root-cause analysis
                # followed the wrong trail.  Say what actually happened.
                raise LLMClientError(
                    "stream closed by peer before [DONE]; "
                    f"received {stream_events} SSE event(s), "
                    f"chunk timeout {chunk_timeout:.0f}s; response is incomplete"
                )
            except InterruptionRequested:
                raise
            except Exception as exc:
                if cancel_event.is_set() or (
                    cancel_check is not None and cancel_check()
                ):
                    emit({
                        "event_type": "llm.interrupted",
                        "phase": "during_stream",
                        "attempt": stream_attempt + 1,
                        "stream_events": stream_events,
                    })
                    raise InterruptionRequested(
                        "LLM stream cancelled while waiting for provider response"
                    ) from exc
                # Fix X: this used to record EVERY exception as a retryable
                # "network" failure.  Two consequences, both live:
                #   * a ProviderCircuitOpenError raised by request_slot (i.e. the
                #     breaker REJECTING a call) was counted as a provider failure,
                #     so every rejection re-armed opened_until — the circuit could
                #     not close while attempts kept arriving.  A rejection is not
                #     evidence that the provider is unhealthy.
                #   * a local bug (JSON, attribute, type) inflated the same counter
                #     and could open the breaker against a perfectly healthy API.
                # The non-streaming path already classifies via classify_http /
                # classify_network; the streaming path now uses the same vocabulary.
                # record_failure() ignores any category outside
                # {network, rate_limit, provider_server}, so non-provider causes
                # stop counting toward the circuit without changing its semantics.
                if isinstance(exc, ProviderCircuitOpenError):
                    failure_category = "provider_circuit_open"
                    failure_retryable = True
                elif isinstance(exc, HTTPError):
                    classification = classify_http(exc.code, "")
                    failure_category = classification.category
                    failure_retryable = classification.retryable
                elif isinstance(exc, (URLError, socket.timeout, TimeoutError,
                                      ssl.SSLError, http.client.HTTPException, OSError)):
                    classification = classify_network(exc)
                    failure_category = classification.category
                    failure_retryable = classification.retryable
                elif isinstance(exc, LLMClientError) and is_incomplete_stream_error(exc):
                    # Silent peer EOF: a transport failure wearing a protocol name.
                    failure_category = "network"
                    failure_retryable = True
                else:
                    failure_category = "client_error"
                    failure_retryable = False
                provider_request_gate.record_failure(
                    self.provider_key, failure_category, failure_retryable,
                )
                # Fix Y: a rejected call is not a node failure.  Wait out a short
                # cooldown (bounded, never consuming the retry budget) and retry.
                # Before this, the stream path spent its 2-attempt budget on 2s/4s
                # backoff sleeps, hit the still-open breaker on the last attempt,
                # and the node was failed with ProviderCircuitOpenError — that is
                # how live N-008/PATCH-R002-SEC's churn was actually spent.
                if isinstance(exc, ProviderCircuitOpenError) and not attempt_has_output:
                    wait_seconds = _circuit_retry_after_seconds(exc)
                    if (
                        wait_seconds is not None
                        and circuit_waits < _circuit_max_waits()
                        and wait_seconds <= _circuit_wait_budget_seconds()
                    ):
                        circuit_waits += 1
                        emit({
                            "event_type": "llm.stream.circuit_wait",
                            "model": self.model,
                            "wait": circuit_waits,
                            "max_waits": _circuit_max_waits(),
                            "wait_seconds": round(wait_seconds + 2.0, 2),
                        })
                        if not _sleep_out_circuit(
                            wait_seconds + 2.0, cancel_check=cancel_check
                        ):
                            emit({
                                "event_type": "llm.interrupted",
                                "phase": "circuit_wait",
                                "attempt": stream_attempt + 1,
                            })
                            raise InterruptionRequested(
                                "LLM stream cancelled while waiting for provider circuit"
                            )
                        continue
                # Classification must govern retry admission as well as the
                # provider breaker. Reissuing an unchanged invalid request or
                # a local programming error cannot recover it.
                if not failure_retryable or attempt_has_output or stream_attempt >= max_stream_retries:
                    emit({
                        "event_type": "llm.stream.failed",
                        "model": self.model,
                        "error": str(exc)[:500],
                        "error_type": type(exc).__name__,
                        "failure_category": failure_category,
                        "retryable": failure_retryable,
                        "chunk_timeout_seconds": chunk_timeout,
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
        owned_paths: Optional[List[str]] = None,
        completion_check: Optional[Callable[[], bool]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        task_execution_budget: Optional[Any] = None,
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
        from AstralNexus.runtime.llm_loop_budget import LLMLoopBudget, LLMLoopBudgetExceeded
        loop_budget = LLMLoopBudget.from_environment(max_tool_rounds, self.max_retries)
        max_tool_rounds = loop_budget.max_rounds
        from AstralNexus.runtime.execution_ledger import ExecutionLedger

        ledger_root = workspace_root or self.workspace_root
        ledger = ExecutionLedger.for_context(ledger_root, owned_paths, self.runtime_execution_id)
        working_tools = tools
        from AstralNexus.runtime.progress_guard import ProgressGuard, WriteProgressGuard, write_progress_feedback
        from AstralNexus.runtime.tool_output_budget import externalize_tool_result
        progress_guard = ProgressGuard()
        # P2 (DeerFlow loop_capped port, doc 30): ProgressGuard only sees
        # REPEATED identical cycles; live N-125 burned a whole budget on ~70
        # DIVERSE read-only calls with zero owned-file writes and no cycle
        # ever repeated. For mutation contracts (owned_paths declared) the
        # write-progress guard escalates nudge → final warning → tool
        # withdrawal as the budget burns without a single landed byte.
        write_progress_guard = (
            WriteProgressGuard.from_env(task_execution_budget if task_execution_budget is not None else loop_budget,
                                        rolling=task_execution_budget is None)
            if ledger is not None and owned_paths
            else None
        )
        tool_round = 0
        full_text: List[str] = []
        # Fix r29 (doc 51 §16 / user step ③): hand the loop guard the execution's
        # OWN declared premises and test commands, so it can tell "the worker is
        # pointlessly repeating a call" apart from "the worker must re-run the
        # anchored verification because a tracked file changed". The guard and the
        # acceptance gate then share one version space (runtime.verification_identity).
        from AstralNexus.runtime.verification_identity import (
            declared_premise_paths,
            declared_execution_commands,
            premise_versions,
        )

        guard_root = workspace_root or self.workspace_root
        declared_contract: Dict[str, Any] = {}
        if guard_root and self.runtime_execution_id:
            try:
                from AstralNexus.runtime.execution_store import ExecutionStore
                declared_contract = (
                    ExecutionStore(Path(guard_root)).get(str(self.runtime_execution_id)).get("contract")
                    or {}
                )
            except Exception:
                declared_contract = {}
        declared_criteria = list(declared_contract.get("acceptance_criteria", []) or [])
        declared_premises = declared_premise_paths(
            acceptance_criteria=declared_criteria,
            upstream_artifacts=declared_contract.get("upstream_artifacts", []),
            owned_paths=declared_contract.get("owned_paths", []),
            root=guard_root,
        )

        def _verification_premises() -> Dict[str, str]:
            if not guard_root or not declared_premises:
                return {}
            return premise_versions(Path(guard_root), declared_premises)

        tool_guard = ToolCallGuard(
            workspace_root=workspace_root or self.workspace_root,
            premise_provider=_verification_premises if declared_premises else None,
            verification_commands=declared_execution_commands(
                declared_criteria, declared=declared_premises, root=guard_root,
            ),
        )
        completion_confirmation_requested = False
        post_withdrawal_text_nudged = False
        truncation_nudged = False
        task_budget_nudged = False
        self.last_tool_artifacts = []
        # Fix L (doc 18 attempt8): one idempotent reissue per round when the
        # provider cuts the SSE stream mid-response; see the retry block below.
        max_turn_retries = max(0, int(get_env("ASTRALNEXUS_TURN_RETRIES", "1")))

        def emit(event: Dict[str, Any]) -> None:
            callback = event_callback or self._event_callback
            if callback is not None:
                try:
                    callback(dict(event))
                except Exception:
                    logger.debug("LLM stream-chat event callback failed", exc_info=True)

        while True:
            budget_reason = (
                task_execution_budget.exhaustion_reason()
                if task_execution_budget is not None else None
            )
            if budget_reason and working_tools is not None:
                working_tools = None
                if not task_budget_nudged:
                    task_budget_nudged = True
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"当前节点的执行预算已耗尽（{budget_reason}）。停止调用工具，"
                            "只总结已经实际完成和验证的内容；未完成项必须明确标为未完成，"
                            "不得声称验收通过。"
                        ),
                    })
                    emit({
                        "event_type": "llm.task_budget.exhausted",
                        "reason_code": budget_reason,
                        "tool_calls_used": int(getattr(task_execution_budget, "tool_calls_used", 0)),
                        "tool_round": tool_round,
                    })
            if cancel_check is not None and cancel_check():
                emit({
                    "event_type": "llm.interrupted",
                    "phase": "before_stream_request",
                    "tool_round": tool_round,
                })
                raise InterruptionRequested("LLM stream chat cancelled")

            streamed_text: List[str] = []
            pending: Dict[int, Dict[str, Any]] = {}
            truncated = False
            # Fix L (doc 18 attempt8): a mid-stream provider cut ("stream ended
            # without [DONE]") killed the whole node session.  No tool has run
            # yet for this round — tools execute only after the stream
            # completes — so reissuing the identical round is side-effect free
            # and idempotent; only this round's partial text buffer has to be
            # rolled back.  stream()'s own reconnect loop cannot cover this
            # case: it is bypassed as soon as any delta has been consumed
            # (attempt_has_output), which is exactly why attempt7 recorded 14
            # llm.stream.failed events all with attempts: 1.  Deltas already
            # emitted by the abandoned attempt stay in the trace, so the retry
            # announces itself with llm.stream.turn_retried to keep that
            # duplication auditable.
            turn_attempt = 0
            while True:
                full_text_len = len(full_text)
                # Freeze the model-visible receipt set for THIS request (doc 17
                # 第一批⑤): the ledger stamps the projection with a request id so
                # later reference checks resolve against what the model actually
                # saw, never against a post-compaction renumbering.
                state_request_id = uuid.uuid4().hex
                current_state = (
                    ledger.message(request_id=state_request_id) if ledger is not None else None
                )
                if ledger is not None:
                    emit({
                        "event_type": "llm.state_view.frozen",
                        "tool_round": tool_round,
                        "state_request_id": state_request_id,
                    })
                try:
                    try:
                        loop_budget.admit_request(tool_round)
                    except LLMLoopBudgetExceeded as exc:
                        emit({"event_type": "llm.task_budget.exhausted", "reason_code": str(exc)})
                        raise LLMClientError(str(exc)) from exc
                    for event in self.stream(
                        conversation,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        response_format=response_format,
                        tools=working_tools,
                        event_callback=event_callback,
                        emit_metrics=True,
                        prepared_messages_callback=lambda prepared: conversation.__setitem__(slice(None), prepared),
                        current_state=current_state,
                        cancel_check=cancel_check,
                    ):
                        if isinstance(event.get("usage"), dict):
                            try:
                                loop_budget.consume_usage(event["usage"])
                            except LLMLoopBudgetExceeded as exc:
                                raise LLMClientError(str(exc)) from exc
                        choices = event.get("choices", [])
                        if choices and choices[0].get("finish_reason") == "length":
                            truncated = True
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
                    break
                except (LLMClientError, URLError, OSError) as exc:
                    # Fix AB: a mid-turn cut is reissuable whether it arrives as
                    # LLMClientError (silent peer close) or as a raw transport
                    # exception (SSL EOF, socket timeout).  HTTPError keeps
                    # falling through: a 4xx/5xx is a provider verdict on the
                    # request, not a severed connection.
                    if (
                        not (
                            is_incomplete_stream_error(exc)
                            or is_mid_turn_transport_error(exc)
                        )
                        or turn_attempt >= max_turn_retries
                    ):
                        raise
                    turn_attempt += 1
                    del full_text[full_text_len:]
                    streamed_text = []
                    pending = {}
                    truncated = False
                    emit({
                        "event_type": "llm.stream.turn_retried",
                        "model": self.model,
                        "tool_round": tool_round,
                        "attempt": turn_attempt + 1,
                        "error": str(exc)[:300],
                        "note": "本轮尚未执行任何工具，重发同一请求；被放弃尝试已流出的文本增量在本 trace 中可见",
                    })

            tool_calls, invalid_ids = _normalize_tool_calls(
                [pending[index] for index in sorted(pending)]
            )
            if not tool_calls:
                if truncated:
                    # Fix Z (doc 18, 2026-09-15 r13b): a truncated answer used to
                    # kill the node outright.  Live evidence: PATCH-R003-RUNTIME
                    # died twice in a row on exactly this, the Governor superseded
                    # it into a fresh generation each time, and every generation
                    # burned recovery churn — a deterministic loop that ends in
                    # no_progress_breaker without the model ever being told what
                    # went wrong.  The sibling path already does the right thing:
                    # when TOOL ARGUMENTS are truncated the calls are refused and
                    # the reason is fed back (failure_class "truncated_output").
                    # Give the no-tool-call case the same courtesy once, then fail
                    # honestly if it happens again.  Refusing to treat a truncated
                    # response as complete is unchanged — this only adds feedback.
                    if not truncation_nudged:
                        truncation_nudged = True
                        conversation.append({
                            "role": "user",
                            "content": (
                                "你上一条回复在输出长度上限处被截断，因此没有被采纳。"
                                "不要试图在一条回复里写完所有内容：长文件用 file_write "
                                "分多次写入（每次一个文件或一段），正文只保留必要的简短说明。"
                                "请重新作答，并把这一轮的输出控制在明显更短的长度。"
                            ),
                        })
                        emit({
                            "event_type": "llm.output_truncation_nudge",
                            "model": self.model,
                            "tool_round": tool_round,
                        })
                        continue
                    raise LLMClientError(
                        "streamed response truncated by output limit twice "
                        "(a shorter-output instruction was already given); "
                        "completion is unverified"
                    )
                answer = "".join(full_text).strip()
                if not answer:
                    raise LLMClientError(
                        "model returned an empty streamed response; "
                        f"tool_rounds={tool_round}; request_stats={self.last_request_stats}"
                    )
                return answer

            if not working_tools:
                # Fix 14 (live r11 resume #21): tools were withdrawn (loop
                # guard force_text_only, completion contract, or round cap)
                # but the model keeps emitting tool calls.  The previous hard
                # LLMClientError killed the whole exec and forfeited every
                # receipt it had collected — five consecutive tester
                # generations died identically, and because exec-level deaths
                # never reach verification, the governed coder-pivot (the
                # only path that grants backend ownership for the real fix)
                # could never trigger.  Nudge once for a text-only wrap-up;
                # if the model still demands tools, end gracefully with the
                # accumulated text so the caller submits a partial result
                # with files and evidence preserved on disk.
                if not post_withdrawal_text_nudged:
                    post_withdrawal_text_nudged = True
                    conversation.append({
                        "role": "user",
                        "content": (
                            "工具已被收回，任何工具调用都不会执行。请不要再输出工具调用。"
                            "直接用纯文本总结：已真实落盘的写入与验证（逐项给出文件路径）、"
                            "未能完成的部分和确切原因。这段文本将作为你的最终执行结果提交，"
                            "虚报完成会被独立复验推翻并记为失败。"
                        ),
                    })
                    emit({
                        "event_type": "llm.post_withdrawal_nudge",
                        "tool_round": tool_round,
                    })
                    continue
                emit({
                    "event_type": "llm.post_withdrawal_graceful_end",
                    "tool_round": tool_round,
                })
                self.last_request_stats["post_withdrawal"] = "graceful_text_end"
                answer = "".join(full_text).strip()
                if answer:
                    return answer
                return (
                    "execution ended after tool withdrawal: the model kept "
                    "requesting tools instead of a text summary; no final "
                    "report was produced. Files and receipts written before "
                    "the withdrawal remain on disk for independent review."
                )

            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": "".join(streamed_text) or None,
                "tool_calls": tool_calls,
            }
            conversation.append(assistant_message)
            executor = ToolExecutor(
                workspace_root=workspace_root or self.workspace_root,
                owned_paths=owned_paths,
                allowed_tools={str(item.get("function", item).get("name")) for item in (working_tools or []) if item.get("function", item).get("name")} or None,
            )
            executor.result_observer = ledger.observe if ledger is not None else None
            admitted = []
            blocked: Dict[str, Dict[str, Any]] = {}
            budget_exhausted_reason: Optional[str] = None
            for index, call in enumerate(tool_calls):
                allowed, reason = tool_guard.admit(call)
                call_id = call.get("id", f"stream_tool_{index}")
                if truncated or call_id in invalid_ids:
                    blocked[call_id] = {
                        "status": "error",
                        "error": "Tool arguments were truncated or invalid; no tool in the truncated response was executed. "
                                 "Reduce scope to the node acceptance criteria. Retry with a smaller complete JSON call; "
                                 "split large files within owned paths: write a small first chunk, then use file_write mode=append "
                                 "with expected_bytes from the previous successful result. Send chunks sequentially.",
                        "failure_class": "truncated_output" if truncated else "invalid_tool_arguments",
                    }
                elif allowed:
                    budget_allowed, budget_reason = (
                        task_execution_budget.admit_tool_call()
                        if task_execution_budget is not None else (True, None)
                    )
                    if budget_allowed:
                        admitted.append(call)
                    else:
                        budget_exhausted_reason = budget_reason or "task_budget_exhausted"
                        blocked[call_id] = {
                            "status": "budget_exhausted",
                            "error": (
                                "Task execution budget exhausted; this tool call was not executed. "
                                "Report remaining work as incomplete."
                            ),
                            "reason_code": budget_exhausted_reason,
                        }
                else:
                    blocked[call_id] = {
                        "status": "error",
                        "error": "tool loop guard halted repeated call",
                        "guard_reason": reason,
                    }
            if budget_exhausted_reason:
                working_tools = None
                if not task_budget_nudged:
                    task_budget_nudged = True
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"当前节点的执行预算已耗尽（{budget_exhausted_reason}）。停止调用工具，"
                            "只总结已经实际完成和验证的内容；未完成项必须明确标为未完成，"
                            "不得声称验收通过。"
                        ),
                    })
                    emit({
                        "event_type": "llm.task_budget.exhausted",
                        "reason_code": budget_exhausted_reason,
                        "tool_calls_used": int(getattr(task_execution_budget, "tool_calls_used", 0)),
                        "tool_round": tool_round,
                    })
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
            executed = executor.execute_model_calls(tool_calls, blocked)
            calls_for_event = []
            force_text_only = False
            recovery_requested = False
            for index, (call, result) in enumerate(zip(tool_calls, executed)):
                call_id = call.get("id", f"stream_tool_{index}")
                result = result if isinstance(result, dict) else {"output": str(result)}
                tool_guard.observe(call, result)
                decision = tool_guard.decision(call)
                if decision["action"] != "retry_or_continue":
                    result = dict(result)
                    result["recovery"] = decision
                if decision["action"] == "switch_tool_or_replan":
                    recovery_requested = True
                if decision["action"] == "halt":
                    force_text_only = True
                calls_for_event.append({
                    "name": str((call.get("function") or {}).get("name", "")),
                    "call_id": call.get("id", ""),
                    "status": result.get("status", "unknown"),
                    "error": str(result.get("error", ""))[:500] if result.get("error") else None,
                    "path": str(result.get("path", "")) if result.get("path") else None,
                    "returncode": result.get("returncode"),
                    "review_coverage": result.get("review_coverage"),
                    "review_warning": result.get("review_warning"),
                    "change_ids": result.get("change_ids", []),
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
                # P4 (DeerFlow tool_output_budget port, doc 30): only the
                # model-bound conversation view is compacted; the ledger
                # receipt, loop detection below and the trace keep observing
                # the ORIGINAL result so no guard is blinded by the budget.
                view_result, externalized_event = externalize_tool_result(
                    result,
                    workspace_root=ledger_root,
                    call_id=str(call.get("id") or ""),
                    tool_round=tool_round,
                )
                if externalized_event is not None:
                    emit(externalized_event)
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": str((call.get("function") or {}).get("name", "")),
                    "content": json.dumps(view_result, ensure_ascii=False),
                })
                progress = progress_guard.observe(call, result, ledger.files() if ledger is not None else {})
                if progress is not None:
                    emit({"event_type": "llm.progress_stalled", **progress, "tool_round": tool_round})
                    if progress["action"] == "handoff":
                        raise RuntimeNoProgressError(progress)
                    conversation.append({"role": "user", "content": PROGRESS_STALLED_FEEDBACK})
                if write_progress_guard is not None:
                    # Reuse ProgressGuard's owned-files digest (fresh per
                    # observation): a filesystem-measured version change is
                    # the only signal that disarms the write-progress guard.
                    write_progress_guard.observe(progress_guard.file_version)
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
            # Fix r29 (doc 51 §16 / user step ③): a sanctioned re-verification is
            # reported as structured state — which declared criterion, which
            # premise files changed, which previous call/attempt it supersedes —
            # instead of leaving the model to merely assert "I want to re-verify".
            for notice in tool_guard.take_notices():
                emit({
                    "event_type": "llm.verification.reverification_admitted",
                    "tool_round": tool_round,
                    **notice,
                })
            if write_progress_guard is not None and working_tools:
                write_signal = write_progress_guard.check()
                if write_signal is not None:
                    emit({
                        "event_type": "llm.write_progress_stalled",
                        **write_signal,
                        "tool_round": tool_round,
                    })
                    conversation.append({
                        "role": "user",
                        "content": write_progress_feedback(write_signal),
                    })
                    self.last_request_stats["write_progress_actions"] = list(
                        write_progress_guard.actions_taken
                    )
                    if write_signal["action"] == "cap_text_only":
                        # DeerFlow capped semantics: withdraw tools EARLY so
                        # the attempt ends as an honest capped submission
                        # with receipts preserved, instead of dying at the
                        # wall clock with the work stranded in context.
                        working_tools = None
                        self.last_request_stats["write_progress"] = "capped_zero_output"
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
                    "content": "工具参数无效或输出被截断，相关调用没有执行。不要再次输出整个大文件：先写一个小段，再按顺序用 file_write mode=append 追加，每次 expected_bytes 使用上次成功返回的 bytes。每段建议不超过3000字符，必须完整JSON。所有分段完成后才编译或测试；失败调用不得算完成。",
                })
            if recovery_requested:
                conversation.append({
                    "role": "user",
                    "content": (
                        "同一工具调用已重复失败。先依据返回的错误执行最小修复并重新验证；"
                        "不要重复原调用，也不要因命令执行失败而重写无关的实现文件。"
                        "可以从已注册工具中选择适合的替代工具；若当前权限或能力无法解决，"
                        "明确报告阻塞和失败证据，交由调度器恢复。失败验证仍是失败。"
                    ),
                })
            self.last_request_stats["tool_guard"] = tool_guard.snapshot()
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


class RuntimeNoProgressError(LLMClientError):
    """A controlled guard handoff, not an API or host failure."""
    def __init__(self, progress: dict[str, Any]):
        self.progress = dict(progress)
        super().__init__(
            "runtime_no_progress: repeated action/evidence cycle after replanning feedback; "
            "preserve receipts and files and return to Governor recovery: " + json.dumps(progress))
