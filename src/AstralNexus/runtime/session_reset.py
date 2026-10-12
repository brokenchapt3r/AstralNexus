"""Conversation reset and archive cleanup.

Implements /reset semantics: refuse implicit deletion of persistent runs,
provide structured session reset with context engine notification,
 conversation archive retention policies, and recoverable new session creation.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4


@dataclass
class ResetRecord:
    reset_id: str
    session_id: str
    timestamp: str
    action: str  # context_reset_only | full_reset | archive_cleanup
    preserved_runs: List[str] = field(default_factory=list)
    cleaned_archives: List[str] = field(default_factory=list)
    context_engine_notified: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reset_id": self.reset_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "preserved_runs": self.preserved_runs,
            "cleaned_archives": self.cleaned_archives,
            "context_engine_notified": self.context_engine_notified,
            "metadata": self.metadata,
        }


@dataclass
class ConversationArchivePolicy:
    """Retention policy for conversation archives, not runtime state."""
    max_age_hours: float = 168  # 7 days
    max_count: int = 50
    preserve_failed: bool = True
    preserve_tagged: bool = True

    def should_retain(self, archive: Dict[str, Any]) -> bool:
        if self.preserve_tagged and archive.get("tagged"):
            return True
        if self.preserve_failed and archive.get("status") == "failed":
            return True
        created = archive.get("created_at", "")
        if created:
            try:
                age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds() / 3600
                if age_hours < self.max_age_hours:
                    return True
            except (ValueError, TypeError):
                pass
        return False


class SessionResetManager:
    """Manages session reset with safety guards and checkpoint cleanup."""

    def __init__(
        self,
        *,
        home_dir: Optional[Path] = None,
        context_engine_callback: Optional[Callable[[str], bool]] = None,
        archive_policy: Optional[ConversationArchivePolicy] = None,
    ):
        self._lock = RLock()
        self.home_dir = home_dir or Path.home() / ".astralnexus"
        self.context_engine_callback = context_engine_callback
        self.policy = archive_policy or ConversationArchivePolicy()
        self._reset_history: List[ResetRecord] = []

    def reset_session(
        self,
        session_id: str,
        *,
        action: str = "context_reset_only",
        preserve_runs: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResetRecord:
        """Reset a session with explicit safety guards.

        Args:
            session_id: The session to reset.
            action: One of 'context_reset_only', 'full_reset', 'archive_cleanup'.
            preserve_runs: If True, refuse to delete persistent run artifacts.
            metadata: Additional context for the reset.

        Returns:
            ResetRecord with details of what was done.
        """
        with self._lock:
            record = ResetRecord(
                reset_id=f"reset-{uuid4().hex[:12]}",
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                action=action,
                metadata=dict(metadata or {}),
            )

            # 1. Refuse implicit deletion of persistent runs
            if preserve_runs:
                runs_dir = self.home_dir / "runs"
                if runs_dir.exists():
                    record.preserved_runs = [
                        p.name for p in runs_dir.iterdir() if p.is_dir()
                    ]

            # 2. Conversation archive cleanup based on policy
            if action in ("full_reset", "archive_cleanup"):
                record.cleaned_archives = self._cleanup_archives(session_id)

            # 3. Notify context engine
            if self.context_engine_callback:
                try:
                    record.context_engine_notified = self.context_engine_callback(session_id)
                except Exception:
                    record.context_engine_notified = False

            # 4. Write reset record
            reset_dir = self.home_dir / "resets"
            reset_dir.mkdir(parents=True, exist_ok=True)
            reset_path = reset_dir / f"{record.reset_id}.json"
            reset_path.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            self._reset_history.append(record)
            return record

    def _cleanup_archives(self, session_id: str) -> List[str]:
        """Clean expired conversation archives per policy."""
        cleaned: list[str] = []
        archive_dir = self.home_dir / "conversation_archives" / session_id
        if not archive_dir.exists():
            return cleaned

        archives = []
        for archive_file in sorted(archive_dir.glob("*.json")):
            try:
                archive_data = json.loads(archive_file.read_text(encoding="utf-8"))
                archives.append((archive_file, archive_data))
            except (json.JSONDecodeError, OSError):
                continue

        # Sort by age, keep newest up to max_count
        retained = [archive for _, archive in archives if self.policy.should_retain(archive)]
        if len(retained) > self.policy.max_count:
            retained = retained[-self.policy.max_count:]

        retained_paths = set()
        for archive_file, archive_data in archives:
            if self.policy.should_retain(archive_data):
                retained_paths.add(archive_file)

        for archive_file, _ in archives:
            if archive_file not in retained_paths:
                try:
                    archive_file.unlink()
                    cleaned.append(archive_file.name)
                except OSError:
                    pass

        return cleaned

    def create_recoverable_session(
        self,
        *,
        owner: str,
        capabilities: Optional[set] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new session with a recoverable conversation archive."""
        session_id = f"session-{uuid4().hex[:16]}"
        session_dir = self.home_dir / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        session_record = {
            "session_id": session_id,
            "owner": owner,
            "capabilities": sorted(capabilities or []),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "metadata": dict(metadata or {}),
            "archive_path": str(session_dir / "session_archive.json"),
        }

        (session_dir / "session.json").write_text(
            json.dumps(session_record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        initial_archive = {
            "session_id": session_id,
            "archive_id": f"archive-{uuid4().hex[:8]}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "tagged": False,
            "context_messages": [],
        }
        (session_dir / "session_archive.json").write_text(
            json.dumps(initial_archive, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return session_record

    def get_reset_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._reset_history[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            actions: dict[str, int] = {}
            for r in self._reset_history:
                actions[r.action] = actions.get(r.action, 0) + 1
            return {
                "total_resets": len(self._reset_history),
                "by_action": actions,
                "context_engine_success_rate": (
                    sum(1 for r in self._reset_history if r.context_engine_notified)
                    / max(len(self._reset_history), 1)
                ),
            }

