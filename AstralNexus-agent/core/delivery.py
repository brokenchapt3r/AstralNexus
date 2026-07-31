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
from typing import Any, Iterable


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
        authorized: bool,
        source_refs: Iterable[str] = (),
        dependencies: Iterable[str] = (),
        version: int | None = None,
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
        if not authorized:
            raise PermissionError("Delivery Gate did not authorize publishing")
        
        if not self.staging_dir.exists():
            raise ValueError("No staging directory found; call begin() first")
        
        staged_files = list(self.staging_dir.rglob("*"))
        staged_files = [f for f in staged_files if f.is_file()]
        
        if not staged_files:
            raise ValueError("Staging directory is empty")
        
        # Validate: reject symlinks and paths escaping run_dir
        for file in staged_files:
            if file.is_symlink():
                raise PermissionError(
                    f"Staging contains symlink: {file.relative_to(self.staging_dir)}"
                )
            real_path = file.resolve()
            if not str(real_path).startswith(str(self.run_dir.resolve())):
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
                
                # Generate manifest
                artifacts = []
                for file in sorted(temp_dir.rglob("*")):
                    if not file.is_file():
                        continue
                    rel_path = file.relative_to(temp_dir)
                    sha256 = hashlib.sha256(file.read_bytes()).hexdigest()
                    artifacts.append({
                        "path": str(rel_path),
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
                }
                
                manifest_path = temp_dir / "delivery_manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )
                
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
                
                # Try symlink first, fall back to pointer file on Windows
                try:
                    self.current_link.symlink_to(version_dir.name, target_is_directory=True)
                except (OSError, NotImplementedError):
                    # Windows may fail without developer mode; use pointer file
                    pointer_file = self.deliverables_dir / "current.json"
                    pointer_file.write_text(
                        json.dumps({"current_version": version, "path": version_dir.name}),
                        encoding="utf-8"
                    )
                
                # Clean staging
                for item in self.staging_dir.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                
                return manifest
                
            except Exception:
                # Cleanup temp directory on failure
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                raise
    
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
