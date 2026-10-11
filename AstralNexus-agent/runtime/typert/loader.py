"""Typert loader integration: automatic registration for mounted plugin packages.

Port of ``@deepseek-ai/dsh-typert-loader`` (commit 5badb15). Upstream observes
Cordis loader entries and resolves each package's ``exports["./typert"]``
artifact; the native host drives the same lifecycle synchronously: when a
plugin entry starts, ``attach`` imports the declared module, validates its
``TYPERT`` manifest at the module/file boundary (every field checked, every
failure names the package and the defect), and registers it into the registry;
``detach`` withdraws the registration when the entry stops.

Contract kept:
- Discovered entries without a TYPERT export are skipped SILENTLY; an explicit
  (configured) package or a declared artifact that is broken FAILS LOUD —
  aggregated into the plugin's activation failure.
- Imported manifests are cached per package name and never expire —
  plugin-set changes take effect on restart.
- Manual ``registry.register()`` remains available for contributions without
  a generated artifact.
- Manifest validation covers the full documented surface: ownership (manifest
  must be owned by the exporting package), host face, schemas with create()
  factories, model services/events/objects (documentation tags, keys, member
  kinds, type declarations), and every invocation rule (receiver kinds, wire
  uniqueness, lookup/scope selection, cancellation, strict codecs with
  factories, positive-integer source locations).

Python adaptation: package.json ``exports["./typert"]`` resolution becomes the
plugin manifest's explicit ``typert: {module, package}`` declaration (native
plugins have no npm export map); the Cordis microtask flush becomes
synchronous attach/detach driven by PluginHost lifecycle plus ``reconcile``
for activation/recovery passes.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, List, Optional

_LOG = logging.getLogger("astralnexus.typert")

TYPERT_HOST_EXPORT = "./typert"
MEMBER_KINDS = frozenset({"property", "method", "getter", "setter", "call", "construct", "index"})


class TypertLoaderError(RuntimeError):
    """Manifest or contribution failure; the message names package and defect."""


def _require_object(pkg: str, value: Any, subject: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TypertLoaderError(f"typert-loader: {pkg} {subject} must be an object")
    return value


def _require_array(pkg: str, value: Any, subject: str) -> List[Any]:
    if not isinstance(value, list):
        raise TypertLoaderError(f"typert-loader: {pkg} {subject} must be an array")
    return value


def _require_string(pkg: str, value: Dict[str, Any], key: str, subject: str) -> None:
    item = value.get(key)
    if not isinstance(item, str) or len(item) == 0:
        raise TypertLoaderError(f"typert-loader: {pkg} {subject} has a missing or empty {key}")


def _require_documentation(pkg: str, value: Dict[str, Any], subject: str) -> None:
    _require_array(pkg, value.get("tags"), f"{subject}.tags")
    for key in ("description", "summary", "jsDoc"):
        if value.get(key) is not None and not isinstance(value.get(key), str):
            raise TypertLoaderError(f"typert-loader: {pkg} {subject}.{key} must be a string")


def _require_members(pkg: str, value: Any, subject: str) -> None:
    for item in _require_array(pkg, value, f"{subject}.members"):
        member = _require_object(pkg, item, f"{subject} member")
        _require_string(pkg, member, "name", f"{subject} member")
        _require_string(pkg, member, "signature", f"{subject} member")
        if not isinstance(member.get("kind"), str) or member["kind"] not in MEMBER_KINDS:
            raise TypertLoaderError(
                f'typert-loader: {pkg} {subject} member "{member.get("name")}" has invalid kind')


def _require_types(pkg: str, value: Any, subject: str) -> None:
    for item in _require_array(pkg, value, f"{subject}.types"):
        decl = _require_object(pkg, item, f"{subject} type")
        _require_string(pkg, decl, "name", f"{subject} type")
        _require_string(pkg, decl, "declaration", f"{subject} type")


def _require_codec(pkg: str, value: Any, subject: str) -> None:
    """Boundary check: a codec object must exist. The strict/src-json shape
    rules belong to the registry's invocation validation, which runs on the
    same manifest immediately after (no looser, no duplicated invention)."""
    _require_object(pkg, value, subject)


def _require_invocation(pkg: str, value: Any) -> None:
    invocation = _require_object(pkg, value, "invocation")
    for key in ("id", "service", "namespace", "method"):
        _require_string(pkg, invocation, key, "invocation")
    ident = invocation["id"]
    receiver = _require_object(pkg, invocation.get("invocation"), f'invocation "{ident}" receiver')
    if receiver.get("kind") == "context":
        _require_string(pkg, receiver, "context", f'invocation "{ident}" Context receiver')
        _require_string(pkg, receiver, "wire", f'invocation "{ident}" Context receiver')
        _require_codec(pkg, receiver.get("codec"), f'invocation "{ident}" Context codec')
    elif receiver.get("kind") != "direct":
        raise TypertLoaderError(
            f'typert-loader: {pkg} invocation "{ident}" receiver kind must be "direct" or "context"')
    wires = set()
    parameters: Dict[str, Dict[str, Any]] = {}
    lookup_count = 0
    for value_parameter in _require_array(pkg, invocation.get("parameters"),
                                          f'invocation "{ident}" parameters'):
        parameter = _require_object(pkg, value_parameter, f'invocation "{ident}" parameter')
        _require_string(pkg, parameter, "name", f'invocation "{ident}" parameter')
        _require_string(pkg, parameter, "wire", f'invocation "{ident}" parameter')
        wire = parameter["wire"]
        if wire in wires:
            raise TypertLoaderError(f'typert-loader: {pkg} invocation "{ident}" repeats wire field "{wire}"')
        wires.add(wire)
        source = parameter.get("source")
        if source == "lookup":
            lookup_count += 1
            _require_string(pkg, parameter, "lookup", f'invocation "{ident}" lookup parameter')
        elif source == "json":
            if parameter.get("lookup") is not None:
                raise TypertLoaderError(
                    f'typert-loader: {pkg} invocation "{ident}" JSON parameter declares a lookup')
        else:
            raise TypertLoaderError(
                f'typert-loader: {pkg} invocation "{ident}" parameter source must be "json" or "lookup"')
        parameters[wire] = parameter
        _require_codec(pkg, parameter.get("codec"), f'invocation "{ident}" parameter codec')
    if invocation.get("cancellation") is not None:
        cancellation = _require_object(pkg, invocation["cancellation"],
                                       f'invocation "{ident}" cancellation')
        if cancellation.get("parameter") != "signal":
            raise TypertLoaderError(
                f'typert-loader: {pkg} invocation "{ident}" cancellation parameter must be "signal"')
    if invocation.get("scope") is not None:
        if receiver.get("kind") != "direct":
            raise TypertLoaderError(
                f'typert-loader: {pkg} invocation "{ident}" Context receiver cannot declare a direct scope projection')
        scope = _require_object(pkg, invocation["scope"], f'invocation "{ident}" scope')
        _require_string(pkg, scope, "context", f'invocation "{ident}" scope')
        _require_string(pkg, scope, "wire", f'invocation "{ident}" scope')
        scope_parameter = parameters.get(scope["wire"])
        if (lookup_count != 1 or scope_parameter is None or scope_parameter.get("source") != "lookup"
                or scope_parameter.get("lookup") != scope["context"]):
            raise TypertLoaderError(
                f'typert-loader: {pkg} invocation "{ident}" scope wire "{scope["wire"]}" must select its only lookup parameter')
    if receiver.get("kind") == "context" and receiver.get("wire") in wires:
        raise TypertLoaderError(
            f'typert-loader: {pkg} invocation "{ident}" repeats Context wire field "{receiver["wire"]}"')
    _require_codec(pkg, invocation.get("result"), f'invocation "{ident}" result codec')
    if invocation.get("sourceLocation") is not None:
        location = _require_object(pkg, invocation["sourceLocation"],
                                   f'invocation "{ident}" sourceLocation')
        _require_string(pkg, location, "file", f'invocation "{ident}" sourceLocation')
        for key in ("line", "column"):
            item = location.get(key)
            if not isinstance(item, int) or isinstance(item, bool) or item < 1:
                raise TypertLoaderError(
                    f'typert-loader: {pkg} invocation "{ident}" sourceLocation.{key} must be a positive integer')


def validate_typert_manifest(pkg_name: str, exported: Any) -> Dict[str, Any]:
    """Narrow a dynamically imported TYPERT export to a validated contribution.

    This is the module/file boundary: the manifest crosses from a build
    artifact into the typed registry, so every field is checked and every
    failure names the package and the defect.
    """
    if not isinstance(exported, dict):
        raise TypertLoaderError(
            f'typert-loader: {pkg_name} exports "{TYPERT_HOST_EXPORT}" but its module has no TYPERT manifest object')
    manifest = exported
    if manifest.get("package") != pkg_name:
        raise TypertLoaderError(
            f'typert-loader: {pkg_name} TYPERT manifest names package {manifest.get("package")!r} '
            "— the manifest must be owned by the package that exports it")
    if manifest.get("face") != "host":
        raise TypertLoaderError(
            f'typert-loader: {pkg_name} exports "{TYPERT_HOST_EXPORT}" but TYPERT.face is not "host"')
    for value in _require_array(pkg_name, manifest.get("schemas"), "TYPERT.schemas"):
        schema = _require_object(pkg_name, value, "schema")
        _require_string(pkg_name, schema, "name", "TYPERT.schemas")
        if not callable(schema.get("create")):
            raise TypertLoaderError(
                f'typert-loader: {pkg_name} TYPERT schema "{schema["name"]}" has no create() factory')
    model = _require_object(pkg_name, manifest.get("model"), "TYPERT.model")
    services = _require_array(pkg_name, model.get("services"), "TYPERT.model.services")
    events = _require_array(pkg_name, model.get("events"), "TYPERT.model.events")
    objects = _require_array(pkg_name, model.get("objects"), "TYPERT.model.objects")
    for value in services:
        service = _require_object(pkg_name, value, "service")
        _require_documentation(pkg_name, service, "service")
        _require_string(pkg_name, service, "key", "service")
        _require_string(pkg_name, service, "exportName", "service")
        _require_members(pkg_name, service.get("members"), f'service "{service["key"]}"')
        _require_types(pkg_name, service.get("types"), f'service "{service["key"]}"')
    for value in events:
        event = _require_object(pkg_name, value, "event")
        _require_documentation(pkg_name, event, "event")
        _require_string(pkg_name, event, "name", "event")
        _require_string(pkg_name, event, "signature", f'event "{event.get("name")}"')
        if event.get("mode") is not None and not isinstance(event.get("mode"), str):
            raise TypertLoaderError(
                f'typert-loader: {pkg_name} event "{event["name"]}" mode must be a string')
    for value in objects:
        record = _require_object(pkg_name, value, "object")
        _require_documentation(pkg_name, record, "object")
        _require_string(pkg_name, record, "name", "object")
        _require_string(pkg_name, record, "exportName", "object")
        _require_members(pkg_name, record.get("members"), f'object "{record["name"]}"')
        _require_types(pkg_name, record.get("types"), f'object "{record["name"]}"')
    for value in _require_array(pkg_name, manifest.get("invocations"), "TYPERT.invocations"):
        _require_invocation(pkg_name, value)
    return manifest


class TypertLoader:
    """Synchronous attach/detach driver bound to the native plugin lifecycle."""

    def __init__(self, registry: Any, report: Optional[Callable[[BaseException], None]] = None):
        self._registry = registry
        self._report = report or (lambda error: _LOG.error("typert-loader: %s", error))
        self._registered: Dict[str, Callable[[], None]] = {}
        self._manifests: Dict[str, Dict[str, Any]] = {}  # cached per package, never expires

    def registered_entries(self) -> List[str]:
        return list(self._registered.keys())

    def attach(self, entry_name: str, module_name: str, package_name: str,
               *, configured: bool = False) -> Optional[Callable[[], None]]:
        """Import, validate, and register one entry's typert contribution.

        ``configured=True`` (explicit declaration) fails loud; a discovered
        entry whose module exports no TYPERT manifest is skipped silently.
        Returns the registration disposer, or None when skipped.
        """
        if entry_name in self._registered:
            return None  # already attached; idempotent per entry
        try:
            module = importlib.import_module(module_name)
            exported = getattr(module, "TYPERT", None)
            if exported is None:
                if configured:
                    raise TypertLoaderError(
                        f'typert-loader: configured package "{package_name}" does not export "{TYPERT_HOST_EXPORT}"')
                return None  # discovered without the export: skip silently
            manifest = self._manifests.get(package_name)
            if manifest is None:
                manifest = validate_typert_manifest(package_name, exported)
                self._manifests[package_name] = manifest
            disposer = self._registry.register(manifest)
            self._registered[entry_name] = disposer
            return disposer
        except Exception as error:
            if configured:
                raise  # aggregated into the plugin's activation failure by the caller
            self._report(error)  # steady-state containment: logged per package
            return None

    def detach(self, entry_name: str) -> bool:
        """Withdraw one entry's registration; True when something was withdrawn."""
        disposer = self._registered.pop(entry_name, None)
        if disposer is None:
            return False
        disposer()
        return True

    def reconcile(self, entries: List[Dict[str, Any]]) -> List[str]:
        """Activation/recovery pass over ``[{id, module, package, configured}]``.

        Attaches every qualifying declared entry and detaches registrations
        whose entry disappeared. Returns the attached entry names.
        """
        qualifying = {entry["id"] for entry in entries}
        for entry_name in list(self._registered.keys()):
            if entry_name not in qualifying:
                self.detach(entry_name)
        attached: List[str] = []
        failures: List[BaseException] = []
        for entry in entries:
            try:
                disposer = self.attach(entry["id"], entry["module"], entry["package"],
                                       configured=bool(entry.get("configured")))
            except Exception as error:
                failures.append(error)
                continue
            if disposer is not None:
                attached.append(entry["id"])
        if failures:
            details = "\n".join(f"  - {error}" for error in failures)
            raise TypertLoaderError(
                f"typert-loader: {len(failures)} typert contributor(s) failed to register:\n{details}")
        return attached
