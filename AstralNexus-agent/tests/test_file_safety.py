"""Tests for core.file_safety module."""
import pytest
from pathlib import Path
from AstralNexus.core.file_safety import (
    FileSafetyAuditor,
    FileOperation,
    RejectReason,
)


@pytest.fixture
def auditor():
    return FileSafetyAuditor(max_audit_log=100)


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return str(ws)


class TestPathSafety:
    def test_safe_path_allowed(self, auditor, workspace):
        safe_path = Path(workspace) / "docs" / "readme.md"
        record = auditor.check_path_safety(
            str(safe_path), workspace, FileOperation.READ, "worker_1"
        )
        assert record.allowed is True
        assert record.reject_reason is None

    def test_outside_workspace_rejected(self, auditor, workspace):
        outside = "/etc/passwd"
        record = auditor.check_path_safety(
            outside, workspace, FileOperation.READ, "worker_1"
        )
        assert record.allowed is False
        assert record.reject_reason == RejectReason.OUTSIDE_WORKSPACE
        assert "outside workspace" in record.reject_detail

    def test_git_directory_rejected(self, auditor, workspace):
        git_path = Path(workspace) / ".git" / "config"
        record = auditor.check_path_safety(
            str(git_path), workspace, FileOperation.READ, "worker_1"
        )
        assert record.allowed is False
        assert record.reject_reason == RejectReason.SENSITIVE_PATH
        assert ".git" in record.reject_detail

    def test_env_file_rejected(self, auditor, workspace):
        env_path = Path(workspace) / ".env"
        record = auditor.check_path_safety(
            str(env_path), workspace, FileOperation.READ, "worker_1"
        )
        assert record.allowed is False
        assert record.reject_reason == RejectReason.SENSITIVE_PATH

    def test_credentials_file_rejected(self, auditor, workspace):
        cred_path = Path(workspace) / "credentials.json"
        record = auditor.check_path_safety(
            str(cred_path), workspace, FileOperation.READ, "worker_1"
        )
        assert record.allowed is False
        assert record.reject_reason == RejectReason.SENSITIVE_PATH
        assert "credentials.json" in record.reject_detail

    def test_pem_extension_rejected(self, auditor, workspace):
        pem_path = Path(workspace) / "private.pem"
        record = auditor.check_path_safety(
            str(pem_path), workspace, FileOperation.READ, "worker_1"
        )
        assert record.allowed is False
        assert record.reject_reason == RejectReason.PROTECTED_EXTENSION
        assert ".pem" in record.reject_detail


class TestAuditLog:
    def test_audit_log_records_checks(self, auditor, workspace):
        path1 = Path(workspace) / "file1.txt"
        path2 = Path(workspace) / "file2.txt"

        auditor.check_path_safety(str(path1), workspace, FileOperation.READ, "w1")
        auditor.check_path_safety(str(path2), workspace, FileOperation.WRITE, "w2")

        log = auditor.get_audit_log(last_n=10)
        assert len(log) == 2
        assert log[0]["caller"] == "w1"
        assert log[1]["operation"] == "write"

    def test_audit_log_truncates_old_entries(self, auditor, workspace):
        for i in range(150):
            path = Path(workspace) / f"file{i}.txt"
            auditor.check_path_safety(str(path), workspace, FileOperation.READ, "w1")

        log = auditor.get_audit_log(last_n=200)
        assert len(log) == 100  # max_audit_log limit


class TestRejectionSummary:
    def test_empty_summary(self, auditor):
        summary = auditor.get_rejection_summary()
        assert summary["total_checks"] == 0
        assert summary["total_rejections"] == 0
        assert summary["rejection_rate"] == 0.0

    def test_summary_counts_rejections(self, auditor, workspace):
        safe = Path(workspace) / "safe.txt"
        git_path = Path(workspace) / ".git" / "config"
        env_path = Path(workspace) / ".env"

        auditor.check_path_safety(str(safe), workspace, FileOperation.READ, "w1")
        auditor.check_path_safety(str(git_path), workspace, FileOperation.READ, "w2")
        auditor.check_path_safety(str(env_path), workspace, FileOperation.READ, "w3")

        summary = auditor.get_rejection_summary()
        assert summary["total_checks"] == 3
        assert summary["total_rejections"] == 2
        assert "sensitive_path" in summary["by_reason"]

    def test_rejection_rate_calculation(self, auditor, workspace):
        for i in range(10):
            path = Path(workspace) / f"file{i}.txt"
            auditor.check_path_safety(str(path), workspace, FileOperation.READ, "w1")

        for i in range(10):
            path = Path(workspace) / ".git" / f"file{i}.txt"
            auditor.check_path_safety(str(path), workspace, FileOperation.READ, "w1")

        summary = auditor.get_rejection_summary()
        assert summary["total_checks"] == 20
        assert summary["total_rejections"] == 10
        assert summary["rejection_rate"] == 0.5


class TestRecordSerialization:
    def test_allowed_record_to_dict(self, auditor, workspace):
        path = Path(workspace) / "test.txt"
        record = auditor.check_path_safety(
            str(path), workspace, FileOperation.READ, "worker_1",
            args={"line_count": 100}
        )
        d = record.to_dict()
        assert d["allowed"] is True
        assert d["operation"] == "read"
        assert d["caller"] == "worker_1"
        assert d["args_summary"] == {"line_count": 100}
        assert d["reject_reason"] is None

    def test_rejected_record_to_dict(self, auditor, workspace):
        path = Path(workspace) / ".ssh" / "id_rsa"
        record = auditor.check_path_safety(
            str(path), workspace, FileOperation.READ, "worker_1"
        )
        d = record.to_dict()
        assert d["allowed"] is False
        assert d["reject_reason"] == "sensitive_path"
        assert len(d["reject_detail"]) > 0
