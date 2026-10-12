"""Contract tests for the account platform protocol/details/logout layers."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from AstralNexus.runtime.account import protocol as proto
from AstralNexus.runtime.account.details import (
    profile, read_account_detail, read_unnotified_bonuses, send_bonus_notified)
from AstralNexus.runtime.account.logout import revoke_account
from AstralNexus.runtime.account.platform import CONFIG_DEFAULTS, validate_config
from AstralNexus.runtime.authorization import CancelToken


def envelope(biz_data: Any, biz_code: int = 0, code: int = 0) -> bytes:
    return json.dumps({"code": code, "data": {"biz_code": biz_code,
                                              "biz_data": biz_data}}).encode("utf-8")


class FakeServer:
    """A scripted loopback HTTP server standing in for the Platform."""

    def __init__(self, routes: Optional[Dict[str, Callable]] = None):
        self.routes: Dict[str, Callable] = routes or {}
        self.requests: List[Tuple[str, Any, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, body: Optional[bytes]):
                payload = None
                if body:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except ValueError:
                        payload = None
                # Keep the case-insensitive Message (urllib capitalizes names).
                outer.requests.append((self.path, payload, self.headers))
                route = outer.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                status, response_body = route(payload, self.headers)
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def do_GET(self):
                self._handle(None)

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                self._handle(self.rfile.read(length) if length else None)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"{self.origin}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture()
def fake():
    made = []

    def build(routes=None):
        server = FakeServer(routes)
        made.append(server)
        return server

    yield build
    for server in made:
        server.close()


# ------------------------------------------------------------------ origins


class TestPlatformOrigin:
    def test_https_accepted_and_normalized(self):
        assert proto.platform_origin("https://platform.deepseek.com") == \
            "https://platform.deepseek.com"
        assert proto.platform_origin("https://platform.deepseek.com:443/") == \
            "https://platform.deepseek.com"

    def test_loopback_http_only_with_opt_in(self):
        with pytest.raises(ValueError, match="HTTPS origin"):
            proto.platform_origin("http://127.0.0.1:8080")
        assert proto.platform_origin("http://127.0.0.1:8080", True) == "http://127.0.0.1:8080"
        assert proto.platform_origin("http://localhost:9", True) == "http://localhost:9"
        assert proto.platform_origin("http://[::1]:9", True) == "http://[::1]:9"

    @pytest.mark.parametrize("value", [
        "https://user:pass@platform.deepseek.com",   # credentials
        "https://platform.deepseek.com/api",         # path
        "https://platform.deepseek.com/?x=1",        # query
        "https://platform.deepseek.com/#f",          # fragment
        "http://10.0.0.5:8080",                      # non-loopback http even opted in
    ])
    def test_rejections(self, value):
        with pytest.raises(ValueError, match="HTTPS origin"):
            proto.platform_origin(value, True)


class TestBrowserUrl:
    ORIGIN = "https://platform.deepseek.com"

    def test_matching_origin_and_path(self):
        assert proto.browser_url(f"{self.ORIGIN}/dsh/authorize?sid=9", self.ORIGIN,
                                 "/dsh/authorize") == f"{self.ORIGIN}/dsh/authorize?sid=9"

    def test_rewrite_maps_validated_https_to_configured_origin(self):
        assert proto.browser_url("https://other.example/dsh/authorize?a=1", self.ORIGIN,
                                 "/dsh/authorize", True) == f"{self.ORIGIN}/dsh/authorize?a=1"
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.browser_url("http://other.example/dsh/authorize", self.ORIGIN,
                              "/dsh/authorize", True)
        assert info.value.code == "protocol"  # rewrite is https-only

    @pytest.mark.parametrize("value", [
        "https://evil.example/dsh/authorize",          # foreign origin
        "https://platform.deepseek.com/other",         # wrong path
        "https://platform.deepseek.com/dsh/authorize#f",  # fragment
        "https://user@platform.deepseek.com/dsh/authorize",  # credentials
        "not a url",
    ])
    def test_rejections_never_forward(self, value):
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.browser_url(value, self.ORIGIN, "/dsh/authorize")
        assert info.value.code == "protocol"
        assert str(info.value) == "account: protocol"  # never the URL itself


class TestPlatformHeaders:
    @pytest.mark.parametrize("name", list(proto.RESERVED_HEADERS))
    def test_reserved_rejected(self, name):
        with pytest.raises(ValueError, match="reserved or duplicate"):
            proto.platform_headers({name: "v"})

    def test_duplicate_case_insensitive(self):
        with pytest.raises(ValueError, match="reserved or duplicate"):
            proto.platform_headers({"X-A": "1", "x-a": "2"})

    def test_normalized_lower(self):
        assert proto.platform_headers({"X-Deploy": "v"}) == {"x-deploy": "v"}


class TestLoginOrigin:
    @pytest.mark.parametrize("value,expected", [
        ("http://127.0.0.1:51380/", "http://127.0.0.1:51380"),
        ("http://localhost:1", "http://localhost:1"),
        ("http://[::1]:80", "http://[::1]:80"),
    ])
    def test_accepted(self, value, expected):
        assert proto.login_origin(value) == expected

    @pytest.mark.parametrize("value", [
        "http://127.0.0.1",            # no explicit port
        "http://127.0.0.1:0",          # port 0
        "https://127.0.0.1:443",       # not http
        "http://example.com:80",       # remote domain
        "http://127.0.0.1:80/path",    # path-based proxy
        "http://user@127.0.0.1:80",    # credentials
        "http://127.0.0.1:80/?x=1",    # query
    ])
    def test_rejected(self, value):
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.login_origin(value)
        assert info.value.code == "protocol"


# ---------------------------------------------------------------- validators


class TestValidators:
    def test_envelope(self):
        data, issues = proto.parse_envelope({"code": 0, "data": {"biz_code": 0, "biz_data": 5}})
        assert data == {"biz_code": 0, "biz_data": 5} and issues == []
        _, issues = proto.parse_envelope({"code": 1, "data": {"biz_code": 0}})
        assert issues[0]["code"] == "invalid_literal" and issues[0]["path"] == ["code"]
        _, issues = proto.parse_envelope({"code": 0, "data": {"biz_code": "0"}})
        assert issues[0]["path"] == ["data", "biz_code"]
        assert issues[0]["received_type"] == "string"
        _, issues = proto.parse_envelope({"code": 0})
        assert issues[0]["received_type"] == "undefined"  # missing projection

    def test_initialization_and_exchange(self):
        ok, issues = proto.parse_initialization(
            {"authorize_url": "https://p/dsh/authorize", "authorize_id": "a", "expires_in": 60})
        assert issues == [] and ok["expires_in"] == 60
        _, issues = proto.parse_initialization(
            {"authorize_url": "nope", "authorize_id": "", "expires_in": 0})
        assert [item["code"] for item in issues] == ["invalid_url", "too_small", "too_small"]
        ok, issues = proto.parse_exchange(
            {"token": "abc!~", "authorized_url": "https://p/dsh/authorized", "user": {"id": "1"}})
        assert issues == [] and ok["user"] == {"id": "1"}
        _, issues = proto.parse_exchange({"token": "has space", "authorized_url": "x"})
        assert issues[0]["code"] == "invalid_string_format"

    def test_grant_and_device(self):
        ok, issues = proto.parse_grant_payload(
            {"version": 1, "token": "t", "issuer": "https://p"})
        assert issues == [] and ok["token"] == "t"
        _, issues = proto.parse_grant_payload({"version": 2, "token": "", "issuer": "x"})
        assert len(issues) == 3
        import uuid
        ok, issues = proto.parse_device_payload({"id": str(uuid.uuid4())})
        assert issues == []
        _, issues = proto.parse_device_payload({"id": "not-a-uuid"})
        assert issues[0]["code"] == "invalid_string_format"


# ---------------------------------------------------------------- transport


class TestPlatformRequest:
    def test_success_unwraps_envelope(self, fake):
        server = fake({"/auth-api/v0/dsh/auth_init":
                       lambda body, headers: (200, envelope({"ok": 1}))})
        assert proto.platform_request(server.url("/auth-api/v0/dsh/auth_init"),
                                      method="POST", body=b"{}", timeout=10) == {"ok": 1}

    def test_unauthorized_paths(self, fake):
        # 40003 is the TOP-LEVEL envelope code (upstream payload.code === 40003).
        expired = json.dumps({"code": 40003, "data": {"biz_code": 0}}).encode("utf-8")
        server = fake({"/a": lambda body, headers: (401, b""),
                       "/b": lambda body, headers: (200, expired)})
        with pytest.raises(proto.AccountUnauthorizedError) as info:
            proto.platform_request(server.url("/a"), method="GET", timeout=10,
                                   headers={"x-dsh-auth-token": "t"})
        assert info.value.code == "expired"
        # 401 WITHOUT the auth token is a plain network failure, not expiry.
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.platform_request(server.url("/a"), method="GET", timeout=10)
        assert info.value.code == "network"
        with pytest.raises(proto.AccountUnauthorizedError):
            proto.platform_request(server.url("/b"), method="GET", timeout=10,
                                   headers={"x-dsh-auth-token": "t"})
        # biz 40003 without a token is a business-code protocol failure.
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.platform_request(server.url("/b"), method="GET", timeout=10)
        assert info.value.code == "protocol"

    def test_body_limit_and_parse(self, fake):
        big = b"x" * (proto.BODY_LIMIT + 1)
        server = fake({"/big": lambda body, headers: (200, big),
                       "/bad": lambda body, headers: (200, b"not json{")})
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.platform_request(server.url("/big"), method="GET", timeout=10)
        assert info.value.code == "protocol"
        with pytest.raises(proto.PlatformAuthError):
            proto.platform_request(server.url("/bad"), method="GET", timeout=10)

    def test_non_ok_and_network_and_redirect(self, fake):
        server = fake({"/err": lambda body, headers: (500, b"oops")})
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.platform_request(server.url("/err"), method="GET", timeout=10)
        assert info.value.code == "network"
        with pytest.raises(proto.PlatformAuthError) as info:
            proto.platform_request("http://127.0.0.1:1/x", method="GET", timeout=2)
        assert info.value.code == "network"  # connection refused
        redirect_server = fake({"/r": lambda body, headers: (200, b"{}")})

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("location", redirect_server.url("/r"))
                self.send_header("content-length", "0")
                self.end_headers()

        mover = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=mover.serve_forever, daemon=True).start()
        try:
            with pytest.raises(proto.PlatformAuthError) as info:
                proto.platform_request(f"http://127.0.0.1:{mover.server_address[1]}/go",
                                       method="GET", timeout=10)
            assert info.value.code == "network"  # a refused redirect is not ok
        finally:
            mover.shutdown()
            mover.server_close()

    def test_request_platform_never_logs_token(self, fake, caplog):
        server = fake({"/auth-api/v0/dsh/auth_exchange":
                       lambda body, headers: (200, envelope({"token": "SECRET-TOKEN"}))})
        import logging
        with caplog.at_level(logging.DEBUG, logger="astralnexus.account"):
            data = proto.request_platform(server.origin, "auth_exchange", {"code": "c"},
                                          {"x-secret": "HEADER-SECRET"}, timeout=10)
        assert data == {"token": "SECRET-TOKEN"}
        text = caplog.text
        assert "SECRET-TOKEN" not in text and "HEADER-SECRET" not in text
        assert "path=/auth-api/v0/dsh/auth_exchange" in text  # structured metadata only


# ------------------------------------------------------------------ details


class TestDetails:
    USER = {"id": "u1", "email": "e@x.com", "mobile": None, "mobile_number": "139",
            "id_profile": {"name": "N", "picture": "https://p/a.png"}}

    def test_profile_projection_and_contact_fallback(self):
        projected = profile(self.USER)
        assert projected == {"id": "u1", "avatarUrl": "https://p/a.png", "name": "N",
                             "contact": "139"}  # mobile_number beats email
        assert profile({"id": None, "email": "e@x.com"}) == {
            "id": None, "avatarUrl": None, "name": None, "contact": "e@x.com"}
        with pytest.raises(proto.PlatformAuthError) as info:
            profile({"id": "u"})  # email missing
        assert info.value.code == "protocol"

    def test_read_account_detail_never_zero_balance(self, fake):
        server = fake({"/auth-api/v0/users/current": lambda body, headers: (500, b"")})
        result = read_account_detail("profile", server.origin, "t", {}, timeout=10)
        assert result == {"status": "failed"}  # not a zero/empty profile
        server.routes["/auth-api/v0/users/current"] = \
            lambda body, headers: (401, b"")
        with pytest.raises(proto.AccountUnauthorizedError):
            read_account_detail("profile", server.origin, "t",
                                {"x-dsh-auth-token": "t"}, timeout=10)

    def test_balance_projection(self, fake):
        summary = {"normal_wallets": [{"currency": "CNY", "balance": "10.5"}],
                   "bonus_wallets": []}
        server = fake({"/api/v0/users/get_user_summary":
                       lambda body, headers: (200, envelope(summary))})
        result = read_account_detail("balance", server.origin, "t", {}, timeout=10)
        assert result == {"status": "ready",
                          "value": [{"currency": "CNY", "balance": "10.5"}], "bonusWallets": []}
        server.routes["/api/v0/users/get_user_summary"] = \
            lambda body, headers: (200, envelope({"normal_wallets": [{"currency": "EUR",
                                                                      "balance": "x"}],
                                                  "bonus_wallets": []}))
        assert read_account_detail("balance", server.origin, "t", {},
                                   timeout=10) == {"status": "failed"}

    def test_bonuses_and_ack(self, fake):
        import uuid
        order = str(uuid.uuid4())
        rows = [{"order_id": order, "campaign": "c", "amount": "1.00", "currency": "USD",
                 "granted_at": "t1", "expires_at": "t2", "msg": "m"}]
        acked = []
        server = fake({
            "/api/v0/users/get_unnotified_bonuses": lambda body, headers: (200, envelope(rows)),
            "/api/v0/users/ack_bonus_notified": lambda body, headers: (
                acked.append(body) or (200, envelope(None))),
        })
        bonuses = read_unnotified_bonuses(server.origin, "t", {}, timeout=10)
        assert bonuses == [{"orderId": order, "campaign": "c", "amount": "1.00",
                            "currency": "USD", "grantedAt": "t1", "expiresAt": "t2",
                            "message": "m"}]
        send_bonus_notified(server.origin, "t", order, {}, timeout=10)
        assert acked == [{"order_id": order}]
        server.routes["/api/v0/users/get_unnotified_bonuses"] = \
            lambda body, headers: (200, envelope([{"order_id": "not-a-uuid"}]))
        with pytest.raises(proto.PlatformAuthError):
            read_unnotified_bonuses(server.origin, "t", {}, timeout=10)


# ------------------------------------------------------------------- logout


class TestRevokeAccount:
    POLICY = {"max_retries": 5, "delay_ms": 1, "request_timeout_ms": 10000}

    def test_success_stops_retrying(self, fake):
        hits = []
        server = fake({"/auth-api/v0/users/logout": lambda body, headers: (
            hits.append(headers.get("x-dsh-auth-token")) or (200, envelope(None)))})
        revoke_account(server.origin, "tok", self.POLICY, CancelToken(), {})
        assert hits == ["tok"]

    def test_exhaustion_is_silent_and_backs_off(self, fake):
        hits = []
        server = fake({"/auth-api/v0/users/logout": lambda body, headers: (
            hits.append(1) or (500, b""))})
        policy = {"max_retries": 2, "delay_ms": 30, "request_timeout_ms": 10000}
        started = time.time()
        revoke_account(server.origin, "tok", policy, CancelToken(), {})
        assert len(hits) == 3  # initial + 2 retries
        assert time.time() - started >= 0.08  # 30ms + 60ms backoff floors

    def test_lifetime_abort_ends_the_loop(self, fake):
        hits = []
        server = fake({"/auth-api/v0/users/logout": lambda body, headers: (
            hits.append(1) or (500, b""))})
        lifetime = CancelToken()
        threading.Timer(0.05, lifetime.abort).start()
        revoke_account(server.origin, "tok",
                       {"max_retries": 50, "delay_ms": 40, "request_timeout_ms": 10000},
                       lifetime, {})
        assert len(hits) < 50  # the abort cut the retry loop short
        assert lifetime.aborted


# ------------------------------------------------------------------- config


class TestConfig:
    def test_defaults(self):
        resolved = validate_config(None)
        assert resolved["platform_origin"] == "https://platform.deepseek.com"
        assert resolved["attempt_timeout_ms"] == 600000
        assert resolved["logout_max_retries"] == 5

    def test_rejections(self):
        with pytest.raises(ValueError, match="unknown option"):
            validate_config({"nope": 1})
        with pytest.raises(ValueError, match="desktop_platform"):
            validate_config({"desktop_platform": "linux"})
        with pytest.raises(ValueError, match="request_timeout_ms"):
            validate_config({"request_timeout_ms": 0})
        with pytest.raises(ValueError, match="request_timeout_ms"):
            validate_config({"request_timeout_ms": 120001})
        with pytest.raises(ValueError, match="allow_loopback_http"):
            validate_config({"allow_loopback_http": "yes"})
        with pytest.raises(ValueError, match="request_headers"):
            validate_config({"request_headers": {"a": 1}})
        with pytest.raises(ValueError, match="logout_max_retries"):
            validate_config({"logout_max_retries": 2.5})
        assert set(CONFIG_DEFAULTS) >= {"platform_origin", "inference_origin"}
