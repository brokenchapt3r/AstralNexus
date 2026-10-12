"""Contract tests for AstralNexus.runtime.scope (dsh-scope port).

Covers: insertion-ordered stores with idempotent undo and generation
detachment; ScopedLayers peek/chain/merge/effect ownership and reclamation;
scope key binding (once-only, rebind, cycle refusal); ScopedContext tag
delegation and LIFO effect teardown; scope_target upward event admission
with base-filter preservation; carrier discrimination; and the ported
scoped-dispatch invariant check.
"""
from __future__ import annotations

import threading

import pytest

from AstralNexus.runtime.scope import (
    SCOPED_SUBJECT_RESOLVERS, AnonymousEntries, NamedEntries, ScopeKey, ScopedContext,
    ScopedLayers, bind_scope_parent, carrier_key_of, check_scoped_dispatch, create_scope,
    is_scope_carrier, new_scope_key, scope_chain_of, scope_of, scope_parent_of,
    scope_target)


def dup_error(name: str) -> Exception:
    return ValueError(f'duplicate "{name}"')


class TestNamedEntries:
    def test_insertion_order_and_lookup(self) -> None:
        table = NamedEntries(dup_error)
        table.insert("b", 2)
        table.insert("a", 1)
        assert list(table.keys()) == ["b", "a"]
        assert list(table.values()) == [2, 1]
        assert dict(table.entries()) == {"b": 2, "a": 1}
        assert table.get("a") == 1
        assert table.get("missing") is None
        assert table.has("b") and not table.has("missing")
        assert not table.is_empty()

    def test_duplicate_uses_caller_owned_diagnostic(self) -> None:
        table = NamedEntries(dup_error)
        table.insert("x", 1)
        with pytest.raises(ValueError, match='duplicate "x"'):
            table.insert("x", 2)
        assert table.get("x") == 1  # the rejected insert changed nothing

    def test_undo_is_idempotent_and_removes_only_that_insertion(self) -> None:
        table = NamedEntries(dup_error)
        undo_x = table.insert("x", 1)
        table.insert("y", 2)
        undo_x()
        undo_x()  # idempotent
        assert not table.has("x") and table.has("y")
        assert not table.is_empty()

    def test_draining_swaps_generation_detaching_live_iterators(self) -> None:
        table = NamedEntries(dup_error)
        undo = table.insert("only", 1)
        live_keys = table.keys()  # a view of the current generation
        undo()
        assert table.is_empty()
        table.insert("later", 2)
        assert list(live_keys) == []  # detached: never observes later insertions
        assert list(table.keys()) == ["later"]

    def test_stale_undo_after_reinsert_deletes_the_name(self) -> None:
        # Faithful upstream quirk: the undo closes over the (still current)
        # table and deletes the name even when a newer insert owns it.
        table = NamedEntries(dup_error)
        undo = table.insert("x", 1)
        undo()
        table.insert("x", 2)
        undo()  # already spent: idempotent no-op
        assert table.get("x") == 2


class TestAnonymousEntries:
    def test_equal_values_remain_separate_registrations(self) -> None:
        table = AnonymousEntries()
        undo_a = table.append("same")
        table.append("same")
        assert list(table.values()) == ["same", "same"]
        undo_a()
        assert list(table.values()) == ["same"]
        assert not table.is_empty()

    def test_drain_generation_detachment(self) -> None:
        table = AnonymousEntries()
        undo = table.append(1)
        live = table.values()
        undo()
        table.append(2)
        assert list(live) == []
        assert list(table.values()) == [2]


class TestScopedLayers:
    def make(self, changes: list | None = None) -> ScopedLayers:
        counter = changes if changes is not None else []
        return ScopedLayers(lambda scope: Layer(scope), lambda: counter.append(1))

    def test_peek_is_chain_blind_and_never_creates(self) -> None:
        layers = self.make()
        key = new_scope_key("agent")
        assert layers.peek(None) is None  # None denotes "no overlay", not global
        assert layers.peek(key) is None
        assert layers._scoped == {}  # reads never create scoped layers
        assert layers.global_layer is not None  # global is eager
        assert layers.global_layer.scope is None

    def test_chain_layers_farthest_first_skipping_absent(self) -> None:
        layers = self.make()
        root = new_scope_key("root")
        mid = new_scope_key("mid")
        leaf = new_scope_key("leaf")
        bind_scope_parent(mid, root)
        bind_scope_parent(leaf, mid)
        root_layer = Layer(root)
        leaf_layer = Layer(leaf)
        layers._scoped[root] = root_layer
        layers._scoped[leaf] = leaf_layer
        assert layers.chain_layers(leaf) == [root_layer, leaf_layer]  # mid absent, global excluded
        assert layers.chain_layers(None) == []

    def test_merge_global_order_preserved_nearest_wins(self) -> None:
        layers = self.make()
        root = new_scope_key("root")
        leaf = new_scope_key("leaf")
        bind_scope_parent(leaf, root)
        layers.global_layer.entries_.insert("a", "global-a")
        layers.global_layer.entries_.insert("b", "global-b")
        root_layer = Layer(root)
        root_layer.entries_.insert("a", "root-a")
        root_layer.entries_.insert("c", "root-c")
        leaf_layer = Layer(leaf)
        leaf_layer.entries_.insert("a", "leaf-a")
        layers._scoped[root] = root_layer
        layers._scoped[leaf] = leaf_layer
        merged = layers.merge(leaf, lambda layer: layer.entries_)
        assert list(merged.items()) == [("a", "leaf-a"), ("b", "global-b"), ("c", "root-c")]

    def test_effect_global_registration_and_notify_counts(self) -> None:
        changes: list = []
        layers = self.make(changes)
        dispose = layers.effect(None, lambda layer: layer.entries_.insert("x", 1),
                                {"label": "test"})
        assert layers.global_layer.entries_.get("x") == 1
        assert changes == [1]  # notified on registration
        dispose()
        dispose()  # idempotent: no second notify
        assert not layers.global_layer.entries_.has("x")
        assert changes == [1, 1]  # exactly one disposal notify

    def test_effect_notify_can_be_suppressed(self) -> None:
        changes: list = []
        layers = self.make(changes)
        dispose = layers.effect(None, lambda layer: layer.entries_.insert("x", 1),
                                {"notify": False})
        dispose()
        assert changes == []

    def test_effect_creates_and_reclaims_only_empty_scoped_overlay(self) -> None:
        layers = self.make()
        key = new_scope_key("agent")
        ctx = ScopedContext(object(), key)
        dispose_a = layers.effect(ctx, lambda layer: layer.entries_.insert("a", 1))
        assert layers.peek(key) is not None
        dispose_b = layers.effect(ctx, lambda layer: layer.entries_.insert("b", 2))
        dispose_a()
        assert layers.peek(key) is not None  # non-empty overlay is kept
        dispose_b()
        assert layers.peek(key) is None  # completely empty overlay reclaimed
        assert layers.global_layer.is_empty_all()

    def test_failed_action_leaves_no_trace(self) -> None:
        layers = self.make()
        key = new_scope_key("agent")
        ctx = ScopedContext(object(), key)

        def boom(layer):
            raise RuntimeError("registration failed")

        with pytest.raises(RuntimeError, match="registration failed"):
            layers.effect(ctx, boom)
        assert layers.peek(key) is None  # created-then-empty overlay deleted

    def test_effect_registers_with_context_effect_surface(self) -> None:
        layers = self.make()
        key = new_scope_key("agent")
        scope = create_scope(object(), key)
        layers.effect(scope.ctx, lambda layer: layer.entries_.insert("x", 1))
        assert layers.peek(key) is not None
        scope.dispose()  # fiber teardown runs the registered disposer
        assert layers.peek(key) is None
        assert not layers.global_layer.entries_.has("x")


class Layer:
    """A minimal aggregate layer for ScopedLayers tests."""

    def __init__(self, scope=None):
        self.scope = scope
        self.entries_ = NamedEntries(dup_error)
        self.anons = AnonymousEntries()

    def is_empty(self) -> bool:
        return self.entries_.is_empty() and self.anons.is_empty()

    def is_empty_all(self) -> bool:
        return self.is_empty()


class TestScopeKeys:
    def test_bind_once_and_rebind(self) -> None:
        key = new_scope_key("child")
        parent_a = new_scope_key("a")
        parent_b = new_scope_key("b")
        binding = bind_scope_parent(key, parent_a)
        assert scope_parent_of(key) is parent_a
        with pytest.raises(RuntimeError, match="already bound to a parent"):
            bind_scope_parent(key, parent_b)
        binding.rebind(parent_b)
        assert scope_parent_of(key) is parent_b
        binding.rebind(None)  # a rebound root keeps its binding entry
        assert scope_parent_of(key) is None
        with pytest.raises(RuntimeError, match="already bound"):
            bind_scope_parent(key, parent_a)

    def test_bound_root_records_the_binding(self) -> None:
        key = new_scope_key("root")
        bind_scope_parent(key, None)
        assert scope_parent_of(key) is None
        with pytest.raises(RuntimeError, match="already bound"):
            bind_scope_parent(key, None)

    def test_cycle_refused_self_and_long(self) -> None:
        a = new_scope_key("a")
        b = new_scope_key("b")
        binding_a = bind_scope_parent(a, None)
        bind_scope_parent(b, a)
        # re-linking a under b would close a → b → a
        with pytest.raises(RuntimeError, match="would form a cycle"):
            binding_a.rebind(b)
        # self-cycle through rebind
        key = new_scope_key("self")
        binding = bind_scope_parent(key, None)
        with pytest.raises(RuntimeError, match="would form a cycle"):
            binding.rebind(key)
        assert scope_parent_of(a) is None  # the refused relink changed nothing

    def test_chain_nearest_first(self) -> None:
        root = new_scope_key("root")
        mid = new_scope_key("mid")
        leaf = new_scope_key("leaf")
        bind_scope_parent(mid, root)
        bind_scope_parent(leaf, mid)
        assert scope_chain_of(leaf) == [leaf, mid, root]
        assert scope_chain_of(None) == []
        orphan = new_scope_key("orphan")
        assert scope_chain_of(orphan) == [orphan]  # unbound key is its own root


class TestScopedContext:
    def test_tag_and_parent_delegation(self) -> None:
        class FakeCtx:
            services = {"credentials": object()}

            def greeting(self):
                return "hello"

        parent = FakeCtx()
        key = new_scope_key("agent")
        scope = create_scope(parent, key)
        assert scope_of(scope.ctx) is key
        assert scope_of(parent) is None
        assert scope.ctx.services is parent.services  # inherited dependency API
        assert scope.ctx.greeting() == "hello"

    def test_nested_scope_tags_nearest(self) -> None:
        outer_key = new_scope_key("outer")
        inner_key = new_scope_key("inner")
        outer = create_scope(object(), outer_key)
        inner = create_scope(outer.ctx, inner_key, {"parent": outer_key})
        assert scope_of(inner.ctx) is inner_key
        assert scope_chain_of(inner_key) == [inner_key, outer_key]

    def test_effects_tear_down_lifo_and_dispose_is_idempotent(self) -> None:
        order: list = []
        key = new_scope_key("agent")
        scope = create_scope(object(), key)
        scope.ctx.effect(lambda: order.append("first"), "e1")
        scope.ctx.effect(lambda: order.append("second"), "e2")
        scope.dispose()
        scope.dispose()  # single-flight memo
        assert order == ["second", "first"]  # LIFO, like fiber teardown
        assert scope.disposed

    def test_manual_dispose_runs_cleanup_and_leaves_the_rest(self) -> None:
        order: list = []
        scope = create_scope(object(), new_scope_key("agent"))
        dispose_one = scope.ctx.effect(lambda: order.append("gone"))
        scope.ctx.effect(lambda: order.append("kept"))
        dispose_one()
        dispose_one()  # idempotent
        scope.dispose()  # teardown runs only the still-owned effects
        assert order == ["gone", "kept"]

    def test_effect_after_disposal_refused(self) -> None:
        scope = create_scope(object(), new_scope_key("agent"))
        scope.dispose()
        with pytest.raises(RuntimeError, match="disposed scope"):
            scope.ctx.effect(lambda: None)

    def test_concurrent_dispose_single_flight(self) -> None:
        runs: list = []
        scope = create_scope(object(), new_scope_key("agent"))
        scope.ctx.effect(lambda: runs.append(1))
        barrier = threading.Barrier(4)
        errors: list = []

        def disposer_thread() -> None:
            try:
                barrier.wait(timeout=5)
                scope.dispose()
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=disposer_thread) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        assert runs == [1]  # the effect ran exactly once across racing disposers

    def test_create_scope_binds_only_with_explicit_parent(self) -> None:
        key = new_scope_key("free")
        create_scope(object(), key)  # no options → no binding at all
        assert key not in scope_chain_of(key)[1:]  # chain is just [key]
        assert scope_chain_of(key) == [key]
        # a second create_scope with the same unbound key is still free
        create_scope(object(), key)


class TestScopeTarget:
    def make_chain(self):
        root = new_scope_key("root")
        leaf = new_scope_key("leaf")
        bind_scope_parent(leaf, root)
        return root, leaf

    def test_untagged_listener_admits_globally(self) -> None:
        root, leaf = self.make_chain()
        carrier = scope_target(object(), leaf)
        assert carrier.admits(object()) is True  # untagged ctx

    def test_matching_and_ancestor_tags_admit_descendant_never_hears(self) -> None:
        root, leaf = self.make_chain()
        carrier = scope_target(object(), leaf)  # dispatch keyed to the leaf
        leaf_ctx = ScopedContext(object(), leaf)
        root_ctx = ScopedContext(object(), root)
        assert carrier.admits(leaf_ctx) is True  # exact match
        assert carrier.admits(root_ctx) is True  # enclosing scope hears descendants
        # now dispatch at the root: a leaf-tagged listener stays excluded
        root_carrier = scope_target(object(), root)
        assert root_carrier.admits(root_ctx) is True
        assert root_carrier.admits(leaf_ctx) is False  # events flow up, never down

    def test_base_filter_preserved(self) -> None:
        _root, leaf = self.make_chain()
        tagged_ctx = ScopedContext(object(), leaf)

        class Blocking:
            base_filter = staticmethod(lambda ctx: False)

        class Permissive:
            base_filter = staticmethod(lambda ctx: True)

        blocked = scope_target(Blocking(), leaf)
        assert blocked.admits(tagged_ctx) is False  # base filter runs first
        assert blocked.admits(object()) is False  # even untagged listeners
        opened = scope_target(Permissive(), leaf)
        assert opened.admits(tagged_ctx) is True

    def test_unscoped_subject_carrier(self) -> None:
        carrier = scope_target(object(), None)
        assert carrier.admits(object()) is True
        tagged = ScopedContext(object(), new_scope_key("x"))
        assert carrier.admits(tagged) is False  # no ancestor chain to match

    def test_carrier_discrimination(self) -> None:
        root, leaf = self.make_chain()
        carrier = scope_target(object(), leaf)
        assert is_scope_carrier(carrier) is True
        assert carrier_key_of(carrier) is leaf
        assert is_scope_carrier(object()) is False
        assert is_scope_carrier(None) is False
        assert carrier_key_of(object()) is None
        assert carrier_key_of("not-a-carrier") is None


class TestScopedDispatchInvariant:
    def test_resolver_table_is_the_generated_27(self) -> None:
        assert len(SCOPED_SUBJECT_RESOLVERS) == 27
        presence_only = {name for name, resolver in SCOPED_SUBJECT_RESOLVERS.items()
                         if resolver is None}
        assert presence_only == {"session/created", "session/disposed", "session/event",
                                 "session/flush", "subagent/end", "subagent/start"}
        agent = object()
        agent_resolver = SCOPED_SUBJECT_RESOLVERS["agent/status"]
        assert agent_resolver is not None
        assert agent_resolver(({"agent": agent},)) is agent
        scope_key = new_scope_key("s")
        resolver = SCOPED_SUBJECT_RESOLVERS["system-prompt/assemble"]
        assert resolver is not None
        assert resolver(({}, {"scope": scope_key})) is scope_key

    def test_unfiltered_event_passes_without_carrier(self) -> None:
        failures: list = []
        check_scoped_dispatch("chat/message", (), object(), failures.append)
        assert failures == []

    def test_filtered_event_without_carrier_fails_verbatim(self) -> None:
        failures: list = []
        check_scoped_dispatch("agent/status", (), None, failures.append)
        assert len(failures) == 1
        assert '"agent/status" is a scope-filtered event' in failures[0]
        assert "agentEvents(ctx, agent)" in failures[0]

    def test_presence_only_event_accepts_any_carrier(self) -> None:
        failures: list = []
        carrier = scope_target(object(), new_scope_key("x"))
        check_scoped_dispatch("session/created", (), carrier, failures.append)
        assert failures == []

    def test_keyed_event_requires_the_same_subject(self) -> None:
        failures: list = []
        agent = object()
        carrier = scope_target(object(), new_scope_key("other"))
        check_scoped_dispatch("agent/status", ({"agent": agent},), carrier, failures.append)
        assert len(failures) == 1
        assert "DIFFERENT subject" in failures[0]

    def test_keyed_event_passes_when_subject_matches(self) -> None:
        failures: list = []
        agent_key = new_scope_key("agent")
        carrier = scope_target(object(), agent_key)
        check_scoped_dispatch("agent/status", ({"agent": agent_key},), carrier,
                              failures.append)
        assert failures == []

    def test_assemble_event_reads_scope_from_second_argument(self) -> None:
        failures: list = []
        scope_key = new_scope_key("s")
        carrier = scope_target(object(), scope_key)
        check_scoped_dispatch("system-prompt/assemble", ({}, {"scope": scope_key}),
                              carrier, failures.append)
        assert failures == []
        other = scope_target(object(), new_scope_key("t"))
        check_scoped_dispatch("system-prompt/assemble", ({}, {"scope": scope_key}),
                              other, failures.append)
        assert len(failures) == 1 and "DIFFERENT subject" in failures[0]


class TestScopeKeyType:
    def test_keys_are_distinct_identities(self) -> None:
        a = ScopeKey("same-label")
        b = ScopeKey("same-label")
        assert a is not b
        assert repr(a) == "ScopeKey('same-label')"
