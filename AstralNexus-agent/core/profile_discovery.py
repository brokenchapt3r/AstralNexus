"""Multi-source Worker Profile discovery with priority and risk gating.

Profiles can be loaded from four sources (lowest to highest priority):

1. **builtin**  — shipped with AstralNexus (role_registry.py)
2. **user**     — user-level ``~/.astralnexus/profiles/``
3. **project**  — project-level ``<cwd>/.astralnexus/profiles/``
4. **task**     — task-scoped temporary profiles created at runtime

A higher-priority source overrides a lower-priority source with the same
``name``.  Project-level profiles are **not** allowed to declare
``risk_level=high`` without explicit Governor approval — this prevents a
malicious or misconfigured project from escalating privileges.

Each profile YAML/JSON must contain at minimum:

    name, description, capabilities, tools, input_contract,
    output_contract, risk_level, model_preferences
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class ProfileSource(IntEnum):
    """Profile discovery priority (higher value = higher priority)."""
    BUILTIN = 0
    USER = 1
    PROJECT = 2
    TASK = 3


class RiskLevel(str):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class WorkerProfileSpec:
    """Immutable specification for a discovered Worker Profile."""
    name: str
    description: str
    capabilities: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    input_contract: str = ""
    output_contract: str = ""
    risk_level: str = "low"
    model_preferences: Dict[str, Any] = field(default_factory=dict)
    source: ProfileSource = ProfileSource.BUILTIN
    source_path: str = ""

    # ----- validation -----
    def __post_init__(self) -> None:
        self.risk_level = (self.risk_level or "low").lower()
        if self.risk_level not in ("low", "medium", "high"):
            raise ValueError(
                f"Profile {self.name!r}: risk_level must be low|medium|high, "
                f"got {self.risk_level!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "risk_level": self.risk_level,
            "model_preferences": dict(self.model_preferences),
            "source": self.source.name.lower(),
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, source: ProfileSource = ProfileSource.BUILTIN,
                  source_path: str = "") -> "WorkerProfileSpec":
        required = {"name", "description"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"Profile missing required fields: {missing}")
        return cls(
            name=str(data["name"]),
            description=str(data["description"]),
            capabilities=list(data.get("capabilities") or []),
            tools=list(data.get("tools") or []),
            input_contract=str(data.get("input_contract", "")),
            output_contract=str(data.get("output_contract", "")),
            risk_level=str(data.get("risk_level", "low")),
            model_preferences=dict(data.get("model_preferences") or {}),
            source=source,
            source_path=source_path,
        )


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryResult:
    """Aggregated result of a profile discovery pass."""
    profiles: Dict[str, WorkerProfileSpec] = field(default_factory=dict)
    rejected: List[Dict[str, str]] = field(default_factory=list)

    def get(self, name: str) -> Optional[WorkerProfileSpec]:
        return self.profiles.get(name)

    def list_names(self) -> List[str]:
        return sorted(self.profiles)

    def to_summary(self) -> Dict[str, Any]:
        return {
            "total_profiles": len(self.profiles),
            "by_source": self._count_by_source(),
            "rejected": list(self.rejected),
        }

    def _count_by_source(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for p in self.profiles.values():
            key = p.source.name.lower()
            counts[key] = counts.get(key, 0) + 1
        return counts


class ProfileDiscovery:
    """Discovers Worker Profiles from multiple sources with priority merging.

    Parameters
    ----------
    builtin_profiles :
        Pre-loaded built-in profiles (from role_registry).
    user_dir :
        Override for user-level profile directory.
    project_dir :
        Override for project-level profile directory.
    governor_approved_high_risk :
        Set of profile names explicitly approved by Governor for high-risk.
    """

    REQUIRED_FIELDS = {"name", "description", "capabilities", "tools",
                       "input_contract", "output_contract", "risk_level",
                       "model_preferences"}

    def __init__(
        self,
        builtin_profiles: Optional[Sequence[WorkerProfileSpec]] = None,
        *,
        user_dir: Optional[str] = None,
        project_dir: Optional[str] = None,
        governor_approved_high_risk: Optional[set[str]] = None,
    ) -> None:
        self._builtin = {p.name: p for p in (builtin_profiles or [])}
        self._user_dir = user_dir or self._default_user_dir()
        self._project_dir = project_dir or self._default_project_dir()
        self._task_profiles: Dict[str, WorkerProfileSpec] = {}
        self._approved_high_risk: set[str] = set(governor_approved_high_risk or [])

    # ----- directory defaults -----
    @staticmethod
    def _default_user_dir() -> str:
        home = os.environ.get("ASTRALNEXUS_HOME", os.path.expanduser("~"))
        return os.path.join(home, ".astralnexus", "profiles")

    @staticmethod
    def _default_project_dir() -> str:
        return os.path.join(os.getcwd(), ".astralnexus", "profiles")

    # ----- public API -----
    def discover(self) -> DiscoveryResult:
        """Run a full discovery pass across all four sources."""
        result = DiscoveryResult()

        # 1. Built-in (lowest priority)
        for name, spec in self._builtin.items():
            result.profiles[name] = spec

        # 2. User-level
        self._load_directory(self._user_dir, ProfileSource.USER, result)

        # 3. Project-level (with high-risk gate)
        self._load_directory(self._project_dir, ProfileSource.PROJECT, result,
                             enforce_risk_gate=True)

        # 4. Task-level (highest priority)
        for name, spec in self._task_profiles.items():
            result.profiles[name] = spec

        return result

    def register_task_profile(self, spec: WorkerProfileSpec) -> None:
        """Register a runtime task-scoped profile."""
        spec.source = ProfileSource.TASK
        self._task_profiles[spec.name] = spec

    def unregister_task_profile(self, name: str) -> bool:
        return self._task_profiles.pop(name, None) is not None

    def approve_high_risk(self, name: str) -> None:
        """Governor explicitly approves a profile for high-risk execution."""
        self._approved_high_risk.add(name)

    # ----- internal -----
    def _load_directory(
        self,
        directory: str,
        source: ProfileSource,
        result: DiscoveryResult,
        *,
        enforce_risk_gate: bool = False,
    ) -> None:
        if not os.path.isdir(directory):
            return

        for fname in sorted(os.listdir(directory)):
            fpath = os.path.join(directory, fname)
            if not os.path.isfile(fpath):
                continue
            if not (fname.endswith(".json") or fname.endswith(".yaml") or fname.endswith(".yml")):
                continue
            self._load_file(fpath, source, result, enforce_risk_gate=enforce_risk_gate)

    def _load_file(
        self,
        fpath: str,
        source: ProfileSource,
        result: DiscoveryResult,
        *,
        enforce_risk_gate: bool = False,
    ) -> None:
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                if fpath.endswith(".json"):
                    data = json.load(fh)
                else:
                    # YAML — try PyYAML, fall back to simple key: value parser
                    data = self._parse_yaml_fallback(fh.read())
        except Exception as exc:
            result.rejected.append({"path": fpath, "reason": f"parse_error: {exc}"})
            return

        if not isinstance(data, dict):
            result.rejected.append({"path": fpath, "reason": "not_a_mapping"})
            return

        # Validate required fields
        missing = self.REQUIRED_FIELDS - set(data)
        if missing:
            result.rejected.append({
                "path": fpath,
                "reason": f"missing_fields: {sorted(missing)}",
            })
            return

        try:
            spec = WorkerProfileSpec.from_dict(data, source=source, source_path=fpath)
        except ValueError as exc:
            result.rejected.append({"path": fpath, "reason": str(exc)})
            return

        # Risk gate: project-level profiles cannot declare high-risk
        if enforce_risk_gate and spec.risk_level == "high":
            if spec.name not in self._approved_high_risk:
                result.rejected.append({
                    "path": fpath,
                    "reason": "project_high_risk_denied: Governor approval required",
                })
                # Downgrade to medium instead of silently dropping
                spec.risk_level = "medium"

        # Higher priority overrides lower
        result.profiles[spec.name] = spec

    @staticmethod
    def _parse_yaml_fallback(text: str) -> Dict[str, Any]:
        """Minimal YAML-subset parser for simple flat profile files.

        Handles ``key: value`` and ``key:\\n  - item`` list syntax.
        """
        try:
            import yaml  # type: ignore
            return yaml.safe_load(text) or {}
        except ImportError:
            pass

        result: Dict[str, Any] = {}
        current_key: Optional[str] = None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and current_key:
                if not isinstance(result.get(current_key), list):
                    result[current_key] = []
                result[current_key].append(stripped[2:].strip().strip('"').strip("'"))
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                current_key = key
                if value:
                    result[key] = value
                else:
                    result[key] = []
        return result
