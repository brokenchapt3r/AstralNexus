"""Lossless compact representation of one model-stream attempt, plus
record-level readers that answer common consumer questions without
materializing members.

Port of ``@deepseek-ai/dsh-llm`` lib/types/assistant-stream.js
(commit 5badb150). Readers trust the static record type;
``expand_assistant_stream`` is the validating path for records read at a
durable boundary.

Python adaptation (documented): ``Number.isSafeInteger`` maps to an
integer-within-2**53 check; ``Object.is(value, -0)`` maps to a signed-zero
float check (Python ints have no negative zero).
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any, Dict, List, Optional, Sequence

from ..dshutil.values import (assert_never, deep_freeze,
                              snapshot_json_value)

__all__ = [
    "AssistantStreamAccumulator",
    "assemble_assistant_stream",
    "assistant_stream_chunks",
    "assistant_stream_first_token_time",
    "assistant_stream_has_visible_content",
    "assistant_stream_has_visible_text",
    "chunk_has_visible_text",
    "expand_assistant_stream",
    "is_token_delta",
    "is_visible_chunk",
    "join_assistant_stream_text",
    "last_assistant_stream_chunk",
    "run_first_token_time",
    "run_first_visible_time",
]

_MAX_SAFE_INTEGER = 2 ** 53 - 1
_NON_WHITESPACE = re.compile(r"\S")


def _is_safe_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= _MAX_SAFE_INTEGER
    if isinstance(value, float):
        return value.is_integer() and abs(value) <= _MAX_SAFE_INTEGER
    return False


def _is_negative_zero(value: Any) -> bool:
    return isinstance(value, float) and value == 0.0 \
        and math.copysign(1.0, value) < 0


def _safe_time(value: Any) -> Any:
    if not _is_safe_integer(value):
        raise TypeError(
            f"Assistant stream time must be a safe integer, got {str(value)}")
    return value


def _safe_index(value: Any, label: str) -> Any:
    if not _is_safe_integer(value) or value < 0 or _is_negative_zero(value):
        raise TypeError(f"{label} index must be a non-negative safe integer")
    return value


def _snapshot_chunk(chunk: Any) -> Any:
    snapshot = snapshot_json_value(chunk)
    if snapshot is None:
        raise TypeError(
            "Assistant stream chunk must be losslessly JSON-serializable")
    return snapshot


def _safe_gap(previous: Any, next_value: Any) -> Optional[int]:
    gap = next_value - previous
    return gap if _is_safe_integer(gap) and previous + gap == next_value \
        else None


class AssistantStreamAccumulator:
    """Incrementally compacts one attempt without retaining a second
    raw-chunk list."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def push(self, value: Dict[str, Any]) -> Any:
        """Add one timed chunk to the compact attempt stream.

        :param value: model chunk and its original Session timestamp.
        :returns: a detached immutable copy for assembly and live
            publication.
        """
        time = _safe_time(value["time"])
        chunk = _snapshot_chunk(value["chunk"])
        timed = deep_freeze({"time": time, "chunk": chunk})
        previous = self.records[-1] if self.records else None
        chunk_type = chunk["type"]
        if chunk_type in ("text-delta", "reasoning-delta"):
            _safe_index(chunk["index"], chunk_type)
            if not isinstance(chunk["text"], str):
                raise TypeError(f"{chunk_type} text must be a string")
            record_type = "text-chunks" if chunk_type == "text-delta" \
                else "reasoning-chunks"
            gap = _safe_gap(previous["lastTime"], time) \
                if previous is not None and previous["type"] == record_type \
                else None
            if previous is not None and previous["type"] == record_type \
                    and previous["index"] == chunk["index"] \
                    and gap is not None:
                previous["dt"].append(gap)
                previous["texts"].append(chunk["text"])
                previous["lastTime"] = time
            else:
                self.records.append({
                    "type": record_type, "time0": time,
                    "index": chunk["index"], "dt": [],
                    "texts": [chunk["text"]], "lastTime": time,
                })
            return timed
        if chunk_type == "tool-call-delta":
            _safe_index(chunk["index"], chunk_type)
            if not isinstance(chunk["id"], str):
                raise TypeError("tool-call-delta id must be a string")
            if "name" in chunk and not isinstance(chunk["name"], str):
                raise TypeError("tool-call-delta name must be a string")
            if not isinstance(chunk["argumentsDelta"], str):
                raise TypeError(
                    "tool-call-delta argumentsDelta must be a string")
            if chunk["id"] == "" or chunk.get("name") == "":
                self.records.append({"type": "chunk", "time": time,
                                     "chunk": chunk})
                return timed
            gap = _safe_gap(previous["lastTime"], time) \
                if previous is not None \
                and previous["type"] == "tool-call-chunks" else None
            same_name = previous is not None \
                and previous["type"] == "tool-call-chunks" \
                and ("name" in previous) == ("name" in chunk) \
                and previous.get("name") == chunk.get("name")
            if previous is not None \
                    and previous["type"] == "tool-call-chunks" \
                    and previous["index"] == chunk["index"] \
                    and previous["id"] == chunk["id"] \
                    and same_name and gap is not None:
                previous["dt"].append(gap)
                previous["args"].append(chunk["argumentsDelta"])
                previous["lastTime"] = time
            else:
                record: Dict[str, Any] = {
                    "type": "tool-call-chunks", "time0": time,
                    "index": chunk["index"], "dt": [], "id": chunk["id"],
                }
                if "name" in chunk:
                    record["name"] = chunk["name"]
                record["args"] = [chunk["argumentsDelta"]]
                record["lastTime"] = time
                self.records.append(record)
            return timed
        if chunk_type in ("block-start", "block-end", "usage", "finish"):
            self.records.append({"type": "chunk", "time": time,
                                 "chunk": chunk})
            return timed
        return assert_never(chunk, "AssistantStreamAccumulator.push")

    def snapshot(self) -> Any:
        """Return the current compact attempt stream as a detached immutable
        record list suitable for a durable event."""
        records = []
        for record in self.records:
            if record["type"] == "chunk":
                records.append({**record})
                continue
            durable = {k: v for k, v in record.items() if k != "lastTime"}
            if durable["type"] == "tool-call-chunks":
                records.append({**durable, "dt": list(durable["dt"]),
                                "args": list(durable["args"])})
            else:
                records.append({**durable, "dt": list(durable["dt"]),
                                "texts": list(durable["texts"])})
        return deep_freeze(records)


def expand_assistant_stream(stream: Sequence[Any]) -> List[Dict[str, Any]]:
    """Expand compact records into the exact timed chunk sequence.

    :param stream: compact records from one durable Assistant settlement.
    :returns: detached timed chunks with every original delta boundary
        preserved.
    :raises TypeError: when a record or reconstructed timestamp is invalid.
    """
    chunks: List[Dict[str, Any]] = []
    for candidate in stream:
        record = _validate_record(candidate)
        if record["type"] == "chunk":
            chunks.append({"time": record["time"], "chunk": record["chunk"]})
            continue
        members = record["args"] if record["type"] == "tool-call-chunks" \
            else record["texts"]
        time = record["time0"]
        for index in range(len(members)):
            if index > 0:
                time += record["dt"][index - 1]
            if record["type"] == "text-chunks":
                chunk: Dict[str, Any] = {"type": "text-delta",
                                         "index": record["index"],
                                         "text": members[index]}
            elif record["type"] == "reasoning-chunks":
                chunk = {"type": "reasoning-delta",
                         "index": record["index"], "text": members[index]}
            else:
                chunk = {"type": "tool-call-delta",
                         "index": record["index"], "id": record["id"]}
                if "name" in record:
                    chunk["name"] = record["name"]
                chunk["argumentsDelta"] = members[index]
            chunks.append({"time": time, "chunk": chunk})
    return chunks


def _has_non_whitespace(text: str) -> bool:
    return bool(_NON_WHITESPACE.search(text))


def _block_is_visible(block: Any) -> bool:
    if block["type"] == "tool-call":
        return False
    if block["type"] in ("text", "reasoning"):
        return _has_non_whitespace(block["text"])
    return True


def is_token_delta(chunk: Any) -> bool:
    """Whether one chunk carries the model's first output token for latency
    measurement."""
    chunk_type = chunk["type"]
    if chunk_type in ("text-delta", "reasoning-delta"):
        return chunk["text"] != ""
    if chunk_type == "tool-call-delta":
        return chunk["argumentsDelta"] != "" or chunk.get("name") is not None
    return False


def is_visible_chunk(chunk: Any) -> bool:
    """Whether one chunk by itself contributes reader-visible transcript
    content."""
    chunk_type = chunk["type"]
    if chunk_type in ("text-delta", "reasoning-delta"):
        return _has_non_whitespace(chunk["text"])
    if chunk_type == "block-start":
        return chunk["blockType"] not in ("text", "reasoning", "tool-call")
    if chunk_type == "block-end":
        return _block_is_visible(chunk["block"])
    return False


def chunk_has_visible_text(chunk: Any) -> bool:
    """Whether one chunk carries non-whitespace text, as a text delta or a
    completed text block."""
    if chunk["type"] == "text-delta":
        return _has_non_whitespace(chunk["text"])
    return chunk["type"] == "block-end" and chunk["block"]["type"] == "text" \
        and _has_non_whitespace(chunk["block"]["text"])


def _first_run_member_time(run: Any, predicate: Any) -> Optional[Any]:
    fragments = run["args"] if run["type"] == "tool-call-chunks" \
        else run["texts"]
    time = run["time0"]
    for index in range(len(fragments)):
        if index > 0:
            time += run["dt"][index - 1]
        if predicate(fragments[index]):
            return time
    return None


def run_first_token_time(run: Any) -> Optional[Any]:
    """Time of the first member of one packed run that is_token_delta
    accepts."""
    if run["type"] == "tool-call-chunks" and run.get("name") is not None:
        return run["time0"]
    return _first_run_member_time(run, lambda fragment: fragment != "")


def run_first_visible_time(run: Any) -> Optional[Any]:
    """Time of the first member of one packed run that is_visible_chunk
    accepts; a tool-call run has none."""
    return None if run["type"] == "tool-call-chunks" \
        else _first_run_member_time(run, _has_non_whitespace)


def assistant_stream_first_token_time(stream: Sequence[Any]) -> Optional[Any]:
    """Time of the first token in one compact stream, read from the records
    themselves and stopping at the first qualifying member."""
    for record in stream:
        if record["type"] == "chunk":
            time = record["time"] if is_token_delta(record["chunk"]) else None
        else:
            time = run_first_token_time(record)
        if time is not None:
            return time
    return None


def assistant_stream_has_visible_content(stream: Sequence[Any]) -> bool:
    """Whether one compact stream carries any reader-visible content."""
    return any(
        is_visible_chunk(record["chunk"]) if record["type"] == "chunk"
        else run_first_visible_time(record) is not None
        for record in stream)


def assistant_stream_has_visible_text(stream: Sequence[Any]) -> bool:
    """Whether one compact stream carries non-whitespace text."""
    return any(
        any(_has_non_whitespace(text) for text in record["texts"])
        if record["type"] == "text-chunks"
        else record["type"] == "chunk"
        and chunk_has_visible_text(record["chunk"])
        for record in stream)


def last_assistant_stream_chunk(stream: Sequence[Any],
                                chunk_type: str) -> Optional[Any]:
    """The last raw chunk of one never-packed type, scanning backwards."""
    for index in range(len(stream) - 1, -1, -1):
        record = stream[index]
        if record["type"] == "chunk" and record["chunk"]["type"] == chunk_type:
            return record["chunk"]
    return None


def assistant_stream_chunks(stream: Sequence[Any],
                            chunk_type: str) -> List[Any]:
    """Every raw chunk of one never-packed type, in stream order."""
    chunks = []
    for record in stream:
        if record["type"] == "chunk" and record["chunk"]["type"] == chunk_type:
            chunks.append(record["chunk"])
    return chunks


def join_assistant_stream_text(stream: Sequence[Any]) -> str:
    """Every streamed text-delta fragment joined in stream order; reasoning
    and tool-call fragments are excluded."""
    parts: List[str] = []
    for record in stream:
        if record["type"] == "text-chunks":
            parts.append("".join(record["texts"]))
        elif record["type"] == "chunk" \
                and record["chunk"]["type"] == "text-delta":
            parts.append(record["chunk"]["text"])
    return "".join(parts)


def assemble_assistant_stream(stream: Sequence[Any],
                              assembler: Optional[Any] = None) -> Any:
    """Feed one compact stream into a BlockAssembler without materializing
    members. The records are trusted, not validated: validate a stream read
    at a durable boundary with expand_assistant_stream first."""
    from .assembler import BlockAssembler
    if assembler is None:
        assembler = BlockAssembler()
    for record in stream:
        record_type = record["type"]
        if record_type == "chunk":
            assembler.push(record["chunk"])
        elif record_type == "text-chunks":
            assembler.push({"type": "text-delta", "index": record["index"],
                            "text": "".join(record["texts"])})
        elif record_type == "reasoning-chunks":
            assembler.push({"type": "reasoning-delta",
                            "index": record["index"],
                            "text": "".join(record["texts"])})
        elif record_type == "tool-call-chunks":
            chunk: Dict[str, Any] = {
                "type": "tool-call-delta", "index": record["index"],
                "id": record["id"],
                "argumentsDelta": "".join(record["args"]),
            }
            if record.get("name") is not None:
                chunk["name"] = record["name"]
            assembler.push(chunk)
        else:
            assert_never(record, "assembleAssistantStream")
    return assembler


def _validate_record(value: Any) -> Any:
    if not isinstance(value, dict):
        raise TypeError("Assistant stream record must be an object")
    record = value
    record_type = record.get("type")
    if record_type in ("text-chunks", "reasoning-chunks"):
        _exact_keys(record, ["type", "time0", "index", "dt", "texts"],
                    record_type)
        texts = _string_array(record["texts"], f"{record_type} texts")
        if len(texts) == 0:
            raise TypeError(f"{record_type} texts must be non-empty")
        _validate_run(record, len(texts), record_type)
        return record
    if record_type == "tool-call-chunks":
        keys = ["type", "time0", "index", "dt", "id", "name", "args"] \
            if "name" in record \
            else ["type", "time0", "index", "dt", "id", "args"]
        _exact_keys(record, keys, record_type)
        args = _string_array(record["args"], "tool-call-chunks args")
        if len(args) == 0:
            raise TypeError("tool-call-chunks args must be non-empty")
        if not isinstance(record["id"], str) or record["id"] == "":
            raise TypeError(
                "tool-call-chunks id must be a non-empty string")
        if record.get("name") is not None and (
                not isinstance(record["name"], str) or record["name"] == ""):
            raise TypeError(
                "tool-call-chunks name must be a non-empty string")
        _validate_run(record, len(args), record_type)
        return record
    if record_type == "chunk":
        _exact_keys(record, ["type", "time", "chunk"], "chunk")
        time = _safe_time(record["time"])
        if not isinstance(record["chunk"], dict):
            raise TypeError(
                "Assistant stream raw chunk must be a lossless JSON object")
        try:
            chunk = _snapshot_chunk(record["chunk"])
        except TypeError as error:
            raise TypeError(
                "Assistant stream raw chunk must be a lossless JSON object"
            ) from error
        return deep_freeze({"type": "chunk", "time": time, "chunk": chunk})
    import json as _json
    raise TypeError("Unsupported Assistant stream record "
                    f"{_json.dumps(record_type)}")


def _validate_run(record: Any, members: int, label: str) -> None:
    _safe_time(record["time0"])
    _safe_index(record["index"], label)
    dt = record["dt"]
    if not isinstance(dt, list) or any(not _is_safe_integer(value)
                                       for value in dt):
        raise TypeError(f"{label} dt must contain safe integers")
    if len(dt) != members - 1:
        raise TypeError(
            f"{label} dt length must be one less than its members")
    time = record["time0"]
    for gap in dt:
        time += gap
        if not _is_safe_integer(time):
            raise TypeError(f"{label} member times must stay safe integers")


def _string_array(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or any(
            not isinstance(member, str) for member in value):
        raise TypeError(f"{label} must be a string array")
    return value


def _exact_keys(record: Dict[str, Any], keys: Sequence[str],
                label: str) -> None:
    if len(record) != len(keys) or any(key not in record for key in keys):
        raise TypeError(
            f"{label} Assistant stream record must contain exactly "
            f"{', '.join(keys)}")
