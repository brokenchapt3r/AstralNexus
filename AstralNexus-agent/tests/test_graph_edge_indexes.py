from __future__ import annotations
from copy import deepcopy
from dataclasses import FrozenInstanceError
import pytest
from AstralNexus.core.task_graph import TaskGraph, TaskNode, TaskEdge
from AstralNexus.core.graph_transaction import TaskGraphTransaction


def graph():
    result = TaskGraph()
    for name in ("A", "B", "C"):
        result.add_node(TaskNode(node_id=name, role="coder", goal=name))
    result.add_edge("A", "B", "depends_on")
    return result


@pytest.mark.parametrize("change", ["append", "extend", "insert", "slice", "replace"])
def test_external_edge_list_changes_invalidate_indexes(change):
    g = graph()
    assert len(g.get_outgoing_edges("A")) == 1
    edge = TaskEdge("A", "C", "depends_on")
    if change == "append": g.edges.append(edge)
    elif change == "extend": g.edges.extend([edge])
    elif change == "insert": g.edges.insert(0, edge)
    elif change == "slice": g.edges[:] = [*g.edges, edge]
    else: g.edges = [*g.edges, edge]
    assert {item.target for item in g.get_outgoing_edges("A")} == {"B", "C"}
    assert g.get_incoming_edges("C") == [edge]
    assert g._would_create_cycle("C", "A")


@pytest.mark.parametrize("change", ["pop", "remove", "del", "clear", "replace"])
def test_removing_edges_never_leaves_stale_adjacency(change):
    g = graph()
    assert g.get_incoming_edges("B")
    if change == "pop": g.edges.pop()
    elif change == "remove": g.edges.remove(g.edges[0])
    elif change == "del": del g.edges[:]
    elif change == "clear": g.edges.clear()
    else: g.edges = []
    assert g.get_outgoing_edges("A") == [] and g.get_incoming_edges("B") == []
    assert not g._would_create_cycle("B", "A")


def test_duplicate_edge_merges_metadata_without_duplicate_neighbors():
    g = graph()
    first = g.edges[0]
    assert g.add_edge(TaskEdge("A", "B", "depends_on", {"updated": True})) is first
    assert len(g.get_outgoing_edges("A")) == 1 and first.metadata["updated"]
    with pytest.raises(FrozenInstanceError): first.target = "C"


def test_failed_transaction_restores_indexes_as_well_as_edges():
    g = graph()
    with pytest.raises(RuntimeError):
        with TaskGraphTransaction(g):
            g.edges.clear()
            g.add_edge("B", "C", "depends_on")
            assert g.get_incoming_edges("C")
            raise RuntimeError("rollback")
    assert [edge.target for edge in g.get_outgoing_edges("A")] == ["B"]
    assert g.get_outgoing_edges("B") == []
    assert isinstance(deepcopy(g.edges), list)
    with pytest.raises(ValueError, match="cycle"): g.add_edge("B", "A", "depends_on")
