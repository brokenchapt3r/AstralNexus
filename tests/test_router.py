"""Test cases for Router."""

import unittest
from unittest.mock import Mock, patch

from AstralNexus.core.router import Router
from AstralNexus.core.task_graph import TaskGraph, TaskNode


class TestRouter(unittest.TestCase):
    """Test cases for Router functionality."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.task_graph = TaskGraph()
        self.router = Router(self.task_graph)
        
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

    def tearDown(self):
        """Clean up test fixtures after each test method."""
        self.task_graph = None
        self.router = None
        self.node1 = None
        self.node2 = None
        self.node3 = None

    def test_select_ready_nodes(self):
        """Test selecting ready nodes."""
        # Initially, nodes start in "admitted" state. Transition to "ready" first.
        self.task_graph.update_node_status("T-001", "ready")
        self.task_graph.update_node_status("T-003", "ready")
        
        ready_nodes = self.router.select_ready_nodes()
        ready_node_ids = [node.node_id for node in ready_nodes]
        
        self.assertIn("T-001", ready_node_ids)
        self.assertIn("T-003", ready_node_ids)
        self.assertNotIn("T-002", ready_node_ids)
        
        # Transition T-001 through full flow: ready -> running -> result_submitted -> verifying -> completed
        self.task_graph.update_node_status("T-001", "running")
        self.task_graph.update_node_status("T-001", "result_submitted")
        self.task_graph.update_node_status("T-001", "verifying")
        self.task_graph.update_node_status("T-001", "completed")
        
        # Now T-002 should be ready (dependency satisfied), but T-001 should NOT be in ready nodes
        self.task_graph.update_node_status("T-002", "ready")
        ready_nodes = self.router.select_ready_nodes()
        ready_node_ids = [node.node_id for node in ready_nodes]
        
        self.assertNotIn("T-001", ready_node_ids)  # T-001 is completed, not ready
        self.assertIn("T-002", ready_node_ids)
        self.assertIn("T-003", ready_node_ids)

    def test_encode_node_features(self):
        """Test encoding node features."""
        # Encode features for a node
        features = self.router.encode_node_features(self.node1)
        
        # Verify features are encoded
        self.assertIsInstance(features, dict)
        self.assertIn("goal_embedding", features)
        self.assertIn("dependency_count", features)
        self.assertIn("risk_level", features)
        self.assertIn("sensitivity", features)

    @patch('AstralNexus.core.router.get_agent_registry')
    def test_choose_role(self, mock_get_agent_registry):
        """Test choosing a role for a node."""
        # Mock the agent registry
        mock_registry = Mock()
        mock_registry.roles = {
            "planner": Mock(capabilities=["planning"]),
            "researcher": Mock(capabilities=["research"]),
            "coder": Mock(capabilities=["coding"])
        }
        mock_get_agent_registry.return_value = mock_registry
        
        # Choose a role for a node
        chosen_role = self.router.choose_role(self.node1)
        
        # Verify a role was chosen
        self.assertIsNotNone(chosen_role)
        self.assertIn(chosen_role, ["planner", "researcher", "coder"])

    def test_retrieve_minimal_context(self):
        """Test retrieving minimal context for a node."""
        # Retrieve minimal context for a node
        context = self.router.retrieve_minimal_context(self.node1)
        
        # Verify context is retrieved
        self.assertIsInstance(context, dict)
        self.assertIn("constraints", context)
        self.assertIn("recent_decisions", context)
        self.assertIn("relevant_facts", context)
        self.assertIn("relevant_artifacts", context)
        self.assertIn("recent_failures", context)

    def test_build_task_contract(self):
        """Test building a task contract."""
        # Build a task contract
        contract = self.router.build_task_contract(
            self.node1, 
            "planner", 
            {"test": "context"}, 
            "local"
        )
        
        # Verify contract is built
        self.assertIsInstance(contract, dict)
        self.assertIn("node_id", contract)
        self.assertIn("role", contract)
        self.assertIn("context", contract)
        self.assertIn("resource", contract)
        self.assertIn("acceptance_criteria", contract)

    def test_refresh_sparse_topology(self):
        """Test refreshing sparse topology."""
        # Refresh the sparse topology
        communication_plan = self.router.refresh_sparse_topology()
        
        # Verify communication plan is generated
        self.assertIsInstance(communication_plan, dict)
        self.assertIn("edges", communication_plan)
        self.assertIn("next_nodes", communication_plan)


if __name__ == "__main__":
    unittest.main()
