from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from AstralNexus.core.state_machine import TaskStatus, assert_transition, normalize_status


class ArtifactAcceptanceStatus(str, Enum):
    """Verifier-controlled artifact admission state."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class TaskNode:
    """Represents a task node in the semantic task graph."""
    
    # Unique identifier for the node
    node_id: str
    
    # Goal or objective of the task
    goal: str
    
    # Role responsible for executing this task
    role: str
    
    # Current status of the task
    status: str = TaskStatus.ADMITTED.value
    
    # Dependencies - tasks that must be completed before this one
    dependencies: List[str] = field(default_factory=list)
    
    # Constraints that must be satisfied
    constraints: List[str] = field(default_factory=list)
    
    # Inputs required for this task
    inputs: List[str] = field(default_factory=list)
    
    # Outputs produced by this task
    outputs: List[str] = field(default_factory=list)
    
    # Risk level of the task
    risk_level: str = "low"  # low, medium, high
    
    # Sensitivity level of the data involved
    sensitivity: str = "public"  # public, internal, sensitive
    
    # Resource tier required
    resource_tier: str = "local"  # local, edge, cloud
    
    # Number of retries
    retry_count: int = 0
    
    # Acceptance criteria
    acceptance_criteria: List[str] = field(default_factory=list)
    
    # Creation and update timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name: str, value: Any) -> None:
        """Validate task lifecycle values at the object boundary.

        This protects the graph from old integrations that still assign
        ``node.status`` directly. Transition legality remains the
        responsibility of ``TaskGraph.update_node_status``.
        """
        if name == "status" and value is not None:
            value = normalize_status(value)
        super().__setattr__(name, value)
    
@dataclass
class TaskEdge:
    """Represents a relationship between task nodes."""
    
    # Source node ID
    source: str
    
    # Target node ID
    target: str
    
    # Type of relationship
    edge_type: str  # depends_on, evidence_for, produces, revises, verifies, blocks, communicates_to
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Fact:
    """Represents a fact extracted during task execution."""
    
    # Unique identifier
    fact_id: str
    
    # The fact statement
    text: str
    
    # Source of the fact (node_id or artifact)
    source: str
    
    # Confidence level (0.0 to 1.0)
    confidence: float
    
    # Timestamp when the fact was recorded
    recorded_at: str
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Artifact:
    """Represents an artifact produced during task execution.
    
    Lifecycle classification (lifecycle_class):
    - "scaffold": Temporary working artifacts (drafts, intermediate states, scratch data)
      that support task execution but are not final deliverables. These are excluded from
      EvidencePacket by default and may be evicted from active working set.
    - "durable": Final conclusions, evidence, constraints, and deliverables that must be
      preserved in EvidencePacket and maintained in working set for downstream dependencies.
    """
    
    # Unique identifier
    artifact_id: str
    
    # Responsible node
    node_id: str
    
    # Type of artifact
    artifact_type: str
    
    # Path or reference to the artifact
    path: str
    
    # Description of the artifact
    description: str
    
    # Acceptance status
    acceptance_status: ArtifactAcceptanceStatus = ArtifactAcceptanceStatus.PENDING
    
    # Lifecycle classification: scaffold (temporary) or durable (persistent)
    lifecycle_class: str = "durable"  # scaffold, durable
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.acceptance_status, ArtifactAcceptanceStatus):
            self.acceptance_status = ArtifactAcceptanceStatus(
                str(self.acceptance_status)
            )


@dataclass
class Metric:
    """Represents a metric collected during task execution."""
    
    # Unique identifier
    metric_id: str
    
    # Associated node
    node_id: str
    
    # Name of the metric
    name: str
    
    # Value of the metric
    value: float
    
    # Unit of measurement
    unit: str
    
    # Timestamp when the metric was recorded
    recorded_at: str = ""
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


class NodeList(Dict[str, TaskNode]):
    """Custom dict subclass that iterates over *values* (TaskNode) instead of keys.

    Design rationale (P2-1 — reserved API):
    ----------------------------------------
    ``NodeList`` is intentionally kept as a dedicated type rather than a plain
    ``dict`` so that downstream consumers (Orchestrator, Router, TraceRecorder)
    can iterate ``for node in graph.nodes`` and get ``TaskNode`` objects
    directly — this is the ergonomic convention used throughout the codebase.

    It is **not** a dead class:
      - ``TaskGraph.__init__`` instantiates ``self.nodes = NodeList()``
      - Orchestrator loops ``for node in self.task_graph.nodes`` dozens of times
      - Router uses the same iteration pattern for topology analysis

    The class also serves as a future extension point: if we later need to add
    iteration filters (e.g. ``active_only=True``) or snapshot semantics, we can
    override ``__iter__`` / ``values()`` here without touching call-sites.
    """

    def __iter__(self):
        """Iterate over node values instead of keys."""
        return iter(self.values())


class TaskGraph:
    """Semantic task graph representing a complex task as a directed acyclic graph."""
    
    def __init__(self, nodes=None, edges=None):
        """Initialize task graph.
        
        Args:
            nodes: Optional initial TaskNode objects.
            edges: Optional initial TaskEdge objects.
        """
        self.nodes: NodeList = NodeList()
        self.edges: List[TaskEdge] = []
        self.facts: Dict[str, Fact] = {}
        self.artifacts: Dict[str, Artifact] = {}
        self.metrics: Dict[str, Metric] = {}
        self._mutation_lock = RLock()
        
        if nodes:
            for node in nodes:
                if isinstance(node, dict):
                    self.add_node(node)
                else:
                    self.add_node(node)
        
        if edges:
            for edge in edges:
                if isinstance(edge, dict):
                    self.add_edge(edge)
                else:
                    self.add_edge(edge)
    
    def add_node(self, node_data) -> TaskNode:
        """Add a node to the task graph.
        
        Args:
            node_data: TaskNode instance or dictionary containing node attributes
            
        Returns:
            The created TaskNode
        """
        if isinstance(node_data, TaskNode):
            node = node_data
        elif isinstance(node_data, dict):
            node = TaskNode(**node_data)
        else:
            raise TypeError(f"Expected TaskNode or dict, got {type(node_data)}")

        # Validate the canonical lifecycle value at the graph boundary.
        node.status = normalize_status(node.status)
        
        if node.created_at is None:
            node.created_at = datetime.now(timezone.utc).isoformat()
        
        self.nodes[node.node_id] = node
        return node
    
    def get_node(self, node_id: str) -> Optional[TaskNode]:
        """Get a node by its ID.
        
        Args:
            node_id: The ID of the node to retrieve
            
        Returns:
            The TaskNode if found, None otherwise
        """
        return self.nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        """Return whether the graph contains a node."""
        return node_id in self.nodes
    
    def get_all_nodes(self) -> List[TaskNode]:
        """Get all nodes in the task graph.
        
        Returns:
            List of all TaskNode objects
        """
        return list(self.nodes.values())
    
    def update_node_status(self, node_id: str, status: str) -> bool:
        """Update the status of a node with state machine enforcement.
        
        Args:
            node_id: The ID of the node to update
            status: The new status (must be a valid transition)
            
        Returns:
            True if the update was successful, False if node not found
            
        Raises:
            ValueError: If the state transition is invalid according to state_machine
        """
        if node_id in self.nodes:
            node = self.nodes[node_id]
            current_status = normalize_status(node.status)
            target_status = normalize_status(status)
            
            # Enforce state machine
            assert_transition(current_status, target_status)
            
            # Perform transition
            node.status = target_status
            node.updated_at = datetime.now(timezone.utc).isoformat()
            return True
        return False
    
    def add_edge(self, edge_or_source, target=None, edge_type=None, metadata=None) -> TaskEdge:
        """Add an edge between two nodes.
        
        Args:
            edge_or_source: TaskEdge instance, dictionary, or source node ID
            target: The target node ID (if edge_or_source is a string)
            edge_type: The type of relationship (if edge_or_source is a string)
            metadata: Additional metadata for the edge
        Returns:
            The created TaskEdge
        """
        if isinstance(edge_or_source, TaskEdge):
            edge = edge_or_source
        elif isinstance(edge_or_source, dict):
            edge_data = {
                key: value
                for key, value in edge_or_source.items()
                if key in {"source", "target", "edge_type", "metadata"}
            }
            edge = TaskEdge(**edge_data)
        elif isinstance(edge_or_source, str):
            edge = TaskEdge(edge_or_source, target, edge_type, metadata or {})
        else:
            raise TypeError(f"Expected TaskEdge, dict, or string, got {type(edge_or_source)}")
        
        if edge.source not in self.nodes:
            raise ValueError(f"Source node {edge.source} does not exist")
        if edge.target not in self.nodes:
            raise ValueError(f"Target node {edge.target} does not exist")

        for existing in self.edges:
            if (existing.source == edge.source and
                existing.target == edge.target and
                existing.edge_type == edge.edge_type):
                existing.metadata.update(edge.metadata)
                if (
                    edge.edge_type in {"dependency", "depends_on"}
                    and edge.source not in self.nodes[edge.target].dependencies
                ):
                    self.nodes[edge.target].dependencies.append(edge.source)
                return existing

        if edge.edge_type in {"dependency", "depends_on"}:
            if self._would_create_cycle(edge.source, edge.target):
                raise ValueError(f"Adding edge {edge.source}->{edge.target} would create cycle")
        
        # Keep the node-level dependency list in sync with dependency edges.
        # Router/ready-wave scheduling checks TaskNode.dependencies, while
        # reports and topology metrics inspect TaskGraph.edges. Both views must
        # describe the same semantic task graph.
        if edge.edge_type in {"dependency", "depends_on"} and edge.target in self.nodes:
            target_node = self.nodes[edge.target]
            if edge.source not in target_node.dependencies:
                target_node.dependencies.append(edge.source)
        self.edges.append(edge)
        return edge
    
    def _would_create_cycle(self, source: str, target: str) -> bool:
        """Check if adding edge source->target would create a cycle."""
        # If source is reachable from target, source->target closes a cycle.
        visited = set()
        stack = [target]
        while stack:
            node = stack.pop()
            if node == source:
                return True
            if node in visited:
                continue
            visited.add(node)
            # Follow outgoing edges
            for edge in self.edges:
                if edge.source == node and edge.edge_type in {"dependency", "depends_on"}:
                    stack.append(edge.target)
        return False
    
    def get_outgoing_edges(self, node_id: str) -> List[TaskEdge]:
        """Get all outgoing edges from a node.
        
        Args:
            node_id: The ID of the node
            
        Returns:
            List of outgoing edges
        """
        return [edge for edge in self.edges if edge.source == node_id]
    
    def get_incoming_edges(self, node_id: str) -> List[TaskEdge]:
        """Get all incoming edges to a node.
        
        Args:
            node_id: The ID of the node
            
        Returns:
            List of incoming edges
        """
        return [edge for edge in self.edges if edge.target == node_id]
    
    def add_fact(self, fact_data) -> Fact:
        """Add a fact to the task graph.
        
        Args:
            fact_data: Fact instance or dictionary containing fact attributes
            
        Returns:
            The created Fact
        """
        if isinstance(fact_data, Fact):
            fact = fact_data
        elif isinstance(fact_data, dict):
            fact = Fact(**fact_data)
        else:
            raise TypeError(f"Expected Fact or dict, got {type(fact_data)}")
        
        self.facts[fact.fact_id] = fact
        return fact
    
    def add_artifact(self, artifact_data) -> Artifact:
        """Add an artifact to the task graph.
        
        Args:
            artifact_data: Artifact instance or dictionary containing artifact attributes
            
        Returns:
            The created Artifact
        """
        if isinstance(artifact_data, Artifact):
            artifact = artifact_data
        elif isinstance(artifact_data, dict):
            artifact = Artifact(**artifact_data)
        else:
            raise TypeError(f"Expected Artifact or dict, got {type(artifact_data)}")
        
        self.artifacts[artifact.artifact_id] = artifact
        return artifact
    
    def add_metric(self, metric_data) -> Metric:
        """Add a metric to the task graph.
        
        Args:
            metric_data: Metric instance or dictionary containing metric attributes
            
        Returns:
            The created Metric
        """
        if isinstance(metric_data, Metric):
            metric = metric_data
        elif isinstance(metric_data, dict):
            metric = Metric(**metric_data)
        else:
            raise TypeError(f"Expected Metric or dict, got {type(metric_data)}")
        
        self.metrics[metric.metric_id] = metric
        return metric
    
    def promote_waiting_nodes(self) -> List[str]:
        """Promote nodes waiting for dependencies to ready status.
        
        Checks nodes with status "admitted" or "waiting_dependencies" and
        promotes them to "ready" if all dependencies are satisfied.
        
        Returns:
            List of node IDs that were promoted
        """
        promoted = []
        for node in self.nodes.values():
            if node.status in ["admitted", "waiting_dependencies"]:
                # Check if all dependencies are satisfied
                deps_satisfied = True
                for dep_id in node.dependencies:
                    dep_node = self.nodes.get(dep_id)
                    dependency_satisfied = bool(
                        dep_node and dep_node.status in ["completed", "superseded"]
                    )
                    # Verification-repair workers consume the failed
                    # parent's submitted result, so they may run while the
                    # parent is repair_pending.  Ordinary downstream work
                    # still requires a completed dependency.
                    repair_worker = node.metadata.get("trigger") in {
                        "verification_failure", "fault_recovery"
                    }
                    if (
                        repair_worker
                        and dep_node
                        and dep_node.status in ["repair_pending", "failed"]
                    ):
                        dependency_satisfied = True
                    if not dependency_satisfied:
                        deps_satisfied = False
                        break
                
                if deps_satisfied:
                    self.update_node_status(node.node_id, "ready")
                    promoted.append(node.node_id)
        
        return promoted
    
    def get_ready_nodes(self) -> List[TaskNode]:
        """Get all nodes that are ready to be executed.
        
        A node is ready if its status is "ready".
        
        Note: "completed" nodes are NOT included (already finished).
        Use get_completed_nodes() if you need to query finished nodes.
        
        Returns:
            List of ready TaskNodes (executable only)
        """
        return [node for node in self.nodes.values() if node.status == "ready"]
    
    def get_completed_nodes(self) -> List[TaskNode]:
        """Get all completed nodes for reference.
        
        Returns:
            List of completed TaskNodes
        """
        return [node for node in self.nodes.values() if node.status == "completed"]
    
    def get_downstream_nodes(self, node_id: str) -> List[TaskNode]:
        """Get all nodes that depend on the given node.
        
        Args:
            node_id: The ID of the node
            
        Returns:
            List of downstream TaskNodes
        """
        downstream = []
        for edge in self.edges:
            if edge.source == node_id and edge.edge_type in {"depends_on", "dependency"}:
                dependent = self.nodes.get(edge.target)
                if dependent:
                    downstream.append(dependent)
        return downstream
    
    def get_upstream_nodes(self, node_id: str) -> List[TaskNode]:
        """Get all nodes that the given node depends on.
        
        Args:
            node_id: The ID of the node
            
        Returns:
            List of upstream TaskNodes
        """
        upstream = []
        for edge in self.edges:
            if edge.target == node_id and edge.edge_type in {"depends_on", "dependency"}:
                dependency = self.nodes.get(edge.source)
                if dependency:
                    upstream.append(dependency)
        return upstream
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert the task graph to a dictionary representation.
        
        Returns:
            Dictionary representation of the task graph
        """
        from dataclasses import asdict
        
        return {
            "nodes": [asdict(node) for node in self.nodes.values()],
            "edges": [asdict(edge) for edge in self.edges],
            "facts": [asdict(fact) for fact in self.facts.values()],
            "artifacts": [asdict(artifact) for artifact in self.artifacts.values()],
            "metrics": [asdict(metric) for metric in self.metrics.values()],
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskGraph:
        """Create a task graph from a dictionary representation.
        
        Args:
            data: Dictionary representation of the task graph
        Returns:
            The created TaskGraph
            
        Raises:
            ValueError: If strict=True and validation fails
        """
        graph = cls()
        
        # Add nodes
        for node_data in data.get("nodes", []):
            graph.add_node(node_data)
        
        # Add edges after all nodes so endpoint and DAG validation is mandatory.
        for edge_data in data.get("edges", []):
            graph.add_edge(edge_data)
        
        # Add facts
        for fact_data in data.get("facts", []):
            graph.add_fact(fact_data)
        
        # Add artifacts
        for artifact_data in data.get("artifacts", []):
            graph.add_artifact(artifact_data)
        
        # Add metrics
        for metric_data in data.get("metrics", []):
            graph.add_metric(metric_data)
        
        return graph
    
