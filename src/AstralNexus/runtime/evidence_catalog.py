"""Run-scoped evidence reading shared by reducers and global verification.

Reading a current file is not a verdict that its contents satisfy a requirement.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from AstralNexus.core.output_paths import identity_key


class EvidenceCatalog:
    def __init__(self, root: Path, *, graph: Any = None, task_version: Any = None):
        self.root = root.resolve()
        self.graph = graph
        self.task_version = task_version
        self.by_path: dict[str, list[Any]] = {}
        for artifact in (graph.artifacts.values() if graph is not None else []):
            key = self._key(artifact.path)
            self.by_path.setdefault(key, []).append(artifact)

    def _key(self, ref: Any) -> str:
        path = Path(str(ref or "").replace("\\", "/"))
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(self.root)
            except ValueError:
                return ""
        return identity_key(path.as_posix())

    def read_text(self, ref: Any, *, max_bytes: int | None = None,
                  errors: str = "strict") -> str | None:
        artifact_ref = self.graph.artifacts.get(str(ref)) if self.graph is not None else None
        key = self._key(artifact_ref.path if artifact_ref is not None else ref)
        if not key or "://" in str(ref):
            return None
        candidates = [self.root / key]
        if key.startswith("deliverables/"):
            candidates.append(self.root / ".delivery_staging" / key)
        target = next((p.resolve() for p in candidates
                       if p.resolve().is_relative_to(self.root) and p.is_file()), None)
        if target is None:
            return None
        try:
            raw = target.read_bytes()
        except OSError:
            return None
        mapped = [artifact_ref] if artifact_ref is not None else self.by_path.get(key, [])
        if mapped:
            valid = False
            for artifact in mapped:
                if getattr(artifact.acceptance_status, "value", artifact.acceptance_status) != "accepted":
                    continue
                version = artifact.metadata.get("task_version")
                if version is not None and self.task_version is not None and str(version) != str(self.task_version):
                    continue
                producer = self.graph.get_node(str(artifact.node_id)) if artifact.node_id else None
                if producer is not None and producer.status in {"failed", "superseded", "cancelled"}:
                    continue
                digest = artifact.metadata.get("content_sha256")
                if digest and hashlib.sha256(raw).hexdigest() != digest:
                    continue
                valid = True
                break
            if not valid:
                return None
        return (raw if max_bytes is None else raw[:max_bytes]).decode("utf-8", errors)

    def validate_reference(self, ref: Any, *, source_nodes: set[str]) -> dict[str, Any]:
        """Require a current accepted artifact and its frozen content version."""
        text = str(ref or "")
        direct = self.graph.artifacts.get(text) if self.graph is not None else None
        key = self._key(direct.path if direct is not None else text)
        mapped = [direct] if direct is not None else self.by_path.get(key, [])
        candidates = [self.root / key]
        if key.startswith("deliverables/"):
            candidates.extend([self.root / ".delivery_staging" / key,
                               self.root / ".delivery_staging" / key.split("/", 1)[1]])
        target = next((p.resolve() for p in candidates if key and p.resolve().is_relative_to(self.root) and p.is_file()), None)
        if target is None:
            return {"valid": False, "reason": "missing_evidence_file"}
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return {"valid": False, "reason": "unreadable_evidence_file"}
        for artifact in mapped:
            if artifact is None or str(artifact.node_id) not in source_nodes:
                continue
            if getattr(artifact.acceptance_status, "value", artifact.acceptance_status) != "accepted":
                continue
            producer = self.graph.get_node(str(artifact.node_id))
            if producer is None or producer.status != "completed":
                continue
            if str(artifact.metadata.get("task_version", 1)) != str(self.task_version):
                continue
            if str(producer.metadata.get("task_version", 1)) != str(self.task_version):
                continue
            if artifact.metadata.get("content_sha256") != digest:
                continue
            subjects_valid = True
            for raw_path, expected_hash in (artifact.metadata.get("physical_subjects") or {}).items():
                subject_key = self._key(raw_path)
                subject = (self.root / subject_key).resolve()
                try:
                    if not subject_key or not subject.is_relative_to(self.root) or not subject.is_file() or hashlib.sha256(subject.read_bytes()).hexdigest() != expected_hash:
                        subjects_valid = False
                        break
                except OSError:
                    subjects_valid = False
                    break
            if not subjects_valid:
                continue
            return {"valid": True, "artifact_id": artifact.artifact_id, "content_sha256": digest,
                    "producer": producer.node_id}
        return {"valid": False, "reason": "unaccepted_stale_or_unbound_evidence"}
