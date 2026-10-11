"""Package-owned LLM stream-protocol invariants.

Port of ``@deepseek-ai/dsh-llm`` lib/invariant.js (commit 5badb150).

Python adaptations (documented):
- The async-generator ``validateStream`` becomes a synchronous generator
  (native host streams are iterators).
- ``install`` accepts either the Cordis context (verbatim ``ctx.on`` /
  ``ctx.get`` wiring, consumed once an invariants host mounts it) or an
  ``LlmRuntime`` instance (native seam wiring through ``on_stream`` /
  ``on_adapters_updated`` — the credentials/system-prompt precedent). With
  the internalized registry the listener lifetime equals the service
  lifetime, so the disposer-time ``ctx.get('llm') === undefined`` guard
  degenerates to the direct reference.
- ``apply`` keeps the verbatim ``ctx.invariants.register(PACKAGE_NAME,
  install)`` shape; the host has no invariants service until the B7
  dsh-invariants port, and the refusal says so.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, Optional

__all__ = ["PACKAGE_NAME", "apply", "inject", "install", "name",
           "validate_delta", "validate_index", "validate_stream"]

PACKAGE_NAME = "@deepseek-ai/dsh-llm"
# Cordis companion plugin name.
name = "llm-invariant"
# Service required before the companion can reserve package ownership.
inject = ["invariants"]

_MAX_SAFE_INTEGER = 2 ** 53 - 1


def _is_safe_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= _MAX_SAFE_INTEGER
    return False


def validate_index(index: Any, fail: Callable[[str], Any]) -> None:
    """Require one chunk index to be a non-negative safe integer."""
    if not _is_safe_integer(index) or index < 0:
        fail("LLM stream block index must be a non-negative safe integer, "
             f"got {index}")


def validate_delta(open_blocks: Dict[Any, str], index: Any, expected: str,
                   fail: Callable[[str], Any]) -> None:
    """Require a delta to address an open block of its matching type."""
    validate_index(index, fail)
    actual = open_blocks.get(index)
    if actual != expected:
        fail(f"{expected} delta at index {index} requires an open {expected} "
             f"block, got {str(actual)}")


def validate_stream(source: Any,
                    fail: Callable[[str], Any]
                    ) -> Iterator[Dict[str, Any]]:
    """Wrap one provider stream and enforce its grammar as chunks are
    consumed."""
    open_blocks: Dict[Any, str] = {}
    usage_seen = False
    finished = False
    for chunk in source:
        if finished:
            fail(f"LLM stream emitted {chunk['type']} after terminal finish")
        chunk_type = chunk["type"]
        if chunk_type == "block-start":
            validate_index(chunk["index"], fail)
            if chunk["index"] in open_blocks:
                fail(f"LLM stream repeated block-start index {chunk['index']}")
            open_blocks[chunk["index"]] = chunk["blockType"]
        elif chunk_type == "text-delta":
            validate_delta(open_blocks, chunk["index"], "text", fail)
        elif chunk_type == "reasoning-delta":
            validate_delta(open_blocks, chunk["index"], "reasoning", fail)
        elif chunk_type == "tool-call-delta":
            validate_delta(open_blocks, chunk["index"], "tool-call", fail)
        elif chunk_type == "block-end":
            validate_index(chunk["index"], fail)
            block_type = open_blocks.get(chunk["index"])
            if block_type is None:
                fail(f"LLM stream block-end index {chunk['index']} has no "
                     "open block")
            if chunk["block"]["type"] != block_type:
                fail(f"LLM stream block-end index {chunk['index']} closes "
                     f"{chunk['block']['type']}, expected {block_type}")
            del open_blocks[chunk["index"]]
        elif chunk_type == "usage":
            if usage_seen:
                fail("LLM stream emitted usage more than once")
            usage_seen = True
        elif chunk_type == "finish":
            if len(open_blocks) > 0 \
                    and chunk["reason"]["kind"] != "error" \
                    and chunk["reason"]["kind"] != "aborted":
                fail(f"LLM stream finished with {len(open_blocks)} open "
                     "block(s)")
            finished = True
        yield chunk
    if not finished:
        fail("LLM stream ended without a terminal finish chunk")


def _adapters_listener_for(get_llm: Callable[[], Any],
                           fail: Callable[[str], Any]) -> Callable[[], None]:
    def listener() -> None:
        llm = get_llm()
        # A disposer-time emit can outlive the service-store entry during
        # whole-context teardown; only a live service promises a readable
        # registry.
        if llm is None:
            return
        for provider in llm.listProviders() if hasattr(llm, "listProviders") \
                else llm.list_providers():
            try:
                llm.provider_retry_policy(provider["id"])
            except BaseException:
                # Reaching here IS the violation: the notification promised
                # a readable registry, and only that broken promise can make
                # the lookup throw.
                fail(f'llm/adapters-updated fired while provider '
                     f'"{provider["id"]}" has no readable registration')
    return listener


def install(target: Any, fail: Callable[[str], Any]) -> Callable[[], None]:
    """Install validation around every provider stream.

    ``target`` is either a Cordis context (verbatim ``ctx.on`` wiring) or an
    ``LlmRuntime`` (native seam wiring). Returns one disposer for both
    listeners.
    """
    stream_handler = lambda options, next_: validate_stream(next_(), fail)  # noqa: E731
    if hasattr(target, "on_stream") and hasattr(target, "on_adapters_updated"):
        off_stream = target.on_stream(stream_handler, prepend=True)
        off_event = target.on_adapters_updated(
            _adapters_listener_for(lambda: target, fail))

        def dispose() -> None:
            off_stream()
            off_event()

        return dispose
    # Cordis context form (verbatim).
    target.on("llm/stream", stream_handler, {"global": True, "prepend": True})
    target.on("llm/adapters-updated",
              _adapters_listener_for(lambda: target.get("llm"), fail),
              {"global": True})

    def dispose_ctx() -> None:
        raise NotImplementedError(
            "llm-invariant: Cordis-context disposer arrives with the "
            "invariants host port (ctx.off face)")

    return dispose_ctx


def apply(ctx: Any) -> Any:
    """Register the LLM invariant companion.

    :param ctx: Cordis context carrying the invariant service.
    :returns: the installed registration's disposer after setup succeeds.
    """
    invariants = getattr(ctx, "invariants", None)
    if invariants is None:
        getter = getattr(ctx, "get", None)
        invariants = getter("invariants") if callable(getter) else None
    if invariants is None:
        raise RuntimeError(
            'llm-invariant: the "invariants" service is required before the '
            "companion can reserve package ownership (B7 dsh-invariants is "
            "not yet ported)")
    return invariants.register(PACKAGE_NAME, install)
