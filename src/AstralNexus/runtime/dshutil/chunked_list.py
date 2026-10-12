"""Persistent append-only chunked lists with bounded copying.

Port of ``@deepseek-ai/dsh-chunked-list`` (commit 5badb15). Semantics kept:
- ``append_chunked_list`` returns a NEW head and never mutates the input; a
  snapshot captured before an append keeps yielding exactly the values it
  contained. Only the current chunk is copied, so a single append moves at
  most ``CHUNK_CAPACITY`` (64) value references no matter how long the list is.
- ``iterate_chunked_list`` yields every stored value in insertion order with
  O(N) time and O(N / 64) scratch space.
- ``validate_chunked_list`` checks nonempty JSON checkpoints recursively:
  every stored value (through the caller-owned validator), chunk size within
  1..64, and no unknown fields (upstream's ``.strict()`` zod behavior).

Python adaptation: the zod schema becomes an explicit recursive validator
over plain dicts (the checkpoint JSON form produced by ``chunked_list_to_json``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional

CHUNK_CAPACITY = 64


@dataclass(frozen=True)
class ChunkedChunk:
    """One node of a persistent append-only list (current chunk + previous)."""
    values: tuple
    previous: Optional["ChunkedChunk"] = None


def append_chunked_list(head: Optional[ChunkedChunk], value: Any) -> ChunkedChunk:
    if head is None or len(head.values) == CHUNK_CAPACITY:
        return ChunkedChunk((value,), head)
    return ChunkedChunk(head.values + (value,), head.previous)


def iterate_chunked_list(head: Optional[ChunkedChunk]) -> Iterator[Any]:
    chunks: List[ChunkedChunk] = []
    chunk = head
    while chunk is not None:
        chunks.append(chunk)
        chunk = chunk.previous
    for node in reversed(chunks):
        yield from node.values


def chunked_list_to_json(head: Optional[ChunkedChunk]) -> Optional[dict]:
    """Checkpoint form: nested ``{"values": [...], "previous": {...}}`` dicts."""
    if head is None:
        return None
    return {"values": list(head.values), "previous": chunked_list_to_json(head.previous)}


def chunked_list_from_json(data: Optional[dict]) -> Optional[ChunkedChunk]:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("chunk must be an object")
    values = data.get("values")
    if not isinstance(values, list) or not 1 <= len(values) <= CHUNK_CAPACITY:
        raise ValueError(f"chunk values must hold 1..{CHUNK_CAPACITY} items")
    return ChunkedChunk(tuple(values), chunked_list_from_json(data.get("previous")))


def validate_chunked_list(data: Any, value_validator: Optional[Callable[[Any], bool]] = None) -> None:
    """Validate a nonempty checkpoint; raise ValueError naming the offending path."""
    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            raise ValueError(f"{path}: chunk must be an object")
        unknown = set(node) - {"values", "previous"}
        if unknown:
            raise ValueError(f"{path}: unknown fields {sorted(unknown)}")
        values = node.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{path}/values: must be a non-empty array")
        if len(values) > CHUNK_CAPACITY:
            raise ValueError(f"{path}/values: chunk exceeds capacity {CHUNK_CAPACITY}")
        if value_validator is not None:
            for index, value in enumerate(values):
                if not value_validator(value):
                    raise ValueError(f"{path}/values/{index}: value rejected by the caller validator")
        if node.get("previous") is not None:
            walk(node["previous"], f"{path}/previous")

    if data is None:
        raise ValueError("checkpoint must be a non-empty chunked list")
    walk(data, "<root>")
