"""Exact publication snapshots; a path-coverage verdict is not a publish permit."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
from typing import Any

EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".probe", ".astralnexus_process_logs", "node_modules", ".git", ".venv"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", "-shm", "-wal")
FRAMEWORK_FILES = frozenset({"delivery_response.md", "delivery_verification.json", "delivery_summary.json", "delivery_path_manifest.md", "summary.json", "acceptance_report.md"})

class DeliveryRejected(RuntimeError):
    """Expected acceptance rejection, distinct from a host crash."""
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("Delivery rejected: " + json.dumps(report, ensure_ascii=False))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def snapshot(root: Path) -> tuple[tuple[str, str, int], ...]:
    root = root.resolve()
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if set(rel.parts) & EXCLUDE_PARTS or path.name.endswith(EXCLUDE_SUFFIXES):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise PermissionError("Publication snapshot contains a link or escaping path: " + rel.as_posix())
        if path.is_file():
            data = path.read_bytes()
            rows.append((rel.as_posix(), hashlib.sha256(data).hexdigest(), len(data)))
    return tuple(rows)


@dataclass(frozen=True)
class PublicationAuthorization:
    staging_root: str
    files: tuple[tuple[str, str, int], ...]
    contract_sha256: str
    gate_report_sha256: str
    task_version: int
    gate_policy_version: str = "staged-snapshot-v1"
    ownership_identity: tuple[str, str, int] | None = None


def authorize_snapshot(root: Path, contract: dict[str, Any], gate_report: dict[str, Any], *, ownership_identity: tuple[str, str, int] | None = None) -> PublicationAuthorization:
    """Bind an accepted verdict to the exact bytes and declared publish set.

    A durable run.delivery fact is the only write authorization. Historical
    verdicts remain readable but cannot create a new publication permit.
    """
    if gate_report.get("overall_status") != "pass" or gate_report.get("failed_tests") or gate_report.get("unknown_tests"):
        raise DeliveryRejected({"reason": "independent_acceptance_not_passed", "verdict": gate_report})
    if int(gate_report.get("task_version", contract.get("task_version", 1))) != int(contract.get("task_version", 1)):
        raise DeliveryRejected({"reason": "gate_contract_version_mismatch"})
    recorded_gate = root / "delivery_verification.json"
    if recorded_gate.is_file() and digest(json.loads(recorded_gate.read_text(encoding="utf-8"))) != digest(gate_report):
        raise DeliveryRejected({"reason": "recorded_gate_does_not_match_authorization"})
    if gate_report.get("gate_policy_version") != "presented-output-v1":
        raise DeliveryRejected({"reason": "legacy_delivery_authorization_retired"})
    from AstralNexus.runtime.execution_store import ExecutionStore
    receipt = gate_report.get("delivery_receipt") or {}
    store_root = Path(gate_report["receipt_store_root"]).resolve()
    if store_root != root.resolve().parent:
        raise DeliveryRejected({"reason": "receipt_belongs_to_another_run"})
    persisted = ExecutionStore(store_root).read_delivery(str(receipt.get("run_id") or ""))
    if digest(persisted) != digest(receipt) or receipt.get("satisfied") is not True:
        raise DeliveryRejected({"reason": "delivery_receipt_not_durable"})
    rows = snapshot(root)
    candidate_files = {name: (sha, size) for name, sha, size in rows}
    for path, version in receipt.get("output_files", {}).items():
        relative = path if gate_report.get("publication_prefix") == "outputs" else path.removeprefix("outputs/")
        if candidate_files.get(relative) != tuple(version[:2]):
            raise DeliveryRejected({"reason": "published_bytes_differ_from_presented_output", "path": path})
    return PublicationAuthorization(str(root.resolve()), rows, digest(contract), digest(gate_report), int(contract.get("task_version", 1)), str(gate_report.get("gate_policy_version", "staged-snapshot-v1")), ownership_identity)


