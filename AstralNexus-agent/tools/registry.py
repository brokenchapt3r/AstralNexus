"""Central tool registry for AstralNexus (天枢).

This module provides the AstralNexus tool registry:
  - Auto-discovery of tool modules that call ``registry.register()``
  - Thread-safe registration with toolset membership and availability checks
  - OpenAI-format schema retrieval for model tool definitions
  - Unified dispatch with structured JSON error returns
  - ``check_fn`` TTL caching to amortize external probes

Usage in tool files::

    from AstralNexus.tools.registry import registry

    def my_tool_handler(args, **kwargs):
        ...
        return json.dumps({"success": True, ...})

    registry.register(
        name="my_tool",
        toolset="file",
        schema={
            "name": "my_tool",
            "description": "Does something useful",
            "parameters": { ... }
        },
        handler=my_tool_handler,
    )
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from AstralNexus.core.exceptions import (
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    format_tool_error,
)
from AstralNexus.tools.approval import consume_one_shot_grant, request_approval

logger = logging.getLogger(__name__)

# TTL for check_fn cache results (seconds).
_CHECK_FN_TTL = 30.0
_CHECK_FN_FAILURE_GRACE = 60.0
_check_fn_cache: Dict[Callable, tuple[float, bool, float]] = {}
_check_fn_cache_lock = threading.Lock()


def _check_fn_cached(fn: Callable) -> bool:
    """Return a cached availability result with a short last-good grace.

    AstralNexus keeps a recently working integration visible during transient
    probe failures.  This prevents one DNS/TLS hiccup from making a tool
    disappear from the next model request while still failing closed after
    the grace period.
    """
    now = time.monotonic()
    with _check_fn_cache_lock:
        cached = _check_fn_cache.get(fn)
        if cached is not None:
            ts, value, last_good = cached
            if now - ts < _CHECK_FN_TTL:
                if value:
                    return True
                return bool(last_good and now - last_good < _CHECK_FN_FAILURE_GRACE)
    try:
        value = bool(fn())
    except Exception:
        value = False
    with _check_fn_cache_lock:
        previous = _check_fn_cache.get(fn)
        last_good = now if value else (previous[2] if previous else 0.0)
        _check_fn_cache[fn] = (now, value, last_good)
    if not value and last_good and now - last_good < _CHECK_FN_FAILURE_GRACE:
        return True
    return value


def invalidate_check_fn_cache() -> None:
    """Drop all cached ``check_fn`` results."""
    with _check_fn_cache_lock:
        _check_fn_cache.clear()


# ---------------------------------------------------------------------------
# Tool entry metadata
# ---------------------------------------------------------------------------

class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "requires_env", "is_async", "description", "emoji",
        "max_result_size_chars", "requires_approval", "schema_overrides",
    )

    def __init__(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        requires_env: Optional[List[str]] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: Optional[int] = None,
        requires_approval: bool = False,
        schema_overrides: Optional[dict] = None,
    ):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = requires_env or []
        self.is_async = is_async
        self.description = description or schema.get("description", "")
        self.emoji = emoji
        self.max_result_size_chars = max_result_size_chars
        self.requires_approval = requires_approval
        self.schema_overrides = schema_overrides or {}


# ---------------------------------------------------------------------------
# Auto-discovery helper
# ---------------------------------------------------------------------------

def _is_registry_register_call(node: ast.AST) -> bool:
    """Return True when *node* is a ``registry.register(...)`` call."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def _module_registers_tools(module_path: Path) -> bool:
    """Return True when the module has a top-level ``registry.register(...)``."""
    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
    except (OSError, SyntaxError):
        return False
    return any(_is_registry_register_call(stmt) for stmt in tree.body)


def discover_builtin_tools(tools_dir: Optional[Path] = None) -> List[str]:
    """Import self-registering tool modules and return their module names."""
    tools_path = Path(tools_dir) if tools_dir is not None else Path(__file__).resolve().parent
    module_names = [
        f"AstralNexus.tools.{path.stem}"
        for path in sorted(tools_path.glob("*.py"))
        if path.name not in {"__init__.py", "registry.py"}
        and _module_registers_tools(path)
    ]

    imported: List[str] = []
    for mod_name in module_names:
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as exc:
            logger.warning("Could not import tool module %s: %s", mod_name, exc)
    return imported


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Singleton registry that collects tool schemas and handlers."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}
        self._toolset_aliases: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._generation: int = 0

    # -- registration -------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        requires_env: Optional[List[str]] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: Optional[int] = None,
        requires_approval: bool = False,
        schema_overrides: Optional[dict] = None,
        override: bool = False,
    ) -> None:
        """Register a tool.  Called at module-import time by each tool file."""
        with self._lock:
            existing = self._tools.get(name)
            if existing and existing.toolset != toolset and not override:
                logger.error(
                    "Tool registration REJECTED: '%s' (toolset '%s') would shadow "
                    "existing tool from toolset '%s'.  Pass override=True to replace.",
                    name, toolset, existing.toolset,
                )
                return
            self._tools[name] = ToolEntry(
                name=name, toolset=toolset, schema=schema, handler=handler,
                check_fn=check_fn, requires_env=requires_env or [],
                is_async=is_async, description=description or schema.get("description", ""),
                emoji=emoji, max_result_size_chars=max_result_size_chars,
                requires_approval=requires_approval, schema_overrides=schema_overrides,
            )
            if check_fn and toolset not in self._toolset_checks:
                self._toolset_checks[toolset] = check_fn
            self._generation += 1

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is None:
                return
            toolset_still_exists = any(
                e.toolset == entry.toolset for e in self._tools.values()
            )
            if not toolset_still_exists:
                self._toolset_checks.pop(entry.toolset, None)
            self._generation += 1

    # -- queries ------------------------------------------------------------

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        with self._lock:
            return self._tools.get(name)

    def get_all_tool_names(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def get_registered_toolset_names(self) -> List[str]:
        with self._lock:
            return sorted({e.toolset for e in self._tools.values()})

    def get_tool_names_for_toolset(self, toolset: str) -> List[str]:
        with self._lock:
            return sorted(e.name for e in self._tools.values() if e.toolset == toolset)

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        with self._lock:
            return {e.name: e.toolset for e in self._tools.values()}

    def register_toolset_alias(self, alias: str, toolset: str) -> None:
        with self._lock:
            self._toolset_aliases[alias] = toolset
            self._generation += 1

    def get_toolset_alias_target(self, alias: str) -> Optional[str]:
        with self._lock:
            return self._toolset_aliases.get(alias)

    def is_toolset_available(self, toolset: str) -> bool:
        with self._lock:
            check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            if not all(os.environ.get(name) for name in self._required_env_for_toolset(toolset)):
                return False
            return bool(_check_fn_cached(check))
        except Exception:
            return False

    def _required_env_for_toolset(self, toolset: str) -> List[str]:
        with self._lock:
            return sorted({name for entry in self._tools.values()
                           if entry.toolset == toolset for name in entry.requires_env})

    def is_tool_available(self, name: str) -> bool:
        entry = self.get_entry(name)
        if entry is None:
            return False
        if any(not os.environ.get(key) for key in entry.requires_env):
            return False
        return not entry.check_fn or _check_fn_cached(entry.check_fn)

    # -- schema retrieval ---------------------------------------------------

    def get_definitions(self, tool_names: Set[str], quiet: bool = False) -> List[dict]:
        """Return OpenAI-format tool schemas for the requested tool names."""
        result = []
        with self._lock:
            snapshot = {e.name: e for e in self._tools.values()}

        for name in sorted(tool_names):
            entry = snapshot.get(name)
            if not entry:
                continue
            if not self.is_tool_available(name):
                if not quiet:
                    logger.debug("Tool %s unavailable (check failed)", name)
                continue
            schema_with_name = {**entry.schema, "name": entry.name}
            if entry.schema_overrides:
                schema_with_name.update(entry.schema_overrides)
            schema_with_name["description"] = entry.description
            result.append({"type": "function", "function": schema_with_name})
        return result

    def get_tools_schema(self) -> List[dict]:
        """Return OpenAI-format schemas for ALL available tools.
        
        Convenience method used by governed workers to provide the model
        with the full list of callable tools.
        """
        return self.get_definitions(set(self.get_all_tool_names()))

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Execute a tool handler by name, returning a JSON string."""
        entry = self.get_entry(name)
        if not entry:
            return format_tool_error(ToolNotFoundError(f"Unknown tool: {name}"), name)
        try:
            if not self.is_tool_available(name):
                return format_tool_error(ToolExecutionError(
                    f"Tool unavailable: {name}"), name)
            # Registry governance metadata must never leak into business
            # handlers, which conventionally accept only ``args``.
            caller = str(kwargs.pop("caller", "runtime"))
            approval_basis = str(kwargs.pop("approval_basis", "registry.tool_policy"))
            if entry.requires_approval:
                decision = request_approval(
                    name, args, caller=caller, basis=approval_basis,
                )
                if decision.status != "approved":
                    return json.dumps({
                        "status": decision.status,
                        "error": f"Tool requires approval: {name}",
                        "approval": decision.to_dict(),
                    }, ensure_ascii=False)
            result = entry.handler(args, **kwargs)
            if entry.max_result_size_chars and isinstance(result, str) and len(result) > entry.max_result_size_chars:
                result = result[:entry.max_result_size_chars] + "\n[tool result truncated by registry]"
            return result
        except Exception as exc:
            logger.exception("Tool %s dispatch error: %s", name, exc)
            return format_tool_error(ToolExecutionError(str(exc)), name)
        finally:
            if entry.requires_approval:
                consume_one_shot_grant(name)


# Module-level singleton
registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Helpers for tool response serialization
# ---------------------------------------------------------------------------

def tool_error(message: str, **extra: Any) -> str:
    """Return a JSON error string for tool handlers."""
    result = {"error": str(message)}
    result.update(extra)
    result.setdefault("status", "error")
    result.setdefault("failure_class", "tool_execution_failed")
    return json.dumps(result, ensure_ascii=False)


def tool_result(data: Any = None, **kwargs: Any) -> str:
    """Return a JSON result string for tool handlers."""
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
