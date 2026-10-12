"""Authorization seam: a registry of credential-obtaining flows, one attempt
at a time per key.

Port of ``@deepseek-ai/dsh-authorization`` (commit 5badb15). Contract kept:

- The seam owns the conversation and the lifecycle; it never owns the
  protocol. A plugin that knows how to obtain its own credential registers a
  flow keyed by the ``CredentialKey`` that flow writes, and the flow talks to
  whatever surface started it through one neutral vocabulary of notices and
  prompts (``session.notify`` / ``session.prompt``). A second protocol arrives
  as another flow, not another seam.
- One flow per key (``DUPLICATE_FLOW``); one attempt per key
  (``ALREADY_IN_FLIGHT`` — a second caller is refused rather than joined).
- ``begin`` reports how the attempt ended: ``authorized`` once the flow's
  record is committed *during this attempt and observed* through the
  credentials event surface, ``cancelled`` when the human declined
  (``AuthorizationDeclinedError``, the ONLY rejection that means "no") or the
  caller withdrew; ``NO_FLOW`` / ``UNKNOWN_METHOD`` / ``NOT_COMMITTED``
  (resolved without committing, or deleted the record instead) are errors.
- ``cancel(key)`` aborts the running attempt unless it is already committing
  (a committing write is never interrupted mid-flight); separate from the
  request's own signal because a transport answers a Cancel button on a
  second call with no handle on the first one's signal.
- ``authorization/settled`` fans out after the key is released, with the same
  contained-listener contract as the credentials seam (INVARIANT rethrows
  last): a broken watcher can never turn a settled attempt into a failure.

Python adaptations (documented):
- Cordis ``Service`` (``ctx.authorization``, ``static inject ["credentials"]``)
  becomes a host-provided ``AuthorizationRegistry`` constructed with a
  credentials getter; because the native credentials plugin is user opt-in
  (desired=disabled), a missing provider fails loud at ``begin`` with
  ``NO_CREDENTIALS`` instead of being guaranteed by composition.
- ``AbortController``/``AbortSignal`` become ``CancelToken`` (threading.Event
  based, first abort reason wins, contained one-shot listeners).
- ``async begin`` + ``Promise.race([run, withdrawn])`` become a worker daemon
  thread for ``flow.run`` raced by the calling thread against the token: an
  external cancel still never waits for flow cooperation, and a reaper thread
  logs a withdrawn flow's after-the-fact failure (upstream
  ``running.catch(debug)``).
- ``HarnessError`` base (dsh-llm, not yet ported) becomes ``RuntimeError`` +
  a structural ``.code`` attribute — the same discrimination style used for
  remote errors in the typert protocol port.
- The ``authorization/settled`` dispatch reuses
  ``credentials.base.contained_fan_out`` (shared contained-listener contract).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from .credentials.base import contained_fan_out, parse_credential_key

RACE_POLL_SECONDS = 0.02


class AuthorizationError(RuntimeError):
    """Stable error taxonomy for authorization failures (``.code`` is the taxon)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class AuthorizationDeclinedError(AuthorizationError):
    """The human said no — the ONLY rejection that may carry code DECLINED.

    A prompt withdrawn by its own signal (a flow retiring the losing question
    of a race) must reject with something else, or a later genuine failure
    would be misread as a decline.
    """

    def __init__(self, message: str = "the authorization prompt was declined"):
        super().__init__(message, "DECLINED")


class CancelToken:
    """Native ``AbortSignal`` + ``AbortController`` in one handle.

    ``aborted`` flips once; the first ``reason`` wins; ``on_abort`` listeners
    fire exactly once each and are contained (a broken withdraw listener must
    never block the abort itself).
    """

    __slots__ = ("_event", "_listeners", "_lock", "reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._listeners: List[Callable[[], Any]] = []
        self._lock = threading.Lock()
        self.reason: Optional[BaseException] = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def abort(self, reason: Optional[BaseException] = None) -> None:
        with self._lock:
            if self._event.is_set():
                return  # idempotent; the first reason wins
            self.reason = reason
            listeners = list(self._listeners)
            self._listeners.clear()
            self._event.set()
        for listener in listeners:
            try:
                listener()
            except Exception:  # contained: abort must complete
                logging.getLogger("astralnexus.authorization").warning(
                    "authorization: an abort listener failed", exc_info=True)

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def throw_if_aborted(self) -> None:
        if not self._event.is_set():
            return
        if isinstance(self.reason, BaseException):
            raise self.reason
        raise AuthorizationError("the authorization attempt was cancelled", "CANCELLED")

    def on_abort(self, listener: Callable[[], Any]) -> Callable[[], None]:
        """Register a one-shot abort listener; returns an unsubscribe closure."""
        if not callable(listener):
            raise TypeError("authorization: abort listener must be callable")
        with self._lock:
            if not self._event.is_set():
                self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass  # idempotent

        return unsubscribe


class AuthorizationFlow:
    """One validated registered flow: the key it writes and how it converses."""

    __slots__ = ("key", "label", "methods", "run")

    def __init__(self, key: str, label: str, methods: List[Dict[str, str]],
                 run: Callable[[Dict[str, Any]], Any]):
        self.key = key
        self.label = label
        self.methods = methods
        self.run = run


def _validate_flow(flow: Any) -> AuthorizationFlow:
    if not isinstance(flow, dict):
        raise TypeError("authorization: a flow must be a mapping with key/label/methods/run")
    key = parse_credential_key(flow.get("key"))
    label = flow.get("label")
    if not isinstance(label, str) or not label:
        raise TypeError("authorization: flow label must be a non-empty string")
    raw_methods = flow.get("methods")
    if not isinstance(raw_methods, list) or not raw_methods:
        raise TypeError("authorization: a flow must offer at least one method")
    methods: List[Dict[str, str]] = []
    for raw in raw_methods:
        if not isinstance(raw, dict):
            raise TypeError("authorization: each flow method must be a mapping")
        method_id = raw.get("id")
        if not isinstance(method_id, str) or not method_id:
            raise TypeError("authorization: each flow method needs a non-empty string id")
        method_label = raw.get("label")
        if method_label is not None and (not isinstance(method_label, str) or not method_label):
            raise TypeError("authorization: a method label must be a non-empty string")
        methods.append({"id": method_id} | ({"label": method_label} if method_label else {}))
    if len({method["id"] for method in methods}) != len(methods):
        raise TypeError("authorization: flow method ids must be unique")
    run = flow.get("run")
    if not callable(run):
        raise TypeError("authorization: flow.run must be callable")
    return AuthorizationFlow(key, label, methods, run)


class _Running:
    __slots__ = ("token", "committing")

    def __init__(self, token: CancelToken):
        self.token = token
        self.committing = False


class AuthorizationRegistry:
    """``ctx.authorization``: credential-obtaining flows, one attempt per key."""

    service_name = "authorization"

    def __init__(self, credentials: Optional[Callable[[], Any]] = None,
                 logger: Optional[logging.Logger] = None):
        # ``credentials`` is a getter so the registry survives the opt-in
        # credentials plugin's enable/disable lifecycle (upstream composition
        # guarantees the service; native resolves it per attempt).
        self._credentials_of = credentials if credentials is not None else (lambda: None)
        self.logger = logger or logging.getLogger("astralnexus.authorization")
        self._flows: Dict[str, AuthorizationFlow] = {}
        self._running: Dict[str, _Running] = {}
        self._lock = threading.RLock()
        self._listeners: Dict[str, List[Callable[..., Any]]] = {}

    # ------------------------------------------------------------ events

    def on(self, event: str, listener: Callable[..., Any]) -> Callable[[], None]:
        """Subscribe one synchronous listener; returns an idempotent unsubscribe."""
        if not callable(listener):
            raise TypeError("authorization: listener must be callable")
        bucket = self._listeners.setdefault(event, [])
        bucket.append(listener)

        def unwatch() -> None:
            try:
                bucket.remove(listener)
            except ValueError:
                pass  # idempotent

        return unwatch

    def _credentials(self) -> Any:
        provider = self._credentials_of()
        if provider is None:
            raise AuthorizationError(
                "the credentials service is not available; enable the credentials-local plugin",
                "NO_CREDENTIALS")
        return provider

    # ------------------------------------------------------- registration

    def register_flow(self, flow: Any) -> Callable[[], None]:
        """Offer a way to obtain one credential. One flow per key.

        Returns the disposer that withdraws this flow and cancels its attempt;
        raises ``DUPLICATE_FLOW`` when the key is already claimed (two plugins
        writing one key would leave whichever ran last a payload the other
        cannot parse).
        """
        validated = _validate_flow(flow)
        with self._lock:
            if validated.key in self._flows:
                raise AuthorizationError(
                    f'an authorization flow for "{validated.key}" is already registered',
                    "DUPLICATE_FLOW")
            self._flows[validated.key] = validated

        def dispose() -> None:
            with self._lock:
                if self._flows.get(validated.key) is validated:
                    del self._flows[validated.key]
            self.cancel(validated.key)

        return dispose

    def list(self) -> List[Dict[str, Any]]:
        """Every registered flow, in registration order."""
        with self._lock:
            return [self._entry(flow) for flow in self._flows.values()]

    def describe(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            flow = self._flows.get(key)
            return None if flow is None else self._entry(flow)

    def _entry(self, flow: AuthorizationFlow) -> Dict[str, Any]:
        return {
            "key": flow.key,
            "label": flow.label,
            "methods": [dict(method) for method in flow.methods],
            "in_flight": flow.key in self._running,
        }

    def cancel(self, key: str) -> bool:
        """Withdraw the attempt running for a key, if any.

        Never interrupts an attempt that is already committing. Returns
        whether an abort was issued (native metadata; upstream returns void).
        """
        running = self._running.get(key)
        if running is not None and not running.committing:
            running.token.abort()
            return True
        return False

    # ------------------------------------------------------------- attempts

    def begin(self, request: Dict[str, Any]) -> Dict[str, str]:
        """Run one attempt to authorize a key, and report how it ended."""
        raw_key = request.get("key")
        key: Optional[str] = raw_key if isinstance(raw_key, str) else None
        with self._lock:
            flow = self._flows.get(key) if key is not None else None
        if flow is None or key is None:
            raise AuthorizationError(f'no authorization flow is registered for "{raw_key}"',
                                     "NO_FLOW")
        method = request.get("method")
        if method is None:
            method = flow.methods[0]["id"]  # an explicit "" is UNKNOWN_METHOD, not a default
        if not any(candidate["id"] == method for candidate in flow.methods):
            raise AuthorizationError(
                f'authorization flow for "{key}" offers no method "{method}"', "UNKNOWN_METHOD")
        if key in self._running:
            raise AuthorizationError(
                f'an authorization attempt for "{key}" is already running', "ALREADY_IN_FLIGHT")
        signal: Optional[CancelToken] = request.get("signal")
        if signal is not None and signal.aborted:
            return {"status": "cancelled"}
        interaction = request.get("interaction")
        if (not isinstance(interaction, dict)
                or not callable(interaction.get("notify"))
                or not callable(interaction.get("prompt"))):
            raise TypeError(
                "authorization: request.interaction must provide notify and prompt callables")
        provider = self._credentials()  # fail loud before any state is taken

        token = CancelToken()

        def withdraw() -> None:
            running = self._running.get(key)
            if running is not None and not running.committing:
                token.abort(signal.reason if signal is not None else None)

        unsubscribe = signal.on_abort(withdraw) if signal is not None else None
        self._running[key] = _Running(token)
        settlement = "failed"
        try:
            outcome = self._attempt(flow, method, token, interaction, provider)
            settlement = outcome["status"]
            return outcome
        finally:
            if unsubscribe is not None:
                unsubscribe()
            self._running.pop(key, None)
            self._settle(key, settlement)

    def _settle(self, key: str, settlement: str) -> None:
        """Fan ``authorization/settled`` out after the key is released."""
        contained_fan_out("authorization/settled", (key, settlement),
                          self._listeners.get("authorization/settled", []),
                          self.logger, prefix="authorization")

    def _attempt(self, flow: AuthorizationFlow, method: str, token: CancelToken,
                 interaction: Dict[str, Any], provider: Any) -> Dict[str, str]:
        observed = {"declined": False, "committed": False}

        def on_record(updated_key: str) -> None:
            if updated_key == flow.key:
                observed["committed"] = True

        unwatch = provider.on("credentials/record-updated", on_record)

        def commit(record: Any) -> None:
            token.throw_if_aborted()
            running = self._running.get(flow.key)
            if running is None or running.token is not token:
                raise AuthorizationError("authorization attempt is no longer active", "CANCELLED")
            running.committing = True
            provider.modify_record(flow.key, lambda current: record)

        def notify(notice: Any) -> None:
            try:
                interaction["notify"](notice)
            except Exception as error:  # a broken surface never fails the flow
                self.logger.warning("authorization: the interaction surface failed to render a notice")
                self.logger.warning("authorization notice error: %r", error)

        def prompt(question: Any) -> Any:
            try:
                return interaction["prompt"](question)
            except AuthorizationDeclinedError:
                observed["declined"] = True
                raise

        session = {"method": method, "signal": token,
                   "commit": commit, "notify": notify, "prompt": prompt}
        box: Dict[str, Any] = {}

        def runner() -> None:
            try:
                box["result"] = flow.run(session)
            except BaseException as error:  # noqa: BLE001 - raced below
                box["error"] = error

        thread = threading.Thread(target=runner, daemon=True,
                                  name=f"authorization-{flow.key}")
        thread.start()
        try:
            withdrawn = False
            while True:
                thread.join(RACE_POLL_SECONDS)
                if token.aborted:
                    # Upstream's Promise.race settles "withdrawn" whenever the
                    # abort precedes the flow's own completion. The poll loop
                    # reproduces that ordering: a cancel wins unless the flow
                    # already finished AND its commit already landed (then the
                    # attempt's outcome stands, as an already-settled race).
                    withdrawn = thread.is_alive() or not observed["committed"]
                    break
                if not thread.is_alive():
                    break
            if withdrawn:
                self._reap_withdrawn(thread, box)
                return {"status": "cancelled"}
            if "error" in box:
                error = box["error"]
                if token.aborted or observed["declined"]:
                    return {"status": "cancelled"}
                raise error
        finally:
            unwatch()
        if not observed["committed"]:
            raise AuthorizationError(
                f'authorization flow for "{flow.key}" resolved without committing '
                "a credential record in this attempt", "NOT_COMMITTED")
        if not provider.describe_record(flow.key).get("configured"):
            raise AuthorizationError(
                f'authorization flow for "{flow.key}" deleted its credential record '
                "instead of committing one", "NOT_COMMITTED")
        return {"status": "authorized"}

    def _reap_withdrawn(self, thread: threading.Thread, box: Dict[str, Any]) -> None:
        """Log a withdrawn flow's after-the-fact failure (upstream running.catch)."""

        def reap() -> None:
            thread.join()
            error = box.get("error")
            if error is not None:
                self.logger.debug(
                    "authorization: withdrawn flow failed after the fact: %r", error)

        threading.Thread(target=reap, daemon=True,
                         name=f"authorization-reap").start()
