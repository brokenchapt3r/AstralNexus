"""Abstract credential seam: references, record keys, and the provider base.

Port of ``@deepseek-ai/dsh-credentials`` (commit 5badb15). Contract kept:
- Settings/composition carry *references* to secrets (environment-variable
  names); providers own the actual values. Consumers resolve once per
  operation, so a changed credential reaches the next operation without any
  plugin restart, and configuration surfaces describe a reference without
  ever seeing its value.
- ``REF_PATTERN`` is the POSIX shell identifier grammar; ``KEY_SEGMENT_PATTERN``
  is lowercase-hyphenated. The ``/`` between two key segments is exactly what
  keeps a key out of the reference grammar.
- ``credential_ref`` / ``credential_key`` / ``parse_credential_key`` throw on
  malformed input; ``is_credential_ref_name`` / ``is_credential_key_segment``
  answer first for names arriving from elsewhere, so a name outside the
  grammar reads as "not set" rather than as a thrown error.
- Two key spaces, two rules: a reference layers over process environment,
  provider store, and ``.env`` files, and an EMPTY stored value is absent
  everywhere (a blank never masquerades as a configured secret); a record key
  has no layering — presence of the record is the whole fact — and
  ``modify_record`` is the only write path because a correct write depends on
  the current value (a token refresh is read-decide-replace under one lock).
- ``notify_updated`` / ``notify_record_updated`` fan out AFTER the write
  committed, with contained listener failures: every listener runs, a failure
  is logged without changing the committed operation's outcome — except
  ``INVARIANT``-coded failures, which rethrow (the first one) after every
  listener ran. A broken observer can never make a durable change look failed.

Python adaptations (documented):
- ``brandString`` branding becomes ``dshutil.brand.brand_string`` (runtime
  boundary check; static nominal typing lives in the brand port).
- The Cordis event dispatch (``ctx.events.dispatch("emit", …)``) becomes the
  provider's own listener registry (``on(event, listener)`` returning an
  unsubscribe); native listeners are synchronous callables — an awaitable
  return is closed and logged (no event loop is implied by this seam).
- Abstract seam methods raise ``NotImplementedError`` (upstream declares them
  in TypeScript types; concrete providers implement them).
"""
from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from ..dshutil.brand import brand_string

REF_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEY_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def is_credential_ref_name(value: Any) -> bool:
    """Whether ``credential_ref`` would accept this value (non-throwing pre-check)."""
    return isinstance(value, str) and bool(REF_PATTERN.match(value))


def credential_ref(value: Any) -> str:
    """Brand a raw string as a credential reference (POSIX shell identifier)."""
    if not is_credential_ref_name(value):
        raise TypeError(
            f'credential ref "{value}" must match {REF_PATTERN.pattern}')
    return brand_string(value)


def is_credential_key_segment(value: Any) -> bool:
    """Whether ``credential_key`` would accept this value as either segment."""
    return isinstance(value, str) and bool(KEY_SEGMENT_PATTERN.match(value))


def credential_key(scope: Any, ident: Any) -> str:
    """Brand a scope and an id as ``<scope>/<id>`` (one record key)."""
    for segment in (scope, ident):
        if not is_credential_key_segment(segment):
            raise TypeError(
                f'credential key segment "{segment}" must match {KEY_SEGMENT_PATTERN.pattern}')
    return brand_string(f"{scope}/{ident}")


def parse_credential_key(value: Any) -> str:
    """Read half of ``credential_key``: admit a stored ``<scope>/<id>`` string."""
    if not isinstance(value, str):
        raise TypeError(f'credential key "{value}" must be "<scope>/<id>"')
    segments = value.split("/")
    if len(segments) != 2:
        raise TypeError(f'credential key "{value}" must be "<scope>/<id>"')
    return credential_key(segments[0], segments[1])


def credential_key_scope(key: str) -> str:
    """The owning plugin's registered name for one key (orphan detection reads this)."""
    return key[:key.index("/")]


def credential_key_id(key: str) -> str:
    """The owning plugin's own addressing unit for one key."""
    return key[key.index("/") + 1:]


def contained_fan_out(event: str, args: tuple, listeners: List[Callable[..., Any]],
                      logger: logging.Logger, prefix: str = "credentials") -> None:
    """Contained dispatch shared by the credential and authorization seams.

    Every listener runs; a failure is logged without changing the committed
    operation's outcome — except ``INVARIANT``-coded failures, which rethrow
    (the first one) after every listener ran. An awaitable return is closed
    and warned about: native listeners on these seams are synchronous.
    """
    invariant_failure: Optional[BaseException] = None

    def warn(subject: Any, error: BaseException) -> None:
        logger.warning('%s: a %s listener for "%s" failed', prefix, event, subject)
        logger.warning("%s listener error: %r", prefix, error)

    for listener in list(listeners):
        try:
            returned = listener(*args)
            if inspect.isawaitable(returned):
                close = getattr(returned, "close", None)
                if callable(close):
                    close()
                warn(args[0] if args else None,
                     RuntimeError(f"native {prefix} listeners must be synchronous"))
        except Exception as error:  # contained: a broken observer never fails the commit
            if getattr(error, "code", None) == "INVARIANT":
                if invariant_failure is None:
                    invariant_failure = error
                continue
            warn(args[0] if args else None, error)
    if invariant_failure is not None:
        raise invariant_failure


class CredentialProvider:
    """Abstract credential service over the two key spaces (service key ``credentials``)."""

    service_name = "credentials"

    def __init__(self, ctx: Any = None, logger: Optional[logging.Logger] = None):
        self.ctx = ctx
        self.logger = logger or logging.getLogger("astralnexus.credentials")
        self._listeners: Dict[str, List[Callable[..., Any]]] = {}

    # ------------------------------------------------------------- events

    def on(self, event: str, listener: Callable[..., Any]) -> Callable[[], None]:
        """Subscribe one synchronous listener; returns an idempotent unsubscribe."""
        if not callable(listener):
            raise TypeError("credentials: listener must be callable")
        bucket = self._listeners.setdefault(event, [])
        bucket.append(listener)

        def unwatch() -> None:
            try:
                bucket.remove(listener)
            except ValueError:
                pass  # idempotent

        return unwatch

    def notify_updated(self, ref: str) -> None:
        """Fan ``credentials/reference-updated`` out after a committed reference write."""
        self.fan_out("credentials/reference-updated", ref)

    def notify_record_updated(self, key: str) -> None:
        """Fan ``credentials/record-updated`` out after a committed record write."""
        self.fan_out("credentials/record-updated", key)

    def fan_out(self, event: str, *args: Any) -> None:
        """Contained dispatch: every listener runs; failures log; INVARIANT rethrows last."""
        contained_fan_out(event, args, self._listeners.get(event, []), self.logger)

    # ------------------------------------------------- abstract seam surface

    def resolve(self, ref: str) -> Optional[Dict[str, str]]:
        """``{value, source}`` for one reference, or None when absent everywhere."""
        raise NotImplementedError

    def describe(self, ref: str) -> Dict[str, Any]:
        """``{configured, source?, writable}`` — never the value."""
        raise NotImplementedError

    def set(self, ref: str, value: str) -> None:
        """Store one reference value (empty values are refused; use unset)."""
        raise NotImplementedError

    def unset(self, ref: str) -> None:
        """Remove one stored reference."""
        raise NotImplementedError

    def read_record(self, key: str) -> Optional[Dict[str, Any]]:
        """The stored record for one key, or None."""
        raise NotImplementedError

    def describe_record(self, key: str) -> Dict[str, Any]:
        """``{configured, kind?, writable}`` for one record key — never the payload."""
        raise NotImplementedError

    def list_records(self) -> List[Dict[str, Any]]:
        """``[{key, kind}]`` over every stored record."""
        raise NotImplementedError

    def modify_record(self, key: str, mutate: Callable[[Optional[Dict[str, Any]]],
                                                       Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        """The only record write path: read-decide-replace under one lock.

        ``mutate(current)`` returns the next record, or None to keep the
        current value untouched (the caller's decision may be a no-op).
        """
        raise NotImplementedError

    def delete_record(self, key: str) -> None:
        """Remove one stored record (absent record is a no-op)."""
        raise NotImplementedError
