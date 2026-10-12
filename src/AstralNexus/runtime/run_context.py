"""Immutable run configuration and explicit propagation into worker threads."""
from __future__ import annotations
import hashlib
import json
import os
import sys
import uuid
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, TypeVar, overload, Iterator
T = TypeVar("T")


@dataclass(frozen=True)
class RunSettings:
    environment: Mapping[str, str] = field(repr=False)
    config_path: Path
    config_body: Mapping[str, Any] = field(repr=False)
    env_file: Path | None = None

    @classmethod
    def capture(cls, *, environment: Mapping[str, str] | None = None, config_path: str | Path | None = None) -> RunSettings:
        values = dict(os.environ if environment is None else environment)
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
            raise ValueError("run environment requires string names and values")
        home = Path(values.get("ASTRALNEXUS_CONFIG_HOME") or
            (str(Path(values.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "AstralNexus")
             if sys.platform == "win32" else str(Path.home() / ".AstralNexus")))
        config = Path(config_path or values.get("ASTRALNEXUS_CONFIG_FILE") or home / "config.yaml").expanduser().resolve()
        candidates = ([config.parent / ".env"] if config_path or values.get("ASTRALNEXUS_CONFIG_FILE") else [])
        if values.get("ASTRALNEXUS_ENV_FILE"):
            candidates.append(Path(values["ASTRALNEXUS_ENV_FILE"]).expanduser())
        candidates.append(home / ".env")
        env_file = None
        for candidate in candidates:
            if not candidate.is_file(): continue
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                key, value = line.split("=", 1)
                value = value.strip().strip("\"'")
                if key.strip() and value: values.setdefault(key.strip(), value)
            env_file = candidate.resolve()
            break
        for key in ("ASTRALNEXUS_MAX_TOOL_ROUNDS", "ASTRALNEXUS_MAX_LLM_REQUESTS", "ASTRALNEXUS_MAX_SESSION_TOKENS", "ASTRALNEXUS_NODE_TIMEOUT"):
            if key not in values: continue
            numeric_budget = float(values[key])
            if not math.isfinite(numeric_budget) or numeric_budget < 0 or (key != "ASTRALNEXUS_NODE_TIMEOUT" and not numeric_budget.is_integer()):
                raise ValueError(f"invalid finite run budget setting: {key}")
        body: dict = {}
        if config.is_file():
            import yaml
            loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError("run model configuration must be an object")
            body = loaded or {}
        from AstralNexus.runtime.immutable import freeze
        return cls(MappingProxyType(values), config, freeze(body), env_file)

    def audit(self) -> dict[str, Any]:
        public = {key: ("[redacted]" if any(token in key for token in ("KEY", "PASSWORD", "SECRET", "ACCESS_TOKEN", "AUTH_TOKEN"))
                        or "URL" in key else value)
                  for key, value in self.environment.items() if key.startswith("ASTRALNEXUS_")}
        encoded = json.dumps(public, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {"settings_version": 1, "fingerprint": hashlib.sha256(encoded).hexdigest(),
                "values": public, "config_path": str(self.config_path), "env_file": str(self.env_file or "")}


@dataclass(frozen=True)
class RunContext:
    run_dir: Path
    settings: RunSettings
    scope_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def approval_identity(self) -> str:
        return "run:" + self.scope_id

    @contextmanager
    def scope(self) -> Iterator[None]:
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)


_active: ContextVar[RunContext | None] = ContextVar("astralnexus_run_context", default=None)


def current_run_context() -> RunContext | None:
    return _active.get()


@contextmanager
def run_context_scope(context: RunContext | None) -> Iterator[None]:
    token = _active.set(context)
    try: yield
    finally: _active.reset(token)


@overload
def get_env(key: str) -> str | None: ...
@overload
def get_env(key: str, default: T) -> str | T: ...
def get_env(key: str, default: Any = None) -> Any:
    context = current_run_context()
    return (context.settings.environment if context is not None else os.environ).get(key, default)
