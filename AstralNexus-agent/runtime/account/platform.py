"""Platform PKCE account provider; browser approval never bypasses local cancellation.

Port of ``@deepseek-ai/dsh-deepseek-account-platform`` (commit 5badb15,
lib/types/index.js — PlatformAccount). Contract kept:

- One stored grant record ``deepseek-account-platform/default`` (schema
  ``{version: 1, token, issuer}``) plus a persistent device id record
  ``deepseek-account-platform/device``. Boot discards a stored grant whose
  issuer no longer matches the configured origin (issuer-mismatch).
- The authorization flow (key/label/method ``browser``) is registered with the
  authorization seam; the attempt state machine runs
  initializing → waiting-browser → exchanging → committing →
  succeeded/cancelled/failed/expired, and ``authorizeUrl`` lives ONLY in the
  waiting-browser view (every update strips it).
- PKCE: 32-byte base64url verifier/state, S256 challenge; the browser
  callback route is exact ``/oauth/callback``, GET only, single ``code`` and
  ``state`` parameters, and ``state`` is compared length-first with a
  constant-time equality. A late/duplicate/aborted callback gets 410; a
  malformed one gets 400; both no-store.
- The callback response is HELD until the attempt settles: 302 to the
  validated completion URL (login_source appended) on success, 204 otherwise,
  and a CSP-nonced self-closing HTML page for a failed ``web`` login source.
- Expiry: the attempt deadline is ``min(attemptTimeout, init expires_in)``;
  an expired deadline surfaces as code ``expired`` and phase ``expired``. An
  abort after ``authorize_id`` is known tells Platform to discard the
  authorization (``auth_cancel``, background, best-effort).
- Credential reads are generation-scoped (``invalidateDetails`` on record
  updates): a cached identity can never answer for a changed credential; a
  failed profile read falls back to the cache ONLY when the token matches.
- ``resolve_token`` releases the grant only to the configured inference
  origin, never for ``dsh_mock_`` tokens, and never for a loopback issuer
  against the production inference origin.
- Sign-out / expiry: single-flight removal, record deleted BEFORE the
  background bounded logout retry loop (remote failures stay in the
  background), ``deepseek-account/session-expired`` (expiry only) and
  ``deepseek-account/signed-out`` events, committing attempts are never
  interrupted.

Python adaptations (documented):
- ``ctx.get("webServer").register({kind:"exact", path})`` becomes an
  ``OAuthCallbackChannel`` the host mounts once at ``/oauth/callback``; the
  upstream "hold the ServerResponse" becomes blocking the Flask request
  thread on a ``_PendingCallback`` until the attempt writes the response
  spec — the synchronous server model's exact equivalent.
- ``authorization.begin`` is synchronous native; ``start_sign_in`` runs it on
  a worker thread and ``attempt["done"]`` is the join Event (upstream's
  promise chain runs in that thread's ``finally``).
- ``Promise.withResolvers`` code box → ``_CodeBox`` (Event + first-set wins);
  ``AbortSignal.any``/``setTimeout`` deadline → CancelToken + threading.Timer
  with the same recompute-after-init rule; a request in flight cannot be
  aborted (urllib), so cancellation is honored at checkpoints and the wire
  timeout is the bound.
- ``watch`` async generator → ``on_changed`` subscription + the host HTTP
  projection (the workbench UI polls; push arrives with B6).
- ``installAccountTaskCancellation`` is NOT wired (agents scope events arrive
  with B4 — see runtime/account/base.py); the events this provider emits
  (``signed-out``) are the same vocabulary the B4 wiring will consume.
- zod/schemastery → issue-collecting validators (protocol.py/details.py) and
  ``validate_config`` below; grant/device parse failures raise
  ``PlatformAuthError("storage")``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import platform as os_platform
import secrets
import sys
import threading
import time
import uuid as uuid_module
from typing import Any, Callable, Dict, List, NoReturn, Optional
from urllib.parse import urlencode, urlparse

from ..authorization import AuthorizationError, CancelToken
from ..credentials.base import contained_fan_out, credential_key
from .base import (
    DeepSeekAccount, merge_platform_cookies, platform_client_headers, platform_wire_locale)
from .details import profile, read_account_detail, read_unnotified_bonuses, send_bonus_notified
from .logout import revoke_account
from .protocol import (
    AccountUnauthorizedError, PlatformAuthError, _is_http_url, _origin_of, browser_url,
    login_origin, parse_device_payload, parse_exchange, parse_grant_payload,
    parse_initialization, platform_headers, platform_origin, log_payload_rejection,
    request_platform)

KEY = credential_key("deepseek-account-platform", "default")
DEVICE = credential_key("deepseek-account-platform", "device")
LOG = logging.getLogger("astralnexus.account")

ACTIVE_PHASES = ("initializing", "waiting-browser", "exchanging", "committing")

CONFIG_DEFAULTS: Dict[str, Any] = {
    "platform_origin": "https://platform.deepseek.com",
    "desktop_platform": None,
    "embedded_page_dist": "",
    "inference_origin": "https://api.deepseek.com",
    "allow_loopback_http": False,
    "rewrite_browser_origin": False,
    "request_headers": {},
    "account_request_headers": {},
    "request_timeout_ms": 30000,
    "balance_timeout_ms": 30000,
    "logout_max_retries": 5,
    "logout_retry_delay_ms": 1000,
    "attempt_timeout_ms": 600000,
}
NUMBER_BOUNDS = {
    "request_timeout_ms": (1, 120000),
    "balance_timeout_ms": (1, 120000),
    "logout_max_retries": (0, 5),
    "logout_retry_delay_ms": (1, 60000),
    "attempt_timeout_ms": (1, 3600000),
}


def validate_config(config: Any) -> Dict[str, Any]:
    """Validated deployment choices (upstream schemastery Config)."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("account: config must be a mapping")
    resolved = dict(CONFIG_DEFAULTS)
    for name, value in config.items():
        if name not in CONFIG_DEFAULTS:
            raise ValueError(f"account: config has unknown option {name!r}")
        resolved[name] = value
    for name in ("platform_origin", "inference_origin", "embedded_page_dist"):
        if not isinstance(resolved[name], str):
            raise ValueError(f"account: config {name} must be a string")
    if resolved["desktop_platform"] not in (None, "darwin", "win32"):
        raise ValueError("account: config desktop_platform must be 'darwin', 'win32' or null")
    for name in ("allow_loopback_http", "rewrite_browser_origin"):
        if not isinstance(resolved[name], bool):
            raise ValueError(f"account: config {name} must be a boolean")
    for name in ("request_headers", "account_request_headers"):
        headers = resolved[name]
        if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError(f"account: config {name} must be a string mapping")
    for name, (low, high) in NUMBER_BOUNDS.items():
        value = resolved[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
            raise ValueError(f"account: config {name} must be a number in [{low}, {high}]")
        resolved[name] = value
    if resolved["logout_max_retries"] != int(resolved["logout_max_retries"]):
        raise ValueError("account: config logout_max_retries must be an integer")
    return resolved


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _grant_or(payload: Any, code: str) -> Dict[str, Any]:
    """Parse a grant payload or fail with the given code (never both None)."""
    parsed, issues = parse_grant_payload(payload)
    if issues or parsed is None:
        raise PlatformAuthError(code)
    return parsed


def _device_or(payload: Any) -> Dict[str, Any]:
    parsed, issues = parse_device_payload(payload)
    if issues or parsed is None:
        raise PlatformAuthError("storage")
    return parsed


def _initialization_or(payload: Any) -> Dict[str, Any]:
    parsed, issues = parse_initialization(payload)
    if issues or parsed is None:
        log_payload_rejection("auth_init", issues)
        raise PlatformAuthError("protocol")
    return parsed


def _exchange_or(payload: Any) -> Dict[str, Any]:
    parsed, issues = parse_exchange(payload)
    if issues or parsed is None:
        log_payload_rejection("auth_exchange", issues)
        raise PlatformAuthError("protocol")
    return parsed


class _CodeBox:
    """One-shot value/error box (upstream ``Promise.withResolvers``)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Optional[str] = None
        self._error: Optional[BaseException] = None

    def resolve(self, value: str) -> None:
        if self._event.is_set():
            return  # first settlement wins
        self._value = value
        self._event.set()

    def reject(self, error: BaseException) -> None:
        if self._event.is_set():
            return
        self._error = error
        self._event.set()

    def wait(self, timeout: Optional[float] = None) -> str:
        if not self._event.wait(timeout):
            raise PlatformAuthError("expired")  # defensive; the deadline timer aborts first
        if self._error is not None:
            raise self._error
        assert self._value is not None
        return self._value


class _PendingCallback:
    """A held browser callback response (upstream: the un-ended ServerResponse)."""

    __slots__ = ("_event", "_spec")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._spec = (204, {"cache-control": "no-store"}, b"")

    def complete(self, spec: tuple) -> None:
        self._spec = spec
        self._event.set()

    def wait(self, timeout: Optional[float] = None) -> tuple:
        self._event.wait(timeout)
        return self._spec


class OAuthCallbackChannel:
    """Host-side exact-route channel for ``/oauth/callback``.

    The host mounts ONE route and forwards here; the account provider
    registers the live handler per attempt and disposes it when the attempt
    settles (upstream ``webServer.register({kind: "exact", …})``). With no
    handler mounted the channel answers 404 no-store.
    """

    def __init__(self) -> None:
        self._handler: Optional[Callable[[Dict[str, Any]], tuple]] = None
        self._lock = threading.Lock()

    def register(self, handler: Callable[[Dict[str, Any]], tuple]) -> Callable[[], None]:
        if not callable(handler):
            raise TypeError("account: callback handler must be callable")
        with self._lock:
            self._handler = handler

        def dispose() -> None:
            with self._lock:
                if self._handler is handler:
                    self._handler = None

        return dispose

    def dispatch(self, method: str, path: str, query: List[tuple]) -> tuple:
        with self._lock:
            handler = self._handler
        if handler is None:
            return (404, {"cache-control": "no-store"}, b"")
        return handler({"method": method, "path": path, "query": list(query)})


class PlatformAccount(DeepSeekAccount):
    """The platform implementation owns login state and its opaque stored grant."""

    def __init__(self, credentials: Callable[[], Any], authorization: Any,
                 callback_channel: Optional[OAuthCallbackChannel] = None,
                 config: Optional[Dict[str, Any]] = None,
                 logger: Optional[logging.Logger] = None):
        super().__init__(ctx=None)
        self.logger = logger or LOG
        self._credentials_of = credentials
        self._authorization = authorization
        self._callback_channel = callback_channel
        resolved = validate_config(config)
        self.origin = platform_origin(resolved["platform_origin"], resolved["allow_loopback_http"])
        inference = urlparse(resolved["inference_origin"])
        if (inference.scheme not in ("http", "https") or inference.username or inference.password
                or inference.path not in ("", "/") or inference.query or inference.fragment
                or not inference.netloc):
            raise ValueError("account: inferenceOrigin must be an HTTP(S) origin without "
                             "credentials, path, query or fragment")
        self.inference_origin = _origin_of(inference)
        self.embedded_page_dist = resolved["embedded_page_dist"]
        self.rewrite_browser_origin = resolved["rewrite_browser_origin"]
        self.platform = resolved["desktop_platform"]
        self.request_headers = platform_headers(resolved["request_headers"])
        account_headers = platform_headers(resolved["account_request_headers"])
        self.account_request_headers = {**self.request_headers, **account_headers}
        if account_headers.get("cookie") is not None:
            self.account_request_headers["cookie"] = merge_platform_cookies(
                self.request_headers.get("cookie", ""), account_headers["cookie"])
        self._request_timeout = float(resolved["request_timeout_ms"])
        self._balance_timeout = float(resolved["balance_timeout_ms"])
        self._attempt_timeout = float(resolved["attempt_timeout_ms"])
        self._logout_policy = {"max_retries": int(resolved["logout_max_retries"]),
                               "delay_ms": float(resolved["logout_retry_delay_ms"]),
                               "request_timeout_ms": self._request_timeout}
        self._logout_lifetime = CancelToken()
        self._revocations: List[threading.Thread] = []
        self._attempt: Optional[Dict[str, Any]] = None
        self._listeners: Dict[str, List[Callable[..., Any]]] = {}
        self._change_listeners: List[Callable[[], Any]] = []
        self._last_profile: Optional[Dict[str, Any]] = None
        self._details_generation = 0
        self._closed = False
        self._removing: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        self._unwatch_records: Optional[Callable[[], None]] = None
        self._dispose_flow = authorization.register_flow({
            "key": KEY,
            "label": "DeepSeek",
            "methods": [{"id": "browser", "label": "DeepSeek"}],
            "run": self._flow_run,
        })

    # ------------------------------------------------------------ lifecycle

    def _credentials(self) -> Any:
        provider = self._credentials_of()
        if provider is None:
            raise PlatformAuthError("storage")  # the seam requires the store
        return provider

    def start(self) -> None:
        """Service.init equivalent: discard a grant whose issuer moved."""
        provider = self._credentials()
        self._unwatch_records = provider.on("credentials/record-updated", self._on_record_updated)
        record = provider.read_record(KEY)
        if record is None:
            return
        if record.get("kind") != "grant":
            raise PlatformAuthError("storage")
        parsed = _grant_or(record.get("payload"), "storage")
        if parsed["issuer"] == self.origin:
            return
        provider.delete_record(KEY)
        self.logger.info("[deepseek-account] stored grant discarded reason=issuer-mismatch")

    def close(self) -> None:
        """The upstream ctx.effect disposer: bounded, ordered shutdown."""
        self._closed = True
        self._logout_lifetime.abort()
        self._invalidate_details()
        attempt = self._attempt
        if attempt is not None:
            if attempt["view"]["phase"] != "committing":
                attempt["token"].abort()
            attempt["done"].wait()
        if self._removing is not None:
            self._removing["event"].wait()
        for thread in list(self._revocations):
            thread.join()
        self._dispose_flow()
        if self._unwatch_records is not None:
            self._unwatch_records()
            self._unwatch_records = None
        self._changed()

    def is_closed(self) -> bool:
        return self._closed

    # --------------------------------------------------------------- events

    def on(self, event: str, listener: Callable[..., Any]) -> Callable[[], None]:
        """Subscribe to ``deepseek-account/session-expired`` / ``signed-out``."""
        if not callable(listener):
            raise TypeError("account: listener must be callable")
        bucket = self._listeners.setdefault(event, [])
        bucket.append(listener)

        def unwatch() -> None:
            try:
                bucket.remove(listener)
            except ValueError:
                pass

        return unwatch

    def on_changed(self, listener: Callable[[], Any]) -> Callable[[], None]:
        """State-change subscription (upstream ``watch`` generator, documented)."""
        if not callable(listener):
            raise TypeError("account: change listener must be callable")
        self._change_listeners.append(listener)

        def unwatch() -> None:
            try:
                self._change_listeners.remove(listener)
            except ValueError:
                pass

        return unwatch

    def _emit(self, event: str) -> None:
        contained_fan_out(event, (), self._listeners.get(event, []), self.logger,
                          prefix="account")

    def _changed(self) -> None:
        for listener in list(self._change_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - a broken watcher never fails state
                self.logger.warning("account: a change listener failed", exc_info=True)

    def _on_record_updated(self, key: str) -> None:
        if key != KEY:
            return
        self._invalidate_details()
        self._changed()

    def _invalidate_details(self) -> None:
        self._last_profile = None
        with self._lock:
            self._details_generation += 1

    def _details_stale(self, generation: int) -> bool:
        return self._details_generation != generation

    # ----------------------------------------------------------------- state

    def get_state(self) -> Dict[str, Any]:
        provider = self._credentials()
        record = provider.read_record(KEY)
        if record is not None:
            if record.get("kind") != "grant":
                raise PlatformAuthError("storage")
            _, issues = parse_grant_payload(record.get("payload"))
            if issues:
                raise PlatformAuthError("storage")
        attempt = self._attempt
        view = dict(attempt["view"]) if attempt is not None else None
        if record is None and view is not None and view.get("phase") == "succeeded":
            view = None  # a stale success view never outlives the sign-out
        return {
            "status": "signed-out" if record is None else "credential-stored",
            "attempt": view,
            "links": {"usageUrl": f"{self.origin}/usage", "topUpUrl": f"{self.origin}/top_up"},
        }

    # --------------------------------------------------------------- details

    def get_profile(self, client: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        generation = self._details_generation
        stored = self._read_current_grant(generation)
        if stored is None or self._details_stale(generation):
            return None
        result = self._get_detail("profile", self._detail_headers(client),
                                  {"generation": generation, "stored": stored})
        if self._details_stale(generation):
            return None
        if result is not None:
            self._cache_profile(stored["token"], result)
        if result is None or result.get("status") != "failed":
            return result
        cached = self._last_profile
        if cached is not None and cached["token"] == stored["token"]:
            return cached["profile"]
        return result

    def get_balance(self, client: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._get_detail("balance", self._detail_headers(client))

    def get_unnotified_bonuses(self, client: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        generation = self._details_generation
        headers = self._detail_headers(client)
        stored = self._read_current_grant(generation)
        if stored is None:
            return None
        account_id = self._current_account_id(generation, stored, headers)
        if account_id is None:
            return None
        try:
            bonuses = read_unnotified_bonuses(self.origin, stored["token"], headers,
                                              self._request_timeout / 1000.0)
            return {"accountId": account_id, "bonuses": bonuses}
        except AccountUnauthorizedError:
            if not self._details_stale(generation):
                self._expire_credential(stored["token"], generation)
            return None

    def ack_bonus_notified(self, account_id: str, order_id: str,
                           client: Dict[str, Any]) -> bool:
        generation = self._details_generation
        headers = self._detail_headers(client)
        stored = self._read_current_grant(generation)
        if stored is None:
            return False
        current = self._current_account_id(generation, stored, headers)
        if current is None or current != account_id:
            return False
        try:
            send_bonus_notified(self.origin, stored["token"], order_id, headers,
                                self._request_timeout / 1000.0)
            return True
        except AccountUnauthorizedError:
            if not self._details_stale(generation):
                self._expire_credential(stored["token"], generation)
            return False

    def _current_account_id(self, generation: int, stored: Dict[str, Any],
                            headers: Dict[str, str]) -> Optional[str]:
        cached = self._last_profile
        if cached is not None and cached["token"] == stored["token"]:
            return self._identity_of(cached["profile"])
        details = self._get_detail("profile", headers,
                                   {"generation": generation, "stored": stored})
        if details is None:
            return None
        if details.get("status") == "failed":
            raise PlatformAuthError("network")
        self._cache_profile(stored["token"], details)
        return self._identity_of(details)

    def _cache_profile(self, token: str, prof: Dict[str, Any]) -> None:
        if prof.get("status") != "ready":
            return
        previous = None
        if self._last_profile is not None:
            previous = self._last_profile["profile"]["value"].get("id") or None
        self._last_profile = {"token": token, "profile": prof}
        if previous != (prof["value"].get("id") or None):
            self._changed()  # a first-seen or changed id notifies; repeats stay silent

    def _identity_of(self, prof: Dict[str, Any]) -> str:
        if prof["value"].get("id") is None:
            raise PlatformAuthError("protocol")  # no stable id cannot isolate notices
        return prof["value"]["id"]

    def _get_detail(self, field: str, headers: Dict[str, str],
                    captured: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        generation = captured["generation"] if captured else self._details_generation
        stored = captured["stored"] if captured else self._read_current_grant(generation)
        if stored is None or self._details_stale(generation):
            return None
        attempt = self._attempt
        if (field == "profile" and attempt is not None
                and attempt.get("initial_profile") is not None
                and attempt["initial_profile"]["token"] == stored["token"]):
            initial = attempt["initial_profile"]["value"]
            attempt["initial_profile"] = None  # consumed exactly once
            return initial
        try:
            timeout = (self._balance_timeout if field == "balance"
                       else self._request_timeout) / 1000.0
            details = read_account_detail(field, self.origin, stored["token"], headers, timeout)
            return None if self._details_stale(generation) else details
        except AccountUnauthorizedError:
            if not self._details_stale(generation):
                self._expire_credential(stored["token"], generation)
            return None

    def _detail_headers(self, client: Dict[str, Any]) -> Dict[str, str]:
        return {**self.account_request_headers, **platform_client_headers(self.platform, client)}

    def reject_token(self, token: str) -> None:
        generation = self._details_generation
        current = self._read_current_grant(generation)
        if current is None or current["token"] != token or self._details_stale(generation):
            return
        self._expire_credential(token, generation)

    def get_platform_session(self) -> Optional[Dict[str, Any]]:
        generation = self._details_generation
        stored = self._read_current_grant(generation)
        if stored is None or self._details_stale(generation):
            return None
        cached = self._last_profile
        session: Dict[str, Any] = {
            "origin": self.origin,
            "token": stored["token"],
            "userId": (cached["profile"]["value"].get("id") or None
                       if cached is not None and cached["token"] == stored["token"] else None),
            "requestHeaders": dict(self.account_request_headers),
        }
        if self.embedded_page_dist:
            session["embeddedPageDist"] = self.embedded_page_dist
        return session

    def _read_current_grant(self, generation: int) -> Optional[Dict[str, Any]]:
        if self._closed:
            return None
        record = self._credentials().read_record(KEY)
        if record is None or self._details_stale(generation):
            return None
        if record.get("kind") != "grant":
            raise PlatformAuthError("storage")
        parsed = _grant_or(record.get("payload"), "storage")
        if parsed["issuer"] != self.origin:
            raise PlatformAuthError("protocol")
        return parsed

    def resolve_token(self, url: str) -> Optional[str]:
        if self._closed or self._removing is not None:
            return None
        destination = urlparse(url)
        if not destination.scheme or not destination.netloc:
            raise ValueError("account: resolveToken requires an absolute URL")
        if (_origin_of(destination) != self.inference_origin
                or destination.username or destination.password):
            return None
        record = self._credentials().read_record(KEY)
        if record is None:
            return None
        if record.get("kind") != "grant":
            raise PlatformAuthError("storage")
        parsed = _grant_or(record.get("payload"), "storage")
        if parsed["token"].startswith("dsh_mock_"):
            return None
        issuer = urlparse(parsed["issuer"])
        if self.inference_origin == "https://api.deepseek.com":
            if (issuer.hostname or "") in ("localhost", "127.0.0.1", "::1"):
                return None  # a loopback dev grant never impersonates production inference
        elif _origin_of(issuer) != self.origin:
            return None
        return parsed["token"]

    # --------------------------------------------------------- sign-in / out

    def start_sign_in(self, client: Dict[str, Any], callback_origin: str,
                      login_source: str) -> Dict[str, Any]:
        if self._removing is not None:
            self._removing["event"].wait()
        origin = login_origin(callback_origin)
        if self._closed:
            raise PlatformAuthError("protocol")
        attempt = self._attempt
        if attempt is not None and attempt["view"]["phase"] in ACTIVE_PHASES:
            return self.get_state()  # an active attempt is never restarted
        if attempt is not None:
            attempt["done"].wait()
            if self._closed:
                raise PlatformAuthError("protocol")
            if self._attempt is not attempt:
                return self.get_state()
        new_attempt: Dict[str, Any] = {
            "origin": origin,
            "login_source": login_source,
            "locale": platform_wire_locale(client["locale"]),
            "client_headers": platform_client_headers(self.platform, client),
            "view": {"id": str(uuid_module.uuid4()), "phase": "initializing"},
            "token": CancelToken(),
            "done": threading.Event(),
            "callback": None,
            "completion_url": None,
            "initial_profile": None,
            "dispose_callback": None,
        }
        self._attempt = new_attempt
        threading.Thread(target=self._begin_attempt, args=(new_attempt,), daemon=True,
                         name="account-sign-in").start()
        self._changed()
        return self.get_state()

    def _begin_attempt(self, attempt: Dict[str, Any]) -> None:
        def refuse_prompt(question: Any) -> Any:
            raise PlatformAuthError("protocol")  # the browser is the only prompt surface

        try:
            outcome = self._authorization.begin({
                "key": KEY,
                "signal": attempt["token"],
                "interaction": {"notify": lambda notice: None, "prompt": refuse_prompt},
            })
            self._update(attempt, {"phase": "succeeded" if outcome["status"] == "authorized"
                                   else "cancelled"})
            self._complete_callback(attempt, outcome)
        except Exception as error:  # noqa: BLE001 - the attempt view carries the code
            code = error.code if isinstance(error, (PlatformAuthError, AuthorizationError)) \
                else "protocol"
            self.logger.info("[deepseek-account] sign-in failed errorCode=%s", code)
            self._update(attempt, {"phase": "expired" if code == "expired" else "failed",
                                   "errorCode": code})
            self._finish_failed_callback(attempt)
        finally:
            dispose = attempt.get("dispose_callback")
            if dispose is not None:
                try:
                    dispose()
                except Exception:  # noqa: BLE001 - disposal is best effort
                    pass
            attempt["done"].set()

    def _complete_callback(self, attempt: Dict[str, Any], outcome: Dict[str, str]) -> None:
        callback = attempt.get("callback")
        if callback is None:
            return
        if outcome["status"] == "authorized" and attempt.get("completion_url"):
            callback.complete((302, {"location": attempt["completion_url"],
                                     "cache-control": "no-store"}, b""))
        else:
            callback.complete((204, {"cache-control": "no-store"}, b""))

    def _finish_failed_callback(self, attempt: Dict[str, Any]) -> None:
        callback = attempt.get("callback")
        if callback is None:
            return
        if attempt["login_source"] == "web":
            nonce = _b64url(secrets.token_bytes(16))
            zh = attempt["locale"] == "zh_CN"
            message = ("登录失败，请关闭此标签页并在原页面重试。" if zh else
                       "Sign-in failed. Close this tab and try again in the original tab.")
            html = (f'<!doctype html><html lang="{"zh-CN" if zh else "en"}">'
                    f'<meta charset="utf-8"><title>{message}</title><body><p>{message}</p>'
                    f'<script nonce="{nonce}">window.close()</script></body></html>')
            callback.complete((200, {
                "content-type": "text/html; charset=utf-8",
                "cache-control": "no-store",
                "content-security-policy": (f"default-src 'none'; script-src 'nonce-{nonce}'; "
                                            "frame-ancestors 'none'"),
                "referrer-policy": "no-referrer",
            }, html.encode("utf-8")))
        else:
            callback.complete((204, {"cache-control": "no-store"}, b""))

    def cancel_sign_in(self, ident: str) -> Dict[str, Any]:
        attempt = self._attempt
        if attempt is not None and attempt["view"]["id"] == ident:
            if attempt["view"]["phase"] != "committing":
                attempt["token"].abort()
                self._authorization.cancel(KEY)
            attempt["done"].wait()
        return self.get_state()

    def sign_out(self, client: Dict[str, Any]) -> Dict[str, Any]:
        return self._single_flight(lambda: self._sign_out_body(client))

    def _sign_out_body(self, client: Dict[str, Any]) -> Dict[str, Any]:
        if self._closed:
            raise PlatformAuthError("protocol")
        attempt = self._attempt
        if attempt is not None:
            self.cancel_sign_in(attempt["view"]["id"])
        provider = self._credentials()
        record = provider.read_record(KEY)
        if record is not None:
            if record.get("kind") != "grant":
                raise PlatformAuthError("storage")
            parsed = _grant_or(record.get("payload"), "storage")
            if parsed["issuer"] != self.origin:
                raise PlatformAuthError("protocol")
            provider.delete_record(KEY)
            self._revoke(parsed["token"], platform_client_headers(self.platform, client))
        self._attempt = None
        self._emit("deepseek-account/signed-out")
        self._changed()
        return self.get_state()

    def _expire_credential(self, token: str, generation: int) -> Dict[str, Any]:
        return self._single_flight(lambda: self._expire_credential_body(token, generation))

    def _expire_credential_body(self, token: str, generation: int) -> Dict[str, Any]:
        attempt = self._attempt
        if attempt is not None:
            self.cancel_sign_in(attempt["view"]["id"])
        provider = self._credentials()
        record = provider.read_record(KEY)
        if (self._closed or self._details_stale(generation) or record is None
                or record.get("kind") != "grant"):
            return self.get_state()
        parsed = _grant_or(record.get("payload"), "storage")
        if parsed["token"] != token or parsed["issuer"] != self.origin:
            return self.get_state()
        provider.delete_record(KEY)
        self._emit("deepseek-account/session-expired")
        self._attempt = None
        self._emit("deepseek-account/signed-out")
        self._changed()
        return self.get_state()

    def _single_flight(self, body: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            slot = self._removing
            owner = slot is None
            if owner:
                slot = {"event": threading.Event(), "result": None, "error": None}
                self._removing = slot
        if not owner:
            slot["event"].wait()  # type: ignore[index]
            error = slot["error"]  # type: ignore[index]
            if error is not None:
                raise error
            return slot["result"]  # type: ignore[index]
        try:
            slot["result"] = body()  # type: ignore[index]
        except Exception as error:  # noqa: BLE001 - shared with the waiters
            slot["error"] = error  # type: ignore[index]
            raise
        finally:
            with self._lock:
                self._removing = None
            slot["event"].set()  # type: ignore[index]
        return slot["result"]  # type: ignore[index]

    def _revoke(self, token: str, headers: Dict[str, str]) -> None:
        if self._closed:
            return
        merged = {**self.request_headers, **headers}

        def worker() -> None:
            try:
                revoke_account(self.origin, token, self._logout_policy,
                               self._logout_lifetime, merged)
            finally:
                try:
                    self._revocations.remove(threading.current_thread())
                except ValueError:
                    pass

        thread = threading.Thread(target=worker, daemon=True, name="account-revoke")
        self._revocations.append(thread)
        thread.start()

    # ------------------------------------------------------------ the flow

    def _flow_run(self, session: Dict[str, Any]) -> None:
        attempt = self._attempt
        if attempt is None:
            raise PlatformAuthError("protocol")
        self._run(session, attempt)

    def _update(self, attempt: Dict[str, Any], value: Dict[str, Any]) -> None:
        view = {name: field for name, field in attempt["view"].items() if name != "authorizeUrl"}
        view.update(value)
        attempt["view"] = view
        self._changed()

    def _throw_if_aborted(self, session_token: CancelToken, deadline: CancelToken) -> None:
        if deadline.aborted:
            raise PlatformAuthError("expired")
        session_token.throw_if_aborted()

    def _request(self, method: str, body: Any, headers: Dict[str, str],
                 session_token: Optional[CancelToken] = None,
                 deadline: Optional[CancelToken] = None) -> Any:
        if session_token is not None and session_token.aborted:
            raise PlatformAuthError("network")  # fetch on an aborted signal
        if deadline is not None and deadline.aborted:
            raise PlatformAuthError("expired")
        return request_platform(self.origin, method, body,
                                {**self.request_headers, **headers},
                                self._request_timeout / 1000.0)

    def _run(self, session: Dict[str, Any], attempt: Dict[str, Any]) -> None:
        channel = self._callback_channel
        if channel is None:
            raise PlatformAuthError("protocol")  # no webServer, no browser leg
        verifier = _b64url(secrets.token_bytes(32))
        state = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        authorize_id: Optional[str] = None
        code_box = _CodeBox()
        deadline = CancelToken()
        expires_at = time.time() * 1000 + self._attempt_timeout
        timer = threading.Timer(self._attempt_timeout / 1000.0, deadline.abort)
        timer.daemon = True
        timer.start()
        session_token: CancelToken = session["signal"]

        def reject_expired() -> None:
            code_box.reject(PlatformAuthError("expired"))

        unsubscribe = [session_token.on_abort(reject_expired), deadline.on_abort(reject_expired)]

        def handler(info: Dict[str, Any]) -> tuple:
            pairs = info["query"]
            codes = [value for name, value in pairs if name == "code"]
            states = [value for name, value in pairs if name == "state"]
            received_state = states[0] if states else ""
            valid_state = (len(received_state.encode("utf-8")) == len(state.encode("utf-8"))
                           and secrets.compare_digest(received_state.encode("utf-8"),
                                                      state.encode("utf-8")))
            if (info["method"] != "GET" or info["path"] != "/oauth/callback" or not valid_state
                    or not codes or len(states) != 1 or len(codes) != 1):
                return (400, {"cache-control": "no-store"}, b"")
            if (session_token.aborted or deadline.aborted or attempt.get("callback") is not None
                    or attempt["view"]["phase"] != "waiting-browser"):
                return (410, {"cache-control": "no-store"}, b"")
            pending = _PendingCallback()
            attempt["callback"] = pending
            code_box.resolve(codes[0])
            hold_limit = (self._attempt_timeout + self._request_timeout) / 1000.0 + 60.0
            return pending.wait(hold_limit)

        try:
            attempt["dispose_callback"] = channel.register(handler)
            self._throw_if_aborted(session_token, deadline)
            redirect_uri = f"{attempt['origin']}/oauth/callback"
            payload = self._request("auth_init", {
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "redirect_uri": redirect_uri,
                "locale": attempt["locale"],
                "login_source": attempt["login_source"],
            }, attempt["client_headers"], session_token, deadline)
            init = _initialization_or(payload)
            authorize_url = browser_url(init["authorize_url"], self.origin, "/dsh/authorize",
                                        self.rewrite_browser_origin)
            authorize_id = init["authorize_id"]
            self._throw_if_aborted(session_token, deadline)
            now = time.time() * 1000
            expires_at = min(expires_at, now + init["expires_in"] * 1000)
            remaining = expires_at - now
            if remaining <= 0:
                deadline.abort()
                self._throw_if_aborted(session_token, deadline)
            timer.cancel()
            timer = threading.Timer(remaining / 1000.0, deadline.abort)
            timer.daemon = True
            timer.start()
            self._update(attempt, {"phase": "waiting-browser", "authorizeUrl": authorize_url,
                                   "expiresAt": int(expires_at)})
            received_code = code_box.wait()
            self._throw_if_aborted(session_token, deadline)
            self._update(attempt, {"phase": "exchanging"})
            provider = self._credentials()
            device_record = provider.modify_record(
                DEVICE,
                lambda current: (None if current is not None else
                                 {"kind": "grant", "payload": {"id": str(uuid_module.uuid4())}}))
            if device_record is None or device_record.get("kind") != "grant":
                raise PlatformAuthError("storage")
            device = _device_or(device_record.get("payload"))
            payload = self._request("auth_exchange", {
                "code": received_code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "device_id": device["id"],
                "device_model": f"{sys.platform}-{os_platform.machine()}",
                "os_version": f"{sys.platform} {os_platform.release()}",
            }, attempt["client_headers"], session_token, deadline)
            result = _exchange_or(payload)
            completion = browser_url(result["authorized_url"], self.origin, "/dsh/authorized",
                                     self.rewrite_browser_origin)
            separator = "&" if urlparse(completion).query else "?"
            attempt["completion_url"] = (completion + separator
                                         + urlencode({"login_source": attempt["login_source"]}))
            if time.time() * 1000 >= expires_at:
                deadline.abort()
            self._throw_if_aborted(session_token, deadline)
            if result["user"] is not None:
                try:
                    attempt["initial_profile"] = {
                        "token": result["token"],
                        "value": {"status": "ready", "value": profile(result["user"])},
                    }
                except Exception:  # noqa: BLE001 - a malformed user never blocks the login
                    pass
            self._update(attempt, {"phase": "committing"})
            timer.cancel()
            try:
                session["commit"]({"kind": "grant",
                                   "payload": {"version": 1, "token": result["token"],
                                               "issuer": self.origin}})
            except Exception as error:  # noqa: BLE001 - the commit boundary is storage
                if isinstance(error, PlatformAuthError):
                    raise
                raise PlatformAuthError("storage") from error
        except Exception:
            if deadline.aborted:
                raise PlatformAuthError("expired")
            raise
        finally:
            if (session_token.aborted or deadline.aborted) and authorize_id is not None:
                self._cancel_request(authorize_id, verifier, attempt["client_headers"])
            timer.cancel()
            for unwatch in unsubscribe:
                unwatch()

    def _cancel_request(self, authorize_id: str, verifier: str, headers: Dict[str, str]) -> None:
        if self._closed:
            return

        def worker() -> None:
            try:
                if not self._logout_lifetime.aborted:
                    request_platform(self.origin, "auth_cancel", {
                        "authorize_id": authorize_id, "code_verifier": verifier},
                        {**self.request_headers, **headers}, self._request_timeout / 1000.0)
            except Exception:  # noqa: BLE001 - best effort, stays in the background
                pass
            finally:
                try:
                    self._revocations.remove(threading.current_thread())
                except ValueError:
                    pass

        thread = threading.Thread(target=worker, daemon=True, name="account-cancel")
        self._revocations.append(thread)
        thread.start()
