"""Reducer reference validation against accepted, versioned run evidence."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from AstralNexus.runtime.evidence_catalog import EvidenceCatalog


def validate_summary(graph, run_dir, version, summary: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    sources = summary.get('source_nodes')
    claims = summary.get('verified_claims')
    if not isinstance(sources, list) or not sources:
        return False, ['missing_source_nodes']
    if not isinstance(claims, list) or not claims:
        return False, ['missing_verified_claims']
    source_ids = {str(value) for value in sources}
    for node_id in source_ids:
        node = graph.get_node(node_id)
        if node is None:
            errors.append('unknown_source_node:' + node_id)
        elif node.status != 'completed':
            errors.append('unaccepted_source_node:' + node_id)
        elif str(node.metadata.get('task_version', 1)) != str(version):
            errors.append('stale_source_node:' + node_id)
    catalog = EvidenceCatalog(Path(str(run_dir)), graph=graph, task_version=version) if run_dir is not None else None

    def valid_ref(ref: Any, owners: set[str]) -> bool:
        text = str(ref or '').strip()
        fact = graph.facts.get(text)
        if fact is not None:
            return (fact.source in owners and fact.metadata.get('verification_verdict') == 'approved'
                    and str(fact.metadata.get('task_version', 1)) == str(version))
        # External citations need an approved fact from the claimed producer;
        # a nonempty URL alone is never sufficient evidence.
        if '://' in text:
            return any(f.source in owners and text in (f.metadata.get('evidence_refs') or [])
                       and f.metadata.get('verification_verdict') == 'approved'
                       and str(f.metadata.get('task_version', 1)) == str(version)
                       for f in graph.facts.values())
        return bool(catalog and catalog.validate_reference(text, source_nodes=owners)['valid'])

    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not str(claim.get('claim') or '').strip():
            errors.append(f'invalid_claim:{index}')
            continue
        declared_owners = claim.get('source_nodes', sources)
        if not isinstance(declared_owners, list):
            errors.append(f'invalid_claim_sources:{index}')
            continue
        owners = {str(value) for value in declared_owners}
        if not owners or not owners.issubset(source_ids):
            errors.append(f'claim_source_mismatch:{index}')
        refs = claim.get('evidence_refs')
        if not isinstance(refs, list) or not refs or not all(valid_ref(ref, owners) for ref in refs):
            errors.append(f'unbound_claim_evidence:{index}')
    refs = summary.get('source_artifacts', [])
    if not isinstance(refs, list) or not all(valid_ref(ref, source_ids) for ref in refs):
        errors.append('unbound_source_artifacts')
    return not errors, errors
