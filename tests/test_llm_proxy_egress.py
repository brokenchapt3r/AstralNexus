"""Fix AJ: LLM egress is a declared setting, not an inherited accident.

``urlopen`` uses urllib's default opener, which reads HTTP_PROXY / HTTPS_PROXY
from the environment.  The provider endpoint is region-specific -- dashscope is
an Alibaba Cloud gateway in China -- so a system proxy in another region routes
every token of every stream across the world and back.  That link does not just
run slow, it breaks mid-stream.  Live generation 14, four dead calls in forty
minutes, none of them about the model or the task:

    13:12  <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] ...>
    13:19  The read operation timed out
    13:40  provider circuit open for https://dashscope.aliyuncs.com/...
    13:47  <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] ...>

The default is unchanged, so nothing moves for a deployment that needs its
proxy; a run that must go direct now says so explicitly.
"""
from __future__ import annotations

import os
from urllib.request import ProxyHandler

import pytest

from AstralNexus.runtime import llm_client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ASTRALNEXUS_LLM_PROXY", raising=False)
    llm_client._PROXY_OPENER_CACHE.clear()
    llm_client._PROXY_CHOICE_LOGGED.clear()
    yield
    llm_client._PROXY_OPENER_CACHE.clear()
    llm_client._PROXY_CHOICE_LOGGED.clear()


def _proxy_handlers(opener):
    return [h for h in opener.handlers if isinstance(h, ProxyHandler)]


def _applied_proxies(opener) -> dict:
    """Every proxy this opener could actually route through.

    urllib only registers a ProxyHandler that has something to do: an empty
    proxy map installs no ``<scheme>_open`` method, so ``add_handler`` drops the
    instance entirely.  Passing it to ``build_opener`` still does the work that
    matters -- it displaces the DEFAULT ProxyHandler, the one that reads
    HTTP_PROXY / HTTPS_PROXY -- so "bypassed" means no handler can apply a
    proxy at all, not that an empty handler is present.
    """
    applied = {}
    for handler in _proxy_handlers(opener):
        applied.update(handler.proxies)
    return applied


def test_unset_keeps_urllibs_default_opener():
    """No declaration means no behaviour change for existing deployments."""
    assert llm_client.llm_proxy_setting() == ""
    assert llm_client._llm_opener("") is None


@pytest.mark.parametrize("token", ["direct", "none", "off", "no", "0", "false",
                                   "DIRECT", "None", "Off"])
def test_direct_tokens_bypass_every_proxy(monkeypatch, token):
    monkeypatch.setenv("ASTRALNEXUS_LLM_PROXY", token)
    opener = llm_client._llm_opener(llm_client.llm_proxy_setting())
    assert _applied_proxies(opener) == {}, "a bypassed opener may route through nothing"


def test_an_explicit_proxy_url_is_used_for_both_schemes(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_LLM_PROXY", "http://127.0.0.1:7890")
    opener = llm_client._llm_opener(llm_client.llm_proxy_setting())
    proxies = _applied_proxies(opener)
    assert proxies["http"] == "http://127.0.0.1:7890"
    assert proxies["https"] == "http://127.0.0.1:7890"


def test_a_system_proxy_is_ignored_when_direct_is_declared(monkeypatch):
    """The live condition: a US proxy exported into the shell, a CN endpoint."""
    monkeypatch.setenv("HTTPS_PROXY", "http://us-proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://us-proxy.example:3128")
    from urllib.request import build_opener
    # The default opener is what the code used before Fix AJ: it picks the US
    # proxy up out of the environment on its own.
    assert "https" in _applied_proxies(build_opener())
    monkeypatch.setenv("ASTRALNEXUS_LLM_PROXY", "direct")
    opener = llm_client._llm_opener(llm_client.llm_proxy_setting())
    assert _applied_proxies(opener) == {}


def test_openers_are_cached_per_setting(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_LLM_PROXY", "direct")
    first = llm_client._llm_opener("direct")
    assert llm_client._llm_opener("direct") is first
    assert llm_client._llm_opener("http://other.example:8080") is not first


def test_declared_egress_is_routed_through_the_opener(monkeypatch):
    """llm_urlopen must not fall back to the module-level urlopen."""
    monkeypatch.setenv("ASTRALNEXUS_LLM_PROXY", "direct")
    seen = {}

    class _Opener:
        def open(self, request, timeout=None):
            seen["request"] = request
            seen["timeout"] = timeout
            return "response"

    monkeypatch.setattr(llm_client, "_llm_opener", lambda setting: _Opener())
    monkeypatch.setattr(llm_client, "urlopen", lambda *a, **k: pytest.fail(
        "default opener used despite a declared egress path"))
    assert llm_client.llm_urlopen("req", timeout=12) == "response"
    assert seen == {"request": "req", "timeout": 12}


def test_undeclared_egress_still_uses_urlopen(monkeypatch):
    called = {}

    def _fake(req, timeout=None):
        called["request"] = req
        called["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(llm_client, "urlopen", _fake)
    assert llm_client.llm_urlopen("req", timeout=9) == "ok"
    assert called == {"request": "req", "timeout": 9}
