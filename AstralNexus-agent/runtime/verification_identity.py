"""Shared verification identity for the tool-loop guard and the acceptance gate.

Two rules must hold at the same time, and each side of the handoff used to
enforce only its own half:

* a refused tool call is **not** an execution result, and
* an execution result is only valid for the premises it was produced under.

Live r29 evidence (doc 51 §16, node ``N-SERVER-RECOVER-01``): the anchored
command ``python server/smoke_check.py --strict`` ran successfully three times
in one execution (last green receipt ``8f4da3711904424e80106e326caba933``,
rc=0, ``passed=36 failed=0``).  The worker then rewrote ``summary.json`` and
``fix_log.json`` — which the framework's own execution contract declares to
invalidate the earlier pass — so the fourth, *required* re-verification was
refused by the loop guard (``tool loop guard halted repeated call``).  The guard
key is constant here because the call's ``arguments`` name ``server/smoke_check.py``
relative to the process ``cwd`` (``deliverables/app``), a path that does not
resolve under the guard's ``workspace_root``; no referenced file version ever
enters the fingerprint, so only ``_calls[key] > max_repeats`` decides.

The ledger recorded the *refusal* as a receipt (``runtime/execution_ledger.py``
records blocked calls), and the gate's "last decisive match wins" anchor rule
picked that refusal, reporting ``TEST_EXECUTION_FAILED`` for a command that had
actually passed — while the real green receipt had gone stale in the meantime.

This module is the single vocabulary both sides use:

* :func:`is_guard_blocked` — a refusal, not a result;
* :func:`declared_premise_paths` / :func:`premise_versions` — the premise set a
  claim is valid against (the same file versions the staleness rule compares);
* :func:`verification_identity` — command + premise versions, so the guard can
  honour a re-verification whose premises changed, and the gate can associate a
  valid receipt with the claim it proves even when the linkage is missing.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: The literal error text the loop guard returns for a refused call
#: (``runtime/llm_client.py``).
GUARD_BLOCK_ERROR = "tool loop guard halted repeated call"
#: Statuses that mean "the tool never ran" rather than "the command failed".
GUARD_BLOCK_STATUSES = frozenset({"blocked", "guard_blocked", "budget_exhausted"})
#: Markers that identify a refusal in a receipt or a tool result.
GUARD_BLOCK_MARKERS = (GUARD_BLOCK_ERROR, "repeated_identical_tool_call", "tool loop guard")
#: Deterministic cap on hashed premise files; the SAME function runs on both
#: sides, so trimming is symmetric and cannot make one side see a change the
#: other does not.
PREMISE_FILES_LIMIT = 48

_PATH_SUFFIXES = (
    ".py", ".pyi", ".sh", ".bat", ".cmd", ".ps1", ".sql", ".json", ".jsonl",
    ".js", ".ts", ".md", ".txt", ".log", ".csv", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".db", ".html", ".css",
)


def _no_returncode(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in {"", "none", "null"}


def is_guard_blocked(receipt: Mapping[str, Any]) -> bool:
    """Whether this receipt records a guard refusal instead of an execution.

    Fail-closed in the safe direction: a *successful* receipt is never a
    refusal, and a receipt whose ``returncode`` proves the command ran
    (e.g. rc=1) is a real failure, not a refusal.
    """
    status = str(receipt.get("status") or "").strip().lower()
    if status == "success":
        return False
    if status in GUARD_BLOCK_STATUSES:
        return True
    haystack = " ".join(
        str(receipt.get(key) or "")
        for key in ("error", "guard_reason", "stderr_tail", "stdout_tail", "raw_result")
    )
    if not any(marker in haystack for marker in GUARD_BLOCK_MARKERS):
        return False
    return _no_returncode(receipt.get("returncode"))


def normalize_command(command: str) -> str:
    """Whitespace/backslash-normalized command text for identity comparison."""
    text = str(command or "").replace("\\", "/")
    return " ".join(text.split())


#: Criterion-grammar keywords a path can be glued to.  ``file_written:x`` and
#: ``report_not_stale:a differs_from:b`` both carry real workspace paths, so the
#: keyword is stripped before the path is judged (Fix S1): an unstripped token
#: (``file_written:docs/contract.md``) is a premise path that can never exist, so
#: it manufactures a permanent "the premises changed" signal.
_LEAF_KEYWORDS = (
    "file_written:", "file:", "report_not_stale:", "sha_matches:",
    "optional_artifact:", "artifact:", "differs_from:",
)


def path_tokens(text: str) -> List[str]:
    """Path-looking tokens of a criterion or command line (order preserved)."""
    found: List[str] = []
    for raw in str(text or "").replace("\\", "/").split():
        token = raw.strip("\"'`,;:()[]{}")
        for keyword in _LEAF_KEYWORDS:
            if token.lower().startswith(keyword):
                token = token[len(keyword):]
                break
        if not token or len(token) > 400 or token.startswith("-"):
            continue
        lowered = token.lower()
        if "/" not in token and not lowered.endswith(_PATH_SUFFIXES):
            continue
        if token not in found:
            found.append(token)
    return found


def _as_text(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


#: Directory segments that mark a subtree as report-class documentation
#: (DESIGN_ARTIFACT_CONTRACT §8 boundary ③).
REPORT_DIR_SEGMENTS = frozenset(
    {"report", "reports", "reports_draft", "报告", "摘要", "复盘", "总结"}
)
#: Basename markers that mark a documentation file as report-class.  A defect
#: manifest counts: it is derived from functional evidence, not evidence itself.
REPORT_NAME_MARKERS = (
    "summary", "report", "conclusion", "wrapup", "wrap-up",
    "defects", "报告", "摘要", "收尾", "复盘", "结项", "缺陷",
)
#: Executable/source suffixes: a module named ``report.py`` is product code, so
#: the name-marker rule never applies to these.
_CODE_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bat", ".cmd",
     ".ps1", ".sql", ".html", ".css"}
)
#: Documentation/serialization suffixes eligible for the name-marker rule.
_DOC_SUFFIXES = frozenset(
    {".md", ".txt", ".rst", ".json", ".jsonl", ".csv", ".yaml", ".yml"}
)


def is_report_class_path(path: Any) -> bool:
    """Whether a path is report-class documentation rather than functional evidence.

    DESIGN_ARTIFACT_CONTRACT §8 boundary ③: only product code, test scripts and
    test inputs may invalidate functional evidence.  A summary/defect manifest is
    *derived from* that evidence, so rewriting it (for example completing the
    report that a green execution was missing) must not turn the very receipt it
    documents into ``TEST_EXECUTION_STALE`` — which is exactly the loop that
    would otherwise re-run a passing test every time its report is updated.

    Conservative on purpose: a path is report-class when it lives under a report
    directory of any suffix, or when a documentation file carries an explicit
    report marker in its name.  Source modules (``.py`` and friends) are never
    report-class by name, so product code stays a premise.
    """
    text = _as_text(path).lower().lstrip("./")
    if not text:
        return False
    parts = [part for part in text.split("/") if part and part != "."]
    if not parts:
        return False
    directories, name = parts[:-1], parts[-1]
    suffix = "." + name.rsplit(".", 1)[1] if "." in name else ""
    if suffix in _CODE_SUFFIXES:
        return False
    if any(segment in REPORT_DIR_SEGMENTS for segment in directories):
        return True
    if suffix and suffix not in _DOC_SUFFIXES:
        return False
    return any(marker in name for marker in REPORT_NAME_MARKERS)


def criterion_payload_paths(text: Any) -> List[str]:
    """Paths a criterion leaf names, through the ONE criterion grammar.

    Fix S1: the leaf grammar lives in ``runtime.acceptance_checks``; this is the
    only place the premise layer asks it.  Prose criteria (and commands) name no
    leaf payload and fall back to :func:`path_tokens`.
    """
    from AstralNexus.runtime.acceptance_checks import criterion_paths

    return criterion_paths(str(text or ""))


def declared_path_identity(path: Any, declared: Sequence[Any]) -> str:
    """The identity a cited path has inside this node's own declared path set.

    A unique suffix match inside the declared set relocates the citation to the
    file the node actually owns (``docs/contract.md`` -> the declared
    ``deliverables/docs/contract.md``); nothing is ever searched for on disk and
    an ambiguous citation keeps its own spelling.
    """
    from AstralNexus.core.output_paths import PathIdentityError, workspace_identity

    try:
        return workspace_identity("", path, declared=declared).rel
    except PathIdentityError:
        return _as_text(path)


def declared_premise_paths(
    acceptance_criteria: Optional[Sequence[Any]] = None,
    upstream_artifacts: Optional[Sequence[Any]] = None,
    owned_paths: Optional[Sequence[Any]] = None,
    *, root: Any = "",
) -> List[str]:
    """The premise candidates a claim is valid against, in a stable order.

    The tracked-file set (``owned_paths`` plus external upstream artifacts) is
    exactly what the staleness rule compares, so it must be part of the premise
    set: a re-verification is legitimate when *those* files changed, even when
    the anchored command never names them (r29: ``summary.json`` /
    ``fix_log.json``).

    Owned output reports are excluded: backfilling derived evidence must not
    invalidate the execution it documents. Explicit command operands and
    accepted upstream report inputs remain premises, regardless of filename.
    Directory-expanded versions use the same functional_file_versions policy.
    """
    found: List[str] = []

    def _add(value: Any, *, consumed: bool = False) -> None:
        if isinstance(value, Mapping):
            status = value.get("acceptance_status")
            if status is not None and str(getattr(status, "value", status)) != "accepted":
                return
            value = value.get("path") or value.get("artifact_path") or ""
        text = _as_text(value)
        if not text or (is_report_class_path(text) and not consumed):
            return
        if text not in found:
            found.append(text)

    for item in upstream_artifacts or ():
        _add(item, consumed=True)
    for item in owned_paths or ():
        _add(item)
    # Physical declarations are authoritative. Command aliases may relocate
    # into them, but must not create a second phantom declaration first.
    from .acceptance_checks import parse_criterion, criterion_paths, bind_criterion_paths, EXECUTION_KINDS, KIND_COMMAND_OUTPUT
    # Bind command operands against all physical declarations, including a
    # report explicitly consumed as input. Output report checks do not add it.
    declared_scope = list(found)
    for item in list(upstream_artifacts or ()) + list(owned_paths or ()):
        value = item.get("path") if isinstance(item, Mapping) else item
        if value:
            declared_scope.append(_as_text(value))
    for criterion in acceptance_criteria or ():
        text = str(criterion[1]) if isinstance(criterion, (tuple, list)) and len(criterion) == 2 else str(criterion)
        bound, _ = bind_criterion_paths([text], declared=declared_scope, root=root, strict=False)
        text = bound[0] if bound else text
        kind, payload = parse_criterion(text)
        if kind in EXECUTION_KINDS:
            command = payload.get("command", "") if kind == KIND_COMMAND_OUTPUT and isinstance(payload, dict) else str(payload)
            tokens = path_tokens(command)
        else:
            tokens = criterion_paths(text) or path_tokens(text)
        for token in tokens:
            _add(token, consumed=kind in EXECUTION_KINDS)
    return found


def declared_execution_commands(acceptance_criteria: Optional[Sequence[Any]] = None, *,
                                declared: Sequence[Any] = (), root: Any = "") -> Dict[str, str]:
    """Commands from the shared execution-claim grammar."""
    from .acceptance_checks import EXECUTION_KINDS, KIND_COMMAND_OUTPUT, parse_criterion, bind_criterion_paths
    commands: Dict[str, str] = {}
    for criterion in acceptance_criteria or ():
        if isinstance(criterion, (tuple, list)) and len(criterion) == 2:
            text = str(criterion[1])
        else:
            text = str(criterion)
        bound, _ = bind_criterion_paths([text], declared=declared, root=root, strict=False)
        kind, payload = parse_criterion(bound[0])
        if kind not in EXECUTION_KINDS:
            continue
        if kind == KIND_COMMAND_OUTPUT:
            if payload.get("invalid"):
                continue
            payload = payload["command"]
        normalized = normalize_command(str(payload))
        if normalized and normalized not in commands:
            commands[normalized] = text.strip()
    return commands


def command_of_arguments(arguments: Any) -> str:
    """Normalized command text carried by a tool call's arguments."""
    parsed = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return normalize_command(arguments)
    if isinstance(parsed, dict):
        for key in ("command", "argv", "cmd", "target", "script"):
            value = parsed.get(key)
            if isinstance(value, (list, tuple)):
                return normalize_command(" ".join(str(part) for part in value))
            if isinstance(value, str) and value.strip():
                return normalize_command(value)
        code = parsed.get("code")
        if isinstance(code, str) and code.strip():
            return normalize_command(code)
    return ""


def resolve_premise_paths(root: str | Path, candidates: Sequence[Any]) -> List[str]:
    """Workspace-relative posix paths that exist under ``root`` (deduped)."""
    try:
        base = Path(root).resolve()
    except (OSError, ValueError):
        return []
    resolved: List[str] = []
    for candidate in candidates or ():
        text = _as_text(candidate).strip("\"'").lstrip("./")
        if not text:
            continue
        try:
            path = Path(text)
            target = path.resolve() if path.is_absolute() else (base / text).resolve()
        except (OSError, ValueError):
            continue
        try:
            if not target.is_relative_to(base) or not target.exists():
                continue
        except OSError:
            continue
        relative = target.relative_to(base).as_posix()
        if relative not in resolved:
            resolved.append(relative)
    return resolved


def functional_file_versions(
    snapshot: Mapping[str, Any], *, explicit_inputs: Sequence[Any] = (),
    root: str | Path | None = None,
) -> Dict[str, str]:
    """One projection for directory-expanded and recorded execution premises.

    Derived reports stay in raw audit inventories, but cannot stale a test
    merely because its report is written afterwards. An explicitly consumed
    report remains an input; source/test modules remain inputs at any path.
    """
    from AstralNexus.core.output_paths import identity_key
    from .file_write_facts import relative_receipt_path

    def key(path: Any) -> str:
        text = _as_text(path).strip("\"'")
        if root is not None:
            text = relative_receipt_path(text, Path(root)) or text
        return identity_key(text)

    consumed = {key(path) for path in explicit_inputs}
    versions: Dict[str, str] = {}
    for path, entry in snapshot.items():
        if not isinstance(entry, Mapping):
            continue
        if is_report_class_path(path) and key(path) not in consumed:
            continue
        digest = str(entry.get("sha256") or "")
        if digest:
            versions[path] = digest
    return versions


def premise_versions(
    root: str | Path,
    candidates: Sequence[Any],
    *,
    limit: int = PREMISE_FILES_LIMIT,
) -> Dict[str, str]:
    """``workspace-relative path -> sha256`` for the premise set.

    Directories expand to their files (same inventory rules as receipts:
    ``runtime.execution_store.snapshot_files``), so this is the version space
    the gate's staleness rule already compares.
    """
    from .execution_store import snapshot_files

    resolved = resolve_premise_paths(root, candidates)
    if not resolved:
        return {}
    snapshot = snapshot_files(Path(root).resolve(), resolved)
    versions = functional_file_versions(snapshot, explicit_inputs=resolved, root=root)
    if limit > 0 and len(versions) > limit:
        versions = {path: versions[path] for path in sorted(versions)[:limit]}
    return versions


def changed_premises(
    expected: Optional[Mapping[str, str]],
    current: Optional[Mapping[str, str]],
) -> List[str]:
    """Paths whose version differs between two premise snapshots."""
    baseline = dict(expected or {})
    latest = dict(current or {})
    changes = {path for path in baseline if path not in latest}
    changes.update(
        path for path, digest in latest.items()
        if path not in baseline or str(baseline[path]) != str(digest)
    )
    return sorted(changes)


def verification_identity(command: str, premises: Optional[Mapping[str, str]] = None) -> str:
    """Stable id of one verification attempt: command plus premise versions.

    Fix S1 (b32_path_identity): the premise KEYS are folded through the shared
    path identity, so ``deliverables/docs/x.md`` and
    ``.\\deliverables\\docs\\X.md`` are one premise.  Before this, the same
    re-check of the same file under a different spelling produced a different
    identity, i.e. it was read as a NEW premise and the evidence was discarded.
    """
    from AstralNexus.core.output_paths import identity_key

    payload = {
        "command": normalize_command(command),
        "premises": {
            identity_key(key): str(value)
            for key, value in sorted((premises or {}).items())
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return digest[:16]
