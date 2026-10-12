from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeCapability:
    name: str
    mode: str
    supported_tools: tuple[str, ...] = ()
    supported_models: tuple[str, ...] = ()
    context_window_tokens: int | None = None
    max_concurrency: int = 1
    supports_file_edits: bool = True
    supports_terminal_tests: bool = True
    supports_memory: bool = True
    supports_context_compression: bool = True


@dataclass(frozen=True)
class RuntimeRequest:
    prompt: str
    run_dir: Path
    run_id: str | None = None  # 运行ID，用于追踪和日志
    graph_store_path: Path | None = None
    active_node_id: str = "T-005"
    model: str | None = None
    provider: str | None = None
    toolsets: tuple[str, ...] = ()
    ignore_rules: bool = False
    ignore_user_config: bool = False
    task_file: str | None = None


@dataclass(frozen=True)
class RuntimeResult:
    status: str
    final_response: str
    run_dir: Path
    capability: RuntimeCapability
    metrics: dict[str, Any] = field(default_factory=dict)
