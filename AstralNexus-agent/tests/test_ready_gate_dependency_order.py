"""Ready-gate: selection must apply the same readiness gate as promotion.

Live defect (r13c, 2026-09-29 08:07:08Z, commit_seq 161-164):
a verification-recovery patch added producer N-120, added the dependency edge
N-118 -> N-120, and updated N-118 from repair_pending -> ready in the same
commit.  Router.select_ready_nodes() trusts ``status == "ready"`` without
recomputing readiness_blockers, so the wave selected ["N-118", "N-120"] and
with max_parallel=1 N-118 started BEFORE N-120 ever ran -- the consumer
executed ahead of the very producer the root-cause routing had created for
it, invalidating the patch's own ordering intent.
"""

import unittest

from AstralNexus.core.router import Router
from AstralNexus.core.task_graph import TaskGraph, TaskNode


class _Recorder:
    """Minimal trace recorder capturing (event_type, payload) pairs."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type, payload=None):  # noqa: ANN001, ANN201
        self.events.append((str(event_type), dict(payload or {})))


def _complete(graph: TaskGraph, node_id: str) -> None:
    for status in ("running", "result_submitted", "verifying", "completed"):
        graph.update_node_status(node_id, status)


class ReadyGateDependencyOrderTest(unittest.TestCase):
    def _recovery_patch_graph(self) -> TaskGraph:
        """Reproduce the patch outcome: producer admitted, consumer forced ready."""
        graph = TaskGraph()
        producer = TaskNode(
            node_id="P-1", goal="produce upstream source facts", role="coder"
        )
        consumer = TaskNode(
            node_id="C-1",
            goal="consume upstream source facts",
            role="synthesizer",
            dependencies=["P-1"],
        )
        graph.add_node(producer)
        graph.add_node(consumer)
        graph.update_node_status("P-1", "ready")
        graph.update_node_status("C-1", "ready")
        return graph

    def test_consumer_with_unsatisfied_dep_is_deferred(self):
        graph = self._recovery_patch_graph()
        router = Router(graph, trace_recorder=_Recorder())
        selected = [node.node_id for node in router.select_ready_nodes()]
        self.assertIn("P-1", selected)
        self.assertNotIn(
            "C-1", selected,
            "consumer must not be selected while its declared producer is unfinished",
        )

    def test_deferred_consumer_selected_after_producer_completes(self):
        graph = self._recovery_patch_graph()
        router = Router(graph)
        self.assertNotIn("C-1", [n.node_id for n in router.select_ready_nodes()])
        _complete(graph, "P-1")
        self.assertIn("C-1", [n.node_id for n in router.select_ready_nodes()])

    def test_repair_leniency_for_failed_parent_preserved(self):
        """Repair consumers may still run against failed/repair_pending parents."""
        graph = TaskGraph()
        failed = TaskNode(node_id="F-1", goal="failed producer", role="coder")
        repair = TaskNode(
            node_id="R-1",
            goal="repair consumer",
            role="synthesizer",
            dependencies=["F-1"],
        )
        repair.metadata["trigger"] = "verification_failure"
        graph.add_node(failed)
        graph.add_node(repair)
        graph.update_node_status("F-1", "repair_pending")
        graph.update_node_status("F-1", "failed")
        graph.update_node_status("R-1", "ready")
        router = Router(graph)
        self.assertIn("R-1", [n.node_id for n in router.select_ready_nodes()])

    def test_deferral_is_recorded_with_blockers(self):
        graph = self._recovery_patch_graph()
        recorder = _Recorder()
        router = Router(graph, trace_recorder=recorder)
        router.select_ready_nodes()
        deferred = [p for e, p in recorder.events if e == "router.ready_gate.deferred"]
        self.assertEqual(len(deferred), 1)
        entry = deferred[0]["deferred"][0]
        self.assertEqual(entry["node_id"], "C-1")
        self.assertEqual(entry["unsatisfied_dependencies"], ["P-1"])

    def test_no_deferral_event_when_all_selectable(self):
        graph = TaskGraph()
        node = TaskNode(node_id="A-1", goal="independent work", role="coder")
        graph.add_node(node)
        graph.update_node_status("A-1", "ready")
        recorder = _Recorder()
        router = Router(graph, trace_recorder=recorder)
        selected = [n.node_id for n in router.select_ready_nodes()]
        self.assertEqual(selected, ["A-1"])
        self.assertNotIn(
            "router.ready_gate.deferred", [e for e, _ in recorder.events]
        )


if __name__ == "__main__":
    unittest.main()
