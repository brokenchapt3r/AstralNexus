"""Per-write change facts: what a ``file_write`` requested and what landed.

User requirement (m02366 ③): the durable receipt of a write must answer, by
itself, which operation touched which path, the before/after content hash and
byte count, whether the bytes on disk actually changed, whether the requested
bytes are the landed bytes, and that a *failed* write modified nothing.

The requested content itself is never recorded -- only its byte count and
SHA-256 -- so a receipt stays small, quotable, and free of generated code.
These are observations about bytes on disk, never task acceptance: ``changed``
says a file changed, never that the work passed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

#: ``file_write`` modes understood by the tool schema (``tools/builtin_tools.py``).
WRITE_MODES = ("overwrite", "append", "replace")
DEFAULT_WRITE_MODE = "overwrite"
CONTENT_KEYS = ("content", "text", "data")
PATH_KEYS = ("path", "file", "file_path", "target")


def _as_mapping(arguments: Any) -> Dict[str, Any]:
    """Accept a mapping or the JSON string form the model sends."""
    if isinstance(arguments, dict):
        return arguments
    text = str(arguments or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_int(value: Any) -> int:
    """Byte counts are ints; anything else is treated as absent, never guessed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def request_facts(arguments: Any) -> Dict[str, Any]:
    """Facts about the *requested* write; never records the content itself."""
    payload = _as_mapping(arguments)
    if not payload:
        return {"parsable": False, "content_recorded": False}
    path = ""
    for key in PATH_KEYS:
        candidate = str(payload.get(key) or "").strip()
        if candidate:
            path = candidate
            break
    mode = str(payload.get("mode") or "").strip().lower() or DEFAULT_WRITE_MODE
    if mode not in WRITE_MODES:
        mode = DEFAULT_WRITE_MODE
    facts: Dict[str, Any] = {
        "parsable": True,
        "operation": mode,
        "path": path,
        "content_recorded": False,
    }
    old_text = payload.get("old_text")
    if isinstance(old_text, str):
        facts["replace_old_text_bytes"] = len(old_text.encode("utf-8"))
    expected_hash = payload.get("expected_sha256")
    if isinstance(expected_hash, str) and expected_hash:
        facts["expected_previous_sha256"] = expected_hash
    expected_bytes = payload.get("expected_bytes")
    if isinstance(expected_bytes, int) and type(expected_bytes) is int:
        facts["expected_previous_bytes"] = expected_bytes
    for key in CONTENT_KEYS:
        raw_content = payload.get(key)
        if isinstance(raw_content, str):
            encoded = raw_content.encode("utf-8")
            facts["request_sha256"] = hashlib.sha256(encoded).hexdigest()
            facts["request_bytes"] = len(encoded)
            break
    return facts


def inventory_version(inventory: Any, *candidates: str) -> Dict[str, Any]:
    """Resolve one path's ``snapshot_files`` version entry (hash and bytes)."""
    if not isinstance(inventory, dict):
        return {}
    for candidate in candidates:
        if not candidate:
            continue
        entry = inventory.get(candidate)
        if not isinstance(entry, dict):
            continue
        version: Dict[str, Any] = {}
        sha = str(entry.get("sha256") or "")
        if sha:
            version["sha256"] = sha
        if entry.get("bytes") is not None:
            version["bytes"] = entry.get("bytes")
        if version:
            return version
    return {}


def relative_receipt_path(raw_path: Any, root: Any) -> str:
    """Run-relative posix path, the key shape used by ``snapshot_files``.

    Fix S1 (b32_path_identity): the run-relative identity comes from the shared
    parser instead of a second ``Path.resolve()``/``relative_to`` implementation
    here, so a receipt path and a declared path name the same file identically.
    """
    from AstralNexus.core.output_paths import workspace_relative

    text = str(raw_path or "").strip()
    if not text:
        return ""
    return workspace_relative(root, text)


def write_receipt_facts(
    arguments: Any,
    before: Any,
    after: Any,
    *,
    relative_path: str = "",
    path: str = "",
    status: str = "",
    returncode: Optional[int] = None,
) -> Dict[str, Any]:
    """Combine request facts and inventory versions into one write record.

    Args:
        arguments: the ``function.arguments`` of the write call.
        before: the inventory observed immediately before the write.
        after: the inventory observed immediately after the write.
        relative_path: preferred key when resolving both inventories.
        path: the absolute path reported by the write result.
        status: the write receipt status (``success`` / ``error`` / ...).
        returncode: the write receipt returncode, if any.

    Returns:
        A JSON-serializable record: operation, path, request hash/bytes,
        previous and landed hash/bytes, ``changed``, whether the landed bytes
        equal the requested bytes, append size consistency, and
        ``failed_write_modified`` -- True only when a non-successful write
        nonetheless changed bytes on disk.
    """
    request = request_facts(arguments)
    candidates: List[str] = [
        relative_path,
        path,
        str(request.get("path") or ""),
    ]
    previous = inventory_version(before, *candidates)
    landed = inventory_version(after, *candidates)

    # A change is decidable whenever the inventory baseline is known: a path
    # absent from a known baseline and present after the write was added, and
    # one that disappeared was removed.  Only a missing baseline (the first
    # receipt of an execution) leaves it open, because the file may have
    # existed before the execution started.
    change_kind = "unknown"
    changed: Optional[bool] = None
    if previous and landed:
        if (previous.get("sha256") != landed.get("sha256")
                or previous.get("bytes") != landed.get("bytes")):
            change_kind, changed = "modified", True
        else:
            change_kind, changed = "unchanged", False
    elif landed and not previous and before:
        change_kind, changed = "added", True
    elif previous and not landed:
        change_kind, changed = "removed", True

    operation = str(request.get("operation") or "")
    if not operation:
        # An unparsable request must never claim an operation it cannot prove
        # (a replayed receipt with ``arguments=None`` is exactly that case).
        operation = DEFAULT_WRITE_MODE if request.get("parsable") else "unknown"
    request_sha = str(request.get("request_sha256") or "")
    landed_sha = str(landed.get("sha256") or "")
    landed_matches_request: Optional[bool] = None
    if operation == "overwrite" and request_sha and landed_sha:
        landed_matches_request = request_sha == landed_sha

    landed_bytes_consistent: Optional[bool] = None
    request_bytes = request.get("request_bytes")
    previous_bytes = previous.get("bytes")
    landed_bytes = landed.get("bytes")
    if operation == "append" and None not in (request_bytes, previous_bytes, landed_bytes):
        landed_bytes_consistent = (
            _as_int(landed_bytes) == _as_int(previous_bytes) + _as_int(request_bytes)
        )

    payload: Dict[str, Any] = dict(request)
    payload.update({
        "previous_sha256": str(previous.get("sha256") or ""),
        "previous_bytes": previous_bytes,
        "landed_sha256": landed_sha,
        "landed_bytes": landed_bytes,
        "changed": changed,
        "change_kind": change_kind,
        "operation": operation,
        "landed_matches_request": landed_matches_request,
        "landed_bytes_consistent": landed_bytes_consistent,
        "failed_write_modified": bool(status and status != "success" and changed is True),
        "status": str(status or ""),
        "returncode": returncode,
    })
    return payload