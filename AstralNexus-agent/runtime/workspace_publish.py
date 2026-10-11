"""Publish isolated-run deliverables into a session-bound workspace area.

Contract
--------
An isolated run directory holds the artifacts produced for one run.  Those
artifacts must be published into a location that is *bound* to the publishing
session/turn/run and to the exact bytes that were published, without ever
overwriting a file the user already owns in the workspace.

All published bytes live under::

    <workspace_root>/.astralnexus_delivery/<delivery_id>/published/<rel>

so the user's own workspace tree is never touched by a default publish.  Every
state transition is written to a journal that is itself atomically replaced, so
an interrupted publish can be rolled back or resumed after a restart.

Pure standard library.  No network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "DELIVERY_DIRNAME",
    "JOURNAL_PREFIX",
    "BINDING_NAME",
    "PublishEntry",
    "PublishResult",
    "build_manifest",
    "validate_manifest",
    "publish_deliverables",
    "recover_pending",
    "read_binding",
    "content_base_mismatches",
]

DELIVERY_DIRNAME = ".astralnexus_delivery"
JOURNAL_PREFIX = "journal_"
BINDING_NAME = "delivery_binding.json"

MANIFEST_SHA256_FIELD = "manifest_sha256"
FILES_FIELD = "files"

_IGNORED_DIR_NAMES = frozenset({"__pycache__"})
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})
_IGNORED_SUFFIXES = (".pyc",)

_PHASE_STAGING = "staging"
_PHASE_PUBLISHING = "publishing"
_PHASE_DONE = "done"
_PHASE_ROLLED_BACK = "rolled_back"

_STATE_PENDING = "pending"
_STATE_STAGED = "staged"
_STATE_PUBLISHED = "published"

_CHUNK = 1 << 20


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishEntry:
    """One deliverable: its source bytes, its destination rel path, its hash."""

    source: Path
    rel: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one publish (or recovery) attempt."""

    delivery_id: str
    run_dir: str
    workspace_root: str
    published_root: str
    published: tuple[str, ...]
    skipped_identical: tuple[str, ...]
    conflicts: tuple[str, ...]
    refused: tuple[str, ...]
    manifest_sha256: str
    journal_path: str
    rolled_back: bool = False
    resumed: bool = False


# ---------------------------------------------------------------------------
# content-base self-consistency
# ---------------------------------------------------------------------------

#: Publishing flattens ``deliverables/``: the published tree root IS the old
#: ``deliverables/`` directory.  A path written into a delivered document that
#: still carries this prefix therefore points at nothing, and every consumer --
#: the acceptance harness included -- fails on it.  Measured live (fix9): the
#: README said ``python deliverables/app/server.py`` while the tree held
#: ``app/server.py``, so the service could not be started from the published
#: tree at all (``can't open file .../published/app/deliverables/app/server.py``).
CONTENT_BASE_TOKEN = "deliverables/"

_CONTENT_DOC_SUFFIXES = (".md", ".txt", ".rst")
_CONTENT_DOC_MAX_BYTES = 262144


def content_base_mismatches(
    published_dir: Path,
    rels: Iterable[Any],
    *,
    max_files: int = 40,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Delivered documents that still speak the pre-publish path base.

    Read-only and bounded: shallow text documents only, the literal token only,
    and the excerpt is a single trimmed line -- never file contents in bulk.

    This is a **diagnostic, never a gate**: refusing here would turn a
    documentation defect into a delivery deadlock, which is the failure mode
    the 2026-10-07 simplification removed.  The authoritative check remains the
    independent acceptance harness, which actually tries to start the service.
    """
    published_dir = Path(published_dir)
    findings: list[dict[str, Any]] = []
    considered = 0
    for raw in sorted(str(rel).replace("\\", "/") for rel in (rels or ())):
        if not raw.lower().endswith(_CONTENT_DOC_SUFFIXES):
            continue
        if raw.count("/") > 2:            # shallow documents only
            continue
        if considered >= max_files:
            break
        considered += 1
        path = published_dir / raw
        try:
            if not path.is_file() or path.stat().st_size > _CONTENT_DOC_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if CONTENT_BASE_TOKEN in line:
                findings.append({
                    "rel": raw,
                    "line": number,
                    "excerpt": line.strip()[:160],
                })
                break
        if len(findings) >= limit:
            break
    return findings


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _manifest_digest(files: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(files), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _journal_files_digest(files: Mapping[str, Any]) -> str:
    """Digest of a journal's files block reduced to the manifest projection.

    Journal entries carry mutable bookkeeping (``state``, ``identical``,
    ``pre_existing``); only the immutable hash/size pair belongs to the
    manifest identity that ``manifest_sha256`` records.
    """
    projection: dict[str, dict[str, Any]] = {}
    for rel, meta in files.items():
        if isinstance(meta, Mapping):
            projection[rel] = {"sha256": meta.get("sha256"), "bytes": meta.get("bytes")}
        else:
            projection[rel] = {"sha256": None, "bytes": None}
    return _manifest_digest(projection)


def _is_ignored(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in _IGNORED_DIR_NAMES for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name in _IGNORED_FILE_NAMES:
        return True
    return name.endswith(_IGNORED_SUFFIXES)


def _is_hex64(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value.lower())


def _rel_problem(rel: Any) -> str | None:
    """Return a reason string when ``rel`` is not an acceptable relative path."""
    if not isinstance(rel, str) or rel == "":
        return "rel must be a non-empty string"
    if rel == ".":
        return f"rel must not be '.': {rel!r}"
    normalized = rel.replace("\\", "/")
    if normalized.startswith("/") or Path(normalized).is_absolute():
        return f"rel must be relative: {rel!r}"
    parts = normalized.split("/")
    if ".." in parts:
        return f"rel must not contain '..': {rel!r}"
    return None


def _delivery_root(workspace_root: Path, delivery_id: str) -> Path:
    return Path(workspace_root) / DELIVERY_DIRNAME / delivery_id


def _journal_path(workspace_root: Path, delivery_id: str) -> Path:
    return Path(workspace_root) / DELIVERY_DIRNAME / f"{JOURNAL_PREFIX}{delivery_id}.json"


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.astralnexus-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return data


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def build_manifest(root: Path) -> dict[str, Any]:
    """Scan ``root`` recursively and hash every deliverable regular file."""
    root = Path(root)
    files: dict[str, dict[str, Any]] = {}
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIR_NAMES)
        for filename in sorted(filenames):
            if filename in _IGNORED_FILE_NAMES or filename.endswith(_IGNORED_SUFFIXES):
                continue
            full = Path(current) / filename
            if not full.is_file() or full.is_symlink():
                continue
            rel = full.relative_to(root).as_posix()
            rel = rel.replace("\\", "/")
            if _is_ignored(rel):
                continue
            files[rel] = {"sha256": _sha256_file(full), "bytes": full.stat().st_size}
    return {FILES_FIELD: files, MANIFEST_SHA256_FIELD: _manifest_digest(files)}


def validate_manifest(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a tuple of reasons the manifest is invalid; empty means valid."""
    reasons: list[str] = []
    if not isinstance(manifest, Mapping):
        return ("manifest must be a mapping",)
    files = manifest.get(FILES_FIELD)
    if not isinstance(files, Mapping):
        return (f"'{FILES_FIELD}' must be a mapping of rel -> metadata",)
    if not files:
        reasons.append("manifest contains no files")
    for rel in sorted(files.keys(), key=str):
        meta = files[rel]
        problem = _rel_problem(rel)
        if problem is not None:
            reasons.append(problem)
            continue
        if not isinstance(meta, Mapping):
            reasons.append(f"{rel}: entry must be a mapping")
            continue
        digest = meta.get("sha256")
        if not _is_hex64(digest):
            reasons.append(f"{rel}: sha256 must be 64 hex characters")
        size = meta.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            reasons.append(f"{rel}: bytes must be a non-negative int")
    expected = manifest.get(MANIFEST_SHA256_FIELD)
    if expected is not None and isinstance(files, Mapping):
        actual = _manifest_digest(files)
        if expected != actual:
            reasons.append(
                f"{MANIFEST_SHA256_FIELD} does not match the files block "
                f"(expected {actual}, got {expected!r})"
            )
    return tuple(reasons)


# ---------------------------------------------------------------------------
# low-level file movement (single monkeypatchable names)
# ---------------------------------------------------------------------------


def _stage_file(entry: PublishEntry, staging_root: Path) -> Path:
    """Copy one source file into the staging tree and return the copy path."""
    staged = Path(staging_root) / entry.rel
    staged.parent.mkdir(parents=True, exist_ok=True)
    tmp = staged.parent / f".{staged.name}.astralnexus-tmp"
    shutil.copyfile(entry.source, tmp)
    os.replace(tmp, staged)
    return staged


def _publish_file(staged: Path, target: Path) -> None:
    """Atomically move a staged file into its destination.

    The bytes are first copied to ``<target_dir>/.<name>.astralnexus-tmp``; the
    temporary copy is re-hashed against the staged file and only then moved
    into place with ``os.replace`` so a target never holds a half-written file.
    """
    staged = Path(staged)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.astralnexus-tmp"
    if tmp.exists():
        tmp.unlink()
    shutil.copyfile(staged, tmp)
    expected = _sha256_file(staged)
    if _sha256_file(tmp) != expected:
        _remove_quietly(tmp)
        raise OSError(f"temporary copy does not match staged bytes: {target}")
    os.replace(tmp, target)


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def _journal_payload(
    *,
    delivery_id: str,
    run_dir: str,
    workspace_root: str,
    session: str,
    turn: str,
    run: str,
    manifest_sha256: str,
    files: Mapping[str, Any],
    phase: str,
) -> dict[str, Any]:
    return {
        "delivery_id": delivery_id,
        "run_dir": run_dir,
        "workspace_root": workspace_root,
        "session": session,
        "turn": turn,
        "run": run,
        MANIFEST_SHA256_FIELD: manifest_sha256,
        "files": {rel: dict(meta) for rel, meta in files.items()},
        "phase": phase,
        "updated_at": _now_iso(),
    }


def _load_entries(
    *,
    run_dir: Path,
    files: Mapping[str, Any],
    source_root: Path | None = None,
) -> list[PublishEntry]:
    entries: list[PublishEntry] = []
    for rel in sorted(files.keys()):
        meta = files[rel]
        entries.append(
            PublishEntry(
                source=(source_root or Path(run_dir) / "deliverables") / Path(rel),
                rel=rel,
                sha256=str(meta["sha256"]),
                bytes=int(meta["bytes"]),
            )
        )
    return entries


def _verify_entries(entries: list[PublishEntry]) -> list[str]:
    """Recompute source hashes/sizes; return one reason per bad entry."""
    refused: list[str] = []
    for entry in entries:
        source = entry.source
        if source.is_symlink():
            refused.append(f"source is a symlink: {entry.rel}")
            continue
        if not source.is_file():
            refused.append(f"source missing: {entry.rel}")
            continue
        if source.stat().st_size != entry.bytes:
            refused.append(f"bytes mismatch: {entry.rel}")
            continue
        if _sha256_file(source) != entry.sha256:
            refused.append(f"sha256 mismatch: {entry.rel}")
    return refused


def _rollback(
    *,
    journal_file: Path,
    journal: dict[str, Any],
    files_state: dict[str, dict[str, Any]],
    published_dir: Path,
    staging_dir: Path,
    backup_dir: Path | None = None,
) -> None:
    for rel, meta in files_state.items():
        if meta.get("state") == _STATE_PUBLISHED:
            if meta.get("pre_existing") and backup_dir is not None:
                backup = Path(backup_dir) / rel
                if backup.is_file():
                    os.replace(backup, Path(published_dir) / rel)
                    continue
            _remove_quietly(Path(published_dir) / rel)
        if meta.get("state") == _STATE_STAGED:
            _remove_quietly(Path(staging_dir) / rel)
    shutil.rmtree(staging_dir, ignore_errors=True)
    if backup_dir is not None:
        shutil.rmtree(backup_dir, ignore_errors=True)
    journal["files"] = files_state
    journal["phase"] = _PHASE_ROLLED_BACK
    journal["updated_at"] = _now_iso()
    _write_json_atomic(journal_file, journal)


def _result_from_journal(
    *,
    journal: dict[str, Any],
    delivery_id: str,
    run_dir: str,
    workspace_root: str,
    published_dir: Path,
    rolled_back: bool,
    resumed: bool,
) -> PublishResult:
    files_state = journal.get("files", {})
    published = tuple(
        rel
        for rel, meta in sorted(files_state.items())
        if isinstance(meta, Mapping) and meta.get("state") == _STATE_PUBLISHED
    )
    skipped = tuple(
        rel
        for rel, meta in sorted(files_state.items())
        if isinstance(meta, Mapping) and meta.get("state") == _STATE_STAGED and meta.get("identical")
    )
    conflicts = tuple(
        rel
        for rel, meta in sorted(files_state.items())
        if isinstance(meta, Mapping) and meta.get("conflict")
    )
    refused = tuple(journal.get("refused", ()))
    return PublishResult(
        delivery_id=delivery_id,
        run_dir=run_dir,
        workspace_root=workspace_root,
        published_root=str(published_dir),
        published=published,
        skipped_identical=skipped,
        conflicts=conflicts,
        refused=refused,
        manifest_sha256=str(journal.get(MANIFEST_SHA256_FIELD, "")),
        journal_path=str(_journal_path(Path(workspace_root), delivery_id)),
        rolled_back=rolled_back,
        resumed=resumed,
    )


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def _empty_result(
    *,
    delivery_id: str,
    run_dir: Path,
    workspace_root: Path,
    refused: tuple[str, ...],
    conflicts: tuple[str, ...],
    manifest_sha256: str,
) -> PublishResult:
    return PublishResult(
        delivery_id=delivery_id,
        run_dir=str(run_dir),
        workspace_root=str(workspace_root),
        published_root=str(_delivery_root(workspace_root, delivery_id) / "published"),
        published=(),
        skipped_identical=(),
        conflicts=conflicts,
        refused=refused,
        manifest_sha256=manifest_sha256,
        journal_path=str(_journal_path(workspace_root, delivery_id)),
    )


def publish_deliverables(
    *,
    run_dir: Path,
    workspace_root: Path,
    session: str = "",
    turn: str = "",
    run: str = "",
    delivery_id: str = "",
    manifest: Mapping[str, Any] | None = None,
    overwrite_on_conflict: bool = False,
    source_root: Path | None = None,
) -> PublishResult:
    """Validate, stage, and atomically publish one run's deliverables."""
    run_dir = Path(run_dir)
    workspace_root = Path(workspace_root)
    source_root = (source_root or run_dir / "deliverables").resolve()
    if not source_root.is_relative_to((run_dir / "deliverables").resolve()):
        raise ValueError("Publication source must belong to this run")
    if not delivery_id:
        delivery_id = f"d{int(time.time() * 1000)}"

    if manifest is None:
        manifest = build_manifest(source_root)

    reasons = list(validate_manifest(manifest))
    files = manifest.get(FILES_FIELD)
    if isinstance(files, Mapping) and not files:
        raise ValueError("manifest contains no deliverable files; nothing to publish")
    if not isinstance(files, Mapping):
        if not reasons:
            reasons.append(f"'{FILES_FIELD}' must be a mapping of rel -> metadata")
        return _empty_result(
            delivery_id=delivery_id,
            run_dir=run_dir,
            workspace_root=workspace_root,
            refused=tuple(reasons),
            conflicts=(),
            manifest_sha256=str(manifest.get(MANIFEST_SHA256_FIELD, "")),
        )
    if reasons:
        return _empty_result(
            delivery_id=delivery_id,
            run_dir=run_dir,
            workspace_root=workspace_root,
            refused=tuple(reasons),
            conflicts=(),
            manifest_sha256=str(manifest.get(MANIFEST_SHA256_FIELD, "")),
        )

    manifest_sha256 = str(manifest.get(MANIFEST_SHA256_FIELD, _manifest_digest(files)))
    entries = _load_entries(run_dir=run_dir, files=files, source_root=source_root)

    refused = _verify_entries(entries)
    if refused:
        return _empty_result(
            delivery_id=delivery_id,
            run_dir=run_dir,
            workspace_root=workspace_root,
            refused=tuple(refused),
            conflicts=(),
            manifest_sha256=manifest_sha256,
        )

    delivery_root = _delivery_root(workspace_root, delivery_id)
    staging_dir = delivery_root / "staging"
    published_dir = delivery_root / "published"
    backup_dir = delivery_root / "backup"
    journal_file = _journal_path(workspace_root, delivery_id)
    staging_dir.mkdir(parents=True, exist_ok=True)
    published_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    files_state: dict[str, dict[str, Any]] = {
        rel: {
            "sha256": str(files[rel]["sha256"]),
            "bytes": int(files[rel]["bytes"]),
            "state": _STATE_PENDING,
        }
        for rel in sorted(files.keys())
    }
    journal = _journal_payload(
        delivery_id=delivery_id,
        run_dir=str(run_dir),
        workspace_root=str(workspace_root),
        session=session,
        turn=turn,
        run=run,
        manifest_sha256=manifest_sha256,
        files=files_state,
        phase=_PHASE_STAGING,
    )
    journal["source_root"] = str(source_root)
    _write_json_atomic(journal_file, journal)

    conflicts: list[str] = []
    skipped: list[str] = []
    try:
        # phase 1: stage, decide collisions.
        for entry in entries:
            target = published_dir / entry.rel
            if target.exists() and not target.is_symlink():
                if _sha256_file(target) == entry.sha256:
                    files_state[entry.rel]["state"] = _STATE_STAGED
                    files_state[entry.rel]["identical"] = True
                    skipped.append(entry.rel)
                    journal["files"] = files_state
                    journal["phase"] = _PHASE_STAGING
                    journal["updated_at"] = _now_iso()
                    _write_json_atomic(journal_file, journal)
                    continue
                if not overwrite_on_conflict:
                    conflicts.append(entry.rel)
                    files_state[entry.rel]["conflict"] = True
            staged = _stage_file(entry, staging_dir)
            if _sha256_file(staged) != entry.sha256 or staged.stat().st_size != entry.bytes:
                raise ValueError(f"staged bytes do not match manifest: {entry.rel}")
            files_state[entry.rel]["state"] = _STATE_STAGED
            journal["files"] = files_state
            journal["phase"] = _PHASE_STAGING
            journal["updated_at"] = _now_iso()
            _write_json_atomic(journal_file, journal)

        if conflicts:
            # whole-order refusal: nothing was published yet.
            journal["files"] = files_state
            journal["conflicts"] = conflicts
            journal["phase"] = _PHASE_ROLLED_BACK
            journal["updated_at"] = _now_iso()
            _write_json_atomic(journal_file, journal)
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(backup_dir, ignore_errors=True)
            return PublishResult(
                delivery_id=delivery_id,
                run_dir=str(run_dir),
                workspace_root=str(workspace_root),
                published_root=str(published_dir),
                published=(),
                skipped_identical=tuple(skipped),
                conflicts=tuple(conflicts),
                refused=(),
                manifest_sha256=manifest_sha256,
                journal_path=str(journal_file),
            )

        # phase 2: publish each staged file atomically.
        journal["phase"] = _PHASE_PUBLISHING
        journal["updated_at"] = _now_iso()
        _write_json_atomic(journal_file, journal)

        published: list[str] = []
        for entry in entries:
            if files_state[entry.rel].get("identical"):
                continue
            staged = staging_dir / entry.rel
            target = published_dir / entry.rel
            if target.is_file():
                # explicit overwrite: keep the previous bytes so a later
                # failure can still restore the user's file.
                backup = backup_dir / entry.rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                _publish_file(target, backup)
                files_state[entry.rel]["pre_existing"] = True
            _publish_file(staged, target)
            files_state[entry.rel]["state"] = _STATE_PUBLISHED
            published.append(entry.rel)
            journal["files"] = files_state
            journal["updated_at"] = _now_iso()
            _write_json_atomic(journal_file, journal)
    except BaseException:
        _rollback(
            journal_file=journal_file,
            journal=journal,
            files_state=files_state,
            published_dir=published_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
        )
        raise

    journal["files"] = files_state
    journal["phase"] = _PHASE_DONE
    journal["updated_at"] = _now_iso()
    _write_json_atomic(journal_file, journal)
    shutil.rmtree(staging_dir, ignore_errors=True)
    shutil.rmtree(backup_dir, ignore_errors=True)
    _write_binding(
        published_dir=published_dir,
        delivery_id=delivery_id,
        run_dir=run_dir,
        session=session,
        turn=turn,
        run=run,
        manifest_sha256=manifest_sha256,
        files=[rel for rel in sorted(files.keys())],
        source_root=source_root,
    )
    return PublishResult(
        delivery_id=delivery_id,
        run_dir=str(run_dir),
        workspace_root=str(workspace_root),
        published_root=str(published_dir),
        published=tuple(published),
        skipped_identical=tuple(skipped),
        conflicts=(),
        refused=(),
        manifest_sha256=manifest_sha256,
        journal_path=str(journal_file),
    )


def _write_binding(
    *,
    published_dir: Path,
    delivery_id: str,
    run_dir: Path,
    session: str,
    turn: str,
    run: str,
    manifest_sha256: str,
    files: list[str],
    source_root: Path | None = None,
) -> Path:
    binding_path = Path(published_dir) / BINDING_NAME
    _write_json_atomic(
        binding_path,
        {
            "delivery_id": delivery_id,
            "run_dir": str(run_dir),
            "session": session,
            "turn": turn,
            "run": run,
            MANIFEST_SHA256_FIELD: manifest_sha256,
            "files": files,
            "path_bases": {
                "run": str(Path(run_dir).resolve()),
                "source": str((source_root or Path(run_dir) / "deliverables").resolve()),
                "published": str(Path(published_dir).resolve()),
                "workspace": str(Path(published_dir).resolve().parents[2]),
                "file_keys_relative_to": "source_and_published",
            },
            "published_at": _now_iso(),
        },
    )
    return binding_path


def recover_pending(*, workspace_root: Path, delivery_id: str) -> PublishResult:
    """Resume or roll back an interrupted publish described by its journal."""
    workspace_root = Path(workspace_root)
    journal_file = _journal_path(workspace_root, delivery_id)
    if not journal_file.is_file():
        raise ValueError(f"no journal for delivery {delivery_id!r} at {journal_file}")

    journal = _read_json(journal_file)
    run_dir = Path(str(journal.get("run_dir", "")))
    delivery_root = _delivery_root(workspace_root, delivery_id)
    staging_dir = delivery_root / "staging"
    published_dir = delivery_root / "published"
    backup_dir = delivery_root / "backup"
    files_state = journal.get("files")
    if not isinstance(files_state, dict):
        raise ValueError(f"journal for {delivery_id!r} has no usable 'files' block")

    recorded_manifest_sha256 = str(journal.get(MANIFEST_SHA256_FIELD, ""))
    expected_manifest_sha256 = _journal_files_digest(files_state)
    if (
        recorded_manifest_sha256
        and recorded_manifest_sha256 != expected_manifest_sha256
    ):
        raise ValueError(
            f"journal manifest_sha256 {recorded_manifest_sha256!r} does not match "
            f"its files block ({expected_manifest_sha256})"
        )
    manifest_sha256 = recorded_manifest_sha256 or expected_manifest_sha256

    phase = journal.get("phase")
    if phase == _PHASE_DONE:
        result = _result_from_journal(
            journal=journal,
            delivery_id=delivery_id,
            run_dir=str(run_dir),
            workspace_root=str(workspace_root),
            published_dir=published_dir,
            rolled_back=False,
            resumed=True,
        )
        return PublishResult(
            delivery_id=result.delivery_id,
            run_dir=result.run_dir,
            workspace_root=result.workspace_root,
            published_root=result.published_root,
            published=(),
            skipped_identical=result.skipped_identical,
            conflicts=result.conflicts,
            refused=result.refused,
            manifest_sha256=result.manifest_sha256,
            journal_path=result.journal_path,
            rolled_back=False,
            resumed=True,
        )
    if phase == _PHASE_ROLLED_BACK:
        return _result_from_journal(
            journal=journal,
            delivery_id=delivery_id,
            run_dir=str(run_dir),
            workspace_root=str(workspace_root),
            published_dir=published_dir,
            rolled_back=True,
            resumed=True,
        )

    # staging / publishing: verify what survives, then continue or roll back.
    bad: list[str] = []
    restage: dict[str, Path] = {}
    for rel, meta in sorted(files_state.items()):
        if not isinstance(meta, Mapping):
            bad.append(f"journal entry for {rel!r} is not a mapping")
            continue
        state = meta.get("state")
        if state == _STATE_PUBLISHED:
            target = published_dir / rel
            if not target.is_file():
                bad.append(f"published file missing: {rel}")
            elif _sha256_file(target) != meta.get("sha256"):
                bad.append(f"published bytes changed: {rel}")
            continue
        staged = staging_dir / rel
        if not staged.is_file():
            if state == _STATE_STAGED:
                bad.append(f"staged file missing: {rel}")
            elif state == _STATE_PENDING:
                source = Path(journal.get("source_root") or run_dir / "deliverables") / Path(rel)
                if not source.is_file() or source.is_symlink():
                    bad.append(f"source missing for pending entry: {rel}")
                elif _sha256_file(source) != meta.get("sha256"):
                    bad.append(f"source sha256 mismatch: {rel}")
                elif source.stat().st_size != meta.get("bytes"):
                    bad.append(f"source bytes mismatch: {rel}")
                else:
                    restage[rel] = source
            continue
        if _sha256_file(staged) != meta.get("sha256"):
            bad.append(f"staged sha256 mismatch: {rel}")
        elif staged.stat().st_size != meta.get("bytes"):
            bad.append(f"staged bytes mismatch: {rel}")

    if bad:
        _rollback(
            journal_file=journal_file,
            journal=journal,
            files_state=files_state,
            published_dir=published_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
        )
        result = _result_from_journal(
            journal=journal,
            delivery_id=delivery_id,
            run_dir=str(run_dir),
            workspace_root=str(workspace_root),
            published_dir=published_dir,
            rolled_back=True,
            resumed=True,
        )
        return PublishResult(
            delivery_id=result.delivery_id,
            run_dir=result.run_dir,
            workspace_root=result.workspace_root,
            published_root=result.published_root,
            published=(),
            skipped_identical=result.skipped_identical,
            conflicts=result.conflicts,
            refused=tuple(bad),
            manifest_sha256=result.manifest_sha256,
            journal_path=result.journal_path,
            rolled_back=True,
            resumed=True,
        )

    published_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    journal["phase"] = _PHASE_PUBLISHING
    journal["updated_at"] = _now_iso()
    _write_json_atomic(journal_file, journal)

    published: list[str] = []
    try:
        for rel, source in sorted(restage.items()):
            meta = files_state[rel]
            entry = PublishEntry(
                source=source,
                rel=rel,
                sha256=str(meta["sha256"]),
                bytes=int(meta["bytes"]),
            )
            staged = _stage_file(entry, staging_dir)
            if _sha256_file(staged) != entry.sha256:
                raise ValueError(f"restaged bytes do not match journal: {rel}")
            meta["state"] = _STATE_STAGED
            journal["files"] = files_state
            journal["updated_at"] = _now_iso()
            _write_json_atomic(journal_file, journal)
        for rel, meta in sorted(files_state.items()):
            if meta.get("state") == _STATE_PUBLISHED:
                continue
            if meta.get("identical"):
                meta["state"] = _STATE_STAGED
                continue
            staged = staging_dir / rel
            target = published_dir / rel
            if target.is_file():
                backup = backup_dir / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                _publish_file(target, backup)
                meta["pre_existing"] = True
            _publish_file(staged, target)
            meta["state"] = _STATE_PUBLISHED
            published.append(rel)
            journal["files"] = files_state
            journal["updated_at"] = _now_iso()
            _write_json_atomic(journal_file, journal)
    except BaseException:
        _rollback(
            journal_file=journal_file,
            journal=journal,
            files_state=files_state,
            published_dir=published_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
        )
        raise

    journal["files"] = files_state
    journal["phase"] = _PHASE_DONE
    journal["updated_at"] = _now_iso()
    _write_json_atomic(journal_file, journal)
    shutil.rmtree(staging_dir, ignore_errors=True)
    shutil.rmtree(backup_dir, ignore_errors=True)
    _write_binding(
        published_dir=published_dir,
        delivery_id=delivery_id,
        run_dir=run_dir,
        session=str(journal.get("session", "")),
        turn=str(journal.get("turn", "")),
        run=str(journal.get("run", "")),
        manifest_sha256=manifest_sha256,
        files=[rel for rel in sorted(files_state.keys())],
        source_root=Path(journal.get("source_root") or run_dir / "deliverables"),
    )
    return PublishResult(
        delivery_id=delivery_id,
        run_dir=str(run_dir),
        workspace_root=str(workspace_root),
        published_root=str(published_dir),
        published=tuple(published),
        skipped_identical=(),
        conflicts=(),
        refused=(),
        manifest_sha256=manifest_sha256,
        journal_path=str(journal_file),
        rolled_back=False,
        resumed=True,
    )


def read_binding(workspace_root: Path, delivery_id: str) -> dict[str, Any]:
    """Read back the binding file written by a successful publish."""
    path = _delivery_root(Path(workspace_root), delivery_id) / "published" / BINDING_NAME
    return _read_json(path)
