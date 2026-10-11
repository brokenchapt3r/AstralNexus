from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from enum import Enum
from datetime import datetime, timezone
from threading import RLock
from functools import wraps
from typing import Any, Dict, List, Optional

from AstralNexus.core.state_machine import TaskStatus, assert_transition, normalize_status
from AstralNexus.core.edge_collection import EdgeCollection
from AstralNexus.core.contract_metadata import ContractMetadata, CONTRACT_FIELDS, LegacyDefaultList, LegacyDefaultDict


def synchronized_graph(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self._mutation_lock:
            return function(self, *args, **kwargs)
    return wrapped


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

    # Explicit data-plane contract.  These fields are intentionally separate
    # from free-form metadata so scheduling and validation cannot lose the
    # producer/consumer boundary between graph patches and Workers.
    owned_paths: List[str] = field(default_factory=LegacyDefaultList)
    input_artifacts: List[str] = field(default_factory=LegacyDefaultList)
    output_artifacts: List[str] = field(default_factory=LegacyDefaultList)
    handoff_contract: Dict[str, Any] = field(default_factory=LegacyDefaultDict)
    
    expected_outputs: List[Any] = field(default_factory=LegacyDefaultList)

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
        if name in CONTRACT_FIELDS:
            if isinstance(value, (LegacyDefaultList, LegacyDefaultDict)):
                self.__dict__.setdefault('_legacy_contract_defaults', set()).add(name)
                value = CONTRACT_FIELDS[name]()
            else:
                self.__dict__.get('_legacy_contract_defaults', set()).discard(name)
            if not isinstance(value, CONTRACT_FIELDS[name]):
                raise ValueError(f'node {name} must be {CONTRACT_FIELDS[name].__name__}')
            super().__setattr__(name, value)
            metadata = self.__dict__.get('metadata')
            if isinstance(metadata, ContractMetadata):
                dict.__setitem__(metadata, name, value)
                contract = metadata.get('task_contract')
                if isinstance(contract, ContractMetadata): dict.__setitem__(contract, name, value)
            return
        if name == 'metadata':
            previous = self.__dict__.get('metadata')
            if isinstance(previous, ContractMetadata) and previous is value:
                return  # |= already updated the active projection.
            metadata = ContractMetadata(value or {}, owner=self)
            legacy_defaults = self.__dict__.pop('_legacy_contract_defaults', set())
            legacy_defaults.update(key for key in CONTRACT_FIELDS if key not in self.__dict__)
            ignored_legacy = {}
            for key, factory in CONTRACT_FIELDS.items():
                canonical = getattr(self, key, factory())
                # One-time legacy metadata import. Subsequent writes through
                # either compatibility face target the canonical attribute.
                if key in legacy_defaults and key in metadata:
                    canonical = metadata[key]
                    if not isinstance(canonical, factory): raise ValueError(f'invalid legacy node {key}')
                elif key in metadata and metadata[key] != canonical:
                    ignored_legacy[key] = deepcopy(metadata[key])
                super().__setattr__(key, canonical)
                dict.__setitem__(metadata, key, canonical)
            self._ignored_legacy_contracts: dict[str, Any] = ignored_legacy
            if isinstance(metadata.get('task_contract'), dict):
                metadata['task_contract'] = metadata['task_contract']
            if isinstance(previous, ContractMetadata): previous.detach()
            value = metadata
        super().__setattr__(name, value)

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.metadata = state.get('metadata') or {}
        if state.get('_ignored_legacy_contracts') and not self._ignored_legacy_contracts:
            self._ignored_legacy_contracts = state['_ignored_legacy_contracts']
    
@dataclass(frozen=True)
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
    
    # Responsible node; None for run-level artifacts.
    node_id: str | None
    
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
    
    # Associated node; None for run-level metrics.
    node_id: str | None
    
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
        self._mutation_lock = RLock()
        self._edge_revision = 0
        self._indexed_revision = -1
        self._outgoing: Dict[str, List[TaskEdge]] = {}
        self._incoming: Dict[str, List[TaskEdge]] = {}
        self._edge_lookup: Dict[tuple[str, str, str], TaskEdge] = {}
        self.nodes: NodeList = NodeList()
        self.edges = []
        self.facts: Dict[str, Fact] = {}
        self.artifacts: Dict[str, Artifact] = {}
        self.metrics: Dict[str, Metric] = {}
        
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
    
    @property
    def edges(self) -> List[TaskEdge]:
        return self._edges

    @edges.setter
    def edges(self, values: List[TaskEdge]) -> None:
        with self._mutation_lock:
            self._edges = EdgeCollection(values, self._mutation_lock, self._edges_changed)
            self._edges_changed()

    def _edges_changed(self) -> None:
        self._edge_revision += 1

    def _ensure_edge_indexes(self) -> None:
        if self._indexed_revision == self._edge_revision:
            return
        self._outgoing, self._incoming, self._edge_lookup = {}, {}, {}
        for edge in self.edges:
            self._index_edge(edge)
        self._indexed_revision = self._edge_revision

    def _index_edge(self, edge: TaskEdge) -> None:
        self._outgoing.setdefault(edge.source, []).append(edge)
        self._incoming.setdefault(edge.target, []).append(edge)
        self._edge_lookup[(edge.source, edge.target, edge.edge_type)] = edge

    @synchronized_graph
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
    
    @synchronized_graph
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
    
    @synchronized_graph
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

        self._ensure_edge_indexes()
        existing = self._edge_lookup.get((edge.source, edge.target, edge.edge_type))
        if existing is not None:
            existing.metadata.update(edge.metadata)
            if edge.edge_type in {"dependency", "depends_on"} and edge.source not in self.nodes[edge.target].dependencies:
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
        self._index_edge(edge)
        self._indexed_revision = self._edge_revision
        return edge
    
    @synchronized_graph
    def _would_create_cycle(self, source: str, target: str) -> bool:
        """Check if adding edge source->target would create a cycle."""
        self._ensure_edge_indexes()
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
            for edge in self._outgoing.get(node, ()):
                if edge.edge_type in {"dependency", "depends_on"}:
                    stack.append(edge.target)
        return False
    
    def get_outgoing_edges(self, node_id: str) -> List[TaskEdge]:
        """Get all outgoing edges from a node.
        
        Args:
            node_id: The ID of the node
            
        Returns:
            List of outgoing edges
        """
        with self._mutation_lock:
            self._ensure_edge_indexes()
            return list(self._outgoing.get(node_id, ()))
    
    def get_incoming_edges(self, node_id: str) -> List[TaskEdge]:
        """Get all incoming edges to a node.
        
        Args:
            node_id: The ID of the node
            
        Returns:
            List of incoming edges
        """
        with self._mutation_lock:
            self._ensure_edge_indexes()
            return list(self._incoming.get(node_id, ()))
    
    @synchronized_graph
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
    
    @synchronized_graph
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
    
    @synchronized_graph
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
    
    def resolve_dependency(self, node_id: str) -> Optional[TaskNode]:
        """Resolve one unambiguous producer; replacement is not completion.

        Missing, cyclic or ambiguous replacement chains fail closed. Historical
        artifacts from a superseded producer do not satisfy its successor.
        """
        seen: set[str] = set()
        while node_id not in seen:
            seen.add(node_id)
            node = self.nodes.get(node_id)
            if node is None or node.status != "superseded":
                return node
            successors = [
                candidate for candidate in self.nodes.values()
                if candidate.status != "cancelled"
                and str(candidate.metadata.get("recovery_of")
                        or candidate.metadata.get("supersedes_node_id") or "") == node_id
            ]
            if len(successors) != 1:
                return None
            node_id = successors[0].node_id
        return None

    def dependency_available(self, consumer: TaskNode, node_id: str,
                             *, for_verification: bool = False) -> bool:
        producer = self.resolve_dependency(node_id)
        if producer is None or producer.node_id == consumer.node_id:
            return False
        allowed = {"completed"}
        if consumer.metadata.get("trigger") in {"verification_failure", "fault_recovery"}:
            allowed.update({"repair_pending", "failed"})
        if for_verification:
            # Submitted evidence can be inspected before its lifecycle closes.
            allowed.update({"result_submitted", "verifying", "repair_pending"})
        return producer.status in allowed

    def readiness_blockers(self, node: TaskNode) -> Dict[str, List[str]]:
        """Explain readiness using the same dependency and evidence gate as scheduling."""
        dependencies = []
        for dep_id in node.dependencies:
            if not self.dependency_available(node, dep_id):
                dependencies.append(dep_id)

        effective_producers = {
            producer.node_id for dep_id in node.dependencies
            if (producer := self.resolve_dependency(dep_id)) is not None
        }
        missing_artifacts = []
        for required in node.input_artifacts:
            normalized = str(required).replace("\\", "/")
            matching = [
                artifact for artifact_id, artifact in self.artifacts.items()
                if (
                    normalized == str(artifact_id).replace("\\", "/")
                    or normalized == str(artifact.path).replace("\\", "/")
                    or str(artifact.path).replace("\\", "/").endswith("/" + normalized)
                )
                and getattr(artifact.acceptance_status, "value", artifact.acceptance_status)
                == ArtifactAcceptanceStatus.ACCEPTED.value
                and (not node.dependencies or artifact.node_id in effective_producers)
            ]
            if not matching:
                missing_artifacts.append(str(required))
        return {
            "unsatisfied_dependencies": dependencies,
            "missing_input_artifacts": missing_artifacts,
        }

    @synchronized_graph
    def promote_waiting_nodes(self) -> List[str]:
        """Promote only nodes with satisfied dependencies and accepted handoffs."""
        promoted = []
        for node in self.nodes.values():
            if node.status in {"admitted", "waiting_dependencies"}:
                if not any(self.readiness_blockers(node).values()):
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
        for edge in self.get_outgoing_edges(node_id):
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
        for edge in self.get_incoming_edges(node_id):
            if edge.target == node_id and edge.edge_type in {"depends_on", "dependency"}:
                dependency = self.nodes.get(edge.source)
                if dependency:
                    upstream.append(dependency)
        return upstream
    
    @synchronized_graph
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
    
