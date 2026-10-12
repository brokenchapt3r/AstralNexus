"""Immutable launch environment recording which layer supplied each value.

Port of ``@deepseek-ai/dsh-launch-environment`` (commit 5badb15). Contract kept:
- Layer order ``process → project-env → user-env``; the first non-undefined
  value wins, so a resolved value always reports its supplying layer and
  ``.env`` files never silently shadow the process environment.
- One physical environment variable with two spellings (``FOO``/``foo`` on
  Windows) resolves once, not twice.
- ``launched_through_ssh`` reads ``SSH_CONNECTION``/``SSH_TTY`` from the
  inherited process layer ONLY — project/user ``.env`` values never establish
  an SSH session.
- ``launch_environment_of(ctx)`` returns the host-registered snapshot, falling
  back to a process-layer-only snapshot when the host provided none.

Python adaptation: lookup keys are uppercased on ``win32`` (case-insensitive
environment), verbatim elsewhere; layer values are exposed through
``MappingProxyType`` and resolutions are frozen dataclasses (immutability).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

DSH_LAUNCH_ENVIRONMENT_KEY = "launchEnvironment"
SOURCE_ORDER: tuple = ("process", "project-env", "user-env")


@dataclass(frozen=True)
class EnvironmentResolution:
    """One resolved variable: the value plus the layer that supplied it."""
    value: str
    source: str
    path: Optional[str] = None


@dataclass(frozen=True)
class LaunchEnvironmentLayer:
    """One declared source layer of the launch environment."""
    source: str
    values: Mapping[str, str]
    path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.source not in SOURCE_ORDER:
            raise ValueError(f"layer source must be one of {SOURCE_ORDER}, got {self.source!r}")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


def _lookup_key(name: str) -> str:
    return name.upper() if sys.platform == "win32" else name


class LaunchEnvironmentSnapshot:
    """Immutable ordered view over launch-environment layers."""

    __slots__ = ("_layers", "_index")

    def __init__(self, layers: Sequence[LaunchEnvironmentLayer]):
        self._layers: List[LaunchEnvironmentLayer] = list(layers)
        index: Dict[str, Dict[str, EnvironmentResolution]] = {}
        for source in SOURCE_ORDER:
            for layer in self._layers:
                if layer.source != source:
                    continue
                for name, value in layer.values.items():
                    # setdefault keeps the first spelling of one physical
                    # variable per layer (FOO before foo on Windows).
                    index.setdefault(_lookup_key(name), {}).setdefault(
                        source, EnvironmentResolution(value, source, layer.path))
        self._index = index

    @property
    def layers(self) -> Sequence[LaunchEnvironmentLayer]:
        return tuple(self._layers)

    def get(self, name: str) -> Optional[EnvironmentResolution]:
        """First declared source with a value, in layer order; None when absent."""
        per_source = self._index.get(_lookup_key(name))
        if not per_source:
            return None
        for source in SOURCE_ORDER:
            if source in per_source:
                return per_source[source]
        return None

    def get_from(self, name: str, sources: Iterable[str]) -> Optional[EnvironmentResolution]:
        """First listed source with a value, in the caller's source order."""
        per_source = self._index.get(_lookup_key(name))
        if not per_source:
            return None
        for source in sources:
            if source in per_source:
                return per_source[source]
        return None


def create_launch_environment_snapshot(
        layers: Sequence[Any]) -> LaunchEnvironmentSnapshot:
    """Build the snapshot from layer declarations (dicts or LaunchEnvironmentLayer)."""
    normalized: List[LaunchEnvironmentLayer] = []
    for layer in layers:
        if isinstance(layer, LaunchEnvironmentLayer):
            normalized.append(layer)
        elif isinstance(layer, dict):
            normalized.append(LaunchEnvironmentLayer(
                source=str(layer["source"]), values=dict(layer["values"]),
                path=layer.get("path")))
        else:
            raise ValueError(f"layer must be a mapping or LaunchEnvironmentLayer, got {type(layer).__name__}")
    return LaunchEnvironmentSnapshot(normalized)


def launch_environment_of(ctx: Any) -> LaunchEnvironmentSnapshot:
    """Host-registered snapshot, or a process-only snapshot when the host gave none."""
    registered = ctx.get(DSH_LAUNCH_ENVIRONMENT_KEY) if hasattr(ctx, "get") else None
    if registered is not None:
        return registered
    return create_launch_environment_snapshot([
        LaunchEnvironmentLayer(source="process", values=dict(os.environ))])


def launched_through_ssh(environment: LaunchEnvironmentSnapshot) -> bool:
    """SSH markers from the inherited process layer only; .env never counts."""
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        resolution = environment.get_from(name, ["process"])
        if resolution is not None and resolution.value != "":
            return True
    return False
