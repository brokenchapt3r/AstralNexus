"""One structured declaration of what a task node must deliver (b33 S2).

Why this module exists: before this landing the same declaration was written
into eight slots (the schema's four keys, the criterion leaves, the two
delivery-id fields and the prose re-extraction of ``goal``), and two
derivations were spelled out more than once inside the framework:

* the delivery criterion id ``<output_id>.C<nn>`` was written out four times in
  ``core/governor.py`` — when reading a criterion's text, when building the
  known-id set, when building the required-id set and again in the
  delivery-contract normalizer;
* one declared output entry was normalized two different ways depending on
  whether it was a ``dict`` or a ``str`` (the ownership gate in
  ``GlobalGovernor.apply_task_graph_patch``), so a ``str`` carrying a prose tail
  derived a "path" that could never pass the node's own ``owned_paths`` gate —
  the path identity plane and the declaration plane disagreed about the same
  file.

This module is the single source for both derivations.

It deliberately does not weaken any check.  It returns paths, and every caller
still runs its own gate (``owned_paths`` coverage, suffix whitelists, the
delivery-contract gates) on those paths.  An entry whose text cannot be
normalized to a concrete path keeps its verbatim spelling, so the caller sees
exactly the string it used to see and keeps rejecting it when it is not real.

Every path this module returns is a comparison key from the single path-identity
plane in ``AstralNexus.core.output_paths`` (landed by b33 S1) — the same plane
and the same key function ownership is compared on.  This module owns no
normalization of its own, because a second normalization rule would recreate
exactly the "one file, two definitions" defect S1 removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from AstralNexus.core.output_paths import declared_output_claims, identity_key


def criterion_leaf_id(output_id: Any, index: int, space: str = "C", explicit: Any = "") -> str:
    """The single spelling of one criterion leaf id.

    ``<output_id>.<space><index:02d>`` while an output id is known, and
    ``<space><index:02d>`` for an anonymous entry -- a bare criteria list
    evaluated on its own, which has no declaration to borrow a prefix from.  An
    id the task book already declares (``explicit``) wins verbatim.

    ``space`` names the leaf family, not a second naming rule: the delivery
    contract (``C``), a node's acceptance criteria (``AC``) and one expected
    output of that node (``EO``) are three families of the same shape, and
    ``runtime/acceptance_checks.py`` parses all three with one pattern.
    """
    declared = str(explicit or "").strip()
    if declared:
        return declared
    suffix = f"{space}{index:02d}"
    prefix = str(output_id or "").strip()
    return f"{prefix}.{suffix}" if prefix else suffix


def criterion_id(output_id: Any, index: int, explicit: Any = "") -> str:
    """The delivery-contract criterion id (``<output_id>.C<index:02d>``).

    An id already declared by the task book (``explicit``) wins; otherwise the
    deterministic naming is derived.  An entry that declares neither has no
    derivable id at all (``""``), which is the same answer the governor's own
    criterion-text reader already gave for that case -- and differs on purpose
    from :func:`criterion_leaf_id`, which labels an anonymous leaf so that an
    id-less criteria list can still be reported on.
    """
    declared = str(explicit or "").strip()
    if declared:
        return declared
    prefix = str(output_id or "").strip()
    return criterion_leaf_id(prefix, index) if prefix else ""


def declared_criterion_ids(entry: Any) -> List[str]:
    """Every criterion id one ``required_outputs`` entry declares, in order.

    The entry's own ``acceptance_criterion_ids`` win when it declares any;
    otherwise the ids are derived from the ``acceptance_criteria`` leaves.  This
    is the one place that answers that question, so the known-id set, the
    required-id set and the delivery-contract normalizer can no longer drift
    apart while all claiming to mirror the same naming rule.
    """
    if not isinstance(entry, dict):
        return []
    explicit = [
        str(cid).strip() for cid in (entry.get("acceptance_criterion_ids") or [])
    ]
    if explicit:
        return [cid for cid in explicit if cid]
    ids: List[str] = []
    for index in range(1, len(entry.get("acceptance_criteria") or []) + 1):
        cid = criterion_id(entry.get("output_id"), index)
        if cid:
            ids.append(cid)
    return ids


def declared_criterion_texts(entries: Any) -> Dict[str, str]:
    """The one reader of a declaration's criterion leaves: id -> text.

    It replaces the governor's private criterion-text map, which spelled the id
    rule a second time.  The per-leaf spelling is kept exactly as it was: an
    explicit id at that leaf position wins, otherwise the id is derived from the
    entry's ``output_id``.  The governor's criterion-binding post-pass is the
    only caller, and it still decides for itself what to do with a text.
    """
    texts: Dict[str, str] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        criteria = [str(text) for text in (entry.get("acceptance_criteria") or [])]
        explicit_ids = [
            str(cid).strip() for cid in (entry.get("acceptance_criterion_ids") or [])
        ]
        for index, text in enumerate(criteria, 1):
            cid = criterion_id(
                entry.get("output_id"),
                index,
                explicit_ids[index - 1] if len(explicit_ids) >= index else "",
            )
            if cid:
                texts[cid] = text
    return texts


@dataclass(frozen=True)
class DeclaredRequirement:
    """One declared output entry, normalized once at the declaration layer.

    ``paths`` holds *identity keys* — comparison keys from the single plane in
    ``AstralNexus.core.output_paths`` — for every way this entry declares a
    path.  It is a *candidate* list: a caller's gate accepts when any candidate
    is covered and rejects when none is, so this layer never narrows what the
    gate sees.  For a string entry the verbatim spelling stays a candidate
    exactly as before this landing; the extracted path is added, so a domain
    prose tail can no longer hide the declared file.  This module owns no path
    normalization of its own: the only normalization here is the call into that
    single parser, and the parser's own rejection semantics (a path escaping the
    workspace root, or an absolute path) are never re-implemented here.

    ``raw`` keeps the declaration's own spelling and ``path_spelling`` is the
    same spelling reduced to the entry's path (the pre-landing comparison
    input).  ``note`` is NOT a cleaned-up prose tail: it is set -- to the
    entry's verbatim ``raw`` text -- only when the entry is a string whose
    extraction found a path other than its verbatim spelling, i.e. the entry
    carried domain prose *around* the path.  A caller that wants the prose part
    must subtract the paths itself; nothing here may be read as "this is the
    non-path text", and a note is never turned into a requirement, a hint or a
    hard obligation by this module.
    """

    raw: str
    paths: List[str]
    path_spelling: str
    note: str


def declared_requirement(raw: Any) -> DeclaredRequirement:
    """Normalize one declared output entry (``dict`` or ``str``) exactly once.

    A ``dict`` declares its path in a field; a ``str`` declares it inline.
    ``paths`` is the union of both readings of the entry: the extracted path(s)
    and, for a string entry, the verbatim spelling.  ``path_spelling`` is the one
    spelling that stands for the entry's path -- the whole string for a string
    entry, the field-derived path for a dict -- so a caller can reproduce its
    pre-landing comparison and skip rule without owning a normalization.
    """
    text = "" if raw is None else str(raw)
    claims = [
        str(claim) for claim in declared_output_claims(raw) if str(claim).strip()
    ]
    if not claims and not isinstance(raw, dict):
        claims = [text]
    paths = [identity_key(claim) for claim in claims]
    verbatim = ""
    if isinstance(raw, str):
        verbatim = identity_key(text)
        if verbatim and verbatim not in paths:
            paths.append(verbatim)
        path_spelling = text
    else:
        path_spelling = claims[0] if claims else ""
    note = (
        text
        if isinstance(raw, str) and paths and paths[0] != verbatim
        else ""
    )
    return DeclaredRequirement(
        raw=text, paths=paths, path_spelling=path_spelling, note=note,
    )

def requires_product_change(metadata: Dict[str, Any], criteria: List[str]) -> bool:
    """Recovery identity does not imply an implementation repair contract.

    Roles select execution capabilities, not hidden acceptance obligations.
    Only a declared product-writing criterion requires a product mutation;
    free-form scope labels cannot exempt that explicit obligation.
    """
    recovery = bool(str(metadata.get("recovery_of") or metadata.get("supersedes_node_id") or "").strip())
    recovery = recovery or bool(metadata.get("_is_repair_node"))
    if not recovery:
        return False
    contract = metadata.get("task_contract")
    if isinstance(contract, dict) and contract.get("requires_product_change") is True:
        return True
    from AstralNexus.runtime.acceptance_checks import parse_criterion, KIND_FILE_WRITTEN
    from AstralNexus.runtime.verification_identity import is_report_class_path
    # An explicit product-writing obligation is still a product repair,
    # regardless of a verifier label or a free-form scope exemption.
    return any(kind == KIND_FILE_WRITTEN and not is_report_class_path(path)
               for kind, path in (parse_criterion(text) for text in criteria))
