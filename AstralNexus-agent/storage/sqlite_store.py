from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from AstralNexus.core.task_graph import Artifact, Fact, Metric, TaskEdge, TaskNode
from AstralNexus.core.state_machine import normalize_status


def _json_default(value: Any) -> Any:
    """Convert runtime objects stored in graph metadata to JSON values."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


class SQLiteTaskGraphStore:
    """SQLite-based storage for task graphs and related data."""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize the database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    goal TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    status TEXT,
                    constraints TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    goal TEXT,
                    role TEXT,
                    status TEXT,
                    dependencies TEXT,
                    constraints TEXT,
                    inputs TEXT,
                    outputs TEXT,
                    risk_level TEXT,
                    sensitivity TEXT,
                    resource_tier TEXT,
                    retry_count INTEGER,
                    acceptance TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    metadata TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS edges (
                    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    source TEXT,
                    target TEXT,
                    edge_type TEXT,
                    metadata TEXT,
                    UNIQUE(run_id, source, target, edge_type)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT,
                    message_type TEXT,
                    content TEXT,
                    recorded_at TEXT,
                    FOREIGN KEY (node_id) REFERENCES nodes (node_id)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    fact_id TEXT PRIMARY KEY,
                    text TEXT,
                    source TEXT,
                    confidence REAL,
                    recorded_at TEXT,
                    metadata TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    node_id TEXT,
                    artifact_type TEXT,
                    path TEXT,
                    description TEXT,
                    acceptance_status TEXT,
                    metadata TEXT,
                    FOREIGN KEY (node_id) REFERENCES nodes (node_id)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    metric_id TEXT PRIMARY KEY,
                    node_id TEXT,
                    name TEXT,
                    value REAL,
                    unit TEXT,
                    recorded_at TEXT,
                    metadata TEXT,
                    FOREIGN KEY (node_id) REFERENCES nodes (node_id)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fault_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fault_id TEXT,
                    fault_type TEXT,
                    target_node TEXT,
                    payload TEXT,
                    timestamp TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recovery_plans (
                    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fault_id TEXT,
                    strategy TEXT,
                    status TEXT,
                    recovery_nodes TEXT,
                    actions TEXT,
                    timestamp TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recovery_evaluations (
                    eval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fault_id TEXT,
                    final_status TEXT,
                    recovery_rate REAL,
                    average_recovery_steps INTEGER,
                    extra_tokens INTEGER,
                    timestamp TEXT
                )
            """)
            
            conn.commit()
    
    def create_run(self, run_id: str, goal: str, constraints: List[str]) -> None:
        """Create a new run record.
        
        Args:
            run_id: The run identifier
            goal: The goal of the run
            constraints: List of constraints
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, goal, started_at, status, constraints) VALUES (?, ?, ?, ?, ?)",
                (run_id, goal, datetime.now(timezone.utc).isoformat(), "running", _json_dumps(constraints))
            )
            conn.commit()

    def list_runs(self) -> List[Dict[str, Any]]:
        """Return persisted runs in newest-first order."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT run_id, goal, started_at, ended_at, status, constraints "
                "FROM runs ORDER BY started_at DESC, run_id DESC"
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            try:
                constraints = json.loads(row["constraints"] or "[]")
            except (TypeError, json.JSONDecodeError):
                constraints = []
            result.append({
                "run_id": row["run_id"],
                "goal": row["goal"] or "",
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "status": row["status"] or "unknown",
                "constraints": constraints if isinstance(constraints, list) else [],
            })
        return result

    def update_run_status(self, run_id: str, status: str) -> None:
        """Persist the current or terminal status of a run."""
        from AstralNexus.core.state_machine import normalize_run_status
        status = normalize_run_status(status)
        ended_at = datetime.now(timezone.utc).isoformat() if status in {
            "completed", "failed", "blocked", "cancelled",
        } else None
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = COALESCE(?, ended_at) "
                "WHERE run_id = ?",
                (status, ended_at, run_id),
            )
            conn.commit()
    
    def add_node(self, node_data: Dict[str, Any], run_id: Optional[str] = None) -> None:
        """Add a node to the task graph.
        
        Args:
            node_data: Dictionary containing node attributes
            run_id: Optional run ID to associate with this node
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO nodes (
                    node_id, run_id, goal, role, status, dependencies, constraints, inputs, outputs,
                    risk_level, sensitivity, resource_tier, retry_count, acceptance,
                    created_at, updated_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_data["node_id"],
                    run_id,
                    node_data["goal"],
                    node_data["role"],
            normalize_status(node_data.get("status", "admitted")),
                    _json_dumps(node_data.get("dependencies", [])),
                    _json_dumps(node_data.get("constraints", [])),
                    _json_dumps(node_data.get("inputs", [])),
                    _json_dumps(node_data.get("outputs", [])),
                    node_data.get("risk_level", "low"),
                    node_data.get("sensitivity", "public"),
                    node_data.get("resource_tier", "local"),
                    node_data.get("retry_count", 0),
                    _json_dumps(node_data.get("acceptance_criteria", [])),
                    node_data.get("created_at", datetime.now(timezone.utc).isoformat()),
                    node_data.get("updated_at", datetime.now(timezone.utc).isoformat()),
                    _json_dumps(node_data.get("metadata", {})),
                )
            )
            conn.commit()
    
    def get_node(self, node_id: str) -> Optional[TaskNode]:
        """Get a node by its ID.
        
        Args:
            node_id: The ID of the node to retrieve
            
        Returns:
            The TaskNode if found, None otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
            row = cursor.fetchone()
            
            if row:
                return TaskNode(
                    node_id=row[0],
                    goal=row[2],
                    role=row[3],
                    status=row[4],
                    dependencies=json.loads(row[5]),
                    constraints=json.loads(row[6]),
                    inputs=json.loads(row[7]),
                    outputs=json.loads(row[8]),
                    risk_level=row[9],
                    sensitivity=row[10],
                    resource_tier=row[11],
                    retry_count=row[12],
                    acceptance_criteria=json.loads(row[13]),
                    created_at=row[14],
                    updated_at=row[15],
                    metadata=json.loads(row[16]),
                )
        
        return None
    
    def add_edge(self, source: str, target: str, edge_type: str, metadata: Dict[str, Any] = None, run_id: str = None) -> None:
        """Add an edge between two nodes.
        
        Args:
            source: The source node ID
            target: The target node ID
            edge_type: The type of relationship
            metadata: Additional metadata for the edge
            run_id: Optional run ID for tracking which run created this edge
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO edges (run_id, source, target, edge_type, metadata) VALUES (?, ?, ?, ?, ?)",
                (run_id, source, target, edge_type, _json_dumps(metadata or {}))
            )
            conn.commit()
    
    def add_message(self, node_id: str, message_type: str, content: str) -> None:
        """Add a message to the message log.
        
        Args:
            node_id: The node ID associated with the message
            message_type: The type of message
            content: The message content
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages (node_id, message_type, content, recorded_at) VALUES (?, ?, ?, ?)",
                (node_id, message_type, content, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    
    def messages(self) -> List[Dict[str, Any]]:
        """Get all messages from the store.
        
        Returns:
            List of message dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT message_id, node_id, message_type, content, recorded_at FROM messages ORDER BY recorded_at"
                )
                messages = []
                for row in cursor.fetchall():
                    messages.append({
                        "message_id": row[0],
                        "node_id": row[1],
                        "role": row[2],
                        "content": row[3],
                        "recorded_at": row[4]
                    })
                return messages
        except Exception:
            return []
    
    def add_fact(self, fact: Fact) -> None:
        """Add a fact to the facts table.
        
        Args:
            fact: The fact to add
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO facts (fact_id, text, source, confidence, recorded_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fact.fact_id,
                    fact.text,
                    fact.source,
                    fact.confidence,
                    fact.recorded_at,
                    _json_dumps(fact.metadata),
                )
            )
            conn.commit()

    def facts(self) -> List[Fact]:
        """Load the durable facts recorded for the current runtime store."""
        if not self.db_path.exists():
            return []

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT fact_id, text, source, confidence, recorded_at, metadata
                FROM facts
                ORDER BY recorded_at, fact_id
                """
            ).fetchall()

        return [
            Fact(
                fact_id=row[0],
                text=row[1],
                source=row[2],
                confidence=float(row[3]),
                recorded_at=row[4],
                metadata=json.loads(row[5]) if row[5] else {},
            )
            for row in rows
        ]
    
    def add_artifact(self, artifact: Artifact) -> None:
        """Add an artifact to the artifacts table.
        
        Args:
            artifact: The artifact to add
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts (artifact_id, node_id, artifact_type, path, description, acceptance_status, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.node_id,
                    artifact.artifact_type,
                    artifact.path,
                    artifact.description,
                    artifact.acceptance_status,
                    _json_dumps(artifact.metadata),
                )
            )
            conn.commit()
    
    def artifacts(self) -> List[Dict[str, Any]]:
        """Get all artifacts from the store.
        
        Returns:
            List of artifact dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT artifact_id, node_id, artifact_type, path, description, acceptance_status, metadata FROM artifacts"
                )
                artifacts = []
                for row in cursor.fetchall():
                    artifacts.append({
                        "artifact_id": row[0],
                        "node_id": row[1],
                        "artifact_type": row[2],
                        "path": row[3],
                        "description": row[4],
                        "acceptance_status": row[5],
                        "metadata": json.loads(row[6]) if row[6] else {}
                    })
                return artifacts
        except Exception:
            return []
    
    def add_metric(self, metric: Metric) -> None:
        """Add a metric to the metrics table.
        
        Args:
            metric: The metric to add
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metrics (metric_id, node_id, name, value, unit, recorded_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    metric.metric_id,
                    metric.node_id,
                    metric.name,
                    metric.value,
                    metric.unit,
                    metric.recorded_at,
                    _json_dumps(metric.metadata),
                )
            )
            conn.commit()
    
    def nodes(self) -> List[Dict[str, Any]]:
        """Get all nodes in the task graph.
        
        Returns:
            List of node dictionaries
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM nodes")
            rows = cursor.fetchall()
            
            nodes = []
            for row in rows:
                nodes.append({
                    "node_id": row[0],
                    "goal": row[2],
                    "role": row[3],
                    "status": row[4],
                    "dependencies": json.loads(row[5]),
                    "constraints": json.loads(row[6]),
                    "acceptance_criteria": json.loads(row[13]),
                })
            
            return nodes
    
    def counts(self) -> Dict[str, int]:
        """Get counts of nodes, edges, facts, messages, artifacts, and metrics in the database.
        
        Returns:
            Dictionary with counts of nodes, edges, facts, messages, artifacts, and metrics
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Count nodes
            cursor.execute("SELECT COUNT(*) FROM nodes")
            node_count = cursor.fetchone()[0]
            
            # Count edges
            cursor.execute("SELECT COUNT(*) FROM edges")
            edge_count = cursor.fetchone()[0]
            
            # Count facts
            cursor.execute("SELECT COUNT(*) FROM facts")
            fact_count = cursor.fetchone()[0]
            
            # Count messages
            cursor.execute("SELECT COUNT(*) FROM messages")
            message_count = cursor.fetchone()[0]
            
            # Count artifacts
            cursor.execute("SELECT COUNT(*) FROM artifacts")
            artifact_count = cursor.fetchone()[0]
            
            # Count metrics
            cursor.execute("SELECT COUNT(*) FROM metrics")
            metric_count = cursor.fetchone()[0]
            
            # Count resource_decisions (may not exist yet)
            try:
                cursor.execute("SELECT COUNT(*) FROM resource_decisions")
                resource_decisions_count = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                resource_decisions_count = 0
            
            # Count communication edges using the same edge taxonomy as the
            # in-memory task graph. Older graphs used "depends_on"; newer
            # benchmark graphs may use "dependency"/"depends".
            cursor.execute("""
                SELECT COUNT(*) FROM edges
                WHERE edge_type IN (
                    'communication', 'communicates', 'provides',
                    'depends', 'depends_on', 'dependency',
                    'evidence', 'evidence_for',
                    'verification', 'verifies', 'documentation'
                )
            """)
            communication_edges_count = cursor.fetchone()[0]
            
            return {
                "nodes": node_count,
                "edges": edge_count,
                "facts": fact_count,
                "messages": message_count,
                "artifacts": artifact_count,
                "metrics": metric_count,
                "resource_decisions": resource_decisions_count,
                "communication_edges": communication_edges_count
            }
    
    def load_task_graph(self, run_id: str) -> Optional['TaskGraph']:
        """Load a complete task graph from SQLite.
        
        Args:
            run_id: The explicit run ID to load.
            
        Returns:
            TaskGraph instance or None if not found
        """
        from AstralNexus.core.task_graph import TaskGraph
        
        with sqlite3.connect(self.db_path) as conn:
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("run_id must be a non-empty string")
            cursor = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,))
            if not cursor.fetchone():
                return None
            
            # Create task graph
            task_graph = TaskGraph()
            
            # Load all nodes
            cursor = conn.execute("SELECT * FROM nodes WHERE run_id = ?", (run_id,))
            for row in cursor.fetchall():
                node = TaskNode(
                    node_id=row[0],
                    goal=row[2],
                    role=row[3],
                    status=row[4],
                    dependencies=json.loads(row[5]),
                    constraints=json.loads(row[6]),
                    inputs=json.loads(row[7]),
                    outputs=json.loads(row[8]),
                    risk_level=row[9],
                    sensitivity=row[10],
                    resource_tier=row[11],
                    retry_count=row[12],
                    acceptance_criteria=json.loads(row[13]),
                    created_at=row[14],
                    updated_at=row[15],
                    metadata=json.loads(row[16]),
                )
                task_graph.nodes[node.node_id] = node
            
            # Load all edges for this run
            cursor = conn.execute(
                "SELECT * FROM edges WHERE run_id = ? OR run_id IS NULL",
                (run_id,)
            )
            for row in cursor.fetchall():
                edge = TaskEdge(
                    source=row[2],  # row[0]=edge_id, row[1]=run_id, row[2]=source
                    target=row[3],
                    edge_type=row[4],
                    metadata=json.loads(row[5]) if row[5] else {},
                )
                task_graph.edges.append(edge)
            
            # Load all facts
            cursor = conn.execute("SELECT * FROM facts")
            for row in cursor.fetchall():
                fact = Fact(
                    fact_id=row[0],
                    text=row[1],
                    source=row[2],
                    confidence=row[3],
                    recorded_at=row[4],
                    metadata=json.loads(row[5]) if row[5] else {},
                )
                task_graph.facts[fact.fact_id] = fact
            
            # Load all artifacts
            cursor = conn.execute("SELECT * FROM artifacts")
            for row in cursor.fetchall():
                artifact = Artifact(
                    artifact_id=row[0],
                    node_id=row[1],
                    artifact_type=row[2],
                    path=row[3],
                    description=row[4],
                    acceptance_status=row[5],
                    metadata=json.loads(row[6]) if row[6] else {},
                )
                task_graph.artifacts[artifact.artifact_id] = artifact
            
            return task_graph
    
    def save_task_graph(self, run_id: str, task_graph: 'TaskGraph') -> None:
        """Save one run's task graph using an explicit identity."""
        from AstralNexus.core.task_graph import TaskGraph
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(task_graph, TaskGraph):
            raise TypeError(f"task_graph must be TaskGraph, got {type(task_graph).__name__}")
        # Ensure the run record exists in the runs table
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,))
            if not cursor.fetchone():
                # Extract goal from first node if available
                goal = ""
                if task_graph.nodes:
                    first_node = next(iter(task_graph.nodes.values()))
                    goal = getattr(first_node, 'goal', '')
                conn.execute(
                    "INSERT INTO runs (run_id, goal, started_at, status) VALUES (?, ?, ?, ?)",
                    (run_id, goal, datetime.now(timezone.utc).isoformat(), "running")
                )
        
        # Save all nodes with run_id
        for node_id, node in task_graph.nodes.items():
            self.add_node({
                "node_id": node.node_id,
                "goal": node.goal,
                "role": node.role,
                "status": node.status,
                "dependencies": node.dependencies,
                "constraints": node.constraints,
                "inputs": node.inputs,
                "outputs": node.outputs,
                "risk_level": node.risk_level,
                "sensitivity": node.sensitivity,
                "resource_tier": node.resource_tier,
                "retry_count": node.retry_count,
                "acceptance_criteria": node.acceptance_criteria,
                "created_at": node.created_at,
                "updated_at": node.updated_at,
                "metadata": node.metadata,
            }, run_id=run_id)
        
        # Save all edges with run_id
        for edge in task_graph.edges:
            self.add_edge(edge.source, edge.target, edge.edge_type, edge.metadata, run_id=run_id)
        
        # Save all facts
        for fact in task_graph.facts.values():
            self.add_fact(fact)
        
        # Save all artifacts
        for artifact in task_graph.artifacts.values():
            self.add_artifact(artifact)
    
    def fault_events(self) -> List[Dict[str, Any]]:
        """Get all fault events recorded in the store.
        
        Returns:
            List of fault event dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT event_id, fault_id, fault_type, target_node, payload, timestamp "
                    "FROM fault_events ORDER BY timestamp"
                )
                events = []
                for row in cursor.fetchall():
                    events.append({
                        "event_id": row[0],
                        "fault_id": row[1],
                        "fault_type": row[2],
                        "target_node": row[3],
                        "payload": json.loads(row[4]) if row[4] else {},
                        "timestamp": row[5]
                    })
                return events
        except Exception:
            return []
    
    def add_fault_event(self, fault_id: str, fault_type: str, target_node: str, payload: Dict[str, Any]) -> None:
        """Add a fault event to the store.
        
        Args:
            fault_id: The fault identifier
            fault_type: The type of fault
            target_node: The target node ID
            payload: Additional fault data
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO fault_events (fault_id, fault_type, target_node, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                (fault_id, fault_type, target_node, _json_dumps(payload), datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    
    def recovery_plans(self) -> List[Dict[str, Any]]:
        """Get all recovery plans recorded in the store.
        
        Returns:
            List of recovery plan dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT plan_id, fault_id, strategy, status, recovery_nodes, actions, timestamp "
                    "FROM recovery_plans ORDER BY timestamp"
                )
                plans = []
                for row in cursor.fetchall():
                    plans.append({
                        "plan_id": row[0],
                        "fault_id": row[1],
                        "strategy": row[2],
                        "status": row[3],
                        "recovery_nodes": json.loads(row[4]) if row[4] else [],
                        "actions": json.loads(row[5]) if row[5] else [],
                        "timestamp": row[6]
                    })
                return plans
        except Exception:
            return []
    
    def add_recovery_plan(self, fault_id: str, strategy: str, status: str, recovery_nodes: List[str], actions: List[Dict[str, Any]]) -> None:
        """Add a recovery plan to the store.
        
        Args:
            fault_id: The fault identifier
            strategy: The recovery strategy
            status: The plan status
            recovery_nodes: List of nodes to recover
            actions: List of recovery actions
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO recovery_plans (fault_id, strategy, status, recovery_nodes, actions, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (fault_id, strategy, status, _json_dumps(recovery_nodes), _json_dumps(actions), datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    
    def recovery_evaluations(self) -> List[Dict[str, Any]]:
        """Get all recovery evaluations recorded in the store.
        
        Returns:
            List of recovery evaluation dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT eval_id, fault_id, final_status, recovery_rate, average_recovery_steps, extra_tokens, timestamp "
                    "FROM recovery_evaluations ORDER BY timestamp"
                )
                evals = []
                for row in cursor.fetchall():
                    evals.append({
                        "eval_id": row[0],
                        "fault_id": row[1],
                        "final_status": row[2],
                        "recovery_rate": row[3],
                        "average_recovery_steps": row[4],
                        "extra_tokens": row[5],
                        "timestamp": row[6]
                    })
                return evals
        except Exception:
            return []
    
    def add_recovery_evaluation(self, fault_id: str, final_status: str, recovery_rate: float, average_recovery_steps: int, extra_tokens: int) -> None:
        """Add a recovery evaluation to the store.
        
        Args:
            fault_id: The fault identifier
            final_status: The final recovery status
            recovery_rate: The recovery rate
            average_recovery_steps: Average steps per recovery
            extra_tokens: Extra tokens used
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO recovery_evaluations (fault_id, final_status, recovery_rate, average_recovery_steps, extra_tokens, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (fault_id, final_status, recovery_rate, average_recovery_steps, extra_tokens, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    
    def communication_edges(self) -> List[Dict[str, Any]]:
        """Get all communication edges from the store.
        
        Returns:
            List of communication edge dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT source, target, edge_type FROM edges
                    WHERE edge_type IN (
                        'communication', 'communicates', 'provides',
                        'depends', 'depends_on', 'dependency',
                        'evidence', 'evidence_for',
                        'verification', 'verifies', 'documentation'
                    )
                    """
                )
                edges = []
                for row in cursor.fetchall():
                    edges.append({
                        "source": row[0],
                        "target": row[1],
                        "edge_type": row[2]
                    })
                return edges
        except Exception:
            return []
    
    def topology_metrics(self) -> Dict[str, Any]:
        """Get topology metrics from the store.
        
        Returns:
            Dictionary with topology metrics
        """
        if not self.db_path.exists():
            return {}
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Count nodes
                node_cursor = conn.execute("SELECT COUNT(*) FROM nodes")
                node_count = node_cursor.fetchone()[0]
                
                # Count edges
                edge_cursor = conn.execute("SELECT COUNT(*) FROM edges")
                edge_count = edge_cursor.fetchone()[0]
                
                # Calculate sparsity (1 - edge_density)
                max_edges = node_count * (node_count - 1) if node_count > 1 else 0
                sparsity = 1 - (edge_count / max_edges) if max_edges > 0 else 1.0
                
                return {
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "sparsity": sparsity
                }
        except Exception:
            return {}
    
    def record_resource_decision(
        self,
        node_id: str,
        role: str,
        resource_tier: str,
        decision_time: float,
        reason: str,
        fallback_from: Optional[str] = None,
        fallback_reason: Optional[str] = None
    ) -> None:
        """Record a resource scheduling decision for a node.
        
        Args:
            node_id: The node ID for which the decision was made
            role: The role assigned to the node
            resource_tier: The resource tier selected (local, edge, cloud)
            decision_time: Time taken to make the decision in seconds
            reason: Reason for the resource selection
            fallback_from: Optional resource tier that was unavailable
            fallback_reason: Optional reason why fallback occurred
        """
        with sqlite3.connect(self.db_path) as conn:
            # Ensure resource_decisions table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS resource_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    resource_tier TEXT NOT NULL,
                    decision_time REAL NOT NULL,
                    reason TEXT NOT NULL,
                    fallback_from TEXT,
                    fallback_reason TEXT,
                    recorded_at TIMESTAMP NOT NULL
                )
            """)
            
            # Insert the decision record
            conn.execute("""
                INSERT INTO resource_decisions 
                (node_id, role, resource_tier, decision_time, reason, fallback_from, fallback_reason, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node_id,
                role,
                resource_tier,
                decision_time,
                reason,
                fallback_from,
                fallback_reason,
                datetime.now(timezone.utc).isoformat()
            ))
            
            conn.commit()
    
    def resource_decisions(self) -> List[Dict[str, Any]]:
        """Query all resource decisions.
        
        Returns:
            List of resource decision dictionaries
        """
        with sqlite3.connect(self.db_path) as conn:
            # Ensure table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS resource_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    resource_tier TEXT NOT NULL,
                    decision_time REAL NOT NULL,
                    reason TEXT NOT NULL,
                    fallback_from TEXT,
                    fallback_reason TEXT,
                    recorded_at TIMESTAMP NOT NULL
                )
            """)
            
            cursor = conn.execute("""
                SELECT node_id, role, resource_tier, decision_time, reason, 
                       fallback_from, fallback_reason, recorded_at
                FROM resource_decisions
                ORDER BY recorded_at DESC
            """)
            
            return [
                {
                    "node_id": row[0],
                    "role": row[1],
                    "resource_tier": row[2],
                    "decision_time": row[3],
                    "reason": row[4],
                    "fallback_from": row[5],
                    "fallback_reason": row[6],
                    "recorded_at": row[7]
                }
                for row in cursor.fetchall()
            ]
    
    def add_constraint_version(self, constraint_data: Dict[str, Any]) -> None:
        """Add or update a constraint version record.
        
        Args:
            constraint_data: Dictionary containing constraint version data
        """
        with sqlite3.connect(self.db_path) as conn:
            # Ensure constraint_versions table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS constraint_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    constraint_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT,
                    affected_nodes TEXT,
                    supersedes TEXT,
                    version INTEGER NOT NULL,
                    recorded_at TIMESTAMP NOT NULL
                )
            """)
            
            # Insert the constraint version
            conn.execute("""
                INSERT INTO constraint_versions 
                (constraint_id, text, source, affected_nodes, supersedes, version, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                constraint_data["constraint_id"],
                constraint_data["text"],
                constraint_data.get("source", ""),
                _json_dumps(constraint_data.get("affected_nodes", [])),
                constraint_data.get("supersedes"),
                constraint_data["version"],
                datetime.now(timezone.utc).isoformat()
            ))
            
            conn.commit()
    
    def constraint_versions(self) -> List[Dict[str, Any]]:
        """Get all constraint versions from the store.
        
        Returns:
            List of constraint version dictionaries
        """
        with sqlite3.connect(self.db_path) as conn:
            # Ensure table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS constraint_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    constraint_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT,
                    affected_nodes TEXT,
                    supersedes TEXT,
                    version INTEGER NOT NULL,
                    recorded_at TIMESTAMP NOT NULL
                )
            """)
            
            cursor = conn.execute("""
                SELECT constraint_id, text, source, affected_nodes, supersedes, version, recorded_at
                FROM constraint_versions
                ORDER BY version ASC
            """)
            
            versions = []
            for row in cursor.fetchall():
                versions.append({
                    "constraint_id": row[0],
                    "text": row[1],
                    "source": row[2],
                    "affected_nodes": json.loads(row[3]) if row[3] else [],
                    "supersedes": row[4],
                    "version": row[5],
                    "recorded_at": row[6]
                })
            
            return versions
