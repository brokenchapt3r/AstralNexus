"""Exact bindings to frozen requirements; no inference from worker prose."""
from typing import Any, Mapping
import hashlib


def frozen_bindings(contract: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    ambiguous: set[str] = set()
    for entry in contract.get("global_acceptance_criteria", []) or []:
        if isinstance(entry, str) and entry.strip():
            text = entry.strip()
            result[text] = "requirement:" + hashlib.sha256(text.encode()).hexdigest()
            continue
        if not isinstance(entry, Mapping):
            continue
        ident = str(entry.get("criterion_id") or entry.get("id") or "")
        text = str(entry.get("criterion") or entry.get("text") or entry.get("description") or "").strip()
        if not ident or not text:
            continue
        if text in result and result[text] != ident:
            ambiguous.add(text)
        result[text] = ident
    return {text: ident for text, ident in result.items() if text not in ambiguous}


def bind_observations(observations: list[dict[str, Any]], bindings: Mapping[str, str], *, strict: bool = False) -> list[dict[str, Any]]:
    result = []
    for item in observations:
        row = dict(item)
        # The caller supplies the frozen contract index, never node-authored IDs.
        row.pop("original_requirement_id", None)
        ident = bindings.get(str(item.get("criterion") or item.get("criterion_text") or "").strip())
        if ident:
            row["original_requirement_id"] = ident
            row["requirement_binding"] = "frozen_contract"
        elif strict:
            row["requirement_binding"] = "unbound"
        result.append(row)
    return result
