"""Canonical output layout for one AstralNexus run.

The layout is deliberately independent of task domain.  A run is the unit of
reproducibility; every execution attempt below it is immutable and uniquely
addressable.
"""

from __future__ import annotations

import re
from pathlib import Path


def normalize_worker_output_path(raw: object) -> str:
    """Map planner paths into the run's contract-owned delivery partition."""
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        return value
    parts = [part for part in value.split("/") if part and part != "."]
    if not parts or ".." in parts:
        return value
    lowered = [part.lower() for part in parts]
    if lowered[0] == "deliverables":
        if len(parts) == 1:
            return value
        if lowered[1] in {"app", "docs", "runs", "evidence"}:
            if lowered[1] == "evidence":
                parts[1] = "runs"
            return "/".join(parts)
        return "/".join(["deliverables", "app", *parts[1:]])
    if lowered[0] in {"docs", "documentation", "文档"}:
        return "/".join(["deliverables", "docs", *parts[1:]])
    if lowered[0] in {"runs", "evidence", "artifacts", "运行证据"}:
        return "/".join(["deliverables", "runs", *parts[1:]])
    if lowered[0] == "src":
        parts = parts[1:]
    return "/".join(["deliverables", "app", *parts])


class RunOutputLayout:
    """Resolve canonical paths without creating files implicitly."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.node_io_dir = self.run_dir / "node_io"
        self.layered_dir = self.run_dir / "layered"
        self.evidence_dir = self.layered_dir / "evidence_packets"
        self.reducer_dir = self.layered_dir / "reducers"
        self.deliverables_dir = self.run_dir / "deliverables"
        self.reports_dir = self.run_dir / "reports"
        self.delivery_staging_dir = self.run_dir / ".delivery_staging"
        self._delivery_root = self.deliverables_dir

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.node_io_dir,
            self.layered_dir,
            self.evidence_dir,
            self.reducer_dir,
            self.deliverables_dir,
            self.reports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def safe_component(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))
        return value.strip("._") or "unknown"

    def execution_stem(self, execution_id: str, node_id: str, task_version: int, attempt: int) -> str:
        return (
            f"{self.safe_component(execution_id)}__node-{self.safe_component(node_id)}"
            f"__v{int(task_version):03d}__a{int(attempt):03d}"
        )

    def node_input(self, stem: str) -> Path:
        return self.node_io_dir / f"{stem}__input.json"

    def node_output_json(self, stem: str) -> Path:
        return self.node_io_dir / f"{stem}__output.json"

    def node_output_markdown(self, stem: str) -> Path:
        return self.node_io_dir / f"{stem}__output.md"

    def packet_path(self, packet_id: str) -> Path:
        """Resolve canonical evidence packet path.

        When the packet_id follows the structured form
        ``packet:<node_id>:v<ver>:a<attempt>:<exec_id>``, prefer the
        short node_id stem so downstream consumers can locate packets by
        their task/node identifier.
        """
        safe = self.safe_component(packet_id)
        # Extract the node_id component (second field after 'packet:')
        parts = str(packet_id).split(":")
        if len(parts) >= 2 and parts[0] == "packet":
            node_id = self.safe_component(parts[1])
            if node_id and node_id != "unknown":
                return self.evidence_dir / f"{node_id}.json"
        return self.evidence_dir / f"{safe}.json"

    def begin_delivery_draft(self) -> Path:
        self.delivery_staging_dir.mkdir(parents=True, exist_ok=True)
        self._delivery_root = self.delivery_staging_dir
        return self.delivery_staging_dir

    def publish_delivery(self, *, authorized: bool, source_refs=(), dependencies=()):
        from AstralNexus.core.delivery import DeliveryManager
        manager = DeliveryManager(self.run_dir)
        manifest = manager.publish(authorized=authorized, source_refs=source_refs,
                                   dependencies=dependencies)
        self._delivery_root = self.deliverables_dir
        return manifest

    def delivery(self, name: str) -> Path:
        return self._delivery_root / name

    @property
    def delivery_dir(self) -> Path:
        """Current draft/publication directory for contract artifacts."""
        return self._delivery_root

    def report(self, name: str) -> Path:
        return self.reports_dir / name
