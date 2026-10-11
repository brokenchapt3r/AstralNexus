"""Contract tests for the native typert port (protocol/registry/loader) and its
real PluginHost + WorkbenchHost integration (B1 typert wave)."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from AstralNexus.runtime.typert import protocol as tp
from AstralNexus.runtime.typert.loader import TypertLoader, TypertLoaderError, validate_typert_manifest
from AstralNexus.runtime.typert.registry import (
    TypertError, TypertRegistry, typert_endpoint, typert_key, typert_package_key,
    validate_invocation)


# ------------------------------------------------------------------ protocol


class TestProtocol:
    def test_remote_error_discriminates_by_code_not_instance(self):
        error = tp.RemoteError("not_found", "missing", {"id": 7})
        assert error.code == "not_found" and error.details == {"id": 7}
        assert tp.remote_error_of(error) is error

        class ForeignCopy(Exception):  # a cross-module copy of the class
            is_dsh_remote_error = True
            code = "foreign"

        foreign = ForeignCopy("x")
        assert tp.remote_error_of(foreign) is foreign
        assert tp.remote_error_of(ValueError("plain")) is None
        assert tp.remote_error_of(None) is None

    def test_uplink_item_allows_top_level_absent_only(self):
        assert tp.is_remote_uplink_item(tp.ABSENT)
        assert tp.is_remote_uplink_item({"a": [1, "x", None, True]})
        assert not tp.is_remote_uplink_item(float("nan"))
        assert not tp.is_remote_uplink_item({"nested": tp.ABSENT})
        assert not tp.is_remote_json_value(tp.ABSENT)
        assert tp.is_remote_json_value(None)  # JSON null is lossless

    def test_owned_value_releases_once(self):
        calls = []
        owned = tp.typert_owned_value({"payload": 1}, lambda: calls.append("release"))
        assert tp.is_typert_owned_value(owned) and not tp.is_typert_owned_value({"payload": 1})
        with owned:
            pass
        owned.close()
        assert calls == ["release"]

    def test_segment_grammar(self):
        assert tp.is_typert_remote_segment("a.b-c_$d")
        for bad in (".", "..", "a/b", "a b", "", None, 42):
            assert not tp.is_typert_remote_segment(bad), bad

    def test_remote_decorator_forms(self):
        class Service:
            @tp.remote
            def bare(self):
                pass

            @tp.remote("exported")
            def renamed(self):
                pass

            @tp.remote({"mode": "stream"})
            def streamed(self):
                pass

            @tp.remote()
            def parenthesized(self):
                pass

        markers = {item["method"]: item for item in tp.remote_methods(Service)}
        assert set(markers) == {"bare", "renamed", "streamed", "parenthesized"}
        assert markers["renamed"]["exportName"] == "exported"
        assert markers["streamed"]["mode"] == "stream"
        assert "exportName" not in markers["bare"] and "mode" not in markers["bare"]
        assert markers["bare"]["invocation"] == {"kind": "direct"}

    def test_remote_rejects_private_and_bad_options(self):
        with pytest.raises(TypeError, match="public instance method"):
            @tp.remote
            def _private():
                pass

        with pytest.raises(TypeError, match='exactly mode: "stream"'):
            tp.remote({"mode": "stream", "extra": 1})
        with pytest.raises(TypeError, match="Remote argument"):
            tp.remote(42)

    def test_remote_scope_marker_and_conflict(self):
        class Scoped:
            @tp.remote_scope("scope.key", "aliasName")
            def scoped(self):
                pass

        marker = tp.remote_methods(Scoped())[0]
        assert marker["invocation"] == {"kind": "context", "context": "scope.key"}
        assert marker["exportName"] == "aliasName"
        with pytest.raises(TypeError, match="Scope key"):
            tp.remote_scope("bad/key")

    def test_remote_methods_snapshot_is_isolated_and_inherited(self):
        class Base:
            @tp.remote
            def inherited(self):
                pass

        class Child(Base):
            @tp.remote
            def own(self):
                pass

        snapshot = tp.remote_methods(Child())
        assert [item["method"] for item in snapshot] == ["inherited", "own"]
        snapshot[0]["method"] = "tampered"
        assert tp.remote_methods(Child())[0]["method"] == "inherited"

    def test_bind_typert_remote_validation_and_default_namespace(self):
        service = object()
        binding = tp.bind_typert_remote(service, "svc.key")
        assert binding["namespace"] == "svc.key" and binding["service"] is service
        binding2 = tp.bind_typert_remote(service, "svc.key", {"namespace": "custom.ns"})
        assert binding2["namespace"] == "custom.ns"
        with pytest.raises(TypeError, match="service key"):
            tp.bind_typert_remote(service, "bad/key")
        with pytest.raises(TypeError, match="namespace"):
            tp.bind_typert_remote(service, "ok", {"namespace": ".."})
        gateway = tp.TypertRemoteService("gw")
        assert gateway.name == "gw" and gateway.typert_remote["serviceKey"] == "gw"


# ------------------------------------------------------------------ registry

JSON_CODEC = {"mode": "src-json"}


def _descriptor(ident="pkg#svc.m", namespace="ns", method="m", **overrides):
    base = {"id": ident, "service": "svc", "namespace": namespace, "method": method,
            "invocation": {"kind": "direct"}, "parameters": [], "result": JSON_CODEC}
    base.update(overrides)
    return base


def _contribution(package="pkg-a", face="host", schemas=None, invocations=None, model=None):
    return {"package": package, "face": face,
            "schemas": schemas if schemas is not None else [],
            "invocations": invocations if invocations is not None else [],
            "model": model or {"services": [], "events": [], "objects": []}}


class _ProjectionSchema:
    def to_json_schema(self, params=None):
        return {"type": "object", "params": params}


class TestRegistry:
    def test_key_grammar(self):
        assert typert_key("p", "n") == "p#n"
        assert typert_package_key("p", "host") == "p#host"
        assert typert_endpoint({"namespace": "ns", "method": "m"}) == "ns/m"

    def test_register_is_atomic_and_disposer_exact(self):
        registry = TypertRegistry()
        good = {"name": "S1", "create": lambda: "schema-1"}
        disposer = registry.register(_contribution(
            schemas=[good], invocations=[_descriptor()]))
        assert registry.get("pkg-a#S1")["schema"] == "schema-1"
        assert registry.local["get"]("ns/m") is not None
        # A batch with a duplicate schema name rejects EVERYTHING.
        with pytest.raises(TypertError, match="already registered"):
            registry.register(_contribution(package="pkg-b",
                                            schemas=[{"name": "S2", "create": lambda: 1},
                                                     {"name": "S2", "create": lambda: 2}]))
        assert registry.get(typert_key("pkg-b", "S2")) is None
        assert "pkg-b#host" not in registry.packages
        disposer()
        disposer()  # idempotent
        assert registry.get("pkg-a#S1") is None
        assert registry.local["get"]("ns/m") is None
        assert registry.local["hasSeen"]("ns/m") is True  # history survives withdrawal

    def test_materialization_cached_per_record(self):
        registry = TypertRegistry()
        calls = []

        def create():
            calls.append(1)
            return "materialized"

        registry.register(_contribution(schemas=[{"name": "S", "create": create}]))
        assert registry.get("pkg-a#S")["schema"] == "materialized"
        assert registry.get("pkg-a#S")["schema"] == "materialized"
        assert registry.list()[0]["schema"] == "materialized"
        assert len(calls) == 1
        # A different package face materializes its own record.
        disposer = registry.register(_contribution(package="pkg-b",
                                                   schemas=[{"name": "S", "create": create}]))
        registry.get("pkg-b#S")
        assert len(calls) == 2
        disposer()

    def test_resolve_distinguishes_three_failures(self):
        registry = TypertRegistry()
        registry.register(_contribution(schemas=[{"name": "S", "create": lambda: 1}]))
        assert registry.resolve("pkg-a#S")["name"] == "S"
        with pytest.raises(TypertError, match="contributes no schema named"):
            registry.resolve("pkg-a#Missing")
        with pytest.raises(TypertError, match="has no registered contribution"):
            registry.resolve("unknown-pkg#S")
        with pytest.raises(TypertError, match="expected \"<package>#<name>\""):
            registry.resolve("malformed-key")

    def test_list_and_package_views(self):
        registry = TypertRegistry()
        registry.register(_contribution(schemas=[{"name": "A", "create": lambda: 1}]))
        registry.register(_contribution(package="pkg-a", face="client",
                                        schemas=[{"name": "B", "create": lambda: 2}]))
        registry.register(_contribution(package="pkg-b",
                                        schemas=[{"name": "C", "create": lambda: 3}]))
        assert [item["key"] for item in registry.list()] == ["pkg-a#A", "pkg-a#B", "pkg-b#C"]
        assert [item["key"] for item in registry.list({"package": "pkg-b"})] == ["pkg-b#C"]
        assert [item["key"] for item in registry.list({"face": "client"})] == ["pkg-a#B"]
        assert registry.get_package("pkg-a")["face"] == "host"  # default face
        assert registry.get_package("pkg-a", "client")["key"] == "pkg-a#client"
        assert registry.get_package("pkg-a", "nope") is None
        assert {item["key"] for item in registry.list_packages()} == {
            "pkg-a#host", "pkg-a#client", "pkg-b#host"}
        with pytest.raises(TypertError, match='invalid face'):
            registry.register(_contribution(package="pkg-c", face="gateway"))

    def test_to_json_schema_projection_and_refusal(self):
        registry = TypertRegistry()
        registry.register(_contribution(
            schemas=[{"name": "P", "create": _ProjectionSchema},
                     {"name": "Raw", "create": lambda: object()}]))
        assert registry.to_json_schema("pkg-a#P") == {"type": "object", "params": None}
        assert registry.to_json_schema("pkg-a#P", {"x": 1})["params"] == {"x": 1}
        with pytest.raises(TypertError, match="no JSON Schema projection"):
            registry.to_json_schema("pkg-a#Raw")

    def test_lookup_store_contract(self):
        registry = TypertRegistry()
        lookups = registry.lookups
        provider = {"parameter": "session", "wire": "session",
                    "hostTypeSymbol": "sym:Host", "wireTypeSymbol": "sym:Wire",
                    "resolve": lambda key: f"host:{key}"}
        dispose = lookups.register("sess", provider)
        assert lookups.get("sess")["resolve"]("k") == "host:k"
        with pytest.raises(TypertError, match="already registered"):
            lookups.register("sess", provider)
        resolver_dispose = lookups.configure("sess", lambda key: f"override:{key}")
        merged = lookups.get("sess")
        assert merged["resolve"]("k") == "override:k"
        assert merged["wire"] == "session"  # resolver never overwrites wire identity
        with pytest.raises(TypertError, match="resolver is already configured"):
            lookups.configure("sess", lambda key: None)
        # Wire declarations are immutable for the registry lifetime.
        resolver_dispose()
        dispose()
        changed = dict(provider, wire="other")
        with pytest.raises(TypertError, match="changed its wire declaration"):
            lookups.register("sess", changed)
        assert lookups.get("sess") is None and lookups.keys() == []
        with pytest.raises(TypertError, match="lookup parameter"):
            lookups.register("bad", dict(provider, parameter=""))

    def test_remote_store_package_and_endpoint_uniqueness(self):
        registry = TypertRegistry()
        remotes = registry.remotes
        dispose = remotes.register({"package": "remote-a",
                                    "descriptors": [_descriptor(ident="r1", namespace="rns")]})
        assert remotes.get("rns/m")["id"] == "r1"
        with pytest.raises(TypertError, match='Remote package "remote-a" is already registered'):
            remotes.register({"package": "remote-a", "descriptors": []})
        with pytest.raises(TypertError, match="already registered"):
            remotes.register({"package": "remote-b",
                              "descriptors": [_descriptor(ident="r2", namespace="rns")]})
        with pytest.raises(TypertError, match="invocation id"):
            remotes.register({"package": "remote-c",
                              "descriptors": [_descriptor(ident="r1", namespace="other")]})
        dispose()
        assert remotes.get("rns/m") is None and remotes.packages() == []

    def test_context_store_host_client(self):
        registry = TypertRegistry()
        contexts = registry.contexts
        adapter = {"wire": "session", "wireTypeSymbol": "sym:S", "create": lambda inv: "host-ctx"}
        dispose = contexts.register_host("sess", adapter)
        assert contexts.get_host("sess")["create"](None) == "host-ctx"
        dispose_resolver = contexts.configure_host("sess", lambda inv: "resolved")
        merged = contexts.get_host("sess")
        assert merged["resolve"](None) == "resolved" and merged["wire"] == "session"
        client_dispose = contexts.register_client("cli", {"create": lambda inv: "client-ctx"})
        assert contexts.get_client("cli")["create"](None) == "client-ctx"
        assert contexts.get_client("missing") is None
        with pytest.raises(TypertError, match="already registered"):
            contexts.register_host("sess", adapter)
        with pytest.raises(TypertError, match="Context wire field"):
            contexts.register_host("bad", {"wire": "a/b", "wireTypeSymbol": "s"})
        dispose()
        dispose_resolver()
        client_dispose()
        assert contexts.get_host("sess") is None and contexts.get_client("cli") is None

    def test_change_observers_isolated_and_reported(self):
        reports = []
        registry = TypertRegistry(report=lambda change, error: reports.append((change, error)))
        changes = []

        def bad_listener(change):
            raise RuntimeError("observer exploded")

        unsubscribe = registry.local["subscribe"](bad_listener)
        registry.local["subscribe"](changes.append)
        registry.register(_contribution(invocations=[_descriptor()]))
        assert len(changes) == 1 and changes[0] == {"kind": "local", "key": "ns/m"}
        assert len(reports) == 1 and isinstance(reports[0][1], RuntimeError)
        unsubscribe()
        unsubscribe()  # idempotent
        registry.local["list"]()

    def test_validate_invocation_rules(self):
        validate_invocation(_descriptor())  # minimal valid
        with pytest.raises(TypertError, match="invocation id"):
            validate_invocation(_descriptor(id=""))
        with pytest.raises(TypertError, match='receiver kind'):
            validate_invocation(_descriptor(invocation={"kind": "weird"}))
        with pytest.raises(TypertError, match="repeats wire field"):
            validate_invocation(_descriptor(parameters=[
                {"name": "a", "wire": "w", "source": "json", "codec": JSON_CODEC},
                {"name": "b", "wire": "w", "source": "json", "codec": JSON_CODEC}]))
        with pytest.raises(TypertError, match="cannot accept undefined"):
            validate_invocation(_descriptor(parameters=[
                {"name": "a", "wire": "a", "source": "lookup", "lookup": "k",
                 "acceptsUndefined": True, "codec": JSON_CODEC}]))
        with pytest.raises(TypertError, match="has no lookup key"):
            validate_invocation(_descriptor(parameters=[
                {"name": "a", "wire": "a", "source": "lookup", "codec": JSON_CODEC}]))
        with pytest.raises(TypertError, match="declares a lookup key"):
            validate_invocation(_descriptor(parameters=[
                {"name": "a", "wire": "a", "source": "json", "lookup": "k", "codec": JSON_CODEC}]))
        with pytest.raises(TypertError, match='cancellation parameter must be "signal"'):
            validate_invocation(_descriptor(cancellation={"parameter": "token"}))
        with pytest.raises(TypertError, match='mode must be "stream"'):
            validate_invocation(_descriptor(mode="batch"))
        with pytest.raises(TypertError, match="cannot declare a direct scope projection"):
            validate_invocation(_descriptor(
                invocation={"kind": "context", "context": "k", "wire": "ctxw", "codec": JSON_CODEC},
                scope={"context": "k", "wire": "w"}))
        with pytest.raises(TypertError, match="must select its only lookup parameter"):
            validate_invocation(_descriptor(scope={"context": "k", "wire": "w"}, parameters=[
                {"name": "a", "wire": "w", "source": "lookup", "lookup": "other", "codec": JSON_CODEC}]))
        validate_invocation(_descriptor(scope={"context": "k", "wire": "w"}, parameters=[
            {"name": "a", "wire": "w", "source": "lookup", "lookup": "k", "codec": JSON_CODEC}]))
        with pytest.raises(TypertError, match="repeats wire field"):
            validate_invocation(_descriptor(
                invocation={"kind": "context", "context": "k", "wire": "w", "codec": JSON_CODEC},
                parameters=[{"name": "a", "wire": "w", "source": "json", "codec": JSON_CODEC}]))
        with pytest.raises(TypertError, match="strict codec has no create"):
            validate_invocation(_descriptor(result={"mode": "strict", "typeSymbol": "sym"}))
        with pytest.raises(TypertError, match="type symbol"):
            validate_invocation(_descriptor(result={"mode": "strict", "create": lambda: 1}))


# -------------------------------------------------------------------- loader

VALID_MANIFEST = {
    "package": "pkg-x",
    "face": "host",
    "schemas": [{"name": "Config", "create": _ProjectionSchema}],
    "model": {"services": [], "events": [], "objects": []},
    "invocations": [{
        "id": "pkg-x#svc.do", "service": "svc", "namespace": "svc", "method": "do",
        "invocation": {"kind": "direct"},
        "parameters": [{"name": "arg", "wire": "arg", "source": "json",
                        "codec": {"mode": "strict", "typeSymbol": "sym:A", "create": lambda: 1}}],
        "result": {"mode": "strict", "typeSymbol": "sym:R", "create": lambda: 1},
        "sourceLocation": {"file": "a.ts", "line": 3, "column": 5},
    }],
}


class TestLoaderValidation:
    def test_valid_manifest_passes(self):
        assert validate_typert_manifest("pkg-x", VALID_MANIFEST) is VALID_MANIFEST

    @pytest.mark.parametrize("mutate,message", [
        (lambda m: m.update(package="other"), "must be owned by the package"),
        (lambda m: m.update(face="client"), 'TYPERT.face is not "host"'),
        (lambda m: m.update(schemas=[{"name": "S"}]), "has no create\\(\\) factory"),
        (lambda m: m.update(invocations=[]), None),  # empty invocations legal
        (lambda m: m["invocations"][0].update(method=""), "has a missing or empty method"),
        (lambda m: m["invocations"][0].update(invocation={"kind": "weird"}),
         'receiver kind must be "direct" or "context"'),
        (lambda m: m["invocations"][0]["parameters"][0].update(source="weird"),
         'parameter source must be "json" or "lookup"'),
        (lambda m: m["invocations"][0].update(cancellation={"parameter": "tok"}),
         'cancellation parameter must be "signal"'),
        (lambda m: m["invocations"][0].update(sourceLocation={"file": "a", "line": 0}),
         "must be a positive integer"),
        (lambda m: m.update(model={"services": [{"tags": [], "key": "k", "exportName": "E",
                                                 "members": [{"name": "m", "signature": "s",
                                                              "kind": "weird"}], "types": []}],
                                    "events": [], "objects": []}),
         "has invalid kind"),
    ])
    def test_manifest_failures_name_the_defect(self, mutate, message):
        import copy
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["schemas"] = [{"name": "Config", "create": _ProjectionSchema}]
        mutate(manifest)
        if message is None:
            validate_typert_manifest("pkg-x", manifest)
        else:
            with pytest.raises(TypertLoaderError, match=message):
                validate_typert_manifest("pkg-x", manifest)

    def test_non_object_export(self):
        with pytest.raises(TypertLoaderError, match="no TYPERT manifest object"):
            validate_typert_manifest("pkg-x", "not-a-manifest")


@pytest.fixture()
def contrib_package(tmp_path, monkeypatch):
    """Real importable modules: an entry plugin + a typert contribution."""
    (tmp_path / "anx_typert_entry.py").write_text(textwrap.dedent("""
        CALLS = []
        def start(ctx):
            CALLS.append(('start', ctx.plugin.id))
            return {'started': True}
        def stop(ctx, handle):
            CALLS.append(('stop', ctx.plugin.id))
    """), encoding="utf-8")
    (tmp_path / "anx_typert_contrib.py").write_text(textwrap.dedent("""
        class _Schema:
            def to_json_schema(self, params=None):
                return {'type': 'object'}
        IMPORTS = []
        TYPERT = {
            'package': 'anx-pkg',
            'face': 'host',
            'schemas': [{'name': 'Config', 'create': _Schema}],
            'model': {'services': [], 'events': [], 'objects': []},
            'invocations': [{
                'id': 'anx-pkg#svc.do', 'service': 'svc', 'namespace': 'svc', 'method': 'do',
                'invocation': {'kind': 'direct'}, 'parameters': [],
                'result': {'mode': 'src-json'},
            }],
        }
        IMPORTS.append(1)
    """), encoding="utf-8")
    (tmp_path / "anx_typert_broken.py").write_text("TYPERT = {'package': 'wrong-owner'}\n",
                                                   encoding="utf-8")
    (tmp_path / "anx_typert_empty.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in ("anx_typert_entry", "anx_typert_contrib", "anx_typert_broken", "anx_typert_empty"):
        sys.modules.pop(name, None)


class TestLoaderLifecycle:
    def _loader(self):
        registry = TypertRegistry()
        return registry, TypertLoader(registry)

    def test_attach_registers_and_detach_withdraws(self, contrib_package):
        registry, loader = self._loader()
        disposer = loader.attach("plugin-a", "anx_typert_contrib", "anx-pkg", configured=True)
        assert disposer is not None
        assert registry.get("anx-pkg#Config")["schema"].to_json_schema() == {"type": "object"}
        assert registry.local["get"]("svc/do") is not None
        assert loader.registered_entries() == ["plugin-a"]
        assert loader.detach("plugin-a") is True
        assert loader.detach("plugin-a") is False  # idempotent
        assert registry.get("anx-pkg#Config") is None
        assert registry.local["get"]("svc/do") is None

    def test_manifest_cache_imports_once(self, contrib_package):
        registry, loader = self._loader()
        loader.attach("plugin-a", "anx_typert_contrib", "anx-pkg", configured=True)
        loader.detach("plugin-a")
        loader.attach("plugin-b", "anx_typert_contrib", "anx-pkg", configured=True)
        import anx_typert_contrib  # type: ignore[import-not-found]  # synthesized module
        assert anx_typert_contrib.IMPORTS == [1]  # module import cached by Python; manifest cached by loader

    def test_discovered_without_export_skips_silently_configured_fails_loud(self, contrib_package):
        registry, loader = self._loader()
        assert loader.attach("plugin-a", "anx_typert_empty", "anx-pkg", configured=False) is None
        assert loader.registered_entries() == []
        with pytest.raises(TypertLoaderError, match="does not export"):
            loader.attach("plugin-a", "anx_typert_empty", "anx-pkg", configured=True)

    def test_broken_manifest_fails_loud_for_configured(self, contrib_package):
        registry, loader = self._loader()
        with pytest.raises(TypertLoaderError, match="must be owned by the package"):
            loader.attach("plugin-a", "anx_typert_broken", "anx-pkg", configured=True)
        assert loader.registered_entries() == []

    def test_discovered_broken_manifest_is_contained(self, contrib_package):
        registry = TypertRegistry()
        errors = []
        loader = TypertLoader(registry, report=errors.append)
        assert loader.attach("plugin-a", "anx_typert_broken", "anx-pkg", configured=False) is None
        assert len(errors) == 1 and "must be owned" in str(errors[0])

    def test_reconcile_attaches_and_detaches(self, contrib_package):
        registry, loader = self._loader()
        loader.attach("gone", "anx_typert_contrib", "anx-pkg", configured=True)
        assert loader.registered_entries() == ["gone"]
        # reconcile detaches entries that disappeared (freeing the package
        # face), then attaches the qualifying declared entries.
        attached = loader.reconcile([{"id": "plugin-a", "module": "anx_typert_contrib",
                                      "package": "anx-pkg", "configured": True}])
        assert attached == ["plugin-a"]
        assert loader.registered_entries() == ["plugin-a"]
        assert registry.get("anx-pkg#Config") is not None

    def test_reconcile_aggregates_configured_failures(self, contrib_package):
        registry, loader = self._loader()
        with pytest.raises(TypertLoaderError, match="contributor\\(s\\) failed to register"):
            loader.reconcile([{"id": "plugin-a", "module": "anx_typert_broken",
                               "package": "anx-pkg", "configured": True}])


# ------------------------------------------------- PluginHost integration


@pytest.fixture()
def plugin_host(tmp_path, contrib_package):
    from AstralNexus.runtime.plugin_host import PluginHost
    from AstralNexus.runtime.workbench import WorkbenchStore

    host = PluginHost(WorkbenchStore(tmp_path / "host"))
    registry = TypertRegistry()
    host.services["typert"] = registry
    host.typert_loader = TypertLoader(registry)
    return host, registry


MANIFEST = {"id": "anx-typert-demo", "name": "Typert Demo", "version": "1.0.0",
            "kind": "native", "entry": "anx_typert_entry",
            "typert": {"module": "anx_typert_contrib", "package": "anx-pkg"}}


def _phase(host, ident):
    entry = next(item for item in host.snapshot() if item["id"] == ident)
    return entry["phase"], entry.get("last_error")


class TestPluginHostIntegration:
    def test_start_registers_stop_withdraws(self, plugin_host):
        host, registry = plugin_host
        host.install_manifest(dict(MANIFEST), source="test")
        host.set_enabled("anx-typert-demo", True)
        assert _phase(host, "anx-typert-demo")[0] == "active"
        assert registry.get("anx-pkg#Config") is not None
        assert registry.local["get"]("svc/do") is not None
        assert host.typert_loader.registered_entries() == ["anx-typert-demo"]
        host.set_enabled("anx-typert-demo", False)
        assert registry.get("anx-pkg#Config") is None
        assert registry.local["get"]("svc/do") is None
        assert host.typert_loader.registered_entries() == []

    def test_broken_contribution_aggregates_into_activation_failure(self, plugin_host):
        host, registry = plugin_host
        broken = dict(MANIFEST, id="anx-typert-broken",
                      typert={"module": "anx_typert_broken", "package": "anx-pkg"})
        host.install_manifest(broken, source="test")
        host.set_enabled("anx-typert-broken", True)
        phase, last_error = _phase(host, "anx-typert-broken")
        assert phase == "failed"
        assert "load_failed" in last_error and "must be owned" in last_error
        import anx_typert_entry  # type: ignore[import-not-found]  # synthesized module
        assert ("stop", "anx-typert-broken") in anx_typert_entry.CALLS  # start rolled back

    def test_declaration_without_host_loader_fails_loud(self, plugin_host):
        host, registry = plugin_host
        host.typert_loader = None
        host.install_manifest(dict(MANIFEST, id="anx-no-loader"), source="test")
        host.set_enabled("anx-no-loader", True)
        phase, last_error = _phase(host, "anx-no-loader")
        assert phase == "failed"
        assert "no typert loader" in last_error

    def test_manifest_validation_rejects_bad_declarations(self, plugin_host):
        from AstralNexus.runtime.plugin_host import PluginError
        with pytest.raises(PluginError, match="only native plugins"):
            plugin_host[0].install_manifest(
                {"id": "gate-x", "name": "G", "version": "1", "kind": "gate",
                 "typert": {"module": "m", "package": "p"}}, source="test")
        with pytest.raises(PluginError, match="non-empty 'module' and 'package'"):
            plugin_host[0].install_manifest(
                dict(MANIFEST, id="bad-typert", typert={"module": "", "package": "p"}),
                source="test")

    def test_recover_restores_contributions(self, plugin_host, contrib_package):
        host, registry = plugin_host
        host.install_manifest(dict(MANIFEST), source="test")
        host.set_enabled("anx-typert-demo", True)
        assert registry.get("anx-pkg#Config") is not None
        # Simulate a restart: a fresh host over the same store recovers desired
        # state and re-registers the contribution.
        from AstralNexus.runtime.plugin_host import PluginHost
        host2 = PluginHost(host.store, home=host.home)
        registry2 = TypertRegistry()
        host2.services["typert"] = registry2
        host2.typert_loader = TypertLoader(registry2)
        host2.recover()
        assert registry2.get("anx-pkg#Config") is not None
        assert host2.is_active("anx-typert-demo")


# ------------------------------------------------ WorkbenchHost HTTP surface


class TestWorkbenchHttp:
    def test_typert_projection_route(self, tmp_path, contrib_package):
        from AstralNexus.dashboard.workbench_server import WorkbenchHost
        instance = WorkbenchHost(tmp_path / "wb")
        try:
            client = instance.app.test_client()
            response = client.get("/api/host/typert")
            assert response.status_code == 200
            body = response.get_json()
            assert body == {"schemas": [], "packages": [], "local_endpoints": [],
                            "remote_packages": [], "lookups": [], "loader_entries": []}
            snapshot = client.get("/api/host").get_json()
            assert snapshot["typert"] == body  # same live projection, no hardcoding
            # A real plugin start feeds the projection; disable withdraws it.
            instance.plugins.install_manifest(dict(MANIFEST, id="anx-wb-demo"), source="test")
            instance.plugins.set_enabled("anx-wb-demo", True)
            body = client.get("/api/host/typert").get_json()
            assert [item["key"] for item in body["schemas"]] == ["anx-pkg#Config"]
            assert body["local_endpoints"][0]["namespace"] == "svc"
            assert body["loader_entries"] == ["anx-wb-demo"]
            instance.plugins.set_enabled("anx-wb-demo", False)
            body = client.get("/api/host/typert").get_json()
            assert body["schemas"] == [] and body["loader_entries"] == []
        finally:
            instance.close()
