"""Per-result context budget for tool outputs (P4).

DeerFlow port (doc 30): ``tool_output_budget_middleware`` persists oversized
tool results to disk and replaces them with a compact typed synopsis plus a
file reference, so a single large tool return can never blow the model
context.  Live r13c evidence for why AstralNexus needs this: worker rounds
slowed to 40-90 seconds against 87K+ token contexts because every
python_exec/terminal_exec stdout (13-29 KB drafts, full file dumps) entered
the conversation verbatim and stayed there for the rest of the attempt; the
attempt then died at the wall clock with the work still only in context.

Design rules kept from the DeerFlow original:

* the FULL result is persisted first and the file on disk is the source of
  truth — the in-context view is lossy by construction and says so;
* the runtime never blocks tool execution on a persistence failure: when the
  externalization write fails, the original result stays in context
  (fail-open, same as DeerFlow's head+tail truncation fallback);
* only the model-bound conversation view is compacted.  The execution
  ledger's receipts, loop detection (``ProgressGuard``) and the trace keep
  observing the ORIGINAL result — guards must never be blinded by the
  budget, and a receipt digest still covers the full bytes via the persisted
  file's sha256.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: Run-root-relative directory holding externalized full results. Named
#: alongside ``.astralnexus_state`` and excluded from owned-file snapshots
#: for the same reason: framework runtime data, not delivery evidence.
EXTERNAL_DIRNAME = ".astralnexus_tool_outputs"

DEFAULT_CAP_CHARS = 12_000
_KEEP_HEAD = 1_500
_KEEP_TAIL = 1_500
_MIN_FIELD_TO_TRIM = 3_200
_BULKY_KEYS = ("stdout", "output", "content", "stderr", "body")
#: Provenance keys kept when even the trimmed view is over budget.
_SCALAR_KEEP_KEYS = frozenset({
    "status", "returncode", "path", "error", "truncated",
    "background", "pid", "cwd", "command", "log_path",
})
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def context_cap_chars() -> int:
    """Configured per-result context cap; <= 0 disables externalization."""
    raw = os.environ.get("ASTRALNEXUS_TOOL_OUTPUT_CONTEXT_CAP", "")
    if not str(raw).strip():
        return DEFAULT_CAP_CHARS
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CAP_CHARS


def _trim_field(value: str, reference: str) -> str:
    omitted = len(value) - _KEEP_HEAD - _KEEP_TAIL
    return (
        value[:_KEEP_HEAD]
        + f"\n...[中间 {omitted} 字符已外置；全文见 {reference}]...\n"
        + value[-_KEEP_TAIL:]
    )


def externalize_tool_result(
    result: Dict[str, Any],
    *,
    workspace_root: Any,
    call_id: str,
    tool_round: int,
    cap_chars: Optional[int] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Return ``(view_result, event_or_None)`` for one tool result.

    ``view_result`` is what may enter the model conversation: either the
    original result (small, disabled, or persistence failed) or a compacted
    copy whose bulky string fields keep head+tail around an explicit
    externalization marker, plus ``full_output_ref`` and an
    ``externalized`` provenance block (bytes + sha256 of the persisted
    full result).  The event payload is ready to emit as
    ``llm.tool_output.externalized``.
    """
    cap = context_cap_chars() if cap_chars is None else int(cap_chars)
    if cap <= 0 or workspace_root is None or not isinstance(result, dict):
        return result, None
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return result, None
    if len(serialized) <= cap:
        return result, None

    root = Path(str(workspace_root))
    out_dir = root / EXTERNAL_DIRNAME
    safe_call = _SAFE_ID_RE.sub("_", str(call_id or "call"))[:24] or "call"
    unique = hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()[:8]
    ref_name = f"r{int(tool_round):04d}_{safe_call}_{unique}.json"
    ref_path = out_dir / ref_name
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        temporary = ref_path.with_suffix(".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(ref_path)
    except OSError:
        # Fail-open exactly like the DeerFlow truncation fallback: a
        # persistence failure must never block tool execution or lose the
        # result — the original stays in context.
        return result, None

    digest = hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()
    try:
        reference = ref_path.relative_to(root).as_posix()
    except ValueError:
        reference = ref_path.as_posix()

    view: Dict[str, Any] = dict(result)
    for key in _BULKY_KEYS:
        value = view.get(key)
        if isinstance(value, str) and len(value) > _MIN_FIELD_TO_TRIM:
            view[key] = _trim_field(value, reference)
    if len(json.dumps(view, ensure_ascii=False, default=str)) > cap:
        # DeerFlow's "compact typed synopsis" fallback: many small fields can
        # keep the trimmed view over budget. Collapse to scalar provenance
        # keys only — the persisted file stays the single source of truth.
        view = {
            key: value for key, value in view.items()
            if key in _SCALAR_KEEP_KEYS and isinstance(value, (str, int, float, bool, type(None)))
        }
    view["full_output_ref"] = reference
    view["externalized"] = {
        "bytes": len(serialized.encode("utf-8", errors="replace")),
        "sha256": digest,
        "note": "完整结果已按上下文预算外置到 full_output_ref；需要全文时用 file_read 读取该路径",
    }
    event = {
        "event_type": "llm.tool_output.externalized",
        "call_id": str(call_id or ""),
        "tool_round": int(tool_round),
        "original_chars": len(serialized),
        "view_chars": len(json.dumps(view, ensure_ascii=False, default=str)),
        "bytes": len(serialized.encode("utf-8", errors="replace")),
        "sha256": digest,
        "ref": reference,
    }
    return view, event
