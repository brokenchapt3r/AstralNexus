"""Strict adapter for the Agent-produced astralnexus.test-results/v1 format.

.. warning:: **This module currently has no production caller.**

   It was written (doc 36, 2026-10-04) to close ``AC-EXECUTION-PROVENANCE`` inside
   ``GlobalGovernor.verify_delivery``: a machine-readable test report had to be
   tied to a recorded execution -- same path, same sha256, inside the 3600 s
   window -- or the delivery gate rejected it.

   On 2026-10-07 the user directed that the delivery gate be reduced to
   DeerFlow's single rule ("produced something, and the work's own declaration
   claims it").  ``AC-EXECUTION-PROVENANCE`` was one of the ~30 retired
   criteria, so nothing calls this module any more.  It is kept, tested and
   marked rather than deleted: the surviving half of「报告不自证」is the
   orchestrator's local report-completion path, and reinstating this leg is a
   deliberate decision, not a restoration of code that quietly rotted.

   See ``02_运行产物/capability_migration/b33_contraction/
   FIX_DELIVERY_SIMPLIFICATION.md``.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def v1_case_counts(payload: dict[str, Any]) -> tuple[bool, int, int]:
    """Validate totals against every concrete case; aliases never hide failures."""
    cases = payload.get('tests')
    totals = payload.get('totals')
    if not isinstance(cases, list) or not cases or not isinstance(totals, dict):
        return False, 0, 0
    counts: Counter[str] = Counter()
    identities: set[str] = set()
    allowed = {'passed', 'failed', 'error', 'xpassed', 'xfailed', 'skipped'}
    for case in cases:
        if not isinstance(case, dict):
            return False, 0, 0
        identity, outcome = case.get('nodeid'), case.get('outcome')
        if (not isinstance(identity, str) or not identity or identity in identities
                or not isinstance(outcome, str) or outcome not in allowed):
            return False, 0, 0
        identities.add(identity)
        counts[outcome] += 1
    total = len(cases)
    if type(totals.get('collected')) is not int or totals['collected'] != total:
        return False, total, counts['failed'] + counts['error']
    if type(payload.get('cases')) is not int or payload['cases'] != total:
        return False, total, counts['failed'] + counts['error']
    for outcome in allowed:
        key = 'errors' if outcome == 'error' else outcome
        value = totals.get(key, 0)
        if type(value) is not int or value != counts[outcome]:
            return False, total, counts['failed'] + counts['error']
        if key in payload and (type(payload[key]) is not int or payload[key] != value):
            return False, total, counts['failed'] + counts['error']
    return True, total, counts['failed'] + counts['error']


def has_recorded_report_execution(path: Path, run_dir: Path) -> bool:
    """Local-clock compatibility requires the exact bytes in a successful receipt.

    Read-only ledger lookup. No ledger creation, timestamp rewrite or approval.
    """
    db_path = run_dir / '.astralnexus_state/executions.sqlite'
    try:
        relative = path.resolve().relative_to(run_dir.resolve()).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not db_path.is_file():
            return False
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            rows = db.execute("SELECT id,outcome FROM executions WHERE status='succeeded'")
            for execution, raw in rows:
                outcome = json.loads(raw or '{}')
                if (outcome.get('owned_files', {}).get(relative) or {}).get('sha256') != digest:
                    continue
                for (receipt_raw,) in db.execute('SELECT payload FROM receipts WHERE execution_id=?', (execution,)):
                    receipt = json.loads(receipt_raw)
                    if (receipt.get('tool') in {'terminal_exec', 'python_exec', 'pytest_run'}
                            and receipt.get('status') == 'success' and receipt.get('returncode') == 0
                            and (receipt.get('files', {}).get(relative) or {}).get('sha256') == digest):
                        return True
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return False
    return False
