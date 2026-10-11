"""Evidence identity — what makes two verdicts *the same evidence*.

b33 case1 fix10 实测（`REPLAY_fix10_recovery_accounting.md` §7）：

* `artifact_ids` **每次判定新铸**（27 条判定、27 个互不相同的 id 集合），
  同一节点重验同一批产物也拿到全新 id；
* `criterion_id` 是**节点作用域**（`P-102.AC01`），换节点即换身份；
* `node_io` 只记裸路径，**无哈希**；`artifact.created` 从不记实际交付物。

⇒ 于是"同一份证据经多个接管节点传递"既无法去重，也无法追溯。

本模块把三件事**分开**，不合并成一个"身份"：

* **内容哈希（sha256）** —— 版本识别：同样内容 = 同一份证据版本。
* **execution_id + receipt_ids** —— 实际执行追溯：同一内容可能来自不同执行，
  哈希不能替代执行身份（评审约束）。
* **契约逻辑 id**（`artifact:dual-run-evidence`）—— **原始验收项身份**：
  它是契约层的，跨接管稳定，不像 criterion_id 那样随节点更换。

**缺失即不可判定。** 任一要素缺失的记录标 `decidable=False`，不参与进展判定；
绝不用当前文件哈希倒推历史执行的版本（fix10 的历史身份无法补成当时事实）。

纯函数，无 IO 副作用（只读文件字节）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVIDENCE_SEAL_SCHEMA = "astralnexus.evidence_seal.v1"

#: 进度判定的不可判定原因（可审计、稳定词表）
UNDECIDABLE_NO_ARTIFACT = "no_accepted_artifact"
UNDECIDABLE_NO_HASH = "artifact_content_unreadable"
UNDECIDABLE_NO_REQUIREMENT = "no_contract_logical_id"
UNDECIDABLE_NO_EXECUTION = "no_source_execution"
#: 没有**稳定的验收项身份**（只有节点作用域的 criterion_id）——接管节点不得据此
#: 制造进展；这是"新恢复节点不能自己增加验收项来制造进展"的落点。
UNDECIDABLE_NODE_SCOPED_ITEM = "only_node_scoped_criterion_ids"
#: 没有验收项从"未成立"变为"成立"——纯转手/重复验证落在这里。
NO_TRANSITION_UNSATISFIED = "no_acceptance_item_satisfied"

#: 节点作用域的 criterion id 形状（``P-102.AC01`` / ``N-401.EO02`` / ``R-901.WR02``）。
_NODE_SCOPED_ID = re.compile(r"^[A-Za-z]+-?\w*[-\d]+\.[A-Z]{2}\d+$")

#: 承载验收项文本的字段（跨接管稳定）；``criterion_id`` 只是节点作用域的序号。
_ITEM_TEXT_KEYS = ("criterion", "text", "requirement", "criterion_text", "detail")


def is_node_scoped_criterion_id(item: str) -> bool:
    """True when ``item`` is a node-scoped criterion id, not a stable requirement."""
    return bool(_NODE_SCOPED_ID.match(str(item or "").strip()))


def _item_identity(observation: Mapping[str, Any]) -> str:
    if observation.get("requirement_binding") == "unbound":
        return ""
    original = str(observation.get("original_requirement_id") or "").strip()
    if original and not is_node_scoped_criterion_id(original):
        return original
    for key in _ITEM_TEXT_KEYS:
        value = str(observation.get(key) or "").strip()
        if value and not is_node_scoped_criterion_id(value):
            return value
    return ""


def satisfied_acceptance_items(
    observations: Iterable[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """Split the *satisfied* acceptance items into stable vs node-scoped.

    只认 ``checked is True and holds is True`` 的项——即代码已判定成立、有执行证据
    支撑的那些。返回值 ``(stable, node_scoped)``：

    * ``stable`` —— 有跨接管稳定的验收项身份（criterion 文本），可作为进展身份；
    * ``node_scoped`` —— 只有节点作用域的 ``criterion_id``，**不能**用来制造进展
      （接管节点换一套 id 就能"造"出新进展，正是要堵的那条）。
    """
    stable: set[str] = set()
    node_scoped: set[str] = set()
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        if not observation.get("checked"):
            continue
        if observation.get("holds") is not True:
            continue
        item = _item_identity(observation)
        if item:
            stable.add(item)
            continue
        legacy = str(observation.get("criterion_id") or "").strip()
        if legacy:
            node_scoped.add(legacy)
    return sorted(stable), sorted(node_scoped)


def sha256_file(path: str | Path) -> str:
    """Content hash of one artifact; ``""`` when it cannot be read.

    An unreadable or absent path is **not** an empty evidence version — it is the
    absence of one, and callers must treat it as undecidable.
    """
    try:
        target = Path(path)
        if not target.is_file():
            return ""
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def resolve_run_path(path: str | Path, run_dir: str | Path | None) -> Path:
    """Resolve an artifact path that may be absolute, run-relative or bare."""
    target = Path(str(path))
    if target.is_absolute():
        return target
    if run_dir:
        candidate = Path(str(run_dir)) / target
        if candidate.exists():
            return candidate
    return target


def lineage_root(
    node_id: str, nodes: Mapping[str, Any], *, limit: int = 64
) -> str:
    """Follow ``recovery_of`` back to the ORIGINAL task node.

    接管节点（P-111 ``recovery_of=P-105``）映射回它接管的原始任务；
    这样就有一个跨节点换代稳定的锚。缺失/成环时停在可走到的最后一个节点。
    """
    seen: set[str] = set()
    current = str(node_id or "")
    for _ in range(max(1, limit)):
        if not current or current in seen:
            break
        seen.add(current)
        node = nodes.get(current)
        if node is None:
            break
        metadata = getattr(node, "metadata", None) or {}
        parent = str(metadata.get("recovery_of") or "").strip()
        if not parent or parent not in nodes:
            break
        current = parent
    return current


def _content_versions(
    artifacts: Iterable[Any], run_dir: str | Path | None
) -> list[dict[str, str]]:
    versions: list[dict[str, str]] = []
    for artifact in artifacts:
        raw_path = str(getattr(artifact, "path", "") or "")
        if not raw_path:
            continue
        resolved = resolve_run_path(raw_path, run_dir)
        versions.append({
            "path": raw_path,
            "content_sha256": sha256_file(resolved),
        })
    return sorted(versions, key=lambda item: item["path"])


def build_evidence_seal(
    *,
    node_id: str,
    nodes: Mapping[str, Any],
    accepted_artifacts: Sequence[Any],
    contract_ids: Sequence[str],
    execution_id: str = "",
    receipt_ids: Sequence[str] = (),
    run_dir: str | Path | None = None,
    accepted: bool = True,
    acceptance_observations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Bind one verdict to (验收项状态变化, 证据版本, 来源执行).

    ``decidable`` 表示**三种身份都齐**（版本 / 执行 / 验收项 + 契约逻辑 id）；
    ``credit_eligible`` 另外要求**真的有验收项从"未成立"变为"成立"**——
    纯转手或重复验证拿不到进展（业主反例：文件字节不变，但第二次才真正执行两遍
    测试、把原缺口关掉；所以进展绑的是**状态变化**，不是内容变化）。
    """
    versions = _content_versions(accepted_artifacts, run_dir)
    hashed = [item for item in versions if item["content_sha256"]]
    requirements = [str(item).strip() for item in contract_ids if str(item).strip()]
    root = lineage_root(node_id, nodes)
    receipts = [str(item) for item in receipt_ids if str(item)]
    satisfied, node_scoped = satisfied_acceptance_items(acceptance_observations)
    # 执行追溯：显式收据 **加上** 支撑成立项的观测自带收据——后者才是"这个验收项
    # 由哪次执行的哪些收据撑起来"的直接证据。
    for observation in acceptance_observations:
        if not isinstance(observation, Mapping):
            continue
        if not (observation.get("checked") and observation.get("holds") is True):
            continue
        for item in observation.get("receipt_ids") or []:
            if str(item) and str(item) not in receipts:
                receipts.append(str(item))

    undecidable: list[str] = []
    if not accepted:
        undecidable.append(UNDECIDABLE_NO_ARTIFACT)
    elif not versions:
        undecidable.append(UNDECIDABLE_NO_ARTIFACT)
    elif not hashed:
        undecidable.append(UNDECIDABLE_NO_HASH)
    if not requirements:
        undecidable.append(UNDECIDABLE_NO_REQUIREMENT)
    if not str(execution_id or "").strip():
        undecidable.append(UNDECIDABLE_NO_EXECUTION)
    if not satisfied and node_scoped:
        undecidable.append(UNDECIDABLE_NODE_SCOPED_ITEM)

    seal: dict[str, Any] = {
        "schema": EVIDENCE_SEAL_SCHEMA,
        "node_id": str(node_id),
        "lineage_root": root,
        "requirements": requirements,
        "evidence_versions": versions,
        # 执行追溯与版本识别分开：同一内容可能来自不同执行。
        "source_execution": {
            "execution_id": str(execution_id or ""),
            "receipt_ids": receipts,
        },
        # 验收项状态：只有这里为真才有资格计功（判据不是内容哈希）。
        "satisfied_acceptance_items": satisfied,
        "node_scoped_criterion_ids": node_scoped,
        "decidable": not undecidable,
        "undecidable_reasons": undecidable,
    }
    seal["credit_eligible"] = bool(seal["decidable"] and satisfied)
    seal["credit_key"] = credit_key(seal)
    return seal


def credit_key(seal: Mapping[str, Any]) -> str | None:
    """进展身份 = **已获得证据支撑的验收项集合**。

    **判据是状态变化，不是内容变化**（业主反例）：

    * 文件字节不变、第一次没真正复验（项未成立）、第二次真跑两遍把缺口关掉
      ⇒ 第一次 ``credit_eligible=False``（无成立项），第二次才有 key ⇒ **计一次**；
    * 同一份证据经多个接管节点转手、项早已成立 ⇒ key 相同 ⇒ **只计一次**；
    * 内容变了但验收项没变化 ⇒ **key 不变、不计功**（内容变化本身不自动计功）。

    谱系根与内容指纹都**不进 key**：前者是审计锚，后者是证据版本（留作追溯）；
    跨接管稳定的身份只有**验收项文本**。不可判定或没有成立项时返回 None。
    """
    if not seal.get("credit_eligible"):
        return None
    items = sorted(str(item) for item in seal.get("satisfied_acceptance_items") or [])
    if not items:
        return None
    # **内容指纹不进 key**：验收项一旦被证据支撑并计过功，改写同样的证据不会
    # 再计一次；"内容变化本身不能自动计功"（业主约束）。证据版本留在
    # evidence_versions 里供审计与追溯，不参与身份。
    payload = "|".join(items)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def credit_decision(
    seal: Mapping[str, Any], already_credited: Iterable[str]
) -> dict[str, Any]:
    """Decide whether this verdict earns progress, and say why.

    Returns a dict carrying the decision plus the binding the review asked for
    (原始验收项 / 缺口来源 / 证据版本 / 来源执行), so the row is auditable
    without re-reading the graph.
    """
    key = credit_key(seal)
    known = {str(item) for item in already_credited}
    binding = {
        "credit_key": key or "",
        "requirements": list(seal.get("requirements") or []),
        "satisfied_acceptance_items": list(seal.get("satisfied_acceptance_items") or []),
        "node_scoped_criterion_ids": list(seal.get("node_scoped_criterion_ids") or []),
        "evidence_versions": list(seal.get("evidence_versions") or []),
        "source_execution": dict(seal.get("source_execution") or {}),
        "lineage_root": seal.get("lineage_root") or "",
    }
    if not seal.get("credit_eligible"):
        if (seal.get("node_scoped_criterion_ids")
                and not seal.get("satisfied_acceptance_items")):
            # 更具体的原因优先：接管节点只有节点作用域 id，不得据此制造进展。
            reason = "node_scoped_item_is_not_a_requirement"
        elif not seal.get("decidable"):
            reason = "undecidable"
        else:
            reason = NO_TRANSITION_UNSATISFIED
        return {
            "credited": False,
            "reason": reason,
            "undecidable_reasons": list(seal.get("undecidable_reasons") or []),
            **binding,
        }
    if key in known:
        return {
            "credited": False,
            "reason": "same_acceptance_item_already_credited",
            "undecidable_reasons": [],
            **binding,
        }
    return {
        "credited": True,
        "reason": "acceptance_item_now_evidence_backed",
        "undecidable_reasons": [],
        **binding,
    }


__all__ = [
    "EVIDENCE_SEAL_SCHEMA",
    "NO_TRANSITION_UNSATISFIED",
    "UNDECIDABLE_NODE_SCOPED_ITEM",
    "UNDECIDABLE_NO_ARTIFACT",
    "UNDECIDABLE_NO_EXECUTION",
    "UNDECIDABLE_NO_HASH",
    "UNDECIDABLE_NO_REQUIREMENT",
    "build_evidence_seal",
    "credit_decision",
    "credit_key",
    "is_node_scoped_criterion_id",
    "lineage_root",
    "resolve_run_path",
    "satisfied_acceptance_items",
    "sha256_file",
]
