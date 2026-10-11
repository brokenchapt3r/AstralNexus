"""PlatformAccount state-machine, plugin and HTTP-surface tests.

The fake Platform is a real loopback HTTP server, so the whole PKCE leg —
auth_init, the held /oauth/callback, auth_exchange, auth_cancel, logout and
the details queries — travels the production transport (urllib) end to end.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.runtime.account.platform import (
    DEVICE, KEY, OAuthCallbackChannel, PlatformAccount, validate_config)
from AstralNexus.runtime.account import protocol as proto
from AstralNexus.runtime.authorization import AuthorizationRegistry
from AstralNexus.runtime.credentials.local import LocalCredentialProvider
from AstralNexus.runtime.dshutil.launch_environment import (
    LaunchEnvironmentLayer, create_launch_environment_snapshot)
from AstralNexus.runtime.plugin_host import BUILTIN_MANIFESTS, PluginHost
from AstralNexus.runtime.workbench import WorkbenchStore
from AstralNexus.dashboard.workbench_server import WorkbenchHost

CLIENT = {"locale": "zh-CN", "anonymous_user_id": "anon-1", "client": "workbench",
          "version": "1.0", "timezone_offset_seconds": -480}
CALLBACK_ORIGIN = "http://127.0.0.1:51380"

# A record whose kind the account provider does not own. The credentials
# document model admits api-key and grant; an api-key squatting on the
# account key is the realistic foreign-kind arrival, and only the account
# layer can refuse it (the reader tolerates it by design).
RAW_API_KEY_KIND = """version: 1
records:
  deepseek-account-platform/default:
    kind: api-key
    key: sk-foreign
"""


def envelope(biz_data: Any, status: int = 200) -> tuple:
    return (status, json.dumps({"code": 0, "data": {"biz_code": 0,
                                                    "biz_data": biz_data}}).encode("utf-8"))


class FakePlatform:
    """A scripted Platform: PKCE endpoints, details queries, logout."""

    def __init__(self):
        self.init_body: Optional[Dict[str, Any]] = None
        self.exchange_body: Optional[Dict[str, Any]] = None
        self.cancel_body: Optional[Dict[str, Any]] = None
        self.logout_hits: List[str] = []
        self.current_hits = 0
        self.expires_in = 60.0
        self.token = "token-1"
        self.user: Any = {"id": "u1", "email": "e@x.com", "id_profile": {"name": "N"}}
        self.init_fails = False
        self.exchange_fails = False
        self.exchange_delay = 0.0
        self.details_401 = False
        self.details_fails = False
        self.summary = {"normal_wallets": [{"currency": "CNY", "balance": "3.50"}],
                        "bonus_wallets": []}
        self.bonuses: List[Dict[str, Any]] = []
        outer = self

        def route(path: str, body, headers):
            if path == "/auth-api/v0/dsh/auth_init":
                outer.init_body = body
                if outer.init_fails:
                    return (500, b"")
                return envelope({"authorize_url": f"{outer.origin}/dsh/authorize?sid=9",
                                 "authorize_id": "auth-1", "expires_in": outer.expires_in})
            if path == "/auth-api/v0/dsh/auth_exchange":
                outer.exchange_body = body
                if outer.exchange_delay:
                    time.sleep(outer.exchange_delay)
                if outer.exchange_fails:
                    return (500, b"")
                return envelope({"token": outer.token,
                                 "authorized_url": f"{outer.origin}/dsh/authorized?sid=9",
                                 "user": outer.user})
            if path == "/auth-api/v0/dsh/auth_cancel":
                outer.cancel_body = body
                return envelope(None)
            if path == "/auth-api/v0/users/logout":
                outer.logout_hits.append(headers.get("x-dsh-auth-token"))
                return envelope(None)
            if path == "/auth-api/v0/users/current":
                outer.current_hits += 1
                if outer.details_401:
                    return (401, b"")
                if outer.details_fails:
                    return (500, b"")
                return envelope(outer.user)
            if path == "/api/v0/users/get_user_summary":
                if outer.details_401:
                    return (401, b"")
                return envelope(outer.summary)
            if path == "/api/v0/users/get_unnotified_bonuses":
                if outer.details_401:
                    return (401, b"")
                return envelope(outer.bonuses)
            if path == "/api/v0/users/ack_bonus_notified":
                if outer.details_401:
                    return (401, b"")
                return envelope(None)
            return (404, b"")

        self._route = route

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, body):
                payload = json.loads(body.decode("utf-8")) if body else None
                status, response = outer._route(self.path, payload, self.headers)
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def do_GET(self):
                self._handle(None)

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                self._handle(self.rfile.read(length) if length else None)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture()
def env(tmp_path):
    platform = FakePlatform()
    credentials = LocalCredentialProvider(
        {"path": str(tmp_path / "host" / ".credentials.yaml"), "watch": False,
         "debounceMs": 30},
        launch_environment=create_launch_environment_snapshot(
            [LaunchEnvironmentLayer("process", {})]))
    authorization = AuthorizationRegistry(credentials=lambda: credentials)
    channel = OAuthCallbackChannel()
    account = PlatformAccount(
        credentials=lambda: credentials, authorization=authorization,
        callback_channel=channel,
        config={"platform_origin": platform.origin, "allow_loopback_http": True,
                "request_timeout_ms": 10000, "balance_timeout_ms": 10000,
                "attempt_timeout_ms": 20000, "logout_retry_delay_ms": 1})
    account.start()  # subscribes the record-updated listener like the plugin does
    yield SimpleNamespace(platform=platform, credentials=credentials,
                          authorization=authorization, channel=channel, account=account,
                          tmp_path=tmp_path)
    account.close()
    credentials.close()
    platform.close()


def write_raw(env, text: str) -> None:
    """Write the document behind the provider's back, then reconcile.

    The fixture runs watch=False, so an external edit is invisible until
    reconcile_from_disk() — exactly the path the watcher would take.
    """
    import pathlib
    target = pathlib.Path(env.credentials.spec["filename"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    env.credentials.reconcile_from_disk()


def wait_phase(env, phase: str, timeout: float = 8.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = env.account.get_state()
        attempt = state.get("attempt")
        if attempt is not None and attempt.get("phase") == phase:
            return state
        time.sleep(0.01)
    raise AssertionError(f"attempt never reached {phase!r}: {env.account.get_state()!r}")


def browser_callback(env, code: str = "the-code", state: Optional[str] = None,
                     method: str = "GET", path: str = "/oauth/callback",
                     pairs: Optional[List[tuple]] = None) -> tuple:
    """Run the held callback dispatch on its own thread (it blocks by design)."""
    query = pairs if pairs is not None else [
        ("code", code),
        ("state", state if state is not None else env.platform.init_body["state"]),
    ]
    box: Dict[str, Any] = {}

    def runner():
        box["spec"] = env.channel.dispatch(method, path, query)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, box


def join_spec(thread, box, timeout=8.0):
    thread.join(timeout)
    assert not thread.is_alive(), "callback response never settled"
    return box["spec"]


def start_sign_in(env, login_source: str = "web") -> Dict[str, Any]:
    return env.account.start_sign_in(CLIENT, CALLBACK_ORIGIN, login_source)


def grant(env, token: str = "token-1", issuer: Optional[str] = None):
    env.credentials.modify_record(KEY, lambda current: {
        "kind": "grant",
        "payload": {"version": 1, "token": token, "issuer": issuer or env.platform.origin}})


# ------------------------------------------------------------------- boot


class TestBoot:
    def test_start_discards_issuer_mismatch(self, env, caplog):
        import logging
        grant(env, issuer="https://moved.example")
        with caplog.at_level(logging.INFO, logger="astralnexus.account"):
            env.account.start()
        assert env.credentials.read_record(KEY) is None
        assert "issuer-mismatch" in caplog.text

    def test_start_keeps_matching_grant(self, env):
        grant(env)
        env.account.start()
        assert env.credentials.read_record(KEY)["payload"]["token"] == "token-1"

    def test_start_rejects_foreign_kind(self, env):
        write_raw(env, RAW_API_KEY_KIND)
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.start()
        assert info.value.code == "storage"


# ------------------------------------------------------------------ state


class TestState:
    def test_signed_out_and_links(self, env):
        state = env.account.get_state()
        assert state["status"] == "signed-out" and state["attempt"] is None
        assert state["links"] == {"usageUrl": f"{env.platform.origin}/usage",
                                  "topUpUrl": f"{env.platform.origin}/top_up"}

    def test_credential_stored_and_foreign_kind(self, env):
        grant(env)
        assert env.account.get_state()["status"] == "credential-stored"
        write_raw(env, RAW_API_KEY_KIND)
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.get_state()
        assert info.value.code == "storage"

    def test_view_is_a_copy_and_stale_success_hidden(self, env):
        state = run_full_sign_in(env)
        assert state["attempt"]["phase"] == "succeeded"
        env.credentials.delete_record(KEY)  # sign-out elsewhere
        hidden = env.account.get_state()
        assert hidden["status"] == "signed-out" and hidden["attempt"] is None
        state["attempt"]["phase"] = "hacked"
        assert env.account.get_state()["attempt"] is None  # projection isolation


# ------------------------------------------------------- sign-in full leg


def run_full_sign_in(env, login_source: str = "web") -> Dict[str, Any]:
    start_sign_in(env, login_source)
    state = wait_phase(env, "waiting-browser")
    thread, box = browser_callback(env)
    spec = join_spec(thread, box)
    wait_phase(env, "succeeded")
    env._last_spec = spec
    return env.account.get_state()


class TestSignInSuccess:
    def test_pkce_leg_commits_grant_and_redirects(self, env):
        state = run_full_sign_in(env)
        platform = env.platform
        # init carried the S256 challenge, state, exact redirect and wire locale
        assert platform.init_body["code_challenge_method"] == "S256"
        assert platform.init_body["redirect_uri"] == f"{CALLBACK_ORIGIN}/oauth/callback"
        assert platform.init_body["locale"] == "zh_CN"
        assert platform.init_body["login_source"] == "web"
        # exchange carried the verifier whose SHA-256 IS the init challenge
        verifier = platform.exchange_body["code_verifier"]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        assert platform.init_body["code_challenge"] == \
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        assert platform.exchange_body["code"] == "the-code"
        assert platform.exchange_body["redirect_uri"] == platform.init_body["redirect_uri"]
        uuid.UUID(platform.exchange_body["device_id"])  # persistent device id
        # the held browser response became the completion redirect
        status, headers, body = env._last_spec
        assert status == 302 and body == b""
        assert headers["location"] == (f"{platform.origin}/dsh/authorized?sid=9"
                                       "&login_source=web")
        assert headers["cache-control"] == "no-store"
        # the grant is stored under the exact issuer
        record = env.credentials.read_record(KEY)
        assert record == {"kind": "grant",
                          "payload": {"version": 1, "token": "token-1",
                                      "issuer": platform.origin}}
        assert state["status"] == "credential-stored"
        assert state["attempt"]["phase"] == "succeeded"
        assert "authorizeUrl" not in state["attempt"]  # only waiting-browser carries it
        assert "token" not in json.dumps(state)  # wire-safe view

    def test_waiting_browser_view_carries_validated_authorize_url(self, env):
        start_sign_in(env)
        state = wait_phase(env, "waiting-browser")
        attempt = state["attempt"]
        assert attempt["authorizeUrl"] == f"{env.platform.origin}/dsh/authorize?sid=9"
        assert isinstance(attempt["expiresAt"], int)
        env.account.cancel_sign_in(attempt["id"])

    def test_device_record_created_once(self, env):
        run_full_sign_in(env)
        device = env.credentials.read_record(DEVICE)
        assert device["kind"] == "grant"
        env.account.sign_out(CLIENT)
        run_full_sign_in(env)
        assert env.credentials.read_record(DEVICE) == device  # stable device id

    def test_initial_profile_consumed_once(self, env):
        run_full_sign_in(env)
        before = env.platform.current_hits
        first = env.account.get_profile(CLIENT)
        assert first == {"status": "ready",
                         "value": {"id": "u1", "avatarUrl": None, "name": "N",
                                   "contact": "e@x.com"}}
        assert env.platform.current_hits == before  # served from the exchange user
        second = env.account.get_profile(CLIENT)
        assert env.platform.current_hits == before + 1  # second read hits the wire
        assert second["status"] == "ready"

    def test_malformed_exchange_user_never_blocks_login(self, env):
        env.platform.user = {"id": 123}  # invalid: id must be a string
        state = run_full_sign_in(env)
        assert state["attempt"]["phase"] == "succeeded"
        env.account.get_profile(CLIENT)  # falls through to the wire, still works

    def test_active_attempt_is_never_restarted(self, env):
        start_sign_in(env)
        first = wait_phase(env, "waiting-browser")["attempt"]["id"]
        again = start_sign_in(env)
        assert again["attempt"]["id"] == first  # the live attempt owns the flow


# ---------------------------------------------------------- callback rules


class TestCallbackRules:
    def test_bad_requests_never_reach_the_flow(self, env):
        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        good_state = env.platform.init_body["state"]
        for method, path, pairs in [
            ("GET", "/oauth/callback", [("code", "c"), ("state", "wrong")]),
            ("GET", "/oauth/callback", [("state", good_state)]),          # no code
            ("GET", "/oauth/callback", [("code", "a"), ("code", "b"),
                                        ("state", good_state)]),          # duplicate
            ("POST", "/oauth/callback", [("code", "c"), ("state", good_state)]),
            ("GET", "/other", [("code", "c"), ("state", good_state)]),    # exact path
        ]:
            status, headers, _ = env.channel.dispatch(method, path, pairs)
            assert status == 400 and headers["cache-control"] == "no-store"
        assert wait_phase(env, "waiting-browser")["attempt"]["phase"] == "waiting-browser"
        env.account.cancel_sign_in(wait_phase(env, "waiting-browser")["attempt"]["id"])

    def test_late_callback_after_settle_finds_no_handler(self, env):
        run_full_sign_in(env)
        # The attempt settled, so its exact-route handler was disposed —
        # faithfully, the channel answers 404 like any unmounted path.
        status, headers, _ = env.channel.dispatch(
            "GET", "/oauth/callback",
            [("code", "c"), ("state", env.platform.init_body["state"])])
        assert status == 404 and headers["cache-control"] == "no-store"

    def test_second_callback_while_held_is_gone(self, env):
        env.platform.exchange_delay = 0.5
        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        first, first_box = browser_callback(env)
        time.sleep(0.1)  # the first callback is held mid-exchange
        status, headers, _ = env.channel.dispatch(
            "GET", "/oauth/callback",
            [("code", "c2"), ("state", env.platform.init_body["state"])])
        assert status == 410 and headers["cache-control"] == "no-store"
        assert join_spec(first, first_box)[0] == 302
        wait_phase(env, "succeeded")

    def test_callback_after_cancel_finds_no_handler(self, env):
        start_sign_in(env)
        ident = wait_phase(env, "waiting-browser")["attempt"]["id"]
        env.account.cancel_sign_in(ident)  # joins the attempt, disposing the handler
        status, _, _ = env.channel.dispatch(
            "GET", "/oauth/callback",
            [("code", "c"), ("state", env.platform.init_body["state"])])
        assert status == 404

    def test_no_handler_means_404(self, env):
        status, headers, _ = env.channel.dispatch("GET", "/oauth/callback", [])
        assert status == 404 and headers["cache-control"] == "no-store"

    def test_callback_is_held_until_the_attempt_settles(self, env):
        env.platform.exchange_delay = 0.4
        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        thread, box = browser_callback(env)
        thread.join(0.15)
        assert thread.is_alive() and "spec" not in box  # still held mid-exchange
        spec = join_spec(thread, box)
        assert spec[0] == 302  # released with the completion redirect
        wait_phase(env, "succeeded")


# ----------------------------------------------------------------- cancel


class TestCancel:
    def test_cancel_withdraws_and_discards_remote_authorization(self, env):
        start_sign_in(env)
        state = wait_phase(env, "waiting-browser")
        ident = state["attempt"]["id"]
        result = env.account.cancel_sign_in(ident)
        assert result["attempt"]["phase"] == "cancelled"
        deadline = time.time() + 5
        while env.platform.cancel_body is None and time.time() < deadline:
            time.sleep(0.02)
        assert env.platform.cancel_body is not None  # Platform told to discard
        assert env.platform.cancel_body["authorize_id"] == "auth-1"
        assert len(env.platform.cancel_body["code_verifier"]) == 43
        assert env.authorization.list()[0]["in_flight"] is False
        assert env.credentials.read_record(KEY) is None  # nothing was committed

    def test_cancel_with_foreign_id_is_a_noop(self, env):
        start_sign_in(env)
        state = wait_phase(env, "waiting-browser")
        env.account.cancel_sign_in("some-other-id")
        assert env.account.get_state()["attempt"]["phase"] == "waiting-browser"
        env.account.cancel_sign_in(state["attempt"]["id"])

    def test_authorization_seam_cancel_withdraws_too(self, env):
        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        assert env.authorization.cancel(KEY) is True
        deadline = time.time() + 5
        while env.account.get_state()["attempt"]["phase"] != "cancelled" \
                and time.time() < deadline:
            time.sleep(0.02)
        assert env.account.get_state()["attempt"]["phase"] == "cancelled"

    def test_begin_without_attempt_is_protocol(self, env):
        with pytest.raises(proto.PlatformAuthError) as info:
            env.authorization.begin({"key": KEY,
                                     "interaction": {"notify": lambda notice: None,
                                                     "prompt": lambda question: None}})
        assert info.value.code == "protocol"


# ---------------------------------------------------------------- deadline


class TestDeadline:
    def test_expires_in_shortens_the_attempt(self, env):
        env.platform.expires_in = 0.25
        start_sign_in(env)
        state = wait_phase(env, "expired", timeout=10)
        assert state["attempt"]["errorCode"] == "expired"
        assert env.credentials.read_record(KEY) is None

    def test_exchange_after_deadline_is_expired(self, env):
        env.platform.exchange_delay = 1.2
        env.platform.expires_in = 0.4  # deadline fires while exchanging
        start_sign_in(env)  # web source: the held callback gets the HTML page
        wait_phase(env, "waiting-browser")
        thread, box = browser_callback(env)
        status, headers, body = join_spec(thread, box, timeout=15)
        assert status == 200 and b"window.close()" in body
        assert headers["cache-control"] == "no-store"
        state = wait_phase(env, "expired", timeout=15)
        assert state["attempt"]["errorCode"] == "expired"


# ---------------------------------------------------------------- failures


class TestFailures:
    def test_init_failure_failed_phase(self, env):
        env.platform.init_fails = True
        start_sign_in(env)
        state = wait_phase(env, "failed")
        assert state["attempt"]["errorCode"] == "network"

    def test_web_failure_page_is_self_closing_and_nonced(self, env):
        env.platform.exchange_fails = True
        start_sign_in(env, "web")
        wait_phase(env, "waiting-browser")
        thread, box = browser_callback(env)
        status, headers, body = join_spec(thread, box)
        wait_phase(env, "failed")
        assert status == 200
        text = body.decode("utf-8")
        assert "登录失败，请关闭此标签页并在原页面重试。" in text
        assert "window.close()" in text
        policy = headers["content-security-policy"]
        assert policy.startswith("default-src 'none'; script-src 'nonce-")
        nonce = policy.split("script-src 'nonce-")[1].split("'")[0]
        assert f'<script nonce="{nonce}">' in text  # the script matches the CSP nonce
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"

    def test_non_web_failure_is_204(self, env):
        env.platform.exchange_fails = True
        start_sign_in(env, "qrcode")
        wait_phase(env, "waiting-browser")
        thread, box = browser_callback(env)
        assert join_spec(thread, box)[0] == 204
        wait_phase(env, "failed")

    def test_commit_failure_is_storage(self, env, monkeypatch):
        original = env.credentials.modify_record

        def broken(key, decide):
            if key == KEY:
                raise OSError("disk gone")
            return original(key, decide)

        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        monkeypatch.setattr(env.credentials, "modify_record", broken)
        thread, box = browser_callback(env)
        join_spec(thread, box)
        state = wait_phase(env, "failed")
        assert state["attempt"]["errorCode"] == "storage"
        assert env.credentials.read_record(KEY) is None


# ------------------------------------------------------------ resolve_token


class TestResolveToken:
    def test_production_inference_never_accepts_loopback_grant(self, env):
        run_full_sign_in(env)
        # env talks to a loopback fake while inferenceOrigin stays production:
        assert env.account.resolve_token("https://api.deepseek.com/chat") is None

    def test_configured_inference_releases_the_token(self, env):
        inference = FakePlatform()  # only its origin matters
        authorization = AuthorizationRegistry(credentials=lambda: env.credentials)
        account = PlatformAccount(
            credentials=lambda: env.credentials, authorization=authorization,
            callback_channel=env.channel,
            config={"platform_origin": env.platform.origin, "allow_loopback_http": True,
                    "inference_origin": inference.origin, "request_timeout_ms": 5000,
                    "attempt_timeout_ms": 5000})
        try:
            grant(env)
            netloc = inference.origin.split("//", 1)[1]
            assert account.resolve_token(f"{inference.origin}/chat") == "token-1"
            assert account.resolve_token(f"{inference.origin}/chat?a=1") == "token-1"
            assert account.resolve_token("https://api.deepseek.com/chat") is None
            assert account.resolve_token(f"http://u:p@{netloc}/chat") is None
            grant(env, token="dsh_mock_x")
            assert account.resolve_token(f"{inference.origin}/chat") is None
            with pytest.raises(ValueError):
                account.resolve_token("/relative")
            account.close()
            grant(env)
            assert account.resolve_token(f"{inference.origin}/chat") is None  # closed
        finally:
            account.close()
            inference.close()

    def test_foreign_kind_is_storage(self, env):
        write_raw(env, RAW_API_KEY_KIND)
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.resolve_token("https://api.deepseek.com/chat")
        assert info.value.code == "storage"


# ------------------------------------------------------- expiry / rejection


class TestExpiry:
    def events(self, env):
        seen = []
        env.account.on("deepseek-account/session-expired", lambda: seen.append("expired"))
        env.account.on("deepseek-account/signed-out", lambda: seen.append("out"))
        return seen

    def test_reject_token_only_kills_the_matching_grant(self, env):
        run_full_sign_in(env)
        seen = self.events(env)
        env.account.reject_token("some-other-token")
        assert env.credentials.read_record(KEY) is not None and seen == []
        env.account.reject_token("token-1")
        assert env.credentials.read_record(KEY) is None
        assert seen == ["expired", "out"]
        assert env.account.get_state()["status"] == "signed-out"

    def test_details_401_expires_the_credential(self, env):
        run_full_sign_in(env)
        env.account.get_profile(CLIENT)  # populate the cache
        seen = self.events(env)
        env.platform.details_401 = True
        assert env.account.get_profile(CLIENT) is None
        assert env.credentials.read_record(KEY) is None
        assert seen == ["expired", "out"]
        env.platform.details_401 = False
        grant(env)
        env.platform.details_401 = True
        assert env.account.get_balance(CLIENT) is None
        assert env.credentials.read_record(KEY) is None

    def test_failed_profile_falls_back_to_matching_cache_only(self, env):
        run_full_sign_in(env)
        assert env.account.get_profile(CLIENT)["status"] == "ready"  # caches token-1
        env.platform.details_fails = True
        cached = env.account.get_profile(CLIENT)
        assert cached["status"] == "ready"  # cache token matches the stored grant
        env.credentials.modify_record(KEY, lambda current: {
            "kind": "grant",
            "payload": {"version": 1, "token": "token-2", "issuer": env.platform.origin}})
        stale = env.account.get_profile(CLIENT)
        assert stale == {"status": "failed"}  # never answer for another credential

    def test_stale_generation_never_expires_a_new_credential(self, env):
        run_full_sign_in(env)
        generation = env.account._details_generation
        env.credentials.modify_record(KEY, lambda current: {
            "kind": "grant",
            "payload": {"version": 1, "token": "token-2", "issuer": env.platform.origin}})
        env.account._expire_credential("token-1", generation)  # stale by now
        record = env.credentials.read_record(KEY)
        assert record["payload"]["token"] == "token-2"  # the new grant survives


# ------------------------------------------------------------------ sign-out


class TestSignOut:
    def test_sign_out_revokes_in_background_and_emits(self, env):
        run_full_sign_in(env)
        seen = self.events(env) if hasattr(self, "events") else []
        seen = []
        env.account.on("deepseek-account/signed-out", lambda: seen.append("out"))
        state = env.account.sign_out(CLIENT)
        assert state["status"] == "signed-out" and state["attempt"] is None
        assert env.credentials.read_record(KEY) is None
        assert seen == ["out"]
        deadline = time.time() + 5
        while not env.platform.logout_hits and time.time() < deadline:
            time.sleep(0.02)
        assert env.platform.logout_hits == ["token-1"]  # bounded remote revocation

    def test_sign_out_without_credential_is_clean(self, env):
        state = env.account.sign_out(CLIENT)
        assert state["status"] == "signed-out" and env.platform.logout_hits == []

    def test_sign_out_rejects_foreign_kind_and_issuer(self, env):
        write_raw(env, RAW_API_KEY_KIND)
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.sign_out(CLIENT)
        assert info.value.code == "storage"
        grant(env, issuer="https://moved.example")
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.sign_out(CLIENT)
        assert info.value.code == "protocol"

    def test_sign_out_after_close_is_protocol(self, env):
        env.account.close()
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.sign_out(CLIENT)
        assert info.value.code == "protocol"

    def test_single_flight_runs_the_body_once(self, env):
        calls = []
        barrier = threading.Barrier(2, timeout=5)

        def body():
            calls.append(1)
            time.sleep(0.2)
            return {"status": "signed-out"}

        results = []

        def worker():
            barrier.wait()
            results.append(env.account._single_flight(body))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert len(calls) == 1 and results == [{"status": "signed-out"}] * 2

    def test_sign_out_waits_for_a_committing_attempt(self, env):
        env.platform.exchange_delay = 0.5
        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        thread, box = browser_callback(env)
        wait_phase(env, "committing")
        state = env.account.sign_out(CLIENT)  # must not interrupt the commit
        spec = join_spec(thread, box)
        assert spec[0] == 302
        assert state["status"] == "signed-out"


# ------------------------------------------------------- details / session


class TestDetailsAndSession:
    def test_balance_and_session_projections(self, env):
        run_full_sign_in(env)
        balance = env.account.get_balance(CLIENT)
        assert balance == {"status": "ready",
                           "value": [{"currency": "CNY", "balance": "3.50"}],
                           "bonusWallets": []}
        env.account.get_profile(CLIENT)  # caches the identity
        session = env.account.get_platform_session()
        assert session["origin"] == env.platform.origin
        assert session["token"] == "token-1" and session["userId"] == "u1"
        assert session["requestHeaders"] == {}
        env.account.sign_out(CLIENT)
        assert env.account.get_platform_session() is None

    def test_bonus_notification_flow(self, env):
        order = str(uuid.uuid4())
        env.platform.bonuses = [{"order_id": order, "campaign": "c", "amount": "1.00",
                                 "currency": "USD", "granted_at": "t1", "expires_at": "t2",
                                 "msg": "m"}]
        run_full_sign_in(env)
        result = env.account.get_unnotified_bonuses(CLIENT)
        assert result["accountId"] == "u1"
        assert result["bonuses"][0]["orderId"] == order
        assert env.account.ack_bonus_notified("someone-else", order, CLIENT) is False
        assert env.account.ack_bonus_notified("u1", order, CLIENT) is True
        env.platform.details_401 = True
        assert env.account.get_unnotified_bonuses(CLIENT) is None
        assert env.credentials.read_record(KEY) is None  # the 401 expired it

    def test_record_update_invalidates_and_notifies(self, env):
        changes = []
        env.account.on_changed(lambda: changes.append(1))
        grant(env)  # external credential write
        deadline = time.time() + 5
        while not changes and time.time() < deadline:
            time.sleep(0.02)
        assert changes  # record-updated on KEY reaches the change surface
        assert env.account._last_profile is None

    def test_identity_without_id_is_protocol(self, env):
        run_full_sign_in(env)
        env.platform.user = {"email": "e@x.com"}  # no id at all
        env.credentials.modify_record(KEY, lambda current: {
            "kind": "grant",
            "payload": {"version": 1, "token": "token-9", "issuer": env.platform.origin}})
        with pytest.raises(proto.PlatformAuthError) as info:
            env.account.get_unnotified_bonuses(CLIENT)
        assert info.value.code == "protocol"


# ------------------------------------------------------------------- close


class TestClose:
    def test_close_cancels_waiting_attempt_and_unwires(self, env):
        start_sign_in(env)
        wait_phase(env, "waiting-browser")
        env.account.close()
        assert env.account.get_state()["attempt"]["phase"] == "cancelled"
        assert env.authorization.list() == []  # the flow was unregistered
        status, _, _ = env.channel.dispatch("GET", "/oauth/callback", [])
        assert status == 404  # the callback handler was disposed

    def test_close_is_idempotent(self, env):
        env.account.close()
        env.account.close()
        assert env.account.is_closed()


# ------------------------------------------------------------------ plugin


def host_plugin_env(tmp_path, platform):
    host = WorkbenchHost(tmp_path / "host")
    host.plugins.put_config("credentials-local", {
        "path": str(tmp_path / "host" / ".credentials.yaml"), "watch": False})
    host.plugins.put_config("deepseek-account-platform", {
        "platform_origin": platform.origin, "allow_loopback_http": True,
        "request_timeout_ms": 5000, "attempt_timeout_ms": 8000,
        "logout_retry_delay_ms": 1})
    return host


def inventory(plugins):
    return {item["id"]: item for item in plugins.inventory()}


class TestPluginIntegration:
    def test_builtin_is_seeded_disabled_with_dependency(self, tmp_path):
        plugins = PluginHost(WorkbenchStore(tmp_path / "host"))
        plugins.seed_builtins(BUILTIN_MANIFESTS)
        account = inventory(plugins)["deepseek-account-platform"]
        assert account["desired"] == "disabled" and account["phase"] == "disposed"
        manifest = next(item for item in BUILTIN_MANIFESTS
                        if item["id"] == "deepseek-account-platform")
        assert manifest["dependencies"] == ["credentials-local"]
        assert manifest["kind"] == "native" and manifest["service"] == "deepseek-account"

    def test_enable_cascades_and_a_bare_host_fails_loud(self, tmp_path):
        # Enabling the account cascades to its credentials dependency; a host
        # without the authorization service still fails loud at load — never
        # a silently "active" account seam.
        plugins = PluginHost(WorkbenchStore(tmp_path / "host"))
        plugins.seed_builtins(BUILTIN_MANIFESTS)
        plugins.put_config("credentials-local", {
            "path": str(tmp_path / "host" / ".credentials.yaml"), "watch": False})
        result = plugins.set_enabled("deepseek-account-platform", True)
        assert set(result["affected"]) >= {"credentials-local", "deepseek-account-platform"}
        items = inventory(plugins)
        assert items["credentials-local"]["phase"] == "active"
        account = items["deepseek-account-platform"]
        assert account["phase"] == "failed"
        assert "no authorization service" in (account.get("last_error") or "")

    def test_plugin_lifecycle_publishes_and_withdraws_the_seam(self, tmp_path):
        platform = FakePlatform()
        host = host_plugin_env(tmp_path, platform)
        try:
            host.plugins.set_enabled("credentials-local", True)
            result = host.plugins.set_enabled("deepseek-account-platform", True)
            assert "deepseek-account-platform" in result["affected"]
            assert inventory(host.plugins)["deepseek-account-platform"]["phase"] == "active"
            account = host.plugins.services.get("deepseek-account")
            assert isinstance(account, PlatformAccount)
            assert account.origin == platform.origin
            assert KEY in {flow["key"] for flow in host.authorization.list()}
            # the full loop through the published seam
            account.start_sign_in(CLIENT, CALLBACK_ORIGIN, "web")
            state = wait_phase(SimpleNamespace(account=account), "waiting-browser")
            account.cancel_sign_in(state["attempt"]["id"])
            assert account.get_state()["attempt"]["phase"] == "cancelled"
            # disable closes the provider and withdraws both registrations
            host.plugins.set_enabled("deepseek-account-platform", False)
            assert inventory(host.plugins)["deepseek-account-platform"]["phase"] == "disposed"
            assert "deepseek-account" not in host.plugins.services
            assert account.is_closed()
            assert KEY not in {flow["key"] for flow in host.authorization.list()}
        finally:
            host.close()
            platform.close()

    def test_bad_config_fails_loud(self, tmp_path):
        platform = FakePlatform()
        host = host_plugin_env(tmp_path, platform)
        try:
            host.plugins.set_enabled("credentials-local", True)
            # schema-legal but semantically invalid: a non-loopback HTTP
            # origin without the opt-in must fail the load, not the seed.
            host.plugins.put_config("deepseek-account-platform", {
                "platform_origin": "http://10.0.0.1:80"})
            host.plugins.set_enabled("deepseek-account-platform", True)
            account = inventory(host.plugins)["deepseek-account-platform"]
            assert account["phase"] == "failed"
            assert "HTTPS origin" in (account.get("last_error") or "")
        finally:
            host.close()
            platform.close()


# -------------------------------------------------------------- HTTP surface


class TestWorkbenchHttp:
    def _enabled_host(self, tmp_path, platform):
        host = host_plugin_env(tmp_path / "http-host-dir", platform)
        host.plugins.set_enabled("credentials-local", True)
        host.plugins.set_enabled("deepseek-account-platform", True)
        return host

    def test_projection_unavailable_when_disabled(self, tmp_path):
        host = WorkbenchHost(tmp_path / "http-off")
        try:
            client = host.app.test_client()
            assert client.get("/api/host/account").get_json() == {"available": False}
            assert client.get("/api/host").get_json()["account"] == {"available": False}
            response = client.get("/oauth/callback?code=c&state=s")
            assert response.status_code == 404  # route mounted, no handler
            assert response.headers["cache-control"] == "no-store"
            error = client.post("/api/host/account/sign-in",
                                json={"client": CLIENT, "login_source": "web"})
            assert error.status_code >= 400  # fail loud, never a silent success
        finally:
            host.close()

    def test_http_sign_in_loop_and_sign_out(self, tmp_path):
        platform = FakePlatform()
        host = self._enabled_host(tmp_path, platform)
        try:
            client = host.app.test_client()
            started = client.post("/api/host/account/sign-in",
                                  headers={"Host": "127.0.0.1:51380"},
                                  json={"client": CLIENT, "login_source": "web"})
            assert started.status_code == 200, started.get_json()
            state = wait_phase(
                SimpleNamespace(account=host.plugins.services["deepseek-account"]),
                "waiting-browser")
            # the browser leg through the REAL Flask route (blocking thread)
            box: Dict[str, Any] = {}

            def browse():
                box["response"] = client.get(
                    "/oauth/callback",
                    query_string={"code": "the-code",
                                  "state": platform.init_body["state"]},
                    headers={"Host": "127.0.0.1:51380"})

            thread = threading.Thread(target=browse, daemon=True)
            thread.start()
            thread.join(10)
            assert not thread.is_alive()
            response = box["response"]
            assert response.status_code == 302
            assert response.headers["location"].endswith("/dsh/authorized?sid=9&login_source=web")
            projection = client.get("/api/host/account").get_json()
            assert projection["available"] is True
            assert projection["state"]["status"] == "credential-stored"
            assert "token" not in json.dumps(projection)  # wire-safe
            snapshot = client.get("/api/host").get_json()
            assert snapshot["account"] == projection
            signed_out = client.post("/api/host/account/sign-out",
                                     json={"client": CLIENT})
            assert signed_out.get_json()["state"]["status"] == "signed-out"
        finally:
            host.close()
            platform.close()

    def test_http_validation(self, tmp_path):
        platform = FakePlatform()
        host = self._enabled_host(tmp_path, platform)
        try:
            client = host.app.test_client()
            assert client.post("/api/host/account/sign-in",
                               json={"login_source": "web"}).status_code >= 400
            assert client.post("/api/host/account/sign-in",
                               json={"client": CLIENT, "login_source": "nope"}
                               ).status_code >= 400
            assert client.post("/api/host/account/cancel", json={}).status_code >= 400
            assert client.post("/api/host/account/sign-out", json={}).status_code >= 400
        finally:
            host.close()
            platform.close()

    def test_http_sign_in_rejects_non_loopback_host(self, tmp_path):
        platform = FakePlatform()
        host = self._enabled_host(tmp_path, platform)
        try:
            client = host.app.test_client()
            response = client.post("/api/host/account/sign-in",
                                   headers={"Host": "example.com"},
                                   json={"client": CLIENT, "login_source": "web"})
            assert response.status_code >= 400  # login_origin refuses remote hosts
            error = response.get_json()
            assert "protocol" in json.dumps(error)
        finally:
            host.close()
            platform.close()
