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
from AstralNexus.runtime.run_context import get_env

import ast
import importlib
import inspect
import copy
import sys
from contextlib import contextmanager
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Iterator

from AstralNexus.core.exceptions import (
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    format_tool_error,
)
from AstralNexus.tools.approval import consume_one_shot_grant, request_approval

from .result_limits import limit_result

logger = logging.getLogger(__name__)

def _registry_error(error: Exception, name: str) -> str:
    payload = json.loads(format_tool_error(error, name))
    payload.setdefault("status", "error")
    payload.setdefault("failure_class", "tool_execution_failed")
    return json.dumps(payload, ensure_ascii=False)


# TTL for check_fn cache results (seconds).
_CHECK_FN_TTL = 30.0
_CHECK_FN_FAILURE_GRACE = 60.0
_check_fn_cache: Dict[Callable, tuple[float, bool, float]] = {}
_check_fn_cache_lock = threading.Lock()


_check_fn_flights: Dict[Callable, tuple[threading.Event, int]] = {}
_check_fn_epoch = 0
_disabled_config_lock = threading.Lock()
_disabled_raw: Optional[str] = None
_disabled_value: Optional[Set[str]] = None


def _disabled_toolsets() -> Optional[Set[str]]:
    """Invalid configuration disables registry tools, with one diagnostic."""
    global _disabled_raw, _disabled_value
    raw = get_env("ASTRALNEXUS_DISABLED_TOOLSETS", "[]")
    with _disabled_config_lock:
        if raw != _disabled_raw:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
                    raise ValueError("expected a list of strings")
                _disabled_value = set(parsed)
            except (ValueError, TypeError) as error:
                _disabled_value = None
                logger.warning("Invalid ASTRALNEXUS_DISABLED_TOOLSETS; registry tools disabled until corrected: %s", error)
            _disabled_raw = raw
        return None if _disabled_value is None else set(_disabled_value)


def _check_fn_cached(fn: Callable) -> bool:
    """One in-flight probe per function; invalidation fences old results."""
    while True:
        now = time.monotonic()
        with _check_fn_cache_lock:
            cached = _check_fn_cache.get(fn)
            if cached is not None and now - cached[0] < _CHECK_FN_TTL:
                return cached[1] or bool(cached[2] and now - cached[2] < _CHECK_FN_FAILURE_GRACE)
            flight = _check_fn_flights.get(fn)
            if flight is None:
                event = threading.Event()
                _check_fn_flights[fn] = (event, threading.get_ident())
                epoch = _check_fn_epoch
                break
            if flight[1] == threading.get_ident():
                logger.error("Recursive availability probe for %s", getattr(fn, "__name__", repr(fn)))
                return False
        flight[0].wait()
    value = False
    completed = False
    last_good = 0.0
    try:
        try:
            value = bool(fn())
        except Exception as error:
            logger.warning("Availability probe %s failed: %s", getattr(fn, "__name__", repr(fn)), error)
        completed = True
    finally:
        now = time.monotonic()
        with _check_fn_cache_lock:
            previous = _check_fn_cache.get(fn)
            last_good = now if value else (previous[2] if previous else 0.0)
            if completed and epoch == _check_fn_epoch:
                _check_fn_cache[fn] = (now, value, last_good)
            _check_fn_flights.pop(fn, None)
            event.set()
    return value or bool(last_good and now - last_good < _CHECK_FN_FAILURE_GRACE)


def invalidate_check_fn_cache() -> None:
    global _check_fn_epoch
    with _check_fn_cache_lock:
        _check_fn_cache.clear()
        _check_fn_epoch += 1


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
    tools_path = Path(__file__).resolve().parent
    if tools_dir is not None and Path(tools_dir).resolve() != tools_path:
        raise ValueError("discover_builtin_tools only supports the builtin package; external plugins must register explicitly")
    module_names = [
        f"AstralNexus.tools.{path.stem}"
        for path in sorted(tools_path.glob("*.py"))
        if path.name not in {"__init__.py", "registry.py"}
        and _module_registers_tools(path)
    ]

    imported: List[str] = []
    for mod_name in module_names:
        modules_before = set(sys.modules)
        staged: Optional[ToolRegistry] = None
        try:
            with registry.registration_transaction() as staged:
                importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as exc:
            rollback_modules = {mod_name}
            if staged is not None:
                rollback_modules.update(getattr(entry.handler, "__module__", "") for entry in staged._entries.values())
            for module in rollback_modules - modules_before:
                if module.startswith("AstralNexus.tools."):
                    sys.modules.pop(module, None)
            logger.warning("Could not import tool module %s; registrations rolled back: %s", mod_name, exc)
    return imported


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Singleton registry that collects tool schemas and handlers."""

    def __init__(self) -> None:
        self._entries: Dict[str, ToolEntry] = {}
        self._aliases: Dict[str, str] = {}
        self._staging = threading.local()
        self._lock = threading.RLock()
        self._version: int = 0

    @property
    def _tools(self) -> Dict[str, ToolEntry]:
        staged = getattr(self._staging, "registry", None)
        return staged._entries if staged is not None else self._entries

    @property
    def _toolset_aliases(self) -> Dict[str, str]:
        staged = getattr(self._staging, "registry", None)
        return staged._aliases if staged is not None else self._aliases

    @property
    def _generation(self) -> int:
        staged = getattr(self._staging, "registry", None)
        return staged._version if staged is not None else self._version

    @_generation.setter
    def _generation(self, value: int) -> None:
        staged = getattr(self._staging, "registry", None)
        if staged is not None:
            staged._version = value
        else:
            self._version = value

    @contextmanager
    def registration_transaction(self) -> Iterator["ToolRegistry"]:
        if getattr(self._staging, "registry", None) is not None:
            raise RuntimeError("nested registration transactions are unsupported")
        with self._lock:
            staged = ToolRegistry()
            staged._entries = dict(self._entries)
            staged._aliases = dict(self._aliases)
            staged._version = self._version
            base = self._version
        self._staging.registry = staged
        try:
            yield staged
            with self._lock:
                if self._version != base:
                    raise RuntimeError("registry changed during discovery; retry import")
                self._entries = staged._entries
                self._aliases = staged._aliases
                self._version = staged._version
        finally:
            self._staging.registry = None

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
        if is_async or inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(getattr(handler, "__call__", None)):
            raise ValueError("Asynchronous handlers are unsupported by synchronous ToolRegistry.dispatch")
        if max_result_size_chars is not None and (type(max_result_size_chars) is not int or max_result_size_chars <= 0):
            raise ValueError("max_result_size_chars must be a positive integer")
        with self._lock:
            existing = self._tools.get(name)
            if existing and not override:
                logger.error(
                    "Tool registration REJECTED: '%s' (toolset '%s') would shadow "
                    "existing tool from toolset '%s'.  Pass override=True to replace.",
                    name, toolset, existing.toolset,
                )
                return
            self._tools[name] = ToolEntry(
                name=name, toolset=toolset, schema=copy.deepcopy(schema), handler=handler,
                check_fn=check_fn, requires_env=list(requires_env or []),
                is_async=is_async, description=description or schema.get("description", ""),
                emoji=emoji, max_result_size_chars=max_result_size_chars,
                requires_approval=requires_approval, schema_overrides=copy.deepcopy(schema_overrides),
            )
            self._generation += 1

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is None:
                return
            self._generation += 1

    # -- queries ------------------------------------------------------------

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        with self._lock:
            entry = self._tools.get(name)
            if entry is None:
                return None
            detached = copy.copy(entry)
            detached.schema = copy.deepcopy(entry.schema)
            detached.schema_overrides = copy.deepcopy(entry.schema_overrides)
            detached.requires_env = list(entry.requires_env)
            return detached

    def get_all_tool_names(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def get_registered_toolset_names(self) -> List[str]:
        with self._lock:
            return sorted({e.toolset for e in self._tools.values()})

    def get_tool_names_for_toolset(self, toolset: str) -> List[str]:
        with self._lock:
            target = self._resolve_toolset(toolset)
            return sorted(e.name for e in self._tools.values() if e.toolset == target)

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        with self._lock:
            return {e.name: e.toolset for e in self._tools.values()}

    def register_toolset_alias(self, alias: str, toolset: str) -> None:
        with self._lock:
            if alias == toolset:
                raise ValueError("toolset alias cannot refer to itself")
            previous = self._toolset_aliases.get(alias)
            self._toolset_aliases[alias] = toolset
            if self._resolve_toolset(alias) is None:
                if previous is None:
                    self._toolset_aliases.pop(alias)
                else:
                    self._toolset_aliases[alias] = previous
                raise ValueError("toolset alias cycle")
            self._generation += 1

    def get_toolset_alias_target(self, alias: str) -> Optional[str]:
        with self._lock:
            return self._toolset_aliases.get(alias)

    def _resolve_toolset(self, toolset: str) -> Optional[str]:
        with self._lock:
            seen: Set[str] = set()
            while toolset in self._toolset_aliases:
                if toolset in seen:
                    return None
                seen.add(toolset)
                toolset = self._toolset_aliases[toolset]
            return toolset

    def _entry_available(self, entry: ToolEntry) -> bool:
        disabled = _disabled_toolsets()
        if disabled is None:
            return False
        if any(self._resolve_toolset(name) == entry.toolset for name in disabled):
            return False
        if any(not get_env(key) for key in entry.requires_env):
            return False
        return entry.check_fn is None or _check_fn_cached(entry.check_fn)

    def is_toolset_available(self, toolset: str) -> bool:
        """A toolset is usable when at least one current member is usable."""
        target = self._resolve_toolset(toolset)
        with self._lock:
            entries = [entry for entry in self._tools.values() if entry.toolset == target]
        return any(self._entry_available(entry) for entry in entries)

    def is_tool_available(self, name: str) -> bool:
        entry = self.get_entry(name)
        return entry is not None and self._entry_available(entry)

    # -- schema retrieval ---------------------------------------------------

    def get_definitions(self, tool_names: Set[str], quiet: bool = False) -> List[dict]:
        """Return OpenAI-format tool schemas for the requested tool names."""
        result = []
        with self._lock:
            snapshot = {name: self.get_entry(name) for name in self._tools}

        for name in sorted(tool_names):
            entry = snapshot.get(name)
            if not entry:
                continue
            if not self._entry_available(entry):
                if not quiet:
                    logger.debug("Tool %s unavailable (check failed)", name)
                continue
            schema_with_name = {**copy.deepcopy(entry.schema), "name": entry.name}
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
            return _registry_error(ToolNotFoundError(f"Unknown tool: {name}"), name)
        try:
            if not self._entry_available(entry):
                return _registry_error(ToolExecutionError(
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
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise ToolExecutionError("Handler returned an awaitable to synchronous dispatch")
            return limit_result(result, entry.max_result_size_chars)
        except Exception as exc:
            logger.exception("Tool %s dispatch error: %s", name, exc)
            return _registry_error(ToolExecutionError(str(exc)), name)
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
