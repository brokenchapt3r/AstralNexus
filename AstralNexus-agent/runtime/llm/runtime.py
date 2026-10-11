"""LLM service: adapter registry with a waterfall-interceptable streaming
call API.

Port of ``@deepseek-ai/dsh-llm`` lib/types/index.js (commit 5badb150).
Exports the ``LlmRuntime`` service class, the abstract ``LlmAdapter`` for
provider backends, ``LlmError``, and ``assert_usable_api_key``.

Python adaptations (documented):
- Promise-returning methods become synchronous (no event loop is implied by
  the native host; adapters answer directly). ``AsyncIterable`` streams
  become iterators/generators; the ``iterator.return()`` close arm becomes
  the Python iterator ``close()``.
- The Cordis event/waterfall faces (``ctx.events.dispatch('emit', …)``,
  ``ctx.waterfall(this, 'llm/stream', …)``) become the service's own
  listener registries with ``on_adapters_updated`` / ``on_stream`` seams —
  the same adaptation the credential-seam and system-prompt ports carry;
  ``prepend`` becomes ``prepend=True`` on ``on_stream``. The INVARIANT
  collection/rethrow and contained-listener semantics are verbatim.
- ``ctx.effect(generator, label)`` keeps the scope-port seam: the setup arm
  runs synchronously (Cordis runs the effect body to its first yield
  eagerly) and the teardown closure is registered through ``ctx.effect``
  when the context provides one, else returned caller-owned.
- ``Object.isFrozen(options)`` becomes the MappingProxyType check (the
  ``deep_freeze`` projection); ``structuredClone`` becomes the message
  module's recursive detach.
- A JS function object carrying ``replace`` becomes a callable handle
  class.
- Plain ``Error`` throws become ``ValueError``.
"""
from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterator, List, Optional

from ..dshutil.values import deep_freeze
from ..typert.protocol import RemoteError, TypertRemoteService, remote
from .adapter_failure import normalize_llm_failure
from .api_key import normalize_api_key
from .call_config import call_config_equals
from .content import (content_has_file, content_has_image, file_handle_text,
                      project_files_to_text, project_images_for_text_model,
                      project_tool_updates)
from .errors import HarnessError, INVALID_CREDENTIAL_CODE
from .message import _detach, freeze_message
from .retry_policy import resolve_retry_policy

__all__ = ["LlmAdapter", "LlmError", "LlmRuntime", "adapter_failure_chunk",
           "assert_usable_api_key"]

_MAX_SAFE_INTEGER = 2 ** 53 - 1
_LOGGER = logging.getLogger("astralnexus.llm")


class LlmError(HarnessError):
    """Typed error for LLM-related failures. Extends HarnessError, so the
    ``code`` string (e.g. AUTH, RATE_LIMIT, NO_ADAPTER) is shared taxonomy."""

    def __init__(self, message: str, code: str,
                 options: Optional[Dict[str, Any]] = None) -> None:
        if not isinstance(message, str) or message == "":
            raise ValueError("LlmError message must be a non-empty string")
        if not isinstance(code, str) or code == "":
            raise ValueError("LlmError code must be a non-empty string")
        status = (options or {}).get("status")
        if status is not None and (isinstance(status, bool)
                                   or not isinstance(status, int)
                                   or status < 100 or status > 599):
            raise ValueError(
                "LlmError status must be an integer from 100 through 599")
        retry_after = (options or {}).get("providerRetryAfterMs")
        if retry_after is not None and (
                isinstance(retry_after, bool)
                or not isinstance(retry_after, (int, float))
                or not _is_finite(retry_after) or retry_after <= 0):
            raise ValueError(
                "LlmError providerRetryAfterMs must be a positive finite "
                "number")
        request_id = (options or {}).get("requestId")
        if request_id is not None and (not isinstance(request_id, str)
                                       or request_id == ""):
            raise ValueError("LlmError requestId must be a non-empty string")
        super().__init__(message, code, options)
        self.name = "LlmError"
        failure: Dict[str, Any] = {"message": message, "code": code}
        if status is not None:
            failure["status"] = status
        if retry_after is not None:
            failure["providerRetryAfterMs"] = retry_after
        if request_id is not None:
            failure["requestId"] = request_id
        offload_images = (options or {}).get("offloadImages")
        if offload_images is not None:
            failure["offloadImages"] = offload_images
        # Serializable facts retained beside this live Error.
        self.failure = MappingProxyType(failure)


def _is_finite(value: Any) -> bool:
    import math
    return isinstance(value, (int, float)) and math.isfinite(value)


def _is_safe_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and abs(value) <= _MAX_SAFE_INTEGER


def assert_usable_api_key(raw: str, pkg: str, ref: str) -> str:
    """Accept one supplied credential, or refuse it as unusable.

    A stored key arrives from the credentials seam, a .env line, or a shell
    export, all of which pick up surrounding whitespace, so trimming is
    silent. Anything else fails here rather than inside the HTTP client. The
    key never enters the message: ``ref`` names where to fix it.
    """
    checked = normalize_api_key(raw)
    if checked["ok"]:
        return checked["value"]
    if checked["reason"] == "empty":
        raise LlmError(
            f"{pkg}: the API key resolved from {ref} is blank; set {ref} to "
            f"the raw key (the web Models page writes it) or export it in "
            "the launching environment", INVALID_CREDENTIAL_CODE)
    raise LlmError(
        f"{pkg}: the API key resolved from {ref} contains characters no HTTP "
        f"header can carry; set {ref} to the raw key alone (the web Models "
        "page writes it)", INVALID_CREDENTIAL_CODE)


class LlmAdapter:
    """Provider-wire adapter for the harness message and stream vocabulary.

    Register implementations with ``llm.register_adapter(providers, adapter)``.
    Every provider HTTP request must include ``attribution_headers()``.
    """

    def provider_info(self, provider: str) -> Dict[str, str]:
        """Describe one provider route owned by this adapter."""
        return {"id": provider, "name": provider}

    def provider_retry_policy(self, provider: str) -> Optional[Any]:
        """Return the provider-owned retry policy captured with this route,
        or None to use the normal defaults."""
        return None

    def image_request_pricing(self, provider: str,
                              model: str) -> Optional[Any]:
        """Resolve provider-side request-image pricing for one exact model
        route. The default declares none, so consumers fall back to their
        own neutral estimate. Implementations must answer synchronously
        without I/O."""
        return None

    def list_models(self, provider: str) -> List[Any]:
        """List models this adapter can currently advertise for one owned
        provider (adapter-preferred order)."""
        return []

    def resolve_model(self, provider: str, model: str,
                      signal: Optional[Any] = None) -> Dict[str, Any]:
        """Resolve all metadata available for one exact model."""
        return {"provider": provider, "id": model, "name": model}

    def prepare_call(self, provider: str, model: str,
                     signal: Optional[Any] = None) -> Dict[str, Any]:
        """Bind exact model metadata and the eventual request dispatch to
        one adapter generation."""
        return {
            "model": self.resolve_model(provider, model, signal),
            "stream": lambda options: self.stream(options),
        }

    def stream(self, options: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """Stream one model call as raw chunks (abstract upstream)."""
        raise NotImplementedError(
            "LlmAdapter.stream is abstract; concrete adapters implement it")


class _RegistrationHandle:
    """Callable disposer carrying ``replace`` (the JS function-object with a
    property, natively)."""

    __slots__ = ("_dispose", "_replace")

    def __init__(self, dispose: Callable[[], None],
                 replace: Callable[[Any], None]) -> None:
        self._dispose = dispose
        self._replace = replace

    def __call__(self) -> None:
        self._dispose()

    def replace(self, next_value: Any) -> None:
        self._replace(next_value)


class LlmRuntime(TypertRemoteService):
    """The abstract ``llm`` service: an adapter registry plus a streaming
    model-call API, interceptable via the ``llm/stream`` waterfall."""

    def __init__(self, ctx: Any) -> None:
        super().__init__("llm")
        self.ctx = ctx
        self.adapters: Dict[str, Dict[str, Any]] = {}
        self.directory: Dict[str, Dict[str, Any]] = {}
        self.discoveries: Dict[str, Callable[..., Any]] = {}
        self._adapters_listeners: List[Callable[[], Any]] = []
        self._stream_interceptors: List[Callable[..., Any]] = []

    # ------------------------------------------------------------- ctx seams

    def _register_effect(self, teardown: Callable[[], None],
                         label: str) -> Callable[[], None]:
        """Own one teardown through the context's effect seam when present
        (scope-port precedent), else caller-owned."""
        register = getattr(self.ctx, "effect", None)
        if callable(register):
            return register(teardown, label)
        return teardown

    def _service(self, key: str) -> Any:
        getter = getattr(self.ctx, "get", None)
        if callable(getter):
            return getter(key)
        return None

    @property
    def _logger(self) -> Any:
        logger = getattr(self.ctx, "logger", None)
        return logger if logger is not None else _LOGGER

    # ------------------------------------------------- listener registries

    def on_adapters_updated(self, listener: Callable[[], Any]
                            ) -> Callable[[], None]:
        """Register an ``llm/adapters-updated`` listener (the native event
        seam); returns the unsubscribe."""
        if not callable(listener):
            raise TypeError("llm: adapters-updated listener must be callable")
        self._adapters_listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._adapters_listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def on_stream(self, handler: Callable[..., Any],
                  prepend: bool = False) -> Callable[[], None]:
        """Register an ``llm/stream`` waterfall interceptor (the native
        waterfall listener seam); returns the unsubscribe."""
        if not callable(handler):
            raise TypeError("llm: stream interceptor must be callable")
        if prepend:
            self._stream_interceptors.insert(0, handler)
        else:
            self._stream_interceptors.append(handler)

        def unsubscribe() -> None:
            try:
                self._stream_interceptors.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def emit_adapters_updated(self) -> None:
        """Notify topology observers without letting one broken listener veto
        the commit."""
        # One synchronous throw must not starve later listeners. Registry
        # notifications are non-vetoing, so contain each callback
        # independently; INVARIANT-coded failures still surface.
        invariant_failure: Optional[BaseException] = None
        for listener in list(self._adapters_listeners):
            try:
                returned = listener()
                if returned is not None and _is_thenable(returned):
                    # An emit listener may still be an async function; its
                    # rejection cannot reach the synchronous INVARIANT
                    # rethrow below, so it is contained here instead of
                    # becoming an unhandled rejection.
                    _contain_awaitable(self, returned)
            except BaseException as error:
                if getattr(error, "code", None) == "INVARIANT":
                    if invariant_failure is None:
                        invariant_failure = error
                    continue
                self._warn_adapters_listener_failure(error)
        if invariant_failure is not None:
            raise invariant_failure

    def _warn_adapters_listener_failure(self, error: Any) -> None:
        """Contained-listener diagnostic shared by the sync and async
        failure paths."""
        self._logger.warn("llm: an llm/adapters-updated listener failed") \
            if hasattr(self._logger, "warn") \
            else self._logger.warning(
                "llm: an llm/adapters-updated listener failed")
        self._logger.warn(error) if hasattr(self._logger, "warn") \
            else self._logger.warning(str(error))

    # ------------------------------------------------------------ registry

    def register_adapter(self, providers: List[str],
                         adapter: LlmAdapter) -> _RegistrationHandle:
        """Register an adapter for the given provider routes. Throws LlmError
        with code DUPLICATE_ADAPTER if any provider already has an adapter
        (all-or-nothing). Disposed with the fiber.

        :returns: the disposer handle, carrying ``replace``.
        """
        # The routes this registration currently holds; replace rewrites it,
        # and the disposer releases whatever it holds at disposal time.
        owned: List[str] = []
        # The disposer has run: owned being empty cannot say so on its own,
        # because replace([]) legally leaves a live registration holding
        # none.
        released = [False]

        def teardown() -> None:
            released[0] = True
            for provider in owned:
                self.adapters.pop(provider, None)
            owned.clear()
            self.emit_adapters_updated()

        # The Cordis effect body runs synchronously to its first yield, so a
        # setup failure propagates to the caller before any disposer exists.
        if len(providers) == 0:
            raise LlmError("an adapter must register at least one provider",
                           "INVALID_ADAPTER")
        self._commit_routes(owned,
                            self._prepare_routes(providers, adapter, owned))
        dispose = self._register_effect(teardown, "llm.registerAdapter()")

        def replace(next_providers: List[str]) -> None:
            # Registering here would leak: the effect's disposer already
            # ran, so nothing remains to release whatever this call would
            # put in the map.
            if released[0]:
                raise LlmError(
                    "a disposed adapter registration cannot replace its "
                    "routes", "REGISTRATION_DISPOSED")
            self._commit_routes(
                owned, self._prepare_routes(next_providers, adapter, owned))

        return _RegistrationHandle(dispose, replace)

    def _prepare_routes(self, providers: List[str], adapter: LlmAdapter,
                        owned: List[str]) -> List[Dict[str, Any]]:
        """Validate one candidate route set for ``adapter``, treating routes
        this registration already holds as available. Nothing is mutated: a
        rejected candidate leaves the registry exactly as it was."""
        unique = set()
        registrations: List[Dict[str, Any]] = []
        for provider in providers:
            if len(provider) == 0:
                raise LlmError("adapter provider names must be non-empty",
                               "INVALID_ADAPTER")
            if provider in unique or (provider in self.adapters
                                      and provider not in owned):
                raise LlmError(
                    f'an adapter for provider "{provider}" is already '
                    "registered", "DUPLICATE_ADAPTER")
            info = adapter.provider_info(provider)
            if not isinstance(info.get("id"), str) or info["id"] != provider \
                    or not isinstance(info.get("name"), str) \
                    or info["name"] == "":
                raise LlmError(
                    f'adapter metadata for provider "{provider}" must '
                    "preserve its id and have a non-empty name",
                    "INVALID_ADAPTER")
            unique.add(provider)
            policy = adapter.provider_retry_policy(provider)
            retry_policy = policy if policy is not None \
                else resolve_retry_policy(
                    None, f'llm: provider "{provider}" retryPolicy')
            registrations.append({
                "adapter": adapter,
                "provider": {"id": info["id"], "name": info["name"]},
                "retryPolicy": retry_policy,
            })
        return registrations

    def _commit_routes(self, owned: List[str],
                       registrations: List[Dict[str, Any]]) -> None:
        """Swap this registration's routes for the prepared ones in one
        synchronous section, so no observer can see the registry between the
        release and the re-registration."""
        for provider in owned:
            self.adapters.pop(provider, None)
        owned.clear()
        for registration in registrations:
            self.adapters[registration["provider"]["id"]] = registration
            owned.append(registration["provider"]["id"])
        self.emit_adapters_updated()

    @remote
    def list_providers(self) -> List[Dict[str, str]]:
        """Describe provider routes with a registered adapter, in
        registration order (detached)."""
        return [{**registration["provider"]}
                for registration in self.adapters.values()]

    def register_configurable_providers(
            self, entries: List[Dict[str, Any]]) -> _RegistrationHandle:
        """Declare provider routes an adapter plugin can activate through
        configuration. Registration is all-or-nothing. Disposed with the
        fiber."""
        held: List[Dict[str, Any]] = []
        disposed = [False]

        def commit(candidates: List[Dict[str, Any]]) -> None:
            """Validate a candidate set in full against everything this
            registration does not already hold, then publish it."""
            detached: List[Dict[str, Any]] = []
            own = {entry["provider"] for entry in held}
            for entry in candidates:
                if entry["provider"] == "" or entry["displayName"] == "" \
                        or entry["settingsNs"] == "":
                    raise LlmError(
                        "configurable providers need a non-empty provider, "
                        "displayName, and settingsNs", "INVALID_DIRECTORY")
                if any(segment == "" for segment in entry["settingsPath"]):
                    raise LlmError(
                        f'configurable provider "{entry["provider"]}" has an '
                        "empty settingsPath segment", "INVALID_DIRECTORY")
                if (entry["provider"] in self.directory
                        and entry["provider"] not in own) \
                        or any(seen["provider"] == entry["provider"]
                               for seen in detached):
                    raise LlmError(
                        f'configurable provider "{entry["provider"]}" is '
                        "already declared", "DUPLICATE_DIRECTORY")
                detached.append({**entry,
                                 "settingsPath": list(entry["settingsPath"])})
            for entry in held:
                self.directory.pop(entry["provider"], None)
            for entry in detached:
                self.directory[entry["provider"]] = entry
            held.clear()
            held.extend(detached)
            self.emit_adapters_updated()

        if len(entries) == 0:
            raise LlmError(
                "a configurable-provider registration must declare at least "
                "one provider", "INVALID_DIRECTORY")
        commit(entries)

        def teardown() -> None:
            disposed[0] = True
            for entry in held:
                self.directory.pop(entry["provider"], None)
            held.clear()
            self.emit_adapters_updated()

        dispose = self._register_effect(
            teardown, "llm.registerConfigurableProviders()")

        def replace(next_entries: List[Dict[str, Any]]) -> None:
            if disposed[0]:
                raise LlmError(
                    "this configurable-provider registration was disposed",
                    "REGISTRATION_DISPOSED")
            commit(next_entries)

        return _RegistrationHandle(dispose, replace)

    @remote
    def list_configurable_providers(self) -> List[Dict[str, Any]]:
        """List every declared configurable provider, registered or dormant
        (detached, in declaration order)."""
        return [{**entry, "settingsPath": list(entry["settingsPath"])}
                for entry in self.directory.values()]

    def register_model_discovery(
            self, settings_ns: str,
            discover: Callable[..., Any]) -> Callable[[], None]:
        """Offer to interrogate provider endpoints on behalf of the settings
        namespace this plugin owns. Disposed with the fiber."""
        if settings_ns == "":
            raise LlmError("model discovery needs a non-empty settings "
                           "namespace", "INVALID_DISCOVERY")
        if settings_ns in self.discoveries:
            raise LlmError(
                f'model discovery for "{settings_ns}" is already registered',
                "DUPLICATE_DISCOVERY")
        self.discoveries[settings_ns] = discover

        def teardown() -> None:
            self.discoveries.pop(settings_ns, None)

        return self._register_effect(
            teardown, "llm.registerModelDiscovery()")

    def discover_models(self, settings_ns: str, request: Dict[str, Any],
                        signal: Optional[Any] = None) -> List[Dict[str, Any]]:
        """Interrogate one provider endpoint for the models it advertises.
        The reply is candidate metadata a surface may offer for adoption."""
        discover = self.discoveries.get(settings_ns)
        if discover is None:
            raise LlmError(
                f'no model discovery is registered for "{settings_ns}"',
                "NO_DISCOVERY")
        # One of the two identifies what to describe: a route the adapter
        # knows, or an endpoint to ask. Neither leaves nothing to answer
        # about.
        if (request.get("provider") or "") == "" \
                and (request.get("baseURL") or "") == "":
            raise LlmError(
                "model discovery needs a provider route or a baseURL",
                "INVALID_DISCOVERY")
        discovered = discover(request) if signal is None \
            else discover(request, signal)
        seen = set()
        models: List[Dict[str, Any]] = []
        for model in discovered:
            model_id = model.get("id")
            if not isinstance(model_id, str) or model_id == "" \
                    or model_id in seen:
                continue
            seen.add(model_id)
            entry: Dict[str, Any] = {"id": model_id}
            if model.get("name") is not None:
                entry["name"] = model["name"]
            if model.get("contextWindow") is not None:
                entry["contextWindow"] = model["contextWindow"]
            if model.get("maxTokens") is not None:
                entry["maxTokens"] = model["maxTokens"]
            if model.get("inputModalities") is not None:
                entry["inputModalities"] = list(model["inputModalities"])
            models.append(entry)
        return models

    @remote("discoverModels")
    def remote_discover_models(self, settings_ns: str,
                               request: Dict[str, Any],
                               signal: Any) -> List[Dict[str, Any]]:
        """Remote adapter for one draft provider interrogation.

        :raises RemoteError: with ``llm/model-discovery-rejected`` when
            discovery refuses or fails.
        """
        try:
            return self.discover_models(settings_ns, request, signal)
        except BaseException as error:
            message = str(error)
            details: Dict[str, Any] = {"settingsNs": settings_ns}
            if request.get("baseURL") is not None:
                details["baseURL"] = request["baseURL"]
            raise RemoteError("llm/model-discovery-rejected", message,
                              details, cause=error) from error

    def provider_retry_policy(self, provider: str) -> Any:
        """Resolve the retry policy captured when one provider route was
        registered (normal defaults already resolved)."""
        return self.registration(provider)["retryPolicy"]

    def image_request_pricing(self, provider: str,
                              model: str) -> Optional[Any]:
        """Resolve provider-side request-image pricing for one exact route,
        or None when the provider is unregistered or declares none."""
        registration = self.adapters.get(provider)
        return registration["adapter"].image_request_pricing(provider, model) \
            if registration is not None else None

    def file_request_text(self, ref: Any) -> str:
        """Resolve the exact text one durable file occurrence contributes to
        every provider request in the current execution environment."""
        return file_handle_text(ref, self.file_read_path(ref))

    def _detached_modalities(self, modalities: Optional[Any]
                             ) -> Optional[List[Any]]:
        """Detach typed adapter-owned modality metadata."""
        return None if modalities is None else list(modalities)

    def list_models(self, provider: str) -> List[Dict[str, Any]]:
        """Discover models advertised by one registered provider (detached,
        adapter-preferred order). Catalog membership does not constrain core
        routing."""
        adapter = self.registration(provider)["adapter"]
        models = adapter.list_models(provider)
        seen = set()
        result: List[Dict[str, Any]] = []
        for model in models:
            model_id = model.get("id")
            if not isinstance(model.get("provider"), str) \
                    or model["provider"] != provider \
                    or not isinstance(model_id, str) or model_id == "" \
                    or not isinstance(model.get("name"), str) \
                    or model["name"] == "" \
                    or (model.get("description") is not None
                        and not isinstance(model["description"], str)) \
                    or model_id in seen:
                raise LlmError(
                    f"adapter returned invalid or duplicate model metadata "
                    f'for provider "{provider}"', "INVALID_CATALOG")
            seen.add(model_id)
            input_modalities = self._detached_modalities(
                model.get("inputModalities"))
            entry: Dict[str, Any] = {"provider": model["provider"],
                                     "id": model_id, "name": model["name"]}
            if model.get("description") is not None:
                entry["description"] = model["description"]
            if input_modalities is not None:
                entry["inputModalities"] = input_modalities
            result.append(entry)
        return result

    def resolve_model_info(self, provider: str, model: str,
                           signal: Optional[Any] = None) -> Dict[str, Any]:
        """Resolve and validate all metadata from the adapter that owns one
        exact route (detached)."""
        return self._resolve_model_info_for(self.registration(provider),
                                            model, signal)

    def _resolve_model_info_for(self, registration: Dict[str, Any],
                                model: str,
                                signal: Optional[Any]) -> Dict[str, Any]:
        resolved = registration["adapter"].resolve_model(
            registration["provider"]["id"], model, signal)
        return self._normalize_model_info(registration, model, resolved)

    def _normalize_model_info(self, registration: Dict[str, Any], model: str,
                              resolved: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and detach one adapter-returned exact model result."""
        provider = registration["provider"]["id"]
        if not isinstance(resolved.get("provider"), str) \
                or resolved["provider"] != provider \
                or not isinstance(resolved.get("id"), str) \
                or resolved["id"] != model \
                or not isinstance(resolved.get("name"), str) \
                or resolved["name"] == "" \
                or (resolved.get("description") is not None
                    and not isinstance(resolved["description"], str)):
            raise LlmError(
                f"adapter returned invalid exact model metadata for provider "
                f'"{provider}" model "{model}"', "INVALID_MODEL_INFO")
        context = resolved.get("context")
        if context is not None and (
                isinstance(context.get("contextWindow"), bool)
                or not isinstance(context.get("contextWindow"), int)
                or context["contextWindow"] <= 0):
            raise LlmError(
                f"adapter returned invalid context metadata for provider "
                f'"{provider}" model "{model}"', "INVALID_MODEL_CONTEXT")
        # Capability metadata rides through: an explicit modality omission is
        # negative capability downstream preflights act on (image admission).
        input_modalities = self._detached_modalities(
            resolved.get("inputModalities"))
        # Widened: adapters derive this mode from catalog config, so the
        # value is checked as a string.
        system_prompt_update = resolved.get("systemPromptUpdate")
        if system_prompt_update is not None \
                and system_prompt_update != "in-history":
            raise LlmError(
                f"adapter returned invalid system prompt update mode for "
                f'provider "{provider}" model "{model}"',
                "INVALID_MODEL_INFO")
        # Widened for the same reason: catalog config supplies the tool
        # update mode as text.
        tool_update = resolved.get("toolUpdate")
        if tool_update is not None and tool_update not in (
                "in-history", "addition-only"):
            raise LlmError(
                f"adapter returned invalid tool update mode for provider "
                f'"{provider}" model "{model}"', "INVALID_MODEL_INFO")
        default_max_tokens = resolved.get("defaultMaxTokens")
        if default_max_tokens is not None and (
                not _is_safe_integer(default_max_tokens)
                or default_max_tokens <= 0):
            raise LlmError(
                f"adapter returned invalid default maxTokens for provider "
                f'"{provider}" model "{model}"', "INVALID_MODEL_MAX_TOKENS")
        info: Dict[str, Any] = {"provider": provider, "id": model,
                                "name": resolved["name"]}
        if resolved.get("description") is not None:
            info["description"] = resolved["description"]
        if input_modalities is not None:
            info["inputModalities"] = input_modalities
        if context is not None:
            info["context"] = {"contextWindow": context["contextWindow"]}
        if default_max_tokens is not None:
            info["defaultMaxTokens"] = default_max_tokens
        if system_prompt_update is not None:
            info["systemPromptUpdate"] = system_prompt_update
        if tool_update is not None:
            info["toolUpdate"] = tool_update
        reasoning = resolved.get("reasoning")
        if reasoning is None:
            return info
        if len(reasoning["efforts"]) == 0:
            raise LlmError(
                f"adapter returned invalid reasoning metadata for provider "
                f'"{provider}" model "{model}"', "INVALID_MODEL_REASONING")
        seen = set()
        efforts: List[Dict[str, Any]] = []
        for effort in reasoning["efforts"]:
            effort_id = effort.get("id")
            if not isinstance(effort_id, str) or effort_id == "" \
                    or not isinstance(effort.get("name"), str) \
                    or effort["name"] == "" \
                    or (effort.get("description") is not None
                        and not isinstance(effort["description"], str)) \
                    or effort_id in seen:
                raise LlmError(
                    "adapter returned invalid or duplicate reasoning effort "
                    f'metadata for provider "{provider}" model "{model}"',
                    "INVALID_MODEL_REASONING")
            seen.add(effort_id)
            entry: Dict[str, Any] = {"id": effort_id, "name": effort["name"]}
            if effort.get("description") is not None:
                entry["description"] = effort["description"]
            efforts.append(entry)
        default_effort = reasoning.get("defaultEffort")
        if default_effort is not None and default_effort not in seen:
            raise LlmError(
                "adapter returned an unknown default reasoning effort for "
                f'provider "{provider}" model "{model}"',
                "INVALID_MODEL_REASONING")
        reasoning_info: Dict[str, Any] = {"efforts": efforts}
        if default_effort is not None:
            reasoning_info["defaultEffort"] = default_effort
        return {**info, "reasoning": reasoning_info}

    def resolve_call_config(self, config: Dict[str, Any],
                            signal: Optional[Any] = None) -> Dict[str, Any]:
        """Validate a conversation call config against its exact model
        capability and materialize adapter-configured defaults. Unsupported
        explicit efforts reject before provider I/O; no clamping or aliasing
        is performed."""
        return self._resolve_call_for(self.registration(config["provider"]),
                                      config, signal)["config"]

    def _resolve_call_for(self, registration: Dict[str, Any],
                          config: Dict[str, Any],
                          signal: Optional[Any]) -> Dict[str, Any]:
        info = self._resolve_model_info_for(registration, config["model"],
                                            signal)
        return self._resolve_call_with_info(config, info)

    def _resolve_call_with_info(self, config: Dict[str, Any],
                                info: Dict[str, Any]) -> Dict[str, Any]:
        """Validate request controls against one already-bound exact model
        result."""
        defaulted = config
        if config.get("maxTokens") is None \
                and info.get("defaultMaxTokens") is not None:
            defaulted = {**config, "maxTokens": info["defaultMaxTokens"]}
        reasoning = info.get("reasoning")
        requested = defaulted.get("reasoningEffort")
        resolved_config = defaulted
        if reasoning is None:
            if requested is not None:
                raise LlmError(
                    f'provider "{config["provider"]}" model '
                    f'"{config["model"]}" does not support reasoning effort '
                    f'"{requested}"', "UNSUPPORTED_REASONING_EFFORT")
        else:
            effective = requested if requested is not None \
                else reasoning.get("defaultEffort")
            if effective is not None:
                if not any(effort["id"] == effective
                           for effort in reasoning["efforts"]):
                    raise LlmError(
                        f'provider "{config["provider"]}" model '
                        f'"{config["model"]}" does not support reasoning '
                        f'effort "{effective}"',
                        "UNSUPPORTED_REASONING_EFFORT")
                if requested != effective:
                    resolved_config = {**defaulted,
                                       "reasoningEffort": effective}
        result: Dict[str, Any] = {"config": resolved_config}
        if info.get("context") is not None:
            result["context"] = info["context"]
        result["modelInfo"] = info
        return result

    def prepare_call(self, config: Dict[str, Any],
                     signal: Optional[Any] = None) -> Any:
        """Resolve one call under its current adapter registration. The
        returned one-shot handle keeps that registration across header
        logging and dispatch, so HMR cannot combine one adapter's capability
        result with another adapter."""
        registration = self.registration(config["provider"])
        adapter_call = registration["adapter"].prepare_call(
            config["provider"], config["model"], signal)
        model_info = self._normalize_model_info(
            registration, config["model"], adapter_call["model"])
        resolved = self._resolve_call_with_info(config, model_info)
        resolved_config = deep_freeze(_detach(resolved["config"]))
        context = None if resolved.get("context") is None \
            else deep_freeze(_detach(resolved["context"]))
        adapter_defaults_source: Dict[str, Any] = {}
        if config.get("reasoningEffort") is None \
                and resolved_config.get("reasoningEffort") is not None:
            adapter_defaults_source["reasoningEffort"] = True
        if config.get("maxTokens") is None \
                and resolved_config.get("maxTokens") is not None:
            adapter_defaults_source["maxTokens"] = True
        adapter_defaults = deep_freeze(adapter_defaults_source)
        dispatched = [False]

        def stream(options: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
            if dispatched[0]:
                raise LlmError(
                    "a prepared LLM call can only be dispatched once",
                    "INVALID_PREPARED_CALL")
            if not call_config_equals(options, resolved_config):
                raise LlmError(
                    "prepared LLM call config changed before adapter "
                    "dispatch", "INVALID_PREPARED_CALL")
            dispatched[0] = True
            return self._stream_with_registration(options, {
                "registration": registration,
                "config": resolved_config,
                "modelInfo": model_info,
                "dispatch": lambda opts: adapter_call["stream"](opts),
            })

        prepared: Dict[str, Any] = {
            "config": resolved_config,
            "retryPolicy": registration["retryPolicy"],
            "adapterDefaults": adapter_defaults,
            "stream": stream,
        }
        if context is not None:
            prepared["context"] = context
        if model_info.get("inputModalities") is not None:
            prepared["inputModalities"] = tuple(model_info["inputModalities"])
        if model_info.get("systemPromptUpdate") is not None:
            prepared["systemPromptUpdate"] = model_info["systemPromptUpdate"]
        if model_info.get("toolUpdate") is not None:
            prepared["toolUpdate"] = model_info["toolUpdate"]
        return MappingProxyType(prepared)

    def registration(self, provider: str) -> Dict[str, Any]:
        registration = self.adapters.get(provider)
        if registration is None:
            raise LlmError(f'no adapter registered for provider "{provider}"',
                           "NO_ADAPTER")
        return registration

    def for_adapter(self, options: Dict[str, Any],
                    adapter: LlmAdapter) -> Dict[str, Any]:
        """Remove replay state whose historical route is owned by another
        adapter."""
        messages = []
        for message in options["messages"]:
            if message["role"] != "assistant":
                messages.append(message)
                continue
            source = message["source"]
            if source.get("replayState") is None:
                messages.append(message)
                continue
            owner = self.adapters.get(source["provider"])
            if owner is not None and owner["adapter"] is adapter:
                messages.append(message)
                continue
            messages.append(freeze_message({
                **message,
                "source": {"kind": "model", "provider": source["provider"],
                           "model": source["model"]},
            }))
        if all(messages[index] is options["messages"][index]
               for index in range(len(messages))):
            return options
        filtered = {**options, "messages": messages}
        return deep_freeze(filtered) \
            if isinstance(options, MappingProxyType) else filtered

    def file_read_path(self, ref: Any) -> Optional[str]:
        """Resolve the current execution-world read path of one durable file
        reference through the mounted attachment and filesystem providers."""
        try:
            attachments = self._service("attachments")
            host_path = attachments.fileHostPath(ref) \
                if attachments is not None else None
        except BaseException:
            # A malformed durable reference degrades this occurrence to the
            # no-path handle instead of failing every later request over the
            # same log.
            return None
        if host_path is None:
            return None
        # Structural face: dsh-llm cannot depend on the filesystem package,
        # and only this one mapping method is consumed.
        fs = self._service("fs")
        return fs.processPathFromHostPath(host_path) if fs is not None \
            else None

    def adapter_stream(self, options: Dict[str, Any],
                       prepared: Optional[Dict[str, Any]] = None
                       ) -> Iterator[Dict[str, Any]]:
        """Final adapter boundary. Adapter selection, dispatch, iterator
        construction, and iteration failures become one terminal failure
        chunk. Middleware and downstream consumer failures remain thrown
        plugin or consumer errors."""
        try:
            registration = prepared["registration"] if prepared is not None \
                else self.registration(options["provider"])
            adapter = registration["adapter"]
            if prepared is None:
                adapter_call = adapter.prepare_call(
                    options["provider"], options["model"],
                    options.get("signal"))
                model_info = self._normalize_model_info(
                    registration, options["model"], adapter_call["model"])
                resolved_config = self._resolve_call_with_info(
                    options, model_info)["config"]
                dispatch: Callable[[Any], Any] = \
                    lambda opts: adapter_call["stream"](opts)
            else:
                model_info = prepared["modelInfo"]
                resolved_config = prepared["config"]
                dispatch = prepared["dispatch"]
            if prepared is not None \
                    and not call_config_equals(options, resolved_config):
                raise LlmError(
                    "prepared LLM call config changed before adapter "
                    "dispatch", "INVALID_PREPARED_CALL")
            if call_config_equals(options, resolved_config):
                resolved_options = options
            else:
                merged = {**options, **resolved_config}
                resolved_options = deep_freeze(merged) \
                    if isinstance(options, MappingProxyType) else merged
            # Files are never dispatched natively: every route receives
            # handle text.
            projected_messages = resolved_options["messages"]
            if any(content_has_file(message["content"])
                   for message in projected_messages):
                projected_messages = project_files_to_text(
                    projected_messages,
                    lambda ref: self.file_read_path(ref))
            modalities = model_info.get("inputModalities")
            if modalities is not None and "image" not in modalities \
                    and any(content_has_image(message["content"])
                            for message in projected_messages):
                projected_messages = project_images_for_text_model(
                    projected_messages)
            # Tool changes are logged on every route; the route's declared
            # mode selects what it receives.
            projected_tools = project_tool_updates(
                projected_messages, resolved_options.get("tools"),
                model_info.get("toolUpdate"),
                resolved_options.get("toolHistory"))
            projected_messages = projected_tools["messages"]
            projected_options = resolved_options
            if projected_messages is not resolved_options["messages"] \
                    or projected_tools["tools"] \
                    is not resolved_options.get("tools"):
                projected_options = {**resolved_options,
                                     "messages": projected_messages}
                if projected_tools["tools"] is not None:
                    projected_options["tools"] = projected_tools["tools"]
                if isinstance(resolved_options, MappingProxyType):
                    projected_options = deep_freeze(projected_options)
            stream = dispatch(self.for_adapter(projected_options, adapter))
            iterator = iter(stream)
        except BaseException as error:
            yield adapter_failure_chunk(error, options.get("signal"))
            return
        completed = False
        try:
            while True:
                try:
                    item = next(iterator)
                except StopIteration:
                    completed = True
                    return
                except BaseException as error:
                    completed = True
                    yield adapter_failure_chunk(error, options.get("signal"))
                    return
                # End the adapter-owned try before yielding: consumer/
                # middleware failures resumed into this generator must
                # remain thrown.
                yield item
        finally:
            if not completed:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()

    def stream(self, options: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        """Stream one model call as raw chunks (token-level deltas),
        possibly wrapped by ``llm/stream`` listeners."""
        return self._stream_with_registration(options)

    def _stream_with_registration(
            self, options: Dict[str, Any],
            prepared: Optional[Dict[str, Any]] = None
    ) -> Iterator[Dict[str, Any]]:
        # The native waterfall arm: fold interceptors in registration order
        # (prepend first) around the adapter boundary.
        interceptors = list(self._stream_interceptors)

        def build(index: int) -> Callable[[], Iterator[Dict[str, Any]]]:
            if index >= len(interceptors):
                return lambda: self.adapter_stream(options, prepared)
            handler = interceptors[index]
            return lambda: handler(options, build(index + 1))

        return build(0)()


def _is_thenable(value: Any) -> bool:
    return hasattr(value, "then") and callable(getattr(value, "then"))


def _contain_awaitable(runtime: LlmRuntime, returned: Any) -> None:
    """Close an awaitable listener return and log its rejection (no event
    loop is implied; credentials-seam precedent)."""
    import inspect
    if inspect.isawaitable(returned):
        close = getattr(returned, "close", None)
        if callable(close):
            try:
                close()
            except BaseException:
                pass
        runtime._warn_adapters_listener_failure(
            RuntimeError("llm: an llm/adapters-updated listener returned an "
                         "awaitable; native listeners are synchronous"))


def adapter_failure_chunk(error: Any,
                          signal: Optional[Any]) -> Dict[str, Any]:
    """Convert one adapter throw into the stream protocol's terminal
    outcome."""
    failure = normalize_llm_failure(error)
    aborted = bool(getattr(signal, "aborted", False)) \
        or failure["code"] == "ABORTED"
    return {
        "type": "finish",
        "reason": {"kind": "aborted", "failure": failure} if aborted
        else {"kind": "error", "failure": failure},
    }
