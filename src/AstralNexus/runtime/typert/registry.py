"""Runtime registry for generated package reflection, invocations, and providers.

Port of ``@deepseek-ai/dsh-typert-registry`` (commit 5badb15). It performs no
TypeScript analysis and no schema generation. Contract kept:
- Key grammar: ``typert_key`` = ``<package>#<name>``, ``typert_package_key`` =
  ``<package>#<face>``, ``typert_endpoint`` = ``<namespace>/<method>``.
- ``register(contribution)`` validates the WHOLE batch first (package-face
  identity, schemas, invocation ids, endpoints) — any duplicate rejects
  everything with nothing committed — then commits atomically and returns the
  exact disposer that withdraws this contribution and nothing else.
- ``resolve`` distinguishes three failures: malformed key, package registered
  but contributes no such schema, package has no registered contribution.
- Schema materialization is cached per record (``create()`` runs at most once
  while the record lives); ``list``/``get`` return registration order.
- Descriptor history: ``has_seen`` remembers endpoints withdrawn this
  registry lifetime.
- Lookup stores: wire declarations are immutable for the registry lifetime;
  a configured resolver overrides only ``resolve``, never the wire identity.
- Change observers are isolated: a throwing listener is reported, never
  propagated, and never blocks other listeners.
- Invocation descriptor validation covers every documented rule (wire-field
  uniqueness, lookup vs JSON parameter restrictions, cancellation "signal",
  mode "stream", scope-must-select-its-only-lookup-parameter, context
  receiver wire collision, codec shape).

Python adaptations (documented):
- Cordis ``ctx.effect`` disposers become plain zero-argument idempotent
  disposer callables owned by the registering plugin's lifecycle.
- ``subscribe(listener)`` returns an unsubscribe callable (no ctx binding).
- ``to_json_schema`` projects via the schema object's own ``to_json_schema()``
  when present (native schemas are not Zod); absent projection fails loud
  instead of faking a document.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set

_LOG = logging.getLogger("astralnexus.typert")

_SEGMENT = re.compile(r"^[A-Za-z0-9_$.-]+$")
Report = Callable[[Dict[str, Any], BaseException], None]


class TypertError(RuntimeError):
    """Registry contract failure; the message names the concrete defect."""


def typert_key(package_name: str, name: str) -> str:
    return f"{package_name}#{name}"


def typert_package_key(package_name: str, face: str) -> str:
    return f"{package_name}#{face}"


def typert_endpoint(descriptor: Dict[str, Any]) -> str:
    return f"{descriptor['namespace']}/{descriptor['method']}"


def validate_segment(subject: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) == 0 or "#" in value:
        raise TypertError(f'typert: invalid {subject} "{value}" — must be nonempty and must not contain "#"')
    return value


def validate_wire_name(subject: str, value: Any) -> str:
    if (not isinstance(value, str) or value in (".", "..") or not _SEGMENT.match(value)):
        raise TypertError(
            f'typert: invalid {subject} "{value}" — must contain only RPC endpoint segment characters')
    return value


def validate_nonempty(subject: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) == 0:
        raise TypertError(f"typert: invalid {subject} — must be nonempty")


def _default_report(change: Dict[str, Any], error: BaseException) -> None:
    _LOG.warning('typert: %s observer for "%s" failed', change.get("kind"), change.get("key"))
    _LOG.warning("typert observer error: %r", error)


class ChangeSource:
    """Listener set with isolated, reported failures."""

    def __init__(self, report: Report):
        self._report = report
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass  # idempotent

        return unsubscribe

    def emit(self, change: Dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception as error:  # observers never block the registry
                self._report(change, error)


class DescriptorStore:
    """Endpoint/id-keyed invocation descriptors with owner-scoped withdrawal."""

    def __init__(self, kind: str, report: Report):
        self.kind = kind
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._ids: Dict[str, Dict[str, Any]] = {}
        self._history: Set[str] = set()
        self._changes = ChangeSource(report)

    def validate(self, descriptors: List[Dict[str, Any]]) -> None:
        endpoints: Set[str] = set()
        ids: Set[str] = set()
        for descriptor in descriptors:
            validate_invocation(descriptor)
            endpoint = typert_endpoint(descriptor)
            if endpoint in endpoints or endpoint in self._entries:
                raise TypertError(f'typert: {self.kind} endpoint "{endpoint}" is already registered')
            if descriptor["id"] in ids or descriptor["id"] in self._ids:
                raise TypertError(
                    f'typert: {self.kind} invocation id "{descriptor["id"]}" is already registered')
            endpoints.add(endpoint)
            ids.add(descriptor["id"])

    def commit(self, owner: Any, descriptors: List[Dict[str, Any]]) -> None:
        for descriptor in descriptors:
            entry = {"descriptor": descriptor, "owner": owner}
            self._entries[typert_endpoint(descriptor)] = entry
            self._ids[descriptor["id"]] = entry
            self._history.add(typert_endpoint(descriptor))
        for descriptor in descriptors:
            self._changes.emit({"kind": self.kind, "key": typert_endpoint(descriptor)})

    def withdraw(self, owner: Any, descriptors: List[Dict[str, Any]]) -> None:
        removed: List[str] = []
        for descriptor in descriptors:
            endpoint = typert_endpoint(descriptor)
            entry = self._entries.get(endpoint)
            if entry is None or entry["owner"] is not owner:
                continue
            del self._entries[endpoint]
            if self._ids.get(descriptor["id"]) is entry:
                del self._ids[descriptor["id"]]
            removed.append(endpoint)
        for endpoint in removed:
            self._changes.emit({"kind": self.kind, "key": endpoint})

    def get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        entry = self._entries.get(endpoint)
        return None if entry is None else entry["descriptor"]

    def has_seen(self, endpoint: str) -> bool:
        return endpoint in self._history

    def list(self) -> List[Dict[str, Any]]:
        return [entry["descriptor"] for entry in self._entries.values()]

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        return self._changes.subscribe(listener)


class RemoteStore:
    """Consumer-selected Remote contributions, one owner per package."""

    def __init__(self, descriptors: DescriptorStore):
        self._descriptors = descriptors
        self._packages: Dict[str, Any] = {}

    def register(self, contribution: Dict[str, Any]) -> Callable[[], None]:
        package = validate_segment("Remote package name", contribution.get("package"))
        if package in self._packages:
            raise TypertError(f'typert: Remote package "{package}" is already registered')
        descriptors = list(contribution.get("descriptors") or [])
        self._descriptors.validate(descriptors)
        owner = object()
        self._packages[package] = owner
        self._descriptors.commit(owner, descriptors)
        disposed: List[bool] = []

        def dispose() -> None:
            if disposed:
                return
            disposed.append(True)
            if self._packages.get(package) is owner:
                del self._packages[package]
            self._descriptors.withdraw(owner, descriptors)

        return dispose

    def get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        return self._descriptors.get(endpoint)

    def packages(self) -> List[str]:
        """Registered Remote package names (registration order)."""
        return list(self._packages.keys())

    def list(self) -> List[Dict[str, Any]]:
        return self._descriptors.list()

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        return self._descriptors.subscribe(listener)


def _lookup_definition_equals(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return all(left[field] == right[field]
               for field in ("parameter", "wire", "hostTypeSymbol", "wireTypeSymbol"))


class LookupStore:
    """Dependency-inverted host object lookup providers and resolvers."""

    def __init__(self, report: Report):
        self._providers: Dict[str, Dict[str, Any]] = {}
        self._resolvers: Dict[str, Dict[str, Any]] = {}
        self._definitions: Dict[str, Dict[str, Any]] = {}
        self._changes = ChangeSource(report)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._providers.get(key)
        if entry is None:
            return None
        provider = entry["provider"]
        resolver_entry = self._resolvers.get(key)
        if resolver_entry is None:
            return provider
        merged = {field: provider[field]
                  for field in ("parameter", "wire", "hostTypeSymbol", "wireTypeSymbol")}
        merged["resolve"] = resolver_entry["provider"]["resolve"]
        return merged

    def configure(self, key: str, resolver: Callable[[str], Any]) -> Callable[[], None]:
        validate_segment("lookup key", key)
        if key in self._resolvers:
            raise TypertError(f'typert: lookup "{key}" resolver is already configured')
        entry = {"provider": {"resolve": resolver}, "owner": object()}
        self._resolvers[key] = entry
        self._changes.emit({"kind": "lookup", "key": key})
        disposed: List[bool] = []

        def dispose() -> None:
            if disposed:
                return
            disposed.append(True)
            if self._resolvers.get(key) is entry:
                del self._resolvers[key]
                self._changes.emit({"kind": "lookup", "key": key})

        return dispose

    def register(self, key: str, provider: Dict[str, Any]) -> Callable[[], None]:
        validate_segment("lookup key", key)
        validate_segment("lookup parameter", provider.get("parameter"))
        validate_wire_name("lookup wire field", provider.get("wire"))
        validate_nonempty("lookup Host type symbol", provider.get("hostTypeSymbol"))
        validate_nonempty("lookup wire type symbol", provider.get("wireTypeSymbol"))
        if key in self._providers:
            raise TypertError(f'typert: lookup "{key}" is already registered')
        definition = {field: provider[field]
                      for field in ("parameter", "wire", "hostTypeSymbol", "wireTypeSymbol")}
        definition["key"] = key
        known = self._definitions.get(key)
        if known is not None and not _lookup_definition_equals(known, definition):
            raise TypertError(
                f'typert: lookup "{key}" changed its wire declaration during this registry lifetime')
        entry = {"provider": provider, "owner": object()}
        self._definitions[key] = definition
        self._providers[key] = entry
        self._changes.emit({"kind": "lookup", "key": key})
        disposed: List[bool] = []

        def dispose() -> None:
            if disposed:
                return
            disposed.append(True)
            if self._providers.get(key) is entry:
                del self._providers[key]
                self._changes.emit({"kind": "lookup", "key": key})

        return dispose

    def definitions(self) -> List[Dict[str, Any]]:
        return list(self._definitions.values())

    def keys(self) -> List[str]:
        return list(self._providers.keys())

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        return self._changes.subscribe(listener)


class ContextStore:
    """Host and Client Context adapters with optional host resolvers."""

    def __init__(self, report: Report):
        self._hosts: Dict[str, Dict[str, Any]] = {}
        self._host_resolvers: Dict[str, Dict[str, Any]] = {}
        self._clients: Dict[str, Dict[str, Any]] = {}
        self._changes = ChangeSource(report)

    def get_host(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._hosts.get(key)
        if entry is None:
            return None
        adapter = entry["provider"]
        resolver_entry = self._host_resolvers.get(key)
        if resolver_entry is None:
            return adapter
        return {"wire": adapter["wire"], "wireTypeSymbol": adapter["wireTypeSymbol"],
                "resolve": resolver_entry["provider"]["resolve"]}

    def get_client(self, key: str) -> Optional[Any]:
        entry = self._clients.get(key)
        return None if entry is None else entry["provider"]

    def configure_host(self, key: str, resolver: Callable[[str], Any]) -> Callable[[], None]:
        validate_segment("Context key", key)
        if key in self._host_resolvers:
            raise TypertError(f'typert: host-context "{key}" resolver is already configured')
        entry = {"provider": {"resolve": resolver}, "owner": object()}
        self._host_resolvers[key] = entry
        self._changes.emit({"kind": "host-context", "key": key})
        disposed: List[bool] = []

        def dispose() -> None:
            if disposed:
                return
            disposed.append(True)
            if self._host_resolvers.get(key) is entry:
                del self._host_resolvers[key]
                self._changes.emit({"kind": "host-context", "key": key})

        return dispose

    def register_host(self, key: str, adapter: Dict[str, Any]) -> Callable[[], None]:
        validate_segment("Context key", key)
        validate_wire_name("Context wire field", adapter.get("wire"))
        validate_nonempty("Context wire type symbol", adapter.get("wireTypeSymbol"))
        return self._register_provider(self._hosts, "host-context", key, adapter)

    def register_client(self, key: str, adapter: Dict[str, Any]) -> Callable[[], None]:
        validate_segment("Context key", key)
        return self._register_provider(self._clients, "client-context", key, adapter)

    def _register_provider(self, table: Dict[str, Dict[str, Any]], kind: str,
                           key: str, provider: Any) -> Callable[[], None]:
        if key in table:
            raise TypertError(f'typert: {kind} provider "{key}" is already registered')
        entry = {"provider": provider, "owner": object()}
        table[key] = entry
        self._changes.emit({"kind": kind, "key": key})
        disposed: List[bool] = []

        def dispose() -> None:
            if disposed:
                return
            disposed.append(True)
            if table.get(key) is entry:
                del table[key]
                self._changes.emit({"kind": kind, "key": key})

        return dispose

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        return self._changes.subscribe(listener)


class TypertRegistry:
    """Registry of generated schemas, package reflection, invocations, providers."""

    def __init__(self, report: Optional[Report] = None):
        self._report: Report = report or _default_report
        self.schemas: Dict[str, Dict[str, Any]] = {}
        self.packages: Dict[str, Dict[str, Any]] = {}
        self.local_store = DescriptorStore("local", self._report)
        self.remote_store = RemoteStore(DescriptorStore("remote", self._report))
        self.lookup_store = LookupStore(self._report)
        self.context_store = ContextStore(self._report)

    # ------------------------------------------------------------- facades

    @property
    def local(self) -> Dict[str, Any]:
        return {"get": self.local_store.get, "hasSeen": self.local_store.has_seen,
                "list": self.local_store.list, "subscribe": self.local_store.subscribe}

    @property
    def remotes(self) -> RemoteStore:
        return self.remote_store

    @property
    def lookups(self) -> LookupStore:
        return self.lookup_store

    @property
    def contexts(self) -> ContextStore:
        return self.context_store

    # ---------------------------------------------------------- registration

    def register(self, contribution: Dict[str, Any]) -> Callable[[], None]:
        """Register one generated contribution atomically; returns its disposer."""
        package_record = self._validate_package(contribution)
        schema_records = self._validate_schemas(contribution)
        invocations = list(contribution.get("invocations") or [])
        self.local_store.validate(invocations)
        owner = object()
        self.packages[package_record["key"]] = package_record
        for record in schema_records:
            self.schemas[record["key"]] = record
        self.local_store.commit(owner, invocations)
        disposed: List[bool] = []

        def dispose() -> None:
            if disposed:
                return
            disposed.append(True)
            if self.packages.get(package_record["key"]) is package_record:
                del self.packages[package_record["key"]]
            for record in schema_records:
                if self.schemas.get(record["key"]) is record:
                    del self.schemas[record["key"]]
            self.local_store.withdraw(owner, invocations)

        return dispose

    def _validate_package(self, contribution: Dict[str, Any]) -> Dict[str, Any]:
        validate_segment("package name", contribution.get("package"))
        face = contribution.get("face")
        if face not in ("host", "client"):
            raise TypertError(
                f'typert: invalid face {json.dumps(face)} — expected "host" or "client"')
        key = typert_package_key(contribution["package"], face)
        if key in self.packages:
            raise TypertError(f'typert: package face "{key}" is already registered')
        return {"package": contribution["package"], "face": face, "key": key,
                "model": contribution.get("model")}

    def _validate_schemas(self, contribution: Dict[str, Any]) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        batch: Set[str] = set()
        for schema in contribution.get("schemas") or []:
            validate_segment("schema name", schema.get("name"))
            if not callable(schema.get("create")):
                raise TypertError(f'typert: schema "{schema["name"]}" has no create() factory')
            key = typert_key(contribution["package"], schema["name"])
            if key in batch or key in self.schemas:
                raise TypertError(f'typert: schema "{key}" is already registered')
            batch.add(key)
            record = dict(schema)
            record.update({"package": contribution["package"], "face": contribution["face"],
                           "key": key})
            records.append(record)
        return records

    # -------------------------------------------------------------- queries

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        record = self.schemas.get(key)
        return None if record is None else _materialize_schema(record)

    def resolve(self, key: str) -> Dict[str, Any]:
        record = self.schemas.get(key)
        if record is not None:
            return _materialize_schema(record)
        if not isinstance(key, str):
            raise TypertError(f'typert: invalid schema key "{key}" — expected "<package>#<name>"')
        hash_at = key.find("#")
        if hash_at <= 0 or hash_at == len(key) - 1:
            raise TypertError(f'typert: invalid schema key "{key}" — expected "<package>#<name>"')
        package_name = key[:hash_at]
        schema_name = key[hash_at + 1:]
        if any(candidate["package"] == package_name for candidate in self.packages.values()):
            raise TypertError(
                f'typert: cannot resolve "{key}" — package "{package_name}" is registered '
                f'but contributes no schema named "{schema_name}"')
        raise TypertError(
            f'typert: cannot resolve "{key}" — package "{package_name}" has no registered contribution')

    def list(self, filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        filter = filter or {}
        return [_materialize_schema(record) for record in self.schemas.values()
                if _matches(record, filter)]

    def get_package(self, package_name: str, face: str = "host") -> Optional[Dict[str, Any]]:
        return self.packages.get(typert_package_key(package_name, face))

    def list_packages(self, filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        filter = filter or {}
        return [record for record in self.packages.values() if _matches(record, filter)]

    def to_json_schema(self, key: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Project a live schema to JSON Schema without caching the result."""
        schema = self.resolve(key)["schema"]
        projection = getattr(schema, "to_json_schema", None)
        if not callable(projection):
            raise TypertError(
                f'typert: schema "{key}" has no JSON Schema projection (to_json_schema)')
        return projection() if params is None else projection(params)


def _materialize_schema(record: Dict[str, Any]) -> Dict[str, Any]:
    if record.get("value") is None:
        record["value"] = record["create"]()
    return {"name": record["name"], "schema": record["value"],
            "package": record["package"], "face": record["face"], "key": record["key"]}


def _matches(record: Dict[str, Any], filter: Dict[str, Any]) -> bool:
    return ((filter.get("package") is None or record["package"] == filter["package"])
            and (filter.get("face") is None or record["face"] == filter["face"]))


# ------------------------------------------------- invocation descriptor rules


def validate_invocation(descriptor: Dict[str, Any]) -> None:
    """Full validation of one generated invocation descriptor (all upstream rules)."""
    if not isinstance(descriptor, dict):
        raise TypertError("typert: invocation descriptor must be an object")
    validate_nonempty("invocation id", descriptor.get("id"))
    ident = descriptor["id"]
    validate_segment("invocation service key", descriptor.get("service"))
    validate_wire_name("invocation namespace", descriptor.get("namespace"))
    validate_wire_name("invocation method", descriptor.get("method"))
    if descriptor.get("implementation") is not None:
        validate_wire_name("invocation implementation method", descriptor["implementation"])
    invocation = descriptor.get("invocation")
    if not isinstance(invocation, dict) or invocation.get("kind") not in ("direct", "context"):
        raise TypertError(f'typert: invocation "{ident}" receiver kind must be "direct" or "context"')
    validate_codec(descriptor.get("result"), f"{ident} result")
    wires: Set[str] = set()
    parameters = descriptor.get("parameters")
    if not isinstance(parameters, list):
        raise TypertError(f'typert: invocation "{ident}" parameters must be an array')
    for parameter in parameters:
        validate_wire_name("parameter name", parameter.get("name"))
        validate_wire_name("parameter wire field", parameter.get("wire"))
        wire = parameter["wire"]
        if wire in wires:
            raise TypertError(f'typert: invocation "{ident}" repeats wire field "{wire}"')
        wires.add(wire)
        if parameter.get("source") == "lookup":
            if parameter.get("acceptsUndefined") is not None:
                raise TypertError(
                    f'typert: invocation "{ident}" lookup parameter "{parameter["name"]}" cannot accept undefined')
            if parameter.get("lookup") is None:
                raise TypertError(
                    f'typert: invocation "{ident}" lookup parameter "{parameter["name"]}" has no lookup key')
            validate_segment("lookup key", parameter["lookup"])
        elif parameter.get("lookup") is not None:
            raise TypertError(
                f'typert: invocation "{ident}" JSON parameter "{parameter["name"]}" declares a lookup key')
        validate_codec(parameter.get("codec"), f"{ident} parameter {parameter.get('name')}")
    cancellation = descriptor.get("cancellation")
    if cancellation is not None and cancellation.get("parameter") != "signal":
        raise TypertError(f'typert: invocation "{ident}" cancellation parameter must be "signal"')
    mode = descriptor.get("mode")
    if mode is not None and mode != "stream":
        raise TypertError(f'typert: invocation "{ident}" mode must be "stream"')
    if descriptor.get("uplink") is not None:
        validate_codec(descriptor["uplink"].get("codec"), f"{ident} uplink")
    scope = descriptor.get("scope")
    if scope is not None:
        if invocation.get("kind") != "direct":
            raise TypertError(
                f'typert: invocation "{ident}" Context receiver cannot declare a direct scope projection')
        validate_segment("scope Context key", scope.get("context"))
        validate_wire_name("scope wire field", scope.get("wire"))
        lookups = [candidate for candidate in parameters if candidate.get("source") == "lookup"]
        parameter = lookups[0] if len(lookups) == 1 else None
        if (parameter is None or parameter.get("wire") != scope.get("wire")
                or parameter.get("lookup") != scope.get("context")):
            raise TypertError(
                f'typert: invocation "{ident}" scope wire "{scope.get("wire")}" must select its only lookup parameter')
    if invocation.get("kind") == "context":
        validate_segment("Context key", invocation.get("context"))
        validate_wire_name("Context wire field", invocation.get("wire"))
        if invocation["wire"] in wires:
            raise TypertError(f'typert: invocation "{ident}" repeats wire field "{invocation["wire"]}"')
        validate_codec(invocation.get("codec"), f"{ident} Context")


def validate_codec(codec: Any, subject: str) -> None:
    if not isinstance(codec, dict):
        raise TypertError(f"typert: {subject} codec must be an object")
    if codec.get("mode") == "src-json":
        return
    validate_nonempty(f"{subject} type symbol", codec.get("typeSymbol"))
    if not callable(codec.get("create")):
        raise TypertError(f"typert: {subject} strict codec has no create() factory")
