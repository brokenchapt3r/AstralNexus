"""Canonical output layout for one AstralNexus run.

The layout is deliberately independent of task domain.  A run is the unit of
reproducibility; every execution attempt below it is immutable and uniquely
addressable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def normalize_worker_output_path(raw: object) -> str:
    """Map planner paths into the run's contract-owned delivery partition."""
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        return value
    parts = [part for part in value.split("/") if part and part != "."]
    if not parts or ".." in parts:
        return value
    lowered = [part.lower() for part in parts]
    if lowered[0] == "deliverables":
        # A fully qualified delivery path is already a user/Planner contract.
        # Inserting an app/ partition changes the promised file location.
        return "/".join(parts)
    if lowered[0] == "app":
        return "/".join(["deliverables", *parts])
    if lowered[0] in {"docs", "documentation", "文档"}:
        return "/".join(["deliverables", "docs", *parts[1:]])
    if lowered[0] in {"runs", "evidence", "artifacts", "运行证据"}:
        return "/".join(["deliverables", "runs", *parts[1:]])
    if lowered[0] == "src":
        parts = parts[1:]
    return "/".join(["deliverables", "app", *parts])


# ---------------------------------------------------------------------------
# Fix S1 (b32_path_identity): ONE workspace path identity.
#
# Before this plane existed the framework answered "which file is this?" in
# nine independent places: normalize_worker_output_path() (declared outputs),
# literal run_dir joins (criterion leaves), str.startswith() prefix tests
# (contracts_overlap / _covers / _paths_conflict), a basename comparison
# (_clause_path_covered) and a filesystem same-name search (orchestrator
# root-cause miner).  A Planner writing ``docs/contract.md`` in
# acceptance_criteria while owned_paths said ``deliverables/docs/contract.md``
# therefore produced two identities for ONE file and every comparison between
# them was a coin flip -- live B+ run task-1791256754557: C-01 reported
# FILE_NOT_FOUND for docs/contract.md while the 40 006 B file was on disk under
# deliverables/docs/contract.md, the node was failed and re-planned.
#
# Everything below parses, compares and relocates through canonical_parts() /
# identity_key(); nothing here searches the filesystem for a same-named file.
# ---------------------------------------------------------------------------

_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:|[\\/])")


class PathIdentityError(ValueError):
    """A declared path that cannot be given one identity inside a root.

    A ValueError on purpose: graph-patch admission already rejects a patch by
    raising ValueError, and the message is written for the model that produced
    the path -- original path, reason, candidates, and the root involved.
    """

    def __init__(self, raw: object, reason: str, *, root: object = "", candidates=()):
        self.raw = str(raw or "")
        self.reason = str(reason)
        self.root = str(root or "")
        self.candidates = [str(item) for item in candidates or ()]
        detail = f"path identity rejected ({self.reason}): {self.raw!r}"
        if self.root:
            detail += f" against workspace root {self.root!r}"
        if self.candidates:
            detail += "; candidates: " + ", ".join(repr(item) for item in self.candidates)
        super().__init__(detail)


def canonical_parts(raw: object, *, root: object = "") -> Tuple[Tuple[str, ...], bool]:
    """Parse ONE workspace path into (segments, declared_as_directory).

    The single parser: Windows separators, ``./``, repeated separators, a
    trailing separator and ``..`` that stays inside the root all collapse to
    the same segment tuple.  A path that is empty, absolute, a home reference,
    or that climbs above the root is REJECTED -- never silently repaired or
    clamped.  Case folding is a comparison concern (``identity_key``), not a
    parsing one.
    """
    text = str(raw if raw is not None else "").strip()
    if not text or text in {".", "./", ".\\"}:
        raise PathIdentityError(raw, "empty_path", root=root)
    text = text.replace("\\", "/")
    if text.startswith("~") or _ABSOLUTE_PATH_RE.match(text):
        raise PathIdentityError(raw, "absolute_path", root=root)
    declared_directory = text.endswith("/")
    segments: List[str] = []
    for segment in text.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if not segments:
                raise PathIdentityError(raw, "escapes_workspace_root", root=root)
            segments.pop()
            continue
        segments.append(segment)
    if not segments:
        raise PathIdentityError(raw, "empty_path", root=root)
    return tuple(segments), declared_directory


def canonical_relative(raw: object, *, root: object = "") -> str:
    """Identity of a path already written relative to the workspace root."""
    segments, _ = canonical_parts(raw, root=root)
    return "/".join(segments)


def declared_relative(raw: object, *, root: object = "") -> str:
    """Identity of a DECLARED artifact path: canonical segments + namespace.

    ``normalize_worker_output_path`` applied to canonical segments, so the
    namespace rule has exactly one implementation and one call order.
    """
    segments, _ = canonical_parts(raw, root=root)
    return normalize_worker_output_path("/".join(segments))


def identity_key(value: object) -> str:
    """The comparable key of one path string.  Never raises, never guesses.

    A value the parser rejects keeps its own folded spelling as its key, so an
    unparseable path can only ever equal itself: it can no longer be equated
    with a different file by a prefix, suffix or basename test.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        segments, _ = canonical_parts(text)
    except PathIdentityError:
        return text.replace("\\", "/").strip("/").casefold()
    return "/".join(segments).casefold()


def same_or_under(owner: object, target: object) -> bool:
    """True when *target* IS *owner* or lives inside it."""
    owner_key, target_key = identity_key(owner), identity_key(target)
    if not owner_key or not target_key:
        return False
    return target_key == owner_key or target_key.startswith(owner_key + "/")


def paths_overlap(left: object, right: object) -> bool:
    """True when two declared paths name the same file or nest into each other.

    The one replacement for ``contracts_overlap`` / ``_covers`` /
    ``_paths_conflict``.
    """
    left_key, right_key = identity_key(left), identity_key(right)
    if not left_key or not right_key:
        return False
    return (
        left_key == right_key
        or left_key.startswith(right_key + "/")
        or right_key.startswith(left_key + "/")
    )


def join_identity(parent: object, child: object) -> str:
    """Address *child* under the (directory) identity *parent*."""
    parent_segments, _ = canonical_parts(parent)
    child_segments, _ = canonical_parts(child)
    return "/".join((*parent_segments, *child_segments))


class PathIdentity:
    """One path, one identity: its rel form under one root, plus how it bound.

    ``status`` records how the path bound to what the contract already
    declares: ``canonical`` (nothing to compare against), ``exact`` (it was
    already the declared spelling), ``relocated`` (one declared path is its
    unique suffix match -- the live C-01 shape ``docs/contract.md`` versus
    ``deliverables/docs/contract.md``) and ``unbound`` (no declared path
    matches; the path keeps its own spelling so a wrong path still fails).
    Ambiguity is not a status: it raises.
    """

    def __init__(self, root, raw, rel, *, declared_directory=False, status="canonical",
                 reason="", candidates=(), relocated_from=""):
        self.root = str(root or "")
        self.raw = str(raw or "")
        self.rel = str(rel or "")
        self.declared_directory = bool(declared_directory)
        self.status = str(status)
        self.reason = str(reason)
        self.candidates = tuple(str(item) for item in candidates or ())
        self.relocated_from = str(relocated_from or "")

    @property
    def key(self) -> str:
        return self.rel.casefold()

    @property
    def relocated(self) -> bool:
        return bool(self.relocated_from)

    @property
    def absolute(self) -> str:
        if not self.root:
            return self.rel
        return str(Path(self.root) / self.rel)

    def as_path(self) -> Path:
        return Path(self.absolute)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"PathIdentity(rel={self.rel!r}, status={self.status!r}, "
                f"relocated_from={self.relocated_from!r})")


def declared_members(raw_paths, *, root: object = "") -> List[Tuple[str, str]]:
    """(key, rel) of every declared path, in order; unparseable ones dropped.

    One identity appears once: a path declared twice (the same file named by two
    overlapping declaration sets) is ONE declaration, not an ambiguity.
    """
    members: List[Tuple[str, str]] = []
    seen: set = set()
    for item in raw_paths or ():
        text = str(item or "").strip()
        if not text or text.startswith("artifact:"):
            continue
        try:
            segments, _ = canonical_parts(text, root=root)
        except PathIdentityError:
            continue
        rel = "/".join(segments)
        key = rel.casefold()
        if key in seen:
            continue
        seen.add(key)
        members.append((key, rel))
    return members


def _names_a_tree(rel: str) -> bool:
    """True when a declared path claims a whole subtree, not one file.

    The same shape test ``apply_task_graph_patch`` already applies to declared
    outputs: no file extension in the last segment.
    """
    return "." not in rel.rsplit("/", 1)[-1]


def declared_tree_member(token: object, declared) -> str:
    """Resolve a BARE name under exactly ONE declared tree, else "".

    A tree claim is an extension-less declared path — the shape
    ``apply_task_graph_patch`` already treats as covering a whole tree.  This is
    the root-cause miner's shape: a verifier message names ``init_db.py`` while
    the owning node declares ``deliverables/app/backend/``.

    Uniqueness is the entire contract: zero or several candidate trees answer
    "" instead of guessing, and the filesystem is never consulted.  Callers that
    must stay strict about by-name coverage (acceptance-clause anchoring,
    criterion binding) deliberately do not use this.
    """
    try:
        canonical = canonical_relative(token)
    except PathIdentityError:
        return ""
    if "/" in canonical:
        return ""
    trees = list(dict.fromkeys(
        rel for _key, rel in declared_members(declared) if _names_a_tree(rel)
    ))
    composed = list(dict.fromkeys(join_identity(rel, canonical) for rel in trees))
    return composed[0] if len(composed) == 1 else ""


def workspace_identity(root, raw, *, declared=(), namespace=False) -> PathIdentity:
    """Bind one path to the artifact the existing declaration set names.

    ``declared`` is the path set the caller may resolve against (a node's
    owned_paths / expected_outputs / criterion leaves, or the frozen contract).
    The only relocation performed is a UNIQUE SUFFIX match inside that set; an
    ambiguous match raises and an absent one is never guessed.  ``namespace``
    picks the unbound answer: declared artifacts get the namespace partition
    (``declared_relative``), paths somebody else wrote stay verbatim
    (``canonical_relative``).
    """
    segments, declared_directory = canonical_parts(raw, root=root)
    canonical = "/".join(segments)
    members = declared_members(declared, root=root)
    if members:
        canonical_key = canonical.casefold()
        exact = [rel for key, rel in members if key == canonical_key]
        if exact:
            return PathIdentity(root, raw, exact[0],
                                declared_directory=declared_directory, status="exact")
        # The DECLARED identity of the token itself — the namespace rule, not a
        # search: "docs/contract.md" IS "deliverables/docs/contract.md".  Tried
        # before any suffix match so the common shape is decided by the rule
        # that created it, never by whichever declaration happens to be unique.
        declared_self = declared_relative(canonical, root=root)
        named = [rel for key, rel in members if key == declared_self.casefold()]
        if named:
            return PathIdentity(root, raw, named[0],
                                declared_directory=declared_directory, status="relocated",
                                relocated_from=canonical,
                                reason="declared identity of the path")
        suffix = [(key, rel) for key, rel in members if key.endswith("/" + canonical_key)]
        if len(suffix) == 1:
            return PathIdentity(root, raw, suffix[0][1],
                                declared_directory=declared_directory, status="relocated",
                                relocated_from=canonical,
                                reason="unique suffix match among the declared paths")
        if len(suffix) > 1:
            raise PathIdentityError(raw, "ambiguous_declared_suffix", root=root,
                                    candidates=[rel for _, rel in suffix])
    unbound_rel = declared_relative(canonical, root=root) if namespace else canonical
    return PathIdentity(root, raw, unbound_rel, declared_directory=declared_directory,
                        status="unbound", candidates=[rel for _, rel in members],
                        reason="no declared path matches it")


def workspace_relative(root, path) -> str:
    """Run-relative posix identity of a real path under ``root`` (else "").

    Filesystem resolution, not comparison: containment comes from the identity
    parser, and every spelling that names the same file yields one string.
    """
    try:
        base = Path(root).resolve()
        raw_path = Path(str(path)).expanduser()
        target = raw_path.resolve() if raw_path.is_absolute() else (base / str(path)).resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    try:
        if not target.is_relative_to(base):
            return ""
    except OSError:
        return ""
    relative = target.relative_to(base).as_posix()
    try:
        return canonical_relative(relative, root=root)
    except PathIdentityError:
        return relative


def declared_output_paths(run_dir, raw, *, include_staging: bool = True) -> List[Path]:
    """The locations ONE declared output string can name, in a stable order.

    The single candidate enumeration: the literal spelling first, then its
    declared identity, then the delivery-staging mirror of both.  Staging is a
    publishing-phase directory, so a caller that is not publishing can switch
    it off instead of growing its own copy of this list.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    literal = Path(text)
    if literal.is_absolute():
        return [literal]
    relatives: List[str] = [text.replace("\\", "/")]
    try:
        identity = declared_relative(text, root=run_dir)
    except PathIdentityError:
        identity = ""
    if identity and identity not in relatives:
        relatives.append(identity)
    paths: List[Path] = [Path(run_dir) / relative for relative in relatives]
    if include_staging:
        staging = Path(run_dir) / ".delivery_staging"
        paths.extend(staging / relative for relative in relatives)
    return paths


def declared_output_path_tokens(text: str) -> List[str]:
    """Path-like fragments embedded in a DESCRIPTIVE declared output.

    Fix S (doc 18, 2026-09-15 r13b): a declared output is routinely a sentence
    that embeds the real paths, e.g.
    ``deliverables/app/backend/ 下的后端源码与 schema.sql``.  Treating that
    whole sentence as a filename reported artifacts which were already on disk
    as missing and failed live r13b's N-003 (14 backend source files plus a
    6.8 KB schema.sql were present), which triggered a replan and burned
    recovery budget — the same mapping-defect class as Fix N, one layer lower.

    Glob contracts keep their own matcher, so patterns are excluded here.

    Fix T (2026-09-15): moved here from ``core.orchestrator`` so the Verifier
    can reuse the same extraction without importing the Orchestrator, which
    would close an import cycle (orchestrator already imports core.verifier).
    ``core.orchestrator`` re-exports both helpers, so existing imports and the
    Fix S regression module keep working unchanged.
    """
    if any(char in str(text) for char in "*?["):
        return []
    return [
        fragment for fragment in re.findall(
            r"[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)*/?",
            str(text),
        )
        if "/" in fragment
    ]


def declared_output_file_claims(text: str) -> List[str]:
    """The FILE paths a declared output actually claims, for ``file:`` leaves.

    Fix T (doc 18, 2026-09-15 r13b): ``Verifier._deterministic_observations``
    used its own filter — keep the declaration when ``Path(name)`` contains a
    dot — and so turned the same prose declaration
    ``deliverables/app/backend/ 下的后端源码与 schema.sql`` into the leaf
    ``file:<whole sentence> non-empty``.  ``_check_file_leaf`` resolves that
    literally, gets FILE_NOT_FOUND, and records checked=True/holds=False: a
    hard "does not hold" for a file that is on disk.  That false gap then
    feeds the root-cause router and can send a completed node back to repair.
    Fix S repaired the Orchestrator's postcondition only; this is the same
    defect one gate later.

    Rules, deliberately conservative — a leaf is emitted only for an explicit
    file claim, never invented from prose:

    * a glob keeps its own matcher and claims nothing here;
    * a directory token (trailing separator, or a basename with no suffix)
      is not a file claim — the Orchestrator postcondition and the semantic
      layer decide directories;
    * a bare word such as ``schema.sql`` with no directory part makes no
      path claim on its own, matching Fix S's documented stance.
    """
    normalized = str(text or "").replace("\\", "/").strip()
    if not normalized or normalized.startswith("artifact:"):
        return []
    claims: List[str] = []
    for token in declared_output_path_tokens(normalized):
        if token.endswith("/"):
            continue
        if not Path(token).suffix:
            continue
        candidate = token.strip("/")
        if candidate and candidate not in claims:
            claims.append(candidate)
    return claims


def runtime_output_claims(text: str, owned=()) -> List[str]:
    """Physical node outputs; slash-separated prose is not a file contract."""
    normalized = str(text or "").replace("\\", "/").strip()
    if not normalized or normalized.startswith("artifact:"):
        return []
    files = declared_output_file_claims(normalized)
    if files:
        return files
    if any(character.isspace() for character in normalized):
        return []
    if (normalized.startswith("deliverables/") or normalized.endswith("/")
            or any(character in normalized for character in "*?[")
            or any(identity_key(normalized) == identity_key(path) for path in owned)
            or (Path(normalized).name == normalized and Path(normalized).suffix)
            or _EXTENSIONLESS_DECLARED_NAME_RE.fullmatch(normalized)):
        return [normalized]
    return []


#: b33 S4 (F16): the only extensionless shape a STRUCTURED declaration field may
#: claim — a bare all-caps convention name such as README or LICENSE.  Every
#: other extensionless value stays prose (a hint), never an obligation.
_EXTENSIONLESS_DECLARED_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_.+\-]*$")


def declared_output_path_field(value: object) -> List[str]:
    """b33 S4 (F16): the literal path a STRUCTURED declaration field names.

    A structured declaration (a ``required_outputs`` / ``expected_outputs``
    entry) states its path in a named field (``path`` / ``file`` / ``location``
    / ``artifact_path``).  That is an explicit file claim, so the suffix
    whitelist — which exists to keep PROSE from inventing files — must not
    silently drop it: a declared ``README`` has to stay visible, normalizable
    and comparable in the ownership gate.  Prose keeps every conservative rule
    of :func:`declared_output_file_claims`; this reader is consulted only for a
    structured path field, and only when the suffix-based reading found nothing.

    Deliberately still conservative, so that Fix T cannot come back: a glob, a
    directory token, an ``artifact:`` reference, an absolute path and a
    whitespace-bearing sentence (``deliverables/app 下的源码与 schema.sql``)
    claim nothing here.  An extensionless value is accepted only in the bare
    all-caps convention form (``README``, ``LICENSE``).
    """
    text = str(value or "").replace("\\", "/").strip()
    if not text or text.startswith("artifact:"):
        return []
    if text.startswith("/") or text.startswith("~"):
        return []
    if any(marker in text for marker in ("*", "?", "[", "]")):
        return []
    if any(character.isspace() for character in text):
        return []
    token = text.strip("/")
    if not token or text.endswith("/"):
        return []
    if Path(token).suffix:
        return [token]
    if _EXTENSIONLESS_DECLARED_NAME_RE.match(token.rsplit("/", 1)[-1]):
        return [token]
    return []


#: 起 A (doc b26 C1): declared-output ``source`` values that mean optional.
#: Conditionality comes from an EXPLICIT field and from nothing else — a prose
#: declaration carries no field, so it stays required (undecidable ⇒ required).
_CONDITIONAL_OUTPUT_SOURCES = {
    "optional", "conditional", "conditionally", "conditional_optional",
}


def declared_output_entry(raw: object) -> Dict[str, Any]:
    """One declared output as a mapping when it is structured, else ``{}``.

    起 A (b26 C1): ``required`` / ``source`` / ``condition`` exist only on the
    structured form of a declaration.  Consumers must classify from those
    fields; ``str(raw)`` erases them, which is how a conditional artifact was
    hardened into a hard postcondition.
    """
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    return {}


def declared_output_claims(raw: object) -> List[str]:
    """The file claims of ONE declared output, structured or prose.

    Structured entries are read field-first (``path``, then ``file`` /
    ``location`` / ``artifact_path``, then ``name``) instead of relying on the
    ``str(dict)`` repr that the old call site happened to scrape.

    b33 S4 (F16): a value in one of those NAMED path fields is a literal claim,
    so it is read by :func:`declared_output_path_field` when the prose reader
    finds nothing — a structured declaration of ``README`` stays visible.  The
    ``name`` field keeps the conservative prose rules, and so does prose.
    """
    entry = declared_output_entry(raw)
    if entry:
        for key in ("path", "file", "location", "artifact_path"):
            value = entry.get(key)
            if value is None or not str(value).strip():
                continue
            claims = declared_output_file_claims(str(value))
            if claims:
                return claims
            # b33 S4 (F16): the value sits in a NAMED path field, so it is an
            # explicit claim and is not subject to the prose suffix whitelist.
            # A declared ``README`` must stay visible and normalizable instead of
            # being dropped by ``declared_output_file_claims``.
            literal = declared_output_path_field(value)
            if literal:
                return literal
        return declared_output_file_claims(str(entry.get("name") or ""))
    return declared_output_file_claims(str(raw or ""))


def declared_output_is_required(raw: object) -> bool:
    """Whether one declared output is a HARD postcondition (deterministic).

    Only an EXPLICIT field may relax a declaration: a ``required`` boolean, a
    ``required``/``source`` string, or a non-empty ``condition``.  Prose is
    never self-exempting — a plain string carries no field, so its
    conditionality is not decidable in code and it stays REQUIRED
    (DESIGN_ARTIFACT_CONTRACT §4: 无法判定的条件按必需处理).  A genuinely
    optional artifact must declare that itself; natural-language words such as
    若/如果/可选 in a goal sentence are not a contract.  ``required: true``
    therefore still means exactly what it always meant.
    """
    entry = declared_output_entry(raw)
    if entry:
        explicit = entry.get("required")
        if isinstance(explicit, bool):
            return explicit
        if isinstance(explicit, str):
            lowered = explicit.strip().lower()
            if lowered in {"true", "yes", "required", "mandatory"}:
                return True
            if lowered in {"false", "no", "optional", "conditional"}:
                return False
        source = str(entry.get("source") or "").strip().lower()
        if source in _CONDITIONAL_OUTPUT_SOURCES:
            return False
        if entry.get("condition") not in (None, "", {}, []):
            return False
        return True
    # 起 A: no structured field ⇒ undecidable ⇒ required.
    return True


#: 起 A: ``source`` value stamped on a PROSE declaration whose conditional
#: clause was located and normalized into structured fields.
_PROSE_CONDITIONAL_SOURCE = "prose_conditional"

#: 起 A: clause markers that OPEN a conditional clause in a PROSE declaration.
#: Only ``normalize_declared_output`` consults them, and only when the clause is
#: locatable (the marker opens the clause that carries the single file claim).
#: ``declared_output_is_required`` never consults them: prose carries no field,
#: so its conditionality is undecidable in code and it stays required.
_CONDITIONAL_CLAUSE_MARKERS = ("若", "如果", "一旦", "除非", "仅当", "按需")
_CONDITIONAL_CLAUSE_RE = re.compile(r"^(?:if|when|unless|only if)\b", re.IGNORECASE)
_CLAUSE_SEPARATORS = "；;。\n"


def _conditional_clause_segments(text: str) -> List[str]:
    """Sub-clauses of ``text`` that OPEN with a condition marker."""
    segments: List[str] = []
    for chunk in re.split(f"[{re.escape(_CLAUSE_SEPARATORS)}]", str(text or "")):
        segment = chunk.strip()
        if not segment:
            continue
        if segment.startswith(_CONDITIONAL_CLAUSE_MARKERS) or _CONDITIONAL_CLAUSE_RE.match(segment):
            segments.append(segment)
    return segments


def normalize_declared_output(raw: object) -> Optional[Dict[str, Any]]:
    """Normalize ONE declared output into structured form, or ``None``.

    ``None`` means "no structured reading applies": the caller MUST keep the
    declaration exactly as written, which means REQUIRED (起 A / DESIGN §4:
    无法判定的条件按必需处理).

    A Mapping is always returned as a copy with ``path`` (from
    ``path``/``file``/``location``/``artifact_path``) normalized and
    ``condition`` preserved.  Nothing is invented for a mapping that declares
    no path at all.

    A plain string is normalized ONLY when its conditionality is explicit and
    locatable in the text itself:

    * exactly one file claim (``declared_output_file_claims``), and
    * that claim sits inside a clause that OPENS with a condition marker
      (若/如果/一旦/除非/仅当/按需 or ``if|when|unless|only if``).

    It then becomes ``{"path": ..., "required": False, "source":
    "prose_conditional", "condition": {"kind": "prose_clause", "text": <clause
    verbatim>, "when_false": "structured_empty_manifest"}}`` — the relaxation is
    auditable and decidable downstream instead of being a silent exemption.

    Every other string — no clause, a clause carrying no claim, several claims
    (必须生成 A，若发现缺陷再生成 B), a directory-only token, empty — stays
    unstructured and therefore REQUIRED.  The clause is quoted verbatim: no
    artifact is invented, no clause semantics are rewritten, and no
    ``when_false`` behaviour other than the declared empty manifest is implied.
    """
    if isinstance(raw, Mapping):
        entry = {str(key): value for key, value in raw.items()}
        declared_path = ""
        for key in ("path", "file", "location", "artifact_path"):
            if str(entry.get(key) or "").strip():
                declared_path = str(entry[key]).strip()
                break
        if declared_path:
            entry["path"] = normalize_worker_output_path(declared_path)
        return entry
    text = str(raw or "").strip()
    if not text or not declared_output_path_tokens(text):
        return None
    claims = declared_output_file_claims(text)
    if len(claims) != 1:
        return None
    claim = normalize_worker_output_path(claims[0])
    clause = next(
        (
            segment
            for segment in _conditional_clause_segments(text)
            if claim in segment or claims[0] in segment
        ),
        "",
    )
    if not clause:
        return None
    return {
        "path": claim,
        "required": False,
        "source": _PROSE_CONDITIONAL_SOURCE,
        "condition": {
            "kind": "prose_clause",
            "text": clause,
            "when_false": "structured_empty_manifest",
        },
    }


def resolve_declared_output(run_dir: Path, output: str, *, include_staging: bool = True) -> Optional[Path]:
    """Resolve a declared node output to a real file/dir (existence only).

    Fix S1: every candidate comes from ``declared_output_paths`` -- the
    literal spelling and its declared identity -- so this resolver and the
    contract comparisons below agree on which file a declaration names.
    Freshness stays a validation concern.
    """
    text = str(output or "")
    if text.endswith(("/", "\\")):
        return next((
            path for path in declared_output_paths(run_dir, text, include_staging=include_staging)
            if path.is_dir() and any(item.is_file() for item in path.rglob("*"))
        ), None)
    direct = next((
        path for path in declared_output_paths(run_dir, text, include_staging=include_staging)
        if path.is_file()
    ), None)
    if direct is not None:
        return direct
    for token in declared_output_path_tokens(text):
        found = next((
            path for path in declared_output_paths(run_dir, token, include_staging=include_staging)
            if path.is_file()
            or (path.is_dir() and any(item.is_file() for item in path.rglob("*")))
        ), None)
        if found is not None:
            return found
    return None


class RunOutputLayout:
    """Resolve canonical paths without creating files implicitly."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.node_io_dir = self.run_dir / "node_io"
        self.layered_dir = self.run_dir / "layered"
        self.evidence_dir = self.layered_dir / "evidence_packets"
        self.reducer_dir = self.layered_dir / "reducers"
        self.deliverables_dir = self.run_dir / "deliverables"
        self.reports_dir = self.run_dir / "reports"
        self.delivery_staging_dir = self.run_dir / ".delivery_staging"
        self._delivery_root = self.deliverables_dir

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.node_io_dir,
            self.layered_dir,
            self.evidence_dir,
            self.reducer_dir,
            self.deliverables_dir,
            self.reports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def safe_component(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))
        return value.strip("._") or "unknown"

    def execution_stem(self, execution_id: str, node_id: str, task_version: int, attempt: int) -> str:
        return (
            f"{self.safe_component(execution_id)}__node-{self.safe_component(node_id)}"
            f"__v{int(task_version):03d}__a{int(attempt):03d}"
        )

    def node_input(self, stem: str) -> Path:
        return self.node_io_dir / f"{stem}__input.json"

    def node_output_json(self, stem: str) -> Path:
        return self.node_io_dir / f"{stem}__output.json"

    def node_output_markdown(self, stem: str) -> Path:
        return self.node_io_dir / f"{stem}__output.md"

    def packet_path(self, packet_id: str) -> Path:
        """Resolve canonical evidence packet path.

        When the packet_id follows the structured form
        ``packet:<node_id>:v<ver>:a<attempt>:<exec_id>``, prefer the
        short node_id stem so downstream consumers can locate packets by
        their task/node identifier.
        """
        safe = self.safe_component(packet_id)
        # Extract the node_id component (second field after 'packet:')
        parts = str(packet_id).split(":")
        if len(parts) >= 2 and parts[0] == "packet":
            node_id = self.safe_component(parts[1])
            if node_id and node_id != "unknown":
                return self.evidence_dir / f"{node_id}.json"
        return self.evidence_dir / f"{safe}.json"

    def begin_delivery_draft(self) -> Path:
        if self.delivery_staging_dir.exists() and any(self.delivery_staging_dir.iterdir()):
            from uuid import uuid4
            history = self.run_dir / ".delivery_drafts"
            history.mkdir(parents=True, exist_ok=True)
            self.delivery_staging_dir.rename(history / uuid4().hex)
        self.delivery_staging_dir.mkdir(parents=True, exist_ok=True)
        self._delivery_root = self.delivery_staging_dir
        return self.delivery_staging_dir

    def publish_delivery(self, *, authorized, source_refs=(), dependencies=(), ownership_check=None, fence_provider=None):
        from AstralNexus.core.delivery import DeliveryManager
        manager = DeliveryManager(self.run_dir)
        manifest = manager.publish(authorized=authorized, source_refs=source_refs,
                                   dependencies=dependencies, ownership_check=ownership_check, fence_provider=fence_provider)
        self._delivery_root = self.deliverables_dir
        return manifest

    def delivery(self, name: str) -> Path:
        return self._delivery_root / name

    @property
    def delivery_dir(self) -> Path:
        """Current draft/publication directory for contract artifacts."""
        return self._delivery_root

    def report(self, name: str) -> Path:
        return self.reports_dir / name
