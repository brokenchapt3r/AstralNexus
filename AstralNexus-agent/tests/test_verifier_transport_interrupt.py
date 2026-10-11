"""Fix AL: a transport cut inside the Verifier is not a verdict.

Live regression (doc 18, r13b generation 16)::

    Verifier failed for PATCH-R007-RUNTIME:
    Network error from LLM API after 120.6s: The read operation timed out

``PATCH-R007-RUNTIME`` had reached ``result_submitted`` with its evidence on
disk, no staleness veto and no acceptance failure -- the network simply failed
to deliver a verdict.  The broad handler forced ``inconclusive`` / 0.0 anyway,
which does two wrong things: it pushes a sound node to ``repair_pending``, and
``_finalize_node_evidence`` writes a PERMANENT rejection into the execution
journal on account of a read timeout.

This is the same misattribution Fix AI removed on the Worker side, one boundary
later.  No verdict was obtained, so none is recorded: the node returns to
``result_submitted`` and verification is retried, bounded by the same budget so
a real outage still reaches the no-progress breaker.

A verification failure that is NOT transport -- a staleness veto, a data
validation failure, a malformed verdict -- must still force inconclusive and
stand, because those ARE statements about the node's evidence.
"""
from __future__ import annotations

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

LIVE_VERIFIER_TIMEOUT = socket.timeout(
    "Network error from LLM API after 120.6s: The read operation timed out"
)


def _disposition(exc, seen: int = 1, budget: int = 3) -> str:
    """The Fix AL branch in core/orchestrator.py, mirrored."""
    transport = (
        is_mid_turn_transport_error(exc)
        or is_incomplete_stream_error(exc)
        or isinstance(exc, ProviderCircuitOpenError)
    )
    if not transport:
        return "verdict_forced_inconclusive"
    return "requeued" if seen <= budget else "budget_exhausted"


# --- the live failure --------------------------------------------------------

def test_the_live_verifier_timeout_is_requeued_not_judged():
    assert _disposition(LIVE_VERIFIER_TIMEOUT) == "requeued"


@pytest.mark.parametrize("exc", [
    LIVE_VERIFIER_TIMEOUT,
    URLError(ssl.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING")),
    ConnectionResetError("connection reset by peer"),
    ProviderCircuitOpenError("provider circuit open for https://x; retry in 26.0s"),
])
def test_transport_classes_never_produce_a_verdict(exc):
    assert _disposition(exc) == "requeued"


# --- what must still force a verdict ----------------------------------------

@pytest.mark.parametrize("exc,why", [
    (ValueError("Execution evidence is stale; owned files changed after the Verifier read them"),
     "a staleness veto is a statement about the evidence"),
    (ValueError("Data validation failed; execution cannot be accepted"),
     "a corrupt artifact is a real rejection"),
    (ValueError("Test claims not anchored to recorded successful executions"),
     "an unanchored test claim is a real rejection"),
    (KeyError("verdict"), "a malformed verifier response is not a network fault"),
    (HTTPError("https://api.example/v1", 400, "Bad Request", Message(), None),
     "a server verdict is not a cut line"),
])
def test_real_verification_failures_still_stand(exc, why):
    assert _disposition(exc) == "verdict_forced_inconclusive", why


# --- the bound ---------------------------------------------------------------

def test_requeue_is_bounded():
    assert _disposition(LIVE_VERIFIER_TIMEOUT, seen=3) == "requeued"
    assert _disposition(LIVE_VERIFIER_TIMEOUT, seen=4) == "budget_exhausted"


def test_a_zero_budget_restores_the_original_behaviour():
    assert _disposition(LIVE_VERIFIER_TIMEOUT, seen=1, budget=0) == "budget_exhausted"


def test_worker_and_verifier_budgets_are_counted_separately():
    """Two different boundaries; one must not consume the other's budget."""
    from AstralNexus.core import orchestrator as orch
    import inspect
    source = inspect.getsource(orch.Orchestrator._run_agent_task_async) + inspect.getsource(orch.Orchestrator._process_worker_results)
    assert 'node.metadata["transport_interrupts"]' in source
    assert 'node.metadata["verifier_transport_interrupts"]' in source
