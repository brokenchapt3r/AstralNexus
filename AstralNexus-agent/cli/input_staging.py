"""Finite, task-local staging; reject oversized bundles before copying."""
from __future__ import annotations
import os
from pathlib import Path

_EXCLUDED = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".astralnexus", ".astralnexus_state",
             ".astralnexus_deploy", "runs", "02_运行产物", "04_归档", "05_比赛交付包"}


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def stage_task_inputs(task_file: Path, run_dir: Path) -> list[str]:
    file_limit = int(os.getenv("ASTRALNEXUS_INPUT_MAX_FILES", "512"))
    single_limit = int(os.getenv("ASTRALNEXUS_INPUT_MAX_FILE_BYTES", str(10 * 1024 * 1024)))
    total_limit = int(os.getenv("ASTRALNEXUS_INPUT_MAX_TOTAL_BYTES", str(64 * 1024 * 1024)))
    if min(file_limit, single_limit, total_limit) <= 0:
        raise ValueError("input staging limits must be positive")
    if not task_file.is_file() or _linked(task_file):
        raise ValueError("task input must be a regular, non-linked file")
    root = task_file.parent.resolve()
    output = run_dir.resolve()
    files = [(task_file, Path(task_file.name))]
    for directory, dirs, names in os.walk(root, followlinks=False):
        folder = Path(directory)
        dirs[:] = sorted(name for name in dirs if name not in _EXCLUDED
                         and not _linked(folder / name)
                         and not (folder / name).resolve().is_relative_to(output))
        for name in sorted(names):
            source = folder / name
            if source.resolve() == task_file.resolve() or name.startswith(".env") or _linked(source) or not source.is_file():
                continue
            if source.resolve().is_relative_to(output):
                continue
            relative = source.relative_to(root)
            destination = relative if relative.parts[0] == "data" else Path("task_bundle") / relative
            files.append((source, destination))
            if len(files) > file_limit:
                raise ValueError(f"input bundle exceeds {file_limit} files")
    sizes = [source.stat().st_size for source, _ in files]
    if any(size > single_limit for size in sizes) or sum(sizes) > total_limit:
        raise ValueError("input bundle exceeds file or total byte limit")
    staged: list[str] = []
    total = 0
    for source, relative in files:
        before = source.lstat()
        if _linked(source):
            raise ValueError("input changed into a link during staging")
        with source.open("rb") as stream:
            actual = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("input changed during staging")
            content = stream.read(single_limit + 1)
        total += len(content)
        if len(content) > single_limit or total > total_limit:
            raise ValueError("input grew beyond staging limits")
        destination = run_dir / "inputs" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        staged.append(str(destination.relative_to(run_dir)))
    return staged
