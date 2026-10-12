from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceBoundary:
    root: Path

    @classmethod
    def from_cwd(cls) -> "WorkspaceBoundary":
        return cls(Path.cwd().resolve())

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Path is outside workspace boundary: {resolved}") from exc
        return resolved

