"""File safety audit with structured rejection and caller tracking.

Implements #13: 更细粒度的组织策略、审计展示、调用者/参数摘要和拒绝依据记录。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from pathlib import Path


class FileOperation(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    SEARCH = "search"


class RejectReason(str, Enum):
    SENSITIVE_PATH = "sensitive_path"
    OUTSIDE_WORKSPACE = "outside_workspace"
    PROTECTED_EXTENSION = "protected_extension"
    PERMISSION_DENIED = "permission_denied"
    SIZE_EXCEEDED = "size_exceeded"
    CALLER_UNAUTHORIZED = "caller_unauthorized"


@dataclass
class FileAuditRecord:
    """Structured audit record for a file operation."""
    timestamp: float
    operation: FileOperation
    path: str
    caller: str
    args_summary: Dict[str, Any]
    allowed: bool
    reject_reason: Optional[RejectReason] = None
    reject_detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "operation": self.operation.value,
            "path": self.path,
            "caller": self.caller,
            "args_summary": self.args_summary,
            "allowed": self.allowed,
            "reject_reason": self.reject_reason.value if self.reject_reason else None,
            "reject_detail": self.reject_detail,
        }


class FileSafetyAuditor:
    """Centralized file safety audit with structured rejection."""

    _PROTECTED_PATHS = frozenset({
        ".git", ".ssh", ".aws", ".azure", ".docker", ".gnupg",
        ".env", ".env.local", ".env.production", ".env.development",
        "credentials", "credential", "secrets", "secret", "tokens",
        ".kube", ".npmrc", ".pypirc",
    })

    _PROTECTED_FILES = frozenset({
        "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys",
        "known_hosts", "credentials.json", "secrets.json",
        "service-account.json", "token.json", "keyfile.json",
        ".netrc", ".pgpass", "shadow", "passwd",
    })

    _PROTECTED_EXTENSIONS = frozenset({
        ".pem", ".key", ".p12", ".pfx", ".keystore",
        ".gpg", ".asc", ".env",
    })

    def __init__(self, *, max_audit_log: int = 1000):
        self._audit_log: List[FileAuditRecord] = []
        self._max_audit_log = max_audit_log

    def check_path_safety(
        self,
        path: str,
        workspace: str,
        operation: FileOperation,
        caller: str,
        args: Optional[Dict[str, Any]] = None,
    ) -> FileAuditRecord:
        """Check if a path is safe and return structured audit record."""
        resolved = Path(path).resolve()
        workspace_resolved = Path(workspace).resolve()

        record = FileAuditRecord(
            timestamp=time.time(),
            operation=operation,
            path=path,
            caller=caller,
            args_summary=args or {},
            allowed=True,
        )

        # Check 1: Outside workspace
        if workspace_resolved not in resolved.parents and resolved != workspace_resolved:
            record.allowed = False
            record.reject_reason = RejectReason.OUTSIDE_WORKSPACE
            record.reject_detail = f"Path {path} is outside workspace {workspace}"
            self._record(record)
            return record

        # Check 2: Protected path components
        path_parts = {p.lower() for p in resolved.parts}
        if path_parts.intersection(self._PROTECTED_PATHS):
            record.allowed = False
            record.reject_reason = RejectReason.SENSITIVE_PATH
            blocked = path_parts.intersection(self._PROTECTED_PATHS)
            record.reject_detail = f"Contains protected component: {blocked}"
            self._record(record)
            return record

        # Check 3: Protected file names
        if resolved.name.lower() in self._PROTECTED_FILES:
            record.allowed = False
            record.reject_reason = RejectReason.SENSITIVE_PATH
            record.reject_detail = f"Protected file: {resolved.name}"
            self._record(record)
            return record

        # Check 4: Protected extensions
        if resolved.suffix.lower() in self._PROTECTED_EXTENSIONS:
            record.allowed = False
            record.reject_reason = RejectReason.PROTECTED_EXTENSION
            record.reject_detail = f"Protected extension: {resolved.suffix}"
            self._record(record)
            return record

        self._record(record)
        return record

    def get_audit_log(self, last_n: int = 50) -> List[Dict[str, Any]]:
        """Get recent audit records."""
        recent = self._audit_log[-last_n:]
        return [r.to_dict() for r in recent]

    def get_rejection_summary(self) -> Dict[str, Any]:
        """Get summary of rejections by reason."""
        rejections = [r for r in self._audit_log if not r.allowed]
        by_reason: Dict[str, int] = {}
        for r in rejections:
            key = r.reject_reason.value if r.reject_reason else "unknown"
            by_reason[key] = by_reason.get(key, 0) + 1

        return {
            "total_checks": len(self._audit_log),
            "total_rejections": len(rejections),
            "rejection_rate": len(rejections) / max(1, len(self._audit_log)),
            "by_reason": by_reason,
        }

    def _record(self, record: FileAuditRecord) -> None:
        """Append record, truncating old entries if needed."""
        self._audit_log.append(record)
        if len(self._audit_log) > self._max_audit_log:
            self._audit_log = self._audit_log[-self._max_audit_log:]
