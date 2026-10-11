"""Staged, auditable multi-format delivery publishing with atomic operations."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Callable
from AstralNexus.core.delivery_snapshot import PublicationAuthorization, snapshot

# Fix Q (doc 18 2026-09-15 r13 audit P1): publish() used to copy the staging
# tree verbatim, so interpreter/log/probe by-products were promoted into the
# immutable version directory AND into delivery_manifest.json artifacts (live
# r13: ~640 KB of __pycache__/*.pyc, .probe/*.db, .astralnexus_process_logs/*).
# Six other seams in the project already exclude __pycache__; this one did not.
_PUBLISH_EXCLUDE_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".probe",
    ".astralnexus_process_logs",
    "node_modules",
    ".git",
    ".venv",
}
_PUBLISH_EXCLUDE_SUFFIXES = (".pyc", ".pyo", "-shm", "-wal")


class DeliveryManager:
    """Manages staged delivery publishing with atomic operations.
    
    Staging → validation → temp directory → atomic rename → version pointer.
    Ensures no partial deliveries on failure.
    
    Thread-safe: version allocation is protected by a lock.
    Windows-compatible: falls back to pointer file if symlink creation fails.
    """
    
    # Class-level lock for version allocation across all instances
    _version_lock = threading.Lock()
    
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.staging_dir = self.run_dir / ".delivery_staging"
        self.deliverables_dir = self.run_dir / "deliverables"
        self.current_link = self.deliverables_dir / "current"
        
    def begin(self) -> Path:
        """Start staging delivery files.
        
        Returns:
            Path to staging directory
        """
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        # Clean previous staging
        for item in self.staging_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        return self.staging_dir
    
    def publish(
        self,
        *,
        authorized: bool | PublicationAuthorization,
        source_refs: Iterable[str] = (),
        dependencies: Iterable[str] = (),
        version: int | None = None,
        ownership_check: Callable[[], bool] | None = None,
        fence_provider: Callable[[], tuple[str, str, int] | None] | None = None,
    ) -> dict[str, Any]:
        """Atomically publish staged files to versioned directory.
        
        Args:
            authorized: Must be True (delivery gate authorization)
            source_refs: Source references for all files
            dependencies: Dependencies for all files
            version: Version number (auto-incremented if None)
            
        Returns:
            Delivery manifest with status, version, artifacts
            
        Raises:
            PermissionError: If not authorized or staging contains symlinks
            ValueError: If staging is empty or version conflict
        """
        if not isinstance(authorized, PublicationAuthorization):
            raise PermissionError("Publishing requires a snapshot-bound acceptance authorization")
        if authorized.staging_root != str(self.staging_dir.resolve()) or snapshot(self.staging_dir) != authorized.files:
            raise PermissionError("Staging changed since acceptance or authorization belongs to another run")
        def assert_owner() -> None:
            if ownership_check is not None and not ownership_check():
                raise PermissionError("Publication ownership lost")
            if authorized.ownership_identity is not None and (fence_provider is None or fence_provider() != authorized.ownership_identity):
                raise PermissionError("Publication generation changed since acceptance")
        assert_owner()

        
        if not self.staging_dir.exists():
            raise ValueError("No staging directory found; call begin() first")
        
        staged_files = list(self.staging_dir.rglob("*"))
        staged_files = [f for f in staged_files if f.is_file()]
        
        if not staged_files:
            raise ValueError("Staging directory is empty")

        # Fix Q: keep by-products out of the published version.  Exclusions are
        # counted and echoed into the manifest so "what was dropped" stays
        # auditable rather than silent.
        def _publishable(file: Path) -> bool:
            if set(file.relative_to(self.staging_dir).parts) & _PUBLISH_EXCLUDE_PARTS:
                return False
            return not file.name.endswith(_PUBLISH_EXCLUDE_SUFFIXES)

        excluded_count = sum(1 for file in staged_files if not _publishable(file))
        staged_files = [file for file in staged_files if _publishable(file)]
        if not staged_files:
            raise ValueError(
                "Staging contains no publishable files after excluding "
                f"{excluded_count} by-product(s)"
            )
        
        # Validate: reject symlinks and paths escaping run_dir
        for file in staged_files:
            if file.is_symlink():
                raise PermissionError(
                    f"Staging contains symlink: {file.relative_to(self.staging_dir)}"
                )
            real_path = file.resolve()
            if not real_path.is_relative_to(self.staging_dir.resolve()):
                raise PermissionError(
                    f"Staging file escapes run_dir: {file.relative_to(self.staging_dir)}"
                )
        
        # Entire publish transaction under lock (not just version allocation)
        with self._version_lock:
            if version is None:
                version = self._next_version()
            
            self.deliverables_dir.mkdir(parents=True, exist_ok=True)
            version_dir = self.deliverables_dir / f"v{version}"
            if version_dir.exists():
                raise ValueError(f"Version v{version} already exists")
            
            # Create temporary directory for atomic operation
            temp_dir = Path(tempfile.mkdtemp(prefix=".delivery_temp_", dir=self.deliverables_dir))
            
            try:
                # Copy staged files to temp directory
                for file in staged_files:
                    rel_path = file.relative_to(self.staging_dir)
                    target = temp_dir / rel_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
                
                if snapshot(temp_dir) != authorized.files:
                    raise PermissionError("Copied publication differs from accepted snapshot")

                # Generate manifest
                artifacts = []
                for file in sorted(temp_dir.rglob("*")):
                    if not file.is_file():
                        continue
                    rel_path = file.relative_to(temp_dir)
                    sha256 = hashlib.sha256(file.read_bytes()).hexdigest()
                    artifacts.append({
                        # Fix Q: manifest paths use forward slashes like every
                        # artifact_ref the delivery gate matches against;
                        # str(Path) would emit backslashes on Windows and make
                        # the manifest non-portable.
                        "path": str(rel_path).replace("\\", "/"),
                        "sha256": sha256,
                        "size_bytes": file.stat().st_size,
                        "source_refs": list(source_refs),
                        "dependencies": list(dependencies),
                        "version": version,
                        "validation": "delivery_gate_authorized",
                    })
                
                manifest = {
                    "status": "published",
                    "version": version,
                    "timestamp": time.time(),
                    "artifacts": artifacts,
                    "excluded_count": excluded_count,
                    "authorization": {"contract_sha256": authorized.contract_sha256, "gate_report_sha256": authorized.gate_report_sha256, "task_version": authorized.task_version, "gate_policy_version": authorized.gate_policy_version, "ownership_identity": authorized.ownership_identity},
                    "exclusion_rules": {
                        "parts": sorted(_PUBLISH_EXCLUDE_PARTS),
                        "suffixes": list(_PUBLISH_EXCLUDE_SUFFIXES),
                    },
                }
                
                manifest_path = temp_dir / "delivery_manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )
                
                assert_owner()
                # Atomic rename: temp → version directory
                os.rename(temp_dir, version_dir)
                
                # Update current pointer (atomic on POSIX, fallback on Windows)
                if self.current_link.exists() or self.current_link.is_symlink():
                    if self.current_link.is_symlink():
                        self.current_link.unlink()
                    elif self.current_link.is_file():
                        self.current_link.unlink()
                    elif self.current_link.is_dir():
                        shutil.rmtree(self.current_link)
                
                # Fix O (doc 18 attempt8): the pointer file is now ALWAYS
                # written, not only when symlinking fails.  The old either/or
                # branch made the published layout host-dependent: after
                # 2026-09-13 this machine could create symlinks, so
                # current.json stopped appearing and every consumer reading the
                # pointer (acceptance harness, regression script) reported a
                # genuinely published delivery as no_published_delivery.
                pointer_file = self.deliverables_dir / "current.json"
                # current.json is the commit point, written only after the final manifest.

                # Symlink is still preferred for path-based readers; the pointer
                # above already covers hosts without symlink permission.
                #
                # Fix AX (2026-09-17): this used to be a bare
                # ``except (OSError, NotImplementedError): pass`` -- so on a host
                # where the process does not hold SeCreateSymbolicLinkPrivilege
                # the failure was invisible and ``deliverables/current`` simply
                # never appeared, with nothing anywhere saying why.  Measured on
                # the r13b host: ``Path.symlink_to`` fails with WinError 1314
                # ("客户端没有所需的特权") in both the workspace and %TEMP%, while
                # ``_winapi.CreateJunction`` needs no such privilege.  So: fall
                # back to a directory junction, then record which mode actually
                # took effect (plus the reason when neither did) in the manifest,
                # because "the pointer is missing" and "linking is unavailable on
                # this host" are very different facts for an acceptance reader.
                pointer_mode, pointer_reason = self._link_current(version_dir)
                
                manifest["current_pointer"] = {
                    "mode": pointer_mode,
                    "target": version_dir.name,
                    "path": str(self.current_link),
                    "reason": pointer_reason,
                    "fallback_pointer_file": pointer_file.name,
                }
                # Rewrite the manifest now that the pointer outcome is known, so
                # the published copy states how its own ``current`` resolves.
                # NOTE: temp_dir was renamed to version_dir above, so the
                # manifest must be re-addressed inside the published version.
                (version_dir / "delivery_manifest.json").write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )
                pointer_temp = self.deliverables_dir / ".current.json.tmp"
                pointer_temp.write_text(
                    json.dumps({"current_version": version, "path": version_dir.name}), encoding="utf-8",
                )
                assert_owner()
                os.replace(pointer_temp, pointer_file)
                # Cleanup is after commit; retaining a candidate is harmless and
                # must not turn a complete publication into a failed operation.
                for item in self.staging_dir.iterdir():
                    try:
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                    except OSError:
                        continue
                return manifest
                
            except Exception:
                # Cleanup temp directory on failure
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                raise
    
    def _link_current(self, version_dir: Path) -> tuple[str, str | None]:
        """Materialise ``deliverables/current`` and report which mode worked.

        Returns ``(mode, reason)`` where mode is one of ``symlink``,
        ``junction`` or ``file_only``.  ``reason`` carries the OS error text
        whenever a link could not be created, so a delivery whose ``current``
        path is missing is explainable instead of merely absent.
        """
        errors: list[str] = []
        try:
            self.current_link.symlink_to(version_dir.name, target_is_directory=True)
            return "symlink", None
        except (OSError, NotImplementedError) as exc:
            errors.append(f"symlink: {type(exc).__name__}: {exc}")
        # Directory junctions need no SeCreateSymbolicLinkPrivilege, so they
        # cover the common Windows case where symlinking is denied outright.
        if os.name == "nt":
            try:
                import _winapi  # noqa: PLC0415 - Windows-only stdlib accelerator
            except ImportError as exc:  # pragma: no cover - non-Windows CPython
                errors.append(f"junction: ImportError: {exc}")
            else:
                create_junction = getattr(_winapi, "CreateJunction", None)
                if create_junction is None:  # pragma: no cover - very old CPython
                    errors.append("junction: _winapi.CreateJunction unavailable")
                else:
                    try:
                        create_junction(str(version_dir.resolve()), str(self.current_link))
                        return "junction", "; ".join(errors)
                    except OSError as exc:
                        errors.append(f"junction: {type(exc).__name__}: {exc}")
        # Neither link worked: deliverables/current.json remains the pointer.
        return "file_only", "; ".join(errors)

    def _next_version(self) -> int:
        """Find next available version number."""
        if not self.deliverables_dir.exists():
            return 1
        
        existing = []
        for item in self.deliverables_dir.iterdir():
            if item.is_dir() and item.name.startswith("v"):
                try:
                    num = int(item.name[1:])
                    existing.append(num)
                except ValueError:
                    continue
        
        return max(existing, default=0) + 1
