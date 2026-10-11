"""Contract tests for AstralNexus.runtime.http_proxy (dsh-http-proxy port).

Covers the pure policy half (env precedence with blank-as-unset, candidate
validation diagnostics, scheme resolution where a rejected slot stays direct,
loopback merging and matching across all of 127/8, bypass list semantics
including the deliberate non-support of CIDR, and the single proxy_for_url
answer) and the install half (process-wide publication through os.environ —
the native transport equivalent of undici's global dispatcher — outermost-
install ownership of the inherited snapshot, child overlay preserving the
user's own values, cleared overlay, and a REAL urllib request observed by a
real loopback proxy server).
"""
from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.runtime import http_proxy
from AstralNexus.runtime.dshutil.launch_environment import (
    LaunchEnvironmentLayer, create_launch_environment_snapshot)


class FakeResolution:
    def __init__(self, value: str):
        self.value = value


class FakeEnv:
    """A minimal launch-environment stand-in with exact-name reads."""

    def __init__(self, values: Dict[str, str]):
        self._values = values

    def get(self, name: str) -> Optional[FakeResolution]:
        value = self._values.get(name)
        return FakeResolution(value) if value is not None else None


def real_env(values: Dict[str, str]):
    return create_launch_environment_snapshot([LaunchEnvironmentLayer("process", values)])


@pytest.fixture()
def proxy_state():
    """Isolate the module singletons and every published env name per test."""
    saved = {name: os.environ.get(name) for name in http_proxy.PROXY_ENV_NAMES}
    http_proxy._active = None
    http_proxy._installed = False
    http_proxy._inherited_proxy_env = None
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    http_proxy._active = None
    http_proxy._installed = False
    http_proxy._inherited_proxy_env = None


class TestReadEnv:
    def test_lowercase_wins_over_uppercase(self) -> None:
        env = FakeEnv({"http_proxy": "http://lower", "HTTP_PROXY": "http://upper"})
        assert http_proxy.read_env(env, "http_proxy") == \
            {"value": "http://lower", "name": "http_proxy"}

    def test_uppercase_is_the_fallback(self) -> None:
        env = FakeEnv({"HTTP_PROXY": "http://upper"})
        assert http_proxy.read_env(env, "http_proxy") == \
            {"value": "http://upper", "name": "HTTP_PROXY"}

    def test_blank_lowercase_does_not_shadow_populated_uppercase(self) -> None:
        # undici's ?? chain quirk: blank counts as unset, so the uppercase
        # value survives — the opposite of a `??` over raw strings.
        env = FakeEnv({"http_proxy": "   ", "HTTP_PROXY": "http://upper"})
        assert http_proxy.read_env(env, "http_proxy") == \
            {"value": "http://upper", "name": "HTTP_PROXY"}

    def test_absent_returns_none(self) -> None:
        assert http_proxy.read_env(FakeEnv({}), "all_proxy") is None

    def test_value_is_trimmed(self) -> None:
        env = FakeEnv({"no_proxy": "  example.com  "})
        assert http_proxy.read_env(env, "no_proxy") == \
            {"value": "example.com", "name": "no_proxy"}


class TestAcceptProxyUrl:
    def test_absent_candidate(self) -> None:
        diagnostics: List[Dict[str, str]] = []
        assert http_proxy.accept_proxy_url(None, diagnostics) == {"kind": "absent"}
        assert diagnostics == []

    def test_unparseable_is_invalid_with_diagnostic(self) -> None:
        diagnostics: List[Dict[str, str]] = []
        result = http_proxy.accept_proxy_url(
            {"value": "not a url", "name": "http_proxy"}, diagnostics)
        assert result == {"kind": "rejected"}
        assert diagnostics == [{"kind": "invalid", "origin": "http_proxy",
                                "message": "http_proxy is not a valid URL; "
                                           "connecting directly"}]

    def test_socks_gets_its_own_diagnostic(self) -> None:
        diagnostics: List[Dict[str, str]] = []
        result = http_proxy.accept_proxy_url(
            {"value": "socks5://proxy:1080", "name": "ALL_PROXY"}, diagnostics)
        assert result["kind"] == "rejected"
        assert diagnostics[0]["kind"] == "socks"
        assert "names a SOCKS proxy" in diagnostics[0]["message"]

    def test_other_scheme_is_invalid_naming_the_scheme(self) -> None:
        diagnostics: List[Dict[str, str]] = []
        http_proxy.accept_proxy_url({"value": "ftp://proxy", "name": "http_proxy"},
                                    diagnostics)
        assert "unsupported ftp// scheme" in diagnostics[0]["message"]

    def test_http_and_https_accepted(self) -> None:
        diagnostics: List[Dict[str, str]] = []
        assert http_proxy.accept_proxy_url({"value": "http://p:3128", "name": "n"},
                                           diagnostics) == \
            {"kind": "accepted", "value": "http://p:3128"}
        assert http_proxy.accept_proxy_url({"value": "https://p", "name": "n"},
                                           diagnostics)["kind"] == "accepted"
        assert diagnostics == []

    def test_is_supported_proxy_url_matches_accept(self) -> None:
        assert http_proxy.is_supported_proxy_url("http://p") is True
        assert http_proxy.is_supported_proxy_url("socks5://p") is False
        assert http_proxy.is_supported_proxy_url("garbage") is False


class TestResolveScheme:
    def test_accepted_wins_over_fallbacks(self) -> None:
        assert http_proxy.resolve_scheme({"kind": "accepted", "value": "own"},
                                         "fallback") == "own"

    def test_rejected_stays_direct_never_falls_back(self) -> None:
        # the diagnostic and the route must agree: a refused slot proxies nothing
        assert http_proxy.resolve_scheme({"kind": "rejected"}, "fallback") is None

    def test_absent_takes_first_present_fallback(self) -> None:
        assert http_proxy.resolve_scheme({"kind": "absent"}, None, "second") == "second"
        assert http_proxy.resolve_scheme({"kind": "absent"}, None, None) is None


class TestWithLoopback:
    def test_empty_list_becomes_the_four_loopback_entries(self) -> None:
        assert http_proxy.with_loopback(None) == "localhost,127.0.0.1,::1,[::1]"
        assert http_proxy.with_loopback("") == "localhost,127.0.0.1,::1,[::1]"

    def test_star_short_circuits(self) -> None:
        assert http_proxy.with_loopback("example.com,*") == "*"

    def test_user_entries_preserved_in_order_and_deduplicated(self) -> None:
        result = http_proxy.with_loopback("example.com, LOCALHOST ,10.0.0.1")
        assert result == "example.com,LOCALHOST,10.0.0.1,127.0.0.1,::1,[::1]"

    def test_whitespace_separators(self) -> None:
        assert http_proxy.with_loopback("a.com b.com").startswith("a.com,b.com,")


class TestSplitHostPort:
    def test_plain_host_and_port(self) -> None:
        assert http_proxy.split_host_port("example.com:8080") == \
            {"host": "example.com", "port": "8080"}
        assert http_proxy.split_host_port("example.com") == \
            {"host": "example.com", "port": None}

    def test_bare_ipv6_is_never_a_port_split(self) -> None:
        # the undici quirk: reading ::1 as host ":" port "1"
        assert http_proxy.split_host_port("::1") == {"host": "::1", "port": None}

    def test_bracketed_ipv6_with_and_without_port(self) -> None:
        assert http_proxy.split_host_port("[::1]") == {"host": "::1", "port": None}
        assert http_proxy.split_host_port("[::1]:443") == {"host": "::1", "port": "443"}


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", [
        "localhost", "LOCALHOST", "a.localhost", "::1", "[::1]", "::", "0.0.0.0",
        "127.0.0.1", "127.0.0.2", "127.255.255.255", "127.1.2.3",
        "::ffff:7f00:1", "::ffff:7f00:0102", "127.0.0.1.",
    ])
    def test_loopback_forms(self, host: str) -> None:
        assert http_proxy.is_loopback_host(host) is True

    @pytest.mark.parametrize("host", [
        "8.8.8.8", "example.com", "128.0.0.1", "127.999.1.1", "1279.0.0.1",
        "::ffff:808:1001", "notlocalhost.com",
    ])
    def test_non_loopback_forms(self, host: str) -> None:
        assert http_proxy.is_loopback_host(host) is False


class TestBypassesProxy:
    def url(self, value: str):
        from urllib.parse import urlparse
        return urlparse(value)

    def test_exact_host_and_subdomains(self) -> None:
        assert http_proxy.bypasses_proxy("example.com",
                                         self.url("http://example.com/x")) is True
        assert http_proxy.bypasses_proxy("example.com",
                                         self.url("http://api.example.com/x")) is True
        assert http_proxy.bypasses_proxy("example.com",
                                         self.url("http://notexample.com/x")) is False

    def test_leading_dot_and_star_dot_are_the_same(self) -> None:
        assert http_proxy.bypasses_proxy(".example.com",
                                         self.url("http://api.example.com/")) is True
        assert http_proxy.bypasses_proxy("*.example.com",
                                         self.url("http://api.example.com/")) is True

    def test_port_narrows(self) -> None:
        assert http_proxy.bypasses_proxy("example.com:8080",
                                         self.url("http://example.com:8080/")) is True
        assert http_proxy.bypasses_proxy("example.com:8080",
                                         self.url("http://example.com/")) is False

    def test_default_ports_are_implied(self) -> None:
        assert http_proxy.bypasses_proxy("example.com:443",
                                         self.url("https://example.com/")) is True
        assert http_proxy.bypasses_proxy("example.com:80",
                                         self.url("http://example.com/")) is True

    def test_star_bypasses_everything(self) -> None:
        assert http_proxy.bypasses_proxy("*", self.url("http://any.where/")) is True

    def test_cidr_is_deliberately_not_matched(self) -> None:
        # an OS bypass list often carries 10.0.0.0/8 — it must NOT match here
        assert http_proxy.bypasses_proxy("10.0.0.0/8",
                                         self.url("http://10.1.2.3/")) is False


class TestResolveProxyPolicy:
    def test_empty_environment_is_direct_without_diagnostics(self) -> None:
        result = http_proxy.resolve_proxy_policy(FakeEnv({}))
        assert result["policy"] == {"no_proxy": "", "source": "none"}
        assert result["diagnostics"] == []

    def test_own_scheme_wins_and_https_falls_back_through_all(self) -> None:
        env = FakeEnv({"http_proxy": "http://own:3128", "all_proxy": "http://all:1080"})
        result = http_proxy.resolve_proxy_policy(env)
        assert result["policy"]["http_proxy"] == "http://own:3128"
        assert result["policy"]["https_proxy"] == "http://all:1080"
        assert result["policy"]["source"] == "env"
        assert result["policy"]["no_proxy"] == "localhost,127.0.0.1,::1,[::1]"

    def test_https_falls_back_to_the_resolved_http_proxy(self) -> None:
        env = FakeEnv({"http_proxy": "http://only-http:3128"})
        result = http_proxy.resolve_proxy_policy(env)
        assert result["policy"]["https_proxy"] == "http://only-http:3128"

    def test_rejected_own_slot_keeps_that_scheme_direct(self) -> None:
        env = FakeEnv({"http_proxy": "socks5://user:1080",
                       "all_proxy": "http://all:1080"})
        result = http_proxy.resolve_proxy_policy(env)
        # http was rejected → stays direct even though ALL_PROXY is usable
        assert "http_proxy" not in result["policy"]
        # https slot was absent → ALL_PROXY fallback applies
        assert result["policy"]["https_proxy"] == "http://all:1080"
        assert [d["kind"] for d in result["diagnostics"]] == ["socks"]
        assert result["diagnostics"][0]["origin"] == "http_proxy"

    def test_all_rejected_is_direct_but_still_reported(self) -> None:
        env = FakeEnv({"http_proxy": "socks5://x", "https_proxy": "ftp://y"})
        result = http_proxy.resolve_proxy_policy(env)
        assert result["policy"]["source"] == "none"
        assert [d["origin"] for d in result["diagnostics"]] == ["http_proxy",
                                                                "https_proxy"]

    def test_diagnostic_order_is_all_then_http_then_https(self) -> None:
        env = FakeEnv({"all_proxy": "bogus", "http_proxy": "socks5://x",
                       "https_proxy": "ftp://y"})
        result = http_proxy.resolve_proxy_policy(env)
        assert [d["origin"] for d in result["diagnostics"]] == \
            ["all_proxy", "http_proxy", "https_proxy"]

    def test_user_no_proxy_entries_are_preserved(self) -> None:
        env = FakeEnv({"http_proxy": "http://p:3128", "no_proxy": "corp.example"})
        result = http_proxy.resolve_proxy_policy(env)
        assert result["policy"]["no_proxy"] == \
            "corp.example,localhost,127.0.0.1,::1,[::1]"

    def test_works_against_the_real_launch_environment(self) -> None:
        env = real_env({"HTTP_PROXY": "http://real:3128"})
        result = http_proxy.resolve_proxy_policy(env)
        assert result["policy"]["http_proxy"] == "http://real:3128"


class TestProxyForUrl:
    POLICY = {"http_proxy": "http://p:3128", "https_proxy": "http://sp:3128",
              "no_proxy": "corp.example,localhost,127.0.0.1,::1,[::1]",
              "source": "env"}

    def parsed(self, value: str):
        from urllib.parse import urlparse
        return urlparse(value)

    def test_schemes_route_to_their_own_proxy(self) -> None:
        assert http_proxy.proxy_for_url(self.POLICY,
                                        self.parsed("http://remote/x")) == "http://p:3128"
        assert http_proxy.proxy_for_url(self.POLICY,
                                        self.parsed("https://remote/x")) == "http://sp:3128"

    def test_other_schemes_are_never_proxied(self) -> None:
        assert http_proxy.proxy_for_url(self.POLICY, self.parsed("ftp://remote/x")) \
            is None

    def test_loopback_hosts_bypass_even_without_list_entries(self) -> None:
        for url in ("http://127.0.0.2:5000/x", "http://[::1]/x", "http://a.localhost/x"):
            assert http_proxy.proxy_for_url(self.POLICY, self.parsed(url)) is None

    def test_bypass_list_exempts(self) -> None:
        assert http_proxy.proxy_for_url(self.POLICY,
                                        self.parsed("http://api.corp.example/x")) is None
        assert http_proxy.proxy_for_url(self.POLICY,
                                        self.parsed("http://elsewhere.com/x")) == \
            "http://p:3128"

    def test_direct_policy_routes_nothing(self) -> None:
        assert http_proxy.proxy_for_url(http_proxy.DIRECT_POLICY,
                                        self.parsed("http://remote/x")) is None


class TestInstall:
    def test_no_policy_means_direct_route(self, proxy_state) -> None:
        from urllib.parse import urlparse
        assert http_proxy.proxy_route_for(urlparse("http://remote/x")) == \
            {"proxied": False}
        assert http_proxy.active_policy() is None

    def test_install_publishes_env_and_routes(self, proxy_state) -> None:
        from urllib.parse import urlparse
        policy = {"http_proxy": "http://p:3128", "https_proxy": "http://sp:3128",
                  "no_proxy": "localhost,127.0.0.1,::1,[::1]", "source": "env"}
        restore = http_proxy.install_global_proxy(policy)
        try:
            assert os.environ["http_proxy"] == "http://p:3128"
            assert os.environ["HTTP_PROXY"] == "http://p:3128"
            assert os.environ["no_proxy"] == "localhost,127.0.0.1,::1,[::1]"
            assert "all_proxy" not in os.environ or os.environ.get("all_proxy") is None
            route = http_proxy.proxy_route_for(urlparse("http://remote.example/x"))
            assert route == {"proxied": True, "proxy": "http://p:3128"}
            assert http_proxy.proxy_route_for(urlparse("http://127.0.0.1/x")) == \
                {"proxied": False}
            assert http_proxy.active_policy() == policy
        finally:
            restore()
        assert os.environ.get("http_proxy") is None
        assert http_proxy.active_policy() is None
        assert http_proxy.proxy_route_for(urlparse("http://remote.example/x")) == \
            {"proxied": False}

    def test_install_restores_previous_user_env(self, proxy_state) -> None:
        os.environ["http_proxy"] = "http://user:1"
        os.environ["NO_PROXY"] = "user.list"
        policy = {"http_proxy": "http://p:3128",
                  "no_proxy": "localhost,127.0.0.1,::1,[::1]", "source": "env"}
        restore = http_proxy.install_global_proxy(policy)
        assert os.environ["http_proxy"] == "http://p:3128"
        restore()
        assert os.environ["http_proxy"] == "http://user:1"
        assert os.environ["NO_PROXY"] == "user.list"

    def test_layered_install_outermost_owns_inherited(self, proxy_state) -> None:
        os.environ["https_proxy"] = "http://user:2"
        outer = {"http_proxy": "http://outer:1",
                 "no_proxy": "localhost,127.0.0.1,::1,[::1]", "source": "env"}
        inner = {"http_proxy": "http://inner:1",
                 "no_proxy": "localhost,127.0.0.1,::1,[::1]", "source": "env"}
        restore_outer = http_proxy.install_global_proxy(outer)
        restore_inner = http_proxy.install_global_proxy(inner)
        assert os.environ["http_proxy"] == "http://inner:1"
        restore_inner()
        # back to the OUTER publication (which cleared the user's https name
        # because the outer policy resolves no https proxy), not a normalized
        # user env
        assert os.environ["http_proxy"] == "http://outer:1"
        assert os.environ.get("https_proxy") is None
        restore_outer()
        assert os.environ.get("http_proxy") is None
        assert os.environ["https_proxy"] == "http://user:2"

    def test_direct_policy_over_a_previous_one_restores_user_env(self, proxy_state) -> None:
        os.environ["http_proxy"] = "http://user:1"
        proxied = {"http_proxy": "http://p:3128",
                   "no_proxy": "localhost,127.0.0.1,::1,[::1]", "source": "env"}
        restore_proxied = http_proxy.install_global_proxy(proxied)
        restore_direct = http_proxy.install_global_proxy(dict(http_proxy.DIRECT_POLICY))
        # the direct install hands back what the USER exported
        assert os.environ["http_proxy"] == "http://user:1"
        assert http_proxy.active_policy() == {"no_proxy": "", "source": "none"}
        restore_direct()
        assert os.environ["http_proxy"] == "http://p:3128"
        restore_proxied()
        assert os.environ["http_proxy"] == "http://user:1"

    def test_first_direct_install_is_a_noop_marker(self, proxy_state) -> None:
        restore = http_proxy.install_global_proxy(dict(http_proxy.DIRECT_POLICY))
        assert http_proxy.active_policy() == {"no_proxy": "", "source": "none"}
        restore()
        assert http_proxy.active_policy() is None


class TestChildEnvironment:
    def test_no_policy_is_an_empty_overlay(self, proxy_state) -> None:
        assert http_proxy.proxy_environment_for_child() == {}

    def test_user_values_kept_resolved_values_filled(self, proxy_state) -> None:
        os.environ["http_proxy"] = "socks5://user-socks:1080"  # kept for curl
        policy = {"http_proxy": "http://p:3128", "https_proxy": "http://sp:3128",
                  "no_proxy": "corp,localhost,127.0.0.1,::1,[::1]", "source": "env"}
        http_proxy.install_global_proxy(policy)
        overlay = http_proxy.proxy_environment_for_child()
        # the user named http in one casing → both names carry THEIR value
        assert overlay["http_proxy"] == "socks5://user-socks:1080"
        assert overlay["HTTP_PROXY"] == "socks5://user-socks:1080"
        # https was never named by the user → the resolved value prevents
        # the child's routing from silently diverging
        assert overlay["https_proxy"] == "http://sp:3128"
        assert overlay["HTTPS_PROXY"] == "http://sp:3128"
        # the bypass list is always the resolved one
        assert overlay["no_proxy"] == "corp,localhost,127.0.0.1,::1,[::1]"
        assert overlay["NO_PROXY"] == "corp,localhost,127.0.0.1,::1,[::1]"

    def test_cleared_overlay_removes_every_name(self, proxy_state) -> None:
        cleared = http_proxy.cleared_proxy_env()
        assert set(cleared) == set(http_proxy.PROXY_ENV_NAMES)
        assert all(value is None for value in cleared.values())
        assert len(cleared) == 8


class TestInstallFromEnvironment:
    def test_reports_in_order_and_installs(self, proxy_state) -> None:
        env = real_env({"all_proxy": "socks5://user:1080",
                        "http_proxy": "http://good:3128"})
        messages: List[str] = []
        restore = http_proxy.install_proxy_from_environment(env, messages.append)
        try:
            assert len(messages) == 1
            assert "all_proxy names a SOCKS proxy" in messages[0]
            assert os.environ["http_proxy"] == "http://good:3128"
            # https: own absent, ALL rejected → the resolved http proxy
            assert os.environ["https_proxy"] == "http://good:3128"
        finally:
            restore()
        assert os.environ.get("http_proxy") is None

    def test_unusable_values_never_stop_the_install(self, proxy_state) -> None:
        env = real_env({"http_proxy": "total garbage", "https_proxy": "ftp://x"})
        messages: List[str] = []
        restore = http_proxy.install_proxy_from_environment(env, messages.append)
        try:
            assert len(messages) == 2
            assert http_proxy.active_policy() == {"no_proxy": "", "source": "none"}
        finally:
            restore()


class _ProxyObserver(BaseHTTPRequestHandler):
    seen: List[str] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).seen.append(self.path)
        self.send_response(502)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:  # silence stderr
        pass


class TestRealTransport:
    def test_urllib_routes_through_the_published_environment(self, proxy_state) -> None:
        # urllib's GLOBAL urlopen opener caches its proxies at first use, so
        # each observation builds a fresh opener — exactly how the account
        # protocol's _opener() consults the environment per request.
        def open_fresh(url: str, timeout: float = 10):
            return urllib.request.build_opener().open(url, timeout=timeout)

        _ProxyObserver.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyObserver)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            policy = {"http_proxy": f"http://127.0.0.1:{port}",
                      "no_proxy": "localhost,127.0.0.1,::1,[::1]",
                      "source": "env"}
            restore = http_proxy.install_global_proxy(policy)
            try:
                # the absolute-form request line lands on the fake proxy
                with pytest.raises(urllib.error.HTTPError) as exc:
                    open_fresh("http://probe.invalid/x")
                assert exc.value.code == 502
                assert _ProxyObserver.seen == ["http://probe.invalid/x"]
                # a loopback URL bypasses: it reaches the fake server directly
                # (origin-form path), proving the bypass list works in transit
                try:
                    open_fresh(f"http://127.0.0.1:{port}/direct", timeout=5)
                except urllib.error.HTTPError:
                    pass
                assert _ProxyObserver.seen == ["http://probe.invalid/x", "/direct"]
            finally:
                restore()
            # after the disposer a fresh opener connects directly: DNS failure
            # for the RFC-2606 .invalid host, and the proxy observes nothing
            before = list(_ProxyObserver.seen)
            with pytest.raises(urllib.error.URLError):
                open_fresh("http://probe.invalid/y", timeout=5)
            assert _ProxyObserver.seen == before
        finally:
            server.shutdown()
            server.server_close()
