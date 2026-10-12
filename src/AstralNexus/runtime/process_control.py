"""Bounded cleanup for Popen consumers, shared by jobs, tools and native calls."""
from __future__ import annotations

import subprocess
from typing import Any


def stop_process_tree(process: subprocess.Popen[Any], *, grace_seconds: float = 2) -> None:
    import psutil  # type: ignore[import-untyped]

    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            process.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=max(0, grace_seconds))
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=2)
    except psutil.NoSuchProcess:
        pass
    try:
        process.wait(timeout=max(0, grace_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
