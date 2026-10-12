"""Execution identity and receipt persistence; task authority stays in Governor/TaskGraph."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager
from .execution_fence import (FenceProvider, current_execution_fence, guard_execution_fence,
    current_execution_identity, guard_active_execution, ExecutionOwnershipLost)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def snapshot_files(root: Path, paths: list[str]) -> dict[str, Any]:
    root = root.resolve()
    inventory: dict[str, Any] = {}
    for raw in paths:
        base = (root / raw).resolve()
        if not base.is_relative_to(root):
            continue
        for path in (base.rglob("*") if base.is_dir() else [base]):
            if not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if any(part in {"node_modules", ".git", ".venv", "__pycache__", ".astralnexus_state", ".astralnexus_tool_outputs"} for part in path.parts):
                continue
            # Fix AG (doc 18, 2026-09-15 r13b): SQLite sidecars carry no durable
            # content claim — the .db file is the artifact — and they mutate
            # whenever the database is merely OPENED, including read-only.
            # accept() opens every owned .db for quick_check/foreign_key_check
            # and then compares this snapshot against the finish-time one, so
            # the integrity check invalidated itself: live PATCH-R004-RUNTIME
            # was approved at 0.9917 with all six dimensions >= 0.95 and was
            # then rejected as "Execution evidence is stale; files changed since
            # execution" one second later, on -wal/-shm bytes its own verifier
            # had just touched.  Excluding the sidecars keeps the real guarantee
            # (the .db itself is still hashed) without the false positive.
            if path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
            inventory[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    return inventory


def _file_inventory_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    """Which files were added, removed or rewritten between two snapshots."""
    before = before or {}
    after = after or {}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(key for key in set(before) & set(after) if before[key] != after[key]),
    }


def _is_under(candidate: str, root: str) -> bool:
    """True when ``candidate`` names ``root`` or something inside it."""
    a = str(candidate or "").replace("\\", "/").strip().strip("/")
    b = str(root or "").replace("\\", "/").strip().strip("/")
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/")


def _run_relative(path: str, root: Path | None) -> str:
    """Normalize a declared path to the run-relative posix form (Fix AK).

    A contract may carry the same artifact several times in different spellings.
    The live r13b contracts hold 58 upstream entries for roughly a dozen files,
    each registered as a Windows absolute path AND as a run-relative one, often
    repeatedly::

        C:\\...\\run\\deliverables\\specs\\api-contract.md
        deliverables\\specs\\api-contract.md
        C:\\...\\run\\deliverables\\specs\\api-contract.md

    Ownership is declared run-relative, so a prefix test against the raw string
    matches the relative spelling and silently misses every absolute one -- the
    self-owned entry would slip back through the upstream gate and re-create the
    false veto this fix exists to remove.  Comparison therefore happens on one
    normalized form, and a path outside the run is left untouched so it stays
    external (and fails the containment test, as it should).
    """
    text = str(path or "").strip()
    if not text:
        return ""
    if root is not None:
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(Path(root).resolve()).as_posix()
            except (ValueError, OSError):
                return text.replace("\\", "/")
    return text.replace("\\", "/")


def external_upstream_paths(contract: dict[str, Any], root: Path | None = None) -> list[str]:
    """Upstream inputs this execution does NOT itself own (Fix AK).

    Ownership is exclusive: the contract grants this execution write authority
    over ``owned_paths``, so a change there is authorized by construction and is
    already judged by the owned-files check.  An upstream artifact that lies
    inside the execution's own owned tree therefore carries no integrity signal
    at all -- and worse, it guarantees a false veto, because doing the node's
    job is what changes those bytes.

    Live regression (doc 18, r13b gen 15): PATCH-R006-RUNTIME took over
    PATCH-R005-RUNTIME via recovery_of, inheriting its owned_paths while also
    listing its outputs as upstream artifacts.  It then did exactly what its
    contract asked -- rewrote launch_recipe.json and startup_run1.log, cleaned
    up its predecessor's scratch, wrote its own evidence -- and was rejected
    with "upstream input files changed since execution: added=6; removed=8;
    changed=5", every one of those paths under deliverables/app/backend/_runtime/,
    which it owns.  No execution of that shape could ever have been accepted.

    Upstream artifacts the execution does not own are unaffected and stay under
    the unconditional hard gate.
    """
    owned = [str(item) for item in contract.get("owned_paths", []) if str(item).strip()]
    paths: list[str] = []
    for item in contract.get("upstream_artifacts", []):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = _run_relative(str(item["path"]), root)
        if any(_is_under(path, owned_root) for owned_root in owned):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def owned_excluded_inventory(
    inventory: dict[str, Any], owned: list[str] | None
) -> dict[str, Any]:
    """An inventory without entries under a path this execution owns (r30 起 B).

    ``external_upstream_paths`` drops upstream ROOTS that lie inside the owned
    tree, but that cannot help when the relationship is the other way round: the
    owned deliverable lives INSIDE a declared upstream directory.  The node then
    does exactly its job -- writes ``app/summary.md`` under the upstream ``app/``
    -- and the raw inventory delta reports its own output as an upstream input
    change, vetoing the evidence it just produced::

        Execution evidence is stale; upstream input files changed since
        execution: added=1 [deliverables/app/收尾摘要.md]      (r30, N-202)

    Ownership is exclusive and already judged by the owned-files check, so owned
    entries are removed from BOTH sides before the comparison.  Genuinely
    external siblings under the same directory (``app/config.json``) stay in and
    keep vetoing staleness.
    """
    roots = [str(item).replace("\\", "/") for item in (owned or []) if str(item).strip()]
    if not roots:
        return dict(inventory or {})
    return {
        path: value
        for path, value in dict(inventory or {}).items()
        if not any(_is_under(path, root) for root in roots)
    }


def _describe_inventory_delta(before: dict[str, Any], after: dict[str, Any], limit: int = 8) -> str:
    """A staleness veto must name the files it vetoed on (Fix AH)."""
    delta = _file_inventory_delta(before, after)
    parts = []
    for label in ("added", "removed", "changed"):
        names = delta[label]
        if not names:
            continue
        shown = ", ".join(names[:limit])
        if len(names) > limit:
            shown += f", ... (+{len(names) - limit})"
        parts.append(f"{label}={len(names)} [{shown}]")
    return "; ".join(parts) or "no observable difference"


class ExecutionStore:
    """One journal per run, shared by workers and the independent verifier.

    IDs belong to runtime executions, never provider tool-call IDs. Each method
    opens a short transaction; no connection crosses worker thread boundaries.
    """
    def __init__(self, root: Path, *, fence_token_provider: FenceProvider | None = None):
        self.root = root.resolve()
        self.fence_token_provider = fence_token_provider or current_execution_fence(self.root)
        self.path = self.root / ".astralnexus_state" / "executions.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY, node_id TEXT NOT NULL, status TEXT NOT NULL,
                    contract TEXT NOT NULL, input_files TEXT NOT NULL, started TEXT NOT NULL,
                    outcome TEXT, acceptance TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    execution_id TEXT NOT NULL REFERENCES executions(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_delivery (
                    run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_receipt_sets (
                    request_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
                    receipt_ids TEXT NOT NULL, frozen_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(executions)')}
            for name, definition in [('owner_id', 'TEXT'), ('generation', 'INTEGER'), ('owner_run_id', 'TEXT')]:
                if name not in columns: db.execute(f'ALTER TABLE executions ADD COLUMN {name} {definition}')

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, node_id: str, contract: dict[str, Any]) -> str:
        execution_id = uuid.uuid4().hex
        upstream = [str(item["path"]) for item in contract.get("upstream_artifacts", []) if isinstance(item, dict) and item.get("path")]
        inputs = snapshot_files(self.root, upstream)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            token = guard_execution_fence(self.root, self.fence_token_provider, db) or {}
            db.execute("INSERT INTO executions(id,node_id,status,contract,input_files,started,owner_id,generation,owner_run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (execution_id, node_id, "running", _json(contract), _json(inputs), datetime.now(timezone.utc).isoformat(),
                        token.get('owner_id'), token.get('generation'), token.get('run_id')))
        return execution_id

    def _guard_writer(self, db: sqlite3.Connection, execution_id: str, *, reuse: bool = False) -> dict[str, Any] | None:
        token = guard_execution_fence(self.root, self.fence_token_provider, db)
        row = db.execute('SELECT owner_id,generation,owner_run_id FROM executions WHERE id=?', (execution_id,)).fetchone()
        if token is not None and not reuse and row is not None and tuple(row) != (token['owner_id'], token['generation'], token['run_id']):
            raise ExecutionOwnershipLost('execution belongs to a different owner or generation')
        return token

    def append(self, execution_id: str, observation: dict[str, Any]) -> dict[str, Any]:
        receipt = {**observation, "receipt_id": uuid.uuid4().hex, "execution_id": execution_id,
                   "recorded_at": datetime.now(timezone.utc).isoformat()}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            token = self._guard_writer(db, execution_id)
            receipt['ownership'] = {key: token[key] for key in ('owner_id', 'generation', 'run_id')} if token else None
            row = db.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None or row[0] != "running":
                raise ValueError("Cannot append evidence to missing or terminal execution")
            db.execute("INSERT INTO receipts(id,execution_id,payload) VALUES(?,?,?)",
                       (receipt["receipt_id"], execution_id, _json(receipt)))
        return receipt

    def stage_response(self, execution_id: str, response: dict[str, Any]) -> None:
        """Preserve a model return before the graph commit barrier.

        Staging is not an execution success or acceptance verdict. Late returns
        after a wave deadline remain raw recovery evidence, never graph edits.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._guard_writer(db, execution_id)
            row = db.execute("SELECT status,outcome FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None or row[0] != "running":
                return
            outcome = json.loads(row[1]) if row[1] else {}
            outcome["staged_response"] = response
            db.execute("UPDATE executions SET outcome=? WHERE id=? AND status='running'", (_json(outcome), execution_id))

    def finish(self, execution_id: str, status: str, outcome: dict[str, Any]) -> None:
        if status not in {"succeeded", "failed", "partial", "interrupted"}:
            raise ValueError("Invalid execution outcome")
        state = self.get(execution_id)
        outcome = {**outcome, "owned_files": snapshot_files(self.root, list(state["contract"].get("owned_paths", [])))}
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._guard_writer(db, execution_id)
            updated = db.execute("UPDATE executions SET status=?,outcome=? WHERE id=? AND status='running'",
                                 (status, _json(outcome), execution_id)).rowcount
            if not updated:
                raise ValueError("Execution already finished or missing")

    def get(self, execution_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute('BEGIN')
            row = db.execute("SELECT node_id,status,contract,input_files,outcome,acceptance,owner_id,generation,owner_run_id FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown execution identity")
            receipts = [json.loads(item[0]) for item in db.execute("SELECT payload FROM receipts WHERE execution_id=? ORDER BY seq", (execution_id,))]
        return {"execution_id": execution_id, "node_id": row[0], "status": row[1], "contract": json.loads(row[2]),
                "input_files": json.loads(row[3]), "outcome": json.loads(row[4]) if row[4] else None,
                "acceptance": json.loads(row[5]) if row[5] else None, "receipts": receipts,
                "ownership": {'owner_id': row[6], 'generation': row[7], 'run_id': row[8]} if row[6] is not None else None}

    def evidence(self, execution_id: str) -> dict[str, Any]:
        state = self.get(execution_id)
        contract = state["contract"]
        current = snapshot_files(self.root, list(contract.get("owned_paths", [])))
        upstream = external_upstream_paths(contract, self.root)
        owned = [str(item) for item in contract.get("owned_paths", []) if str(item).strip()]
        inputs_now = owned_excluded_inventory(snapshot_files(self.root, upstream), owned)
        # The baseline was recorded over ALL upstream artifacts at begin(); it
        # must be compared over the same, self-owned-excluded set (Fix AK, and
        # the reverse containment case in Fix r30 起 B).
        inputs_then = owned_excluded_inventory(
            {key: value for key, value in (state["input_files"] or {}).items()
             if any(_is_under(key, root) for root in upstream)},
            owned,
        )
        receipts = []
        # Preserve recent evidence per tool/outcome before filling the tail.
        # A burst of compile validations must not evict the HTTP/test command
        # or its nonzero exit receipt from the verifier's bounded projection.
        recorded = state["receipts"]
        representatives: dict[tuple[Any, ...], int] = {}
        for index, item in enumerate(recorded):
            category = (item.get("tool"), item.get("status"), item.get("returncode"))
            representatives[category] = index
        selected = set(sorted(representatives.values(), reverse=True)[:16])
        for index in range(len(recorded) - 1, -1, -1):
            if len(selected) >= 16:
                break
            selected.add(index)
        for index in sorted(selected):
            item = recorded[index]
            receipts.append({**{key: value for key, value in item.items() if key not in {"files", "raw_result"}},
                             "owned_files_changed": item.get("files") != current})
        return {"execution_id": execution_id, "status": state["status"],
                "upstream_files_changed": inputs_now != inputs_then,
                "receipts": receipts, "omitted_receipts": max(0, len(state["receipts"]) - len(receipts)),
                "acceptance": state["acceptance"],
                "owned_files_changed_since_finish": (
                    current != (state["outcome"] or {}).get("owned_files") if state["status"] != "running" else None),
                "previous_execution_id": contract.get("previous_execution_id")}

    def interrupt_open(self, execution_id: str) -> None:
        if self.get(execution_id)["status"] == "running":
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                token = self._guard_writer(db, execution_id, reuse=True)
                db.execute("UPDATE executions SET status='interrupted',outcome=? WHERE id=? AND status='running'",
                    (_json({'reason': 'checkpoint_resume', 'interrupted_by': token}), execution_id))

    def previous_evidence(self, execution_id: str) -> dict[str, Any] | None:
        state = self.get(execution_id)
        previous_id = state["contract"].get("previous_execution_id")
        if not previous_id:
            return None
        previous = self.get(previous_id)
        return {"execution_id": previous_id, "status": previous["status"],
                "outcome_summary": str((previous["outcome"] or {}).get("content") or (previous["outcome"] or {}).get("error") or "")[-2000:],
                "evidence": self.evidence(previous_id)}

    def accept(self, execution_id: str, decision: dict[str, Any],
               verified_files: dict[str, Any] | None = None) -> None:
        scope = decision.get("acceptance_scope", "business")
        if scope not in {"execution", "business"}:
            raise ValueError("unknown execution acceptance scope")
        decision = {**decision, "acceptance_scope": scope, "business_accepted": bool(decision.get("approved")) and scope == "business"}
        if decision.get("approved"):
            from .data_checks import check_sqlite_artifacts
            checks = check_sqlite_artifacts(self.root, list(self.get(execution_id)["contract"].get("owned_paths", [])))
            if any(check["result"]["status"] != "success" for check in checks):
                raise ValueError("Data validation failed; execution cannot be accepted")
            # Hard gate (doc 17 第一批⑥, DeerFlow acceptance_checks port):
            # exit code 0 is command-execution evidence only. When the
            # contract declares tests_passed criteria, approval requires each
            # to anchor to a recorded execution — matching command, success
            # status, recognized passing summary, and file snapshots that
            # have not changed since. Self-reported "passed" text, an
            # unrelated successful command, or a stale success cannot pass.
            state = self.get(execution_id)
            from .acceptance_checks import EXECUTION_KINDS, parse_criterion
            criteria = [
                str(item).strip()
                for item in state["contract"].get("acceptance_criteria", [])
                if parse_criterion(str(item))[0] in EXECUTION_KINDS
            ]
            if criteria:
                from .acceptance_checks import bind_criterion_paths, run_acceptance_checks
                from .verification_identity import (
                    declared_premise_paths,
                    premise_versions,
                )
                receipts = list(state["receipts"])
                contract = state["contract"]
                # Fix r29 (doc 51 §16): the gate must judge the anchor against the
                # same premise set the guard used, otherwise a legitimate
                # re-verification is refused by the guard and then rejected here
                # as stale/blocked with no way out.
                declared = declared_premise_paths(
                    acceptance_criteria=contract.get("acceptance_criteria", []),
                    upstream_artifacts=contract.get("upstream_artifacts", []),
                    owned_paths=contract.get("owned_paths", []),
                    root=self.root,
                )
                criteria, _ = bind_criterion_paths(
                    criteria, declared=declared, root=self.root, strict=False,
                )
                current_premises = premise_versions(self.root, declared)
                latest: dict[str, Any] = {}
                for receipt in reversed(receipts):
                    files = receipt.get("files")
                    if isinstance(files, dict) and files:
                        latest = files
                        break
                blocking = [
                    observation
                    for observation in run_acceptance_checks(
                        criteria, receipts=receipts, execution_id=execution_id,
                        latest_files=latest,
                        premise_versions=current_premises,
                        declared_premises=declared,
                        root=self.root,
                    )
                    if not (observation["checked"] and observation["holds"])
                ]
                if blocking and scope == "execution":
                    decision = {**decision, "criterion_diagnostics": blocking}
                if blocking and scope == "business":
                    # Fix P5#17: carry the observation detail (e.g. the
                    # recognized-summary vocabulary for
                    # TEST_SUMMARY_NOT_RECOGNIZED) into the rejection message
                    # so repair contexts teach the deterministic fix.
                    detail = "; ".join(
                        f"{item['criterion_id']} [{item['reason']}]"
                        + (f" {item['detail']}" if item.get("detail") else "")
                        for item in blocking
                    )
                    raise ValueError(
                        "Execution claims not anchored to recorded successful executions; "
                        f"execution cannot be accepted: {detail}"
                    )
        evidence = self.evidence(execution_id)
        # Fix AH (doc 18, 2026-09-15 r13b): the staleness gate used the WRONG
        # reference point.  A verdict is a statement about the bytes the
        # Verifier READ (T_v), but the gate compared accept-time bytes against
        # the finish-time snapshot (T_f).  Any settling in [T_f, T_v] — a
        # launched service still flushing its log, a stop-flag written by the
        # shutdown handshake, a WAL checkpoint on clean close — therefore
        # vetoed a verdict that had already been formed on the SETTLED bytes.
        # Live evidence: PATCH-R004-RUNTIME, whose contracted job is to start
        # the backend and capture runtime evidence, was rejected three times
        # this way, each time rewriting a real score to 0.0/inconclusive.
        # Same defect class as Fix AG: the integrity check invalidated by
        # activity the framework itself set in motion.
        #
        # The guarantee is unchanged in substance and is NOT weakened:
        #   * upstream inputs must still be byte-identical — always hard-fails;
        #   * the bytes the verdict is ABOUT must not move under it: when the
        #     Verifier supplies its own reading basis, drift in [T_v, T_a] is
        #     still a hard failure;
        #   * with no basis supplied the legacy T_f comparison stands, so every
        #     existing caller keeps its original strictness.
        # Pre-verification settling is not discarded, it is RECORDED, so a
        # worker that rewrote its outputs after finishing stays visible in the
        # journal instead of being silently forgiven.
        owned_now = snapshot_files(self.root, list(self.get(execution_id)["contract"].get("owned_paths", [])))
        settled_drift = _file_inventory_delta(
            (self.get(execution_id)["outcome"] or {}).get("owned_files") or {}, owned_now
        )
        if decision.get("approved"):
            if evidence["upstream_files_changed"]:
                contract_now = self.get(execution_id)["contract"]
                owned_roots = [
                    str(item) for item in contract_now.get("owned_paths", []) if str(item).strip()
                ]
                external = external_upstream_paths(contract_now, self.root)
                baseline = owned_excluded_inventory(
                    {
                        key: value
                        for key, value in (self.get(execution_id)["input_files"] or {}).items()
                        if any(_is_under(key, root) for root in external)
                    },
                    owned_roots,
                )
                raise ValueError(
                    "Execution evidence is stale; upstream input files changed since execution: "
                    + _describe_inventory_delta(
                        baseline,
                        owned_excluded_inventory(snapshot_files(self.root, external), owned_roots),
                    )
                )
            if verified_files is None:
                if evidence["owned_files_changed_since_finish"]:
                    raise ValueError(
                        "Execution evidence is stale; owned files changed since execution finished: "
                        + _describe_inventory_delta(
                            (self.get(execution_id)["outcome"] or {}).get("owned_files") or {}, owned_now
                        )
                    )
            elif owned_now != verified_files:
                raise ValueError(
                    "Execution evidence is stale; owned files changed after the Verifier read them: "
                    + _describe_inventory_delta(verified_files, owned_now)
                )
        if settled_drift["added"] or settled_drift["removed"] or settled_drift["changed"]:
            decision = {**decision, "pre_verification_drift": settled_drift}
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            token = self._guard_writer(db, execution_id, reuse=True)
            row = db.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None or row[0] == "running":
                raise ValueError("Cannot verify a missing or running execution")
            if decision.get("approved") and row[0] != "succeeded":
                raise ValueError("Failed or interrupted execution cannot be accepted")
            if token is not None:
                decision = {**decision, 'verification_owner': {key: token[key] for key in ('owner_id', 'generation', 'run_id')}}
            db.execute("UPDATE executions SET acceptance=? WHERE id=?", (_json(decision), execution_id))

    # --- Frozen model-visible receipt sets (doc 17 第一批⑤, DeerFlow port) ---
    # The receipt set a model request ACTUALLY saw is frozen at assembly time.
    # Later reference checks resolve against the request's own frozen set, so
    # compaction or a shifted projection window can never cross wires: a
    # receipt the model never saw in that request cannot be cited from it.

    def freeze_request_receipts(self, request_id: str, execution_id: str, receipt_ids: list[str]) -> None:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if current_execution_identity(self.root) == execution_id:
                guard_active_execution(self.root, execution_id)
            self._guard_writer(db, execution_id, reuse=True)
            identities = sorted({str(item) for item in receipt_ids if str(item).strip()})
            if db.execute('SELECT 1 FROM executions WHERE id=?', (execution_id,)).fetchone() is None:
                raise ValueError('unknown execution for receipt set')
            existing = db.execute('SELECT execution_id,receipt_ids FROM request_receipt_sets WHERE request_id=?', (request_id,)).fetchone()
            if existing is not None:
                if existing[0] == execution_id and json.loads(existing[1]) == identities: return
                raise ValueError('frozen request receipt set cannot be rewritten')
            for receipt_id in identities:
                if db.execute('SELECT 1 FROM receipts WHERE id=?', (receipt_id,)).fetchone() is None:
                    raise ValueError('receipt set references an unknown receipt')
            db.execute(
                "INSERT INTO request_receipt_sets(request_id,execution_id,receipt_ids,frozen_at) VALUES(?,?,?,?)",
                (str(request_id), str(execution_id),
                 _json(identities),
                 datetime.now(timezone.utc).isoformat()),
            )

    def request_receipts(self, request_id: str) -> list[str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT receipt_ids FROM request_receipt_sets WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        return [str(item) for item in json.loads(row[0])] if row else []

    def resolve_receipt_reference(self, request_id: str, receipt_id: str) -> str:
        """visible | unknown_to_request | unknown_request.

        ``unknown_to_request`` means the receipt exists in the ledger but was
        NOT in this request's frozen set — the caller must reverify against
        the full ledger instead of trusting the citation (引用未知或已失效时
        复验, per the porting plan).
        """
        frozen = self.request_receipts(request_id)
        if not frozen:
            return "unknown_request"
        return "visible" if str(receipt_id) in frozen else "unknown_to_request"

    def record_delivery(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist the unique terminal delivery fact before terminal run status."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            guard_execution_fence(self.root, self.fence_token_provider, db)
            existing = db.execute("SELECT payload FROM run_delivery WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                previous = json.loads(existing[0])
                if _json(previous) != _json(payload):
                    raise ValueError("run.delivery is immutable once finalized")
                return previous
            db.execute("INSERT INTO run_delivery(run_id,payload,recorded_at) VALUES(?,?,?)",
                       (run_id, _json(payload), datetime.now(timezone.utc).isoformat()))
        return json.loads(_json(payload))

    def read_delivery(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM run_delivery WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None
