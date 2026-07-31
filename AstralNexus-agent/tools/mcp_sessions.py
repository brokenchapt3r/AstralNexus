"""Stateful MCP session boundary for AstralNexus tools.

This module owns session identity, authorization and lifecycle.  It is
transport-neutral: an MCP client/adapter can attach a real transport later
without leaking session state into the task graph or worker objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any, Dict, Optional
from uuid import uuid4


@dataclass
class MCPSession:
    session_id: str
    server_name: str
    owner: str
    capabilities: set[str] = field(default_factory=set)
    state: str = "open"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_used_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.last_used_at = datetime.now(timezone.utc).isoformat()

    def authorize(self, capability: str) -> bool:
        self.touch()
        return self.state == "open" and capability in self.capabilities


class MCPSessionManager:
    def __init__(self, storage_path: Optional[str | Path] = None) -> None:
        self._lock = RLock()
        self._sessions: Dict[str, MCPSession] = {}
        self.storage_path = Path(storage_path) if storage_path else None
        if self.storage_path:
            self._load()

    def _load(self) -> None:
        """Load durable session metadata from the shared run directory."""
        assert self.storage_path is not None
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"failed to load MCP session store: {exc}") from exc
        if not isinstance(raw, list):
            raise RuntimeError("MCP session store must contain a JSON list")
        for item in raw:
            if not isinstance(item, dict):
                continue
            session_id = item.get("session_id")
            if not session_id:
                continue
            self._sessions[session_id] = MCPSession(
                session_id=str(session_id),
                server_name=str(item.get("server_name", "")),
                owner=str(item.get("owner", "")),
                capabilities=set(item.get("capabilities", [])),
                state=str(item.get("state", "open")),
                metadata=dict(item.get("metadata") or {}),
                created_at=str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
                last_used_at=str(item.get("last_used_at") or datetime.now(timezone.utc).isoformat()),
            )

    def _persist(self) -> None:
        if self.storage_path is None:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot()
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.storage_path.name}.",
            suffix=".tmp",
            dir=str(self.storage_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.storage_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def persist(self, session: MCPSession) -> None:
        """Persist a session after transport activity in a client process."""
        with self._lock:
            if session.session_id not in self._sessions:
                raise KeyError(f"unknown MCP session: {session.session_id}")
            self._persist()

    def open_session(
        self,
        server_name: str,
        *,
        owner: str,
        capabilities: Optional[set[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MCPSession:
        session = MCPSession(
            session_id=f"mcp-{uuid4().hex}",
            server_name=server_name,
            owner=owner,
            capabilities=set(capabilities or set()),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._sessions[session.session_id] = session
            self._persist()
        return session

    def get(self, session_id: str, *, owner: Optional[str] = None) -> MCPSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or (owner is not None and session.owner != owner):
                raise KeyError(f"unknown MCP session: {session_id}")
            session.touch()
            self._persist()
            return session

    def close(self, session_id: str, *, owner: Optional[str] = None) -> None:
        session = self.get(session_id, owner=owner)
        with self._lock:
            session.state = "closed"
            session.touch()
            self._persist()

    def snapshot(self) -> list[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "session_id": session.session_id,
                    "server_name": session.server_name,
                    "owner": session.owner,
                    "capabilities": sorted(session.capabilities),
                    "state": session.state,
                    "metadata": dict(session.metadata),
                }
                for session in self._sessions.values()
            ]

