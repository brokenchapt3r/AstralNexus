"""Bounded-stream event contracts adapted from DeerFlow's MIT test suite."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from AstralNexus.runtime.event_bus import RuntimeEventBus


def test_history_is_bounded_and_retains_the_latest_terminal_event(tmp_path):
    bus = RuntimeEventBus(tmp_path, history_size=3)
    for index in range(5):
        bus.publish("agent.activity", {"summary": f"step-{index}"})
    terminal = bus.publish("task.completed", {"summary": "done", "status": "completed"})

    history = bus.history()
    assert len(history) == 3
    assert [item["payload"]["summary"] for item in history] == ["step-3", "step-4", "done"]
    assert history[-1]["payload"]["runtime_event_id"] == terminal["payload"]["runtime_event_id"]


def test_history_is_a_deep_copy_of_canonical_events(tmp_path):
    bus = RuntimeEventBus(tmp_path)
    bus.publish("agent.activity", {"details": {"items": ["original"]}})

    first = bus.history()
    first[0]["payload"]["details"]["items"].append("mutated")

    assert bus.history()[0]["payload"]["details"]["items"] == ["original"]


def test_subscriber_mutation_cannot_corrupt_history_or_other_subscribers(tmp_path):
    bus = RuntimeEventBus(tmp_path)
    observed = []

    def mutating_subscriber(event):
        event["payload"]["details"]["value"] = "corrupted"

    bus.subscribe(mutating_subscriber)
    bus.subscribe(observed.append)
    published = bus.publish("agent.activity", {"details": {"value": "safe"}})

    assert published["payload"]["details"]["value"] == "safe"
    assert observed[0]["payload"]["details"]["value"] == "safe"
    assert bus.history()[0]["payload"]["details"]["value"] == "safe"


def test_failing_subscriber_does_not_block_delivery_and_unsubscribe_is_idempotent(tmp_path):
    bus = RuntimeEventBus(tmp_path)
    observed = []

    def broken(_event):
        raise RuntimeError("subscriber failed")

    unsubscribe_broken = bus.subscribe(broken)
    unsubscribe_observer = bus.subscribe(observed.append)
    bus.publish("agent.activity", {"summary": "first"})
    unsubscribe_broken()
    unsubscribe_broken()
    unsubscribe_observer()
    unsubscribe_observer()
    bus.publish("agent.activity", {"summary": "second"})

    assert [item["payload"]["summary"] for item in observed] == ["first"]
    assert [item["payload"]["summary"] for item in bus.history()] == ["first", "second"]


def test_concurrent_publish_keeps_all_events_and_unique_ids(tmp_path):
    bus = RuntimeEventBus(tmp_path, history_size=64)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: bus.publish("agent.activity", {"summary": f"step-{index}"}),
                range(50),
            )
        )

    history = bus.history()
    ids = [item["payload"]["runtime_event_id"] for item in history]
    assert len(history) == 50
    assert len(set(ids)) == 50

