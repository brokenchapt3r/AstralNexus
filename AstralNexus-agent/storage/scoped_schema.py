"""Transactional migration from global graph keys to run-scoped records.

Original rows remain in legacy_v1_* tables; ambiguous legacy provenance is
kept in the unbound scope rather than guessed into a live run.
"""
from __future__ import annotations
import sqlite3
import re

TABLES = ("nodes", "edges", "facts", "artifacts", "metrics", "messages",
          "fault_events", "recovery_plans", "recovery_evaluations",
          "resource_decisions", "constraint_versions")
KEYS = {"nodes": "node_id", "facts": "fact_id", "artifacts": "artifact_id", "metrics": "metric_id"}


def migrate_scoped_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS storage_schema_version (version INTEGER PRIMARY KEY)")
    if conn.execute("SELECT 1 FROM storage_schema_version WHERE version=2").fetchone():
        return
    schemas = {table: conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] for table in TABLES}
    runs = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE run_id <> ''")]
    default = runs[0] if len(runs) == 1 else ""
    owners = {row[0]: row[1] or default for row in conn.execute("SELECT node_id,run_id FROM nodes")}
    conn.execute("INSERT OR IGNORE INTO runs(run_id,status) VALUES('', 'legacy_unbound')")
    for table in TABLES:
        conn.execute(f"ALTER TABLE {table} RENAME TO legacy_v1_{table}")
    for table, sql in schemas.items():
        sql = re.sub(r'CREATE TABLE(?: IF NOT EXISTS)?\s+"?'+table+r'"?', "CREATE TABLE "+table, sql, count=1, flags=re.I)
        sql = re.sub(r"FOREIGN KEY \(node_id\) REFERENCES nodes \(node_id\)",
                     "FOREIGN KEY (run_id,node_id) REFERENCES nodes (run_id,node_id) ON DELETE CASCADE", sql)
        if table in KEYS:
            key = KEYS[table]
            sql = sql.replace(key+" TEXT PRIMARY KEY", key+" TEXT NOT NULL")
            closing = sql.rfind(")")
            sql = sql[:closing]+f", PRIMARY KEY(run_id,{key})"+sql[closing:]
        if "run_id TEXT" in sql:
            sql = sql.replace("run_id TEXT", "run_id TEXT NOT NULL DEFAULT ''")
        else:
            # Insert before table constraints while preserving existing column offsets.
            index = sql.find("FOREIGN KEY")
            if index < 0:
                index = sql.find("PRIMARY KEY(run_id")
            if index < 0:
                index = sql.rfind(")")
                sql = sql[:index]+", run_id TEXT NOT NULL DEFAULT ''"+sql[index:]
            else:
                sql = sql[:index]+"run_id TEXT NOT NULL DEFAULT '', "+sql[index:]
        if table == "edges":
            index = sql.rfind(")")
            sql = sql[:index] + ", FOREIGN KEY(run_id,source) REFERENCES nodes(run_id,node_id) ON DELETE CASCADE, FOREIGN KEY(run_id,target) REFERENCES nodes(run_id,node_id) ON DELETE CASCADE" + sql[index:]
        if not re.search(r"FOREIGN KEY\s*\(run_id\)\s*REFERENCES runs", sql):
            index = sql.rfind(")")
            sql = sql[:index] + ", FOREIGN KEY(run_id) REFERENCES runs(run_id)" + sql[index:]
        conn.execute(sql)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM legacy_v1_{table}").fetchall()
        for row in rows:
            data = dict(row)
            scope = data.get("run_id") or owners.get(data.get("node_id") or data.get("target_node") or data.get("source"), default)
            data["run_id"] = scope
            conn.execute("INSERT OR IGNORE INTO runs(run_id,status) VALUES(?, 'legacy_unbound')", (scope,))
            if table in {"artifacts", "metrics", "messages"} and data.get("node_id"):
                if table == "metrics" and data["node_id"] == "global":
                    data["node_id"] = None  # documented legacy run-level metric
                elif not conn.execute("SELECT 1 FROM nodes WHERE run_id=? AND node_id=?", (scope, data["node_id"])).fetchone():
                    continue  # orphan preserved verbatim in legacy_v1_* archive
            if table == "edges" and any(not conn.execute("SELECT 1 FROM nodes WHERE run_id=? AND node_id=?", (scope, data[key])).fetchone() for key in ("source", "target")):
                continue
            columns = list(data)
            conn.execute(f"INSERT OR IGNORE INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(data.values()))
    conn.row_factory = None
    for table in TABLES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_v2_{table}_run ON {table}(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_v2_messages_node_time ON messages(run_id,node_id,recorded_at)")
    for table in ("artifacts", "metrics", "resource_decisions"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_v2_{table}_node ON {table}(run_id,node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_v2_fault_target ON fault_events(run_id,target_node,timestamp)")
    for table in ("recovery_plans", "recovery_evaluations"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_v2_{table}_fault ON {table}(run_id,fault_id,timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_v2_constraints_version ON constraint_versions(run_id,constraint_id,version)")
    conn.execute("INSERT INTO storage_schema_version(version) VALUES(2)")
