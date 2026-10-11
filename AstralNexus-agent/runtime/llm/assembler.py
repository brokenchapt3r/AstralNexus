"""Incremental chunk-to-message assembler.

Port of ``@deepseek-ai/dsh-llm`` lib/types/assembler.js (commit 5badb150).
This is the single canonical assembly algorithm used by the agent loop to
build an assistant message from a chunk stream while logging the raw chunks
for replay fidelity.

Tolerant of delta-only protocols (no block-start/end); deltas arriving for
an index already closed by ``block-end`` are ignored (malformed stream) so a
misbehaving adapter cannot grow memory or corrupt a completed block.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..dshutil.brand import brand_string
from ..dshutil.values import assert_never
from .message import create_assistant_message

__all__ = ["BlockAssembler"]


class BlockAssembler:
    """Incrementally assembles raw StreamChunks into complete ContentBlocks
    and a final assistant Message.

    The agent loop feeds it while logging raw chunks for replay fidelity,
    then reads ``blocks()`` / ``message()`` / ``usage`` / ``finish`` once the
    stream ends, or ``interrupted_blocks()`` when cancellation cut the stream
    short.
    """

    def __init__(self) -> None:
        self.partials: Dict[Any, Dict[str, Any]] = {}
        self.order: List[Any] = []
        self._usage: Optional[Any] = None
        self._finish: Optional[Any] = None
        self._replay_state: Optional[Any] = None

    def push(self, chunk: Dict[str, Any]) -> None:
        """Feed one chunk into the assembly state, in stream order."""
        chunk_type = chunk["type"]
        if chunk_type == "block-start":
            if chunk["index"] not in self.partials:
                self.order.append(chunk["index"])
                self.partials[chunk["index"]] = {
                    "blockType": chunk["blockType"],
                    "text": "",
                    "toolCallArguments": "",
                }
            return
        if chunk_type in ("text-delta", "reasoning-delta"):
            partial = self.ensure(
                chunk["index"],
                "text" if chunk_type == "text-delta" else "reasoning")
            if partial.get("block") is not None:
                return  # closed by block-end; ignore stragglers
            partial["text"] += chunk["text"]
            return
        if chunk_type == "tool-call-delta":
            partial = self.ensure(chunk["index"], "tool-call")
            if partial.get("block") is not None:
                return  # closed by block-end; ignore stragglers
            partial["toolCallId"] = chunk["id"]
            if chunk.get("name"):
                partial["toolCallName"] = chunk["name"]
            partial["toolCallArguments"] += chunk["argumentsDelta"]
            return
        if chunk_type == "block-end":
            partial = self.ensure(chunk["index"], chunk["block"]["type"])
            # First close wins; ignoring re-close stragglers keeps streamed
            # output and the final assembled block in agreement.
            if partial.get("block") is not None:
                return
            partial["block"] = chunk["block"]
            return
        if chunk_type == "usage":
            self._usage = chunk["usage"]
            return
        if chunk_type == "finish":
            self._finish = chunk["reason"]
            self._replay_state = chunk.get("replayState")
            return
        assert_never(chunk, "BlockAssembler.push")

    def ensure(self, index: Any, block_type: str) -> Dict[str, Any]:
        partial = self.partials.get(index)
        if partial is None:
            partial = {"blockType": block_type, "text": "",
                       "toolCallArguments": ""}
            self.partials[index] = partial
            self.order.append(index)
        return partial

    def assemble(self, partial: Dict[str, Any], index: Any) -> Dict[str, Any]:
        if partial.get("block") is not None:
            return partial["block"]
        block_type = partial["blockType"]
        if block_type == "text":
            return {"type": "text", "text": partial["text"]}
        if block_type == "reasoning":
            return {"type": "reasoning", "text": partial["text"]}
        if block_type == "tool-call":
            tool_call_id = partial.get("toolCallId")
            return {
                "type": "tool-call",
                "id": tool_call_id if tool_call_id is not None
                else brand_string(f"call-{index}"),
                "name": partial.get("toolCallName", ""),
                "arguments": partial["toolCallArguments"],
            }
        raise ValueError(
            f'cannot assemble incomplete block of type "{block_type}"')

    def must_get(self, index: Any) -> Dict[str, Any]:
        """Invariant accessor: every index in ``order`` has a partial."""
        partial = self.partials.get(index)
        if partial is None:
            raise ValueError(
                f"BlockAssembler invariant violated: no partial for index "
                f"{index}")
        return partial

    def assembled(self) -> Dict[str, Any]:
        """The one shared keep/drop decision over all seen blocks: max-token
        truncation drops tool calls that cannot be executed safely. Emitted
        blocks and replay metadata both derive from this result, so they
        cannot disagree."""
        all_blocks = [self.assemble(self.must_get(index), index)
                      for index in self.order]
        kept: Optional[List[bool]] = None
        if self.finish["kind"] == "max-tokens":
            kept = [block["type"] != "tool-call" for block in all_blocks]
        blocks = all_blocks if kept is None else [
            block for position, block in enumerate(all_blocks)
            if kept[position]]
        envelope = self._replay_state
        if envelope is None or envelope.get("blocks") is None:
            return {"blocks": blocks, "replay": envelope}
        if len(envelope["blocks"]) != len(all_blocks):
            return {"blocks": blocks, "replay": None}
        if kept is None or len(blocks) == len(all_blocks):
            return {"blocks": blocks, "replay": envelope}
        return {
            "blocks": blocks,
            "replay": {
                "response": envelope["response"],
                "blocks": [block for position, block
                           in enumerate(envelope["blocks"])
                           if kept[position]],
            },
        }

    def blocks(self) -> List[Dict[str, Any]]:
        """Assemble all blocks seen so far, in stream order, except that
        max-token truncation drops tool calls that cannot be executed
        safely."""
        return self.assembled()["blocks"]

    def interrupted_blocks(self) -> List[Dict[str, Any]]:
        """Assemble the prefix an interrupted stream can safely finalize:
        closed and open text/reasoning blocks with non-whitespace content,
        in stream order. Tool calls are omitted because interruption
        precedes dispatch; open unknown blocks are also omitted."""
        result: List[Dict[str, Any]] = []
        for index in self.order:
            partial = self.must_get(index)
            block = partial.get("block")
            block_type = block["type"] if block is not None \
                else partial["blockType"]
            if block_type not in ("text", "reasoning"):
                continue
            assembled = self.assemble(partial, index)
            if assembled["type"] in ("text", "reasoning") \
                    and assembled["text"].strip() != "":
                result.append(assembled)
        return result

    @property
    def usage(self) -> Optional[Any]:
        """Usage from the usage chunk; None until one arrives."""
        return self._usage

    @property
    def finish(self) -> Any:
        """Finish reason from the finish chunk; ``{"kind": "stop"}`` when the
        stream ended without one."""
        return self._finish if self._finish is not None else {"kind": "stop"}

    @property
    def replay_state(self) -> Optional[Any]:
        """Replay metadata from the terminal finish chunk, if any, with
        per-block entries pruned in step with ``blocks``."""
        return self.assembled()["replay"]

    def message(self, source: Dict[str, Any]) -> Any:
        """The assembled assistant message over ``blocks()``."""
        return create_assistant_message(
            {"content": self.blocks(), "source": source})
