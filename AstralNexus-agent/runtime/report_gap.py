"""Report-only gaps: reuse execution evidence, never the acceptance verdict.

Design contract (user directives m02729 / m02774, recorded in
``02_运行产物/capability_migration/b25_artifact_contract/DESIGN_ARTIFACT_CONTRACT.md``
§8 and in
``04_归档/项目文档/2026-10-10_历史修复记录/51_全栈能力优先适配.md`` §22):

* ① "only the report is missing" is decided by the **framework**, from recorded
  execution facts.  A worker claiming "all green" is not evidence, so the
  classification demands a real successful execution (``status == "success"``
  and ``returncode == 0``), checked criteria, and no other failure.
* ② what is reused is **execution evidence, not the acceptance verdict**: the
  final delivery gate still re-verifies the functional criteria, and the node
  whose report was missing must not be flipped to ``completed``.  The completion
  itself happens **inside the same execution** (``local_completion_plan``):
  zero added graph nodes, zero added executions, no re-run of the functional
  tests the completion cites.
* ③ completing a report must not invalidate functional evidence: report-class
  paths are excluded from the functional premise set by
  ``runtime.verification_identity.is_report_class_path``, so the receipt that
  backs the functional criteria stays valid while the report is written.
* ④ budget exhaustion keeps the **primary** failure reason: such a run is never
  reclassified as a report gap.  ``budget_exhausted`` is reported as-is and the
  missing artifact stays an additional gap (live r30: ``MissingDeclaredArtifacts``
  must never replace ``budget_exhausted`` for N-103).

Live defect this module exists to prevent (r30): a node whose functional test
passed 53/53 was failed only because ``deliverables/runs/browser/defects.json``
was absent, and the recovery re-ran the whole node with a fresh worker.  The
report class is knowable, so the framework must be able to say "only the report
is missing" instead of re-running functional work.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from AstralNexus.runtime.verification_identity import is_report_class_path

#: Machine-readable classification emitted when (and only when) the framework
#: decides a node is missing report-class artifacts and nothing else.
REPORT_GAP_REASON = "REPORT_ONLY_GAP"

#: 生成器版本（用户 2026-10-06 第⑥条：不新增节点、不新增执行，不等于不需要审计）。
#: 写在补全记录里，让"这份记录由框架的哪个版本产出"可被逐字追溯；被拒路径把同一个
#: 常量写进既有事件 ``governor.report_completion.rejected`` 的 payload。
GENERATOR = "astralnexus.report_local_completion/1"

#: Admission reasons that veto reuse, in reporting order.  They are returned
#: alongside ``None`` so callers can log *why* the framework refused to treat a
#: report gap as reusable without inventing their own wording.
VETO_BUDGET_EXHAUSTED = "budget_exhausted"
VETO_NO_MISSING_PATHS = "no_missing_paths"
VETO_NON_REPORT_MISSING = "non_report_artifact_missing"
VETO_ADDITIONAL_FAILURES = "additional_failures"
VETO_NO_EXECUTION = "no_anchored_execution"
VETO_NOT_A_REAL_SUCCESS = "execution_not_a_real_success"
VETO_ASSERTIONS_UNCHECKED = "assertions_not_checked"
VETO_STALE = "evidence_stale"


@dataclass(frozen=True)
class ReportGap:
    """A framework-decided "only the report is missing" classification.

    ``missing_reports`` are the report-class paths that never materialized;
    ``execution_id`` is the *existing* execution whose receipts may back the
    still-valid functional criteria.  Nothing here says the node passed: the
    delivery gate re-verifies, and ``reuse`` only ever means "these functional
    receipts do not have to be produced again".
    """

    missing_reports: tuple[str, ...]
    execution_id: str
    reason: str = REPORT_GAP_REASON
    rerun_functional_tests: bool = False
    reuse_execution_evidence: bool = True
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """Shape used for node metadata / trace payloads."""
        return {
            "reason": self.reason,
            "missing_reports": list(self.missing_reports),
            "reuse_execution_id": self.execution_id,
            "reuse_execution_evidence": self.reuse_execution_evidence,
            "rerun_functional_tests": self.rerun_functional_tests,
        }


@dataclass(frozen=True)
class ReportGapDecision:
    """Either a :class:`ReportGap` or the veto that kept the node a normal failure."""

    gap: ReportGap | None
    veto: str = ""

    @property
    def is_gap(self) -> bool:
        return self.gap is not None


def decide_report_gap(
    *,
    missing_paths: Sequence[str],
    execution_id: str = "",
    execution_status: str = "",
    returncode: int | None = None,
    assertions_checked: bool = False,
    budget_exhausted: bool = False,
    stale: bool = False,
    additional_failures: Sequence[str] = (),
) -> ReportGapDecision:
    """Decide whether a node's only gap is a missing report-class artifact.

    Returns a :class:`ReportGapDecision`; ``veto`` names the first rejected
    condition so the caller can log a reason instead of guessing.  The order is
    deliberate: budget exhaustion (④) outranks everything, so a run that ran out
    of budget is never re-labelled as a report gap.
    """
    reports = [str(item).strip() for item in missing_paths if str(item).strip()]
    if budget_exhausted:
        return ReportGapDecision(None, VETO_BUDGET_EXHAUSTED)
    if not reports:
        return ReportGapDecision(None, VETO_NO_MISSING_PATHS)
    if stale:
        return ReportGapDecision(None, VETO_STALE)
    if any(str(item).strip() for item in additional_failures):
        return ReportGapDecision(None, VETO_ADDITIONAL_FAILURES)
    if not str(execution_id).strip():
        return ReportGapDecision(None, VETO_NO_EXECUTION)
    if str(execution_status).strip().lower() != "success" or returncode != 0:
        return ReportGapDecision(None, VETO_NOT_A_REAL_SUCCESS)
    if not assertions_checked:
        return ReportGapDecision(None, VETO_ASSERTIONS_UNCHECKED)
    non_report = [path for path in reports if not is_report_class_path(path)]
    if non_report:
        return ReportGapDecision(None, VETO_NON_REPORT_MISSING)
    return ReportGapDecision(
        ReportGap(
            missing_reports=tuple(reports),
            execution_id=str(execution_id).strip(),
            detail={"non_report_missing": ()},
        )
    )


def classify_report_gap(**kwargs: Any) -> ReportGap | None:
    """Convenience wrapper: the gap, or ``None`` when reuse is not admissible."""
    return decide_report_gap(**kwargs).gap


def report_requirement_satisfiable_from_receipts(path: str) -> bool:
    """Can a provenance record at ``path`` satisfy that report's content requirement?

    **No -- for every report-class path.**  用户 2026-10-06（报告不自证）：the
    framework-authored record deterministically summarises what the ledger already
    holds (execution id, command, exit code, receipt id); it adds no test
    conclusion, no defect disposition and no human judgement.  A provenance
    record therefore can never *be* the report the contract asked for, and the
    answer must stay ``False`` so the delivery gate cannot accept it as proof
    that the report requirement is met.

    This is a **pure query**: no side effects, no state, no new decision point --
    ``decide_report_gap`` remains the only place that classifies a gap, and no
    second suffix table is introduced (the callers reuse ``_JSON_REPORT_SUFFIXES``
    and ``is_report_class_path``).  Callers are the delivery gate
    (``core/governor.py`` -> ``verify_delivery``) and
    ``local_completion_document`` itself, which declares ``content_complete`` and
    ``report_requirement`` from this same answer instead of hard-coding them.

    ``path`` names the report under question so every call site states what it is
    asking about; the answer deliberately does not depend on it.
    """
    del path  # 被询问的报告类路径；"溯源记录是否满足它"的答案与路径无关。
    return False


#: Machine-readable kind of the in-execution completion action.  It is NOT a task
#: graph node kind: nothing is added to the graph and nothing is executed.
LOCAL_COMPLETION_KIND = "report_local_completion"

#: Suffixes whose report file must stay parseable, so the completion writes JSON.
_JSON_REPORT_SUFFIXES = (".json", ".jsonl")


def local_completion_plan(gap: ReportGap, *, node_id: str = "") -> dict[str, Any]:
    """Deterministic shape of the in-execution report completion (m02774 ①②③).

    User directive 2026-10-06 (§5): a green execution that is only missing its
    report is completed **inside the same execution** by citing the recorded
    receipts -- not by spawning a successor Worker, a new graph node or a new
    execution record.  The plan therefore declares zero added nodes and zero
    added executions, and it can never run tests.

    The framework -- not the worker and not an LLM proposal -- decides that only a
    report is missing and what the completion may touch: exactly the missing
    report files, never product code or a test script, so it cannot invalidate the
    evidence it exists to preserve.
    """
    reports = [str(item).strip() for item in gap.missing_reports if str(item).strip()]
    return {
        "kind": LOCAL_COMPLETION_KIND,
        "node_id": node_id,
        "owned_paths": reports,
        "reuse_execution_id": gap.execution_id,
        "reuse_execution_evidence": gap.reuse_execution_evidence,
        "rerun_functional_tests": False,
        "executes_tests": False,
        "adds_nodes": 0,
        "adds_executions": 0,
        "source": gap.reason,
    }


def local_completion_evidence(
    receipts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The anchored execution's OWN receipts, projected for verbatim citation.

    Only recorded ledger facts are projected: no worker narration, no summary, no
    inferred business conclusion.  This is the "引用原执行收据" half of the
    directive -- the completion has to say which execution and which receipts it
    reused, and a reader must be able to check each one against the store.
    """
    evidence: list[dict[str, Any]] = []
    for payload in receipts or ():
        if not isinstance(payload, Mapping):
            continue
        arguments = payload.get("arguments")
        command = ""
        if isinstance(arguments, Mapping):
            command = str(arguments.get("command") or "")
        elif isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping):
                command = str(parsed.get("command") or "")
        evidence.append({
            "receipt_id": str(payload.get("receipt_id") or ""),
            "tool": str(payload.get("tool") or ""),
            "call_id": str(payload.get("call_id") or ""),
            "status": str(payload.get("status") or ""),
            "returncode": payload.get("returncode"),
            "command": command[:400],
            "stdout_tail": str(payload.get("stdout_tail") or "")[:400],
        })
    return evidence


def local_completion_document(
    gap: ReportGap,
    *,
    path: str,
    node_id: str = "",
    receipts: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render one report file from recorded facts -- never from invention.

    The document is a provenance record, not a business conclusion: it carries the
    reused execution id, the rerun/execution flags and the cited receipts, and it
    says in-band that it adds no finding of its own.  ``.json``/``.jsonl`` targets
    are written as a JSON object so an existing report reader cannot crash on a
    format flip; every other report-class suffix gets the markdown rendering.

    审计字段（用户 2026-10-06 第⑥条："不新增节点不等于不需要审计"）：``generator``
    给出生成器版本，``source_execution_id`` 明确"来源执行"，``outcome`` 记录处理
    结果，``report_sha256`` 是"**去掉 ``report_sha256`` 这一个键/行之后的规范渲染
    字节**"的真实 SHA-256（先渲染字节 → 哈希 → 再写入，不留占位）。
    ``content_complete``／``report_requirement`` 不写死常量，而由纯查询
    :func:`report_requirement_satisfiable_from_receipts` 决定：溯源记录不满足报告的
    内容要求，因此恒为 ``False``／``"unresolved"``，并在 ``note`` 里写明不得据此
    报告判定验收。md 与 json 两条路径都带同一组字段。
    """
    evidence = local_completion_evidence(receipts)
    # 声明与判定同源：这里消费唯一那处纯查询（无副作用），不另立判定点。
    content_complete = report_requirement_satisfiable_from_receipts(path)
    header: dict[str, Any] = {
        "report_kind": "framework_local_report_completion",
        "provenance": "anchored_execution_receipts",
        "generator": GENERATOR,
        "note": (
            "Framework-authored provenance record for a report-class artifact "
            "whose functional evidence is already recorded.  It states no new "
            "business conclusion; read the cited receipts for the findings.  "
            "它没有满足报告的内容要求（content_complete=false，report_requirement="
            "\"unresolved\"），不得据此报告判定验收。"
        ),
        "report_path": str(path).replace("\\", "/").strip(),
        "node_id": str(node_id),
        "source_execution_id": gap.execution_id,
        "reuse_execution_id": gap.execution_id,
        "reuse_execution_evidence": gap.reuse_execution_evidence,
        "rerun_functional_tests": False,
        "executed_tests": False,
        "outcome": "written",
        "content_complete": content_complete,
        "report_requirement": "resolved" if content_complete else "unresolved",
        "reason": gap.reason,
        "evidence": evidence,
    }
    suffix = str(path).strip().lower()
    if suffix.endswith(_JSON_REPORT_SUFFIXES):
        # 先渲染（不含哈希键的规范字节）→ 哈希 → 再插入该键。``sort_keys=True``
        # 保证"删掉 report_sha256 这一行"就等于被哈希的字节（父代理可逐字复算）。
        canonical = json.dumps(header, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        header["report_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return json.dumps(header, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lines: list[str] = [
        f"# report completion: {header['report_path']}",
        "",
        f"- report_kind: {header['report_kind']}",
        f"- provenance: {header['provenance']}",
        f"- generator: {header['generator']}",
        f"- node_id: {header['node_id']}",
        f"- source_execution_id: {header['source_execution_id']}",
        f"- reuse_execution_id: {header['reuse_execution_id']}",
        f"- reuse_execution_evidence: {header['reuse_execution_evidence']}",
        f"- rerun_functional_tests: {header['rerun_functional_tests']}",
        f"- executed_tests: {header['executed_tests']}",
        f"- outcome: {header['outcome']}",
        f"- content_complete: {header['content_complete']}",
        f"- report_requirement: {header['report_requirement']}",
        f"- reason: {header['reason']}",
        "",
        str(header["note"]),
        "",
        "## cited receipts (anchored execution)",
        "",
    ]
    if not evidence:
        lines.append("- (the anchored execution recorded no receipts)")
    for index, item in enumerate(evidence, 1):
        lines.append(
            f"{index}. receipt_id={item['receipt_id']} tool={item['tool']} "
            f"status={item['status']} returncode={item['returncode']} "
            f"call_id={item['call_id']}"
        )
        if item["command"]:
            lines.append(f"   - command: {item['command']}")
        if item["stdout_tail"]:
            lines.append(f"   - stdout_tail: {item['stdout_tail']}")
    # 同一条纪律：先渲染完整正文（不含哈希行）→ 哈希 → 再把哈希行插进 header 块。
    # 复算步骤＝把这一行原样删掉，对剩余字节求 SHA-256。
    digest = hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
    hash_line = lines.index(f"- report_requirement: {header['report_requirement']}") + 1
    lines.insert(hash_line, f"- report_sha256: {digest}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# P5b（用户 2026-10-06 复核条件 1/2）：补出记录能不能当证据，取决于它**引用**的
# 执行/收据是否完整有效，而不是"谁写的"。判定只此一处纯查询
# （:func:`evaluate_completion_record`）。
#
# 2026-10-07 交付门简化后，这里的消费者只剩"局部补报告"路径
# （``core/orchestrator.py`` ``_complete_report_gap_locally``：已存在的补出记录
# 必须自哈希自洽且引用成立，才算完成）。交付门的 AC-EVIDENCE-* / AC-EXECUTION-
# PROVENANCE 两条腿已随简化移除，见 FIX_DELIVERY_SIMPLIFICATION.md。
# --------------------------------------------------------------------------

#: 「框架补出记录」的标记（唯一定义处）。``core/governor.py`` 复用同一常量，
#: 避免这套判据出现两份实现。
LOCAL_COMPLETION_RECORD_MARKERS = (
    "framework_local_report_completion",
    "astralnexus.report_local_completion/",
)

#: 结构化理由码：逐字写进判据 explanation、事件面与证据文档。
REASON_RECORD_UNREADABLE = "record_unreadable"
REASON_NOT_A_RECORD = "not_a_completion_record"
REASON_RECORD_UNPARSEABLE = "record_unparseable"
REASON_DIGEST_MISSING = "report_sha256_missing"
REASON_DIGEST_MISMATCH = "report_sha256_mismatch"
REASON_NO_EXECUTION_ID = "source_execution_id_missing"
REASON_EXECUTION_UNTRACEABLE = "cited_execution_untraceable"
REASON_EXECUTION_NOT_SUCCEEDED = "cited_execution_not_succeeded"
REASON_NO_CITED_RECEIPTS = "no_cited_receipts"
REASON_RECEIPT_MISSING = "cited_receipt_missing"
REASON_RECEIPT_MISMATCH = "cited_receipt_mismatch"
REASON_RECEIPT_NOT_A_SUCCESS = "cited_receipt_not_a_success"
REASON_RECEIPT_STALE = "cited_receipt_stale"


@dataclass(frozen=True)
class CompletionRecordVerdict:
    """一条补出记录能否作为**有效证据**支撑交付判据（结构化、可逐字复算）。

    ``traceable`` 是唯一结论：被引执行在账本、状态为 ``succeeded``，所引收据存在、
    属于该执行、与该执行记录一致且未过期，并且记录自身的 ``report_sha256`` 自洽。
    记录自声明 ``content_complete=false``／``report_requirement="unresolved"`` 不因
    此改变：可追溯只说明"它引用的证据成立"，不说明它是报告正文。
    """

    traceable: bool
    reasons: tuple[str, ...] = ()
    cited_execution_id: str = ""
    cited_receipt_ids: tuple[str, ...] = ()
    is_framework_record: bool = False
    digest_ok: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "traceable": self.traceable,
            "reasons": list(self.reasons),
            "cited_execution_id": self.cited_execution_id,
            "cited_receipt_ids": list(self.cited_receipt_ids),
            "is_framework_record": self.is_framework_record,
            "digest_ok": self.digest_ok,
        }


def _completion_record_path(ref: str, run_dir: Any) -> Path | None:
    """Resolve the record under the run workspace (draft staging included)."""
    raw = str(ref).replace("\\", "/").strip()
    if not raw:
        return None
    candidates: list[Path] = []
    direct = Path(str(ref))
    if direct.is_absolute():
        candidates.append(direct)
    if run_dir is not None:
        root = Path(str(run_dir))
        relative = raw.lstrip("/")
        candidates.append(root / relative)
        if relative.startswith("deliverables/"):
            candidates.append(
                root / ".delivery_staging" / relative[len("deliverables/"):]
            )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _completion_record_fields(text: str) -> dict[str, Any] | None:
    """Parse the two renderings the generator can emit (JSON object, Markdown)."""
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None
    fields: dict[str, Any] = {}
    receipts: list[dict[str, Any]] = []
    for line in text.splitlines():
        body = line.strip()
        if body.startswith("- ") and ": " in body:
            key, _, value = body[2:].partition(": ")
            fields[key.strip()] = value.strip()
            continue
        match = re.match(
            r"^\d+\.\s+receipt_id=(\S+) tool=(\S+) status=(\S+) "
            r"returncode=(\S+) call_id=(\S+)$",
            body,
        )
        if match:
            receipts.append({
                "receipt_id": match.group(1),
                "tool": match.group(2),
                "status": match.group(3),
                "returncode": match.group(4),
                "call_id": match.group(5),
            })
    if not fields:
        return None
    fields["evidence"] = receipts
    return fields


def _completion_record_digest(fields: Mapping[str, Any], text: str) -> bool:
    """Recompute ``report_sha256`` exactly as :func:`local_completion_document` renders.

    JSON: drop the ``report_sha256`` key and re-render canonically
    (``sort_keys=True``).  Markdown: delete the ``- report_sha256:`` line and hash the
    remaining bytes.  A mutated/truncated record therefore reads as **not intact**.
    """
    declared = str(fields.get("report_sha256") or "").strip()
    if text.lstrip().startswith("{"):
        body = {key: value for key, value in fields.items() if key != "report_sha256"}
        canonical = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        kept = [
            line for line in text.splitlines()
            if not line.strip().startswith("- report_sha256:")
        ]
        canonical = "\n".join(kept) + "\n"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return bool(declared) and declared == digest


def _cited_receipt_rows(fields: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = fields.get("evidence")
    rows: list[dict[str, Any]] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Mapping):
                rows.append({str(key): value for key, value in item.items()})
    return rows


def _ledger_execution(run_dir: Any, execution_id: str) -> tuple[dict[str, Any] | None, str]:
    """Read-only ledger lookup (never creates the journal directory).

    ``ExecutionStore`` would ``mkdir`` and create the sqlite file on construction, so
    this query opens the existing journal ``mode=ro`` instead -- the same idiom as
    ``runtime.test_report_contract.has_recorded_report_execution``.
    """
    if run_dir is None:
        return None, "no_run_dir"
    db_path = Path(str(run_dir)) / ".astralnexus_state" / "executions.sqlite"
    try:
        if not db_path.is_file():
            return None, "ledger_absent"
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT status,outcome FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if row is None:
                return None, "unknown_execution"
            receipts = [
                json.loads(item[0])
                for item in db.execute(
                    "SELECT payload FROM receipts WHERE execution_id=?", (execution_id,)
                )
            ]
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        return None, f"ledger_error:{type(exc).__name__}"
    return {"status": str(row[0]), "receipts": receipts}, ""


def _cited_receipt_is_stale(run_dir: Any, receipt: Mapping[str, Any]) -> str:
    """Freshness: every non-report file the receipt recorded must still match.

    Only recorded digests are compared -- no new registry, no new timestamp.  A
    receipt that recorded no file digests cannot be freshness-checked; that limit is
    reported in the evidence document instead of being papered over.  Report-class
    paths are excluded because writing the report legitimately changes them
    (``runtime.verification_identity.is_report_class_path``).
    """
    files = receipt.get("files")
    if run_dir is None or not isinstance(files, Mapping):
        return ""
    root = Path(str(run_dir))
    for relative, entry in files.items():
        name = str(relative).replace("\\", "/")
        if not name or is_report_class_path(name):
            continue
        if not isinstance(entry, Mapping):
            continue
        digest = str(entry.get("sha256") or "")
        if not digest:
            continue
        try:
            current = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            return name
        if current != digest:
            return name
    return ""


def evaluate_completion_record(ref: str, run_dir: Any = None) -> CompletionRecordVerdict:
    """Is ``ref`` usable evidence?  The **one** judgment the four gates consume.

    用户 2026-10-06 复核条件 1/2：不得只按文件名或生成者过滤——一份引用证据完整、
    有效的框架汇总报告也可能满足报告要求。判据因此是"引用证据是否完整、有效"：

    a. 记录可解析，且 ``report_sha256`` 自洽（删键/删行复算；同一口径同时用于
       "文件存在即完成"的识别：哈希不匹配 ⇒ 未完成 ⇒ 可原子重写）；
    b. 被引执行在 ``ExecutionStore`` 账本中存在且 ``status == "succeeded"``；
    c. 每条被引收据在该执行名下存在、``status``/``returncode`` 与账本逐字一致，
       且是成功收据（``status == "success"`` 且 ``returncode == 0``）；
    d. 新鲜度：收据记录过的非报告类文件摘要与当前字节一致（无记录的收据不予判断）。

    Pure query: read-only, no ledger creation, no event, no state.
    """
    path = _completion_record_path(str(ref), run_dir)
    if path is None:
        return CompletionRecordVerdict(False, (REASON_RECORD_UNREADABLE,))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return CompletionRecordVerdict(
            False, (f"{REASON_RECORD_UNREADABLE}:{type(exc).__name__}",)
        )
    if not any(marker in text for marker in LOCAL_COMPLETION_RECORD_MARKERS):
        return CompletionRecordVerdict(False, (REASON_NOT_A_RECORD,))
    fields = _completion_record_fields(text)
    if fields is None:
        return CompletionRecordVerdict(
            False, (REASON_RECORD_UNPARSEABLE,), is_framework_record=True
        )
    digest_ok = _completion_record_digest(fields, text)
    reasons: list[str] = []
    if not str(fields.get("report_sha256") or "").strip():
        reasons.append(REASON_DIGEST_MISSING)
    elif not digest_ok:
        reasons.append(REASON_DIGEST_MISMATCH)
    execution_id = str(
        fields.get("source_execution_id") or fields.get("reuse_execution_id") or ""
    ).strip()
    cited = _cited_receipt_rows(fields)
    cited_ids = tuple(str(item.get("receipt_id") or "") for item in cited)
    if not execution_id:
        reasons.append(REASON_NO_EXECUTION_ID)
    else:
        ledger, ledger_error = _ledger_execution(run_dir, execution_id)
        if ledger is None:
            reasons.append(f"{REASON_EXECUTION_UNTRACEABLE}:{ledger_error}")
        elif ledger["status"] != "succeeded":
            reasons.append(f"{REASON_EXECUTION_NOT_SUCCEEDED}:{ledger['status']}")
        else:
            by_id = {
                str(item.get("receipt_id") or ""): item
                for item in ledger["receipts"]
            }
            for item in cited:
                receipt_id = str(item.get("receipt_id") or "")
                recorded = by_id.get(receipt_id)
                if recorded is None:
                    reasons.append(f"{REASON_RECEIPT_MISSING}:{receipt_id}")
                    continue
                if (
                    str(item.get("status") or "") != str(recorded.get("status") or "")
                    or str(item.get("returncode")) != str(recorded.get("returncode"))
                ):
                    reasons.append(f"{REASON_RECEIPT_MISMATCH}:{receipt_id}")
                    continue
                if (
                    str(recorded.get("status") or "") != "success"
                    or str(recorded.get("returncode")) != "0"
                ):
                    reasons.append(f"{REASON_RECEIPT_NOT_A_SUCCESS}:{receipt_id}")
                    continue
                stale = _cited_receipt_is_stale(run_dir, recorded)
                if stale:
                    reasons.append(f"{REASON_RECEIPT_STALE}:{stale}")
    if not cited:
        reasons.append(REASON_NO_CITED_RECEIPTS)
    return CompletionRecordVerdict(
        traceable=not reasons,
        reasons=tuple(reasons),
        cited_execution_id=execution_id,
        cited_receipt_ids=cited_ids,
        is_framework_record=True,
        digest_ok=digest_ok,
    )


def describe_completion_record(ref: str, run_dir: Any = None) -> str:
    """``ref -> reason; reason`` -- the single wording used by every gate."""
    verdict = evaluate_completion_record(ref, run_dir)
    if verdict.traceable:
        return f"{ref} -> traceable"
    return f"{ref} -> " + "；".join(verdict.reasons)
