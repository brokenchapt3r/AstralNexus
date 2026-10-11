"""Offline, deterministic acceptance for an existing delivery candidate.

The live delivery pipeline is deliberately not imported here.  Rechecking an
old run must not call an LLM, mutate the task graph, repair files, or publish a
new version.  This module reads the frozen contract and the exact candidate
tree, then emits an auditable verdict.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from AstralNexus.core.delivery_snapshot import EXCLUDE_PARTS, EXCLUDE_SUFFIXES, digest
from AstralNexus.core.output_paths import (
    PathIdentityError,
    canonical_relative,
    declared_output_is_required,
    normalize_worker_output_path,
)
from AstralNexus.runtime.acceptance_checks import run_acceptance_checks


POLICY_VERSION = "strict-offline-v1"
_PUBLISHED_VERSION_RE = re.compile(r"^v([1-9][0-9]*)$")
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_CITATION_RE = re.compile(
    r"(?:https?://\S+|\[[^\]]+\]\([^)]+\)|\[[0-9]+\]|(?:^|\s)\[@[^\]]+\])",
    re.MULTILINE,
)
_FRAMEWORK_FILES = {
    "acceptance_report.md",
    "delivery_manifest.json",
    "delivery_path_manifest.md",
    "delivery_response.md",
    "delivery_summary.json",
    "delivery_verification.json",
    "summary.json",
}
_FILE_OUTPUT_TYPES = {"file", "files", "software", "data"}


@dataclass(frozen=True)
class DeliveryCandidate:
    root: Path
    kind: str
    version: Optional[int] = None


def _files_below(root: Path, *, unversioned: bool = False) -> list[Path]:
    files: list[Path] = []
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if set(relative.parts) & EXCLUDE_PARTS or path.name.endswith(EXCLUDE_SUFFIXES):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise PermissionError(
                "delivery candidate contains a link or escaping path: "
                + relative.as_posix()
            )
        if unversioned and relative.parts:
            first = relative.parts[0]
            if _PUBLISHED_VERSION_RE.fullmatch(first) or first == "current":
                continue
            if len(relative.parts) == 1 and first == "current.json":
                continue
        if path.is_file():
            files.append(path)
    return files


def resolve_delivery_candidate(
    run_dir: Path, candidate: Optional[Path | str] = None,
) -> DeliveryCandidate:
    """Resolve the exact tree whose bytes are being accepted."""
    run_dir = Path(run_dir).expanduser().resolve()
    if candidate is not None:
        root = Path(candidate).expanduser()
        if not root.is_absolute():
            root = run_dir / root
        root = root.resolve()
        if root.is_file():
            root = root.parent
        if not root.is_dir():
            raise FileNotFoundError(f"delivery candidate does not exist: {root}")
        match = _PUBLISHED_VERSION_RE.fullmatch(root.name)
        return DeliveryCandidate(
            root=root,
            kind="published" if match else "explicit",
            version=int(match.group(1)) if match else None,
        )

    staging = run_dir / ".delivery_staging"
    if _files_below(staging):
        return DeliveryCandidate(staging.resolve(), "staging")

    deliverables = run_dir / "deliverables"
    pointer = deliverables / "current.json"
    if pointer.is_file():
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            relative = str(payload.get("path") or "").strip()
            target = (deliverables / relative).resolve()
            target.relative_to(deliverables.resolve())
            match = _PUBLISHED_VERSION_RE.fullmatch(target.name)
            if match and target.is_dir():
                return DeliveryCandidate(target, "published", int(match.group(1)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    versions: list[tuple[int, Path]] = []
    if deliverables.is_dir():
        for path in deliverables.iterdir():
            match = _PUBLISHED_VERSION_RE.fullmatch(path.name)
            if match and path.is_dir():
                versions.append((int(match.group(1)), path))
    if versions:
        version, root = max(versions, key=lambda item: item[0])
        return DeliveryCandidate(root.resolve(), "published", version)

    if _files_below(deliverables, unversioned=True):
        return DeliveryCandidate(deliverables.resolve(), "unversioned")
    raise FileNotFoundError(f"no delivery candidate found under {run_dir}")


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_delivery_contract(
    run_dir: Path, candidate: Optional[DeliveryCandidate] = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Load the strongest available frozen contract without deriving a new one."""
    run_dir = Path(run_dir).resolve()
    warnings: list[str] = []
    if candidate is not None:
        summary = _json_object(candidate.root / "delivery_summary.json")
        contract = summary.get("delivery_contract") or {}
        if isinstance(contract, dict) and contract:
            return dict(contract), "candidate/delivery_summary.json", warnings
    try:
        from AstralNexus.core.runtime_state import read_authoritative_cursor

        authoritative = read_authoritative_cursor(run_dir)
    except Exception as exc:  # an offline checker must remain usable on legacy runs
        authoritative = None
        warnings.append(f"authoritative cursor unavailable: {type(exc).__name__}: {exc}")
    if authoritative:
        if authoritative.get("source") == "db_read_failed":
            warnings.append(str(authoritative.get("warning") or "checkpoint database unreadable"))
        else:
            cursor = authoritative.get("cursor") or {}
            governance = cursor.get("governance_state") or {}
            contract = governance.get("delivery_contract") or {}
            if isinstance(contract, dict) and contract:
                warnings.extend(str(item) for item in authoritative.get("anomalies", []) or [])
                return dict(contract), str(authoritative.get("source") or "checkpoint"), warnings

    runtime_state = _json_object(run_dir / "runtime_state.json")
    contract = (runtime_state.get("governance_state") or {}).get("delivery_contract") or {}
    if isinstance(contract, dict) and contract:
        warnings.append("using runtime_state.json fallback contract")
        return dict(contract), "runtime_state.json", warnings

    roots = [candidate.root] if candidate else []
    roots.append(run_dir / "deliverables")
    for root in roots:
        summary = _json_object(root / "delivery_summary.json")
        contract = summary.get("delivery_contract") or {}
        if isinstance(contract, dict) and contract:
            warnings.append("using delivery_summary.json embedded contract")
            return dict(contract), "delivery_summary.json", warnings
    return {}, "missing", warnings


def _candidate_files(candidate: DeliveryCandidate) -> list[Path]:
    return _files_below(candidate.root, unversioned=candidate.kind == "unversioned")


def _relative_path(candidate: DeliveryCandidate, path: Path) -> str:
    return path.relative_to(candidate.root).as_posix()


def _declared_candidate_relative(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        relative = canonical_relative(text)
    except PathIdentityError:
        return ""
    normalized = normalize_worker_output_path(relative)
    if normalized.casefold().startswith("deliverables/"):
        return normalized.split("/", 1)[1]
    return normalized


def _output_path(output: dict[str, Any]) -> str:
    for key in ("artifact_path", "path", "file", "location"):
        if str(output.get(key) or "").strip():
            return _declared_candidate_relative(output[key])
    return ""


def _covered(relative: str, declarations: Iterable[tuple[str, bool]]) -> bool:
    folded = relative.casefold()
    for declared, is_tree in declarations:
        key = declared.casefold().rstrip("/")
        if folded == key or (is_tree and folded.startswith(key + "/")):
            return True
    return False


def _read_receipts(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    database = run_dir / ".astralnexus_state" / "executions.sqlite"
    if not database.is_file():
        return [], "execution receipt database missing"
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute("SELECT payload FROM receipts ORDER BY seq").fetchall()
        receipts = []
        for (payload,) in rows:
            item = json.loads(payload)
            if isinstance(item, dict):
                receipts.append(item)
        return receipts, ""
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return [], f"execution receipts unreadable: {type(exc).__name__}: {exc}"


def _candidate_reader(candidate: DeliveryCandidate, run_dir: Path):
    def read(_root: Path, relative: str):
        text = str(relative or "").replace("\\", "/").strip()
        values: list[Path] = []
        if text.casefold().startswith("deliverables/"):
            values.append(candidate.root / text.split("/", 1)[1])
        else:
            values.append(candidate.root / text)
            values.append(run_dir / text)
        for value in values:
            try:
                resolved = value.resolve()
                allowed = (
                    resolved.is_relative_to(candidate.root.resolve())
                    or resolved.is_relative_to(run_dir.resolve())
                )
                if not allowed:
                    continue
                if not resolved.exists():
                    continue
                if not resolved.is_file():
                    return None, None, "FILE_NOT_IN_SHARED_DOMAIN"
                size = resolved.stat().st_size
                return size, resolved.read_bytes() if size <= 8 * 1024 * 1024 else None, None
            except OSError:
                return None, None, "FILE_CHECK_IO_ERROR"
        return None, None, "FILE_NOT_FOUND"
    return read


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _criterion_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("criterion", "text", "description", "requirement"):
            if str(value.get(key) or "").strip():
                return str(value[key]).strip()
        return ""
    return str(value or "").strip()


def _flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "optional", "conditional"}
    return bool(value)


def _successful_execution_receipts(receipts: Iterable[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for receipt in receipts:
        status = str(receipt.get("status") or "").lower()
        returncode = receipt.get("returncode")
        if status != "success" or returncode is None:
            continue
        try:
            succeeded = int(returncode) == 0
        except (TypeError, ValueError):
            succeeded = False
        if succeeded:
            result.append(str(receipt.get("receipt_id") or receipt.get("call_id") or ""))
    return [item for item in result if item]


def evaluate_delivery(
    run_dir: Path | str,
    *,
    candidate: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Recheck a previous result.  This function has no mutation or LLM path."""
    run_root = Path(run_dir).expanduser().resolve()
    selected = resolve_delivery_candidate(run_root, candidate)
    contract, contract_source, warnings = load_delivery_contract(run_root, selected)
    checks: list[dict[str, Any]] = []

    def check(
        check_id: str,
        name: str,
        status: str,
        detail: str,
        evidence_refs: Iterable[str] = (),
    ) -> None:
        checks.append({
            "check_id": check_id,
            "name": name,
            "status": status,
            "detail": detail,
            "evidence_refs": list(evidence_refs),
        })

    files = _candidate_files(selected)
    relative_files = [_relative_path(selected, path) for path in files]
    snapshot = {
        relative: {
            "sha256": _hash_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path, relative in zip(files, relative_files)
    }
    check(
        "DG-CANDIDATE",
        "candidate_non_empty",
        "pass" if files else "fail",
        f"candidate contains {len(files)} file(s)",
        relative_files[:20],
    )
    if not contract:
        check(
            "DG-CONTRACT",
            "frozen_contract_available",
            "unknown",
            "no frozen delivery contract is available; a new contract was not invented",
        )
    else:
        check(
            "DG-CONTRACT",
            "frozen_contract_available",
            "pass",
            f"loaded from {contract_source}",
        )

    manifest = _json_object(selected.root / "delivery_manifest.json")
    if selected.kind == "published":
        manifest_errors: list[str] = []
        if not manifest:
            manifest_errors.append("delivery_manifest.json missing or unreadable")
        else:
            if manifest.get("status") != "published":
                manifest_errors.append("manifest status is not published")
            try:
                manifest_version = int(manifest.get("version") or -1)
            except (TypeError, ValueError):
                manifest_version = -1
            if selected.version is not None and manifest_version != selected.version:
                manifest_errors.append("manifest version does not match directory")
            raw_entries = manifest.get("artifacts", [])
            if not isinstance(raw_entries, list):
                raw_entries = []
                manifest_errors.append("manifest artifacts is not a list")
            entry_rows = [
                (str(item.get("path") or "").replace("\\", "/"), item)
                for item in raw_entries
                if isinstance(item, dict) and item.get("path")
            ]
            entries = dict(entry_rows)
            if len(entries) != len(entry_rows):
                manifest_errors.append("manifest contains duplicate artifact paths")
            actual = {item for item in relative_files if item != "delivery_manifest.json"}
            if actual != set(entries):
                missing = sorted(set(entries) - actual)
                unbound = sorted(actual - set(entries))
                if missing:
                    manifest_errors.append("manifest entries missing on disk: " + ", ".join(missing))
                if unbound:
                    manifest_errors.append("files absent from manifest: " + ", ".join(unbound))
            for relative, item in entries.items():
                path = (selected.root / relative).resolve()
                try:
                    path.relative_to(selected.root.resolve())
                except ValueError:
                    manifest_errors.append(f"manifest path escapes candidate: {relative}")
                    continue
                if path.is_file() and _hash_file(path) != item.get("sha256"):
                    manifest_errors.append(f"sha256 mismatch: {relative}")
            authorization = manifest.get("authorization")
            if isinstance(authorization, dict) and contract:
                expected_contract = str(authorization.get("contract_sha256") or "")
                if expected_contract and expected_contract != digest(contract):
                    manifest_errors.append("manifest authorization contract hash does not match frozen contract")
        check(
            "DG-MANIFEST",
            "published_manifest_integrity",
            "fail" if manifest_errors else "pass",
            "; ".join(manifest_errors) if manifest_errors else "manifest paths and hashes match the candidate",
            ["delivery_manifest.json"],
        )
    else:
        check(
            "DG-MANIFEST",
            "published_manifest_integrity",
            "pass",
            f"{selected.kind} candidate is checked as an immutable in-memory snapshot",
        )

    response_path = selected.root / "delivery_response.md"
    response = response_path.read_text(encoding="utf-8", errors="replace") if response_path.is_file() else ""
    summary_required = _flag(contract.get("summary_required"), False) if contract else False
    check(
        "DG-SUMMARY",
        "required_delivery_summary",
        "pass" if response.strip() or not summary_required else "fail",
        (
            "delivery_response.md is present and non-empty"
            if response.strip()
            else "summary is optional" if not summary_required
            else "contract requires a non-empty delivery_response.md"
        ),
        ["delivery_response.md"] if response_path.is_file() else [],
    )
    outputs = [item for item in contract.get("required_outputs", []) if isinstance(item, dict)] if contract else []
    declarations: list[tuple[str, bool]] = []
    for index, output in enumerate(outputs, 1):
        relative = _output_path(output)
        required = declared_output_is_required(output)
        output_type = str(output.get("type") or "other").strip().lower()
        if relative:
            candidate_path = selected.root / relative
            is_tree = candidate_path.is_dir() or output_type in {"files", "software"}
            declarations.append((relative, is_tree))
            exists = candidate_path.is_file() and candidate_path.stat().st_size > 0
            if candidate_path.is_dir():
                exists = bool(_files_below(candidate_path))
            check(
                f"DG-OUTPUT-{index:02d}",
                str(output.get("name") or output.get("output_id") or relative),
                "pass" if exists or not required else "fail",
                (
                    f"declared path {'exists' if exists else 'is absent'}: {relative}"
                    + (" (optional)" if not required else "")
                ),
                [relative] if exists else [],
            )
        elif required and output_type in _FILE_OUTPUT_TYPES:
            check(
                f"DG-OUTPUT-{index:02d}",
                str(output.get("name") or output.get("output_id") or "required output"),
                "fail",
                "required physical output has no deterministic path in the frozen contract",
            )
        elif required:
            check(
                f"DG-OUTPUT-{index:02d}",
                str(output.get("name") or output.get("output_id") or "text output"),
                "pass" if response.strip() else "fail",
                "non-empty delivery_response.md" if response.strip() else "required text output is empty",
                ["delivery_response.md"] if response.strip() else [],
            )

    if contract:
        undeclared = [
            relative for relative in relative_files
            if relative not in _FRAMEWORK_FILES and not _covered(relative, declarations)
        ]
        check(
            "DG-SCOPE",
            "published_file_scope",
            "fail" if undeclared else "pass",
            (
                "candidate contains undeclared file(s): " + ", ".join(undeclared)
                if undeclared else "every candidate file is contract-owned or framework-owned"
            ),
            undeclared[:20],
        )
    else:
        check(
            "DG-SCOPE",
            "published_file_scope",
            "unknown",
            "file scope cannot be decided without a frozen delivery contract",
        )

    headings = {match.group(1).strip() for match in _MARKDOWN_HEADING_RE.finditer(response)}
    required_sections = [str(item).strip() for item in contract.get("required_sections", []) if str(item).strip()] if contract else []
    missing_sections = [item for item in required_sections if item not in headings]
    check(
        "DG-SECTIONS",
        "required_markdown_sections",
        "fail" if missing_sections else "pass",
        "missing exact headings: " + ", ".join(missing_sections) if missing_sections else f"all {len(required_sections)} required heading(s) present",
        ["delivery_response.md"] if response_path.is_file() else [],
    )

    forbidden = [str(item) for item in contract.get("forbidden_claims", [])] if contract else []
    violations = _unsupported_forbidden_claims(response, forbidden)
    check(
        "DG-FORBIDDEN",
        "forbidden_claims",
        "fail" if violations else "pass",
        "unsupported claim(s): " + ", ".join(violations) if violations else "no unsupported forbidden marker was asserted",
        ["delivery_response.md"] if response_path.is_file() else [],
    )

    if _flag(contract.get("needs_citations"), False):
        has_citations = bool(_CITATION_RE.search(response))
        check(
            "DG-CITATIONS",
            "citation_presence",
            "pass" if has_citations else "fail",
            "citation-like references found" if has_citations else "contract requires citations but none were found",
            ["delivery_response.md"] if response_path.is_file() else [],
        )

    if _flag(contract.get("needs_artifact_refs"), False):
        artifacts = [item for item in relative_files if item not in _FRAMEWORK_FILES]
        check(
            "DG-ARTIFACT-REFS",
            "artifact_references_available",
            "pass" if artifacts else "fail",
            (
                f"candidate contains {len(artifacts)} contract-addressable artifact(s)"
                if artifacts else "contract requires artifact references but no artifact exists"
            ),
            artifacts[:20],
        )

    criteria: list[tuple[str, str]] = []
    for field, prefix in (
        ("hard_constraints", "HARD"),
        ("global_acceptance_criteria", "GLOBAL"),
        ("completion_evidence", "EVIDENCE"),
    ):
        for index, criterion in enumerate(contract.get(field, []) or [], 1):
            text = _criterion_text(criterion)
            if text:
                criteria.append((f"DG-{prefix}-{index:02d}", text))
    for output_index, output in enumerate(outputs, 1):
        if not declared_output_is_required(output):
            continue
        ids = [str(item) for item in output.get("acceptance_criterion_ids", []) or []]
        for criterion_index, criterion in enumerate(output.get("acceptance_criteria", []) or [], 1):
            text = _criterion_text(criterion)
            if text:
                criterion_id = ids[criterion_index - 1] if criterion_index <= len(ids) else f"DG-O{output_index:02d}-AC{criterion_index:02d}"
                criteria.append((criterion_id, text))
    receipts, receipt_warning = _read_receipts(run_root)
    if receipt_warning:
        warnings.append(receipt_warning)
    policy = str(contract.get("execution_evidence_policy") or "mixed").strip().lower() if contract else ""
    successful_receipts = _successful_execution_receipts(receipts)
    if policy in {"runtime_required", "mixed"}:
        check(
            "DG-EXECUTION-POLICY",
            "runtime_evidence_available",
            "pass" if successful_receipts else "unknown",
            (
                "at least one successful, exit-code-bearing execution receipt is available"
                if successful_receipts
                else "runtime evidence is required but no successful execution receipt is available"
            ),
            successful_receipts[-5:],
        )
    elif policy == "plan_only":
        check(
            "DG-EXECUTION-POLICY",
            "runtime_evidence_available",
            "pass",
            "contract declares plan_only; runtime execution is not required",
        )

    latest_files: dict[str, Any] = {}
    for relative, metadata in snapshot.items():
        latest_files[relative] = metadata
        latest_files[f"deliverables/{relative}"] = metadata
    observations = run_acceptance_checks(
        criteria,
        receipts=receipts,
        root=run_root,
        latest_files=latest_files,
        reader=_candidate_reader(selected, run_root),
    )
    for observation in observations:
        status = "pass" if observation.get("checked") and observation.get("holds") is True else (
            "fail" if observation.get("checked") and observation.get("holds") is False else "unknown"
        )
        check(
            str(observation.get("criterion_id") or "DG-CRITERION"),
            str(observation.get("criterion") or "acceptance criterion"),
            status,
            f"{observation.get('reason') or status}: {observation.get('detail') or ''}".strip(),
            [str(item) for item in observation.get("receipt_ids", []) or []],
        )

    summary_document = _json_object(selected.root / "delivery_summary.json")
    if str(summary_document.get("status") or "").lower() == "failed":
        check(
            "DG-DECLARED-STATUS",
            "delivery_declared_status",
            "fail",
            "delivery_summary.json declares this result failed: "
            + str(summary_document.get("failure_reason") or "unspecified"),
            ["delivery_summary.json"],
        )
    recorded_gate = summary_document.get("delivery_gate")
    if isinstance(recorded_gate, dict):
        recorded_pass = (
            recorded_gate.get("delivery_authorized") is True
            and recorded_gate.get("overall_status") == "pass"
            and not recorded_gate.get("failed_tests")
            and not recorded_gate.get("unknown_tests")
        )
        check(
            "DG-RECORDED-GATE",
            "recorded_delivery_gate",
            "pass" if recorded_pass else "fail",
            (
                "recorded delivery gate is internally complete"
                if recorded_pass else "delivery_summary.json records a non-passing delivery gate"
            ),
            ["delivery_summary.json"],
        )

    final_files = _candidate_files(selected)
    final_snapshot = {
        _relative_path(selected, path): {
            "sha256": _hash_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in final_files
    }
    check(
        "DG-CANDIDATE-STABILITY",
        "candidate_unchanged_during_evaluation",
        "pass" if final_snapshot == snapshot else "fail",
        (
            "candidate bytes remained stable during evaluation"
            if final_snapshot == snapshot else "candidate changed while it was being evaluated"
        ),
    )

    failed = sum(item["status"] == "fail" for item in checks)
    unknown = sum(item["status"] == "unknown" for item in checks)
    passed = sum(item["status"] == "pass" for item in checks)
    status = "fail" if failed else ("inconclusive" if unknown else "pass")
    return {
        "policy_version": POLICY_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_root),
        "candidate": {
            "root": str(selected.root),
            "kind": selected.kind,
            "version": selected.version,
            "file_count": len(files),
            "snapshot": snapshot,
        },
        "contract": {
            "source": contract_source,
            "goal_id": contract.get("goal_id") if contract else None,
            "task_version": contract.get("task_version") if contract else None,
            "summary_required": contract.get("summary_required") if contract else None,
        },
        "status": status,
        "delivery_authorized": status == "pass",
        "summary": {"passed": passed, "failed": failed, "unknown": unknown},
        "checks": checks,
        "warnings": list(dict.fromkeys(warnings)),
    }


def write_acceptance_report(report: dict[str, Any], path: Path | str) -> Path:
    """Atomically persist a standalone verdict when the caller requests it."""
    target = Path(path).expanduser().resolve()
    candidate_root = Path(str((report.get("candidate") or {}).get("root") or "")).resolve()
    if target.is_relative_to(candidate_root):
        raise ValueError("acceptance report must be written outside the evaluated candidate tree")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def _unsupported_forbidden_claims(response: str, forbidden_markers: list[str]) -> list[str]:
    """Detect unsupported assertions without rejecting honest limitations.

    Governor forbidden claims are policy descriptions, not literal banned
    words. A final answer may legitimately say that a test, deployment, or
    evidence item is *not* complete. Treat a marker as a violation only when
    it appears without nearby negation or uncertainty language.
    """
    text = str(response or "")
    negations = (
        "未", "尚未", "尚无", "没有", "无", "缺少", "不足", "无法", "不能",
        "不得", "不应", "未能", "待", "计划", "假设", "如果", "仅建议",
        "not", "without", "pending", "planned", "not yet",
    )
    violations: list[str] = []
    for marker in forbidden_markers:
        marker = str(marker or "").strip()
        if not marker or marker not in text:
            continue
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            context = text[max(0, index - 80):index]
            if not any(token in context for token in negations):
                violations.append(marker)
                break
            start = index + len(marker)
    return violations
