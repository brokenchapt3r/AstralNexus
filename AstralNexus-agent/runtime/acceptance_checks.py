"""Deterministic acceptance-criteria checks — code-decidable layer (DeerFlow port).

Contract source: deer-flow@2814bd5d498a ``subagents/acceptance_checks.py``
(MIT, reference copy under 02_运行产物/99_临时文件/deerflow_runtime_reference/).
Pure contract/algorithm extraction per
04_归档/项目文档/2026-10-10_历史修复记录/17_DeerFlow运行时源码映射与移植方案.md
第一批: no LangChain/LangGraph dependency; leaves read AstralNexus
ExecutionStore receipts (``runtime.execution_store``) instead of LangGraph
tool-message harvests.

Leaf families:
- ``file:<path> exists`` / ``file:<path> non-empty`` / ``file_written:<path>``
  — scoped to the run workspace root; containment is canonicalized; reads are
  byte-bounded (size established first; above the cap the leaf answers from
  the size alone; unknown size degrades to UNVERIFIED, never an unbounded
  read; paths outside the shared domain return checked=False rather than
  assuming reachability).
- ``tests_passed:<command>`` — typed claim binding: the criterion must anchor
  to a SPECIFIC recorded execution (a terminal_exec/python_exec receipt whose
  command structurally matches, with success status and a test-summary shape
  in its output tail). Exit code 0 alone never passes; a self-reported
  "passed" text with no recorded execution never passes; a mention inside an
  echo/printf argument cannot anchor the leaf.
- ``report_not_stale:<path> differs_from:<baseline>`` / ``sha_matches:<path>
  sha256:<hex>`` — P6-B additions (doc 32 §4, r14 fraud lesson): re-shipped
  byte-identical voided artifacts are a decidable FAIL; hash pinning is
  available when the expected digest has a reliable provenance.  A missing
  baseline degrades to UNVERIFIED, never to a silent pass.
- Anything else is undecidable in code: ``checked=False``, rendered
  ``UNVERIFIED``, never silently passed.

Vocabulary layering (upstream discipline, kept): leaf booleans are
``checked``/``holds`` — never satisfied/verified/passed. Execution success,
deterministic check holding, and final business acceptance are three
different things; strong-positive wording stays with the runtime gate.

Local modifications vs upstream (recorded per plan §复用方式):
- ``TEST_EXECUTION_STALE`` / ``TESTS_REPORTED_FAILURES`` reasons added: an
  anchored success is invalidated when the receipt file snapshots show any
  owned file changed afterwards (旧成功不能复用), and a recognized summary
  reporting failures is a decidable FAIL even when the shell returned 0
  (e.g. ``pytest || true``).
- Command matching is a documented pragmatic subset of upstream's shell
  provenance analysis: quote-aware segment splitting on ``&& || ; |`` and
  newline, executable+argument comparison after whitespace normalization,
  and exclusion of echo/printf/Write-Output segments (a criterion string
  merely printed is not executed). Commands with unbalanced quotes or shell
  escaping degrade to TEST_COMMAND_MATCH_UNVERIFIED instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from AstralNexus.core.task_declaration import criterion_leaf_id

from .file_write_facts import relative_receipt_path
from .verification_identity import (
    is_guard_blocked,
    is_report_class_path,
    verification_identity,
    functional_file_versions,
    path_tokens,
)

CHECKED_BY = "acceptance_check"
FILE_CONTENT_READ_CAP_BYTES = 2 * 1024 * 1024

#: Closed reason enum (module-level so tests can pin the vocabulary).
REASONS = frozenset({
    "UNDECIDABLE_IN_CODE",
    "FILE_NOT_FOUND",
    "FILE_EMPTY",
    "FILE_CONTENT_MISMATCH",
    "FILE_CONTENT_UNAVAILABLE",
    "INVALID_FILE_CONTENT_CRITERION",
    "FILE_NOT_IN_SHARED_DOMAIN",
    "FILE_SIZE_UNKNOWN",
    "FILE_CHECK_IO_ERROR",
    "TEST_EXECUTION_NOT_FOUND",
    "TEST_EXECUTION_FAILED",
    # Fix r29 (doc 51 §16): the only matching attempts were REFUSED by the
    # tool-loop guard.  A refusal is not an execution, so it can neither anchor a
    # pass nor be silently reported as "not found" — it is a decided failure
    # whose repair is to make the guard admit the re-verification.
    "TEST_EXECUTION_BLOCKED",
    "TEST_SUMMARY_NOT_RECOGNIZED",
    "TEST_COMMAND_MATCH_UNVERIFIED",
    "TEST_EXECUTION_UNUSABLE",
    "TEST_EXECUTION_STALE",
    "TESTS_REPORTED_FAILURES",
    "TEST_SUMMARY_ZERO_TESTS",
    "COMMAND_OUTPUT_MISMATCH",
    "COMMAND_EXIT_CODE_UNAVAILABLE",
    "COMMAND_OUTPUT_UNAVAILABLE",
    "INVALID_EXECUTION_CRITERION",
    "STALE_REPORT_BYTE_IDENTICAL",
    "STALE_BASELINE_UNAVAILABLE",
    "STALE_COMPARISON_INCONCLUSIVE",
    "SHA_MISMATCH",
    "WORKSPACE_UNAVAILABLE",
    # R5 (DESIGN_R5_WRITE_FACTS §4 Patch 1): the write-fact leaf vocabulary.
    # Not being in this closed set is a silent death — ``_observation`` below
    # rewrites an unknown reason to CHECK_INTERNAL_ERROR (:294-295).  Blocking
    # reasons describe definite no-op evidence only; ``WRITE_CHANGE_UNKNOWN`` is
    # deliberately NOT blocking (a missing baseline is not proof of no change,
    # and in a repair execution the first write is the usual fix).
    "WRITE_NO_PRODUCT_CHANGE",
    "WRITE_CHANGE_UNKNOWN",
    "WRITE_FAILED_BUT_MODIFIED",
    "WRITE_ONLY_TEST_CHANGE",
    "WRITE_ONLY_REPORT_CHANGE",
    "WRITE_LANDED",
    "CHECK_INTERNAL_ERROR",
})

KIND_FILE_CONTAINS = "file_contains"
KIND_FILE_EXISTS = "file_exists"
KIND_FILE_NON_EMPTY = "file_non_empty"
KIND_FILE_WRITTEN = "file_written"
KIND_TESTS_PASSED = "tests_passed"
KIND_COMMAND_SUCCEEDED = "command_succeeded"
KIND_COMMAND_OUTPUT = "command_output"
EXECUTION_KINDS = frozenset({KIND_TESTS_PASSED, KIND_COMMAND_SUCCEEDED, KIND_COMMAND_OUTPUT})
KIND_REPORT_NOT_STALE = "report_not_stale"
KIND_SHA_MATCHES = "sha_matches"
KIND_UNDECIDABLE = "undecidable"
#: R5: every write-fact leaf carries this kind; the reason string decides which
#: write question was answered (see ``write_change_observations``).  The
#: pre-existing ``KIND_FILE_WRITTEN`` above is untouched — the ``file_written:``
#: criterion is a different, pre-existing question about an existing-path leaf.
KIND_WRITE_LANDED = "write_landed"

_FILE_EXISTS_RE = re.compile(r"^file:(?P<path>.+?)\s+exists$", re.IGNORECASE)
_FILE_NON_EMPTY_RE = re.compile(r"^file:(?P<path>.+?)\s+non-?empty$", re.IGNORECASE)
_FILE_WRITTEN_RE = re.compile(r"^file_written:(?P<path>.+?)$", re.IGNORECASE)
_TESTS_PASSED_RE = re.compile(r"^tests_passed:(?P<command>.+)$", re.IGNORECASE)
_COMMAND_SUCCEEDED_RE = re.compile(r"^command_succeeded:(?P<command>.+)$", re.IGNORECASE)
_COMMAND_OUTPUT_RE = re.compile(r"^command_output:(?P<spec>.+)$", re.IGNORECASE)

#: P6-B (doc 32 §4, r14 fraud lesson): a superseding node re-delivering a
#: byte-identical copy of the voided artifact is a decidable FAIL — the
#: N9RR lineage archived stale reports and re-shipped them twice before a
#: 5-hour forensic node caught it by hand.  The baseline (archive/voided
#: copy) must exist for the comparison; a missing baseline degrades to
#: UNVERIFIED (the planner named a wrong path), never to a silent pass.
_REPORT_NOT_STALE_RE = re.compile(
    r"^report_not_stale:(?P<path>.+?)\s+differs_from:(?P<baseline>.+?)$",
    re.IGNORECASE,
)
#: P6-B: pin a delivered artifact to a hash with a reliable provenance
#: (upstream receipt, published manifest).  64 hex digits or undecidable.
_SHA_MATCHES_RE = re.compile(
    r"^sha_matches:(?P<path>.+?)\s+sha256:(?P<digest>[0-9a-fA-F]{64})$",
    re.IGNORECASE,
)

#: Fix P5#14 (r17 live, 2026-10-02): ``pytest_run`` is an execution tool whose
#: receipts carry ``arguments.target`` but no command key; without it the
#: deterministic tests_passed anchor silently skipped the only real pytest
#: execution in R-TESTS-REPAIR's run and degraded to UNVERIFIED.
_EXEC_TOOLS = {"terminal_exec", "python_exec", "pytest_run", "shell", "bash"}
_PRINT_EXECUTABLES = {"echo", "printf", "write-output", "write-host"}

_SUMMARY_PATTERNS: Tuple[Tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    # (pass-shape, fail-shape) — pytest / unittest / go / node-TAP / jest
    (re.compile(r"\d+\s+passed", re.IGNORECASE), re.compile(r"\d+\s+(failed|error)", re.IGNORECASE)),
    # Fix r29 (doc 51 §16, offline repro): this bare-word shape is the unittest
    # token, which the runner always emits in upper case (``FAILED
    # (failures=1)``).  Under IGNORECASE it also matched the lower-case word
    # ``failed`` wherever it appeared as prose — including a check script's
    # empty section banner ``---- failed detail ----`` printed next to
    # ``SMOKE_SUMMARY {"passed": 36, "failed": 0}``.  That flipped three real
    # passing anchors (rc=0) into TESTS_REPORTED_FAILURES and, because this
    # scan runs before the staleness check, masked the honest verdict
    # (TEST_EXECUTION_STALE) and routed recovery to "repair the implementation"
    # instead of "re-run after the tracked files changed".  Non-count labels
    # are not failure evidence; every count-bearing shape below stays
    # case-insensitive.
    (re.compile(r"\bOK\b(\s*\(|\s*$)"), re.compile(r"\bFAILED\b")),
    (re.compile(r"(?m)^PASS\b|^ok\s+\S+"), re.compile(r"(?m)^FAIL\b", re.IGNORECASE)),
    (re.compile(r"#\s*pass\s+\d+", re.IGNORECASE), re.compile(r"#\s*fail\s+\d+", re.IGNORECASE)),
    (re.compile(r"Tests:\s.*passed", re.IGNORECASE), re.compile(r"Tests:\s.*failed", re.IGNORECASE)),
    # Fix P5#17b (r17-resume#3 live, 2026-10-03 05:15): recovery contracts
    # teach check scripts to print their own machine-readable JSON summary
    # line (``{"passed": 13, "failed": 0, ...}``), but the then-IGNORECASE
    # ``\bFAILED\b`` fail shape above matched the JSON key ``"failed"`` even
    # at zero count — the taught output was judged TESTS_REPORTED_FAILURES.
    # Recognize key:value summary counts (quoted or bare) as first-class
    # pass/fail shapes; zero counts are neutralized by _ZERO_FAIL_MENTION_RE.
    # (r29 made that bare-word shape case-sensitive, so only the digit-first
    # "0 failed" mention and any numeric zero key still need the strip.)
    (re.compile(r"[\"']?passed[\"']?\s*[:=]\s*[1-9]\d*", re.IGNORECASE),
     re.compile(r"[\"']?failed[\"']?\s*[:=]\s*[1-9]\d*", re.IGNORECASE)),
)


#: The recognized vocabularies of ``_SUMMARY_PATTERNS``, rendered once for
#: humans.  Single source on purpose: the verifier's rejection detail and the
#: Planner's criterion grammar both render this string, so the requirement is
#: knowable when a ``tests_passed:`` criterion is AUTHORED -- not only after its
#: node has already failed and been re-planned.
#:
#: Fix P5#17 taught the REPAIR path this vocabulary ("never WHICH vocabularies
#: are -- so repair workers had to guess the format, burning recovery cycles").
#: The authoring path never got it: ``core/governor.py`` told the Planner only
#: that the criterion is "一条确切测试命令 ... 输出中的测试摘要", naming no
#: shape.  Live b33case1-rerun1 N-110 then authored
#: ``python -c "...;sys.exit(0 if ... else 1)"`` -- an idiomatic exit-code check
#: whose stdout is empty by design -- which no recognized shape can ever accept,
#: making the obligation unsatisfiable by construction.
SUMMARY_EVIDENCE_FORMS = (
    "'<N> passed' with N>0 (pytest style), 'OK' (unittest), line-initial "
    "'PASS' (go/TAP), '# pass <N>', 'Tests: ... passed', or a key:value "
    "summary such as \"passed\": <N> (N>0) with \"failed\": 0 (JSON counts)"
)


#: Fix P5#17b: zero-count failure mentions are not failure evidence.  Without
#: this strip, "13 passed, 0 failed" (digit-first fail shape) was judged
#: TESTS_REPORTED_FAILURES — bona fide passing summaries rejected.  Non-zero
#: counts ("3 failed", ``"failed": 2``, "10 errors") are untouched, so real
#: failures still decide FAIL.  ``\b0`` cannot match inside "10"/"20" (no word
#: boundary between digits).
_ZERO_FAIL_MENTION_RE = re.compile(
    r"\b0\s+(?:failed|failures?|errors?)\b"
    r"|[\"']?(?:failed|failures?|errors?)[\"']?\s*[:=]\s*0\b",
    re.IGNORECASE,
)


#: Fix BB (doc 26 §42, 2026-09-18 r13b): upstream ``_TEST_ZERO_SHAPE_RE``.
#: ``re.compile(r"\d+\s+passed")`` also matches "**0 passed**", so a run that
#: executed no test at all was recorded as HOLDS.  Zero is not a pass.
_ZERO_TEST_SHAPE_RE = re.compile(
    r"\b0\s+passed\b"
    r"|[\"']?passed[\"']?\s*[:=]\s*0\b"
    r"|\[\s*no\s+test\s+files\s*\]"
    r"|\[\s*no\s+tests?\s+(?:to\s+run|ran|found)\s*\]"
    r"|\bRan\s+0\s+tests?\b"
    r"|\bno\s+tests?\s+ran\b",
    re.IGNORECASE,
)

#: Upstream ``_negation_overlaps`` subset: pytest options that remove the very
#: target a criterion names make the anchored success worthless.
_NEGATING_OPTIONS = ("--ignore", "--ignore-glob", "--deselect", "--deselect-file")


def _unquote(token: str) -> str:
    text = str(token or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _path_payload(token: str) -> str:
    text = _unquote(token)
    return text[1:-1].strip() if text.startswith("<") and text.endswith(">") else text


def _command_tokens(command: str) -> List[str]:
    try:
        return [_unquote(item) for item in shlex.split(str(command or ""), posix=False)]
    except ValueError:
        return [_unquote(item) for item in str(command or "").split()]


def _negation_values(command: str) -> set:
    """Explicit ``--ignore``/``--deselect`` values declared by one command."""
    tokens = _command_tokens(command)
    values = set()
    for index, token in enumerate(tokens):
        name = token.split("=", 1)[0].lower()
        if name not in _NEGATING_OPTIONS:
            continue
        if "=" in token:
            values.add(token.split("=", 1)[1])
        elif index + 1 < len(tokens):
            values.add(tokens[index + 1])
    return {value for value in values if value}


def _negation_overlaps_criterion(criterion_command: str, receipt_command: str) -> bool:
    """Whether a receipt negated a target the criterion explicitly names.

    ``pytest tests --ignore tests/x`` must not anchor a criterion whose own
    command names ``tests/x``.  The criterion's OWN negations are exempt (a
    criterion that itself asks to ignore ``tests/slow`` still matches a run that
    does the same).  Only explicit option values are compared; ``-k`` filter
    expressions stay with the fail-closed UNVERIFIED path rather than guessed at.
    """
    wanted = [token for token in _command_tokens(criterion_command) if token]
    extra = _negation_values(receipt_command) - _negation_values(criterion_command)
    if extra and _normalize(criterion_command) in {"pytest", "python -m pytest"}:
        return True
    for value in extra:
        stem = value.rstrip("*")
        for target in wanted:
            if not target:
                continue
            if target == value or (stem and (target.startswith(stem) or stem.startswith(target))):
                return True
    return False


def parse_criterion(criterion: str) -> Tuple[str, Any]:
    """Map a criterion string to (kind, payload); undecidable payload is None.

    Payload is a string for the single-argument families and a dict for the
    two-argument P6-B families (report_not_stale: path+baseline; sha_matches:
    path+digest).
    """
    text = str(criterion or "").strip()
    if text.lower().startswith("file_contains:"):
        try:
            spec = json.loads(text.split(":", 1)[1])
        except (ValueError, TypeError):
            spec = None
        if (not isinstance(spec, dict) or set(spec) - {"path", "text", "case_sensitive"}
                or not isinstance(spec.get("path"), str) or not spec["path"].strip()
                or not isinstance(spec.get("text"), str) or not spec["text"]
                or not isinstance(spec.get("case_sensitive", True), bool)):
            return KIND_FILE_CONTAINS, {"invalid": True}
        return KIND_FILE_CONTAINS, {**spec, "path": _path_payload(spec["path"])}
    for regex, kind in (
        (_FILE_EXISTS_RE, KIND_FILE_EXISTS),
        (_FILE_NON_EMPTY_RE, KIND_FILE_NON_EMPTY),
        (_FILE_WRITTEN_RE, KIND_FILE_WRITTEN),
        (_TESTS_PASSED_RE, KIND_TESTS_PASSED),
        (_COMMAND_SUCCEEDED_RE, KIND_COMMAND_SUCCEEDED),
    ):
        match = regex.match(text)
        if match:
            payload = match.group("path") if "path" in match.groupdict() else match.group("command")
            return kind, _path_payload(payload) if "path" in match.groupdict() else payload.strip()
    output_match = _COMMAND_OUTPUT_RE.match(text)
    if output_match:
        try:
            spec = json.loads(output_match.group("spec"))
        except (ValueError, TypeError):
            spec = None
        if (not isinstance(spec, dict) or set(spec) != {"command", "stdout"}
                or not isinstance(spec.get("command"), str) or not spec["command"].strip()
                or not isinstance(spec.get("stdout"), str)):
            return KIND_COMMAND_OUTPUT, {"invalid": True}
        return KIND_COMMAND_OUTPUT, spec
    stale_match = _REPORT_NOT_STALE_RE.match(text)
    if stale_match:
        return KIND_REPORT_NOT_STALE, {
            "path": _path_payload(stale_match.group("path")),
            "baseline": _path_payload(stale_match.group("baseline")),
        }
    sha_match = _SHA_MATCHES_RE.match(text)
    if sha_match:
        return KIND_SHA_MATCHES, {
            "path": _path_payload(sha_match.group("path")),
            "digest": sha_match.group("digest").strip().lower(),
        }
    return KIND_UNDECIDABLE, None


#: Fix S1 (b32_path_identity): the ONE mapping from "a criterion leaf" to the
#: workspace paths it names -- regex, kind, and the payload fields that carry a
#: path, in payload order.  ``report_not_stale`` names two paths (the artifact
#: and the baseline it must differ from).  Adding a path-carrying leaf here is
#: the only supported way to extend the grammar.
_LEAF_GRAMMAR: Tuple[Tuple["re.Pattern[str]", str, Tuple[str, ...]], ...] = (
    (_FILE_EXISTS_RE, KIND_FILE_EXISTS, ("path",)),
    (_FILE_NON_EMPTY_RE, KIND_FILE_NON_EMPTY, ("path",)),
    (_FILE_WRITTEN_RE, KIND_FILE_WRITTEN, ("path",)),
    (_REPORT_NOT_STALE_RE, KIND_REPORT_NOT_STALE, ("path", "baseline")),
    (_SHA_MATCHES_RE, KIND_SHA_MATCHES, ("path",)),
)


def criterion_match(criterion: str) -> Optional["re.Match[str]"]:
    """The grammar match of one criterion leaf (``None`` when it names no path)."""
    text = str(criterion or "").strip()
    for regex, _kind, _fields in _LEAF_GRAMMAR:
        match = regex.match(text)
        if match:
            return match
    return None


def criterion_paths(criterion: str) -> List[str]:
    """The workspace paths one criterion leaf names, in payload order.

    Fix S1: ``file_written:`` carries a path exactly like ``file:`` does.  The
    two paths of ``report_not_stale:`` are both workspace paths.  A leaf with
    no path payload (``tests_passed:``, prose) yields no path and is never
    given one.
    """
    kind, payload = parse_criterion(criterion)
    if kind == KIND_FILE_CONTAINS:
        return [] if payload.get("invalid") else [payload["path"]]
    match = criterion_match(criterion)
    if match is None:
        return []
    fields = next(
        (fields for regex, _kind, fields in _LEAF_GRAMMAR if regex is match.re),
        (),
    )
    return [_path_payload(str(match.group(field) or "")) for field in fields]


def criterion_path(criterion: str) -> str:
    """The FIRST path one criterion leaf names (``""`` when it names none).

    The one replacement for ``Verifier._criterion_file_path``.
    """
    paths = criterion_paths(criterion)
    return paths[0] if paths else ""


def criterion_with_paths(criterion: str, identities: Sequence[Any]) -> str:
    """One leaf text with its path payloads replaced (everything else verbatim).

    Only the matched payload spans are rewritten, right to left, so a
    ``report_not_stale:`` baseline that happens to be a suffix of its path is
    never re-substituted into the freshly written path.
    """
    text = str(criterion or "").strip()
    match = criterion_match(text)
    if match is None:
        return text
    fields = next(
        (fields for regex, _kind, fields in _LEAF_GRAMMAR if regex is match.re),
        (),
    )
    edits: List[Tuple[int, int, str]] = []
    for field, identity in zip(fields, identities):
        if identity is None:
            continue
        original = match.group(field)
        if str(identity) == original:
            continue
        edits.append((match.start(field), match.end(field), str(identity)))
    for start, end, identity in reversed(edits):
        text = text[:start] + identity + text[end:]
    return text


def _bind_test_command_paths(
    command: str, declared: Sequence[Any], root: Any, strict: bool,
    *, actual_cwd: Optional[str] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """Bind script/test targets, never arbitrary arguments or shell code.

    Use the same declaration identity as file leaves. Receipts remain literal
    execution facts; only the admission-time contract is relocated.
    """
    from AstralNexus.core.output_paths import PathIdentityError, workspace_identity

    segments = _split_segments(command)
    if segments is None or len(segments) != 1:
        return command, []
    spans = list(re.finditer(r'''"[^"\n]*"|'[^'\n]*'|[^\s"']+''', command))
    tokens = [match.group().strip("\"'") for match in spans]
    if not tokens:
        return command, []
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    python = bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable))
    targets: List[int] = []
    if python and len(tokens) > 1 and not tokens[1].startswith("-"):
        targets = [1]
    elif executable in {"pytest", "pytest.exe"} or (
        python and tokens[1:3] == ["-m", "pytest"]
    ):
        start = 1 if not python else 3
        targets = [index for index in range(start, len(tokens))
                   if not tokens[index].startswith("-")
                   and ("/" in tokens[index] or "\\" in tokens[index]
                        or tokens[index].split("::", 1)[0].endswith(".py"))]
    elif python and tokens[1:3] == ["-m", "unittest"]:
        if "discover" not in tokens[3:] and any(token in {
            "-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory",
        } for token in tokens[3:]):
            return _bind_test_command_paths(
                _join_command_tokens(tokens[:3] + ["discover"] + tokens[3:]),
                declared, root, strict, actual_cwd=actual_cwd,
            )
        if all(token in {"-v", "-q", "--verbose", "--quiet"} for token in tokens[3:]):
            parents = {
                str(Path(str(path).replace("\\", "/")).parent).replace("\\", "/")
                for path in declared
                if Path(str(path).replace("\\", "/")).name.startswith("test_")
                and str(path).endswith(".py")
            }
            directory = actual_cwd if actual_cwd is not None else (
                next(iter(parents)) if len(parents) == 1 else ""
            )
            if directory and directory != ".":
                return _join_command_tokens(tokens[:3] + ["discover", "-s", directory] + tokens[3:]), []
        targets = [index + 1 for index, token in enumerate(tokens[:-1])
                   if token in {"-s", "--start-directory", "-t", "--top-level-directory"}]
    edits: List[Tuple[int, int, str]] = []
    relocations: List[Dict[str, str]] = []
    for index in targets:
        path, separator, selector = tokens[index].partition("::")
        try:
            identity = workspace_identity(
                root, (actual_cwd.rstrip("/") + "/" + path) if actual_cwd else path,
                declared=() if actual_cwd is not None else declared,
            )
        except PathIdentityError:
            if strict:
                raise
            continue
        if actual_cwd is None and identity.status not in {"exact", "relocated"}:
            continue
        value = identity.rel + separator + selector
        if value != tokens[index]:
            edits.append((spans[index].start(), spans[index].end(), shlex.quote(value)))
        if identity.relocated:
            relocations.append({"raw": identity.raw, "identity": identity.rel,
                                "reason": identity.reason, "root": identity.root})
    for start, end, value in reversed(edits):
        command = command[:start] + value + command[end:]
    return command, relocations


def bind_criterion_paths(
    criteria: Sequence[Any],
    *,
    declared: Sequence[Any] = (),
    root: Any = "",
    strict: bool = True,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Give every criterion leaf ONE path identity before it is stored.

    Fix S1 (b32_path_identity): the leaf used to be resolved literally against
    the run dir while ``owned_paths`` / ``expected_outputs`` carried the
    namespace partition, so one node held two identities for one file -- live
    B+ run task-1791256754557: C-01 reported ``FILE_NOT_FOUND`` for
    ``docs/contract.md`` while its 40 006 B deliverable
    ``deliverables/docs/contract.md`` was on disk and recorded as ``added``,
    and the node was failed and re-planned (recovery_churn 14).

    ``declared`` is the path set the leaf may be relocated into -- the node's
    own owned_paths / expected_outputs, never a search of the filesystem.  A
    unique suffix match inside that set relocates the leaf and is recorded; an
    ambiguous match, an absolute path or a path escaping the root raises
    :class:`PathIdentityError` (``strict``), which is what rejects the patch.
    With ``strict=False`` such a leaf keeps its own spelling, so a graph that
    was admitted before this fix still verifies exactly as it did.

    Returns ``(rewritten criteria, relocation records)``.
    """
    from AstralNexus.core.output_paths import PathIdentityError, workspace_identity, workspace_relative

    declared = [workspace_relative(root, path) if root and Path(str(path)).is_absolute()
                else path for path in declared]

    rewritten: List[str] = []
    relocations: List[Dict[str, str]] = []
    for criterion in criteria or ():
        text = str(criterion or "").strip()
        if not text:
            continue
        kind, payload = parse_criterion(text)
        if kind == KIND_FILE_CONTAINS:
            if payload.get("invalid"):
                if strict:
                    raise ValueError("file_contains requires JSON path/text and optional boolean case_sensitive")
                rewritten.append(text)
                continue
            try:
                identity = workspace_identity(root, payload["path"], declared=declared)
                payload = {**payload, "path": identity.rel}
            except PathIdentityError:
                if strict:
                    raise
            rewritten.append("file_contains:" + json.dumps(payload, ensure_ascii=False))
            continue
        if kind in EXECUTION_KINDS:
            if kind == KIND_COMMAND_OUTPUT and payload.get("invalid"):
                if strict:
                    raise ValueError("command_output requires JSON with exactly command and stdout strings")
                rewritten.append(text)
                continue
            raw_command = payload["command"] if kind == KIND_COMMAND_OUTPUT else str(payload)
            command, command_relocations = _bind_test_command_paths(
                raw_command, declared, root, strict,
            )
            rewritten.append(text[:text.index(":") + 1] + (
                json.dumps({**payload, "command": command}, ensure_ascii=False)
                if kind == KIND_COMMAND_OUTPUT else command
            ))
            relocations.extend(command_relocations)
            continue
        identities: List[str] = []
        for path in criterion_paths(text):
            if not path:
                identities.append("")
                continue
            try:
                identity = workspace_identity(root, path, declared=declared)
            except PathIdentityError as error:
                if strict:
                    raise
                identities.append(path)
                continue
            identities.append(identity.rel)
            if identity.relocated:
                relocations.append({
                    "raw": identity.raw,
                    "identity": identity.rel,
                    "reason": identity.reason,
                    "root": identity.root,
                })
        rewritten.append(criterion_with_paths(text, identities))
    return rewritten, relocations


def _observation(
    criterion_id: str,
    criterion: str,
    kind: str,
    execution_id: str,
    *,
    checked: bool,
    holds: Optional[bool],
    reason: str,
    detail: str = "",
    receipt_ids: Optional[Sequence[str]] = None,
    artifact_versions: Optional[Dict[str, str]] = None,
    verification: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Fix P5#11 (r15 live, 2026-10-01): the empty reason of a decided-holds
    # leaf is NOT an error.  The old normalization rewrote "" (not in REASONS)
    # to CHECK_INTERNAL_ERROR, mislabeling every healthy holds observation —
    # invisible in r14 only because the deterministic layer was starved to
    # all-UNDECIDABLE.  Empty stays empty; render shows "decided".
    if reason and reason not in REASONS:
        reason = "CHECK_INTERNAL_ERROR"
    payload: Dict[str, Any] = {
        "criterion_id": criterion_id,
        "criterion": criterion,
        "kind": kind,
        "execution_id": execution_id,
        "checked": bool(checked),
        "holds": None if holds is None else bool(holds),
        "reason": reason,
        "detail": detail[:500],
        "receipt_ids": [str(item) for item in (receipt_ids or [])],
        "artifact_versions": dict(artifact_versions or {}),
        "checked_by": CHECKED_BY,
    }
    if verification:
        # Fix r29: the structured handoff (which claim, which anchor, which
        # attempts were refused, which premise versions and identity the verdict
        # belongs to) is emitted only when a caller supplies it, so older
        # observations keep their exact shape.
        payload["verification"] = dict(verification)
    return payload


def render_acceptance_observation(observation: Dict[str, Any]) -> str:
    """Model/报告-visible rendering; strong-positive words stay out of leaves."""
    if not observation.get("checked"):
        return f"UNVERIFIED [{observation.get('reason', 'CHECK_INTERNAL_ERROR')}]"
    if observation.get("holds"):
        return f"HOLDS [{observation.get('reason') or 'decided'}]"
    return f"DOES NOT HOLD [{observation.get('reason') or 'decided'}]"


def _default_reader(root: Path, relative: str) -> Tuple[Optional[int], Optional[bytes], Optional[str]]:
    """Containment-checked, byte-bounded read. Returns (size, content, reason)."""
    try:
        base = root.resolve()
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(base):
            return None, None, "FILE_NOT_IN_SHARED_DOMAIN"
        if not candidate.exists():
            return None, None, "FILE_NOT_FOUND"
        if not candidate.is_file():
            return None, None, "FILE_NOT_IN_SHARED_DOMAIN"
        size = candidate.stat().st_size
        if size > FILE_CONTENT_READ_CAP_BYTES:
            return size, None, None
        return size, candidate.read_bytes(), None
    except OSError:
        return None, None, "FILE_CHECK_IO_ERROR"


def _check_file_leaf(
    kind: str,
    relative: str,
    root: Optional[Path],
    reader: Optional[Callable[[Path, str], Tuple[Optional[int], Optional[bytes], Optional[str]]]],
) -> Tuple[bool, Optional[bool], str, str]:
    if root is None and reader is None:
        return False, None, "WORKSPACE_UNAVAILABLE", "no workspace root or reader injected"
    active_root = root if root is not None else Path(".")
    try:
        if reader is not None:
            size, content, reason = reader(active_root, relative)
        else:
            size, content, reason = _default_reader(active_root, relative)
    except Exception as exc:  # defensive: leaves degrade, never raise
        return False, None, "CHECK_INTERNAL_ERROR", str(exc)
    if reason == "FILE_NOT_FOUND":
        return True, False, "FILE_NOT_FOUND", f"{relative} does not exist in the workspace"
    if reason is not None:
        return False, None, reason, f"{relative}: {reason}"
    if size is None:
        return False, None, "FILE_SIZE_UNKNOWN", f"{relative}: size could not be established"
    if kind == KIND_FILE_EXISTS:
        return True, True, "", f"{relative} exists ({size} bytes)"
    if size == 0:
        return True, False, "FILE_EMPTY", f"{relative} exists but is empty"
    if content is None:
        # Above the read cap the size alone decides non-emptiness.
        return True, True, "", f"{relative} is non-empty ({size} bytes, above read cap)"
    return True, True, "", f"{relative} is non-empty ({size} bytes)"


def _hash_leaf(
    relative: str,
    root: Optional[Path],
    reader: Optional[Callable[[Path, str], Tuple[Optional[int], Optional[bytes], Optional[str]]]],
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """(sha256, size, reason) for one workspace-relative file; reason None on success.

    Hashing is memory-bounded: content under the read cap hashes from the
    reader result; above it, a real root streams the file in 64 KB chunks.
    Reader-only injection facing an above-cap file cannot hash and degrades
    to UNVERIFIED (FILE_SIZE_UNKNOWN), never to a guessed verdict.
    """
    if root is None and reader is None:
        return None, None, "WORKSPACE_UNAVAILABLE"
    active_root = root if root is not None else Path(".")
    try:
        if reader is not None:
            size, content, reason = reader(active_root, relative)
        else:
            size, content, reason = _default_reader(active_root, relative)
    except Exception:
        return None, None, "CHECK_INTERNAL_ERROR"
    if reason is not None:
        return None, None, reason
    if content is not None:
        return hashlib.sha256(content).hexdigest(), size, None
    if root is not None:
        try:
            base = root.resolve()
            candidate = (base / relative).resolve()
            if not candidate.is_relative_to(base) or not candidate.is_file():
                return None, None, "FILE_NOT_IN_SHARED_DOMAIN"
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest(), candidate.stat().st_size, None
        except OSError:
            return None, None, "FILE_CHECK_IO_ERROR"
    # Unhashable (reader-only injection above the cap): keep the known size —
    # a size difference alone can still decide differs_from.
    return None, size, "FILE_SIZE_UNKNOWN"


def _check_report_not_stale(
    payload: Dict[str, str],
    root: Optional[Path],
    reader: Optional[Callable[[Path, str], Tuple[Optional[int], Optional[bytes], Optional[str]]]],
) -> Tuple[bool, Optional[bool], str, str]:
    """P6-B: new artifact must differ from the voided baseline, byte-wise."""
    relative = payload["path"]
    baseline = payload["baseline"]
    new_sha, new_size, new_reason = _hash_leaf(relative, root, reader)
    if new_reason == "FILE_NOT_FOUND":
        return True, False, "FILE_NOT_FOUND", f"{relative} does not exist in the workspace"
    if new_reason is not None and new_reason != "FILE_SIZE_UNKNOWN":
        return False, None, new_reason, f"{relative}: {new_reason}"
    old_sha, old_size, old_reason = _hash_leaf(baseline, root, reader)
    if old_reason == "FILE_NOT_FOUND":
        # The planner named a baseline that is not there; staleness cannot be
        # decided in code.  UNVERIFIED, never a silent pass.
        return False, None, "STALE_BASELINE_UNAVAILABLE", (
            f"baseline {baseline} does not exist; cannot decide staleness of {relative} in code"
        )
    if old_reason is not None and old_reason != "FILE_SIZE_UNKNOWN":
        return False, None, old_reason, f"{baseline}: {old_reason}"
    if new_size is not None and old_size is not None and new_size != old_size:
        return True, True, "", (
            f"{relative} differs from {baseline} ({new_size} vs {old_size} bytes)"
        )
    if new_sha is None or old_sha is None:
        return False, None, "STALE_COMPARISON_INCONCLUSIVE", (
            f"equal sizes but hash unavailable for {relative} or {baseline}"
        )
    if new_sha == old_sha:
        return True, False, "STALE_REPORT_BYTE_IDENTICAL", (
            f"{relative} is byte-identical to voided baseline {baseline} (sha256 {new_sha[:16]}…)"
        )
    return True, True, "", (
        f"{relative} differs from {baseline} (sha256 {new_sha[:16]}… vs {old_sha[:16]}…)"
    )


def _check_sha_matches(
    payload: Dict[str, str],
    root: Optional[Path],
    reader: Optional[Callable[[Path, str], Tuple[Optional[int], Optional[bytes], Optional[str]]]],
) -> Tuple[bool, Optional[bool], str, str]:
    """P6-B: artifact hash equals the expected digest from a reliable source."""
    relative = payload["path"]
    expected = payload["digest"]
    actual, size, reason = _hash_leaf(relative, root, reader)
    if reason == "FILE_NOT_FOUND":
        return True, False, "FILE_NOT_FOUND", f"{relative} does not exist in the workspace"
    if reason is not None:
        return False, None, reason, f"{relative}: {reason}"
    if actual == expected:
        return True, True, "", f"{relative} sha256 matches {expected[:16]}… ({size} bytes)"
    return True, False, "SHA_MISMATCH", (
        f"{relative} sha256 {str(actual)[:16]}… != expected {expected[:16]}…"
    )


def _join_command_tokens(tokens: Any) -> str:
    """Fix P5#12 (r15 live, 2026-10-01): join list-form commands losslessly.

    ``terminal_exec`` receipts store ``arguments.command`` as an argv LIST.
    The old ``str(list)`` conversion produced a Python repr with quotes and
    commas that could never match criterion text, so a genuinely green pytest
    receipt (r15 N-103: rc=0, "23 passed") was invisible to the deterministic
    anchor and degraded to TEST_COMMAND_MATCH_UNVERIFIED.  ``shlex.join``
    round-trips through the ``shlex.split(posix=True)`` in ``_normalize``.
    """
    items = [str(token) for token in tokens]
    try:
        return shlex.join(items)
    except (TypeError, ValueError):
        return " ".join(items)


def _command_from_value(value: Any) -> str:
    if isinstance(value, list):
        return _join_command_tokens(value)
    if value is not None and str(value).strip():
        return str(value)
    return ""


def _pytest_run_command(target: Any) -> str:
    """Fix P5#14: reconstruct ``pytest_run``'s fixed argv as canonical text.

    ``builtin_tools._pytest_run`` always execs
    ``[sys.executable, "-m", "pytest", str(target), "-q"]``, so a
    whitespace-free ``target`` maps losslessly to
    ``python -m pytest {target} -q`` — the vocabulary acceptance criteria
    use.  A target containing whitespace (e.g. the mangled
    ``"deliverables/app/tests -q"`` passed as ONE argv token, r17
    R-TESTS-REPAIR, rc=4 "no tests ran") cannot round-trip through
    normalization without conflating token boundaries; refuse to guess and
    keep the observation honestly UNVERIFIED.
    """
    text = str(target or "").strip()
    if not text or any(character.isspace() for character in text):
        return ""
    return f"python -m pytest {text} -q"


def _receipt_command(receipt: Dict[str, Any], root: Optional[Path] = None) -> str:
    command = _literal_receipt_command(receipt)
    if root is None or not command:
        return command
    raw = receipt.get("raw_result")
    cwd = raw.get("cwd") if isinstance(raw, dict) else None
    if not cwd:
        arguments = receipt.get("arguments")
        try:
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (ValueError, TypeError):
            arguments = None
        cwd = arguments.get("cwd") if isinstance(arguments, dict) else None
    if not cwd:
        return command
    try:
        base = root.resolve()
        directory = Path(str(cwd))
        directory = (directory if directory.is_absolute() else base / directory).resolve()
        relative = directory.relative_to(base).as_posix()
        return _bind_test_command_paths(command, (), base, True, actual_cwd=relative)[0]
    except (ValueError, OSError):
        return ""


def _literal_receipt_command(receipt: Dict[str, Any]) -> str:
    top_level = _command_from_value(receipt.get("command"))
    if top_level:
        return top_level
    raw_arguments = receipt.get("arguments")
    if raw_arguments:
        try:
            parsed = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            if isinstance(parsed, dict):
                # Fix P5#14: pytest_run carries no command key — reconstruct
                # the tool's fixed argv template from its target instead.
                if str(receipt.get("tool") or "") == "pytest_run":
                    return _pytest_run_command(parsed.get("target"))
                for key in ("command", "argv", "code"):
                    extracted = _command_from_value(parsed.get(key))
                    if extracted:
                        return extracted
                return ""
        except (ValueError, TypeError):
            return str(raw_arguments)
    raw_result = receipt.get("raw_result")
    if isinstance(raw_result, dict):
        return _command_from_value(raw_result.get("command"))
    return ""


def _split_segments(command: str) -> Optional[List[str]]:
    """Quote-aware split on shell separators; None when provenance is unclear."""
    if command.count('"') % 2 or command.count("'") % 2:
        return None
    if "\\\n" in command or re.search(r"\\[;&|]", command):
        return None
    segments: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
            current.append(char)
        elif command[index:index + 2] in {"&&", "||"}:
            segments.append("".join(current))
            current = []
            index += 1
        elif char in {";", "|", "&", "\n"}:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _shell_control_signature(command: str) -> Optional[Tuple[str, ...]]:
    """Return unquoted shell control operators, or ``None`` if ambiguous.

    A passing summary is attributable only when the criterion declares the
    same control-flow shape as the executed command. Otherwise a trailing
    ``echo``/``true``, a pipeline, background execution, or a short-circuited
    prefix can manufacture exit status and output independently of the named
    test command. This lexical check deliberately fails closed instead of
    attempting to interpret a shell program.
    """
    operators: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        pair = command[index:index + 2]
        if pair in {"&&", "||"}:
            operators.append(pair)
            index += 2
            continue
        if char in {";", "|", "&", "\n"}:
            operators.append(char)
        index += 1
    return None if quote is not None else tuple(operators)


def _success_provenance_matches(criterion_command: str, receipt_command: str) -> bool:
    """Whether a successful receipt has the criterion's declared shell flow."""
    expected_signature = _shell_control_signature(_without_terminal_semicolon(criterion_command))
    actual_signature = _shell_control_signature(_without_terminal_semicolon(receipt_command))
    if expected_signature is None or actual_signature is None:
        return False
    if expected_signature != actual_signature:
        return False
    expected_segments = _split_segments(criterion_command)
    actual_segments = _split_segments(receipt_command)
    return (
        expected_segments is not None
        and actual_segments is not None
        and len(expected_segments) == len(actual_segments)
    )


def _without_terminal_semicolon(command: str) -> str:
    text = command.rstrip()
    signature = _shell_control_signature(text)
    if text.endswith(";") and signature and signature[-1] == ";" and not text.endswith(";;"):
        return text[:-1]
    return text


def _normalize(segment: str) -> str:
    try:
        tokens = shlex.split(segment.replace("\n", " "), posix=True)
        # Pytest reporting/concurrency flags do not change selected tests.
        runner_end = 1 if tokens and Path(tokens[0]).name.lower() in {"pytest", "pytest.exe"} else 3 if tokens[1:3] == ["-m", "pytest"] else 0
        if runner_end:
            normalized = tokens[:runner_end]
            index = runner_end
            while index < len(tokens):
                token = tokens[index]
                if token in {"-q", "-v", "-vv", "--quiet", "--verbose", "--disable-warnings"} or token.startswith(("--tb=", "--dist=", "--maxfail=")) or re.fullmatch(r"-(?:n\d+|r[A-Za-z]+)", token):
                    index += 1
                    continue
                if token in {"--tb", "--dist", "--maxfail", "-n", "-r"} and index + 1 < len(tokens):
                    index += 2
                    continue
                normalized.append(token)
                index += 1
            tokens = normalized
        # unittest's explicit default pattern and verbosity do not change its
        # executed test set. A narrower pattern remains a different command.
        if len(tokens) >= 4 and tokens[1:4] == ["-m", "unittest", "discover"]:
            normalized = tokens[:4]
            index = 4
            while index < len(tokens):
                if tokens[index] in {"-v", "-q", "--verbose", "--quiet"}:
                    index += 1
                    continue
                if tokens[index:index + 2] in [["-p", "test_*.py"], ["--pattern", "test_*.py"]]:
                    index += 2
                    continue
                normalized.append(tokens[index])
                index += 1
            tokens = normalized
        return " ".join(tokens)
    except ValueError:
        return " ".join(segment.split())


def _executable_of(segment: str) -> str:
    normalized = _normalize(segment)
    tokens = normalized.split(" ")
    for token in tokens:
        bare = token.strip("\"'")
        if bare:
            return Path(bare).name.lower()
    return ""


def _commands_match(criterion_command: str, receipt_command: str, *, allow_extra_args: bool = True) -> Optional[bool]:
    """Structural match; None when provenance cannot be established."""
    criterion_segments = _split_segments(criterion_command)
    receipt_segments = _split_segments(receipt_command)
    if criterion_segments is None or receipt_segments is None:
        return None
    wanted = [_normalize(segment) for segment in criterion_segments]
    wanted = [segment for segment in wanted if segment]
    if not wanted:
        return None
    executable_pool = [
        _executable_of(segment) for segment in receipt_segments
    ]
    if all(name in _PRINT_EXECUTABLES for name in executable_pool if name):
        # The receipt only printed the criterion text; nothing was executed.
        return False
    for wanted_segment in wanted:
        hit = any(
            _normalize(segment) == wanted_segment
            or (allow_extra_args and " -m unittest" not in wanted_segment
                and _normalize(segment).startswith(wanted_segment + " "))
            for segment in receipt_segments
            if _executable_of(segment) not in _PRINT_EXECUTABLES
        )
        if not hit:
            return False
    return True


EXECUTION_CRITERION_GUIDANCE = (
    '文档文字要求使用 file_contains:{"path":"deliverables/app/README.md","text":"python -m unittest -v"}；这是跨平台的精确文本断言，不需要 grep。不要把未确认安装的 grep/bash/Select-String 命令当作跨平台文件内容判据。\n'
    '普通命令与测试必须分开：command_succeeded:<确切命令> 只验证真实执行且退出码为 0；'
    '若要求特定输出，使用 command_output:{"command":"python main.py 2 3","stdout":"5"}，'
    '同时检查退出码 0 与 stdout 精确匹配（仅忽略末尾换行）。'
    '只有测试运行器或包含真实断言并输出测试计数的脚本使用 tests_passed:<确切测试命令>。'
    '禁止将普通业务命令声明为 tests_passed，禁止为了满足摘要格式修改业务输出。'
    '三者都必须锚定真实工具收据，文件版本变更后旧证据失效；普通命令通过不代表测试通过。'
    '工具命令以运行根为基准，可使用 deliverables/app 路径；交付源码和测试内部的路径必须可迁移，'
    '例如通过 pathlib.Path(__file__).resolve().parent 定位同目录文件，不能写死运行根或 deliverables/ 前缀。'
    'README 中的用户命令应从交付项目目录运行，发布后仍必须可用。'
    'unittest 的 -s/-p/-t 是 discover 子命令参数，必须写 python -m unittest discover ...，'
    '不能将这些参数直接附在 python -m unittest -v 后面。'
)


def render_execution_contract(criteria: Sequence[str], *, declared: Sequence[Any] = (), root: Any = "") -> str:
    """Teach the deterministic gate before execution, using its own parser."""
    criteria, _ = bind_criterion_paths(criteria, declared=declared, root=root, strict=False)
    commands = [payload for item in criteria
                for kind, payload in [parse_criterion(item)] if kind == KIND_TESTS_PASSED]
    ordinary = [item for item in criteria if parse_criterion(item)[0] in
                {KIND_COMMAND_SUCCEEDED, KIND_COMMAND_OUTPUT}]
    prefix = ("命令执行证据契约（执行前）：\n" + EXECUTION_CRITERION_GUIDANCE
              + "\n" + "\n".join("- " + item for item in ordinary) + "\n") if ordinary else ""
    if not commands:
        return prefix
    return prefix + (
        "测试执行证据契约（执行前）：\n"
        + "\n".join("- " + str(command) for command in dict.fromkeys(commands))
        + "\n这些 tests_passed 命令必须真实执行并由工具回执锚定；退出码 0、HTTP 200、"
        "文件存在或 Worker 自述都不能单独证明测试通过。自定义检查脚本必须执行真实断言，"
        "失败以非零退出码报告；成功时在 stdout 末尾输出实际计数，例如 "
        "{\"passed\": 3, \"failed\": 0}（数字必须来自本次实际检查，不得照抄示例）。"
        "不得用 echo、包装输出或改写旧回执伪造通过摘要。标准测试运行器保留其原生摘要。"
        "完成实现修改后再执行锚定命令；后续修改被跟踪文件会使旧通过证据过期。\n"
    )




def _summary_verdict(output_tail: str) -> Optional[bool]:
    """True pass-shape, False fail-shape, None no recognized test summary."""
    # Fix P5#17b: neutralize zero-count failure mentions before shape
    # matching (see _ZERO_FAIL_MENTION_RE); "13 passed, 0 failed" and
    # JSON ``"failed": 0`` are pass-side evidence, not failures.
    tail = _ZERO_FAIL_MENTION_RE.sub(" ", output_tail)
    # A subprocess may combine several test runners. A pytest pass must not
    # hide a later unittest/TAP failure merely because its vocabulary is first.
    # Inspect every failure shape before accepting any positive summary.
    if any(fail_shape.search(tail) for _, fail_shape in _SUMMARY_PATTERNS):
        return False
    if any(pass_shape.search(tail) for pass_shape, _ in _SUMMARY_PATTERNS):
        return True
    return None


def _latest_snapshot(receipts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    for receipt in reversed(list(receipts)):
        files = receipt.get("files")
        if isinstance(files, dict) and files:
            return files
    return {}


def _check_execution_claim(
    command: str,
    receipts: Sequence[Dict[str, Any]],
    latest_files: Optional[Dict[str, Any]],
    premise_versions: Optional[Dict[str, str]] = None,
    *,
    require_test_summary: bool = True,
    expected_stdout: Optional[str] = None,
    root: Optional[Path] = None,
    explicit_inputs: Sequence[str] = (),
) -> Tuple[bool, Optional[bool], str, str, List[str], Dict[str, str]]:
    ambiguous = False
    anchor: Optional[Dict[str, Any]] = None
    anchor_success_provenance = False
    refused: List[str] = []
    for receipt in receipts:
        if str(receipt.get("tool") or "") not in _EXEC_TOOLS:
            continue
        receipt_command = _receipt_command(receipt, root)
        if not receipt_command:
            continue
        if require_test_summary and _normalize(command) in {"pytest", "python -m pytest", "python -m unittest"} and _normalize(receipt_command).startswith(_normalize(command) + " "):
            # A bare runner means default discovery; a narrower selection does
            # not establish that discovery, even with a genuine passing tail.
            ambiguous = True
            continue
        match = _commands_match(command, receipt_command, allow_extra_args=require_test_summary)
        if match is None:
            ambiguous = True
            continue
        if not match:
            continue
        if is_guard_blocked(receipt):
            # Fix r29 (doc 51 §16): the ledger records refused calls, and this
            # loop's last-decisive-match rule let such a refusal out-rank the
            # real passing execution recorded earlier in the same execution.
            # A refused call never ran: skip it, but keep its id so an execution
            # with ONLY refusals fails explicitly instead of degrading to
            # "not found" or, worse, being read as a passing attempt.
            refused.append(str(receipt.get("receipt_id") or receipt.get("call_id") or ""))
            continue
        if _negation_overlaps_criterion(command, receipt_command):
            # Fix BB: the receipt negated a target this criterion names, so it
            # cannot anchor a pass.  Fall through to UNVERIFIED rather than
            # claiming an execution that did not actually exercise the target.
            ambiguous = True
            continue
        # Fix P5#18 (r17-resume#3 live, 2026-10-03, gen-8 R-SEED-CANON-INPROC-
        # ONESHOT field evidence): the FIRST decisive match used to decide and
        # return immediately, so an early dev-trial receipt (rc=1) permanently
        # poisoned the criterion even though the authoritative final anchored
        # execution (rc=0, passing summary) was recorded later in the same
        # frozen set — receipt 70f07d1a (rc=1) anchored AC07 to
        # TEST_EXECUTION_FAILED while receipt be1d579f (same argv, rc=0) was
        # never consumed; the gen-6 worker had predicted exactly this from a
        # read-only load of this module.  Contracts universally place the
        # anchored execution as the LAST top-level command after all writes,
        # so the LAST decisive match is the authoritative anchor; symmetrically
        # a later failed run supersedes an earlier pass (staleness principle).
        anchor = receipt
        anchor_success_provenance = _success_provenance_matches(
            command, receipt_command
        )
    if anchor is None:
        if refused:
            blocked_ids = sorted({item for item in refused if item})
            return True, False, "TEST_EXECUTION_BLOCKED", (
                "every matching attempt was refused by the tool-loop guard "
                f"({', '.join(blocked_ids[:5])}); a refused call is not an "
                "execution, so no test result exists for this criterion"
            ), blocked_ids[:5], {}
        if ambiguous:
            return False, None, "TEST_COMMAND_MATCH_UNVERIFIED", (
                "recorded commands exist but shell provenance cannot be established"
            ), [], {}
        return False, None, "TEST_EXECUTION_NOT_FOUND", (
            "no recorded execution matches this command; self-reported text is not evidence"
        ), [], {}
    receipt = anchor
    receipt_id = str(receipt.get("receipt_id") or receipt.get("call_id") or "")
    status = str(receipt.get("status") or "").lower()
    returncode = receipt.get("returncode")
    raw_files = receipt.get("files")
    files: Dict[str, Any] = raw_files if isinstance(raw_files, dict) else {}
    versions = functional_file_versions(files, explicit_inputs=[*explicit_inputs, *path_tokens(command)], root=root)
    if status != "success" or (returncode is not None and int(returncode) != 0):
        return True, False, "TEST_EXECUTION_FAILED", (
            f"anchored execution returned status={status or 'unknown'} "
            f"returncode={returncode}"
        ), [receipt_id], versions
    stdout_tail = str(receipt.get("stdout_tail") or receipt.get("output") or "")
    if not require_test_summary:
        if returncode is None:
            return False, None, "COMMAND_EXIT_CODE_UNAVAILABLE", (
                "ordinary command acceptance requires a recorded exit code"
            ), [receipt_id], versions
        raw_result = receipt.get("raw_result")
        full_stdout = raw_result.get("stdout") if isinstance(raw_result, dict) else None
        if expected_stdout is not None and not isinstance(full_stdout, str) and len(stdout_tail) >= 1500:
            return False, None, "COMMAND_OUTPUT_UNAVAILABLE", (
                "a possibly truncated stdout tail cannot prove exact full output"
            ), [receipt_id], versions
        observed_stdout = full_stdout if isinstance(full_stdout, str) else stdout_tail
        if expected_stdout is not None and observed_stdout.rstrip("\r\n") != expected_stdout.rstrip("\r\n"):
            return True, False, "COMMAND_OUTPUT_MISMATCH", (
                "recorded stdout does not equal the explicitly declared expected output"
            ), [receipt_id], versions
    # ``unittest`` writes its ENTIRE report -- including the closing ``OK`` /
    # ``FAILED (...)`` line that the vocabulary names -- to STDERR, and the
    # receipt already captures it as ``stderr_tail``.  Searching stdout alone
    # made any ``python -m unittest ...`` criterion unsatisfiable by
    # construction: live b33case1-fix3 n-be-01 anchored
    # ``python -m unittest discover -s deliverables/tests/api -v``, got
    # returncode 0 with ``stdout_tail`` empty and
    # ``stderr_tail = "Ran 30 tests in 0.885s\n\nOK"`` -- and was rejected.
    #
    # stdout and stderr are one execution result. Any recognized failure
    # vetoes a passing summary in the other stream.
    if not require_test_summary and not anchor_success_provenance:
        return False, None, "TEST_COMMAND_MATCH_UNVERIFIED", (
            "the recorded command adds undeclared shell control flow; its "
            "exit status and output cannot be attributed to the criterion"
        ), [receipt_id], versions
    if require_test_summary:
        stderr_tail = str(receipt.get("stderr_tail") or "")
        both_streams = stdout_tail + "\n" + stderr_tail
        if _ZERO_TEST_SHAPE_RE.search(both_streams):
            # Fix BB: zero executed tests is a decided FAIL, not a pass — a
            # ``0 passed`` tail otherwise satisfied the pass shape and (since
            # Fix BA) could even carry an aborted turn's evidence forward.
            return True, False, "TEST_SUMMARY_ZERO_TESTS", (
                "recognized test summary reports zero tests executed; "
                "zero is not a pass"
            ), [receipt_id], versions
        verdict = _summary_verdict(both_streams)
        if verdict is not False and not anchor_success_provenance:
            return False, None, "TEST_COMMAND_MATCH_UNVERIFIED", (
                "recorded shell control flow cannot establish the requested execution"
            ), [receipt_id], versions
        if verdict is None:
            # Fix P5#17 (r17-resume#3 live, 2026-10-03 04:17/04:40): two
            # consecutive lineages (R-UI-STARTUP-STABILITY, then its takeover
            # R-SEED-CHECK-READONLY) had 0.98-approved verdicts force-overridden
            # to inconclusive by this gate because their custom check scripts
            # printed proprietary SUMMARY formats ("SUMMARY rounds=2
            # ready=2/2 ...").  The former detail text only said the summary
            # was unrecognized — never WHICH vocabularies are — so repair
            # workers had to guess the format, burning recovery cycles toward
            # the no_progress breaker.  Enumerate the recognized shapes; this
            # text propagates through verifier.exception → repair context →
            # recovery goal injection, teaching a deterministic fix.
            return False, None, "TEST_SUMMARY_NOT_RECOGNIZED", (
                "anchored execution has no recognized test summary in its "
                "output tail; exit code 0 alone is not test evidence; the "
                "command's own stdout must contain a recognized summary "
                "line, e.g. " + SUMMARY_EVIDENCE_FORMS +
                "; make the check script print one of these on success"
            ), [receipt_id], versions
        if verdict is False:
            return True, False, "TESTS_REPORTED_FAILURES", (
                "recognized test summary reports failures"
            ), [receipt_id], versions
    baseline = latest_files if latest_files is not None else _latest_snapshot(receipts)
    stale = [
        path for path, digest in versions.items()
        if digest and (
            (latest_files is not None and path not in baseline)
            or (isinstance(baseline.get(path), dict)
                and str(baseline[path].get("sha256") or "") != digest)
        )
    ]
    if premise_versions:
        # Fix r29: compare the anchor's recorded versions against the CURRENT
        # versions of the declared premises (tracked files plus external upstream
        # artifacts).  A premise the receipt never recorded cannot confirm a pass
        # when the receipt carries no artifact snapshot at all.
        stale.extend(
            path for path, digest in premise_versions.items()
            if path in versions and digest and versions[path] != digest
        )
        if not files:
            stale.extend(path for path in premise_versions if path not in versions)
    stale = sorted(set(stale))
    if stale:
        return True, False, "TEST_EXECUTION_STALE", (
            "owned files changed after the anchored execution: "
            + ", ".join(sorted(stale)[:5])
        ), [receipt_id], versions
    return True, True, "", ("anchored to a recorded successful execution with a passing test summary"
                             if require_test_summary else "anchored to recorded successful command execution"), [receipt_id], versions


#: Fix BA (doc 26 §41, 2026-09-18 r13b) — DeerFlow 2.0 status/stop_reason split.
#: ``deer-flow@2814bd5d498a subagents/status_contract.py`` (#3875 Phase 2): a run
#: ended by a guardrail cap that still produced usable output stays
#: ``status=completed`` and carries the cause on ``stop_reason``; only a capped
#: run with NO usable output is ``failed``.  AstralNexus's turn boundary derived
#: the node status from the raw execution boolean alone, so verified work was
#: discarded and redone until the run-level churn budget was gone.
TURN_STOP_REASON_ABORTED_WITH_EVIDENCE = "ABORTED_WITH_VERIFIABLE_EVIDENCE"


def usable_partial_evidence(observations: Sequence[Dict[str, Any]]) -> bool:
    """Whether an abnormally ended turn already produced code-verifiable output.

    Fail-closed: at least one criterion was decided IN CODE and holds, and no
    decided criterion fails.  Criteria that are undecidable (``checked=False``)
    or decided without a boolean verdict (``holds`` is ``None``) neither help
    nor hurt — the semantic layer and the runtime gate still own final
    acceptance.  An empty or all-UNVERIFIED observation list is NOT usable
    evidence, so a node that cannot complete a single verifiable step keeps
    failing exactly as before (project rule 不能增加次数掩盖问题).
    """
    decided = [
        item
        for item in observations
        if item.get("checked") and isinstance(item.get("holds"), bool)
    ]
    if not decided:
        return False
    if any(not item.get("holds") for item in decided):
        return False
    return True


#: Explicit, reviewable whitelist for test-class paths.  ``is_report_class_path``
#: cannot answer this question: it returns False for every source suffix
#: (``runtime/verification_identity.py:163-164``), which is right for report
#: classification but makes ``tests/test_app.py`` and ``deliverables/app/app.py``
#: the same class.  These rules are closed on purpose — a substring test on
#: "test" would classify ``contest.py`` as test code.
TEST_CLASS_DIR_SEGMENTS = frozenset({"tests", "fixtures"})
TEST_CLASS_FILE_NAMES = frozenset({"conftest.py"})


def is_test_class_path(path: Any) -> bool:
    """Whether a path is test code/test input rather than deliverable product."""
    text = str(path or "").strip().replace("\\", "/").lower()
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return False
    name = parts[-1]
    if any(segment in TEST_CLASS_DIR_SEGMENTS for segment in parts[:-1]):
        return True
    if name in TEST_CLASS_FILE_NAMES:
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith("_test.py")


#: Closed criterion text and headline per write-leaf reason.
_WRITE_LEAF_CRITERIA = {
    "WRITE_NO_PRODUCT_CHANGE": "a repair round must change product bytes, not republish identical ones",
    "WRITE_CHANGE_UNKNOWN": "a repair round's write outcome must be auditable, not silently unread",
    "WRITE_FAILED_BUT_MODIFIED": "a failed write must not modify bytes on disk",
    "WRITE_ONLY_TEST_CHANGE": "a repair must change product code, not only test code",
    "WRITE_ONLY_REPORT_CHANGE": "a repair must change product code, not only report-class paths",
    "WRITE_LANDED": "a product-path write changed bytes on disk",
}
_WRITE_LEAF_HEADLINES = {
    "WRITE_NO_PRODUCT_CHANGE": "every product-path write left the bytes unchanged",
    "WRITE_CHANGE_UNKNOWN": "product write outcome is not decidable",
    "WRITE_FAILED_BUT_MODIFIED": "a failed write still modified bytes on disk",
    "WRITE_ONLY_TEST_CHANGE": "only test-class paths changed in this repair round",
    "WRITE_ONLY_REPORT_CHANGE": "only report-class paths changed in this repair round",
    "WRITE_LANDED": "a product-path write changed bytes on disk",
}
#: Reasons that make the deterministic dimension fail (holds=False).
WRITE_LEAF_BLOCKING_REASONS = frozenset({
    "WRITE_NO_PRODUCT_CHANGE",
    "WRITE_FAILED_BUT_MODIFIED",
})

#: Field whitelist for write-leaf details.  ``request_facts`` /
#: ``write_receipt_facts`` never record the requested content, and this list
#: keeps a whole-fact dump out of the observation as well: paths, digests, byte
#: counts and booleans only (the ``content_recorded`` invariant).
_WRITE_DETAIL_FIELDS: Tuple[str, ...] = (
    "operation",
    "change_kind",
    "status",
    "returncode",
    "previous_sha256",
    "previous_bytes",
    "landed_sha256",
    "landed_bytes",
    "request_sha256",
    "request_bytes",
    "landed_matches_request",
    "landed_bytes_consistent",
    "failed_write_modified",
)
_WRITE_DETAIL_MAX_CHARS = 460
_WRITE_DETAIL_MAX_FACTS = 3
_WRITE_NO_FACT_HEADLINE = (
    "no file_write fact in this repair execution; the fix may have landed "
    "through python_exec/terminal_exec"
)


def _tri_state(value: Any) -> Optional[bool]:
    """``True``/``False``/``None`` — never a truthy coercion of a non-bool."""
    return value if isinstance(value, bool) else None


def _write_fact_path(
    write: Dict[str, Any],
    receipt: Dict[str, Any],
    *,
    declared_path: str,
    root: Optional[Path],
) -> str:
    """Run-relative path named by one write fact; "" when nothing names a path."""
    for candidate in (write.get("path"), receipt.get("path"), declared_path):
        text = str(candidate or "").strip()
        if not text:
            continue
        if root is not None:
            relative = relative_receipt_path(text, root)
            if relative:
                return relative
        return text.replace("\\", "/")
    return ""


def _short(value: Any, limit: int = 12) -> str:
    text = str(value)
    return text[:limit] + "..." if len(text) > limit else text


def _write_fact_line(fact: Dict[str, Any]) -> str:
    parts = [f"path={fact.get('path') or '?'}"]
    for key in _WRITE_DETAIL_FIELDS:
        value = fact.get(key)
        if value is None or value == "":
            continue
        if key.endswith("_sha256"):
            value = _short(value)
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _write_detail(headline: str, facts: Sequence[Dict[str, Any]]) -> str:
    """Compact auditable detail: headline plus at most three whitelisted facts."""
    lines = [_write_fact_line(fact) for fact in facts[:_WRITE_DETAIL_MAX_FACTS]]
    omitted = len(facts) - len(lines)
    if omitted > 0:
        lines.append(f"(+{omitted} more write fact(s))")
    text = "; ".join([headline] + lines)
    return text if len(text) <= _WRITE_DETAIL_MAX_CHARS else text[:_WRITE_DETAIL_MAX_CHARS] + "..."


def _write_facts_from_receipts(
    receipts: Sequence[Dict[str, Any]],
    *,
    path: str,
    root: Optional[Path],
) -> List[Dict[str, Any]]:
    """One classified write fact per receipt that actually carries a ``write`` map."""
    facts: List[Dict[str, Any]] = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        write = receipt.get("write")
        if not isinstance(write, dict):
            # Inference from absence is forbidden (DESIGN_R5_WRITE_FACTS §3, R-W6):
            # a receipt with no write key is not evidence of a failed write.
            continue
        resolved = _write_fact_path(write, receipt, declared_path=path, root=root)
        if is_test_class_path(resolved):
            category = "test"
        elif is_report_class_path(resolved):
            category = "report"
        elif resolved:
            category = "product"
        else:
            category = "unclassified"
        fact: Dict[str, Any] = {
            "category": category,
            "path": resolved,
            "changed": _tri_state(write.get("changed")),
            "landed_matches_request": _tri_state(write.get("landed_matches_request")),
            "failed_write_modified": write.get("failed_write_modified") is True,
            "receipt_id": str(receipt.get("receipt_id") or receipt.get("call_id") or ""),
        }
        for key in _WRITE_DETAIL_FIELDS:
            fact[key] = write.get(key)
        facts.append(fact)
    return facts


_CRITERION_NODE_RE = re.compile(r"^(?P<node>.+?)\.(?:AC|EO|C)\d{2}(?:\.\d+)?$")


def _criterion_node_id(criteria: Sequence[Any]) -> str:
    """Node prefix of the conventional ``<node>.<AC|EO|C>NN`` criterion id.

    ``core/verifier.py:564,585`` builds exactly that shape, so a write leaf is
    numbered ``<node>.WR01``.  Without a conventional id the leaf is numbered
    ``WR01`` alone instead of inventing a node.
    """
    for item in criteria:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            match = _CRITERION_NODE_RE.match(str(item[0]).strip())
            if match:
                return match.group("node")
    return ""


def write_change_observations(
    *,
    receipts: Sequence[Dict[str, Any]],
    node_id: str,
    repair_round: bool,
    path: str = "",
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Deterministic write-fact leaves for a repair round (R5).

    Decision rule — a deliberate, documented deviation from
    DESIGN_R5_WRITE_FACTS §3, which also blocked on ``changed is None``:

      * BLOCKING (``checked=True``, ``holds=False``, so the deterministic
        dimension fails and the node can never be approved):
        - ``WRITE_NO_PRODUCT_CHANGE``: every product-path write fact of this
          execution has ``changed is False``, no product-path write has
          ``changed is True``, and none has a landed-request match.
        - ``WRITE_FAILED_BUT_MODIFIED``: a fact reports
          ``failed_write_modified is True``; its own leaf, never swallowed.
      * NON-BLOCKING but explicitly auditable (``checked=False``,
        ``holds=None``, distinct reason, visible in the observations and the
        trace, never silent): ``WRITE_CHANGE_UNKNOWN`` (product writes exist but
        none is decidable, or the round produced no product write fact at all —
        the fix may have landed through ``python_exec``/``terminal_exec``),
        ``WRITE_ONLY_TEST_CHANGE``, ``WRITE_ONLY_REPORT_CHANGE``;
        ``WRITE_LANDED`` is ``checked=True`` / ``holds=True``.

    Why ``None`` must not block: ``runtime/execution_ledger.py:62`` baselines a
    write against the PREVIOUS observation's files, so the FIRST file_write of
    an execution has an empty baseline and ``runtime/file_write_facts.py:163-164``
    yields ``changed=None`` / ``change_kind="unknown"`` — and in a repair
    execution that first write is usually the fix.  Blocking on it would invent
    a failure from data that cannot decide, stopping a legitimate repair; only
    definite no-op evidence blocks.  A non-repair round yields no leaf at all,
    and a receipt carrying no ``write`` key yields the audit leaf at most, never
    a failed-write leaf.
    """
    if not repair_round:
        return []
    facts = _write_facts_from_receipts(receipts, path=path, root=root)
    product = [fact for fact in facts if fact["category"] == "product"]
    test_facts = [fact for fact in facts if fact["category"] == "test"]
    report_facts = [fact for fact in facts if fact["category"] == "report"]
    failed = [fact for fact in facts if fact["failed_write_modified"]]
    landed = [fact for fact in product if fact["changed"] is True]
    definite_no_op = (
        bool(product)
        and all(fact["changed"] is False for fact in product)
        and not any(fact["changed"] is True for fact in product)
        and not any(fact["landed_matches_request"] is True for fact in product)
    )

    plan: List[Tuple[str, bool, Optional[bool], str, List[Dict[str, Any]]]] = []
    if failed:
        plan.append((
            "WRITE_FAILED_BUT_MODIFIED", True, False,
            _WRITE_LEAF_HEADLINES["WRITE_FAILED_BUT_MODIFIED"], failed,
        ))
    if landed:
        plan.append((
            "WRITE_LANDED", True, True,
            _WRITE_LEAF_HEADLINES["WRITE_LANDED"], landed,
        ))
    if definite_no_op:
        plan.append((
            "WRITE_NO_PRODUCT_CHANGE", True, False,
            _WRITE_LEAF_HEADLINES["WRITE_NO_PRODUCT_CHANGE"], product,
        ))
    if product and not landed and not definite_no_op:
        undecided = [fact for fact in product if fact["changed"] is None]
        matched = [
            fact for fact in product
            if fact["changed"] is not True and fact["landed_matches_request"] is True
        ]
        notes = []
        if undecided:
            notes.append(f"{len(undecided)} product write(s) have no baseline (changed=None)")
        if matched:
            notes.append("product bytes equal the request while the baseline reports no change")
        headline = _WRITE_LEAF_HEADLINES["WRITE_CHANGE_UNKNOWN"]
        if notes:
            headline = f"{headline}: " + "; ".join(notes)
        plan.append((
            "WRITE_CHANGE_UNKNOWN", False, None, headline,
            undecided or matched or product,
        ))
    if not product:
        if test_facts and report_facts:
            plan.append((
                "WRITE_CHANGE_UNKNOWN", False, None,
                _WRITE_LEAF_HEADLINES["WRITE_CHANGE_UNKNOWN"] + ": only test-class and report-class paths changed",
                test_facts + report_facts,
            ))
        elif test_facts:
            plan.append((
                "WRITE_ONLY_TEST_CHANGE", False, None,
                _WRITE_LEAF_HEADLINES["WRITE_ONLY_TEST_CHANGE"], test_facts,
            ))
        elif report_facts:
            plan.append((
                "WRITE_ONLY_REPORT_CHANGE", False, None,
                _WRITE_LEAF_HEADLINES["WRITE_ONLY_REPORT_CHANGE"], report_facts,
            ))
        else:
            plan.append((
                "WRITE_CHANGE_UNKNOWN", False, None,
                _WRITE_NO_FACT_HEADLINE, facts,
            ))

    leaves: List[Dict[str, Any]] = []
    for index, (reason, checked, holds, headline, items) in enumerate(plan, 1):
        criterion_id = f"{node_id}.WR{index:02d}" if node_id else f"WR{index:02d}"
        receipt_ids = [fact["receipt_id"] for fact in items if fact["receipt_id"]]
        leaves.append(_observation(
            criterion_id,
            _WRITE_LEAF_CRITERIA[reason],
            KIND_WRITE_LANDED,
            "",
            checked=checked,
            holds=holds,
            reason=reason,
            detail=_write_detail(headline, items),
            receipt_ids=receipt_ids,
        ))
    return leaves


def run_acceptance_checks(
    criteria: Sequence[Any],
    *,
    receipts: Sequence[Dict[str, Any]],
    execution_id: str = "",
    root: Optional[Path] = None,
    latest_files: Optional[Dict[str, Any]] = None,
    reader: Optional[Callable[[Path, str], Tuple[Optional[int], Optional[bytes], Optional[str]]]] = None,
    premise_versions: Optional[Dict[str, str]] = None,
    declared_premises: Sequence[str] = (),
    write_facts_enabled: bool = False,
) -> List[Dict[str, Any]]:
    """Evaluate decidable criteria in code; undecidable ones stay UNVERIFIED.

    ``criteria`` items are plain strings or ``(criterion_id, text)`` pairs.
    ``receipts`` are ExecutionStore receipt payloads for this execution.
    ``write_facts_enabled`` (R5, default off) appends the repair-round
    write-fact leaves; every existing caller keeps byte-identical behaviour.
    """
    observations: List[Dict[str, Any]] = []
    for index, item in enumerate(criteria, 1):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            criterion_id, criterion = str(item[0]), str(item[1])
        else:
            criterion_id, criterion = criterion_leaf_id("", index), str(item)
        kind, payload = parse_criterion(criterion)
        if kind in EXECUTION_KINDS and declared_premises:
            rebound, _ = bind_criterion_paths(
                [criterion], declared=declared_premises, root=root, strict=False,
            )
            kind, payload = parse_criterion(rebound[0])
        try:
            if kind == KIND_UNDECIDABLE or not payload:
                observations.append(_observation(
                    criterion_id, criterion, KIND_UNDECIDABLE, execution_id,
                    checked=False, holds=None, reason="UNDECIDABLE_IN_CODE",
                    detail="criterion is not code-decidable; semantic judgment required",
                ))
                continue
            if kind == KIND_FILE_CONTAINS:
                if payload.get("invalid"):
                    checked, holds, reason, detail = True, False, "INVALID_FILE_CONTENT_CRITERION", "Invalid file_contains specification"
                elif root is None:
                    checked, holds, reason, detail = False, None, "WORKSPACE_UNAVAILABLE", "No workspace"
                else:
                    _size, raw, error = (reader or _default_reader)(root, payload["path"])
                    if error == "FILE_NOT_FOUND":
                        checked, holds, reason, detail = True, False, error, payload["path"]
                    elif error or raw is None:
                        checked, holds, reason, detail = False, None, error or "FILE_CONTENT_UNAVAILABLE", payload["path"]
                    else:
                        try:
                            haystack = raw.decode("utf-8")
                            needle = payload["text"]
                            if not payload.get("case_sensitive", True):
                                haystack, needle = haystack.casefold(), needle.casefold()
                            holds = needle in haystack
                            checked, reason, detail = True, "" if holds else "FILE_CONTENT_MISMATCH", payload["path"]
                        except UnicodeDecodeError:
                            checked, holds, reason, detail = False, None, "FILE_CONTENT_UNAVAILABLE", "Not UTF-8 text"
                observations.append(_observation(criterion_id, criterion, kind, execution_id,
                                                 checked=checked, holds=holds, reason=reason, detail=detail))
                continue
            if kind in EXECUTION_KINDS:
                if kind == KIND_COMMAND_OUTPUT and payload.get("invalid"):
                    observations.append(_observation(
                        criterion_id, criterion, kind, execution_id,
                        checked=True, holds=False, reason="INVALID_EXECUTION_CRITERION",
                        detail="command_output requires JSON with exactly command and stdout strings",
                    ))
                    continue
                command = payload["command"] if kind == KIND_COMMAND_OUTPUT else str(payload)
                checked, holds, reason, detail, receipt_ids, versions = _check_execution_claim(
                    command, receipts, latest_files, premise_versions,
                    require_test_summary=kind == KIND_TESTS_PASSED,
                    expected_stdout=payload["stdout"] if kind == KIND_COMMAND_OUTPUT else None,
                    root=root, explicit_inputs=declared_premises,
                )
                verification: Optional[Dict[str, Any]] = None
                if premise_versions is not None or declared_premises:
                    # Fix r29 (user step ③): hand the verifier the same identity
                    # the guard used — which criterion, which anchor receipt,
                    # which attempts were refused, and which premise versions and
                    # verification identity the verdict belongs to.
                    verification = {
                        "criterion_id": criterion_id,
                        "command": command,
                        "execution_id": execution_id,
                        "anchor_receipt_ids": [str(item) for item in receipt_ids],
                        "guard_refused_receipt_ids": [
                            str(item.get("receipt_id") or item.get("call_id") or "")
                            for item in receipts
                            if is_guard_blocked(item)
                            and str(item.get("tool") or "") in _EXEC_TOOLS
                            and _commands_match(command, _receipt_command(item, root)) is True
                        ],
                        "declared_premises": [str(item) for item in declared_premises],
                        "premise_versions": {
                            str(key): str(value)
                            for key, value in sorted((premise_versions or {}).items())
                        },
                        "verification_identity": verification_identity(command, premise_versions),
                    }
                observations.append(_observation(
                    criterion_id, criterion, kind, execution_id,
                    checked=checked, holds=holds, reason=reason, detail=detail,
                    receipt_ids=receipt_ids, artifact_versions=versions,
                    verification=verification,
                ))
                continue
            if kind == KIND_REPORT_NOT_STALE:
                checked, holds, reason, detail = _check_report_not_stale(payload, root, reader)
                observations.append(_observation(
                    criterion_id, criterion, kind, execution_id,
                    checked=checked, holds=holds, reason=reason, detail=detail,
                ))
                continue
            if kind == KIND_SHA_MATCHES:
                checked, holds, reason, detail = _check_sha_matches(payload, root, reader)
                observations.append(_observation(
                    criterion_id, criterion, kind, execution_id,
                    checked=checked, holds=holds, reason=reason, detail=detail,
                ))
                continue
            checked, holds, reason, detail = _check_file_leaf(kind, payload, root, reader)
            observations.append(_observation(
                criterion_id, criterion, kind, execution_id,
                checked=checked, holds=holds, reason=reason, detail=detail,
            ))
        except Exception as exc:  # leaves never raise into the caller
            observations.append(_observation(
                criterion_id, criterion, kind, execution_id,
                checked=False, holds=None, reason="CHECK_INTERNAL_ERROR",
                detail=str(exc),
            ))
    if write_facts_enabled:
        # R5: repair-round write facts.  Enabled only by a caller that already
        # knows this is a repair round with receipts (core/verifier.py:613-634);
        # the default keeps every existing caller byte-identical.
        write_leaves = write_change_observations(
            receipts=receipts,
            node_id=_criterion_node_id(criteria),
            repair_round=True,
            root=root,
        )
        for leaf in write_leaves:
            leaf["execution_id"] = execution_id
        observations.extend(write_leaves)
    return observations

