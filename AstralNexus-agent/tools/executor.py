"""Tool executor for AstralNexus — unified tool dispatch layer.

Integrates with the central ToolRegistry for discovery, schema retrieval,
approval-aware dispatch, and execution.
"""

from __future__ import annotations
from AstralNexus.runtime.run_context import get_env

import logging
import threading
import traceback
import os
import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from AstralNexus.tools.registry import ToolEntry, registry, discover_builtin_tools
from AstralNexus.tools.tool_router import alternative_tools
from AstralNexus.tools.context import get_tool_owned_paths, tool_owned_paths, tool_workspace, resolve_tool_path

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
        resolved = resolve_tool_path(path)
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
        left_resolved = resolve_tool_path(left)
        right_resolved = resolve_tool_path(right)
    except (OSError, RuntimeError):
        left_resolved, right_resolved = left, right
    left_parts, right_parts = left_resolved.parts, right_resolved.parts
    common = min(len(left_parts), len(right_parts))
    return bool(left_parts and right_parts and left_parts[:common] == right_parts[:common])


# Fix AS ---------------------------------------------------------------------
# The ownership guard extracts "explicit workspace paths" from tool payloads.
# Its pattern was ``[A-Z]:[\\/]``, which matches the tail of any URL scheme:
# ``http://127.0.0.1:8291/health`` contains ``p://127.0.0.1:8291/health``, and
# ``Path()`` turns that into the Windows path ``p:\127.0.0.1:8291\health``.
#
# Live r13b, PATCH-R017-FINAL-ACCEPTANCE-REPAIR seq 2940: the worker finally
# issued the real acceptance run -- subprocess launch of serve.py, isolated DB
# copy, stop-file, urllib probe -- and the call was refused with
#
#   python_exec references a workspace path outside the Worker owned_paths
#   contract: p:\127.0.0.1:8291\health
#
# A node whose entire contract is "start the service and hit it over HTTP"
# therefore could not do its job: any script that both writes a file and names
# a localhost URL was rejected. Two generations of this node produced
# null-valued acceptance reports because of it.
#
# The guard itself is kept. Only its notion of "this text is a filesystem path"
# is corrected, each rule for a stated reason:
#   1. URL literals are removed before scanning (a URL is not a path claim).
#   2. A drive letter must not follow a word character (kills ``...http:`` and
#      ``fooC:\``); a real drive letter stands alone.
#   3. A candidate whose components carry characters Windows forbids in a file
#      name (``: * ? " < > |``) is not a path (kills ``127.0.0.1:8291``).
#   4. A drive followed by a single escape letter (``R:\n`` from ``print("R:\n")``)
#      is source-text escaping, not a path. Observed live at seq 2972.
_URL_LITERAL = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s\"'`)\]}>,;|]*")
_PATH_CLAIM = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])[A-Z]:[\\/][^\"'\r\n;|]+|(?:\.\.[\\/])+[^\"'\r\n;| ]+)"
)
_WINDOWS_FORBIDDEN = set(':*?"<>|')
_ESCAPE_LETTERS = set("nrtbfav0")


def _extract_path_claims(payload: str) -> List[str]:
    """Path-looking substrings of a tool payload, with URLs and escapes excluded."""
    text = _URL_LITERAL.sub(" ", payload or "")
    claims: List[str] = []
    for match in _PATH_CLAIM.findall(text):
        candidate = match.strip().rstrip(")]}>,")
        if not candidate:
            continue
        remainder = candidate[2:] if re.match(r"(?i)^[A-Z]:[\\/]", candidate) else candidate
        parts = [part for part in re.split(r"[\\/]", remainder) if part]
        if any(set(part) & _WINDOWS_FORBIDDEN for part in parts):
            continue
        if len(parts) == 1 and len(parts[0]) == 1 and parts[0].lower() in _ESCAPE_LETTERS:
            continue
        claims.append(candidate)
    return claims


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
    _SCOPED_POOLS: Dict[str, ThreadPoolExecutor] = {}
    _POOL_LOCK = threading.Lock()
    _POOL_SHUTDOWN = False

    @classmethod
    def _get_shared_pool(cls) -> ThreadPoolExecutor:
        """Get or create the shared thread pool for tool execution."""
        with cls._POOL_LOCK:
            if cls._SHARED_POOL is None:
                max_workers = max(1, int(get_env("ASTRALNEXUS_MAX_TOOL_CONCURRENCY", "32")))
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

    def _get_run_pool(self) -> ThreadPoolExecutor:
        root = getattr(self.trace_recorder, "run_dir", None) or self.workspace_root
        if root is None:
            return self._get_shared_pool()
        key = os.path.normcase(str(resolve_tool_path(Path(root))))
        with self._POOL_LOCK:
            pool = self._SCOPED_POOLS.get(key)
            if pool is None:
                pool = ThreadPoolExecutor(max_workers=max(1, int(get_env("ASTRALNEXUS_MAX_TOOL_CONCURRENCY", "32"))),
                                          thread_name_prefix="AstralNexus-RunTool")
                self._SCOPED_POOLS[key] = pool
            return pool

    @classmethod
    def shutdown_run_pool(cls, run_dir: str | Path, *, wait: bool = True) -> None:
        key = os.path.normcase(str(resolve_tool_path(Path(run_dir))))
        with cls._POOL_LOCK:
            pool = cls._SCOPED_POOLS.pop(key, None)
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=not wait)

    def __init__(self, trace_recorder=None, workspace_root: Optional[str | Path] = None,
                 owned_paths: Optional[list[str]] = None, approval_identity: str = "", allowed_tools: Optional[set[str]] = None):
        self.trace_recorder = trace_recorder
        self.allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else None
        from AstralNexus.runtime.presentation import current_presentation
        self.presentation = current_presentation()
        from AstralNexus.core.agent_registry import get_agent_registry
        self.agent_catalog = get_agent_registry()
        from AstralNexus.runtime.run_context import current_run_context
        self.run_context = current_run_context()
        from AstralNexus.runtime.workspace_review import current_review
        self.workspace_review = current_review()
        from AstralNexus.runtime.execution_fence import current_execution_fence, current_execution_identity
        if workspace_root is None and self.run_context is not None:
            workspace_root = self.run_context.run_dir
        self.execution_fence_provider = current_execution_fence(Path(workspace_root)) if workspace_root is not None else None
        self.execution_identity = current_execution_identity(Path(workspace_root)) if workspace_root is not None else ''
        if self.execution_identity:
            self.presentation = None  # Delegated workers never inherit lead presentation authority.
        self.result_observer: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None
        self.workspace_root = (
            resolve_tool_path(Path(workspace_root))
            if workspace_root is not None and str(workspace_root).strip()
            else None
        )
        self.owned_paths = tuple(str(item).replace("\\", "/").strip() for item in (owned_paths or []) if str(item).strip())
        from AstralNexus.tools.approval import _get_provider
        root = getattr(trace_recorder, "run_dir", None) or self.workspace_root
        self.approval_identity = approval_identity or (self.run_context.approval_identity if self.run_context else "") or (os.path.normcase(str(resolve_tool_path(Path(root)))) if root else f"executor:{id(self)}")
        self.approval_provider = _get_provider(self.approval_identity)
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
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return {"status": "error", "error": "tool is not available to this agent", "guard_reason": "tool_not_allowed", "executed": False}
        from AstralNexus.runtime.run_context import run_context_scope
        from AstralNexus.runtime.presentation import presentation_scope
        with run_context_scope(self.run_context), presentation_scope(self.presentation):
            return self._execute_tool_with_context(tool_name, params)

    def _execute_tool_with_context(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
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
            get_env("ASTRALNEXUS_TOOL_ACQUIRE_TIMEOUT", "60")
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
            from AstralNexus.tools.approval import approval_scope
            with tool_workspace(self.workspace_root), tool_owned_paths(self.owned_paths), approval_scope(
                self.approval_identity, provider=self.approval_provider, bind_provider=True,
            ):
                self._guard_execution(tool_name)
                def invoke():
                    # A review snapshot can take time; recheck immediately
                    # before the operation rather than trusting the old check.
                    self._guard_execution(tool_name)
                    return self._execute_tool_inner(tool_name, params)
                if self.workspace_review is not None:
                    return self.workspace_review.execute(tool_name, params, self.workspace_root, invoke, self.owned_paths)
                return self._execute_tool_inner(tool_name, params)
        finally:
            _TOOL_SEMAPHORE.release()

    def _guard_execution(self, tool_name: str) -> None:
        if self.workspace_root is None: return
        from AstralNexus.runtime.execution_fence import guard_active_execution, guard_execution_fence
        guard_active_execution(self.workspace_root, self.execution_identity)
        if self.execution_fence_provider is not None or tool_name in {
                'file_write', 'file_replace', 'file_edit', 'file_patch', 'terminal_exec', 'python_exec', 'pytest_run'}:
            guard_execution_fence(self.workspace_root, self.execution_fence_provider)

    def _execute_tool_inner(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Inner dispatch after concurrency slot is held."""
        params = dict(params)
        if tool_name == "file_write":
            violation = self._write_scope_violation(params)
            if violation:
                return {
                    "status": "error",
                    "error": violation,
                    "failure_class": "owned_path_violation",
                    "next_action": "replan_with_disjoint_output_contract",
                }
        if tool_name in {"terminal_exec", "python_exec"}:
            violation = self._prepare_owned_process_scope(tool_name, params)
            if violation:
                return {
                    "status": "error",
                    "error": violation,
                    "failure_class": "owned_path_violation",
                    "next_action": "use_the_worker_owned_path_or_request_a_graph_patch",
                }
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

    def _write_scope_violation(self, params: Dict[str, Any]) -> Optional[str]:
        """Reject file writes outside the Worker-owned namespace."""
        owned = get_tool_owned_paths()
        if not owned:
            return None
        raw_path = str(params.get("path", "")).strip()
        if not raw_path:
            return "file_write requires a path"
        from AstralNexus.runtime.presentation import mapped_user_path
        candidate = mapped_user_path(raw_path) or Path(raw_path).expanduser()
        if not candidate.is_absolute() and self.workspace_root is not None:
            candidate = self.workspace_root / candidate
        try:
            candidate = resolve_tool_path(candidate)
        except (OSError, RuntimeError):
            candidate = Path(os.path.abspath(str(candidate)))
        for raw_root in owned:
            root = Path(raw_root)
            if not root.is_absolute() and self.workspace_root is not None:
                root = self.workspace_root / root
            try:
                root = resolve_tool_path(root)
                candidate.relative_to(root)
                return None
            except (ValueError, OSError, RuntimeError):
                continue
        return f"file_write path is outside the Worker owned_paths contract: {candidate}"

    def _owned_roots(self) -> List[Path]:
        roots: List[Path] = []
        for raw in get_tool_owned_paths():
            root = Path(raw).expanduser()
            if not root.is_absolute() and self.workspace_root is not None:
                root = self.workspace_root / root
            try:
                roots.append(resolve_tool_path(root))
            except (OSError, RuntimeError):
                roots.append(Path(os.path.abspath(str(root))))
        return roots

    @staticmethod
    def _path_is_within(candidate: Path, roots: List[Path]) -> bool:
        for root in roots:
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _prepare_owned_process_scope(
        self, tool_name: str, params: Dict[str, Any]
    ) -> Optional[str]:
        """Bind process tools to the Worker namespace and reject explicit escapes.

        This is a contract guard, not an OS sandbox.  ``terminal_exec`` remains
        approval-gated because a command can construct paths dynamically.
        """
        roots = self._owned_roots()
        if not roots:
            return None

        # A file ownership contract is not a process working directory.
        # Workers that verify or consume another node's outputs must be able
        # to run commands from those input directories.  Keep file_write
        # checks against the original roots, while process tools remain
        # bounded by this isolated run workspace.
        directory_roots = [root.parent if root.is_file() or (not root.is_dir() and root.suffix) else root for root in roots]
        process_roots = [self.workspace_root] if self.workspace_root is not None else directory_roots
        raw_cwd = str(params.get("cwd", "")).strip()
        cwd = Path(raw_cwd).expanduser() if raw_cwd else (self.workspace_root or directory_roots[0])
        if not cwd.is_absolute() and self.workspace_root is not None:
            cwd = self.workspace_root / cwd
        try:
            cwd = resolve_tool_path(cwd)
        except (OSError, RuntimeError):
            cwd = Path(os.path.abspath(str(cwd)))
        if not self._path_is_within(cwd, process_roots):
            return f"{tool_name} cwd is outside the Worker owned_paths contract: {cwd}"
        if self.workspace_root is not None and not self._path_is_within(cwd, [self.workspace_root]):
            return f"{tool_name} cwd is outside the run workspace: {cwd}"
        cwd.mkdir(parents=True, exist_ok=True)
        params["cwd"] = str(cwd)

        payloads: List[str] = []
        if tool_name == "python_exec":
            payloads.append(str(params.get("code", "")))
        else:
            command = params.get("command", [])
            payloads.extend(command if isinstance(command, list) else [str(command)])

        # Reject explicit workspace paths that target another Worker's tree.
        # The executable itself may be an absolute system path, so only paths
        # rooted under this run's workspace participate in ownership checks.
        workspace = resolve_tool_path(self.workspace_root) if self.workspace_root else None
        for payload in payloads:
            for raw_match in _extract_path_claims(str(payload)):
                raw_match = raw_match.strip().rstrip(")]}>,")
                # Fix AW (2026-09-17): remember how the claim was *spelled*.
                # An absolute claim that resolves outside the run workspace is
                # not a claim on another Worker's tree (see the writes branch).
                absolute_claim = bool(re.match(r"(?i)^(?:[A-Z]:[\\/]|\\\\)", raw_match))
                candidate = Path(raw_match).expanduser()
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                try:
                    candidate = resolve_tool_path(candidate)
                except (OSError, RuntimeError):
                    candidate = Path(os.path.abspath(str(candidate)))
                # Check explicit writes before exempting external executable
                # paths. Otherwise ../ from the run root bypasses ownership
                # checks precisely because it resolves outside the workspace.
                writes_payload = bool(re.search(
                    r"(?i)(write_text|write_bytes|open\s*\([^)]*['\"][wax]|>>|>[^=])",
                    str(payload),
                ))
                if writes_payload and not self._path_is_within(candidate, roots):
                    if absolute_claim and workspace is not None:
                        # The executable itself may be an absolute system path.
                        # Live r13b, PATCH-R017-FINAL-ACCEPTANCE-REPAIR round 97:
                        # a script that spawned a specific interpreter and also
                        # wrote its own output was refused with
                        #
                        #   python_exec references a workspace path outside the
                        #   Worker owned_paths contract:
                        #   C:\...\Programs\Python\Python310\python.exe
                        #
                        # Naming a toolchain is a program selection, not ownership
                        # of a path, and nothing this guard protects lives outside
                        # the workspace -- so an absolutely-spelled claim out there
                        # is skipped.  Relative claims keep the strict treatment:
                        # "..\..\x" escapes the workspace while still naming a path
                        # inside it, which is what the ordering above guards.
                        try:
                            candidate.relative_to(workspace)
                        except ValueError:
                            continue
                    return (
                        f"{tool_name} references a workspace path outside the Worker "
                        f"owned_paths contract: {candidate}"
                    )
                if workspace is not None:
                    try:
                        candidate.relative_to(workspace)
                    except ValueError:
                        continue
                if not self._path_is_within(candidate, process_roots):
                    return (
                        f"{tool_name} references a workspace path outside the run "
                        f"workspace: {candidate}"
                    )
        return None

    def execute_model_calls(
        self, tool_calls: List[Dict[str, Any]],
        blocked_results: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """One receipt boundary for executed and rejected model calls.

        A rejected call is a sequential barrier: preserve model order and
        observe its outcome without invoking the handler. Observe each real
        call at execution time, before a later mutation changes its premises.
        Observer failures propagate; missing durable evidence is not success.
        """
        blocked_results = blocked_results or {}
        results: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []

        def flush() -> None:
            if pending:
                results.extend(self.execute_tool_calls(list(pending)))
                pending.clear()

        for call in tool_calls:
            call_id = str(call.get("id") or "")
            if call_id not in blocked_results:
                pending.append(call)
                continue
            flush()
            result = dict(blocked_results[call_id])
            if self.result_observer is not None:
                self.result_observer(call, result)
            results.append(result)
        flush()
        return results

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
                pool = self._get_run_pool()
                futures = [pool.submit(self.execute_tool, _call_name(call), _call_args(call)) for call in calls]
                # Never let a single parallel tool hold the LLM conversation
                # forever.  The handler itself has a process timeout, while
                # this second boundary protects registry tools and third-party
                # handlers that do not honor it.
                future_timeout = max(1.0, float(get_env(
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
                for call in calls:
                    value = self.execute_tool(_call_name(call), _call_args(call))
                    results[positions[id(call)]] = value
                    if self.result_observer is not None:
                        self.result_observer(call, value)
                continue
            for call, value in zip(calls, values):
                results[positions[id(call)]] = value
                if self.result_observer is not None:
                    self.result_observer(call, value)
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
        max_attempts = 1 + (max(0, int(get_env("ASTRALNEXUS_TOOL_RETRIES", "1"))) if retryable else 0)
        last_error = None
        for attempt in range(max_attempts):
            try:
                # All execution now passes through the registry policy layer:
                # availability, approval gates, and result limits stay
                # consistent for LLM workers and direct callers.
                if isinstance(entry, ToolEntry) or registry.get_entry(tool_name) is entry:
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
                except _json.JSONDecodeError as exc:
                    return self._record_tool_error(tool_name, ValueError(
                        f"Registered tool returned invalid JSON: {exc}"))
            else:
                result = raw_result
            if isinstance(result, dict) and result.get("error") and "status" not in result:
                result["status"] = "error"
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
        result: Dict[str, Any] = {
            "status": "error",
            "error": error_msg,
            "traceback": traceback.format_exc(),
            "failure_class": "tool_exception",
            "next_action": "switch_tool_or_replan",
        }
        result["alternative_tools"] = self._available_alternatives(tool_name)
        return result

    def _available_alternatives(self, tool_name: str) -> list[str]:
        return [name for name in alternative_tools(tool_name)
                if (self.allowed_tools is None or name in self.allowed_tools)
                and registry.is_tool_available(name)]

    def _annotate_failure(self, tool_name: str, result: Any) -> Any:
        """Attach a recovery hint without changing successful results."""
        if not isinstance(result, dict) or str(result.get("status", "")).lower() not in {
            "error", "failed", "failure", "approval_pending"
        }:
            return result
        if result.get("status") == "approval_pending":
            result.setdefault("failure_class", "approval_required")
            result.setdefault("next_action", "request_approval")
            return result
        if isinstance(result.get("error"), bool):
            result["error_flag"] = result["error"]
            result["error"] = str(result.get("message") or "Tool execution failed")
        result.setdefault("failure_class", "tool_execution_failed")
        result.setdefault("next_action", "switch_tool_or_replan")
        result["alternative_tools"] = self._available_alternatives(tool_name)
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
                role = self.agent_catalog.get_role(profile)
                if role is None:
                    raise KeyError(f"Unknown worker profile: {profile}")
                allowed_tools = set(role.default_tools)
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
