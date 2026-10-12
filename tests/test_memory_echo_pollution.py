# -*- coding: utf-8 -*-
"""Round 9 (live r11 iter-136 stall, parallel-session stack sampling):
fact-memory echo pollution + checkpoint write amplification.

Live forensics: the orchestrator rendered every durable fact back into the
pre-compression context ("Fact [fact_…]: … (confidence: …, source: …)"),
``capture_before_compression`` re-extracted those echoes as NEW facts on
every compression (5,080 → 19,105 → 37,690 → 111,693; 111,525 of 111,708
facts contained old fact references), MemoryFederation's constructor wrote
every loaded fact back through per-item commits, and ``save_task_graph``
rewrote the whole pile with one connection + commit per row — a 361 MB
store and a 28+ minute main-loop stall with heartbeats masking it.

Three fixes under test:
1. capture never re-captures serialized durable memories (echo filter);
2. loading facts hydrates memory WITHOUT persisting;
3. checkpoint saves in ONE transaction and unchanged facts are not rewritten.
"""
import time

from AstralNexus.core.memory_federation import MemoryFederation
from AstralNexus.core.task_graph import Fact, TaskGraph, TaskNode
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


def _fact(fact_id: str, text: str, recorded_at: str = "2026-09-12T00:00:00+00:00") -> Fact:
    return Fact(
        fact_id=fact_id, text=text, source="seed",
        confidence=0.9, recorded_at=recorded_at,
    )


def test_capture_skips_serialized_memory_echoes():
    fed = MemoryFederation()
    transcript = "\n".join([
        "Task N-001 [completed]: backend service.",
        "Fact [fact_captured_global_0]: The service must listen on port 5000 "
        "(confidence: 0.9, source: transcript:global)",
        "Fact [fact_x]: Nested echo Fact [fact_y]: deeper must nest "
        "(confidence: 0.8, source: s)",
        "Artifact [artifact_1]: backend component must ship",
        "The worker needs to start uvicorn before curl",
    ])
    first = fed.capture_before_compression("global", transcript)
    texts = list(first.relevant_facts)

    # Genuine new transcript information is still captured (sentence
    # splitting is on [.!?]+, so the captured chunk may span lines).
    assert any("The worker needs to start uvicorn before curl" in t for t in texts)
    assert texts == ["The worker needs to start uvicorn before curl"]
    # Serialized store echoes are never re-captured — whole lines dropped
    # before sentence splitting, so keyword-rich fact texts cannot leak.
    assert not any(t.startswith("Fact [") for t in texts)
    assert not any(t.startswith("Artifact [") for t in texts)
    assert not any("must listen on port 5000" in t for t in texts)
    assert not any("Nested echo" in t or "deeper must nest" in t for t in texts)

    # Idempotence: feeding the store's own serialization back captures
    # NOTHING — the recursion that produced 111k nested facts is dead.
    echoed = "\n".join(
        f"Fact [fact_captured_global_{i}]: {t} (confidence: 0.9, "
        f"source: transcript:global)"
        for i, t in enumerate(texts)
    )
    tier_size = len(fed.medium_term_memory)
    second = fed.capture_before_compression("global", echoed)
    assert second.total_captured == 0
    assert len(fed.medium_term_memory) == tier_size


def test_capture_echo_filter_also_guards_constraints():
    fed = MemoryFederation()
    transcript = "\n".join([
        "Fact [fact_a]: The worker must not skip RBAC checks. "
        "It is forbidden to bypass the constraint (confidence: 0.9, source: s)",
        "The policy forbids closing a work order without receipts",
    ])
    result = fed.capture_before_compression("global", transcript)
    assert not any("RBAC" in c for c in result.captured_constraints)
    assert any("policy forbids" in c for c in result.captured_constraints)


class _RecordingStore:
    def __init__(self, facts):
        self._facts = facts
        self.added = []

    def facts(self):
        return list(self._facts)

    def add_fact(self, fact):
        self.added.append(fact.fact_id)


def test_federation_load_does_not_write_back():
    store = _RecordingStore([_fact("fact_a", "service must run")])
    fed = MemoryFederation(store)
    # Tier is hydrated…
    assert "fact_a" in fed.medium_term_memory
    # …but loading never persists (old code re-committed every loaded fact
    # through a fresh connection each).
    assert store.added == []


def test_save_task_graph_single_transaction_and_fact_guard(tmp_path):
    store = SQLiteTaskGraphStore(tmp_path / "graph.sqlite")
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="N-1", goal="g", role="coder", status="completed"))
    for i in range(50):
        graph.add_fact(_fact(f"fact_{i}", f"service must run variant {i}"))

    started = time.monotonic()
    store.save_task_graph("run-1", graph)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"batch save too slow: {elapsed:.1f}s"

    loaded = {f.fact_id: f for f in store.facts()}
    assert len(loaded) == 50

    # Unchanged re-save keeps every row byte-identical (guarded upsert).
    store.save_task_graph("run-1", graph)
    loaded2 = {f.fact_id: f for f in store.facts()}
    assert len(loaded2) == 50
    assert {k: v.text for k, v in loaded2.items()} == {k: v.text for k, v in loaded.items()}

    # Changed text updates in place; no duplicate rows.
    graph.facts["fact_1"].text = "service must listen on 5000"
    store.save_task_graph("run-1", graph)
    loaded3 = {f.fact_id: f for f in store.facts()}
    assert len(loaded3) == 50
    assert loaded3["fact_1"].text == "service must listen on 5000"

    # Standalone add_fact keeps working (backward compatibility).
    store.add_fact(_fact("fact_solo", "solo must persist"))
    assert any(f.fact_id == "fact_solo" for f in store.facts())


def test_large_fact_checkpoint_is_not_amplified(tmp_path):
    """2,000 facts: old code opened 2,000 connections + commits per
    checkpoint; the batched transaction must stay fast and stable across
    repeated saves."""
    store = SQLiteTaskGraphStore(tmp_path / "big.sqlite")
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="N-1", goal="g", role="coder", status="completed"))
    for i in range(2000):
        graph.add_fact(_fact(f"fact_{i}", f"durable statement {i} must hold"))

    started = time.monotonic()
    store.save_task_graph("run-1", graph)
    first = time.monotonic() - started
    started = time.monotonic()
    store.save_task_graph("run-1", graph)
    second = time.monotonic() - started
    assert first < 20, f"first save too slow: {first:.1f}s"
    assert second < 20, f"unchanged re-save too slow: {second:.1f}s"
    assert len(store.facts()) == 2000
