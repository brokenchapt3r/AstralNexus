"""Validated platform HTTP messages and restricted browser destinations.

Port of the protocol layer of ``@deepseek-ai/dsh-deepseek-account-platform``
(commit 5badb15, lib/types/protocol.js). Contract kept:

- ``PlatformAuthError`` exposes a stable code and NEVER a response body or an
  authorization URL (message is exactly ``account: {code}``).
- ``AccountUnauthorizedError`` (code ``expired``) fires on HTTP 401 or
  envelope code 40003 — only when the request actually carried
  ``x-dsh-auth-token``.
- ``platform_origin`` accepts HTTPS origins, or loopback HTTP ONLY as an
  explicit development opt-in; credentials/path/query/fragment are rejected.
- ``browser_url`` validates platform-owned browser destinations (origin match
  or https + rewrite opt-in, exact path, no credentials, no fragment) and
  logs the structured rejection reason — it never forwards an arbitrary URL.
- ``platform_headers`` rejects reserved (authorization, x-dsh-auth-token,
  host, content-length, transfer-encoding, connection, content-type) and
  duplicate headers; authorization/routing/framing remain provider-owned.
- ``platform_request`` reads one bounded (64 KiB) response with stable,
  non-secret diagnostics at every stage (request/response/codes/envelope
  rejection with issue paths only/body-limit/parse stage), refuses redirects,
  and unwraps the ``{code: 0, data: {biz_code, biz_data}}`` envelope.
- ``login_origin`` accepts a browser-accessible loopback HTTP origin with an
  EXPLICIT port (local or SSH-forwarded login); remote domains and path-based
  proxies are unsupported.

Python adaptations (documented): fetch → urllib with a redirect-refusing
opener; an in-flight request cannot be cancelled by a token (urllib has no
abort), so cancellation is honored at the caller's checkpoints and the
timeout is the wire bound; zod schemas → issue-collecting validators whose
issues carry ``path``/``code``/``received_type`` and never a value; console.info
→ the ``astralnexus.account`` logger at INFO with the same structured fields.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

LOG = logging.getLogger("astralnexus.account")

BODY_LIMIT = 65536
RESERVED_HEADERS = frozenset({
    "authorization", "x-dsh-auth-token", "host", "content-length",
    "transfer-encoding", "connection", "content-type"})
# urlparse().hostname strips IPv6 brackets ("::1"); the JS URL keeps them.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
TOKEN_PRINTABLE = re.compile(r"^[\x21-\x7e]+$")
_LOGIN_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1|\[::1\]):([0-9]+)\/?$", re.IGNORECASE)


class PlatformAuthError(Exception):
    """Protocol errors expose a stable code, never a response body or URL."""

    def __init__(self, code: str):
        super().__init__(f"account: {code}")
        self.code = code


class AccountUnauthorizedError(PlatformAuthError):
    """An authenticated Platform request was rejected with HTTP 401 or code 40003."""

    def __init__(self) -> None:
        super().__init__("expired")


def _opener():
    from urllib.request import HTTPRedirectHandler

    class RefuseRedirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # a redirect becomes an HTTPError, i.e. not ok

    return build_opener(RefuseRedirects)


def _is_http_url(value: Any) -> bool:
    """z.url() equivalent restricted to the platform's HTTP(S) semantics."""
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def received_type(value: Any, missing: bool = False) -> str:
    """The upstream issue projection: null / array / a typeof name, never content."""
    if missing:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    return "object"


def issue(path: List[Any], code: str, value: Any = None, missing: bool = False) -> Dict[str, Any]:
    return {"path": list(path), "code": code,
            "received_type": received_type(value, missing)}


def log_payload_rejection(stage: str, issues: List[Dict[str, Any]]) -> None:
    LOG.info("[deepseek-account] payload rejected stage=%s issues=%s", stage,
             json.dumps(issues, ensure_ascii=False))


# --------------------------------------------------------------- validators


def parse_envelope(payload: Any) -> Tuple[Any, List[Dict[str, Any]]]:
    """``{code: 0, data: {biz_code: int, biz_data: unknown}}`` → (data, issues).

    The success arm returns the inner ``{"biz_code", "biz_data"}`` mapping;
    the caller enforces ``biz_code == 0`` (upstream reads it off the parsed
    envelope the same way).
    """
    issues: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return None, [issue([], "invalid_type", payload)]
    if payload.get("code", None) != 0 or isinstance(payload.get("code"), bool):
        issues.append(issue(["code"], "invalid_literal", payload.get("code"),
                            missing="code" not in payload))
    data = payload.get("data")
    if not isinstance(data, dict):
        issues.append(issue(["data"], "invalid_type", data, missing="data" not in payload))
        return None, issues
    biz_code = data.get("biz_code")
    if not isinstance(biz_code, int) or isinstance(biz_code, bool):
        issues.append(issue(["data", "biz_code"], "invalid_type", biz_code,
                            missing="biz_code" not in data))
    if issues:
        return None, issues
    return {"biz_code": biz_code, "biz_data": data.get("biz_data")}, []


def parse_initialization(value: Any) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``{authorize_url: url, authorize_id: str>=1, expires_in: positive}``."""
    issues: List[Dict[str, Any]] = []
    if not isinstance(value, dict):
        return None, [issue([], "invalid_type", value)]
    url = value.get("authorize_url")
    if not _is_http_url(url):
        issues.append(issue(["authorize_url"], "invalid_url", url,
                            missing="authorize_url" not in value))
    authorize_id = value.get("authorize_id")
    if not isinstance(authorize_id, str) or not authorize_id:
        issues.append(issue(["authorize_id"], "too_small", authorize_id,
                            missing="authorize_id" not in value))
    expires_in = value.get("expires_in")
    if (not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool)
            or expires_in <= 0):
        issues.append(issue(["expires_in"], "too_small", expires_in,
                            missing="expires_in" not in value))
    if issues:
        return None, issues
    return {"authorize_url": url, "authorize_id": authorize_id, "expires_in": expires_in}, []


def parse_exchange(value: Any) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``{token: printable-ASCII, authorized_url: url, user?: unknown}``."""
    issues: List[Dict[str, Any]] = []
    if not isinstance(value, dict):
        return None, [issue([], "invalid_type", value)]
    token = value.get("token")
    if not isinstance(token, str) or not TOKEN_PRINTABLE.match(token):
        issues.append(issue(["token"], "invalid_string_format", token,
                            missing="token" not in value))
    authorized_url = value.get("authorized_url")
    if not _is_http_url(authorized_url):
        issues.append(issue(["authorized_url"], "invalid_url", authorized_url,
                            missing="authorized_url" not in value))
    if issues:
        return None, issues
    return {"token": token, "authorized_url": authorized_url,
            "user": value.get("user")}, []


def parse_grant_payload(payload: Any) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``{version: 1, token: str>=1, issuer: url}``."""
    issues: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return None, [issue([], "invalid_type", payload)]
    if payload.get("version") != 1 or isinstance(payload.get("version"), bool):
        issues.append(issue(["version"], "invalid_literal", payload.get("version"),
                            missing="version" not in payload))
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        issues.append(issue(["token"], "too_small", token, missing="token" not in payload))
    issuer = payload.get("issuer")
    if not _is_http_url(issuer):
        issues.append(issue(["issuer"], "invalid_url", issuer, missing="issuer" not in payload))
    if issues:
        return None, issues
    return {"version": 1, "token": token, "issuer": issuer}, []


def parse_device_payload(payload: Any) -> Tuple[Optional[Dict[str, str]], List[Dict[str, Any]]]:
    """``{id: uuid}``."""
    import uuid as uuid_module
    if not isinstance(payload, dict):
        return None, [issue([], "invalid_type", payload)]
    device_id = payload.get("id")
    ok = isinstance(device_id, str)
    if ok:
        try:
            uuid_module.UUID(device_id)
        except ValueError:
            ok = False
    if not ok or not isinstance(device_id, str):
        return None, [issue(["id"], "invalid_string_format", device_id,
                            missing="id" not in payload)]
    return {"id": device_id}, []


# ------------------------------------------------------------ URL policies


def platform_origin(value: str, allow_loopback_http: bool = False) -> str:
    """Normalize an HTTPS platform origin, or explicit loopback development HTTP."""
    parsed = urlparse(value)
    loopback = parsed.hostname in LOOPBACK_HOSTS if parsed.hostname else False
    scheme_ok = parsed.scheme == "https" or (
        allow_loopback_http and loopback and parsed.scheme == "http")
    if (parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not scheme_ok or not parsed.netloc):
        raise ValueError("account: platformOrigin must be an HTTPS origin or "
                         "explicitly enabled loopback HTTP origin")
    return _origin_of(parsed)


def _origin_of(parsed: Any) -> str:
    port = parsed.port
    default = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    host = parsed.hostname or ""  # already lower-cased by urlparse
    if ":" in host:  # IPv6: restore the bracketed form the JS URL keeps
        host = f"[{host}]"
    netloc = host if default or port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def browser_url(value: str, origin: str, path: str, rewrite_origin: bool = False) -> str:
    """Validate a platform-owned browser destination; never forward an arbitrary URL."""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        LOG.info("[deepseek-account] browser URL rejected path=%s reason=invalid-url", path)
        raise PlatformAuthError("protocol")
    url_origin = _origin_of(parsed)
    allowed_origin = url_origin == origin or (rewrite_origin and parsed.scheme == "https")
    if (not allowed_origin or parsed.path != path or parsed.username or parsed.password
            or parsed.fragment):
        LOG.info("[deepseek-account] browser URL rejected path=%s originMismatch=%s "
                 "pathMismatch=%s hasCredentials=%s hasFragment=%s", path,
                 not allowed_origin, parsed.path != path,
                 bool(parsed.username or parsed.password), bool(parsed.fragment))
        raise PlatformAuthError("protocol")
    if rewrite_origin:
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{origin}{parsed.path}{query}"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{url_origin}{parsed.path}{query}"  # normalized, like upstream's url.href


def platform_headers(values: Dict[str, str]) -> Dict[str, str]:
    """Validate Host-only deployment headers; names normalize to lower case."""
    if not isinstance(values, dict):
        raise ValueError("account: requestHeaders must be a mapping")
    headers: Dict[str, str] = {}
    seen = set()
    for name, value in values.items():
        key = name.lower()
        if key in RESERVED_HEADERS or key in seen:
            raise ValueError("account: requestHeaders contains a reserved or duplicate header")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("account: requestHeaders contains an invalid header")
        seen.add(key)
        headers[key] = value
    return headers


def login_origin(value: str) -> str:
    """Normalize a loopback HTTP callback origin with an explicit non-zero port."""
    parsed = urlparse(value)
    match = _LOGIN_ORIGIN.match(value)
    explicit_port = match.group(1) if match else None
    if (explicit_port is None or int(explicit_port) == 0 or parsed.scheme != "http"
            or (parsed.hostname or "") not in LOOPBACK_HOSTS
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise PlatformAuthError("protocol")
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{int(explicit_port)}"


# ------------------------------------------------------------- the transport


def platform_request(url: str, *, method: str, headers: Optional[Dict[str, str]] = None,
                     body: Optional[bytes] = None, timeout: float = 30.0) -> Any:
    """Read one bounded platform response with stable, non-secret diagnostics.

    Returns the successful ``biz_data`` payload; every rejection path raises
    ``PlatformAuthError`` (``AccountUnauthorizedError`` for an expired token)
    and logs only path/method/status/stage/issue metadata.
    """
    path = urlparse(url).path
    all_headers = dict(headers or {})
    LOG.info("[deepseek-account] request path=%s method=%s", path, method)
    request = Request(url, data=body, method=method, headers=all_headers)
    try:
        response = _opener().open(request, timeout=timeout)
    except HTTPError as error:
        # Non-2xx (including a refused redirect) arrives here; upstream cancels
        # the body unread, so neither do we.
        status = error.code
        LOG.info("[deepseek-account] response path=%s status=%s", path, status)
        error.close()
        if status == 401 and _has_auth_token(all_headers):
            raise AccountUnauthorizedError() from None
        raise PlatformAuthError("network") from None
    except (URLError, OSError, TimeoutError) as error:
        LOG.info("[deepseek-account] request failed path=%s errorCode=network", path)
        raise PlatformAuthError("network") from error
    with response:
        status = response.status
        LOG.info("[deepseek-account] response path=%s status=%s", path, status)
        raw = response.read(BODY_LIMIT + 1)
    if len(raw) > BODY_LIMIT:
        LOG.info("[deepseek-account] response rejected path=%s stage=body-limit errorCode=protocol",
                 path)
        raise PlatformAuthError("protocol")
    stage = "parse-json"
    try:
        payload = json.loads(raw.decode("utf-8"))
        if (isinstance(payload, dict) and payload.get("code") == 40003
                and _has_auth_token(all_headers)):
            raise AccountUnauthorizedError()
        if isinstance(payload, dict) and isinstance(payload.get("code"), int):
            data = payload.get("data")
            biz = data.get("biz_code") if isinstance(data, dict) else None
            LOG.info("[deepseek-account] response codes path=%s code=%s bizCode=%s",
                     path, payload.get("code"), biz)
        stage = "envelope"
        envelope_data, issues = parse_envelope(payload)
        if issues:
            LOG.info("[deepseek-account] envelope rejected path=%s issues=%s", path,
                     json.dumps(issues, ensure_ascii=False))
            raise PlatformAuthError("protocol")
        stage = "business-code"
        if envelope_data["biz_code"] != 0:
            raise PlatformAuthError("protocol")
        return envelope_data["biz_data"]
    except PlatformAuthError as error:
        LOG.info("[deepseek-account] response rejected path=%s stage=%s errorCode=%s",
                 path, stage, error.code)
        raise
    except (ValueError, UnicodeDecodeError) as error:
        LOG.info("[deepseek-account] response rejected path=%s stage=%s errorCode=protocol",
                 path, stage)
        raise PlatformAuthError("protocol") from error


def _has_auth_token(headers: Dict[str, str]) -> bool:
    return any(name.lower() == "x-dsh-auth-token" for name in headers)


def request_platform(origin: str, method: str, body: Any, headers: Dict[str, str],
                     timeout: float) -> Any:
    """POST one protocol request to ``/auth-api/v0/dsh/{method}``; never logged."""
    merged = dict(headers)
    merged["content-type"] = "application/json"
    return platform_request(f"{origin}/auth-api/v0/dsh/{method}", method="POST",
                            headers=merged,
                            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            timeout=timeout)


def account_headers(headers: Dict[str, str], token: str) -> Dict[str, str]:
    return {**headers, "x-dsh-auth-token": token}


def request_account(origin: str, path: str, token: str, headers: Dict[str, str],
                    timeout: float) -> Any:
    return platform_request(f"{origin}{path}", method="GET",
                            headers=account_headers(headers, token), timeout=timeout)


def request_unnotified_bonuses(origin: str, token: str, headers: Dict[str, str],
                               timeout: float) -> Any:
    return platform_request(f"{origin}/api/v0/users/get_unnotified_bonuses", method="GET",
                            headers=account_headers(headers, token), timeout=timeout)


def request_bonus_notified(origin: str, token: str, order_id: str, headers: Dict[str, str],
                           timeout: float) -> Any:
    merged = account_headers(headers, token)
    merged["content-type"] = "application/json"
    return platform_request(f"{origin}/api/v0/users/ack_bonus_notified", method="POST",
                            headers=merged,
                            body=json.dumps({"order_id": order_id}).encode("utf-8"),
                            timeout=timeout)


def logout_account(origin: str, token: str, headers: Dict[str, str], timeout: float) -> None:
    """End the Platform session using its existing logout endpoint."""
    merged = dict(headers)
    merged["x-dsh-auth-token"] = token
    platform_request(f"{origin}/auth-api/v0/users/logout", method="POST", headers=merged,
                     timeout=timeout)
