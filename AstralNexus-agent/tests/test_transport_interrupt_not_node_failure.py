"""Fix AI: a transport cut must not be recorded as the node's failure.

Live regression (doc 18, r13b).  ``LLMRoleAgent`` wrapped its whole execution
in one broad ``except`` that flattened every escaping exception into
``ExecutionOutcome.FAILED``.  A dropped TLS connection and "the model did the
task wrong" therefore produced the identical verdict, and a node killed that
way burns a recovery generation plus recovery churn.  With a flaky provider the
same transport failure recurs, the Governor replans to the same outcome, and
``no_progress_breaker`` fires on what is really a network problem:

    gen 13  PATCH-R004-RUNTIME  SSL: UNEXPECTED_EOF_WHILE_READING  -> failed
    gen 14  PATCH-R005-RUNTIME  SSL: UNEXPECTED_EOF_WHILE_READING  -> failed
    gen 14  PATCH-R005-RUNTIME  The read operation timed out       -> failed

Every bounded retry below this point (stream reconnect, then turn retry) is
already spent when the exception escapes, so the honest classification is
INTERRUPTED -- the node returns to ready with no recovery branch.  The budget
keeps that from hiding a real outage: once spent the node fails normally and
the breaker still fires (项目规则 不能增加次数掩盖问题).
"""
from __future__ import annotations

import http.client
import socket
import ssl
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from AstralNexus.runtime.llm_client import (
    is_incomplete_stream_error,
    is_mid_turn_transport_error,
)
from AstralNexus.runtime.provider_request_gate import ProviderCircuitOpenError

LIVE_SSL_CUT = URLError(
    ssl.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING", "EOF occurred in violation of protocol")
)
LIVE_READ_TIMEOUT = socket.timeout("The read operation timed out")


# --- classification: exactly the two live failures, and nothing more ---------

@pytest.mark.parametrize("exc", [
    LIVE_SSL_CUT,
    LIVE_READ_TIMEOUT,
    ssl.SSLError("bad record mac"),
    ConnectionResetError("connection reset by peer"),
    http.client.IncompleteRead(b""),
    TimeoutError("timed out"),
])
def test_transport_failures_are_classified_as_transport(exc):
    assert is_mid_turn_transport_error(exc)


@pytest.mark.parametrize("exc", [
    ValueError("model returned malformed JSON"),
    KeyError("content"),
    RuntimeError("tool router rejected the call"),
    HTTPError("https://api.example/v1", 400, "Bad Request", Message(), None),
])
def test_task_failures_are_not_reclassified(exc):
    """A wrong answer, a bad payload, or a 4xx must still fail the node."""
    assert not is_mid_turn_transport_error(exc)
    assert not is_incomplete_stream_error(exc)


def test_http_error_is_never_transport():
    """HTTPError subclasses URLError; a server verdict is not a cut line."""
    assert not is_mid_turn_transport_error(
        HTTPError("https://api.example/v1", 500, "Server Error", Message(), None)
    )


# --- the agent verdict ------------------------------------------------------

def _agent_verdict(exc):
    """The Fix AI branch in agents/base.py, mirrored."""
    if (
        is_mid_turn_transport_error(exc)
        or is_incomplete_stream_error(exc)
        or isinstance(exc, ProviderCircuitOpenError)
    ):
        return "interrupted"
    return "failed"


def test_live_generation_13_and_14_killers_now_interrupt():
    assert _agent_verdict(LIVE_SSL_CUT) == "interrupted"
    assert _agent_verdict(LIVE_READ_TIMEOUT) == "interrupted"


def test_a_real_task_failure_still_fails():
    assert _agent_verdict(ValueError("expected_outputs not produced")) == "failed"


# --- the budget -------------------------------------------------------------
#
# NOTE (Fix AZ, doc 26 §18): the helpers below only MIRROR the budget rule, so they
# cannot catch a regression in the real branch.  Live gen30 showed what that costs:
# the counter was a node-lifetime total, so PATCH-R017 was converted to node_failure
# on its 4th transport cut after completing 111 productive tool rounds, and governed
# recovery answered a network timeout with a worker/tier reassignment.
# The real path is driven in tests/test_transport_interrupt_progress_budget.py.

def _disposition(seen: int, budget: int = 3) -> str:
    """The Fix AI bound in core/orchestrator.py, mirrored."""
    return "requeued" if seen <= budget else "budget_exhausted"


def test_interruptions_are_requeued_up_to_the_budget():
    assert [_disposition(n) for n in (1, 2, 3)] == ["requeued"] * 3


def test_budget_exhaustion_falls_back_to_failure():
    """A genuine outage must still reach the no-progress breaker."""
    assert _disposition(4) == "budget_exhausted"
    assert _disposition(9) == "budget_exhausted"


def test_a_zero_budget_preserves_the_original_behaviour():
    assert _disposition(1, budget=0) == "budget_exhausted"


def test_circuit_open_rejection_is_an_interruption():
    """The gate refuses before the request leaves the machine, so the node
    never had a turn.  Live gen 14 recorded this as PATCH-R005-RUNTIME failing.
    """
    exc = ProviderCircuitOpenError(
        "provider circuit open for https://dashscope.aliyuncs.com/compatible-mode/v1; "
        "retry in 26.0s"
    )
    assert _agent_verdict(exc) == "interrupted"


def test_circuit_open_is_not_mistaken_for_a_transport_cut():
    """It is its own category -- the classification must not rely on message text."""
    exc = ProviderCircuitOpenError("provider circuit open for https://x; retry in 26.0s")
    assert not is_mid_turn_transport_error(exc)
    assert not is_incomplete_stream_error(exc)
