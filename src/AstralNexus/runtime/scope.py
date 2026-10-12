"""Scope tags with a single binding site and event routing filters.

Port of ``@deepseek-ai/dsh-scope`` (commit 5badb15, lib/index.js 357 lines +
lib/invariant.js 73 lines, both read in full). Contract kept:

- ``NamedEntries``: insertion-ordered unique names, caller-owned duplicate
  diagnostics, each insert returns an idempotent undo removing only that
  insertion; draining the table swaps in a fresh generation so detached
  iterators never observe later insertions.
- ``AnonymousEntries``: equal values remain separate registrations
  (independent identity keys); same undo/generation rules.
- ``ScopedLayers``: one eagerly created global layer plus exact-scope
  overlays. ``peek`` is deliberately chain-blind (None scope → no overlay);
  ``chain_layers`` returns EXISTING scoped overlays only (farthest ancestor
  first, exact scope last, global never included); ``merge(scope, pick)``
  materializes the picked global table then applies chain shadows so the
  nearest scope wins a name while global insertion order is preserved;
  ``effect(ctx, action, options)`` derives visibility AND ownership from the
  context's tag, creates/reuses the exact overlay, reclaims only a completely
  empty aggregate layer, collects undo before notifying, and notifies on both
  registration and disposal (options notify defaults True, label forwarded).
- Scope keys are opaque identities. ``bind_scope_parent(key, parent)`` is the
  single write site: an already-bound key throws (only the returned binding's
  ``rebind`` may relink), cycles are refused, and a bound root records the
  binding with parent None. ``scope_chain_of`` is nearest-first.
- ``scope_target(base, key)`` builds a routing-only carrier: the base filter
  is preserved, untagged listeners admit globally, and a tagged listener
  admits iff its tag appears in the DISPATCH KEY's ancestor chain — an
  enclosing scope hears every descendant's events; a tag below the key stays
  excluded (events flow up, never down). ``is_scope_carrier`` /
  ``carrier_key_of`` discriminate carriers minted here.
- The invariant companion's generated resolver table (26 scope-filtered
  events) and its dispatch check are ported verbatim as
  ``SCOPED_SUBJECT_RESOLVERS`` + ``check_scoped_dispatch``.

Python adaptations (documented):
- cordis ``ctx.plugin(scope)`` fiber + ``ctx.extend({[kScope]: key})`` →
  ``ScopedContext`` proxy (delegates attribute reads to the minting parent,
  carries the tag, owns an LIFO effect stack) and a ``Scope`` handle whose
  ``dispose`` is idempotent single-flight (quiesceFiber's promise memo →
  Event); ``raw_dispose`` is the claimable exact boundary (fiber.dispose).
- ``ctx.effect(generator, label)`` → if the context exposes a callable
  ``effect`` surface the disposer is registered there (scope teardown runs
  it, matching fiber ownership); otherwise the disposer is returned for
  caller ownership. ScopedLayers.effect keeps the upstream reclamation and
  notification semantics in both modes.
- ``Context.filter`` symbol → the carrier's ``admits(ctx)`` method;
  ``base[Context.filter]`` → an optional ``base_filter`` callable attribute.
- JS live Map iterators → dict views (same generation detachment via the
  empty-table swap; mid-iteration mutation raises RuntimeError natively —
  consumers snapshot, as upstream's own merge does).
- WeakMap tables → ``WeakKeyDictionary`` over weakref-able ``ScopeKey``
  instances minted by ``new_scope_key``.
- The invariant companion registration (apply/inject over the cordis
  ``invariants`` service and its ``internal/dispatch`` hook) is deferred
  until the native dispatch/invariants surface arrives (B4); the resolver
  table and the pure check function are its portable, testable body.
"""
from __future__ import annotations

import collections.abc
import threading
import weakref
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "NamedEntries", "AnonymousEntries", "ScopedLayers", "ScopeKey", "new_scope_key",
    "bind_scope_parent", "scope_parent_of", "scope_chain_of", "ScopedContext",
    "scope_of", "create_scope", "scope_target", "is_scope_carrier", "carrier_key_of",
    "SCOPED_SUBJECT_RESOLVERS", "check_scoped_dispatch",
]


# ------------------------------------------------------------------- stores


class NamedEntries:
    """Insertion-ordered named entries with caller-owned duplicate diagnostics.

    Values are borrowed. Iterators are views of one nonempty table
    generation; draining the table detaches them from later insertions.
    Each successful insertion returns an idempotent undo for that exact
    entry.
    """

    __slots__ = ("duplicate_error", "_data")

    def __init__(self, duplicate_error: Callable[[str], Exception]):
        self.duplicate_error = duplicate_error
        self._data: Dict[str, Any] = {}

    def insert(self, name: str, value: Any) -> Callable[[], None]:
        data = self._data
        if name in data:
            raise self.duplicate_error(name)
        data[name] = value
        active = [True]

        def undo() -> None:
            if not active[0]:
                return  # idempotent: only this insertion is removed, once
            active[0] = False
            data.pop(name, None)
            if len(data) == 0 and self._data is data:
                self._data = {}  # fresh generation: detached iterators stay detached

        return undo

    def get(self, name: str) -> Any:
        return self._data.get(name)

    def has(self, name: str) -> bool:
        return name in self._data

    def keys(self) -> "collections.abc.KeysView[str]":
        return self._data.keys()

    def entries(self) -> "collections.abc.ItemsView[str, Any]":
        return self._data.items()

    def values(self) -> "collections.abc.ValuesView[Any]":
        return self._data.values()

    def is_empty(self) -> bool:
        return len(self._data) == 0


class AnonymousEntries:
    """Insertion-ordered anonymous entries with independent identity.

    Equal values remain separate registrations; same undo/generation rules
    as NamedEntries.
    """

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: Dict[Any, Any] = {}

    def append(self, value: Any) -> Callable[[], None]:
        data = self._data
        key = object()  # the Symbol() equivalent: independent registration identity
        data[key] = value
        active = [True]

        def undo() -> None:
            if not active[0]:
                return
            active[0] = False
            data.pop(key, None)
            if len(data) == 0 and self._data is data:
                self._data = {}

        return undo

    def values(self) -> "collections.abc.ValuesView[Any]":
        return self._data.values()

    def is_empty(self) -> bool:
        return len(self._data) == 0


class ScopedLayers:
    """Own the global and exact-scope layers for one registry.

    Reads never create scoped layers. Registrations derive both visibility
    and effect ownership from the supplied context's scope tag, collect undo
    before notification, and reclaim only a completely empty aggregate layer
    (an exact-scope layer never falls back to the global layer).
    """

    __slots__ = ("create_layer", "on_change", "global_layer", "_scoped")

    def __init__(self, create_layer: Callable[[Optional[Any]], Any],
                 on_change: Callable[[], None]):
        if not callable(create_layer) or not callable(on_change):
            raise TypeError("scope: create_layer and on_change must be callable")
        self.create_layer = create_layer
        self.on_change = on_change
        self.global_layer = create_layer(None)  # eagerly constructed context-global layer
        self._scoped: Dict[Any, Any] = {}

    # -- read side

    def peek(self, scope: Optional[Any]) -> Optional[Any]:
        """Read an existing exact-scope overlay, deliberately chain-blind."""
        if scope is None:
            return None  # undefined denotes "no overlay", never the global layer
        return self._scoped.get(scope)

    def chain_layers(self, scope: Optional[Any]) -> List[Any]:
        """Existing overlays along the parent chain, farthest ancestor first."""
        layers: List[Any] = []
        for key in reversed(scope_chain_of(scope)):
            layer = self._scoped.get(key)
            if layer is not None:
                layers.append(layer)
        return layers

    def merge(self, scope: Optional[Any],
              pick: Callable[[Any], NamedEntries]) -> Dict[str, Any]:
        """Global named entries shadowed along the chain; nearest scope wins."""
        merged: Dict[str, Any] = dict(pick(self.global_layer).entries())
        for layer in self.chain_layers(scope):
            for name, value in pick(layer).entries():
                merged[name] = value  # existing names keep their global position
        return merged

    # -- write side

    def effect(self, ctx: Optional[Any], action: Callable[[Any], Callable[[], None]],
               options: Optional[Dict[str, Any]] = None) -> Callable[[], None]:
        """Attach one synchronous layer mutation to its registration context."""
        options = options or {}
        label = options.get("label", "")
        notify = bool(options.get("notify", True))
        scope = scope_of(ctx)

        layer: Any
        created = False
        if scope is None:
            layer = self.global_layer
        else:
            existing = self._scoped.get(scope)
            if existing is None:
                layer = self.create_layer(scope)
                self._scoped[scope] = layer
                created = True
            else:
                layer = existing
        try:
            undo = action(layer)
        except Exception:
            if scope is not None and created and layer.is_empty():
                del self._scoped[scope]  # a failed registration leaves no trace
            raise

        spent = [False]

        def teardown() -> None:
            if spent[0]:
                return  # the disposer is idempotent (cordis effect semantics)
            spent[0] = True
            undo()
            if scope is not None and layer.is_empty():
                self._scoped.pop(scope, None)
            if notify:
                self.on_change()

        if notify:
            self.on_change()
        register = getattr(ctx, "effect", None)
        if callable(register):
            return register(teardown, label)  # scope/fiber owns the teardown
        return teardown  # caller-owned disposer (root or effect-less context)


# ------------------------------------------------------------- scope keys


class ScopeKey:
    """An opaque scope identity (the upstream ``Symbol(description)``)."""

    __slots__ = ("label", "__weakref__")

    def __init__(self, label: str = ""):
        self.label = label

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ScopeKey({self.label!r})"


def new_scope_key(label: str = "") -> ScopeKey:
    return ScopeKey(label)


_SCOPE_PARENTS: "weakref.WeakKeyDictionary[ScopeKey, Optional[ScopeKey]]" = \
    weakref.WeakKeyDictionary()


def _link_scope_parent(key: ScopeKey, parent: Optional[ScopeKey]) -> None:
    """Cycle-checked write shared by the bind and every rebind."""
    cursor = parent
    while cursor is not None:
        if cursor is key:
            raise RuntimeError("dsh-scope: scope parent link would form a cycle")
        cursor = _SCOPE_PARENTS.get(cursor)
    _SCOPE_PARENTS[key] = parent  # a bound root records the binding with None


class ScopeParentBinding:
    """The only handle allowed to relink a bound key (upstream object literal)."""

    __slots__ = ("_key",)

    def __init__(self, key: ScopeKey):
        self._key = key

    def rebind(self, parent: Optional[ScopeKey]) -> None:
        _link_scope_parent(self._key, parent)


def bind_scope_parent(key: ScopeKey, parent: Optional[ScopeKey]) -> ScopeParentBinding:
    """Bind ``parent`` as ``key``'s enclosing scope, once."""
    if key in _SCOPE_PARENTS:
        raise RuntimeError("dsh-scope: scope key is already bound to a parent; "
                           "re-linking requires the binding returned by the original bind")
    _link_scope_parent(key, parent)
    return ScopeParentBinding(key)


def scope_parent_of(key: Optional[ScopeKey]) -> Optional[ScopeKey]:
    if key is None:
        return None
    return _SCOPE_PARENTS.get(key)


def scope_chain_of(key: Optional[ScopeKey]) -> List[ScopeKey]:
    """The chain from a key to its root ancestor, nearest first."""
    chain: List[ScopeKey] = []
    cursor = key
    while cursor is not None:
        chain.append(cursor)
        cursor = _SCOPE_PARENTS.get(cursor)
    return chain


# ---------------------------------------------------------- scope contexts


class ScopedContext:
    """A context proxy carrying a scope tag (cordis ``ctx.extend`` equivalent).

    Delegates every unknown attribute to the minting parent context (the
    inherited dependency API) and owns an LIFO effect stack torn down by
    ``Scope.raw_dispose`` — the fiber's registration ownership, natively.
    """

    def __init__(self, parent: Any, key: ScopeKey):
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_scope_key", key)
        object.__setattr__(self, "_effects", [])  # LIFO of (label, disposer)
        object.__setattr__(self, "_disposed", False)

    @property
    def scope_key(self) -> ScopeKey:
        return object.__getattribute__(self, "_scope_key")

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_parent"), name)

    def effect(self, disposer: Callable[[], None], label: str = "") -> Callable[[], None]:
        """Own one disposer for this scope's lifetime.

        The returned closure mirrors the cordis ``ctx.effect`` disposer: it
        RUNS the cleanup immediately (once), removing it from the stack so
        scope teardown will not repeat it.
        """
        if not callable(disposer):
            raise TypeError("scope: effect disposer must be callable")
        if object.__getattribute__(self, "_disposed"):
            raise RuntimeError("scope: cannot register effects on a disposed scope")
        stack = object.__getattribute__(self, "_effects")
        entry = (label, disposer)
        stack.append(entry)
        done = [False]

        def dispose_effect() -> None:
            if done[0]:
                return
            done[0] = True
            try:
                stack.remove(entry)
            except ValueError:
                pass  # already claimed by scope teardown
            disposer()

        return dispose_effect

    def _teardown(self) -> None:
        object.__setattr__(self, "_disposed", True)
        stack = object.__getattribute__(self, "_effects")
        while stack:
            _label, disposer = stack.pop()  # LIFO, like fiber teardown
            disposer()


def scope_of(ctx: Any) -> Optional[ScopeKey]:
    """Read the nearest scope tag inherited by a context (None when unscoped)."""
    if isinstance(ctx, ScopedContext):
        return ctx.scope_key  # nearest tag wins (extend shadows)
    return None


class Scope:
    """The scoped context and its exact/shared disposal boundaries."""

    __slots__ = ("ctx", "key", "_claimed", "_lock", "_done")

    def __init__(self, ctx: ScopedContext, key: ScopeKey):
        self.ctx = ctx
        self.key = key
        self._claimed = False
        self._lock = threading.Lock()
        self._done = threading.Event()

    def raw_dispose(self) -> None:
        """The exact disposer (fiber.dispose): tears effects down, once."""
        with self._lock:
            already = self._claimed
            self._claimed = True
        if not already:
            self.ctx._teardown()
        self._done.set()

    def dispose(self) -> None:
        """The shared boundary (quiesceFiber memo): idempotent, waits for teardown."""
        self.raw_dispose()
        self._done.wait()

    @property
    def disposed(self) -> bool:
        return self._done.is_set()


def create_scope(ctx: Any, key: ScopeKey,
                 options: Optional[Dict[str, Any]] = None) -> Scope:
    """Mint a scope under ``ctx``: inherit its API, tag registrations, own disposal."""
    if options is not None and options.get("parent") is not None:
        bind_scope_parent(key, options["parent"])
    scoped = ScopedContext(ctx, key)
    return Scope(scoped, key)


# --------------------------------------------------------- scope targeting


class _ScopeCarrier:
    """A routing-only dispatch receiver (cordis ``Context.filter`` equivalent)."""

    __slots__ = ("admits", "__weakref__")

    def __init__(self, admits: Callable[[Any], bool]):
        self.admits = admits


_CARRIER_KEYS: "weakref.WeakKeyDictionary[_ScopeCarrier, Optional[ScopeKey]]" = \
    weakref.WeakKeyDictionary()


def scope_target(base: Any, key: Optional[ScopeKey]) -> _ScopeCarrier:
    """Preserve ``base``'s filter; admit untagged globally, tagged up the chain.

    A listener owned by an enclosing scope receives every descendant scope's
    events; a tag BELOW the dispatch key stays excluded — events flow up the
    chain, never down.
    """
    base_filter = getattr(base, "base_filter", None)

    def admits(ctx: Any) -> bool:
        if base_filter is not None and not base_filter(ctx):
            return False
        tag = scope_of(ctx)
        if tag is None:
            return True  # untagged listeners always receive scope-filtered events
        cursor = key
        while cursor is not None:  # walk the DISPATCH KEY's ancestors for the tag
            if cursor is tag:
                return True
            cursor = _SCOPE_PARENTS.get(cursor)
        return False

    carrier = _ScopeCarrier(admits)
    _CARRIER_KEYS[carrier] = key
    return carrier


def is_scope_carrier(value: Any) -> bool:
    return isinstance(value, _ScopeCarrier) and value in _CARRIER_KEYS


def carrier_key_of(value: Any) -> Optional[ScopeKey]:
    if not is_scope_carrier(value):
        return None
    return _CARRIER_KEYS.get(value)


# ------------------------------------------------- scoped-dispatch invariant


def _agent_subject(args: Any) -> Any:
    return args[0]["agent"]


SCOPED_SUBJECT_RESOLVERS: Dict[str, Optional[Callable[[Any], Any]]] = {
    "agent/assistant-stream": _agent_subject,
    "agent/created": _agent_subject,
    "agent/disposed": _agent_subject,
    "agent/error": _agent_subject,
    "agent/inbox/claimed": _agent_subject,
    "agent/inbox/discarded": _agent_subject,
    "agent/inbox/inserted": _agent_subject,
    "agent/pre-step": _agent_subject,
    "agent/request": _agent_subject,
    "agent/request-error": _agent_subject,
    "agent/status": _agent_subject,
    "agent/turn-stopping": _agent_subject,
    "approval/request": _agent_subject,
    "goal/changed": _agent_subject,
    "session/created": None,
    "session/disposed": None,
    "session/event": None,
    "session/flush": None,
    "subagent/end": None,
    "subagent/start": None,
    "system-prompt/assemble": lambda args: args[1]["scope"],
    "tools/execute": _agent_subject,
    "tools/post-execute": _agent_subject,
    "tools/pre-execute": _agent_subject,
    "tools/ptc-dispatch-log": _agent_subject,
    "tools/result": _agent_subject,
    "user-questions/request": _agent_subject,
}


def check_scoped_dispatch(event: str, args: Any, this_arg: Any,
                          fail: Callable[[str], None]) -> None:
    """The invariant companion's dispatch check as a pure function.

    ``fail`` receives the verbatim upstream diagnostic. The companion
    registration over the invariants service and the native dispatch surface
    arrives with B4; this check and its generated resolver table are the
    portable body.
    """
    if event not in SCOPED_SUBJECT_RESOLVERS:
        return
    resolver = SCOPED_SUBJECT_RESOLVERS[event]
    if not is_scope_carrier(this_arg):
        fail(f'"{event}" is a scope-filtered event but was dispatched without a scope '
             f"carrier — pass scopeTarget(base, subject) as the dispatch thisArg "
             f"(agent events: use agentEvents(ctx, agent))")
        return
    if resolver is not None and carrier_key_of(this_arg) is not resolver(args):
        fail(f'"{event}" was dispatched with a scope carrier keyed to a DIFFERENT '
             f"subject than its arguments name — the carrier key and the event's "
             f"subject must be the same object (use agentEvents(ctx, agent))")
