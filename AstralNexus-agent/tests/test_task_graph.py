"""Test cases for TaskGraph."""

import unittest
from datetime import datetime, timezone
from pathlib import Path

from AstralNexus.core.task_graph import Artifact, Fact, Metric, TaskEdge, TaskGraph, TaskNode


class TestTaskGraph(unittest.TestCase):
    """Test cases for TaskGraph functionality."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.task_graph = TaskGraph()
        
        # Create test nodes
        self.node1 = TaskNode(
            node_id="T-001",
            goal="Initial task",
            role="planner"
        )
        
        self.node2 = TaskNode(
            node_id="T-002",
            goal="Dependent task",
            role="researcher",
            dependencies=["T-001"]
        )
        
        self.node3 = TaskNode(
            node_id="T-003",
            goal="Another task",
            role="coder"
        )
        
        # Add nodes to graph
        self.task_graph.add_node(self.node1)
        self.task_graph.add_node(self.node2)
        self.task_graph.add_node(self.node3)
        
        # Add edges
        self.task_graph.add_edge(TaskEdge(
            source="T-001",
            target="T-002",
            edge_type="depends_on"
        ))

    def tearDown(self):
        """Clean up test fixtures after each test method."""
        self.task_graph = None
        self.node1 = None
        self.node2 = None
        self.node3 = None

    def test_add_node(self):
        """Test adding a node to the task graph."""
        # Create a new node
        new_node = TaskNode(
            node_id="T-004",
            goal="New task",
            role="verifier"
        )
        
        # Add the node
        self.task_graph.add_node(new_node)
        
        # Verify the node was added
        retrieved_node = self.task_graph.get_node("T-004")
        self.assertIsNotNone(retrieved_node)
        self.assertEqual(retrieved_node.node_id, "T-004")
        self.assertEqual(retrieved_node.goal, "New task")
        self.assertEqual(retrieved_node.role, "verifier")

    def test_get_node(self):
        """Test retrieving a node from the task graph."""
        # Retrieve an existing node
        node = self.task_graph.get_node("T-001")
        self.assertIsNotNone(node)
        self.assertEqual(node.node_id, "T-001")
        self.assertEqual(node.goal, "Initial task")
        self.assertEqual(node.role, "planner")
        
        # Try to retrieve a non-existent node
        non_existent = self.task_graph.get_node("T-999")
        self.assertIsNone(non_existent)

    def test_update_node_status(self):
        """Test updating a node's status."""
        # First transition: admitted -> ready
        self.task_graph.update_node_status("T-001", "ready")
        
        # Verify the status was updated
        node = self.task_graph.get_node("T-001")
        self.assertEqual(node.status, "ready")
        self.assertIsNotNone(node.updated_at)
        
        # Second transition: ready -> running
        self.task_graph.update_node_status("T-001", "running")
        
        # Verify the status was updated again
        node = self.task_graph.get_node("T-001")
        self.assertEqual(node.status, "running")
        
        # Third transition: running -> result_submitted -> verifying -> completed
        self.task_graph.update_node_status("T-001", "result_submitted")
        self.task_graph.update_node_status("T-001", "verifying")
        self.task_graph.update_node_status("T-001", "completed")
        
        # Verify the status was updated again
        node = self.task_graph.get_node("T-001")
        self.assertEqual(node.status, "completed")

    def test_add_edge(self):
        """Test adding an edge to the task graph."""
        # Add a new edge
        edge = TaskEdge(
            source="T-002",
            target="T-003",
            edge_type="depends_on"
        )
        self.task_graph.add_edge(edge)
        
        # Verify the edge was added
        edges = self.task_graph.edges
        self.assertIn(edge, edges)
        
        # Verify edge properties
        added_edge = next(e for e in edges if e.source == "T-002" and e.target == "T-003")
        self.assertEqual(added_edge.edge_type, "depends_on")

    def test_get_ready_nodes(self):
        """Test getting ready nodes from the task graph."""
        # Initially, nodes start in "admitted" state. Need to transition them to "ready".
        # T-001 and T-003 have no dependencies, so they can go directly to ready
        self.task_graph.update_node_status("T-001", "ready")
        self.task_graph.update_node_status("T-003", "ready")
        
        ready_nodes = self.task_graph.get_ready_nodes()
        ready_node_ids = [node.node_id for node in ready_nodes]
        
        self.assertIn("T-001", ready_node_ids)
        self.assertIn("T-003", ready_node_ids)
        self.assertNotIn("T-002", ready_node_ids)  # T-002 depends on T-001
        
        # Transition T-001 through full flow: ready -> running -> result_submitted -> verifying -> completed
        self.task_graph.update_node_status("T-001", "running")
        self.task_graph.update_node_status("T-001", "result_submitted")
        self.task_graph.update_node_status("T-001", "verifying")
        self.task_graph.update_node_status("T-001", "completed")
        
        # Now T-002 should be ready (dependency satisfied), but T-001 should NOT be in ready nodes
        self.task_graph.update_node_status("T-002", "ready")
        ready_nodes = self.task_graph.get_ready_nodes()
        ready_node_ids = [node.node_id for node in ready_nodes]
        
        self.assertNotIn("T-001", ready_node_ids)  # T-001 is completed, not ready
        self.assertIn("T-002", ready_node_ids)  # T-002's dependency is satisfied
        self.assertIn("T-003", ready_node_ids)
        
        # T-001 should be in completed nodes
        completed_nodes = self.task_graph.get_completed_nodes()
        completed_node_ids = [node.node_id for node in completed_nodes]
        self.assertIn("T-001", completed_node_ids)

    def test_get_downstream_nodes(self):
        """Test getting downstream nodes."""
        # Get downstream nodes of T-001
        downstream = self.task_graph.get_downstream_nodes("T-001")
        downstream_ids = [node.node_id for node in downstream]
        self.assertIn("T-002", downstream_ids)
        
        self.assertIn("T-002", downstream_ids)  # T-002 depends on T-001
        self.assertNotIn("T-001", downstream_ids)  # Should not include the node itself
        self.assertNotIn("T-003", downstream_ids)  # T-003 is independent

    def test_get_upstream_nodes(self):
        """Test getting upstream nodes."""
        # Get upstream nodes of T-002
        upstream = self.task_graph.get_upstream_nodes("T-002")
        upstream_ids = [node.node_id for node in upstream]
        self.assertIn("T-001", upstream_ids)
        
        self.assertIn("T-001", upstream_ids)  # T-001 is a dependency of T-002
        self.assertNotIn("T-002", upstream_ids)  # Should not include the node itself
        self.assertNotIn("T-003", upstream_ids)  # T-003 is independent

    def test_to_dict(self):
        """Test converting task graph to dictionary."""
        # Convert to dictionary
        graph_dict = self.task_graph.to_dict()
        
        # Verify structure
        self.assertIn("nodes", graph_dict)
        self.assertIn("edges", graph_dict)
        self.assertIn("facts", graph_dict)
        self.assertIn("artifacts", graph_dict)
        self.assertIn("metrics", graph_dict)
        
        # Verify node count
        self.assertEqual(len(graph_dict["nodes"]), 3)
        
        # Verify edge count
        self.assertEqual(len(graph_dict["edges"]), 1)

    def test_from_dict(self):
        """Test creating task graph from dictionary."""
        # Create a dictionary representation
        graph_dict = {
            "nodes": [
                {
                    "node_id": "T-004",
                    "goal": "Test node",
                    "role": "tester",
                    "status": "admitted",
                    "dependencies": [],
                    "constraints": [],
                    "inputs": [],
                    "outputs": [],
                    "risk_level": "low",
                    "sensitivity": "public",
                    "resource_tier": "local",
                    "retry_count": 0,
                    "acceptance_criteria": [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {}
                },
                {
                    "node_id": "T-005",
                    "goal": "Dependency node",
                    "role": "tester",
                    "status": "completed",
                }
            ],
            "edges": [
                {
                    "source": "T-004",
                    "target": "T-005",
                    "edge_type": "depends_on",
                    "metadata": {}
                }
            ],
            "facts": [],
            "artifacts": [],
            "metrics": []
        }
        
        new_graph = TaskGraph.from_dict(graph_dict)
        
        # Verify the graph was created correctly
        self.assertEqual(len(new_graph.nodes), 2)
        self.assertEqual(len(new_graph.edges), 1)
        
        # Verify node properties
        node = new_graph.get_node("T-004")
        self.assertIsNotNone(node)
        self.assertEqual(node.goal, "Test node")
        self.assertEqual(node.role, "tester")


if __name__ == "__main__":
    unittest.main()
