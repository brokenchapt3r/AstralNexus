"""Native plugin host: declaration, dependency resolution, config validation,
lifecycle, status projection, and restart recovery (DSH B1 core loop).

Ported contracts (baseline commit 5badb15, native Python adaptation):
- ``cordis-plugin-loader``: dependency-ordered start, disposal, and failure
  isolation; a fiber's states map onto the persisted phases below.
- ``@deepseek-ai/dsh-host-plugin-inventory``: read-only projection of loader
  entries — listing the inventory never starts a plugin implicitly.
- ``@deepseek-ai/dsh-plugin-manager``: desired-state management for the
  current profile (enable/disable persists and survives host restarts).
- ``@deepseek-ai/dsh-settings`` + ``dsh-storage-json``/``dsh-storage-sqlite``:
  schema-validated plugin configuration persisted in the existing workbench
  SQLite database (no second persistence layer).

Architecture boundary (doc 48): Governor/TaskGraph/Verifier/RunCoordinator
keep authority; plugins touch host state only through the adapter interfaces
passed in ``PluginContext``. Kinds:
- ``native``: a Python module with ``start(ctx)``/``stop(ctx)`` callables.
- ``gate``: no process of its own; exposes ``toolsets``/``service`` flags the
  host consults before serving the corresponding capability.
- ``sidecar``: requires the controlled Node JSON-RPC sidecar (B3+); until the
  sidecar runtime exists, start fails loudly with a concrete reason — never a
  fake pass.

Phase vocabulary mirrors Cordis fiber phases: ``pending`` (declared, desired
enabled, not yet started), ``loading``, ``active``, ``failed``, ``unloading``,
``disposed`` (stopped; the Cordis projection of DISPOSED is null — callers
outside the host see ``stopped``).
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from .json_schema import JsonSchemaValidationError, apply_defaults, validate, validate_schema

PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
KINDS = ("native", "gate", "sidecar")
PHASES = ("pending", "loading", "active", "failed", "unloading", "disposed")
RUNNING_PHASES = ("loading", "active", "unloading")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins(id TEXT PRIMARY KEY,manifest TEXT NOT NULL,
    source TEXT NOT NULL,installed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plugin_state(id TEXT PRIMARY KEY,desired TEXT NOT NULL DEFAULT 'disabled',
    phase TEXT NOT NULL DEFAULT 'disposed',last_error TEXT,version TEXT,
    started_at REAL,stopped_at REAL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plugin_config(id TEXT PRIMARY KEY,config TEXT NOT NULL,updated_at REAL NOT NULL);
"""


class PluginError(ValueError):
    """A plugin operation failed; the message names the concrete reason."""


class PluginManifest:
    """Validated plugin declaration. Parsing fails loud on any contract gap."""

    __slots__ = ("id", "name", "version", "description", "dependencies",
                 "kind", "entry", "config_schema", "toolsets", "service", "platform",
                 "typert")

    def __init__(self, raw: dict[str, Any]):
        if not isinstance(raw, dict):
            raise PluginError("manifest must be an object")
        ident = raw.get("id")
        if not isinstance(ident, str) or not PLUGIN_ID.match(ident):
            raise PluginError(f"manifest id must match {PLUGIN_ID.pattern}: {ident!r}")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PluginError(f"{ident}: name must be a non-empty string")
        version = raw.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PluginError(f"{ident}: version must be a non-empty string")
        kind = raw.get("kind", "gate")
        if kind not in KINDS:
            raise PluginError(f"{ident}: kind must be one of {KINDS}, got {kind!r}")
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
            raise PluginError(f"{ident}: dependencies must be a list of plugin ids")
        if ident in dependencies:
            raise PluginError(f"{ident}: plugin cannot depend on itself")
        toolsets = raw.get("toolsets", [])
        if not isinstance(toolsets, list) or any(not isinstance(x, str) for x in toolsets):
            raise PluginError(f"{ident}: toolsets must be a list of strings")
        entry = raw.get("entry")
        if kind == "native":
            if not isinstance(entry, str) or not entry.strip():
                raise PluginError(f"{ident}: native plugins must declare an entry module")
        elif entry is not None:
            raise PluginError(f"{ident}: only native plugins declare an entry module")
        config_schema = raw.get("config_schema")
        if config_schema is not None:
            try:
                validate_schema(config_schema)
            except JsonSchemaValidationError as exc:
                raise PluginError(f"{ident}: config_schema is invalid: {exc}") from None
        service = raw.get("service")
        if service is not None and not isinstance(service, str):
            raise PluginError(f"{ident}: service must be a string")
        platform = raw.get("platform")
        if platform is not None:
            if not isinstance(platform, dict) or ("os" in platform and (
                    not isinstance(platform["os"], list)
                    or any(not isinstance(x, str) for x in platform["os"]))):
                raise PluginError(f"{ident}: platform must be an object with an optional os list")
        typert = raw.get("typert")
        if typert is not None:
            if kind != "native":
                raise PluginError(f"{ident}: only native plugins declare a typert contribution")
            if (not isinstance(typert, dict)
                    or not isinstance(typert.get("module"), str) or not typert["module"].strip()
                    or not isinstance(typert.get("package"), str) or not typert["package"].strip()):
                raise PluginError(
                    f"{ident}: typert must declare non-empty 'module' and 'package' strings")
        self.id = ident
        self.name = name.strip()
        self.version = version.strip()
        self.description = str(raw.get("description") or "")
        self.dependencies: List[str] = list(dependencies)
        self.kind = kind
        self.entry = entry
        self.config_schema = config_schema
        self.toolsets: List[str] = list(toolsets)
        self.service = service
        self.platform = platform
        self.typert = typert

    def to_dict(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}

    @classmethod
    def from_json(cls, text: str) -> "PluginManifest":
        try:
            return cls(json.loads(text))
        except json.JSONDecodeError as exc:
            raise PluginError(f"manifest is not valid JSON: {exc}") from None


class PluginContext:
    """Adapter surface handed to native plugin entry points.

    Deliberately small: config, host home, and host-provided service
    singletons. Never the Governor, TaskGraph, or execution ledger — plugins
    hold no governance authority (doc 48 architecture boundary).
    """

    __slots__ = ("plugin", "config", "home", "services")

    def __init__(self, plugin: PluginManifest, config: dict[str, Any],
                 home: Path, services: Dict[str, Any]):
        self.plugin = plugin
        self.config = config
        self.home = home
        self.services = services


class DependencyResolver:
    """Topological resolution over declared manifests with honest failures."""

    def __init__(self, manifests: Dict[str, PluginManifest]):
        self.manifests = manifests

    def order(self, roots: Optional[Iterable[str]] = None) -> List[str]:
        """Return dependency-first order. Raises PluginError naming the exact
        missing dependency edge or the full cycle path (no silent truncation)."""
        targets = list(self.manifests) if roots is None else list(dict.fromkeys(roots))
        state: Dict[str, int] = {}
        stack: List[str] = []
        result: List[str] = []

        def visit(ident: str) -> None:
            marker = state.get(ident, 0)
            if marker == 2:
                return
            if marker == 1:
                cycle = stack[stack.index(ident):] + [ident]
                raise PluginError(f"dependency cycle detected: {' -> '.join(cycle)}")
            manifest = self.manifests.get(ident)
            if manifest is None:
                parent = stack[-1] if stack else "<root>"
                raise PluginError(f"missing dependency: {parent} requires {ident!r}, which is not declared")
            state[ident] = 1
            stack.append(ident)
            for dep in manifest.dependencies:
                visit(dep)
            stack.pop()
            state[ident] = 2
            result.append(ident)

        for ident in targets:
            visit(ident)
        return result


class PluginHost:
    """Authoritative plugin lifecycle for the persistent workbench host."""

    def __init__(self, store: Any, home: Optional[Path] = None):
        self.store = store
        self.home = Path(home if home is not None else store.home).resolve()
        self.services: Dict[str, Any] = {}
        # Optional TypertLoader (runtime/typert/loader.py) wired by the host;
        # plugins declaring a typert contribution fail loud without it.
        self.typert_loader: Any = None
        self._handles: Dict[str, Any] = {}
        self._lock = threading.RLock()
        with self.store.db() as db:
            db.executescript(SCHEMA)
        # A previous process died holding these phases; nothing survived the
        # restart, so project them to disposed before reconciling desired state.
        now = time.time()
        with self.store.db() as db:
            db.execute(
                "UPDATE plugin_state SET phase='disposed',stopped_at=?,updated_at=? "
                "WHERE phase IN ('active','loading','unloading')", (now, now))

    # ------------------------------------------------------------------ store

    def _manifests(self) -> Dict[str, PluginManifest]:
        with self.store.db() as db:
            rows = db.execute("SELECT id,manifest FROM plugins").fetchall()
        manifests: Dict[str, PluginManifest] = {}
        for row in rows:
            try:
                manifests[row["id"]] = PluginManifest.from_json(row["manifest"])
            except PluginError as exc:  # corrupt stored manifest: visible, not fatal
                manifests[row["id"]] = None  # type: ignore[assignment]
                self._set_state(row["id"], phase="failed", last_error=f"stored manifest is unreadable: {exc}")
        return manifests

    def _state(self, ident: str) -> dict[str, Any]:
        with self.store.db() as db:
            row = db.execute("SELECT * FROM plugin_state WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else {}

    def _set_state(self, ident: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        current = self._state(ident)
        if not current:
            with self.store.db() as db:
                db.execute("INSERT INTO plugin_state(id,desired,phase,updated_at) VALUES(?,?,?,?)",
                           (ident, fields.pop("desired", "disabled"), fields.pop("phase", "disposed"),
                            fields["updated_at"]))
            return
        columns = ", ".join(f"{key}=?" for key in fields)
        with self.store.db() as db:
            db.execute(f"UPDATE plugin_state SET {columns} WHERE id=?", (*fields.values(), ident))

    # -------------------------------------------------------------- inventory

    def install_manifest(self, raw: dict[str, Any], source: str,
                         desired: Optional[str] = None) -> PluginManifest:
        """Declare (or re-declare) a plugin. Declaration never starts it."""
        manifest = PluginManifest(raw)
        if desired is not None and desired not in ("enabled", "disabled"):
            raise PluginError(f"desired must be 'enabled' or 'disabled', got {desired!r}")
        now = time.time()
        with self._lock, self.store.db() as db:
            existing = db.execute("SELECT manifest FROM plugins WHERE id=?", (manifest.id,)).fetchone()
            db.execute("INSERT OR REPLACE INTO plugins VALUES(?,?,?,?)",
                       (manifest.id, json.dumps(manifest.to_dict(), ensure_ascii=False), source, now))
            if existing is None:
                db.execute("INSERT INTO plugin_state(id,desired,phase,updated_at) VALUES(?,?,?,?)",
                           (manifest.id, desired or "disabled", "disposed", now))
            elif desired is not None:
                db.execute("UPDATE plugin_state SET desired=?,updated_at=? WHERE id=?",
                           (desired, now, manifest.id))
        if existing is not None:
            old = PluginManifest.from_json(existing["manifest"])
            if old.version != manifest.version and self._state(manifest.id).get("phase") == "active":
                # A running plugin keeps its loaded version until reconciled;
                # never pretend the new declaration is already in effect.
                self._set_state(manifest.id, last_error=(
                    self._state(manifest.id).get("last_error")
                    or f"declared version {manifest.version} differs from running {old.version}; restart to apply"))
        return manifest

    def inventory(self) -> List[dict[str, Any]]:
        """Read-only projection (dsh-host-plugin-inventory contract): declared
        manifests plus persisted state. Never starts, stops, or reconciles."""
        manifests = self._manifests()
        with self.store.db() as db:
            states = {row["id"]: dict(row) for row in db.execute("SELECT * FROM plugin_state")}
            configs = {row["id"]: row["config"] for row in db.execute("SELECT id,config FROM plugin_config")}
        items: List[dict[str, Any]] = []
        for ident in sorted(manifests):
            manifest = manifests[ident]
            state = states.get(ident, {})
            entry: dict[str, Any] = {
                "id": ident,
                "phase": state.get("phase", "disposed"),
                "desired": state.get("desired", "disabled"),
                "last_error": state.get("last_error"),
                "running_version": state.get("version"),
                "has_config": ident in configs,
            }
            if manifest is not None:
                entry.update({key: getattr(manifest, key) for key in
                              ("name", "version", "description", "dependencies", "kind",
                               "entry", "toolsets", "service", "platform")})
                entry["requires_config"] = manifest.config_schema is not None
            else:
                entry.update({"name": ident, "version": None, "kind": None,
                              "dependencies": [], "toolsets": [], "corrupt": True})
            items.append(entry)
        return items

    def snapshot(self) -> List[dict[str, Any]]:
        """Slim projection for the web client and the runtime_plugins tool."""
        manifests = self._manifests()
        dependents: Dict[str, List[str]] = {ident: [] for ident in manifests}
        for ident, manifest in manifests.items():
            if manifest is None:
                continue
            for dep in manifest.dependencies:
                dependents.setdefault(dep, []).append(ident)
        items: List[dict[str, Any]] = []
        for entry in self.inventory():
            ident = entry["id"]
            state = self._state(ident)
            items.append({
                "id": ident,
                "name": entry.get("name", ident),
                "version": entry.get("version"),
                "kind": entry.get("kind"),
                "status": "stopped" if entry["phase"] == "disposed" else entry["phase"],
                "phase": entry["phase"],
                "desired": entry.get("desired", "disabled"),
                "enabled": state.get("phase") == "active",
                "dependencies": entry.get("dependencies", []),
                "dependents": sorted(dependents.get(ident, [])),
                "last_error": entry.get("last_error"),
                "toolsets": entry.get("toolsets", []),
                "service": entry.get("service"),
            })
        return items

    def is_active(self, ident: str) -> bool:
        return self._state(ident).get("phase") == "active"

    def active_toolsets(self) -> Set[str]:
        """Toolsets whose gate plugin is currently active (host-wide floor for
        per-session tool visibility)."""
        result: Set[str] = set()
        for entry in self.inventory():
            if entry["phase"] == "active" and entry.get("kind") == "gate":
                result.update(entry.get("toolsets") or [])
        return result

    def gate_toolsets(self) -> Set[str]:
        """Every toolset declared by a gate plugin, active or not."""
        result: Set[str] = set()
        for entry in self.inventory():
            if entry.get("kind") == "gate":
                result.update(entry.get("toolsets") or [])
        return result

    def toolset_gate(self, toolset: str) -> Optional[dict[str, Any]]:
        """The gate plugin controlling ``toolset``, if any (for UI truth)."""
        for entry in self.inventory():
            if entry.get("kind") == "gate" and toolset in (entry.get("toolsets") or []):
                return {"id": entry["id"], "phase": entry["phase"],
                        "desired": entry.get("desired"), "last_error": entry.get("last_error")}
        return None

    def active_services(self) -> Set[str]:
        result: Set[str] = set()
        for entry in self.inventory():
            if entry["phase"] == "active" and entry.get("service"):
                result.add(entry["service"])
        return result

    # -------------------------------------------------------------- lifecycle

    def _resolved_config(self, manifest: PluginManifest) -> dict[str, Any]:
        with self.store.db() as db:
            row = db.execute("SELECT config FROM plugin_config WHERE id=?", (manifest.id,)).fetchone()
        stored = json.loads(row["config"]) if row else {}
        if manifest.config_schema is None:
            return stored
        resolved = apply_defaults(stored, manifest.config_schema)
        validate(resolved, manifest.config_schema)  # raises JsonSchemaValidationError
        return resolved

    def _start_one(self, manifest: PluginManifest) -> None:
        ident = manifest.id
        state = self._state(ident)
        if state.get("phase") in ("active", "loading"):
            return
        if manifest.platform and "os" in manifest.platform and sys.platform not in manifest.platform["os"]:
            self._set_state(ident, phase="failed",
                            last_error=f"platform_not_supported: requires os {manifest.platform['os']}, running on {sys.platform!r}")
            return
        for dep in manifest.dependencies:
            if self._state(dep).get("phase") != "active":
                self._set_state(ident, phase="failed",
                                last_error=f"missing_dependency: {dep!r} is not active")
                return
        try:
            config = self._resolved_config(manifest)
        except (JsonSchemaValidationError, json.JSONDecodeError) as exc:
            self._set_state(ident, phase="failed", last_error=f"config_invalid: {exc}")
            return
        self._set_state(ident, phase="loading", last_error=None)
        handle: Any = None
        if manifest.kind == "native":
            try:
                module = importlib.import_module(str(manifest.entry))
                start: Optional[Callable[[PluginContext], Any]] = getattr(module, "start", None)
                if not callable(start):
                    raise PluginError(f"entry module {manifest.entry!r} has no start(ctx) callable")
                context = PluginContext(manifest, config, self.home, self.services)
                handle = start(context)
                if manifest.typert is not None:
                    # A declared contribution is part of activation: failures
                    # aggregate into this plugin's load_failed (upstream
                    # activation-throw semantics), rolling the start back.
                    try:
                        if self.typert_loader is None:
                            raise PluginError(
                                "typert contribution declared but the host provides no typert loader")
                        self.typert_loader.attach(ident, manifest.typert["module"],
                                                  manifest.typert["package"], configured=True)
                    except Exception:
                        rollback = getattr(module, "stop", None)
                        if callable(rollback):
                            try:
                                rollback(context, handle)
                            except Exception:  # rollback must not mask the primary error
                                pass
                        handle = None
                        raise
            except Exception as exc:  # failure isolation: never take the host down
                self._handles.pop(ident, None)
                self._set_state(ident, phase="failed",
                                last_error=f"load_failed: {type(exc).__name__}: {exc}")
                return
        elif manifest.kind == "sidecar":
            self._set_state(ident, phase="failed",
                            last_error="sidecar_runtime_not_available: the controlled Node sidecar (B3) is not wired yet")
            return
        self._handles[ident] = handle
        self._set_state(ident, phase="active", version=manifest.version,
                        started_at=time.time(), last_error=None)

    def _stop_one(self, manifest: PluginManifest) -> None:
        ident = manifest.id
        state = self._state(ident)
        if state.get("phase") in ("disposed", "failed", "pending"):
            self._set_state(ident, phase="disposed", stopped_at=time.time())
            return
        self._set_state(ident, phase="unloading")
        handle = self._handles.pop(ident, None)
        withdraw_error: Optional[str] = None
        if self.typert_loader is not None:
            # Withdraw the contribution before stopping the plugin; a withdraw
            # failure is recorded, never swallowed, and never blocks disposal.
            try:
                self.typert_loader.detach(ident)
            except Exception as exc:
                withdraw_error = f"typert_withdraw_failed: {type(exc).__name__}: {exc}"
        if manifest.kind == "native":
            try:
                config = self._resolved_config(manifest)
            except Exception:
                config = {}
            try:
                module = importlib.import_module(str(manifest.entry))
                stop = getattr(module, "stop", None)
                if callable(stop):
                    stop(PluginContext(manifest, config, self.home, self.services), handle)
            except Exception as exc:
                # Disposal must complete; the error is recorded, not swallowed.
                self._set_state(ident, phase="disposed", stopped_at=time.time(),
                                last_error=f"stop_failed: {type(exc).__name__}: {exc}")
                return
        self._set_state(ident, phase="disposed", stopped_at=time.time(), last_error=withdraw_error)

    def start(self, ident: str) -> dict[str, Any]:
        with self._lock:
            manifests = self._manifests()
            manifest = manifests.get(ident)
            if manifest is None:
                raise PluginError(f"unknown plugin: {ident!r}")
            self._start_one(manifest)
            return self._state(ident)

    def stop(self, ident: str) -> List[str]:
        """Stop dependents first (depth-first), then the plugin. Returns the
        ids actually stopped by the cascade (the UI shows this list before a
        parent is disabled)."""
        with self._lock:
            manifests = self._manifests()
            if ident not in manifests:
                raise PluginError(f"unknown plugin: {ident!r}")
            stopped: List[str] = []

            def cascade(current: str) -> None:
                for other, manifest in sorted(manifests.items()):
                    if manifest is None or other == current or other in stopped:
                        continue
                    if current in manifest.dependencies and self._state(other).get("phase") in RUNNING_PHASES + ("active",):
                        cascade(other)
                if self._state(current).get("phase") in RUNNING_PHASES:
                    manifest = manifests[current]
                    if manifest is not None:
                        self._stop_one(manifest)
                    stopped.append(current)

            cascade(ident)
            return stopped

    def set_enabled(self, ident: str, enabled: bool) -> dict[str, Any]:
        """Persist desired state and reconcile the real lifecycle.

        Enabling also enables the transitive dependency chain (loader
        semantics); disabling cascades to dependents. Returns the affected
        plugin projection plus the ids whose state changed.
        """
        with self._lock:
            manifests = self._manifests()
            if ident not in manifests:
                raise PluginError(f"unknown plugin: {ident!r}")
            affected: List[str] = []
            if enabled:
                try:
                    order = DependencyResolver(manifests).order([ident])
                except PluginError:
                    # Unresolvable chain (missing dep or cycle): still persist
                    # the desired flip; _start_one below records the concrete
                    # per-plugin failure instead of throwing at the caller.
                    order = [ident]
                for target in order:
                    if self._state(target).get("desired") != "enabled":
                        self._set_state(target, desired="enabled")
                        affected.append(target)
                # Converge every desired-enabled plugin in dependency order, so
                # dependents left pending by an earlier disable come back with
                # their parent (restart-consistent reconcile).
                converge = self._desired_enabled(manifests)
                for target in converge:
                    before = self._state(target).get("phase")
                    manifest = manifests.get(target)
                    if manifest is not None:
                        self._start_one(manifest)
                    if self._state(target).get("phase") != before and target not in affected:
                        affected.append(target)
            else:
                if self._state(ident).get("desired") != "disabled":
                    self._set_state(ident, desired="disabled")
                stopped = self.stop(ident)
                for target in stopped:
                    if target != ident and self._state(target).get("desired") == "enabled":
                        # Dependents keep their desired state; they are pending
                        # until the parent returns (restart-consistent).
                        self._set_state(target, phase="pending",
                                        last_error=f"dependency_disabled: {ident!r} was disabled")
                    if target not in affected:
                        affected.append(target)
                if ident not in affected:
                    affected.append(ident)
            return {"plugin": self._projection(ident), "affected": affected}

    def _projection(self, ident: str) -> dict[str, Any]:
        for item in self.snapshot():
            if item["id"] == ident:
                return item
        raise PluginError(f"unknown plugin: {ident!r}")

    def _desired_enabled(self, manifests: Dict[str, PluginManifest]) -> List[str]:
        """Dependency-first order over all desired-enabled plugins; a broken
        edge isolates to its own chain instead of blocking healthy ones."""
        wanted = [ident for ident in sorted(manifests)
                  if self._state(ident).get("desired") == "enabled"]
        ordered: List[str] = []
        for ident in wanted:
            try:
                chain = DependencyResolver(manifests).order([ident])
            except PluginError:
                chain = [ident]
            for target in chain:
                if target not in ordered and self._state(target).get("desired") == "enabled":
                    ordered.append(target)
        return ordered

    def recover(self) -> List[str]:
        """Host boot reconciliation: start every desired-enabled plugin in
        dependency order, isolated. Returns ids that failed to come up."""
        with self._lock:
            manifests = self._manifests()
            failed: List[str] = []
            for ident in self._desired_enabled(manifests):
                manifest = manifests.get(ident)
                if manifest is None:
                    failed.append(ident)
                    continue
                if self._state(ident).get("phase") == "active":
                    continue
                self._set_state(ident, phase="pending")
                self._start_one(manifest)
                if self._state(ident).get("phase") != "active":
                    failed.append(ident)
            return failed

    def shutdown(self) -> None:
        """Graceful reverse-dependency-order disposal on host close."""
        with self._lock:
            manifests = self._manifests()
            try:
                order = DependencyResolver(manifests).order()
            except PluginError:
                order = sorted(manifests)
            for ident in reversed(order):
                manifest = manifests.get(ident)
                if manifest is not None and self._state(ident).get("phase") in RUNNING_PHASES:
                    self._stop_one(manifest)

    # ----------------------------------------------------------------- config

    def get_config(self, ident: str) -> dict[str, Any]:
        manifests = self._manifests()
        manifest = manifests.get(ident)
        if manifest is None:
            raise PluginError(f"unknown plugin: {ident!r}")
        with self.store.db() as db:
            row = db.execute("SELECT config FROM plugin_config WHERE id=?", (ident,)).fetchone()
        stored = json.loads(row["config"]) if row else {}
        if manifest.config_schema is not None:
            stored = apply_defaults(stored, manifest.config_schema)
        return {"id": ident, "config": stored,
                "schema": manifest.config_schema, "active": self.is_active(ident)}

    def put_config(self, ident: str, config: Any) -> dict[str, Any]:
        """Validate, persist, and — when the plugin is active — apply through a
        real restart (config changes never take effect as unapplied writes)."""
        if not isinstance(config, dict):
            raise PluginError("config must be an object")
        with self._lock:
            manifests = self._manifests()
            manifest = manifests.get(ident)
            if manifest is None:
                raise PluginError(f"unknown plugin: {ident!r}")
            resolved = config
            if manifest.config_schema is not None:
                resolved = apply_defaults(config, manifest.config_schema)
                try:
                    validate(resolved, manifest.config_schema)
                except JsonSchemaValidationError as exc:
                    raise PluginError(f"config_invalid: {exc}") from None
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO plugin_config VALUES(?,?,?)",
                           (ident, json.dumps(resolved, ensure_ascii=False), time.time()))
            restarted: List[str] = []
            was_active = self.is_active(ident)
            if was_active:
                restarted = self.stop(ident)
                self._start_one(manifest)
                for other in restarted:
                    if other != ident and self._state(other).get("desired") == "enabled":
                        other_manifest = manifests.get(other)
                        if other_manifest is not None:
                            self._start_one(other_manifest)
            return {"id": ident, "config": resolved, "applied": was_active,
                    "restarted": restarted, "phase": self._state(ident).get("phase"),
                    "last_error": self._state(ident).get("last_error")}

    # ------------------------------------------------------------- migration

    def seed_builtins(self, manifests: Sequence[dict[str, Any]]) -> None:
        """Install builtin declarations once; existing desired state is kept
        (redeclaration never silently flips a user's switch). A declaration
        may carry its own first-install default desired state — sensitive
        capabilities start disabled until the user opts in."""
        for raw in manifests:
            ident = str(raw.get("id"))
            with self.store.db() as db:
                known = db.execute("SELECT id FROM plugins WHERE id=?", (ident,)).fetchone()
            default_desired = str(raw.get("desired", "enabled"))
            if default_desired not in ("enabled", "disabled"):
                raise PluginError(f"{ident}: builtin desired must be 'enabled' or 'disabled'")
            self.install_manifest(raw, source="builtin",
                                  desired=None if known else default_desired)


# Builtin capability gates for the existing workbench services. Each maps to a
# real enforcement point (route gate or toolset floor), not a display entry.
BUILTIN_MANIFESTS: List[dict[str, Any]] = [
    {
        "id": "chat",
        "name": "工作台对话",
        "version": "1.0.0",
        "description": "会话内对话服务（ChatService）。停用后 /api/host/chat/* 拒绝新消息并给出具体原因。",
        "kind": "gate",
        "service": "chat",
        "dependencies": [],
    },
    {
        "id": "mcp",
        "name": "MCP 外部能力桥",
        "version": "1.0.0",
        "description": "mcp_servers/mcp_tools/mcp_call/mcp_close 工具组；停用后任何会话都拿不到 MCP 工具。",
        "kind": "gate",
        "toolsets": ["mcp"],
        "dependencies": [],
    },
    {
        "id": "runtime-host",
        "name": "持久宿主能力",
        "version": "1.0.0",
        "description": "LSP 查询、工作流与调度工具组（runtime_host）；停用后对应模型工具与会话能力一并下线。",
        "kind": "gate",
        "toolsets": ["runtime_host"],
        "dependencies": [],
    },
    {
        # Sensitive capability: first-install default is DISABLED — the user
        # opts in explicitly, and the host never touches $DSH_HOME unexpectedly.
        "id": "credentials-local",
        "name": "本地凭据库",
        "version": "1.0.0",
        "description": ("$DSH_HOME/.credentials.yaml 凭据文档（引用+记录，环境分层、注释保留热重载、"
                        "跨进程写锁）。启用后经 services['credentials'] 提供凭据缝；停用后写路径拒绝。"),
        "kind": "native",
        "entry": "AstralNexus.runtime.credentials.plugin_entry",
        "service": "credentials",
        "desired": "disabled",
        "config_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "dshHome": {"type": "string", "minLength": 1},
                "watch": {"type": "boolean", "default": True},
                "debounceMs": {"type": "number", "minimum": 0, "default": 100},
            },
            "additionalProperties": False,
        },
        "dependencies": [],
    },
    {
        # Sensitive external capability: default DISABLED, explicit opt-in;
        # talks to the DeepSeek Platform over PKCE with a local callback.
        "id": "deepseek-account-platform",
        "name": "DeepSeek 账户（Platform）",
        "version": "1.0.0",
        "description": ("DeepSeek Platform PKCE 账户提供方（浏览器授权 + 本地回调 /oauth/callback、"
                        "grant 记录持久化、issuer 校验、过期失效、后台 bounded 登出）。"
                        "启用后经 services['deepseek-account'] 提供账户缝；依赖 credentials-local。"),
        "kind": "native",
        "entry": "AstralNexus.runtime.account.plugin_entry",
        "service": "deepseek-account",
        "desired": "disabled",
        "config_schema": {
            "type": "object",
            "properties": {
                "platform_origin": {"type": "string", "minLength": 1},
                "desktop_platform": {"type": ["string", "null"], "enum": ["darwin", "win32", None]},
                "embedded_page_dist": {"type": "string"},
                "inference_origin": {"type": "string", "minLength": 1},
                "allow_loopback_http": {"type": "boolean"},
                "rewrite_browser_origin": {"type": "boolean"},
                "request_headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "account_request_headers": {"type": "object",
                                            "additionalProperties": {"type": "string"}},
                "request_timeout_ms": {"type": "number", "minimum": 1, "maximum": 120000},
                "balance_timeout_ms": {"type": "number", "minimum": 1, "maximum": 120000},
                "logout_max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                "logout_retry_delay_ms": {"type": "number", "minimum": 1, "maximum": 60000},
                "attempt_timeout_ms": {"type": "number", "minimum": 1, "maximum": 3600000},
            },
            "additionalProperties": False,
        },
        "dependencies": ["credentials-local"],
    },
]
