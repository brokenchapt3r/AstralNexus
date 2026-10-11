"""Compiler-independent Remote metadata and Typert provider protocols.

Port of ``@deepseek-ai/dsh-typert-protocol`` (commit 5badb15). Contract kept:
- ``RemoteError``: the one Remote failure class shared by owners, Gateway and
  consumers — a real error carrying a stable ``code`` and typed ``details``.
  Discrimination is ALWAYS by code / structural marker, never by ``isinstance``
  (cross-module copies of the class must keep matching).
- ``is_remote_json_value`` / ``is_remote_uplink_item``: lossless JSON boundary
  checks; an uplink item may additionally be the top-level ABSENT sentinel
  (JS ``undefined`` carried as an item frame without a value). Nested ABSENT,
  NaN and infinities stay rejected.
- ``typert_owned_value``: transfer cleanup ownership without another resource
  reference count; release runs at most once and never throws by contract.
- Remote segment grammar ``^[A-Za-z0-9_$.-]+$`` excluding ``.``/``..``.
- ``@remote`` / ``@remote_scope`` record versioned invocation markers on the
  method; ``remote_methods`` returns an order-stable snapshot that cannot
  mutate the stored marker; conflicting markers on one method fail loud.
- ``bind_typert_remote`` validates the service key and namespace and returns a
  frozen, inspectable binding with no compiler-injected metadata.

Python adaptations (documented):
- TS decorators become Python method decorators; markers ride on the function
  object and ``remote_methods`` scans the MRO (upstream attaches to the live
  instance's own prototype at construction). Order: bases first, then class
  definition order — deterministic and inspectable.
- ``Symbol.dispose`` becomes ``close()`` + context-manager protocol.
- The Cordis ``ctx.invocation`` accessor registration has no native equivalent
  until the B4 agent-loop port; ``bind_typert_remote`` therefore only records
  the binding (no silent fake).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from ..dshutil.values import is_json_value

TYPERT_REMOTE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_$.-]+$")
REMOTE_METHOD_DESCRIPTOR_VERSION = 1


class RemoteError(Exception):
    """One Remote call failure: stable code + typed details, wire-portable."""

    is_dsh_remote_error = True  # structural marker; never discriminate by isinstance

    def __init__(self, code: str, message: str, details: Any = None, *, cause: Optional[BaseException] = None):
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause
        self.code = code
        self.details = details
        self.name = "RemoteError"


def remote_error_of(value: Any) -> Optional[Any]:
    """Structurally identify a RemoteError across module copies of the class."""
    if (isinstance(value, object) and getattr(value, "is_dsh_remote_error", None) is True
            and isinstance(getattr(value, "code", None), str)):
        return value
    return None


class _Absent:
    """Top-level undefined equivalent for uplink items."""
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - display only
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT = _Absent()


def is_remote_json_value(value: Any) -> bool:
    """Whether a value crosses JSON transport without coercion or omission."""
    return is_json_value(value)


def is_remote_uplink_item(value: Any) -> bool:
    """A lossless JSON value, or top-level ABSENT (wire item frame without value)."""
    return value is ABSENT or is_json_value(value)


class OwnedValue:
    """Invocation-owned payload: disposed after invocation and reply settlement."""

    is_typert_owned_value = True
    __slots__ = ("value", "_release", "_active")

    def __init__(self, value: Any, release: Callable[[], None]):
        if not callable(release):
            raise TypeError("typert-protocol: release must be callable")
        self.value = value
        self._release = release
        self._active = True

    def close(self) -> None:
        """Non-throwing synchronous release, called at most once."""
        if not self._active:
            return
        self._active = False
        self._release()

    def __enter__(self) -> "OwnedValue":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def typert_owned_value(value: Any, release: Callable[[], None]) -> OwnedValue:
    """Transfer cleanup ownership without adding another resource reference count."""
    return OwnedValue(value, release)


def is_typert_owned_value(value: Any) -> bool:
    """Identify invocation-owned values using the shared structural marker."""
    return (isinstance(value, object) and getattr(value, "is_typert_owned_value", None) is True)


def is_typert_remote_segment(value: Any) -> bool:
    """Whether a generated Remote name can cross the shared RPC carrier unchanged."""
    return (isinstance(value, str) and value not in (".", "..")
            and bool(TYPERT_REMOTE_SEGMENT_PATTERN.match(value)))


def validate_name(subject: str, value: Any) -> None:
    if not is_typert_remote_segment(value):
        raise TypeError(f"typert-protocol: {subject} must contain only RPC endpoint segment characters")


_MARKER_ATTR = "_typert_remote_marker"


def _freeze_marker(method: str, invocation: Dict[str, Any], mode: Optional[str],
                   export_name: Optional[str]) -> Dict[str, Any]:
    marker: Dict[str, Any] = {"method": method, "invocation": dict(invocation)}
    if export_name is not None and export_name != method:
        marker["exportName"] = export_name
    if mode is not None:
        marker["mode"] = mode
    return marker


def _mark(fn: Callable[..., Any], invocation: Dict[str, Any], mode: Optional[str],
          export_name: Optional[str]) -> Callable[..., Any]:
    name = getattr(fn, "__name__", None)
    if not isinstance(name, str) or name.startswith("_"):
        raise TypeError("typert-protocol: Remote decorators require a public instance method with a string name")
    existing = getattr(fn, _MARKER_ATTR, None)
    marker = _freeze_marker(name, invocation, mode, export_name)
    if existing is not None and existing != marker:
        raise RuntimeError(
            f'typert-protocol: Remote method "{name}" has conflicting invocation markers')
    setattr(fn, _MARKER_ATTR, marker)
    return fn


def remote(arg: Any = None) -> Any:
    """Mark a method as a Remote endpoint.

    Forms (mirroring upstream overloads): ``@remote``, ``@remote()``,
    ``@remote("exportName")``, ``@remote({"mode": "stream"})`` — the options
    form must contain exactly ``mode: "stream"``.
    """
    if callable(arg):
        return _mark(arg, {"kind": "direct"}, None, None)
    if isinstance(arg, str):
        validate_name("Remote export name", arg)
        return lambda fn: _mark(fn, {"kind": "direct"}, None, arg)
    if isinstance(arg, dict):
        if arg != {"mode": "stream"}:
            raise TypeError('typert-protocol: Remote options must contain exactly mode: "stream"')
        return lambda fn: _mark(fn, {"kind": "direct"}, "stream", None)
    if arg is not None:
        raise TypeError("typert-protocol: Remote argument must be a method, an export name, or stream options")
    return lambda fn: _mark(fn, {"kind": "direct"}, None, None)


def remote_scope(key: str, export_name: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for a method resolved from one Remote Scope Context key."""
    validate_name("Scope key", key)
    if export_name is not None:
        validate_name("Remote export name", export_name)
    return lambda fn: _mark(fn, {"kind": "context", "context": key}, None, export_name)


def remote_methods(service: Any) -> List[Dict[str, Any]]:
    """Snapshot of Remote markers on a live service's class, declaration order.

    The returned dicts are copies: callers cannot mutate the stored marker.
    """
    klass = service if isinstance(service, type) else type(service)
    collected: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for base in reversed(klass.__mro__):
        for name in vars(base):
            fn = vars(base)[name]
            marker = getattr(fn, _MARKER_ATTR, None)
            if marker is None:
                continue
            current = seen.get(marker["method"])
            if current is not None:
                if current != marker:
                    method = marker["method"]
                    raise RuntimeError(
                        f'typert-protocol: Remote method "{method}" has conflicting invocation markers')
                continue
            seen[marker["method"]] = marker
            collected.append(dict(marker))
    return collected


def bind_typert_remote(service: Any, service_key: str,
                       options: Optional[Dict[str, Any]] = None) -> Any:
    """Bind one visible service field to a service key and Remote namespace."""
    validate_name("service key", service_key)
    options = options or {}
    namespace = options.get("namespace", service_key)
    validate_name("namespace", namespace)
    binding = {"service": service, "serviceKey": service_key, "namespace": namespace}
    from types import MappingProxyType
    return MappingProxyType(binding)


class TypertRemoteService:
    """Base exposing its registered name through the Typert Gateway binding.

    Native adaptation: no Cordis Service base; the owning plugin registers the
    instance under ``service_key`` itself. ``ctx.invocation`` accessor
    semantics land with the B4 agent-loop port (documented deviation).
    """

    def __init__(self, service_key: str, options: Optional[Dict[str, Any]] = None):
        self.name = service_key
        self.typert_remote = bind_typert_remote(self, service_key, options)
