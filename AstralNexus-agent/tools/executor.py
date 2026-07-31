"""Tool executor for AstralNexus — unified tool dispatch layer.

Integrates with the central ToolRegistry for discovery, schema retrieval,
approval-aware dispatch, and execution.
"""

from __future__ import annotations

import logging
import threading
import traceback
import os
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional

from AstralNexus.tools.registry import registry, discover_builtin_tools
from AstralNexus.tools.tool_router import alternative_tools
from AstralNexus.tools.context import tool_workspace

logger = logging.getLogger(__name__)

# Global tool concurrency control.
# Limits the total number of concurrent tool executions across all workers
# and ToolExecutor instances. Prevents resource exhaustion when multiple
# workers run parallel tool batches simultaneously.
#
# Configured via ASTRALNEXUS_MAX_TOOL_CONCURRENCY (default: 32).
# Each tool invocation acquires this semaphore before execution.
_TOOL_SEMAPHORE = threading.BoundedSemaphore(
    max(1, int(os.environ.get("ASTRALNEXUS_MAX_TOOL_CONCURRENCY", "32")))
)

_PARALLEL_SAFE_TOOLS = frozenset({
    "file_read", "search_files", "web_search", "http_get",
    "openalex_search", "crossref_search", "arxiv_search",
    "github_repo_contents", "browser_fetch", "skills_list", "skill_view",
})
_PATH_SCOPED_TOOLS = frozenset({"file_read", "file_write"})


def _call_name(call: Dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name", ""))


def _call_args(call: Dict[str, Any]) -> Dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments", {})
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _path_scope(call: Dict[str, Any]) -> Optional[Path]:
    raw = _call_args(call).get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw).expanduser()
    # Resolve to absolute, normalized, symlink-free canonical path.
    # This protects against case-insensitive collisions, junction points,
    # short (8.3) Windows paths, and relative path ambiguity.
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(str(path)))
    return resolved


def _reject_symlink_or_escape(path: Path, root: Optional[Path] = None) -> None:
    """Reject paths that are symlinks or escape the allowed root.
    
    Raises:
        PermissionError: if the path is a symlink or escapes root.
    """
    # Check if the original path is a symlink before resolving
    if path.is_symlink():
        raise PermissionError(f"path is a symlink and is rejected for security: {path}")
    
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        real = path
    
    if root is not None:
        try:
            real.relative_to(root.resolve())
        except ValueError:
            raise PermissionError(
                f"path escapes allowed root: {path} (root={root})"
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    """Compare canonical (resolved, case-normalized) paths for overlap.
    
    Uses resolve() so symlinks, case-insensitivity, and short/long Windows
    names all collapse to the same identity before the string comparison.
    """
    try:
        left_resolved = left.resolve(strict=False)
        right_resolved = right.resolve(strict=False)
    except (OSError, RuntimeError):
        left_resolved, right_resolved = left, right
    left_parts, right_parts = left_resolved.parts, right_resolved.parts
    common = min(len(left_parts), len(right_parts))
    return bool(left_parts and right_parts and left_parts[:common] == right_parts[:common])


class ToolExecutor:
    """Unified tool executor that bridges the central ToolRegistry
    with per-agent tool dispatch.

    Features:
      - Auto-discovers builtin tools via registry.discover_builtin_tools()
      - Dispatches to registry handlers for registered tools
      - Records all tool invocations to the trace recorder
      - Reuses a shared thread pool for parallel tool execution
    """

    # Shared thread pool across all ToolExecutor instances.
    # Sized to match the global tool semaphore so we never have more
    # threads waiting than available concurrency slots.
    _SHARED_POOL: Optional[ThreadPoolExecutor] = None
    _POOL_LOCK = threading.Lock()
    _POOL_SHUTDOWN = False

    @classmethod
    def _get_shared_pool(cls) -> ThreadPoolExecutor:
        """Get or create the shared thread pool for tool execution."""
        with cls._POOL_LOCK:
            if cls._SHARED_POOL is None:
                max_workers = max(1, int(os.environ.get("ASTRALNEXUS_MAX_TOOL_CONCURRENCY", "32")))
                cls._SHARED_POOL = ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="AstralNexus-Tool",
                )
                cls._POOL_SHUTDOWN = False
            return cls._SHARED_POOL

    @classmethod
    def shutdown_global_pool(
        cls, *, wait: bool = True, cancel_futures: Optional[bool] = None
    ) -> None:
        """Shutdown the process-wide pool without poisoning future runs.
        
        Args:
            wait: If True, block until all pending tasks complete
            cancel_futures: If omitted, cancel pending futures only for non-waiting shutdown.
        """
        if cancel_futures is None:
            cancel_futures = not wait
        with cls._POOL_LOCK:
            pool, cls._SHARED_POOL = cls._SHARED_POOL, None
            cls._POOL_SHUTDOWN = False
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=cancel_futures)
    
    @classmethod
    def is_pool_shutdown(cls) -> bool:
        """Check if the global pool is shutdown."""
        return cls._POOL_SHUTDOWN

    def __init__(self, trace_recorder=None, workspace_root: Optional[str | Path] = None):
        self.trace_recorder = trace_recorder
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None and str(workspace_root).strip()
            else None
        )
        self._registry_tools_loaded = False
        self._load_tools()

    def _load_tools(self) -> None:
        """Load tools from the central registry.
        
        Only centrally registered tools are executable.
        """
        # 1. Discover and import all self-registering tool modules
        try:
            imported = discover_builtin_tools()
            self._registry_tools_loaded = True
            if imported:
                logger.debug("Auto-discovered tool modules: %s", imported)
        except Exception as exc:
            logger.warning("Tool auto-discovery failed: %s", exc)

    def execute_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a centrally registered tool by name.

        Dispatch priority:
          1. Central ToolRegistry (registered tools)
        Concurrency is governed by the global tool semaphore so parallel
        workers cannot collectively spawn unbounded thread pools.

        Args:
            tool_name: Name of the tool to execute
            params: Parameters for the tool

        Returns:
            Result dictionary with at least a 'status' key
        """
        # Acquire global tool concurrency slot.
        # This limits the total concurrent tool executions across all
        # ToolExecutor instances and all workers in the orchestrator.
        acquired = _TOOL_SEMAPHORE.acquire(timeout=float(
            os.environ.get("ASTRALNEXUS_TOOL_ACQUIRE_TIMEOUT", "60")
        ))
        if not acquired:
            return {
                "status": "error",
                "error": "tool concurrency limit reached; try again later",
                "failure_class": "tool_concurrency_exhausted",
                "next_action": "retry_or_replan",
            }
        try:
            # The context is installed inside the worker thread as well as
            # on the caller thread.  This keeps parallel tool batches scoped
            # to the same run without mutating process-global environment.
            with tool_workspace(self.workspace_root):
                return self._execute_tool_inner(tool_name, params)
        finally:
            _TOOL_SEMAPHORE.release()

    def _execute_tool_inner(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Inner dispatch after concurrency slot is held."""
        # Record tool start
        if self.trace_recorder:
            self.trace_recorder.record(
                "tool.start",
                {"tool_name": tool_name, "params": params},
            )

        # --- Dispatch to central registry first ---
        entry = registry.get_entry(tool_name)
        if entry is not None:
            return self._dispatch_registry_tool(tool_name, entry, params)

        # --- Tool not found ---
        error_msg = (
            f"Tool '{tool_name}' not found in the central registry. "
            f"Available: {self.list_tools()}"
        )
        if self.trace_recorder:
            self.trace_recorder.record(
                "tool.error", {"tool_name": tool_name, "error": error_msg}
            )
        return {"status": "error", "error": error_msg}

    def execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute one model tool batch with AstralNexus-style safety segments.

        Independent read-only calls and independent path-scoped file calls
        run concurrently.  Unknown tools, terminal execution, and overlapping
        file targets are sequential barriers.  Results always return in the
        model's original order, so concurrency never changes conversation
        semantics or side-effect ordering.
        """
        segments: List[tuple[str, List[Dict[str, Any]]]] = []
        parallel: List[Dict[str, Any]] = []
        reserved: List[Path] = []

        def close_parallel() -> None:
            nonlocal parallel, reserved
            if parallel:
                segments.append(("parallel", parallel))
                parallel, reserved = [], []

        def add_sequential(call: Dict[str, Any]) -> None:
            close_parallel()
            if segments and segments[-1][0] == "sequential":
                segments[-1][1].append(call)
            else:
                segments.append(("sequential", [call]))

        for call in tool_calls:
            name = _call_name(call)
            if name in _PATH_SCOPED_TOOLS:
                path = _path_scope(call)
                if path is None:
                    add_sequential(call)
                elif any(_paths_overlap(path, other) for other in reserved):
                    close_parallel()
                    parallel.append(call)
                    reserved.append(path)
                else:
                    parallel.append(call)
                    reserved.append(path)
                continue
            if name in _PARALLEL_SAFE_TOOLS:
                parallel.append(call)
                continue
            add_sequential(call)
        close_parallel()

        results: List[Optional[Dict[str, Any]]] = [None] * len(tool_calls)
        positions = {id(call): index for index, call in enumerate(tool_calls)}
        for kind, calls in segments:
            if kind == "parallel" and len(calls) >= 2:
                if self.trace_recorder:
                    self.trace_recorder.record("tool.batch.parallel", {
                        "count": len(calls),
                        "tools": [_call_name(call) for call in calls],
                    })
                # Use shared pool instead of creating a new one each time
                pool = self._get_shared_pool()
                futures = [pool.submit(self.execute_tool, _call_name(call), _call_args(call)) for call in calls]
                # Never let a single parallel tool hold the LLM conversation
                # forever.  The handler itself has a process timeout, while
                # this second boundary protects registry tools and third-party
                # handlers that do not honor it.
                future_timeout = max(1.0, float(os.environ.get(
                    "ASTRALNEXUS_TOOL_BATCH_TIMEOUT", "150"
                )))
                values = []
                for future, call in zip(futures, calls):
                    try:
                        values.append(future.result(timeout=future_timeout))
                    except FutureTimeoutError:
                        values.append({
                            "status": "error",
                            "error": f"tool execution timed out after {future_timeout:g}s",
                            "failure_class": "tool_timeout",
                            "tool_name": _call_name(call),
                            "next_action": "switch_tool_or_replan",
                        })
                    except Exception as exc:
                        values.append({
                            "status": "error",
                            "error": f"parallel tool execution failed: {exc}",
                            "failure_class": "tool_exception",
                            "tool_name": _call_name(call),
                            "next_action": "switch_tool_or_replan",
                        })
            else:
                values = [self.execute_tool(_call_name(call), _call_args(call)) for call in calls]
            for call, value in zip(calls, values):
                results[positions[id(call)]] = value
        return [value or {"status": "error", "error": "tool result missing"} for value in results]

    def _dispatch_registry_tool(
        self, tool_name: str, entry: Any, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Dispatch to a tool registered in the central ToolRegistry."""
        import json as _json
        # Only replay operations that are normally idempotent.  Mutating
        # tools (patch/write/terminal/install) must fail visibly instead of
        # being executed twice after a transient exception.
        retryable = any(token in tool_name.lower() for token in (
            "read", "search", "fetch", "parse", "test", "pytest", "query", "monitor"
        ))
        max_attempts = 1 + (max(0, int(os.environ.get("ASTRALNEXUS_TOOL_RETRIES", "1"))) if retryable else 0)
        last_error = None
        for attempt in range(max_attempts):
            try:
                # All execution now passes through the registry policy layer:
                # availability, approval gates, and result limits stay
                # consistent for LLM workers and direct callers.
                if registry.get_entry(tool_name) is entry:
                    raw_result = registry.dispatch(
                        tool_name, params, caller="tool_executor",
                        approval_basis="role_worker_tool_policy",
                    )
                else:
                    # Preserve the small backward-compatible seam used by
                    # embedders/tests that pass an entry-like object.
                    raw_result = entry.handler(params)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    if self.trace_recorder:
                        self.trace_recorder.record("tool.retry", {
                            "tool_name": tool_name,
                            "attempt": attempt + 1,
                            "category": "tool_transient_failure",
                            "error": str(exc)[:500],
                        })
                    continue
        if last_error is not None:
            return self._record_tool_error(tool_name, last_error)
        try:
            # Registry handlers return JSON strings; parse back to dict
            if isinstance(raw_result, str):
                try:
                    result = _json.loads(raw_result)
                except _json.JSONDecodeError:
                    result = {"status": "success", "data": raw_result}
            else:
                result = raw_result
            result = self._annotate_failure(tool_name, result)

            if self.trace_recorder:
                self.trace_recorder.record(
                    "tool.complete",
                    {"tool_name": tool_name, "result": result},
                )
            return result
        except Exception as exc:
            return self._record_tool_error(tool_name, exc)

    def _record_tool_error(self, tool_name: str, exc: Exception) -> Dict[str, Any]:
        """Record a tool error and return a structured error dict."""
        error_msg = f"Tool '{tool_name}' failed: {exc}"
        if self.trace_recorder:
            self.trace_recorder.record(
                "tool.error",
                {
                    "tool_name": tool_name,
                    "error": error_msg,
                    "traceback": traceback.format_exc(),
                },
            )
        result = {
            "status": "error",
            "error": error_msg,
            "traceback": traceback.format_exc(),
            "failure_class": "tool_exception",
            "next_action": "switch_tool_or_replan",
        }
        result["alternative_tools"] = alternative_tools(tool_name)
        return result

    @staticmethod
    def _annotate_failure(tool_name: str, result: Any) -> Any:
        """Attach a recovery hint without changing successful results."""
        if not isinstance(result, dict) or str(result.get("status", "")).lower() not in {
            "error", "failed", "failure", "approval_pending"
        }:
            return result
        if result.get("status") == "approval_pending":
            result.setdefault("failure_class", "approval_required")
            result.setdefault("next_action", "request_approval")
            return result
        result.setdefault("failure_class", "tool_execution_failed")
        result.setdefault("next_action", "switch_tool_or_replan")
        result.setdefault("alternative_tools", alternative_tools(tool_name))
        return result

    def list_tools(self) -> List[str]:
        """List all centrally registered tools."""
        return sorted(registry.get_all_tool_names())

    def get_tool_schemas(
        self, 
        profile: Optional[str] = None,
        task_context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Get OpenAI-format schemas for available tools.
        
        Args:
            profile: Optional worker profile name to filter tools by capability.
                     If None, returns all registry tools.
            task_context: Optional task metadata for semantic filtering.
        
        Returns:
            List of tool schemas filtered by profile and context.
        """
        # Get all registry tool names
        all_tools = set(registry.get_all_tool_names())
        
        # If profile specified, filter by profile capabilities
        if profile:
            try:
                from AstralNexus.agent.registry import get_profile_capabilities
                capabilities = get_profile_capabilities(profile)
                allowed_tools = set(capabilities.get("allowed_tools", []))
                # Intersection: only tools in both registry and profile
                filtered_tools = all_tools & allowed_tools
                if not filtered_tools:
                    # Profile exists but no tools match - this is a configuration error
                    # Block request instead of falling back to all tools
                    raise ValueError(
                        f"Profile '{profile}' capability match yielded no tools. "
                        f"Profile may be misconfigured or task requires different profile."
                    )
            except (ImportError, KeyError, AttributeError, ValueError) as e:
                # Profile query failed - block request, do NOT fall back to all tools
                # This prevents DashScope input overflow and security bypass
                raise RuntimeError(
                    f"Failed to resolve profile '{profile}' tool capabilities. "
                    f"Request blocked to prevent uncontrolled tool injection. "
                    f"Error: {e}"
                ) from e
        else:
            filtered_tools = all_tools
        
        # Optional: semantic filtering by task context
        if task_context and "tool_whitelist" in task_context:
            whitelist = set(task_context["tool_whitelist"])
            filtered_tools = filtered_tools & whitelist
        
        if self.trace_recorder and profile:
            self.trace_recorder.record("tool.schemas.filtered", {
                "profile": profile,
                "total_available": len(all_tools),
                "filtered_count": len(filtered_tools),
                "filtered_tools": sorted(filtered_tools),
            })
        
        # Return schemas for filtered tools
        return registry.get_definitions(filtered_tools)
