"""Tests for session reset and checkpoint cleanup."""
import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import shutil


class TestResetRecord:
    def test_to_dict(self):
        from AstralNexus.runtime.session_reset import ResetRecord
        r = ResetRecord(
            reset_id="reset-abc",
            session_id="session-123",
            timestamp="2026-07-25T00:00:00Z",
            action="context_reset_only",
            preserved_runs=["run-1", "run-2"],
        )
        d = r.to_dict()
        assert d["reset_id"] == "reset-abc"
        assert d["session_id"] == "session-123"
        assert d["action"] == "context_reset_only"
        assert len(d["preserved_runs"]) == 2


class TestConversationArchivePolicy:
    def test_should_retain_tagged(self):
        from AstralNexus.runtime.session_reset import ConversationArchivePolicy
        p = ConversationArchivePolicy(preserve_tagged=True)
        assert p.should_retain({"tagged": True, "created_at": "2020-01-01T00:00:00Z"})

    def test_should_retain_failed(self):
        from AstralNexus.runtime.session_reset import ConversationArchivePolicy
        p = ConversationArchivePolicy(preserve_failed=True)
        assert p.should_retain({"status": "failed", "created_at": "2020-01-01T00:00:00Z"})

    def test_should_not_retain_expired(self):
        from AstralNexus.runtime.session_reset import ConversationArchivePolicy
        p = ConversationArchivePolicy(max_age_hours=1, preserve_failed=False, preserve_tagged=False)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        assert not p.should_retain({"created_at": old})

    def test_should_retain_recent(self):
        from AstralNexus.runtime.session_reset import ConversationArchivePolicy
        p = ConversationArchivePolicy(max_age_hours=24)
        recent = datetime.now(timezone.utc).isoformat()
        assert p.should_retain({"created_at": recent})


class TestSessionResetManager:
    @pytest.fixture
    def tmp_home(self, tmp_path):
        return tmp_path / "astralnexus_home"

    def test_reset_context_only_preserves_runs(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        runs_dir = tmp_home / "runs"
        runs_dir.mkdir()
        (runs_dir / "run-001").mkdir()
        (runs_dir / "run-002").mkdir()

        mgr = SessionResetManager(home_dir=tmp_home)
        record = mgr.reset_session("session-1", action="context_reset_only")

        assert record.action == "context_reset_only"
        assert "run-001" in record.preserved_runs
        assert "run-002" in record.preserved_runs
        assert (runs_dir / "run-001").exists()
        assert (runs_dir / "run-002").exists()

    def test_reset_writes_json_record(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        mgr = SessionResetManager(home_dir=tmp_home)
        record = mgr.reset_session("session-1")

        reset_dir = tmp_home / "resets"
        assert reset_dir.exists()
        files = list(reset_dir.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["reset_id"] == record.reset_id

    def test_reset_notifies_context_engine(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        callback = MagicMock(return_value=True)
        mgr = SessionResetManager(home_dir=tmp_home, context_engine_callback=callback)
        record = mgr.reset_session("session-1")

        callback.assert_called_once_with("session-1")
        assert record.context_engine_notified is True

    def test_reset_context_engine_failure_handled(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        callback = MagicMock(side_effect=RuntimeError("engine down"))
        mgr = SessionResetManager(home_dir=tmp_home, context_engine_callback=callback)
        record = mgr.reset_session("session-1")

        assert record.context_engine_notified is False

    def test_archive_cleanup_removes_expired(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager, ConversationArchivePolicy
        tmp_home.mkdir(parents=True)
        cp_dir = tmp_home / "conversation_archives" / "session-1"
        cp_dir.mkdir(parents=True)

        old_time = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        new_time = datetime.now(timezone.utc).isoformat()

        (cp_dir / "old.json").write_text(json.dumps({"created_at": old_time}))
        (cp_dir / "new.json").write_text(json.dumps({"created_at": new_time}))

        policy = ConversationArchivePolicy(max_age_hours=168, preserve_failed=False, preserve_tagged=False)
        mgr = SessionResetManager(home_dir=tmp_home, archive_policy=policy)
        record = mgr.reset_session("session-1", action="archive_cleanup", preserve_runs=False)

        assert "old.json" in record.cleaned_archives
        assert not (cp_dir / "old.json").exists()
        assert (cp_dir / "new.json").exists()

    def test_create_recoverable_session(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        mgr = SessionResetManager(home_dir=tmp_home)

        session = mgr.create_recoverable_session(
            owner="user-1",
            capabilities={"tools/list", "tools/call"},
        )

        assert session["owner"] == "user-1"
        assert "tools/list" in session["capabilities"]
        assert "archive_path" in session

        session_dir = tmp_home / "sessions" / session["session_id"]
        assert (session_dir / "session.json").exists()
        assert (session_dir / "session_archive.json").exists()

    def test_get_stats(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        mgr = SessionResetManager(home_dir=tmp_home)
        mgr.reset_session("s1", action="context_reset_only")
        mgr.reset_session("s2", action="full_reset")

        stats = mgr.get_stats()
        assert stats["total_resets"] == 2
        assert stats["by_action"]["context_reset_only"] == 1
        assert stats["by_action"]["full_reset"] == 1

    def test_get_reset_history_limit(self, tmp_home):
        from AstralNexus.runtime.session_reset import SessionResetManager
        tmp_home.mkdir(parents=True)
        mgr = SessionResetManager(home_dir=tmp_home)
        for i in range(10):
            mgr.reset_session(f"s-{i}")

        history = mgr.get_reset_history(limit=5)
        assert len(history) == 5
