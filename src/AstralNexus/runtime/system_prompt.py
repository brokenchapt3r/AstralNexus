"""Registry for ordered system sections, dynamic context, tool schemas, and prompt variables.

Port of ``@deepseek-ai/dsh-system-prompt`` (commit 5badb15, lib/index.js 365
lines read in full). Contract kept:

- ``SECTION_ORDERS`` / ``CONTEXT_ORDERS`` centrally owned placements;
  ``PERSONA_PREFIX_SECTION`` / ``PERSONA_SUFFIX_SECTION`` shared slot names
  (a composition REPLACES a slot by naming it — an agent preset shadows the
  deployment persona rather than duplicating it).
- ``render_prompt(assembly)``: per-section strict ``{{variable}}``
  interpolation (``interpolate: false`` keeps literal text), empty sections
  dropped, blank-line join. Unknown/malformed references throw with the
  owning section/context named; a lone ``{{`` without any later ``}}`` is
  literal prose; substituted values are never re-scanned. There is NO escape
  syntax — a group whose inner text fails the name grammar is malformed.
- ``render_context_sections(assembly)`` → per-context rendered entries with
  empties filtered; ``join_context_sections`` prefixes the standing snapshot
  supersession sentence; ``render_context_snapshot`` composes both (logging
  and model-facing snapshot text, never part of the section prompt).
- ``validate_tool_order``: duplicates refused, the ``<unlisted-tools>`` rest
  marker required. ``order_tools``: reserved name from a provider fails the
  assembly; no configured order → code-unit lexicographic; unknown configured
  names fail listing the known tools; unlisted tools insert lexicographically
  at the rest slot.
- ``SystemPrompt(ctx, config)``: cordis Service equivalent over ONE
  ScopedLayers of aggregate PromptLayer records (sections/contexts/variables
  named + runtimeContextSuppressors/toolProviders anonymous). Config:
  includeHarnessIdentity (default true → the standing identity section),
  includeRuntimeContext (default true; false installs a suppressor),
  personaPrefix/personaSuffix (default "" → the two persona slots),
  toolOrder. Registrations: section/context (finite order required),
  suppressRuntimeContext (multiple suppressors independently disposable),
  tools(provider → {schemas, knownNames?}; parameters deep-copied so the
  assembly owns them; deferLoading kept only when true), variable(name
  grammar ``^[a-z][a-z0-9_]*$``, provider may return None — rendering a
  reference to it then fails).
- ``assemble(context)``: scope from ``context["scope"]``; suppression = the
  global layer OR any chain layer owning a suppressor; variables resolved
  global-then-chain (nearest shadows); sections and contexts merged
  nearest-wins; tool providers global AND chain all contribute; sections
  sorted by (order, name code-unit), contexts by order (stable); more than
  one ``complete: true`` section throws naming them all; the assembly then
  crosses the interceptor waterfall, whose result is authoritative EXCEPT
  that an effective complete section is restored afterwards as the sole
  prompt section and suppression empties contexts post-waterfall.

Python adaptations (documented):
- cordis ``Service`` base + zod Config → plain class + ``validate_config``
  (snake_case keys, the account-wave convention; zod's default unknown-key
  stripping kept). Service name "systemPrompt" has no native registration
  surface here (the agent plane instantiates it) — recorded, not faked.
- ``ctx.waterfall(scopeTarget(this, scope), "system-prompt/assemble", …,
  fallback)`` → ``on_assemble(handler, ctx)`` registrations held in their own
  ScopedLayers and folded global → farthest ancestor → exact scope (each in
  registration order); no interceptor → the assembly itself (the upstream
  fallback arm). Handlers are synchronous (the native contract; upstream
  awaited promises).
- ``ctx.emit("system-prompt/change")`` on every registration/disposal →
  forwarded when the context exposes a callable ``emit`` surface, otherwise
  dropped (the native event surface arrives with B4/B6; the seam is kept).
- dsh-llm ``ToolDefinition`` is a TYPE-ONLY import upstream; natively the
  provider schema surface is structural ({name, description, parameters,
  deferLoading?}); the full llm tool schema arrives with the B4 llm port.
- ``structuredClone`` → ``copy.deepcopy``; async assemble → sync (providers
  and text callables are synchronous natively).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from .scope import AnonymousEntries, NamedEntries, ScopedLayers

__all__ = [
    "SECTION_ORDERS", "CONTEXT_ORDERS", "PERSONA_PREFIX_SECTION", "PERSONA_SUFFIX_SECTION",
    "TOOL_ORDER_REST", "VARIABLE_NAME", "GROUP_AT", "validate_tool_order", "order_tools",
    "render_prompt", "render_context_sections", "join_context_sections",
    "render_context_snapshot", "validate_config", "PromptLayer", "SystemPrompt",
]

LOG = logging.getLogger("astralnexus.system_prompt")

SECTION_ORDERS: Dict[str, int] = {
    "HARNESS_IDENTITY": -1000,
    "DEPLOYMENT_PERSONA_PREFIX": 0,
    "PLAN_POLICY": 500,
    "TEAM_POLICY": 600,
    "PTC_ONLY": 800,
    "FILE_REFERENCE": 900,
    "TOOL_BASH": 1000,
    "TOOL_PWSH": 1010,
    "TOOL_READ": 1100,
    "TOOL_WRITE": 1200,
    "TOOL_EDIT": 1300,
    "TOOL_GLOB": 1400,
    "TOOL_GREP": 1500,
    "TOOL_JOBS": 1600,
    "TOOL_PTY": 1700,
    "TOOL_WEB_SEARCH": 2000,
    "TOOL_WEB_FETCH": 2100,
    "TOOL_LSP": 2200,
    "TOOL_SESSION_QUERY": 2300,
    "TOOL_GOAL": 2400,
    "TOOL_WORKFLOW": 2600,
    "TOOL_RALPH": 2700,
    "TOOL_SUBAGENT": 2800,
    "TOOL_REPORT": 2900,
    "TOOL_COMPUTER_USE": 3000,
    "MCP_SERVERS": 3100,
    "TOOLS_SDK": 5000,
    "DELIVERABLE_FILE_REFERENCES": 9000,
    "STRUCTURED_OUTPUT": 9900,
    "HARNESS_SOURCE": 10000,
    "WEB_SURFACE": 10100,
    "DEPLOYMENT_PERSONA_SUFFIX": 10200,
}

CONTEXT_ORDERS: Dict[str, int] = {
    "SANDBOX_POLICY": 110,
    "APPROVAL_POLICY": 115,
    "SUBAGENT_DELEGATION": 120,
}

PERSONA_PREFIX_SECTION = "deployment:persona-prefix"
PERSONA_SUFFIX_SECTION = "deployment:persona-suffix"
VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
GROUP_AT = re.compile(r"\A\{\{([^{}]*)\}\}")
TOOL_ORDER_REST = "<unlisted-tools>"
SNAPSHOT_PREFIX = ("Current runtime context. This snapshot supersedes earlier "
                   "runtime-context snapshots.")


def validate_tool_order(tool_order: Optional[List[str]]) -> Optional[List[str]]:
    """Duplicate names and the required rest marker are checked up front."""
    if tool_order is None:
        return None
    seen = set()
    for name in tool_order:
        if name in seen:
            raise ValueError(f'toolOrder lists "{name}" more than once')
        seen.add(name)
    if TOOL_ORDER_REST not in seen:
        raise ValueError(f'toolOrder must contain the "{TOOL_ORDER_REST}" rest entry '
                         f"(where unlisted tools are inserted)")
    return tool_order


def order_tools(tools: List[Dict[str, Any]], tool_order: Optional[List[str]],
                known_names: Any) -> List[Dict[str, Any]]:
    """Apply configured order; unlisted tools go lexicographically at the rest slot."""
    if any(tool["name"] == TOOL_ORDER_REST for tool in tools):
        raise ValueError(f'tool provider returned reserved tool name "{TOOL_ORDER_REST}" '
                         f"(reserved for toolOrder's rest entry)")
    if tool_order is None:
        return sorted(tools, key=lambda tool: tool["name"])  # code-unit order
    unknown = [name for name in tool_order
               if name != TOOL_ORDER_REST and name not in known_names]
    if unknown:
        plural = "s" if len(unknown) > 1 else ""
        known_list = ", ".join(sorted(known_names)) or "(none)"
        raise ValueError(f"toolOrder lists unregistered tool{plural} "
                         f"{', '.join(f'{json.dumps(name)}' for name in unknown)}; "
                         f"known tools: {known_list}")
    listed = set(tool_order)
    rest = sorted((tool for tool in tools if tool["name"] not in listed),
                  key=lambda tool: tool["name"])
    result: List[Dict[str, Any]] = []
    for name in tool_order:
        if name == TOOL_ORDER_REST:
            result.extend(rest)
        else:
            result.extend(tool for tool in tools if tool["name"] == name)
    return result


def _interpolate(entry: Dict[str, Any], variables: Dict[str, Any], kind: str) -> str:
    """Interpolate one section/context; diagnostics name the owning input."""
    text = entry["text"]
    result = ""
    last = 0
    open_at = text.find("{{")
    while open_at >= 0:
        # GROUP_AT is string-anchored; match against the slice like upstream's
        # text.slice(open) — re.match(string, pos) would not honor \A/^ at pos.
        group = GROUP_AT.match(text[open_at:])
        if group is None:
            if text.find("}}", open_at + 2) >= 0:
                raise ValueError(
                    f'malformed prompt variable reference at "{text[open_at:open_at + 16]}…" '
                    f'in {kind} "{entry["name"]}" (references are complete simple '
                    f"{{{{name}}}} groups)")
            result += text[last:open_at + 2]  # a lone {{ with no later }} is prose
            last = open_at + 2
            open_at = text.find("{{", last)
            continue
        name = group.group(0)[2:-2]
        if not VARIABLE_NAME.match(name):
            raise ValueError(f'malformed prompt variable reference "{{{{{name}}}}}" in '
                             f'{kind} "{entry["name"]}" (variable names match '
                             f"{VARIABLE_NAME.pattern})")
        if name not in variables:
            known = list(variables.keys())
            raise ValueError(f'unknown prompt variable "{{{{{name}}}}}" in {kind} '
                             f'"{entry["name"]}"; registered variables: '
                             f"{', '.join(known) if known else '(none)'}")
        value = variables[name]
        if value is None:
            raise ValueError(f'prompt variable "{{{{{name}}}}}" has no value for this '
                             f'assembly ({kind} "{entry["name"]}")')
        result += text[last:open_at] + value  # substituted values are not re-scanned
        last = open_at + len(group.group(0))
        open_at = text.find("{{", last)
    return result + text[last:]


def render_prompt(assembly: Dict[str, Any]) -> str:
    """Interpolate strict references, drop empty sections, join with blank lines."""
    rendered = [section["text"] if section.get("interpolate") is False
                else _interpolate(section, assembly["variables"], "section")
                for section in assembly["sections"]]
    return "\n\n".join(text for text in rendered if len(text) > 0)


def render_context_sections(assembly: Dict[str, Any]) -> List[Dict[str, str]]:
    """One entry per contributing context that rendered to non-empty text."""
    sections = [{"name": context["name"],
                 "text": _interpolate(context, assembly["variables"], "context")}
                for context in assembly["contexts"]]
    return [section for section in sections if len(section["text"]) > 0]


def join_context_sections(sections: List[Dict[str, str]]) -> str:
    """The model-facing snapshot text for an already-rendered section list."""
    body = "\n\n".join(section["text"] for section in sections)
    if len(body) == 0:
        return ""
    return f"{SNAPSHOT_PREFIX}\n\n{body}"


def render_context_snapshot(assembly: Dict[str, Any]) -> str:
    """The complete dynamic context snapshot ('' when no context is active)."""
    return join_context_sections(render_context_sections(assembly))


CONFIG_DEFAULTS: Dict[str, Any] = {
    "include_harness_identity": True,
    "include_runtime_context": True,
    "persona_prefix": "",
    "persona_suffix": "",
    "tool_order": None,
}


def validate_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """zod-equivalent coercion: defaults filled, types checked, unknown keys stripped."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("system-prompt: config must be a mapping")
    resolved = dict(CONFIG_DEFAULTS)
    for name, value in config.items():
        if name not in CONFIG_DEFAULTS:
            continue  # zod's default object behavior strips unknown keys
        resolved[name] = value
    for name in ("include_harness_identity", "include_runtime_context"):
        if not isinstance(resolved[name], bool):
            raise ValueError(f"system-prompt: config {name} must be a boolean")
    for name in ("persona_prefix", "persona_suffix"):
        if not isinstance(resolved[name], str):
            raise ValueError(f"system-prompt: config {name} must be a string")
    tool_order = resolved["tool_order"]
    if tool_order is not None:
        if not isinstance(tool_order, list) or \
                any(not isinstance(item, str) for item in tool_order):
            raise ValueError("system-prompt: config tool_order must be a list of strings")
    resolved["tool_order"] = validate_tool_order(tool_order)
    return resolved


class PromptLayer:
    """All prompt registrations owned by one global or scoped layer."""

    __slots__ = ("scope", "sections", "contexts", "variables",
                 "runtime_context_suppressors", "tool_providers")

    def __init__(self, scope: Optional[Any] = None):
        self.scope = scope

        def duplicate(label: str) -> Callable[[str], Exception]:
            def error(name: str) -> Exception:
                if scope is None:
                    return ValueError(f'prompt {label} "{name}" is already registered '
                                      f"(for a per-agent override, register through "
                                      f"that agent's `agent.ctx` instead)")
                return ValueError(f'prompt {label} "{name}" is already registered '
                                  f"in this scope")
            return error

        self.sections = NamedEntries(duplicate("section"))
        self.contexts = NamedEntries(duplicate("context"))
        self.variables = NamedEntries(duplicate("variable"))
        self.runtime_context_suppressors = AnonymousEntries()
        self.tool_providers = AnonymousEntries()

    def is_empty(self) -> bool:
        return (self.sections.is_empty() and self.contexts.is_empty()
                and self.runtime_context_suppressors.is_empty()
                and self.tool_providers.is_empty() and self.variables.is_empty())


def _finite_order(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SystemPrompt:
    """Registry service for the prompt inputs assembled before each model step."""

    def __init__(self, ctx: Any = None, config: Optional[Dict[str, Any]] = None,
                 logger: Optional[logging.Logger] = None):
        self.ctx = ctx
        self.logger = logger or LOG
        resolved = validate_config(config)
        self.tool_order: Optional[List[str]] = resolved["tool_order"]

        def on_change() -> None:
            emit = getattr(ctx, "emit", None)
            if callable(emit):
                emit("system-prompt/change")

        self.layers = ScopedLayers(PromptLayer, on_change)
        self.assembles = ScopedLayers(lambda scope: AnonymousEntries(), lambda: None)

        if resolved["include_harness_identity"]:
            self.section({"name": "harness:identity",
                          "order": self.get_section_order("HARNESS_IDENTITY"),
                          "text": "You are an AI agent powered by DeepSeek Harness."})
        self.section({"name": PERSONA_PREFIX_SECTION,
                      "order": self.get_section_order("DEPLOYMENT_PERSONA_PREFIX"),
                      "text": resolved["persona_prefix"]})
        self.section({"name": PERSONA_SUFFIX_SECTION,
                      "order": self.get_section_order("DEPLOYMENT_PERSONA_SUFFIX"),
                      "text": resolved["persona_suffix"]})
        if not resolved["include_runtime_context"]:
            self.suppress_runtime_context()

    # --------------------------------------------------------- registration

    def get_section_order(self, name: str) -> Optional[int]:
        """The centrally owned placement of a repository prompt section."""
        return SECTION_ORDERS.get(name)

    def get_context_order(self, name: str) -> Optional[int]:
        """The centrally owned placement of a repository runtime context."""
        return CONTEXT_ORDERS.get(name)

    def section(self, section: Dict[str, Any]) -> Callable[[], None]:
        """Register an ordered prompt section in the calling context's scope."""
        if not _finite_order(section.get("order")):
            raise TypeError(f'prompt section "{section.get("name")}" order must be '
                            f"a finite number")
        return self.layers.effect(self.ctx,
                                  lambda layer: layer.sections.insert(section["name"], section),
                                  {"label": "systemPrompt.section()"})

    def context(self, context: Dict[str, Any]) -> Callable[[], None]:
        """Register ordered dynamic context in the calling context's scope."""
        if not _finite_order(context.get("order")):
            raise TypeError(f'prompt context "{context.get("name")}" order must be '
                            f"a finite number")
        return self.layers.effect(self.ctx,
                                  lambda layer: layer.contexts.insert(context["name"], context),
                                  {"label": "systemPrompt.context()"})

    def suppress_runtime_context(self) -> Callable[[], None]:
        """Suppress every dynamic runtime-context contribution in this scope."""
        return self.layers.effect(
            self.ctx, lambda layer: layer.runtime_context_suppressors.append(True),
            {"label": "systemPrompt.suppressRuntimeContext()"})

    def tools(self, provider: Callable[[Any], Dict[str, Any]]) -> Callable[[], None]:
        """Register a tool-schema provider; global and scoped providers both contribute."""
        if not callable(provider):
            raise TypeError("system-prompt: tool provider must be callable")
        return self.layers.effect(self.ctx,
                                  lambda layer: layer.tool_providers.append(provider),
                                  {"label": "systemPrompt.tools()"})

    def variable(self, name: str,
                 provider: Callable[[Any], Any]) -> Callable[[], None]:
        """Register a prompt variable; scoped values shadow globals."""
        if not isinstance(name, str) or not VARIABLE_NAME.match(name):
            raise ValueError(f'invalid prompt variable name "{name}" (must match '
                             f"{VARIABLE_NAME.pattern})")
        if not callable(provider):
            raise TypeError("system-prompt: variable provider must be callable")
        return self.layers.effect(self.ctx,
                                  lambda layer: layer.variables.insert(name, provider),
                                  {"label": "systemPrompt.variable()"})

    def on_assemble(self, handler: Callable[[Dict[str, Any], Any], Dict[str, Any]],
                    ctx: Optional[Any] = None) -> Callable[[], None]:
        """Register an assemble interceptor (the native waterfall listener seam)."""
        if not callable(handler):
            raise TypeError("system-prompt: assemble handler must be callable")
        return self.assembles.effect(ctx if ctx is not None else self.ctx,
                                     lambda layer: layer.append(handler),
                                     {"label": "systemPrompt.onAssemble()"})

    # ------------------------------------------------------------- assembly

    def assemble(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Global + scoped composition, canonical ordering, interceptor waterfall."""
        if context is None:
            context = {}
        scope = context.get("scope")
        scope_layers = self.layers.chain_layers(scope)
        runtime_context_suppressed = \
            not self.layers.global_layer.runtime_context_suppressors.is_empty() or \
            any(not layer.runtime_context_suppressors.is_empty() for layer in scope_layers)

        variables: Dict[str, Any] = {}
        for name, provider in self.layers.global_layer.variables.entries():
            variables[name] = provider(context)
        for layer in scope_layers:
            for name, provider in layer.variables.entries():
                variables[name] = provider(context)  # nearest scope shadows

        section_by_name = self.layers.merge(scope, lambda layer: layer.sections)
        context_by_name = self.layers.merge(scope, lambda layer: layer.contexts)
        providers = list(self.layers.global_layer.tool_providers.values())
        for layer in scope_layers:
            providers.extend(layer.tool_providers.values())

        collected: List[Dict[str, Any]] = []
        known_names = set()
        for provider in providers:
            result = provider(context)
            schemas = []
            for schema in result["schemas"]:
                detached: Dict[str, Any] = {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": copy.deepcopy(schema["parameters"]),
                }
                if schema.get("deferLoading") is True:
                    detached["deferLoading"] = True
                schemas.append(detached)
            accepted = result.get("knownNames")
            if accepted is None:
                accepted = [tool["name"] for tool in schemas]
            collected.extend(schemas)
            for name in accepted:
                known_names.add(name)

        section_definitions = sorted(section_by_name.values(),
                                     key=lambda entry: (entry["order"], entry["name"]))
        complete_sections = [section for section in section_definitions
                             if section.get("complete") is True]
        if len(complete_sections) > 1:
            names = ", ".join(json.dumps(section["name"]) for section in complete_sections)
            raise ValueError(f"multiple complete prompt sections are active: {names}")

        complete_section: Optional[Dict[str, Any]] = None
        sections: List[Dict[str, Any]] = []
        for section in section_definitions:
            text = section["text"]
            assembled: Dict[str, Any] = {
                "name": section["name"],
                "text": text(context) if callable(text) else text,
            }
            if section.get("interpolate") is not None:
                assembled["interpolate"] = section["interpolate"]
            if section.get("complete") is True:
                complete_section = dict(assembled)
            sections.append(assembled)

        assembly: Dict[str, Any] = {
            "sections": sections,
            "contexts": [] if runtime_context_suppressed else [
                {"name": entry["name"],
                 "text": entry["text"](context) if callable(entry["text"]) else entry["text"]}
                for entry in sorted(context_by_name.values(),
                                    key=lambda item: item["order"])],
            "tools": order_tools(collected, self.tool_order, known_names),
            "variables": variables,
        }

        transformed = self._run_assemble_waterfall(scope, assembly, context)
        if complete_section is None and not runtime_context_suppressed:
            return transformed
        result = dict(transformed)
        result["sections"] = transformed["sections"] if complete_section is None \
            else [complete_section]
        result["contexts"] = [] if runtime_context_suppressed else transformed["contexts"]
        return result

    def _run_assemble_waterfall(self, scope: Any, assembly: Dict[str, Any],
                                context: Any) -> Dict[str, Any]:
        """The native waterfall arm: fold listeners, fall back to the assembly."""
        value = assembly
        listeners: List[Callable[[Dict[str, Any], Any], Dict[str, Any]]] = \
            list(self.assembles.global_layer.values())
        for layer in self.assembles.chain_layers(scope):  # farthest ancestor first
            listeners.extend(layer.values())
        for handler in listeners:
            value = handler(value, context)
        return value
