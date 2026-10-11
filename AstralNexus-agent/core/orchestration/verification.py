"""Verification responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING, Mapping
from .models import WaveResult
from .support import (
    Any,
    hashlib,
    Artifact,
    ArtifactAcceptanceStatus,
    Dict,
    ExecutionRunResult,
    Fact,
    List,
    Optional,
    Path,
    SimpleNamespace,
    TaskStatus,
    Tuple,
    VerificationDimension,
    VerificationResult,
    VerificationVerdict,
    datetime,
    identity_key,
    json,
    logger,
    os,
    recovery_operation,
    resolve_declared_output,
    tempfile,
    timezone,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def _record_verification_basis(self: Orchestrator, node) -> None:
    """Snapshot the owned tree the Verifier is about to read (Fix AH).

    Stored on the node rather than passed through ``verify_node`` so the
    independent Verifier's signature stays untouched.  A failure here must
    never block verification: without a basis ``accept()`` falls back to its
    original finish-time comparison, which is strictly stricter.
    """
    execution_id = node.metadata.get("runtime_execution_id") if node else None
    if not execution_id:
        return
    try:
        from AstralNexus.runtime.execution_store import ExecutionStore, snapshot_files

        store = ExecutionStore(self.run_dir)
        owned_paths = list(store.get(execution_id)["contract"].get("owned_paths", []))
        node.metadata["_verification_files_basis"] = snapshot_files(store.root, owned_paths)
    except Exception as exc:  # pragma: no cover - diagnostic only
        node.metadata.pop("_verification_files_basis", None)
        self.trace_recorder.record("verifier.basis_unavailable", {
            "node_id": getattr(node, "node_id", ""),
            "error": str(exc)[:300],
            "note": "回退到 finish 时刻比对（更严格）",
        })


def _record_worker_verdict(self: Orchestrator, node, verdict: VerificationVerdict) -> None:
    """Project the authoritative Verifier verdict onto worker lifecycles."""
    approved = verdict == VerificationVerdict.APPROVED
    repairable = verdict in {
        VerificationVerdict.REPAIR_REQUIRED,
        VerificationVerdict.REPLAN_REQUIRED,
        VerificationVerdict.INCONCLUSIVE,
    }
    worker_agent_id = node.metadata.get("worker_agent_id") if node else None
    if worker_agent_id:
        self.lifecycle_manager.record_verification_verdict(
            worker_agent_id,
            approved=approved,
            repairable=repairable,
            reason=f"Verifier verdict: {verdict.value}",
        )


def _declared_contract_paths(node) -> List[str]:
    """Declared output paths of one node, in declaration order.

    ``output_artifacts`` falls back to ``expected_outputs`` when the
    Planner omits it (``core/governor.py`` admission), so the two lists are
    the same declaration set in two spellings and stay positionally
    aligned.  Structured entries carry their path under ``path``.
    """
    declared: List[str] = []
    for entry in (node.metadata.get("expected_outputs") or []):
        if isinstance(entry, dict):
            entry = str(entry.get("path") or "")
        else:
            entry = str(entry or "")
        entry = entry.strip()
        if entry:
            declared.append(entry)
    return declared


def _contract_physical_path(
    self: Orchestrator,
    *,
    index: int,
    declared_paths: List[str],
    physical_paths: List[str],
) -> tuple[str, str]:
    """Bind one logical output contract to the one file it actually owns.

    Returns ``(path, basis)``; ``path`` is empty when no honest binding
    exists.  A contract bundle must never borrow an arbitrary sibling's
    path: stamping ``physical_paths[0]`` on every logical output made
    ``artifact:deliverables_verify_case_py`` and
    ``artifact:runs_verify_logs_run2`` both claim ``run1.log`` in the b33
    fixed case, which then collided under the basename alias key in
    ``Governor.apply_task_graph_patch`` and rejected a legal graph patch.
    """
    # ``run_dir`` is absent on the ``Orchestrator.__new__`` doubles some
    # callers build; without a root there is nothing to resolve against, so
    # fall through to the single-artifact rule instead of raising.
    run_dir = getattr(self, "run_dir", None)
    if run_dir is not None and index < len(declared_paths):
        resolved = resolve_declared_output(run_dir, declared_paths[index])
        if resolved is not None:
            return str(resolved.resolve()), "declared_output_index"
    if len(physical_paths) == 1:
        return physical_paths[0], "single_physical_artifact"
    return "", "unbound"


def _finalize_node_evidence(
    self: Orchestrator,
    node,
    *,
    approved: bool,
    verification_score: float,
    verdict: VerificationVerdict,
) -> None:
    """Commit or reject worker evidence at the unique Verifier boundary."""
    if node.metadata.get("runtime_execution_id"):
        from AstralNexus.runtime.execution_store import ExecutionStore
        ExecutionStore(self.run_dir, fence_token_provider=self.fence_token_provider).accept(
            node.metadata["runtime_execution_id"],
            {"approved": approved, "score": verification_score, "verdict": verdict.value,
             "acceptance_scope": "business" if getattr(getattr(self, "verifier", None), "business_acceptance_checks", True) else "execution"},
            verified_files=node.metadata.pop("_verification_files_basis", None),
        )
    task_version = self._node_task_version(node)
    artifact_ids = list(node.metadata.pop("_pending_artifact_ids", []))
    fact_records = list(node.metadata.pop("_pending_fact_records", []))
    decision = ArtifactAcceptanceStatus.ACCEPTED if approved else ArtifactAcceptanceStatus.REJECTED

    # One verification event stamps one time.  This used to be read from the
    # clock inside the loop below, so two rows written by the same handoff
    # could differ by microseconds (live r30 N-104-readme: README.md landed
    # as A-...-003/004 one millisecond apart) -- a value that then described
    # row-write order rather than the event it is named after.  No consumer
    # is keyed on it today; it is recorded so that it stays true.
    verified_at = datetime.now(timezone.utc).isoformat()
    for artifact_id in artifact_ids:
        artifact = self.task_graph.artifacts.get(artifact_id)
        if artifact is None:
            continue
        artifact.acceptance_status = decision
        artifact.metadata.update(
            {
                "verification_verdict": verdict.value,
                "verification_score": float(verification_score),
                "verified_at": verified_at,
            }
        )

    bound_output_artifact_ids: list[str] = []
    accepted_artifacts: list[Any] = []
    if approved and artifact_ids:
        accepted_artifacts = [
            self.task_graph.artifacts[artifact_id]
            for artifact_id in artifact_ids
            if artifact_id in self.task_graph.artifacts
            and getattr(
                self.task_graph.artifacts[artifact_id].acceptance_status,
                "value",
                str(self.task_graph.artifacts[artifact_id].acceptance_status),
            ) == ArtifactAcceptanceStatus.ACCEPTED.value
        ]
        physical_paths = [artifact.path for artifact in accepted_artifacts]
        declared_contract_paths = self._declared_contract_paths(node)
        unbound_contract_ids: list[str] = []
        for index, logical_id in enumerate(node.output_artifacts):
            logical_id = str(logical_id).strip()
            if not logical_id:
                continue
            contract_path, contract_path_basis = self._contract_physical_path(
                index=index,
                declared_paths=declared_contract_paths,
                physical_paths=physical_paths,
            )
            if contract_path_basis == "unbound":
                unbound_contract_ids.append(logical_id)
            existing = self.task_graph.artifacts.get(logical_id)
            if existing is not None and existing.node_id != node.node_id:
                owner = (
                    self.task_graph.get_node(str(existing.node_id))
                    if existing.node_id
                    else None
                )
                owner_status = str(getattr(owner, "status", "") or "")
                if owner is not None and owner_status in {
                    "completed", "superseded", "failed", "cancelled",
                }:
                    # Governed-successor rebind: the registered owner is
                    # inactive and can never write the artifact again,
                    # while this node just passed verification for a
                    # governed patch that legally owns the underlying
                    # files.  Live r11 (resume #7): N-002-R6 repaired
                    # init_db.py, verifier approved 1.0/1.0, then the
                    # exclusivity check hard-failed it because completed
                    # N-002-R5 still held 'artifact:backend_component' —
                    # sending the lineage into an unwinnable recovery
                    # loop (each successor hits the same wall).  Rebind
                    # with an audit trail instead; active owners keep the
                    # hard failure (true concurrent-writer conflict).
                    self.trace_recorder.record(
                        "artifacts.logical_ownership_rebound",
                        {
                            "artifact_id": logical_id,
                            "previous_owner": existing.node_id,
                            "previous_owner_status": owner_status,
                            "new_owner": node.node_id,
                            "verification_score": float(verification_score),
                        },
                    )
                else:
                    raise RuntimeError(
                        f"logical artifact {logical_id!r} is already owned by "
                        f"{existing.node_id!r}"
                    )
            self.task_graph.add_artifact(
                Artifact(
                    artifact_id=logical_id,
                    node_id=node.node_id,
                    artifact_type="contract_bundle",
                    path=contract_path,
                    description=(
                        f"Verified output contract {logical_id} produced by "
                        f"{node.node_id}"
                    ),
                    acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
                    metadata={
                        "logical_contract": True,
                        "physical_artifact_ids": list(artifact_ids),
                        "physical_paths": physical_paths,
                        "verification_verdict": verdict.value,
                    "verification_scope": "business" if getattr(getattr(self, "verifier", None), "business_acceptance_checks", True) else "execution",
                        "verification_score": float(verification_score),
                        "task_version": task_version,
                        # Same verification event as the physical artifacts
                        # above, so the bundle's provenance is complete
                        # rather than half-stated.  This is a record, not a
                        # merge input: the alias merge in
                        # ``Governor.apply_task_graph_patch`` keys on
                        # node_id plus a task_version/retry_count conflict
                        # test, and reads neither of these.
                        "retry_count": node.retry_count,
                        "verified_at": verified_at,
                        # How ``path`` was decided, for audit.  ``unbound``
                        # means the bundle names no single file and the
                        # authoritative set stays ``physical_paths``.
                        "path_binding": contract_path_basis,
                    },
                )
            )
            bound_output_artifact_ids.append(logical_id)
        if unbound_contract_ids:
            # A bundle that names no single file is not a failure -- the
            # authoritative set stays ``metadata["physical_paths"]`` -- but
            # it must be visible rather than silently empty.
            self.trace_recorder.record("artifact.contract_path.unbound", {
                "node_id": node.node_id,
                "logical_ids": unbound_contract_ids,
                "declared_outputs": declared_contract_paths,
                "physical_paths": physical_paths,
            })

    committed_fact_ids: list[str] = []
    if approved:
        for index, record in enumerate(fact_records, start=1):
            fact_id = (
                f"F-{node.node_id}-v{task_version}"
                f"-r{node.retry_count}-{index:03d}"
            )
            fact = Fact(
                fact_id=fact_id,
                text=str(record["text"]),
                source=node.node_id,
                confidence=max(
                    0.0, min(1.0, float(record.get("confidence", 0.0)))
                ),
                recorded_at=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "evidence_refs": list(record.get("evidence") or []),
                    "verification_score": float(verification_score),
                    "verification_verdict": verdict.value,
                    "verification_scope": "business" if getattr(getattr(self, "verifier", None), "business_acceptance_checks", True) else "execution",
                    "task_version": task_version,
                },
            )
            self.task_graph.add_fact(fact)
            self.memory_federation.add_fact(fact)
            committed_fact_ids.append(fact_id)

        downstream_ids = self._find_downstream_nodes(node.node_id)
        if downstream_ids:
            from AstralNexus.core.governor import TaskGraphPatch

            self.governor.apply_task_graph_patch(
                TaskGraphPatch(
                    add_edges=[
                        {
                            "source": node.node_id,
                            "target": downstream_id,
                            "edge_type": "evidence_for",
                            "metadata": {
                                "fact_ids": committed_fact_ids,
                "artifact_ids": artifact_ids,
                "bound_output_artifact_ids": bound_output_artifact_ids,
                                "verification_score": float(verification_score),
                            },
                        }
                        for downstream_id in downstream_ids
                    ],
                    reason_codes=["VERIFIED_EVIDENCE_LINK"],
                    reasoning="Bind approved evidence to dependent nodes.",
                )
            )

    # b33 case1 fix10 §7 第 1 步：把判定绑到证据身份上——证据版本（内容哈希）、
    # 来源执行（execution_id + receipt_ids）、原始验收项（契约逻辑 id，跨接管
    # 稳定）。进展信用按这个身份发放，于是"同一份证据经多个接管节点传递"只记
    # 一次，"换节点"也不清零已成立的进展。缺任一要素 → 不可判定，不发放信用。
    from AstralNexus.runtime.evidence_identity import (
        build_evidence_seal, credit_decision,
    )

    receipt_ids: list[str] = []
    receipt = node.metadata.get("_verification_receipt")
    if isinstance(receipt, dict):
        for observation in receipt.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            receipt_ids.extend(
                str(item) for item in (observation.get("receipt_ids") or [])
            )
    # 验收项状态：只有"从未成立变为成立"的项才有资格计功（判据不是内容哈希）。
    # 观测来源是本节点的确定性验收检查，与 verifier.deterministic_acceptance
    # 事件同源。
    acceptance_observations: list[Any] = []
    verifier = getattr(self, "verifier", None)
    if verifier is not None and hasattr(verifier, "deterministic_observations"):
        acceptance_observations = list(
            verifier.deterministic_observations(node) or []
        )
    evidence_seal = build_evidence_seal(
        node_id=node.node_id,
        nodes=self.task_graph.nodes,
        accepted_artifacts=accepted_artifacts,
        contract_ids=list(node.output_artifacts or []),
        execution_id=str(node.metadata.get("runtime_execution_id") or ""),
        receipt_ids=receipt_ids,
        run_dir=self.run_dir,
        accepted=approved,
        acceptance_observations=self._bind_requirement_observations(acceptance_observations),
    )
    if approved:
        versions = {item["path"]: item["content_sha256"] for item in evidence_seal.get("evidence_versions", [])}
        for sealed_artifact in accepted_artifacts:
            digest = versions.get(str(sealed_artifact.path))
            if digest:
                sealed_artifact.metadata["content_sha256"] = digest
                sealed_artifact.metadata["task_version"] = task_version
        for logical_id in bound_output_artifact_ids:
            logical_artifact = self.task_graph.artifacts.get(logical_id)
            if logical_artifact is not None and str(logical_artifact.path) in versions:
                logical_artifact.metadata["content_sha256"] = versions[str(logical_artifact.path)]
    if approved and node.metadata.get("_runtime_output_ref"):
        output_ref = str(node.metadata["_runtime_output_ref"])
        output_path = (self.run_dir / output_ref).resolve()
        if not output_path.is_relative_to(self.run_dir.resolve()) or not output_path.is_file():
            raise RuntimeError("Verified node output journal is missing or outside the run")
        raw_output = output_path.read_bytes()
        if hashlib.sha256(raw_output).hexdigest() != node.metadata.get("_runtime_output_sha256"):
            raise RuntimeError("Worker output journal changed before acceptance")
        payload = json.loads(raw_output)
        if (payload.get("node_id") != node.node_id
                or payload.get("execution_outcome") != "succeeded"
                or str(payload.get("task_version")) != str(task_version)):
            raise RuntimeError("Verified node output journal does not match the accepted attempt")
        self.task_graph.add_artifact(Artifact(
            artifact_id="execution-record:" + str(node.metadata.get("runtime_execution_id") or payload["execution_id"]),
            node_id=node.node_id, artifact_type="execution_record", path=output_ref,
            description="Verifier-accepted worker output journal",
            acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
            metadata={"task_version": task_version, "content_sha256": hashlib.sha256(raw_output).hexdigest(),
                      "execution_id": node.metadata.get("runtime_execution_id"),
                      "trace_execution_id": payload.get("execution_id"), "verification_verdict": verdict.value,
                      "physical_subjects": {str(item.path): str(item.metadata.get("content_sha256")) for item in accepted_artifacts if item.metadata.get("content_sha256")}},
        ))
    progress_credit = credit_decision(
        evidence_seal,
        self.runtime_state_manager.state.credited_evidence_keys or [],
    )
    if progress_credit["credited"]:
        self.runtime_state_manager.credit_evidence_key(
            progress_credit["credit_key"]
        )
    # b33 case1 fix10 第 4 步：把这次判定记到**谱系根**上——换节点不清零；
    # 没有新成立项时该缺口的"未改善计数"+1（无有效进展判据的数据源）。
    try:
        lineage_root_id = str(
            evidence_seal.get("lineage_root") or node.node_id
        )
        satisfied_items = list(
            evidence_seal.get("satisfied_acceptance_items") or []
        )
        gap_items = [
            str(item) for item in (evidence_seal.get("requirements") or [])
            if str(item) not in satisfied_items
        ]
        self.runtime_state_manager.record_lineage_progress(
            lineage_root_id,
            satisfied_items,
            gap_fingerprint=(
                "gap:%s|%s" % (lineage_root_id, ",".join(sorted(gap_items)))
                if gap_items else ""
            ),
        )
    except Exception:
        # 记账失败不得改变运行行为。
        pass

    self.trace_recorder.record(
        "task.evidence.finalized",
        {
            "node_id": node.node_id,
            "verdict": verdict.value,
            "approved": approved,
            "verification_score": float(verification_score),
            "artifact_ids": artifact_ids,
            "fact_ids": committed_fact_ids,
            "rejected_claim_count": 0 if approved else len(fact_records),
            "evidence_seal": evidence_seal,
            "progress_credit": progress_credit,
        },
    )


def _node_task_version(node: Any) -> int:
    """Read the canonical task version from node metadata.

    TaskNode deliberately owns graph structure and lifecycle only. The
    mutable task version belongs to the Governor/runtime contract and is
    therefore persisted in metadata.
    """
    try:
        return max(1, int((getattr(node, "metadata", {}) or {}).get("task_version", 1)))
    except (TypeError, ValueError):
        return 1


def _verification_self_lock_codes(self: Orchestrator, patch) -> List[str]:
    """Reason codes by which the proposal admits its own premise is frozen."""
    return [
        str(code)
        for code in (getattr(patch, "reason_codes", []) or [])
        if any(token in str(code) for token in self._SELF_LOCK_REASON_TOKENS)
    ]


def _verification_claim_sets(self: Orchestrator, exclude_node_ids: set | None = None) -> tuple:
    """图上**已经**被声明的交付物路径与证据产物。

    DeerFlow 2.0 对齐：验收是就地的判定（L2 的 checked/holds），不可判定一律
    UNVERIFIED，**绝不为"再验一次"扩建工作图**。所以要判断一份验证恢复补丁
    是否真的推进了工作，必须看它是否声明了图上**还没有的**交付物/证据——
    而不是看它有没有新增节点。
    """
    existing_paths: set = set()
    existing_artifacts: set = set()
    # F2: 归属要按**补丁生效后仍存活的图**判定——被本补丁取代/改写的节点
    # 交出的归属不算"图上已有"，否则接管型修复节点永远被判成无新证据。
    skip = set(exclude_node_ids or ())
    # The graph's identity is its nodes DICT KEY: reading it here (instead of
    # `existing.node_id`) removes the hidden "a node object must carry its own
    # id" dependency, which a fake graph of SimpleNamespace nodes exposed.
    for node_id, existing in self.task_graph.nodes.items():
        if node_id in skip:
            continue
        # b33 S2 (D4): 归属按 S1 的唯一身份平面取键。此前这里是内联的
        # ``str(item).replace("\\","/").strip("/")``——与 identity_key 口径不同
        # 的第五份归一。判定语义不变：仍然只看集合差。
        for item in getattr(existing, "owned_paths", []) or []:
            normalized = identity_key(item)
            if normalized:
                existing_paths.add(normalized)
        for item in getattr(existing, "output_artifacts", []) or []:
            normalized = str(item).strip()
            if normalized:
                existing_artifacts.add(normalized)
    return existing_paths, existing_artifacts


def _added_nodes_claim_new_evidence(
    self: Orchestrator, added_nodes: List[Any], exclude_node_ids: set | None = None
) -> bool:
    """补丁新增的节点是否声明了图上**还没有的**交付物路径或证据产物。

    True 才算"有新工作/新证据"；False 意味着这只是**重跑一个已不可判定的
    前提**（自锁），即使它新增了节点也不构成进展。
    """
    if not added_nodes:
        return False
    existing_paths, existing_artifacts = self._verification_claim_sets(exclude_node_ids)
    for raw_node in added_nodes:
        if not isinstance(raw_node, dict):
            continue
        metadata = raw_node.get("metadata") or {}
        claimed_paths = {
            identity_key(item)
            for item in (
                raw_node.get("owned_paths") or metadata.get("owned_paths") or []
            )
            if str(item).strip()
        }
        claimed_paths |= {
            identity_key(item)
            for item in (
                raw_node.get("expected_outputs")
                or metadata.get("expected_outputs")
                or []
            )
            if str(item).strip()
            and not str(item).strip().startswith("artifact:")
        }
        claimed_artifacts = {
            str(item).strip()
            for item in (
                raw_node.get("output_artifacts")
                or metadata.get("output_artifacts")
                or []
            )
            if str(item).strip()
        }
        if (claimed_paths - existing_paths) or (
            claimed_artifacts - existing_artifacts
        ):
            return True
    return False


def _patch_superseded_owners(
    self: Orchestrator, node, added_nodes: List[Any], updated_nodes: List[Any]
) -> set:
    """本补丁接管/取代/重开的所有者 id 集合（F2 归属判定用）。

    断言按**补丁生效后仍存活的图**算：被接管方交出的归属不算"图上已有"，
    否则完成态重开转修复节点、跨代接管、去冲突改写都会被误判成"无新证据"，
    而它们恰恰是替代"重跑同一前提"的合法形状。
    """
    owners = {str(node.node_id)}
    transfer_keys = (
        "recovery_of",
        "_repair_target",
        "repairs_completed_node",
        "supersedes",
        "superseded_by",
        "ownership_transfer_to",
    )
    for item in list(added_nodes) + list(updated_nodes):
        if not isinstance(item, dict):
            continue
        target = str(item.get("node_id") or item.get("task_id") or "")
        if target:
            owners.add(target)
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        for key in transfer_keys:
            value = item.get(key, metadata.get(key))
            if isinstance(value, str) and value.strip():
                owners.add(value.strip())
            elif isinstance(value, (list, tuple, set)):
                owners.update(
                    str(entry).strip() for entry in value if str(entry).strip()
                )
    return owners


def _verification_premise_baseline(self: Orchestrator, node) -> Tuple[List[str], Dict[str, str]]:
    """The premise file set / versions recorded on ``node``'s last receipt.

    Returns ``(declared_paths, {path: sha256})``.  The premise vocabulary is
    the verifier's own (``declared_premise_paths`` / ``premise_versions``),
    so guard and verifier compare the same file set.
    """
    from AstralNexus.runtime.verification_identity import declared_premise_paths

    receipt = node.metadata.get("_verification_receipt") or {}
    declared: List[str] = []
    baseline: Dict[str, str] = {}
    for observation in receipt.get("observations") or []:
        if not isinstance(observation, dict):
            continue
        for path in observation.get("declared_premises") or []:
            text = str(path).strip()
            if text and text not in declared:
                declared.append(text)
        versions = observation.get("premise_versions") or {}
        if isinstance(versions, dict):
            for path, digest in versions.items():
                text = str(path).strip()
                digest = str(digest).strip()
                if text and digest and text not in baseline:
                    baseline[text] = digest
    if not declared:
        declared = declared_premise_paths(
            acceptance_criteria=list(getattr(node, "acceptance_criteria", []) or []),
            upstream_artifacts=list(getattr(node, "input_artifacts", []) or []),
            owned_paths=list(getattr(node, "owned_paths", []) or []),
            root=self.run_dir,
        )
    return declared, baseline


def _verification_premise_changed(
    self: Orchestrator,
    node,
    *,
    baseline: Optional[Tuple[List[str], Dict[str, str]]] = None,
) -> bool:
    """True when the files the last verdict rested on have moved since.

    F2 (live b31, 2026-10-05): the verification-recovery channel applied the
    same no-op proposal twice before rejecting it (N-102 exec-000006 ran
    three times on one executor dispatch, R-601 twice) — every re-run cost
    ~6 minutes and produced no product increment.  A re-dispatch is only
    legitimate when the premise the verdict was decided on actually moved;
    otherwise the proposal must declare deliverable/evidence paths the graph
    does not own yet (that check lives in the caller) or escalate.

    ``baseline`` is the *previous* verdict's premise snapshot.  The caller
    MUST pass the snapshot captured before this attempt persisted its own
    receipt (``_apply_governed_verification_recovery`` does so): comparing
    the receipt against itself can never show movement.  A node whose
    receipt carries no premise snapshot can never *prove* movement and is
    therefore not re-admitted on the strength of a re-run alone.
    """
    from AstralNexus.runtime.verification_identity import (
        changed_premises,
        premise_versions,
    )

    if baseline is None:
        declared, premise_snapshot = self._verification_premise_baseline(node)
        baseline_dict: Dict[str, str] = dict(premise_snapshot)
    else:
        declared = list(baseline[0])
        baseline_dict = dict(baseline[1])
    if not declared:
        return False
    root = getattr(self.verifier, "workspace_root", None) or self.run_dir
    current = premise_versions(root, declared)
    if not baseline_dict:
        baseline_dict = dict(current)
    return bool(changed_premises(baseline_dict, current))


def _executable_ready_frontier(self: Orchestrator) -> List[str]:
    """Node ids the scheduler can still put to work, without side effects.

    ``Router.select_ready_nodes`` records wave metrics for the parallelism
    evidence, so calling it from a bookkeeping path would pollute that
    evidence; this mirror applies the same dependency/artifact gate
    read-only.

    A freshly applied structural patch lands its nodes as ``admitted``
    (default at ``core/task_graph.py:34``) and only ``promote_waiting_nodes``
    (``core/task_graph.py:551``) turns them into ``ready``.  Counting only
    ``ready`` therefore read an *empty* frontier immediately after a correct
    repair and charged that repair to the no-progress budget; the promotable
    statuses have to count as pending executable work.
    """
    return [
        node.node_id
        for node in self.task_graph.nodes.values()
        if node.status in {
            TaskStatus.ADMITTED.value,
            TaskStatus.WAITING_DEPENDENCIES.value,
            TaskStatus.READY.value,
        }
        and not any(self.task_graph.readiness_blockers(node).values())
    ]


def _complete_report_gap_locally(
    self: Orchestrator,
    node,
    verification_result,
    report_gap: Dict[str, Any],
) -> Dict[str, Any]:
    """Complete a framework-classified report-only gap inside this execution.

    doc 51 §9 边界④ / 用户 2026-10-06 第⑤条（S3 局部恢复）：缺口由框架按锚定
    receipt 判定（``runtime/report_gap.py``——预算耗尽、其他失败、陈旧证据、未检查
    断言、非报告类缺失都已 VETO 否决，故报告缺口与它们互斥）。恢复动作因此只在
    **本执行内**把报告写出来并引用原执行收据：

    * 新增节点数 = 0、新增执行数 = 0（不派发 ``RC-*`` 后继、不起新 worker）；
    * 不重跑功能测试（复用的就是那条锚定 receipt，``rerun_functional_tests`` False）；
    * 不改写失败节点的状态——报告缺失是**附加**缺口：既有失败判据（含预算耗尽
      主因）保持不动，失败节点继续阻断交付门，绝不被翻成 completed；
    * 无法引用账本（没有该执行记录、路径逃出工作区、非报告类路径）时**显式拒绝**
      并交回既有终止判定，不退回 LLM 即兴规划。

    取代的是过去"缺报告 ⇒ 整节点派发 RC-* 后继＋新 worker"的默认反应，而不是取消
    判定本身：判定仍由 Verifier/``report_gap.py`` 做，本方法只是它的执行端。
    """
    from AstralNexus.core.output_paths import workspace_relative
    from AstralNexus.runtime.execution_store import ExecutionStore
    from AstralNexus.runtime.report_gap import (
        GENERATOR,
        ReportGap,
        evaluate_completion_record,
        local_completion_document,
        local_completion_plan,
    )
    from AstralNexus.runtime.verification_identity import is_report_class_path

    missing_reports = [
        str(item).replace("\\", "/").strip()
        for item in (report_gap.get("missing_reports") or [])
        if str(item).strip()
    ]
    reuse_execution_id = str(
        report_gap.get("reuse_execution_id")
        or report_gap.get("execution_id")
        or ""
    ).strip()
    failure_code = str((node.metadata or {}).get("error_type") or "").strip()
    if not failure_code:
        failure_code = (
            str(report_gap.get("reason") or "REPORT_ONLY_GAP").strip()
            or "REPORT_ONLY_GAP"
        )
    root = Path(getattr(self.verifier, "workspace_root", None) or self.run_dir)

    def refuse(
        outcome: str,
        error: str,
        *,
        action: str = "terminal",
        consume_churn: bool = True,
    ) -> Dict[str, Any]:
        """A completion that writes nothing is terminal: never an LLM improvisation.

        用户第⑤条要求"拒绝重复恢复"时给出**明确阻塞原因**，因此拒绝既进既有
        事件类型（不新增事件类型）也作为 ``blocking_reason`` 直接返回给调用方。

        用户 2026-10-06 第⑥条（"不新增节点/执行 ≠ 不需要审计"）：被拒路径也是一次
        处理，同样要留下生成器版本、来源执行与处理结果。只**扩既有事件的 payload**
        （``generator``／``source_execution_id``／``outcome="refused:<slug>"``），不新增
        事件类型、不改变 ``reason_codes``／``action`` 的既有语义。``consume_churn=False``
        只服务"已经决定要写、写失败了"这一条路径：那次异常既没有产生新的恢复尝试，
        也没有产生新的进度，沿用 S3 现状（异常上抛不动 churn 记账）。
        """
        reason = str(error)[:1000]
        if consume_churn:
            self._consume_recovery_churn("report_completion_rejected")
        self.trace_recorder.record(
            "governor.report_completion.rejected",
            {
                "node_id": node.node_id,
                "missing_reports": missing_reports,
                "reuse_execution_id": reuse_execution_id,
                "source_execution_id": reuse_execution_id,
                "generator": GENERATOR,
                "outcome": f"refused:{outcome}",
                "failure_code": failure_code,
                "reason_codes": ["REPORT_COMPLETION_REJECTED"],
                "error": reason,
                "blocking_reason": reason,
                "status": node.status,
                "action": action,
            },
        )
        return {
            "accepted": False,
            "rejected": True,
            "node_id": node.node_id,
            "recovery_type": "report_completion",
            "missing_reports": missing_reports,
            "reuse_execution_id": reuse_execution_id,
            "failure_code": failure_code,
            "error": reason,
            "blocking_reason": reason,
            "adds_nodes": 0,
            "adds_executions": 0,
        }

    # A verification gap must never become an ownership grab on product code.
    for path in missing_reports:
        if not is_report_class_path(path):
            return refuse(
                "non_report_class_path",
                f"local report completion may only own report-class artifacts: {path}",
            )
    # 证据复用绑定：必须能引用原执行的账本记录，否则不补——没有可引用的收据就
    # 没有"只缺报告"这回事（§5 的五项绑定里的执行 ID 与命令/版本都在这里落地）。
    if not reuse_execution_id:
        return refuse("no_anchored_execution_id", "no anchored execution id to cite")
    try:
        anchored = ExecutionStore(root).get(reuse_execution_id)
    except (ValueError, KeyError) as exc:
        return refuse(
            "no_anchored_execution_record",
            f"no anchored execution record to cite: {exc}",
        )

    plan = local_completion_plan(
        ReportGap(
            missing_reports=tuple(missing_reports),
            execution_id=reuse_execution_id,
        ),
        node_id=node.node_id,
    )
    written: list[str] = []
    skipped_existing: list[str] = []
    pending: list[tuple[str, Path]] = []
    root_abs = Path(root)
    for path in missing_reports:
        relative = workspace_relative(root_abs, path)
        if not relative:
            return refuse(
                "path_outside_workspace",
                f"report path escapes the run workspace: {path}",
            )
        target = root_abs / relative
        if target.exists() and target.stat().st_size > 0:
            # 复核条件 4：文件存在 ≠ 完成。框架补出记录只有**自哈希自洽且引用
            # 成立**（可追溯）才算已完成；截断/损坏/哈希不匹配、或引用不再成立的
            # 记录视为未完成，落回 pending 走原子重写。非框架的非空文件维持既有
            # "跳过不覆盖"语义（P3-13 不变），0 字节仍可改写。
            verdict = evaluate_completion_record(relative, root_abs)
            if verdict.is_framework_record and not (
                verdict.digest_ok and verdict.traceable
            ):
                pending.append((relative, target))
                continue
            skipped_existing.append(relative)
            continue
        pending.append((relative, target))
    if not pending:
        # 用户 2026-10-06 第⑤条：无修改、无新事实的重复恢复必须被拒绝并给出明确
        # 阻塞原因，而不是无限重做同一动作（旧语义的"重复派发 ⇒ 拒绝"循环刹车
        # 在这里以新形状保留：报告已经落地，说明这次请求没有带来任何新事实）。
        return refuse(
            "repeated_recovery",
            "report already present; repeated recovery refused: "
            + ", ".join(skipped_existing),
        )

    def write_record_atomically(target_path: Path, document_text: str) -> None:
        """同目录临时文件 + ``os.replace``：要么完整落地，要么目标保持原样。

        P5b：原实现对目标路径直接 ``open(target, "w")``，渲染/写入中途异常时会留下
        **非空半截文件**，而上面的 ``target.exists() and st_size > 0`` 会把半截文件
        当成"报告已落地"，于是 P3-13 的"写失败可重试"在部分写情形失效（重试被
        ``repeated_recovery`` 拒绝，而报告内容其实不完整）。临时文件与目标同目录
        ⇒ ``os.replace`` 是同一文件系统内的原子替换；失败时删掉临时文件，目标保持
        原样（不存在，或仍是那个 0 字节文件——"0 字节可改写"的既有语义不变）。
        写入用内建 ``open``（而不是 ``os.fdopen``）：P3-13 的中断测试是在
        ``builtins.open``/``Path.open`` 上注入 KeyboardInterrupt 的。
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target_path.parent),
            prefix=f".{target_path.name}.",
            suffix=".tmp",
        )
        os.close(fd)
        try:
            with open(tmp_name, "w", encoding="utf-8", newline="") as handle:
                handle.write(document_text)
            os.replace(tmp_name, target_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    for relative, target in pending:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            document = local_completion_document(
                ReportGap(
                    missing_reports=tuple(missing_reports),
                    execution_id=reuse_execution_id,
                ),
                path=relative,
                node_id=node.node_id,
                receipts=list(anchored.get("receipts") or []),
            )
            write_record_atomically(target, document)
        except Exception as exc:
            # 渲染/写入异常同样是一次"处理结果"（第⑥条）：OSError 与非 OSError
            # （P5b：后者此前不留痕）都留下生成器/来源执行/结果后原样上抛，不吞
            # 异常、不改节点状态、不动恢复记账（S3/P3-13 语义不变）。
            # KeyboardInterrupt/SystemExit 是中断而非"处理结果"：由内层清理临时
            # 文件后原样上抛，不留事件（P3-13 的"中断"用例语义不变）。
            refuse(
                "write_failed",
                f"local report completion write failed: {type(exc).__name__}: {exc}",
                action="raised",
                consume_churn=False,
            )
            raise
        written.append(relative)

    # 重新判定：用同一个 Verifier 判据复核缺口是否真的消失。结果只作诊断
    # （``gap_cleared``），绝不在此改写节点状态或验收结论——判定权属仍然在
    # Verifier 与交付门。
    gap_cleared = None
    try:
        dimension = self.verifier._verify_deterministic_acceptance(node)
    except Exception:  # noqa: BLE001 - diagnostics only, never a decision
        dimension = None
    if dimension is not None:
        gap_cleared = getattr(dimension, "report_gap", None) is None

    # 不消耗 recovery_churn：本补全不产生恢复轮、不新增节点/执行，既未放宽
    # 预算也未新增尝试（旧派发路径的 productive 记账随整节点派发一起取消）。
    applied: Dict[str, Any] = {"added": [], "updated": []}
    self.trace_recorder.record(
        "orchestrator.verification_recovery.applied",
        {
            "node_id": node.node_id,
            "verdict": verification_result.verdict.value,
            "score": verification_result.overall_score,
            "attempt": 1,
            "recovery_type": "report_completion",
            "reuse_execution_id": reuse_execution_id,
            "failure_code": failure_code,
            "missing_reports": missing_reports,
            "reuse_execution_evidence": True,
            "rerun_functional_tests": False,
            "local_completion": True,
            "adds_nodes": 0,
            "adds_executions": 0,
            "written": written,
            "skipped_existing": skipped_existing,
            "gap_cleared": gap_cleared,
            "plan": plan,
            "patch": applied,
        },
    )
    return {
        "accepted": True,
        "rejected": False,
        "node_id": node.node_id,
        "recovery_type": "report_completion",
        "reuse_execution_id": reuse_execution_id,
        "failure_code": failure_code,
        "missing_reports": missing_reports,
        "reuse_execution_evidence": True,
        "rerun_functional_tests": False,
        "local_completion": True,
        "adds_nodes": 0,
        "adds_executions": 0,
        "written": written,
        "skipped_existing": skipped_existing,
        "gap_cleared": gap_cleared,
        "plan": plan,
        "patch": applied,
        "added": [],
        "updated": [],
    }


@recovery_operation
def _apply_governed_verification_recovery(self: Orchestrator, node, verification_result) -> Dict[str, Any]:
    """Ask GlobalGovernor for the only permitted verification-recovery patch."""
    dimensions = [
        {
            "name": dimension.name,
            "score": dimension.score,
            "passed": dimension.passed,
            "details": dimension.details,
            "evidence_refs": list(dimension.evidence_refs),
        }
        for dimension in verification_result.dimensions
    ]
    rejection_reason = ""
    # doc 18 修复F②：except 分支引用 patch——planner 自身抛错时
    # patch 未绑定，先置 None 防 UnboundLocalError 掩盖原始错误。
    patch: TaskGraphPatch | None = None  # type: ignore[assignment]
    # The worker's own report is first-class recovery routing input:
    # summary + structured next_actions (root-cause escalations) + the
    # last runtime failure.  Live audits showed replans iterating
    # test-side workarounds for six generations while every report
    # declared the root cause inside an upstream completed node's files.
    # Round 11: persist THIS verdict before proposing any recovery — if a
    # successor generation supersedes this node, its verdict must already
    # be on disk for the lineage's ancestor fallback (live r11: V2's
    # near-pass verdict was lost to supersession, leaving unverified V3
    # with no receipt anywhere in its chain).
    # F2: capture the premise baseline BEFORE this attempt persists its own
    # receipt.  ``_verification_premise_changed`` compares the files the
    # repaired verdict rested on against the files as they are *now*; if it
    # read the receipt this call is about to overwrite, the comparison would
    # always find "no movement" and the guard would refuse even a grounded
    # re-verification (m00547 invariant: 守卫不能阻止有明确依据的必要复验).
    previous_premise_baseline = self._verification_premise_baseline(node)
    self._persist_verification_receipt(node, verification_result)
    worker_report = self._worker_report_for_node(node)
    # Round 11 (live r11 resume #16): V2 sat ONE freshness receipt away
    # from approval (403/stdout/ownership evidence all confirmed), yet
    # successor V3's three verification-recovery proposals were all
    # cap-rejected (lineage history=11, none declared ownership) and the
    # terminal strike killed a nearly-done node.  The stalled path has
    # carried the cap-exemption recipe since round 8; this path never did
    # (flagged by the parallel-session audit).  Mine the same root-cause
    # files and hand the recipe to the governor prompt so recovery
    # proposals can declare the ownership that exempts them.
    cap_recipe_text = ""
    recipe_resolved = self._resolve_declared_root_causes(
        node, verification_result, worker_report, include_self_owned=True
    )
    if recipe_resolved:
        cap_recipe_text = (
            "\n谱系 cap 豁免配方（必须遵守，否则补丁会被再次拦截）："
            + json.dumps([{
                "terminal_node": node.node_id,
                "root_cause_files": {
                    token: {"inactive_owner": owner, "paths": paths}
                    for token, (owner, paths) in sorted(recipe_resolved.items())
                },
            }], ensure_ascii=False)
            + "\n上述 root_cause_files 的所有者均为非活跃节点"
            "（completed/superseded/failed），新修复/取证节点可以合法持有"
            "这些路径。新增节点必须在 owned_paths（或 expected_outputs）中"
            "逐字声明这些文件的完整相对路径——仅在 goal 文本中描述不构成"
            "声明；并把受阻消费者的 dependencies 改接到该节点。"
        )
    # doc 51 §9 边界④（确定性局部补全）：Verifier 已由锚定 receipt 把本次失败
    # 判定为"只缺报告"（runtime/report_gap.py，且预算耗尽时被 VETO 否决，
    # 故两者互斥）。此类缺口不需要 LLM 规划，也不允许重跑功能测试：直接在
    # 同一次执行内按原执行收据把报告写出来并重新判定（新增节点/执行数 = 0）。
    report_gap = getattr(verification_result, "report_gap", None)
    if isinstance(report_gap, dict) and (report_gap.get("missing_reports") or []):
        return self._complete_report_gap_locally(node, verification_result, report_gap)
    for patch_attempt in range(3):
        self.trace_recorder.set_recovery_attempt(patch_attempt)
        try:
            recovery_kwargs = {
                "node_id": node.node_id,
                "verdict": verification_result.verdict.value,
                "overall_score": verification_result.overall_score,
                "dimensions": dimensions,
                "worker_report": worker_report,
                "cap_exemption_recipe": cap_recipe_text,
            }
            if patch_attempt:
                recovery_kwargs["rejection_reason"] = rejection_reason
            patch = self.governor.plan_verification_recovery(**recovery_kwargs)
            # Round 12 (live r11 resume #18): REPAIR-ROOT sat one
            # file_read away from approval, yet three verification-
            # recovery proposals were cap-rejected (attempt_count=12)
            # and terminalized it — the provider kept returning
            # recovery_of takeovers with EMPTY owned_paths even though
            # the prompt carried the exemption recipe and the system
            # rules mandate "保留 owned_paths、output_artifacts、交付
            # 条款与验收责任".  An explicit takeover of the failed node
            # therefore inherits the failed node's ownership contract
            # when the provider omitted it, making the depth-cap
            # exemption structural instead of narrative.  Nodes without
            # recovery_of keep passing the normal ownership gate below.
            #
            # Round 13 (live r11 resume #19, churn diag 20:31-20:37):
            # inheritance alone backfires when the takeover PIVOTS
            # responsibility — the failed node was a TESTER (security
            # paths), the proposal was a CODER fixing the missing
            # backend assign endpoint, and its file_write to main.py
            # died on "outside the Worker owned_paths contract" (the
            # inherited/declared tester paths locked it out), after
            # which the worker flailed seven tool rounds into
            # runtime_no_progress.  The recipe already mined exactly
            # the right files with INACTIVE owners (completed backend
            # node — no graph conflict, exclusion at recovery_of), so
            # union the recipe paths into every takeover's contract:
            # the pivot coder can then write the root cause it was
            # routed to fix.
            recipe_paths: list[str] = []
            for _owner, paths in recipe_resolved.values():
                for path in paths:
                    normalized = str(path).replace("\\", "/").strip().strip("/")
                    if normalized and normalized not in recipe_paths:
                        recipe_paths.append(normalized)
            for raw_node in patch.add_nodes:
                if not isinstance(raw_node, dict):
                    continue
                if str(raw_node.get("recovery_of") or "").strip() != node.node_id:
                    continue
                declared = [
                    str(item).replace("\\", "/").strip().strip("/")
                    for item in (raw_node.get("owned_paths") or [])
                    if str(item).strip()
                ]
                if not declared and node.owned_paths:
                    declared = [
                        str(item).replace("\\", "/").strip().strip("/")
                        for item in node.owned_paths
                    ]
                    raw_node["owned_paths"] = list(node.owned_paths)
                    self.trace_recorder.record(
                        "governor.verification_recovery.takeover_ownership_inherited",
                        {
                            "node_id": node.node_id,
                            "replacement_node_id": str(
                                raw_node.get("node_id") or raw_node.get("task_id") or ""
                            ),
                            "inherited_path_count": len(node.owned_paths),
                        },
                    )
                missing = [
                    path for path in recipe_paths
                    if not any(
                        path == owned or path.startswith(owned.rstrip("/") + "/")
                        for owned in declared
                    )
                ]
                if missing:
                    raw_node["owned_paths"] = declared + missing
                    self.trace_recorder.record(
                        "governor.verification_recovery.takeover_root_cause_paths_granted",
                        {
                            "node_id": node.node_id,
                            "replacement_node_id": str(
                                raw_node.get("node_id") or raw_node.get("task_id") or ""
                            ),
                            "granted_paths": missing,
                        },
                    )
            # Verification recovery is scoped to the failed node.  Some
            # providers omit ``recovery_of`` even when proposing a new
            # node with the exact same files/artifacts.  Normalize only
            # that unambiguous takeover here; unrelated workers must
            # still pass the normal ownership gate.
            for raw_node in patch.add_nodes:
                if not isinstance(raw_node, dict):
                    continue
                if str(raw_node.get("recovery_of") or raw_node.get("supersedes") or "").strip():
                    continue
                proposed_id = str(raw_node.get("node_id") or raw_node.get("task_id") or "").strip()
                # The governed verifier convention uses <failed>-R<n>
                # for recovery nodes.  Treat that explicit naming as an
                # unambiguous takeover even when the provider omitted the
                # optional recovery_of field and declared no paths in the
                # raw JSON envelope.
                if proposed_id.startswith(f"{node.node_id}-R"):
                    raw_node["recovery_of"] = node.node_id
                    raw_node.setdefault("metadata", {})["recovery_of"] = node.node_id
                    raw_node.setdefault("metadata", {})["auto_recovery_of"] = True
                    self.trace_recorder.record("governor.verification_recovery.normalized_takeover", {
                        "node_id": node.node_id,
                        "replacement_node_id": proposed_id,
                        "reason": "verifier recovery-node naming convention",
                    })
                    continue
                proposed_paths = {
                    str(item).replace("\\", "/").rstrip("/")
                    for item in raw_node.get("owned_paths", []) or []
                    if str(item).strip()
                }
                # Providers often express the contract through
                # expected_outputs (or metadata) and omit the normalized
                # owned_paths field.  Use those declared files only for
                # this failed-node takeover inference; the Governor still
                # performs the authoritative normalization and validation.
                if not proposed_paths:
                    proposed_paths = {
                        str(item).replace("\\", "/").strip("/")
                        for item in (
                            raw_node.get("expected_outputs", [])
                            or (raw_node.get("metadata") or {}).get("expected_outputs", [])
                        )
                        if str(item).strip()
                        and not str(item).strip().startswith("artifact:")
                        and Path(str(item)).suffix
                    }
                target_paths = {
                    str(item).replace("\\", "/").rstrip("/")
                    for item in node.owned_paths
                    if str(item).strip()
                }
                proposed_artifacts = {str(item).strip() for item in raw_node.get("output_artifacts", []) or []}
                if not proposed_artifacts:
                    proposed_artifacts = {
                        str(item).strip()
                        for item in (raw_node.get("metadata") or {}).get("output_artifacts", []) or []
                        if str(item).strip()
                    }
                target_artifacts = {str(item).strip() for item in node.output_artifacts}
                if (proposed_paths & target_paths) or (proposed_artifacts & target_artifacts):
                    raw_node["recovery_of"] = node.node_id
                    raw_node.setdefault("metadata", {})["recovery_of"] = node.node_id
                    raw_node.setdefault("metadata", {})["auto_recovery_of"] = True
                    self.trace_recorder.record("governor.verification_recovery.normalized_takeover", {
                        "node_id": node.node_id,
                        "replacement_node_id": raw_node.get("node_id") or raw_node.get("task_id"),
                        "reason": "exact failed-node ownership overlap",
                    })
            # A recovery proposal that only re-admits the same node and
            # adds no new evidence is not progress. Count repeats before
            # applying it so the graph cannot cycle indefinitely.
            added_nodes = list(getattr(patch, "add_nodes", []) or [])
            updated_nodes = list(getattr(patch, "update_nodes", []) or [])
            reopens_same_node = any(
                str(item.get("node_id") or item.get("task_id")) == node.node_id
                and str(item.get("status") or "") in {"ready", "repair_pending"}
                for item in updated_nodes
                if isinstance(item, dict)
            )
            # Layer-2 self-lock guard: the proposal adds nodes but its own
            # reason codes say the premise is frozen (stale evidence that a
            # read-only verifier cannot refresh).  Such a proposal cannot
            # produce new evidence, so it counts as no progress and is
            # stopped at the second occurrence instead of growing the graph
            # until the run-level churn breaker fires.
            self_lock_codes = self._verification_self_lock_codes(patch)
            if self_lock_codes:
                occurrences = int(
                    node.metadata.get("_verification_recovery_self_lock", 0)
                ) + 1
                node.metadata["_verification_recovery_self_lock"] = occurrences
                self.trace_recorder.record(
                    "governor.verification_recovery.self_lock_detected",
                    {
                        "node_id": node.node_id,
                        "reason_codes": self_lock_codes,
                        "occurrence": occurrences,
                        "add_node_count": len(added_nodes),
                        "action": "count_as_no_progress",
                    },
                )
                if occurrences >= 2:
                    raise ValueError(
                        "verification recovery is self-locked: the verifier "
                        "declares its own evidence stale and this premise "
                        "cannot refresh it "
                        f"({', '.join(self_lock_codes)}); rerunning it cannot "
                        "produce new evidence "
                        f"({occurrences} occurrences)"
                    )
            patch = self._convert_completed_reopen_to_repair_node(patch, node)
            patch = self._normalize_recovery_ownership_transfer(patch)
            self._strip_unregistered_artifact_refs(patch)
            self._normalize_recovery_targets(patch)
            # doc 18 修复F①：剥离制造裸节点后的二次接管扫描——
            # attempt5 实证：提案 recovery_of=COMPLETED 节点被
            # _normalize_recovery_targets 剥离后，首轮接管规范化已
            # 错过（它跳过带 recovery_of 的节点），裸节点与活跃
            # repair_pending 所有者冲突 → governor 三连拒烧预算。
            # 规范化器幂等（唯一冲突才接管、已有 recovery_of 跳过），
            # 二次调用只补上"剥离后"这个新状态。
            patch = self._normalize_recovery_ownership_transfer(patch)
            self._ensure_artifact_producer_deps(patch)
            resolved_root_causes = self._resolve_declared_root_causes(
                node, verification_result, worker_report
            )
            # doc 18 修复E：矛盾指令接缝先规范化再路由检查——
            # "接管 COMPLETED 根因文件"与"目标必须 failed"两验证器
            # 的合法交集形状由确定性改写补全，不再靠三连拒烧预算。
            self._normalize_root_cause_takeover(node, patch, resolved_root_causes)
            addresses_upstream = self._enforce_root_cause_routing(
                node, patch, resolved_root_causes,
                # 第三次提案（终判前最后一案）：确定性补救代替拒绝
                last_resort=(patch_attempt == 2),
            )
            # Round 8: self-owned offender files count for the depth-cap
            # exemption (ownership-declaring takeover = genuine change of
            # approach) even though they never count as UPSTREAM routing.
            declares_offender = self._patch_covers_root_causes(
                patch,
                self._resolve_declared_root_causes(
                    node, verification_result, worker_report,
                    include_self_owned=True,
                ),
            )
            # Cross-generation repeat guard: a new node ID must not reset
            # the lineage memory. Same root cause + same file state +
            # same repair approach is blocked from the second occurrence
            # on (doc 14 §12:50 user directive, step 3).
            lineage_fingerprint = self._lineage_recovery_fingerprint(
                node, patch, verification_result, worker_report,
                resolved_root_causes,
            )
            self._check_lineage_recovery_repeat(
                node,
                lineage_fingerprint,
                addresses_upstream=addresses_upstream,
                declares_offender_ownership=declares_offender,
                derives_lineage=self._patch_derives_from_lineage(node, patch),
            )
            # DeerFlow 2.0 对齐 / F2（live b31）：**"新增了节点"本身不是进展** ✗。
            # 原判据是 ``not added_nodes and …``，于是只要规划器往图里塞节点就
            # 永远算"有进展"，no-progress 制动从不触发——线上因此长出
            # R003..R009 七代运行时自证、R016..R021 六代终验。DeerFlow 的
            # 分层里验收是就地判定、不可判定即 UNVERIFIED、绝不派活；所以这里
            # 按**证据**判定：补丁声明了图上还没有的交付物/证据路径，或判决所
            # 依据的前提文件此后确实变过，才算推进。
            # F2 位置说明：必须在规范化之后、且在线系去重之后判定——规范化会把
            # 失败节点的归属路径补进补丁（那本身就是新证据），线系守卫则要保留
            # 它更精确的"跨代重复"判词。
            normalized_added = list(getattr(patch, "add_nodes", []) or [])
            normalized_updates = list(getattr(patch, "update_nodes", []) or [])
            reopens_same_node = any(
                str(item.get("node_id") or item.get("task_id")) == node.node_id
                and str(item.get("status") or "") in {"ready", "repair_pending"}
                for item in normalized_updates
                if isinstance(item, dict)
            )
            premise_changed = self._verification_premise_changed(
                node, baseline=previous_premise_baseline
            )
            # 被本补丁取代/改写的节点（含当前节点）交出的归属不算"已有证据"：
            # 接管、去冲突、完成态重开转修复节点都必须能被判为进展。
            superseded_owners = self._patch_superseded_owners(
                node, normalized_added, normalized_updates
            )
            adds_new_evidence = self._added_nodes_claim_new_evidence(
                normalized_added, exclude_node_ids=superseded_owners
            )
            no_progress = (
                (not normalized_added and reopens_same_node)
                or (bool(normalized_added) and not adds_new_evidence)
            ) and not premise_changed
            if no_progress:
                repeats = int(node.metadata.get("_verification_recovery_no_progress", 0)) + 1
                node.metadata["_verification_recovery_no_progress"] = repeats
                self.trace_recorder.record(
                    "governor.verification_recovery.no_new_evidence",
                    {
                        "node_id": node.node_id,
                        "occurrence": repeats,
                        "add_node_count": len(normalized_added),
                        "reopens_same_node": bool(reopens_same_node),
                        "premise_changed": False,
                        "reason": "added nodes declare no new deliverable or evidence",
                    },
                )
                # F2: reject on the FIRST occurrence.  The old rule waited for
                # three consecutive no-op proposals; live b31 paid ~6 minutes
                # per extra re-run (N-102 exec-000006 ran three times within
                # one executor dispatch, R-601 twice) with zero product
                # increment.
                raise ValueError(
                    "需要改变方案 (change of approach required): "
                    "verification recovery produced no new work or evidence "
                    f"for {repeats} proposal(s) resting on an unchanged "
                    "premise — the nodes it adds declare no deliverable or "
                    "evidence path the graph does not already own, and none of "
                    "the files the verdict rested on moved, so this premise "
                    "cannot be refreshed by re-running it. Propose an upstream "
                    "repair node that owns the root-cause files, a genuinely "
                    "different fix strategy, or escalate with new evidence."
                )
            else:
                node.metadata["_verification_recovery_no_progress"] = 0
            if patch is None:
                raise ValueError("Verification recovery produced no patch")
            applied = self.governor.apply_task_graph_patch(
                patch,
                parent_task_id=node.node_id,
            )
            self._record_lineage_recovery_attempt(
                node, patch, lineage_fingerprint, applied
            )
            # doc 18 修复C：替换提交内原子改写下游依赖死边，
            # 原生产者→当前生产者→下游满足链在此统一维护。
            self._rewire_superseded_dependents()
            # doc 18 churn 预算：应用成功的恢复补丁是结构性变更，
            # 计入 run 级单调预算（身份漂移不能绕过）。
            self._consume_recovery_churn(
                "verification_recovery_applied",
                # F12: only *count down* the no-progress budget when the
                # proposal really moved the graph — new evidence paths, or a
                # premise that changed since the verdict.
                structural_changed=bool(adds_new_evidence or premise_changed),
            )
            self.trace_recorder.record(
                "orchestrator.verification_recovery.applied",
                {
                    "node_id": node.node_id,
                    "verdict": verification_result.verdict.value,
                    "score": verification_result.overall_score,
                    "attempt": patch_attempt + 1,
                    # doc 51 §9 显式默认值：非报告缺口属于"必须重新执行/
                    # 重新验证"的一侧，不复用既有执行证据，也不声明缺失
                    # 报告（报告缺口走 _complete_report_gap_locally）。
                    "recovery_type": "verification_recovery",
                    "reuse_execution_id": "",
                    "failure_code": str(
                        (node.metadata or {}).get("error_type") or ""
                    ),
                    "missing_reports": [],
                    "reuse_execution_evidence": False,
                    "rerun_functional_tests": True,
                    "patch": applied,
                },
            )
            return applied
        except (ValueError, TypeError, KeyError) as exc:
            rejection_reason = str(exc)[:1000]
            # doc 18 修复F②：终判前最后一案的所有权冲突 → 确定性
            # 接管转换后重试一次（判决词自认的合法形状）。attempt5
            # 实证：N-003-R* 谱系每代三连拒 → 终判 → 级联，约 6 单位
            # 预算烧在同一冲突上；转换把 3拒+终判 变成 2拒+1救。
            if (
                patch_attempt == 2
                and patch is not None
                and (
                    self._convert_conflict_to_takeover(patch, rejection_reason)
                    or self._convert_redundant_to_dependent(patch, rejection_reason)
                )
            ):
                try:
                    applied = self.governor.apply_task_graph_patch(
                        patch,
                        parent_task_id=node.node_id,
                    )
                except (ValueError, TypeError, KeyError) as retry_exc:
                    rejection_reason = str(retry_exc)[:1000]
                else:
                    self._record_lineage_recovery_attempt(
                        node, patch, lineage_fingerprint, applied
                    )
                    self._rewire_superseded_dependents()
                    self._consume_recovery_churn(
                        "verification_recovery_applied"
                    )
                    self.trace_recorder.record(
                        "governor.verification_recovery."
                        "ownership_conflict_takeover_converted",
                        {
                            "node_id": node.node_id,
                            "attempt": patch_attempt + 1,
                            "original_error": rejection_reason[:300],
                            "patch": applied,
                        },
                    )
                    self.trace_recorder.record(
                        "orchestrator.verification_recovery.applied",
                        {
                            "node_id": node.node_id,
                            "verdict": verification_result.verdict.value,
                            "score": verification_result.overall_score,
                            "attempt": patch_attempt + 1,
                            # doc 51 §9 显式默认值，同主应用点。
                            "recovery_type": "verification_recovery",
                            "reuse_execution_id": "",
                            "failure_code": str(
                                (node.metadata or {}).get("error_type") or ""
                            ),
                            "missing_reports": [],
                            "reuse_execution_evidence": False,
                            "rerun_functional_tests": True,
                            "patch": applied,
                        },
                    )
                    return applied
            # doc 18 churn 预算：被拒提案同样消耗预算——"拒绝→豁免
            # →再拒绝"的所有权冲突死锁环（DeerFlow 对照 #6）由此有界。
            self._consume_recovery_churn("verification_recovery_rejected")
            self.trace_recorder.record(
                "governor.verification_recovery.rejected",
                {
                    "node_id": node.node_id,
                    "attempt": patch_attempt + 1,
                    "error": rejection_reason,
                    "reason_codes": ["INVALID_RECOVERY_PATCH"],
                    "status": node.status,
                    "action": "replan" if patch_attempt < 2 else "fail",
                },
            )

    # Three independently generated patches violated the state/graph
    # contract. Only now is the recovery considered exhausted.
    error = rejection_reason or "verification recovery patch rejected"
    node.metadata["_recovery_patch_rejected"] = True
    node.metadata["_terminal_failure"] = True
    node.metadata["_terminal_reason"] = (
        f"invalid governed verification recovery patch: {error}"
    )
    # Round 8 (live r11 run-blocked postmortem): persist the verifier's
    # evidence truth onto the node.  The stalled planner runs after
    # restarts where the in-memory verification_results are gone; without
    # this receipt it re-planned from stale ancestral root causes — three
    # identical init_db.py proposals while the verifier's actual blocking
    # gap was the missing live HTTP receipts.
    self._persist_verification_receipt(node, verification_result)
    if node.status == TaskStatus.REPAIR_PENDING.value:
        self.task_graph.update_node_status(node.node_id, TaskStatus.FAILED.value)
    # Converge run state at the terminal boundary, not at the next
    # iteration (user directive 2026-09-12).
    self._converge_runtime_state_after_terminal(
        node.node_id, "invalid_governed_recovery_patch"
    )
    return {
        "accepted": False,
        "rejected": True,
        "node_id": node.node_id,
        "error": error,
    }


def _process_worker_results(self: Orchestrator, wave_results: Mapping[str, bool | WaveResult], results: ExecutionRunResult) -> None:
    """Apply existing Governor/Verifier transitions on the orchestration thread."""
    for node_id, success in wave_results.items():
        node = self.task_graph.get_node(node_id)
        if node is None:
            raise RuntimeError(f"Executed node {node_id!r} disappeared from task graph")
        if success:

            # A previous failure may already have admitted an
            # exception-recovery child.  Once this retry succeeds,
            # close that stale branch before it can become ready.
            self._supersede_stale_recovery_children(node_id)

            # Worker-discovered work is only a proposal. GlobalGovernor
            # performs value admission and is the sole graph writer.
            if node.metadata.get("evidence_packet"):
                evidence_packet = node.metadata["evidence_packet"]
                next_actions = evidence_packet.get("next_actions", [])

                if next_actions and isinstance(next_actions, list) and len(next_actions) > 0:
                    try:
                        admission = self.governor.admit_worker_subtasks(
                            parent_node_id=node_id,
                            proposals=next_actions,
                        )
                        spawned_nodes = list(admission.get("added", []))
                        spawned_count = len(spawned_nodes)
                        if spawned_count > 0:
                            results.setdefault("semantic_spawns", 0)
                            results["semantic_spawns"] += spawned_count
                            self.trace_recorder.record("orchestrator.governed_semantic_expansion", {
                                "parent": node_id,
                                "proposed_count": len(next_actions),
                                "spawned_count": spawned_count,
                                "spawned_nodes": spawned_nodes,
                                "rejected_count": len(admission.get("rejected_proposals", [])),
                            })
                    except Exception as se:
                        self.trace_recorder.record(
                            "governor.worker_subtasks.failed",
                            {"node_id": node_id, "error": str(se)},
                        )

            # Verification-driven replanning: verify result_submitted node (阶段五 P0)
            self.verifier.task_graph = self.task_graph

            is_repair = bool(node and node.metadata.get("_is_repair_node", False))
            if is_repair:
                self.trace_recorder.record("orchestrator.repair.verification_required", {
                    "node_id": node_id,
                    "repair_target": node.metadata.get("_repair_target", ""),
                })
            try:
                # Transition to verifying state
                if node.status == "result_submitted":
                    self.task_graph.update_node_status(node_id, "verifying")

                # Fix AH (doc 18, 2026-09-15 r13b): freeze the bytes this
                # verdict is ABOUT.  ExecutionStore.accept() previously
                # vetoed an approved verdict whenever the owned tree had
                # settled between finish() and accept() — a launched
                # service flushing its log, a stop-flag from the shutdown
                # handshake, a WAL checkpoint on clean close.  The Verifier
                # reads the SETTLED bytes, so its verdict was correct and
                # the veto was not.  Recording the basis here makes the
                # gate check what it actually means to check: that nothing
                # moved under the verdict after it was formed.
                self._record_verification_basis(node)

                # Perform verification
                vr = self.verifier.verify_node(node_id)

                # Update state based on verification result
                if vr.verdict == VerificationVerdict.APPROVED:
                    self._finalize_node_evidence(
                        node,
                        approved=True,
                        verification_score=vr.overall_score,
                        verdict=vr.verdict,
                    )
                    self.task_graph.update_node_status(node_id, "completed")
                    self._record_worker_verdict(node, vr.verdict)
                    self.trace_recorder.record(
                        "node.completed",
                        {
                            "node_id": node_id,
                            "status": TaskStatus.COMPLETED.value,
                            "verification_score": vr.overall_score,
                        },
                    )
                    if is_repair:
                        parent_id = node.metadata.get("parent_node_id")
                        parent = self.task_graph.get_node(parent_id) if parent_id else None
                        if parent and parent.status == "repair_pending":
                            self.task_graph.update_node_status(parent.node_id, "ready")
                            self.trace_recorder.record(
                                "orchestrator.repair.parent_rearmed",
                                {
                                    "parent_node": parent_id,
                                    "repair_node": node_id,
                                    "verification_score": vr.overall_score,
                                },
                            )
                elif vr.verdict == VerificationVerdict.REPAIR_REQUIRED:
                    self._finalize_node_evidence(
                        node,
                        approved=False,
                        verification_score=vr.overall_score,
                        verdict=vr.verdict,
                    )
                    self.task_graph.update_node_status(node_id, "repair_pending")
                    self._record_worker_verdict(node, vr.verdict)
                    self.trace_recorder.record(
                        "node.repair_pending",
                        {
                            "node_id": node_id,
                            "status": TaskStatus.REPAIR_PENDING.value,
                            "verification_score": vr.overall_score,
                        },
                    )
                    results.setdefault("verification_repairs", 0)
                    results["verification_repairs"] += 1
                    self.trace_recorder.record("orchestrator.verification.repair", {
                        "node_id": node_id,
                        "score": vr.overall_score,
                        "dimensions": {d.name: d.score for d in vr.dimensions},
                    })
                    repair_attempts = int(node.metadata.get("_verification_repair_attempts", 0)) + 1
                    node.metadata["_verification_repair_attempts"] = repair_attempts
                    applied = self._apply_governed_verification_recovery(node, vr)
                    results.setdefault("governed_verification_repairs", 0)
                    results["governed_verification_repairs"] += 1
                    node.metadata["_last_verification_recovery_patch"] = applied
                elif vr.verdict == VerificationVerdict.REPLAN_REQUIRED:
                    self._finalize_node_evidence(
                        node,
                        approved=False,
                        verification_score=vr.overall_score,
                        verdict=vr.verdict,
                    )
                    self.task_graph.update_node_status(node_id, "repair_pending")
                    self._record_worker_verdict(node, vr.verdict)
                    results.setdefault("verification_replans", 0)
                    results["verification_replans"] += 1
                    verification_replans = int(
                        node.metadata.get("_verification_replan_attempts", 0)
                    ) + 1 if node else 1
                    if node:
                        node.metadata["_verification_replan_attempts"] = verification_replans
                    applied = self._apply_governed_verification_recovery(node, vr)
                    self.trace_recorder.record("orchestrator.verification.replan", {
                        "node_id": node_id,
                        "repeat_count": verification_replans,
                        "patch": applied,
                        "score": vr.overall_score,
                    })
                else:
                    # Verification failed
                    self._finalize_node_evidence(
                        node,
                        approved=False,
                        verification_score=vr.overall_score,
                        verdict=vr.verdict,
                    )
                    self.task_graph.update_node_status(node_id, "failed")
                    self._record_worker_verdict(node, vr.verdict)
                    self.trace_recorder.record(
                        "node.failed",
                        {
                            "node_id": node_id,
                            "status": TaskStatus.FAILED.value,
                            "verification_score": vr.overall_score,
                            "verdict": vr.verdict.value,
                        },
                    )
            except Exception as e:
                logger.warning("Verifier failed for %s: %s", node_id, e)
                # Fix AL (doc 18, 2026-09-15 r13b): a transport cut inside
                # the VERIFIER is the same misattribution Fix AI removed on
                # the Worker side, one boundary later.  Live gen 16:
                #   Verifier failed for PATCH-R007-RUNTIME:
                #   Network error from LLM API after 120.6s: The read
                #   operation timed out
                # The node had reached result_submitted with its evidence on
                # disk and no staleness veto; the network merely failed to
                # deliver a verdict.  Forcing inconclusive/0.0 here does two
                # wrong things: it pushes a sound node to repair_pending, and
                # _finalize_node_evidence writes a PERMANENT rejection into
                # the execution journal for a read timeout.
                #
                # No verdict was obtained, so none is recorded.  The node
                # returns to result_submitted and verification is retried.
                # Bounded by the same budget as Fix AI, so a real outage
                # still reaches the breaker rather than spinning here.
                # Fix AM: classify through the __cause__ chain (see
                # llm_client.is_transport_interruption) -- gen 17 lost a
                # verdict to an LLMClientError that wrapped a circuit-open
                # rejection, which the isinstance test could not see.
                from AstralNexus.runtime.llm_client import is_transport_interruption
                if is_transport_interruption(e):
                    try:
                        budget = max(0, int(os.environ.get(
                            "ASTRALNEXUS_NODE_TRANSPORT_INTERRUPTS", "3")))
                    except (TypeError, ValueError):
                        budget = 3
                    seen = int(node.metadata.get("verifier_transport_interrupts", 0)) + 1
                    node.metadata["verifier_transport_interrupts"] = seen
                    self.trace_recorder.record("verifier.transport_interrupted", {
                        "node_id": node_id,
                        "attempt": seen,
                        "budget": budget,
                        "error": str(e)[:300],
                        "error_type": type(e).__name__,
                        "disposition": "requeued" if seen <= budget else "budget_exhausted",
                        "note": "未取得判定即中断；不记录裁决，重排验收",
                    })
                    if seen <= budget:
                        try:
                            if node.status == "verifying":
                                self.task_graph.update_node_status(
                                    node_id, "result_submitted")
                        except ValueError:
                            pass
                        continue
                    # A bounded retry budget is a stop boundary, not a
                    # license to turn transport failure into INCONCLUSIVE.
                    # Preserve the submitted result and leave the run
                    # blocked for explicit human review; no receipt or
                    # recovery patch may be emitted without a verdict.
                    if node.status == "verifying":
                        try:
                            self.task_graph.update_node_status(
                                node_id, "result_submitted")
                        except ValueError:
                            pass
                    self._verification_unavailable = {
                        "node_id": node_id,
                        "error_type": type(e).__name__,
                        "error": str(e)[:500],
                        "attempts": seen,
                        "budget": budget,
                    }
                    self.trace_recorder.record(
                        "verifier.unavailable",
                        {
                            **self._verification_unavailable,
                            "stop_code": "verification_unavailable",
                            "note": "未取得验收判定；保留待验收结果，停止自动修复",
                        },
                    )
                    self._persist_graph_checkpoint(
                        stage="verifier_transport_budget_exhausted",
                        node_id=node_id,
                    )
                    continue
                # Fix AF (doc 18, 2026-09-15 r13b): this handler turns any
                # exception into inconclusive/0.0, but the reason went only
                # to logger.warning — the trace showed a node dropping from
                # an approved 0.9917 to 0.0 with no cause recorded anywhere,
                # and the actual message ("Execution evidence is stale")
                # had to be excavated from the runner's stderr.  A verdict
                # reversal must be auditable from the trace alone.  The
                # reversal itself is intentional: ExecutionStore.accept() is
                # a hard gate and its veto must stand.
                self.trace_recorder.record("verifier.exception", {
                    "node_id": node_id,
                    "error": str(e)[:500],
                    "error_type": type(e).__name__,
                    "verdict_forced": VerificationVerdict.INCONCLUSIVE.value,
                    "note": "验收判定被异常改写；原因以本事件为准",
                })
                self._finalize_node_evidence(
                    node,
                    approved=False,
                    verification_score=0.0,
                    verdict=VerificationVerdict.INCONCLUSIVE,
                )
                try:
                    if node.status == "verifying":
                        self.task_graph.update_node_status(node_id, "repair_pending")
                    inconclusive = VerificationResult(
                        node_id=node_id,
                        verdict=VerificationVerdict.INCONCLUSIVE,
                        dimensions=[
                            VerificationDimension(
                                name="verifier_runtime",
                                score=0.0,
                                passed=False,
                                details=str(e),
                            )
                        ],
                        overall_score=0.0,
                    )
                    self.trace_recorder.record(
                        "verifier.failed",
                        {
                            "node_id": node_id,
                            "error": str(e),
                            "status": TaskStatus.REPAIR_PENDING.value,
                        },
                    )
                    self._apply_governed_verification_recovery(node, inconclusive)
                except Exception as recovery_error:
                    self.trace_recorder.record(
                        "governor.verification_recovery.failed",
                        {
                            "node_id": node_id,
                            "verifier_error": str(e),
                            "recovery_error": str(recovery_error),
                            "status": node.status,
                        },
                    )

            # Verification is the terminal lifecycle boundary.  Save
            # after it, not only before it, so SQLite reflects the
            # same completed/repair_pending state observed in trace.
            self._persist_graph_checkpoint(
                stage="post_verification", node_id=node_id
            )
        else:

            # P0-1 Fix: Skip recovery for terminal-failure nodes
            node_obj = self.task_graph.get_node(node_id)
            if node_obj and (node_obj.metadata.get("_terminal_failure") or node_obj.metadata.get("_capability_gap_exhausted")):
                self.trace_recorder.record("orchestrator.node_terminal_skip_recovery", {
                    "node_id": node_id,
                    "reason": node_obj.metadata.get("_terminal_reason", "terminal_failure"),
                })
                continue  # Skip to next wave result

            # Execution failures enter the single governed recovery
            # path; no auxiliary spawner may mutate the graph.
            recovery_success = self._attempt_recovery(node_id)
            if recovery_success:
                results["recovery_attempts"] += 1
            else:
                # Recovery failed — mark node as terminal-failed but
                # do NOT break the entire run. Other nodes may still
                # complete and contribute to a partial delivery.
                self.trace_recorder.record("orchestrator.node_fatal", {
                    "node_id": node_id,
                    "reason": "recovery_exhausted",
                })
                node_obj = self.task_graph.get_node(node_id)
                if node_obj:
                    node_obj.metadata["_terminal_failure"] = True
                    node_obj.metadata["_terminal_reason"] = "recovery_exhausted"
                results.setdefault("terminal_failures", [])
                results["terminal_failures"].append(node_id)
                # Terminalization is a graph-truth change: converge the
                # observable run state NOW (user directive 2026-09-12 —
                # waiting for the next iteration boundary left monitors
                # and resumes reading stale pre-terminal snapshots).
                self._converge_runtime_state_after_terminal(
                    node_id, "recovery_exhausted"
                )


    self._refresh_result_counts(results)


def _persist_verification_receipt(self: Orchestrator, node, verification_result) -> None:
    """Persist the verifier's evidence truth onto the node's metadata.

    Round 8: written at 3-strike terminalization so post-restart stalled
    planning sees the real gaps.  Round 11 (live r11 resume #16): ALSO
    written at every verification-recovery attempt — V2 sat one freshness
    receipt away from approval, was superseded by V3, and its verdict
    existed only in memory; V3 then died unverified (exec partial) with
    NO receipt anywhere in its chain, and the next stalled planner lost
    the precise gap.  Superseded generations must carry their verdicts
    on disk so ``_latest_verification_view``'s ancestor fallback can
    serve the newest real verification truth.
    """
    try:
        node.metadata["_verification_receipt"] = {
            "verdict": verification_result.verdict.value,
            "overall_score": verification_result.overall_score,
            "observations": [
                observation
                for dimension in verification_result.dimensions
                for observation in getattr(dimension, "observations", [])
            ],
            "failed_dimensions": [
                {
                    "name": dimension.name,
                    "score": dimension.score,
                    "details": self._clip_head_tail(
                        dimension.details, 950, 500
                    ),
                }
                for dimension in verification_result.dimensions
                if not dimension.passed
            ],
        }
    except Exception:
        self.trace_recorder.record(
            "orchestrator.verification_receipt.persist_failed",
            {"node_id": node.node_id},
        )


def _latest_verification_view(self: Orchestrator, node):
    """Best available verification truth for ``node`` (round 8).

    Prefers the live in-memory ``VerificationResult``; after a restart
    falls back to the ``_verification_receipt`` persisted at
    terminalization, normalized so ``_resolve_declared_root_causes`` can
    mine its dimension details exactly like a live result.

    Round 11: when the node itself was never verified (live r11: V3's
    exec ended partial, no verdict of its own), walks the ``recovery_of``
    chain and serves the NEAREST ancestor's live result or persisted
    receipt — the freshest real verification truth of the lineage.
    Returns None when nothing exists anywhere in the chain.
    """
    result = None
    try:
        result = self.verifier.verification_results.get(node.node_id)
    except Exception:
        result = None
    if result is not None:
        return result
    receipt = node.metadata.get("_verification_receipt")
    if isinstance(receipt, dict) and receipt:
        return SimpleNamespace(
            verdict=receipt.get("verdict"),
            overall_score=receipt.get("overall_score"),
            dimensions=receipt.get("failed_dimensions") or [],
        )
    seen = {node.node_id}
    current = node
    for _ in range(8):
        parent_id = str(
            (getattr(current, "metadata", None) or {}).get("recovery_of")
            or ""
        ).strip()
        if not parent_id or parent_id in seen:
            return None
        seen.add(parent_id)
        parent = self.task_graph.get_node(parent_id)
        if parent is None:
            return None
        try:
            parent_result = self.verifier.verification_results.get(parent_id)
        except Exception:
            parent_result = None
        if parent_result is not None:
            return parent_result
        parent_receipt = (getattr(parent, "metadata", None) or {}).get(
            "_verification_receipt"
        )
        if isinstance(parent_receipt, dict) and parent_receipt:
            return SimpleNamespace(
                verdict=parent_receipt.get("verdict"),
                overall_score=parent_receipt.get("overall_score"),
                dimensions=parent_receipt.get("failed_dimensions") or [],
            )
        current = parent
    return None


