from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from functools import wraps
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

from AstralNexus.core.task_graph import Artifact, Fact, Metric, TaskEdge, TaskNode, TaskGraph
from AstralNexus.core.state_machine import normalize_status


def graph_snapshot_locked(function):
    @wraps(function)
    def wrapped(self, run_id, task_graph, **kwargs):
        if not isinstance(task_graph, TaskGraph):
            return function(self, run_id, task_graph, **kwargs)
        with task_graph._mutation_lock:
            return function(self, run_id, task_graph, **kwargs)
    return wrapped


def _json_default(value: Any) -> Any:
    """Convert runtime objects stored in graph metadata to JSON values."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
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
    
    def __init__(self, db_path: Path, *, read_only: bool = False):
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
    
    def _connect(self, *, isolation_level: Optional[Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"]] = "DEFERRED") -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1,
                                   isolation_level=isolation_level)
        else:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=isolation_level)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=" + ("1000" if self.read_only else "30000"))
        return conn

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
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
                    owned_paths TEXT DEFAULT '[]',
                    input_artifacts TEXT DEFAULT '[]',
                    output_artifacts TEXT DEFAULT '[]',
                    handoff_contract TEXT DEFAULT '{}',
                    expected_outputs TEXT DEFAULT '[]',
                    FOREIGN KEY (run_id) REFERENCES runs (run_id)
                )
            """)

            # Existing deployments predate explicit Worker ownership and
            # artifact handoff contracts. Additive migration preserves old
            # runs and keeps resume equivalent to uninterrupted execution.
            node_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
            }
            for column, declaration in {
                "owned_paths": "TEXT DEFAULT '[]'",
                "input_artifacts": "TEXT DEFAULT '[]'",
                "output_artifacts": "TEXT DEFAULT '[]'",
                "handoff_contract": "TEXT DEFAULT '{}'",
                "expected_outputs": "TEXT DEFAULT '[]'",
            }.items():
                if column not in node_columns:
                    conn.execute(f"ALTER TABLE nodes ADD COLUMN {column} {declaration}")
                    # Only newly introduced columns inherit legacy metadata.
                    # Existing []/{} are explicit canonical values, including
                    # deliberate clears; never resurrect them on reopen.
                    for ident, raw_metadata in conn.execute('SELECT rowid,metadata FROM nodes').fetchall():
                        metadata = json.loads(raw_metadata or '{}')
                        if not isinstance(metadata, dict): raise ValueError('legacy node metadata must be an object')
                        if column in metadata:
                            expected_type = dict if column == 'handoff_contract' else list
                            if not isinstance(metadata[column], expected_type): raise ValueError('invalid legacy node ' + column)
                            conn.execute(f'UPDATE nodes SET {column}=? WHERE rowid=?', (json.dumps(metadata[column]), ident))
            
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
                    lifecycle_class TEXT DEFAULT 'durable',
                    FOREIGN KEY (node_id) REFERENCES nodes (node_id)
                )
            """)
            artifact_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if "lifecycle_class" not in artifact_columns:
                conn.execute(
                    "ALTER TABLE artifacts ADD COLUMN lifecycle_class "
                    "TEXT DEFAULT 'durable'"
                )
            
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
            conn.execute("CREATE TABLE IF NOT EXISTS graph_commit_fence (run_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, commit_seq INTEGER NOT NULL, graph_revision INTEGER NOT NULL, committed_at TEXT NOT NULL, cursor_json TEXT)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_commit_fence)")}
            if "cursor_json" not in columns:
                conn.execute("ALTER TABLE graph_commit_fence ADD COLUMN cursor_json TEXT")
            from .scoped_schema import migrate_scoped_schema
            migrate_scoped_schema(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS message_history (message_id INTEGER PRIMARY KEY, node_id TEXT, message_type TEXT, content TEXT, recorded_at TEXT, run_id TEXT NOT NULL)")
            conn.execute("CREATE VIEW IF NOT EXISTS all_messages AS SELECT * FROM messages UNION ALL SELECT * FROM message_history")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_history_run_time ON message_history(run_id,node_id,recorded_at)")
            conn.commit()
    
    def create_run(self, run_id: str, goal: str, constraints: List[str]) -> None:
        """Create a new run record.
        
        Args:
            run_id: The run identifier
            goal: The goal of the run
            constraints: List of constraints
        """
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, goal, started_at, status, constraints) VALUES (?, ?, ?, ?, ?)",
                (run_id, goal, datetime.now(timezone.utc).isoformat(), "running", _json_dumps(constraints))
            )
            conn.commit()

    def list_runs(self) -> List[Dict[str, Any]]:
        """Return persisted runs in newest-first order."""
        with closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT run_id, goal, started_at, ended_at, status, constraints "
                "FROM runs WHERE run_id <> '' ORDER BY started_at DESC, run_id DESC"
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
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = ? "
                "WHERE run_id = ?",
                (status, ended_at, run_id),
            )
            conn.commit()
    
    def _scope(self, conn: sqlite3.Connection, run_id: Optional[str],
               node_id: Optional[str] = None) -> str:
        if run_id is not None:
            return run_id
        if node_id:
            rows = conn.execute("SELECT DISTINCT run_id FROM nodes WHERE node_id=?", (node_id,)).fetchall()
            if len(rows) == 1:
                return str(rows[0][0])
            if len(rows) > 1:
                raise ValueError("ambiguous node identity; specify run_id")
        rows = conn.execute("SELECT run_id FROM runs WHERE run_id <> ''").fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous run identity; specify run_id")
        return str(rows[0][0]) if rows else ""

    def _select(self, conn: sqlite3.Connection, run_id: Optional[str], sql: str,
                params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        import re
        scope = self._scope(conn, run_id)
        match = re.search(r"FROM\s+(\w+)", sql, re.I)
        if match is None:
            raise ValueError("scoped read needs a table")
        tail = sql[match.end():]
        if re.match(r"\s*WHERE\b", tail, re.I):
            tail = re.sub(r"^\s*WHERE\b", " WHERE run_id=? AND", tail, count=1, flags=re.I)
        else:
            tail = " WHERE run_id=? " + tail
        return conn.execute(sql[:match.end()] + tail, (scope, *params))

    def _insert_audit(self, conn: sqlite3.Connection, sql: str,
                      params: tuple[Any, ...], run_id: Optional[str],
                      node_id: Optional[str] = None) -> sqlite3.Cursor:
        import re
        scope = self._scope(conn, run_id, node_id)
        conn.execute("INSERT OR IGNORE INTO runs(run_id,status) VALUES(?, 'running')", (scope,))
        # Append-only records retain their run even after a node leaves the graph.
        match = re.search(r"INSERT INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)", sql, re.I)
        if match is None:
            raise ValueError("invalid audit insert")
        if match[1] == "messages" and not conn.execute(
                "SELECT 1 FROM nodes WHERE run_id=? AND node_id=?", (scope, node_id)).fetchone():
            raise sqlite3.IntegrityError("message references a nonexistent node")
        scoped_sql = (sql[:match.start()] + f"INSERT INTO {match[1]}({match[2]},run_id) VALUES({match[3]},?)" + sql[match.end():])
        return conn.execute(scoped_sql, (*params, scope))

    def _upsert(self, table: str, record: Dict[str, Any], keys: List[str],
                run_id: Optional[str], node_id: Optional[str] = None,
                conn: Optional[sqlite3.Connection] = None) -> None:
        if conn is None:
            with closing(self._connect()) as own, own:
                self._upsert(table, record, keys, run_id, node_id, own)
            return
        record = dict(record)
        record["run_id"] = self._scope(conn, run_id, node_id)
        columns = list(record)
        updates = [column for column in columns if column not in keys]
        sql = (f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
               f"ON CONFLICT({','.join(keys)}) DO UPDATE SET "
               + ','.join(f"{column}=excluded.{column}" for column in updates)
               + " WHERE " + ' OR '.join(f"{table}.{column} IS NOT excluded.{column}" for column in updates))
        conn.execute(sql, tuple(record.values()))

    def add_node(self, node_data: Dict[str, Any], run_id: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> None:
        allowed = set(TaskNode.__dataclass_fields__) | {'run_id', 'acceptance'}
        runtime_only = {'_ignored_legacy_contracts', '_legacy_contract_defaults'}
        unknown = set(node_data) - allowed - runtime_only
        if unknown: raise ValueError('unknown persistent node fields: ' + ', '.join(sorted(map(str, unknown))))
        record = {key: value for key, value in node_data.items() if key in allowed}
        record["acceptance"] = record.pop("acceptance_criteria", [])
        record["status"] = normalize_status(record.get("status", "admitted"))
        defaults: Dict[str, Any] = {"dependencies": [], "constraints": [], "inputs": [], "outputs": [],
                                    "metadata": {}, "owned_paths": [], "input_artifacts": [],
                                    "output_artifacts": [], "handoff_contract": {}, "expected_outputs": [], "acceptance": []}
        for field, default in defaults.items():
            record[field] = _json_dumps(record.get(field, default))
        record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        record.setdefault("updated_at", record["created_at"])
        if conn is None:
            with closing(self._connect()) as own, own:
                scope = self._scope(own, run_id)
                own.execute("INSERT OR IGNORE INTO runs(run_id,status) VALUES(?, 'running')", (scope,))
                self._upsert("nodes", record, ["run_id", "node_id"], scope, conn=own)
        else:
            self._upsert("nodes", record, ["run_id", "node_id"], run_id, conn=conn)

    def get_node(self, node_id: str, run_id: Optional[str] = None) -> Optional[TaskNode]:
        """Get a node by its ID.
        
        Args:
            node_id: The ID of the node to retrieve
            
        Returns:
            The TaskNode if found, None otherwise
        """
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("SELECT * FROM nodes WHERE run_id=? AND node_id=?", (self._scope(conn, run_id, node_id), node_id))
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
                    owned_paths=json.loads(row[17] or "[]"),
                    input_artifacts=json.loads(row[18] or "[]"),
                    output_artifacts=json.loads(row[19] or "[]"),
                    handoff_contract=json.loads(row[20] or "{}"),
                    expected_outputs=json.loads(row[21] or "[]"),
                )
        
        return None
    
    def add_edge(self, source: str, target: str, edge_type: str, metadata: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> None:
        """Add an edge between two nodes.
        
        Args:
            source: The source node ID
            target: The target node ID
            edge_type: The type of relationship
            metadata: Additional metadata for the edge
            run_id: Optional run ID for tracking which run created this edge
            conn: Round 9 — caller-owned connection (see ``add_node``).
        """
        self._upsert("edges", {"source": source, "target": target, "edge_type": edge_type,
                               "metadata": _json_dumps(metadata or {})},
                     ["run_id", "source", "target", "edge_type"], run_id, source, conn)

    def add_message(self, node_id: str, message_type: str, content: str, run_id: Optional[str] = None) -> None:
        """Add a message to the message log.
        
        Args:
            node_id: The node ID associated with the message
            message_type: The type of message
            content: The message content
        """
        with closing(self._connect()) as conn, conn:
            self._insert_audit(conn, 
                "INSERT INTO messages (node_id, message_type, content, recorded_at) VALUES (?, ?, ?, ?)",
                (node_id, message_type, content, datetime.now(timezone.utc).isoformat())
            , run_id, node_id)
            conn.commit()
    
    def messages(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all messages from the store.
        
        Returns:
            List of message dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with closing(self._connect()) as conn, conn:
                cursor = self._select(conn, run_id, 
                    "SELECT message_id, node_id, message_type, content, recorded_at FROM all_messages ORDER BY recorded_at"
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
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return []
            raise
    
    def add_fact(self, fact: Fact, conn: Optional[sqlite3.Connection] = None,
                     run_id: Optional[str] = None) -> None:
        record = asdict(fact)
        record["metadata"] = _json_dumps(record.get("metadata", {}))
        self._upsert("facts", record, ["run_id", "fact_id"], run_id,
                     record.get("node_id"), conn)

    def facts(self, run_id: Optional[str] = None) -> List[Fact]:
        """Load the durable facts recorded for the current runtime store."""
        if not self.db_path.exists():
            return []

        with closing(self._connect()) as conn, conn:
            rows = self._select(conn, run_id, 
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
    
    def add_artifact(self, artifact: Artifact, conn: Optional[sqlite3.Connection] = None,
                     run_id: Optional[str] = None) -> None:
        record = asdict(artifact)
        record["metadata"] = _json_dumps(record.get("metadata", {}))
        self._upsert("artifacts", record, ["run_id", "artifact_id"], run_id,
                     record.get("node_id"), conn)

    def artifacts(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all artifacts from the store.
        
        Returns:
            List of artifact dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with closing(self._connect()) as conn, conn:
                cursor = self._select(conn, run_id, 
                    "SELECT artifact_id, node_id, artifact_type, path, description, acceptance_status, metadata, lifecycle_class FROM artifacts"
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
                        "metadata": json.loads(row[6]) if row[6] else {},
                        "lifecycle_class": row[7] or "durable",
                    })
                return artifacts
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return []
            raise
    
    def add_metric(self, metric: Metric, conn: Optional[sqlite3.Connection] = None,
                     run_id: Optional[str] = None) -> None:
        record = asdict(metric)
        record["metadata"] = _json_dumps(record.get("metadata", {}))
        self._upsert("metrics", record, ["run_id", "metric_id"], run_id,
                     record.get("node_id"), conn)

    def nodes(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all nodes in the task graph.
        
        Returns:
            List of node dictionaries
        """
        with closing(self._connect()) as conn, conn:
            cursor = self._select(conn, run_id, "SELECT * FROM nodes")
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
    
    def counts(self, run_id: Optional[str] = None) -> Dict[str, int]:
        """Get counts of nodes, edges, facts, messages, artifacts, and metrics in the database.
        
        Returns:
            Dictionary with counts of nodes, edges, facts, messages, artifacts, and metrics
        """
        with closing(self._connect()) as conn, conn:
            cursor = conn.cursor()
            
            # Count nodes
            cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM nodes")
            node_count = cursor.fetchone()[0]
            
            # Count edges
            cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM edges")
            edge_count = cursor.fetchone()[0]
            
            # Count facts
            cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM facts")
            fact_count = cursor.fetchone()[0]
            
            # Count messages
            cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM all_messages")
            message_count = cursor.fetchone()[0]
            
            # Count artifacts
            cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM artifacts")
            artifact_count = cursor.fetchone()[0]
            
            # Count metrics
            cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM metrics")
            metric_count = cursor.fetchone()[0]
            
            # Count resource_decisions (may not exist yet)
            try:
                cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM resource_decisions")
                resource_decisions_count = cursor.fetchone()[0]
            except sqlite3.OperationalError as error:
                if "no such table:" not in str(error):
                    raise
                resource_decisions_count = 0
            
            # Count communication edges using the same edge taxonomy as the
            # in-memory task graph. Older graphs used "depends_on"; newer
            # benchmark graphs may use "dependency"/"depends".
            cursor = self._select(conn, run_id, """
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
        
        with closing(self._connect()) as conn, conn:
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("run_id must be a non-empty string")
            conn.execute("BEGIN")
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
                    owned_paths=json.loads(row[17] or "[]"),
                    input_artifacts=json.loads(row[18] or "[]"),
                    output_artifacts=json.loads(row[19] or "[]"),
                    handoff_contract=json.loads(row[20] or "{}"),
                    expected_outputs=json.loads(row[21] or "[]"),
                )
                task_graph.nodes[node.node_id] = node
            
            # Load all edges for this run
            cursor = conn.execute(
                "SELECT * FROM edges WHERE run_id = ?",
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
            cursor = conn.execute("SELECT * FROM facts WHERE run_id=?", (run_id,))
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
            cursor = conn.execute("SELECT * FROM artifacts WHERE run_id=?", (run_id,))
            for row in cursor.fetchall():
                artifact = Artifact(
                    artifact_id=row[0],
                    node_id=row[1],
                    artifact_type=row[2],
                    path=row[3],
                    description=row[4],
                    acceptance_status=row[5],
                    metadata=json.loads(row[6]) if row[6] else {},
                    lifecycle_class=row[7] or "durable",
                )
                task_graph.artifacts[artifact.artifact_id] = artifact
            
            for row in conn.execute("SELECT * FROM metrics WHERE run_id=?", (run_id,)):
                metric = Metric(metric_id=row[0], node_id=row[1], name=row[2], value=row[3],
                                unit=row[4], recorded_at=row[5], metadata=json.loads(row[6] or "{}"))
                task_graph.metrics[metric.metric_id] = metric
            return task_graph
    
    def _write_graph_rows(
        self, conn: sqlite3.Connection, run_id: str, task_graph: 'TaskGraph',
    ) -> None:
        conn.execute("INSERT OR IGNORE INTO runs(run_id,goal,started_at,status) VALUES(?,?,?, 'running')",
                     (run_id, next(iter(task_graph.nodes.values())).goal if task_graph.nodes else "",
                      datetime.now(timezone.utc).isoformat()))
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS snapshot_ids(id TEXT PRIMARY KEY)")
        for table, key, records in (("artifacts", "artifact_id", task_graph.artifacts),
                                    ("metrics", "metric_id", task_graph.metrics),
                                    ("facts", "fact_id", task_graph.facts),
                                    ("nodes", "node_id", task_graph.nodes)):
            conn.execute("DELETE FROM snapshot_ids")
            conn.executemany("INSERT INTO snapshot_ids(id) VALUES(?)", ((identity,) for identity in records.keys()))
            if table == "nodes":
                conn.execute("INSERT OR IGNORE INTO message_history SELECT * FROM messages WHERE run_id=? AND node_id NOT IN (SELECT id FROM snapshot_ids)", (run_id,))
            conn.execute(f"DELETE FROM {table} WHERE run_id=? AND {key} NOT IN (SELECT id FROM snapshot_ids)", (run_id,))
        conn.execute("DELETE FROM edges WHERE run_id=?", (run_id,))
        # Round 9: ONE transaction for the whole checkpoint (caller-managed).
        # The per-item connect+commit pattern turned a polluted 111k-fact
        # graph into a 28+ minute main-loop stall (live r11 iter-136, 26/26
        # stack samples inside this save chain, 17 in conn.commit()).
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
                "owned_paths": node.owned_paths,
                "input_artifacts": node.input_artifacts,
                "output_artifacts": node.output_artifacts,
                "handoff_contract": node.handoff_contract,
                "expected_outputs": node.expected_outputs,
            }, run_id=run_id, conn=conn)

        for edge in task_graph.edges:
            self.add_edge(edge.source, edge.target, edge.edge_type, edge.metadata, run_id=run_id, conn=conn)

        # Guarded upsert: unchanged rows are not rewritten
        for fact in task_graph.facts.values():
            self.add_fact(fact, conn=conn, run_id=run_id)

        for artifact in task_graph.artifacts.values():
            self.add_artifact(artifact, conn=conn, run_id=run_id)

        for metric in task_graph.metrics.values():
            self.add_metric(metric, conn=conn, run_id=run_id)

    def read_commit_fence(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Last jointly-committed fence identity for one run (doc 17 gap③).

        Includes the authoritative runtime cursor committed inside the same
        fenced transaction (gap⑥): recovery rebuilds from this payload and
        treats runtime_state.json as a rebuildable display cache that a
        late writer may have overwritten.
        """
        if not self.db_path.exists():
            return None
        with closing(self._connect()) as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_commit_fence'"
            ).fetchone()
            if not table:
                return None
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(graph_commit_fence)")
            }
            select_cursor = "cursor_json" if "cursor_json" in columns else "NULL"
            row = conn.execute(
                "SELECT run_id, generation, commit_seq, graph_revision, committed_at,"
                f" {select_cursor}"
                " FROM graph_commit_fence WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        cursor_payload = None
        if row[5] is not None:
            parsed = json.loads(str(row[5]))
            if not isinstance(parsed, dict):
                raise ValueError("authoritative cursor must be a JSON object")
            cursor_payload = parsed
        return {
            "run_id": row[0],
            "generation": int(row[1]),
            "commit_seq": int(row[2]),
            "graph_revision": int(row[3]),
            "committed_at": row[4],
            "cursor": cursor_payload,
        }

    def latest_commit_fence(self, run_id: str = "") -> Optional[Dict[str, Any]]:
        """Fence identity located from the database itself (doc 17 第五轮).

        ``run_id`` empty -> only a single fence identity is acceptable;
        multiple identities require an explicit run_id. Never compares
        sequence numbers belonging to unrelated runs. Never uses JSON for
        identity: the cache is what this record authenticates.
        """
        if not self.db_path.exists():
            return None
        with closing(self._connect()) as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_commit_fence'"
            ).fetchone()
            if not table:
                return None
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(graph_commit_fence)")
            }
            select_cursor = "cursor_json" if "cursor_json" in columns else "NULL"
            if str(run_id or "").strip():
                rows = conn.execute(
                    "SELECT run_id, generation, commit_seq, graph_revision, committed_at,"
                    f" {select_cursor} FROM graph_commit_fence WHERE run_id=?",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, generation, commit_seq, graph_revision, committed_at,"
                    f" {select_cursor} FROM graph_commit_fence"
                    " ORDER BY commit_seq DESC, committed_at DESC"
                ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError("multiple fence identities; specify run_id")
        row = rows[0]
        cursor_payload = None
        if row[5] is not None:
            parsed = json.loads(str(row[5]))
            if not isinstance(parsed, dict):
                raise ValueError("authoritative cursor must be a JSON object")
            cursor_payload = parsed
        return {
            "run_id": row[0],
            "generation": int(row[1]),
            "commit_seq": int(row[2]),
            "graph_revision": int(row[3]),
            "committed_at": row[4],
            "cursor": cursor_payload,
            "fence_identity_count": len(rows),
        }

    @graph_snapshot_locked
    def save_task_graph(
        self,
        run_id: str,
        task_graph: 'TaskGraph',
        *,
        fence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Save one run's task graph using an explicit identity.

        ``fence`` (doc 17 第三批 gap②): dict with runs_db_path / run_id /
        owner_id / generation / now_iso / commit_seq / graph_revision. The
        ownership verification is a conditional UPDATE against the ATTACHed
        run-ownership database executed INSIDE the same transaction as the
        graph writes, so generation verification and the authoritative
        commit share one cross-process commit boundary (SQLite multi-db
        atomic commit over rollback journals). A stale executor that paused
        between probe and write cannot land an overwrite: its token no
        longer matches owner/generation/lease and the whole transaction
        rolls back. Returns False when the fence rejected the commit; True
        (or None-path legacy behavior) otherwise.
        """
        from AstralNexus.core.task_graph import TaskGraph
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(task_graph, TaskGraph):
            raise TypeError(f"task_graph must be TaskGraph, got {type(task_graph).__name__}")
        if fence is None:
            with closing(self._connect()) as conn, conn:
                self._write_graph_rows(conn, run_id, task_graph)
            return True

        conn = self._connect(isolation_level=None)
        attached = False
        try:
            conn.execute("ATTACH DATABASE ? AS runsdb", (str(fence["runs_db_path"]),))
            attached = True
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE runsdb.runs SET updated_at=?"
                " WHERE run_id=? AND owner_id=? AND generation=?"
                " AND status IN ('pending','running')"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (
                    str(fence["now_iso"]), str(fence["run_id"]),
                    str(fence["owner_id"]), int(fence["generation"]),
                    str(fence["now_iso"]),
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            cursor = conn.execute(
                "INSERT INTO graph_commit_fence"
                "(run_id, generation, commit_seq, graph_revision, committed_at, cursor_json)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " generation=excluded.generation, commit_seq=excluded.commit_seq,"
                " graph_revision=excluded.graph_revision, committed_at=excluded.committed_at,"
                " cursor_json=excluded.cursor_json"
                " WHERE excluded.generation > graph_commit_fence.generation"
                " OR (excluded.generation = graph_commit_fence.generation"
                " AND excluded.commit_seq > graph_commit_fence.commit_seq"
                " AND excluded.graph_revision >= graph_commit_fence.graph_revision)",
                (
                    run_id, int(fence["generation"]), int(fence["commit_seq"]),
                    int(fence["graph_revision"]), str(fence["now_iso"]),
                    fence.get("cursor_json"),
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            self._write_graph_rows(conn, run_id, task_graph)
            conn.execute("COMMIT")
            return True
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            if attached:
                try:
                    conn.execute("DETACH DATABASE runsdb")
                except sqlite3.Error:
                    pass
            conn.close()
    
    def fault_events(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all fault events recorded in the store.
        
        Returns:
            List of fault event dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with closing(self._connect()) as conn, conn:
                cursor = self._select(conn, run_id, 
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
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return []
            raise
    
    def add_fault_event(self, fault_id: str, fault_type: str, target_node: str, payload: Dict[str, Any], run_id: Optional[str] = None) -> None:
        """Add a fault event to the store.
        
        Args:
            fault_id: The fault identifier
            fault_type: The type of fault
            target_node: The target node ID
            payload: Additional fault data
        """
        with closing(self._connect()) as conn, conn:
            self._insert_audit(conn, 
                "INSERT INTO fault_events (fault_id, fault_type, target_node, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                (fault_id, fault_type, target_node, _json_dumps(payload), datetime.now(timezone.utc).isoformat())
            , run_id, target_node)
            conn.commit()
    
    def recovery_plans(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all recovery plans recorded in the store.
        
        Returns:
            List of recovery plan dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with closing(self._connect()) as conn, conn:
                cursor = self._select(conn, run_id, 
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
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return []
            raise
    
    def add_recovery_plan(self, fault_id: str, strategy: str, status: str, recovery_nodes: List[str], actions: List[Dict[str, Any]], run_id: Optional[str] = None) -> None:
        """Add a recovery plan to the store.
        
        Args:
            fault_id: The fault identifier
            strategy: The recovery strategy
            status: The plan status
            recovery_nodes: List of nodes to recover
            actions: List of recovery actions
        """
        with closing(self._connect()) as conn, conn:
            self._insert_audit(conn, 
                "INSERT INTO recovery_plans (fault_id, strategy, status, recovery_nodes, actions, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (fault_id, strategy, status, _json_dumps(recovery_nodes), _json_dumps(actions), datetime.now(timezone.utc).isoformat())
            , run_id, None)
            conn.commit()
    
    def recovery_evaluations(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all recovery evaluations recorded in the store.
        
        Returns:
            List of recovery evaluation dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with closing(self._connect()) as conn, conn:
                cursor = self._select(conn, run_id, 
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
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return []
            raise
    
    def add_recovery_evaluation(self, fault_id: str, final_status: str, recovery_rate: float, average_recovery_steps: int, extra_tokens: int, run_id: Optional[str] = None) -> None:
        """Add a recovery evaluation to the store.
        
        Args:
            fault_id: The fault identifier
            final_status: The final recovery status
            recovery_rate: The recovery rate
            average_recovery_steps: Average steps per recovery
            extra_tokens: Extra tokens used
        """
        with closing(self._connect()) as conn, conn:
            self._insert_audit(conn, 
                "INSERT INTO recovery_evaluations (fault_id, final_status, recovery_rate, average_recovery_steps, extra_tokens, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (fault_id, final_status, recovery_rate, average_recovery_steps, extra_tokens, datetime.now(timezone.utc).isoformat())
            , run_id, None)
            conn.commit()
    
    def communication_edges(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all communication edges from the store.
        
        Returns:
            List of communication edge dictionaries
        """
        if not self.db_path.exists():
            return []
        
        try:
            with closing(self._connect()) as conn, conn:
                cursor = self._select(conn, run_id, 
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
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return []
            raise
    
    def topology_metrics(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Get topology metrics from the store.
        
        Returns:
            Dictionary with topology metrics
        """
        if not self.db_path.exists():
            return {}
        
        try:
            with closing(self._connect()) as conn, conn:
                # Count nodes
                node_cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM nodes")
                node_count = node_cursor.fetchone()[0]
                
                # Count edges
                edge_cursor = self._select(conn, run_id, "SELECT COUNT(*) FROM edges")
                edge_count = edge_cursor.fetchone()[0]
                
                # Calculate sparsity (1 - edge_density)
                max_edges = node_count * (node_count - 1) if node_count > 1 else 0
                sparsity = 1 - (edge_count / max_edges) if max_edges > 0 else 1.0
                
                return {
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "sparsity": sparsity
                }
        except sqlite3.OperationalError as error:
            if "no such table:" in str(error):
                return {}
            raise
    
    def record_resource_decision(
        self,
        node_id: str,
        role: str,
        resource_tier: str,
        decision_time: float,
        reason: str,
        fallback_from: Optional[str] = None,
        fallback_reason: Optional[str] = None,
        run_id: Optional[str] = None,
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
        with closing(self._connect()) as conn, conn:

            
            # Insert the decision record
            self._insert_audit(conn, """
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
            ), run_id, node_id)
            
            conn.commit()
    
    def resource_decisions(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query all resource decisions.
        
        Returns:
            List of resource decision dictionaries
        """
        with closing(self._connect()) as conn, conn:

            
            cursor = self._select(conn, run_id, """
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
    
    def add_constraint_version(self, constraint_data: Dict[str, Any], run_id: Optional[str] = None) -> None:
        """Add or update a constraint version record.
        
        Args:
            constraint_data: Dictionary containing constraint version data
        """
        with closing(self._connect()) as conn, conn:

            
            # Insert the constraint version
            self._insert_audit(conn, """
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
            ), run_id, None)
            
            conn.commit()
    
    def constraint_versions(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all constraint versions from the store.
        
        Returns:
            List of constraint version dictionaries
        """
        with closing(self._connect()) as conn, conn:

            
            cursor = self._select(conn, run_id, """
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
