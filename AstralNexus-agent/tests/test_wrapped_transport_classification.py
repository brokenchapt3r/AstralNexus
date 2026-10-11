"""Fix AM: classification must survive a retry loop that re-raises.

Fix AI (Worker) and Fix AL (Verifier) both decided "is this transport?" with
``isinstance``.  That test is defeated the moment a retry loop wraps the cause.
When the circuit-open budget runs out, ``llm_client`` raises a plain
``LLMClientError`` whose message still says so but whose type does not::

    Verifier failed for PATCH-R009-APP-RUNTIME-CLEANUP:
    provider circuit open after 3 retries: provider circuit open for
    https://dashscope.aliyuncs.com/compatible-mode/v1; retry in 21.97s;
    request_stats={...}

Live r13b generation 17.  Both fixes missed it, and a node was judged on a
request that never left the machine.

The re-raise uses ``raise ... from e``, so the original exception sits on the
``__cause__`` chain.  Walking that chain recovers the true category without
matching on message text -- which would silently stop working the next time
someone reworded the message.
"""
from __future__ import annotations

import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest

from AstralNexus.runtime.llm_client import LLMClientError, is_transport_interruption
from AstralNexus.runtime.provider_request_gate import ProviderCircuitOpenError
from email.message import Message


def _wrapped(inner: BaseException, message: str) -> LLMClientError:
    """Reproduce the module's own re-raise."""
    try:
        try:
            raise inner
        except BaseException as exc:
            raise LLMClientError(message) from exc
    except LLMClientError as outer:
        return outer


# --- the live failure --------------------------------------------------------

def test_the_live_wrapped_circuit_open_is_recognized():
    outer = _wrapped(
        ProviderCircuitOpenError(
            "provider circuit open for https://dashscope.aliyuncs.com/compatible-mode/v1; "
            "retry in 21.97s"
        ),
        "provider circuit open after 3 retries: provider circuit open for "
        "https://dashscope.aliyuncs.com/compatible-mode/v1; retry in 21.97s; "
        "request_stats={'message_count': 2}",
    )
    assert not isinstance(outer, ProviderCircuitOpenError), "precondition: the type is lost"
    assert is_transport_interruption(outer)


@pytest.mark.parametrize("inner", [
    ProviderCircuitOpenError("provider circuit open for https://x; retry in 26.0s"),
    URLError(ssl.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING")),
    socket.timeout("The read operation timed out"),
    ConnectionResetError("connection reset by peer"),
])
def test_any_transport_cause_is_recovered_through_the_wrapper(inner):
    assert is_transport_interruption(_wrapped(inner, "failed after 3 retries"))


def test_a_doubly_wrapped_cause_is_still_recovered():
    inner = ProviderCircuitOpenError("provider circuit open for https://x")
    once = _wrapped(inner, "provider circuit open after 3 retries")
    twice = _wrapped(once, "role execution failed")
    assert is_transport_interruption(twice)


def test_an_implicit_context_chain_is_also_walked():
    """``raise X`` inside an except block sets __context__, not __cause__."""
    try:
        try:
            raise socket.timeout("The read operation timed out")
        except socket.timeout:
            raise LLMClientError("giving up")
    except LLMClientError as outer:
        assert is_transport_interruption(outer)


# --- what must NOT be reclassified ------------------------------------------

@pytest.mark.parametrize("exc", [
    ValueError("Execution evidence is stale; owned files changed after the Verifier read them"),
    ValueError("Data validation failed; execution cannot be accepted"),
    KeyError("verdict"),
    RuntimeError("tool router rejected the call"),
    LLMClientError("model returned malformed JSON"),
    HTTPError("https://api.example/v1", 400, "Bad Request", Message(), None),
    HTTPError("https://api.example/v1", 500, "Server Error", Message(), None),
])
def test_real_failures_are_not_reclassified(exc):
    assert not is_transport_interruption(exc)


def test_a_wrapper_over_a_real_failure_stays_a_real_failure():
    outer = _wrapped(ValueError("expected_outputs not produced"), "role execution failed")
    assert not is_transport_interruption(outer)


def test_message_text_alone_never_decides():
    """A model that merely SAYS 'provider circuit open' is not a transport cut."""
    assert not is_transport_interruption(
        ValueError("the worker reported: provider circuit open after 3 retries")
    )


def test_a_cyclic_cause_chain_terminates():
    first = LLMClientError("a")
    second = LLMClientError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert is_transport_interruption(first) is False


def test_both_fix_sites_use_the_chain_aware_classifier():
    from AstralNexus.core import orchestrator as orch
    from AstralNexus.agents import base as agents_base
    from AstralNexus.core.orchestration import execution, verification
    for module in (verification, agents_base):
        source = open(module.__file__, encoding="utf-8").read()
        assert "is_transport_interruption" in source, module.__name__
