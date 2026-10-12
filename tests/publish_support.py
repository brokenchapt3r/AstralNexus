"""Publish input builders and scoped real hosts; assertions stay in tests."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Callable, TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from AstralNexus.dashboard.workbench_server import WorkbenchHost


WORKBENCH_FILES = {
    "app/x.py": b"print(1)\n", "README.md": b"# run\n",
    "current.json": b'{"version": "v1"}', "v1/old.md": b"old package\n",
}


def make_run(
    root: Path, payload: dict[str, bytes], *, name: str = "run",
) -> tuple[Path, Path]:
    run = root / name
    deliverables = run / "deliverables"
    deliverables.mkdir(parents=True, exist_ok=True)
    for relative, data in payload.items():
        target = deliverables / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return run, deliverables


def make_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture
def temporary_host() -> Callable[[Path], closing[WorkbenchHost]]:
    """Explicit context scope supports close/reopen on the same home directory."""
    from AstralNexus.dashboard.workbench_server import WorkbenchHost

    return lambda home: closing(WorkbenchHost(home))
