"""Recovery responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .lineage_index import LineageIndex
from .support import (
    Any,
    Callable,
    Dict,
    Fault,
    InterruptionRequested,
    LLMClientError,
    List,
    Optional,
    Path,
    PathIdentityError,
    ROOT_CAUSE_TEXT_SUFFIXES,
    _extract_response_envelope,
    _json_safe,
    datetime,
    declared_tree_member,
    hashlib,
    identity_key,
    json,
    paths_overlap,
    re,
    recovery_operation,
    synchronized_state,
    time,
    timezone,
    workspace_identity,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


@synchronized_state
def _consume_recovery_churn(
    self: Orchestrator, kind: str, *, weight: int = 1, productive: bool = False,
    structural_changed: Optional[bool] = None,
    lineage_root: Optional[str] = None,
) -> int:
    """Count one structural autonomous-recovery event against the run budget.

    doc 18 / DeerFlow 对照（2814bd5d）：DeerFlow 的续跑帽挂在同一
    goal/run 身份的单调计数器上（continuation_count / no_progress_count），
    帽触发即终态 stand-down，capped run 永不派生新 run。天枢的恢复是
    新图实体，身份逐代漂移（N-02→R1→R-01-FIX→…），一切按身份取键的
    重复检测都会被漂移绕过。本计数器挂在 RUN 身份上、单调累计、提示词
    配方不能豁免——与 DeerFlow 语义对齐的结构性上界。

    doc 18 / live b31（F12）：单调总数无法区分"修好了图"与"又提了一遍
    冻结的前提"——线上 20/20 触发时，最后一份补丁刚刚解阻三个从未执行
    的节点。productive 是旧接口的结构变化投影；它不授予验收进展信用。
    是否减少无进展计数只能由已计功证据集合的增长决定。

    doc 18 / b33 case1 fix10（实测修正）：调用方声明的 productive 只是
    **"图变了"** 的证据，不足以证明交付进展。fix10 实跑 30 次恢复事件
    （`recovery_churn_total=30`，预算 20）熔断一次都没装：13 次
    productive 把无进展计数反复减半（4→2→1→0），因为 supersede/接管替换
    同样会改图并留下 ready 前沿，于是每次都被判"有进展"。认领条件因此
    收紧为**真实验收进展**：自上次计功以来新增稳定身份的有效证据信用。
    completed_nodes 仅用于观测，不能重置无进展计数；同一证据跨接管传递不重复计功。判据全部由运行自己算得，
    不由调用方宣告（DeerFlow 口径：只有运行算得出来的证据才算数）。
    """
    import uuid
    operation_id = str(getattr(self.trace_recorder, "recovery_operation_id", "") or uuid.uuid4().hex)
    if operation_id in self.runtime_state_manager.state.charged_recovery_operations:
        self.trace_recorder.record("orchestrator.recovery_churn.recorded", {
            "kind": kind, "recovery_operation_id": operation_id, "charged": False,
            "total": self.runtime_state_manager.state.recovery_churn_total,
        })
        return int(self.runtime_state_manager.state.recovery_churn_total)
    total = self.runtime_state_manager.record_churn(kind, weight=weight, operation_id=operation_id)
    completed = self._completed_node_count()
    evidence_count = len(self.runtime_state_manager.state.credited_evidence_keys or [])
    structural_changed = bool(productive) if structural_changed is None else structural_changed
    if self._churn_progress_marker is None:
        # 首次进入只立基线、不发放信用：在第一次恢复之前就已完成的节点
        # 不是这次恢复的功劳。
        self._churn_progress_marker = evidence_count
    proved = evidence_count > self._churn_progress_marker
    if proved:
        self._churn_progress_marker = evidence_count
        self.runtime_state_manager.record_recovery_progress()
    else:
        self.runtime_state_manager.record_no_progress_churn(weight=weight)
    no_progress = int(
        self.runtime_state_manager.state.recovery_no_progress_churn or 0
    )
    exhausted = no_progress >= self._recovery_limits().no_progress_operations
    self.trace_recorder.record("orchestrator.recovery_churn.recorded", {
        "kind": kind,
        "recovery_operation_id": operation_id,
        "charged": True,
        "weight": weight,
        "productive": bool(structural_changed),  # legacy projection
        "productive_proved": proved,  # legacy projection
        "structural_changed": bool(structural_changed),
        "acceptance_progress": proved,
        "completed_nodes": completed,
        "total": total,
        "no_progress_total": no_progress,
        "budget": self.recovery_churn_budget,
        "exhausted": exhausted,
    })
    if exhausted:
        # Latched, never re-opened by a later productive patch: a genuine
        # exhaustion stays the run's main blocking cause.
        self._recovery_budget_exhausted = True
    # b33 case1 fix10 第 4 步：恢复次数也累到**谱系根**上（换节点不清零）。
    try:
        self.runtime_state_manager.record_lineage_cost(
            lineage_root or self._lineage_root_for(""), recoveries=weight,
        )
    except Exception:
        # 记账失败不能改运行行为（成本统计是观测面，不是闸门）。
        pass
    return total


def admit_recovery(self: Orchestrator, origin: str, gaps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """All outer recovery loops use the run's existing costs and stop gates."""
    from AstralNexus.core.recovery_control import gap_identity
    stopped = self._lineage_stand_down_verdict()
    if stopped or self._recovery_budget_exhausted:
        return stopped or {"stop_code": "no_progress_breaker", "stop_reason": "run recovery budget exhausted"}
    root = "__" + origin + "__"
    self._consume_recovery_churn(origin, lineage_root=root)
    self.runtime_state_manager.record_lineage_progress(
        root, [], gap_fingerprint=gap_identity(gaps), counted_as_recovery=True,
    )
    stopped = self._lineage_stand_down_verdict()
    if stopped or self._recovery_budget_exhausted:
        return stopped or {"stop_code": "no_progress_breaker", "stop_reason": "run recovery budget exhausted"}
    return None


def _lineage_root_for(self: Orchestrator, node_id: str) -> str:
    """该节点所属**谱系根**（``recovery_of`` 回溯）；空 node_id 取当前活跃节点。

    成本/进展都按这个根记账，所以接管换节点不会另起一本账。
    """
    target = str(node_id or "")
    if target.startswith("__") and target.endswith("__"):
        return target
    node = self.task_graph.get_node(target) if target else None
    if node is None:
        for candidate in self.task_graph.nodes.values():
            if str(getattr(candidate, "status", "")) in {
                "running", "verifying", "result_submitted", "repair_pending",
            }:
                node = candidate
                break
    if node is None:
        return target
    try:
        return str(self._lineage_root_node_id(node) or node.node_id)
    except Exception:
        return str(getattr(node, "node_id", target) or target)


def _bind_requirement_observations(self: Orchestrator, observations):
    from AstralNexus.runtime.requirement_identity import frozen_bindings, bind_observations
    contract = getattr(getattr(self, "governor", None), "delivery_contract", {}) or {}
    return bind_observations(observations, frozen_bindings(contract), strict=bool(contract))


def _recovery_limits(self: Orchestrator):
    from AstralNexus.core.recovery_limits import RecoveryLimits
    return RecoveryLimits.from_environment(int(self.recovery_churn_budget))


def _lineage_stand_down_verdict(self: Orchestrator) -> Optional[Dict[str, Any]]:
    from .models import StandDownVerdict
    payload = _compute_stand_down_verdict(self)
    return StandDownVerdict.from_payload(payload).as_dict() if payload else None


def _compute_stand_down_verdict(self: Orchestrator) -> Optional[Dict[str, Any]]:
    """谱系级熔断的**两种**判断（互不冒充），返回 None 表示不停机。

    * ``no_effective_progress``：同一验收缺口反复未改善 ⇒ 停机，并给出**具体阻塞项**；
    * ``cost_exhausted``：成本触顶但**进展是真的** ⇒ 诚实标记"成本预算耗尽"，
      保留检查点与已成立证据——**不得**说成死循环，也不悄悄续跑。
    """
    ledger = getattr(self.runtime_state_manager.state, "lineage_accounting", None) or {}
    if not ledger:
        return None
    limits = self._recovery_limits()
    budget = limits.same_gap_operations
    for root in sorted(ledger):
        verdict = self.runtime_state_manager.lineage_breaker(root, budget=budget,
            recovery_budget=limits.total_operations)
        state = str(verdict.get("state") or "")
        cost = dict(verdict.get("cost") or {})
        common = {
            "lineage_root": root,
            "state": state,
            "budget": budget,
            "cost": cost,
        }
        if state == "no_effective_progress":
            gap = str(verdict.get("blocking_gap") or "")
            return {
                **common,
                "stop_code": "no_effective_progress_breaker",
                "stop_reason": (
                    "autonomous recovery made no effective progress: the same "
                    f"acceptance gap went unchanged across {verdict.get('gap_recoveries')} "
                    f"recoveries for lineage {root} (budget={budget}); "
                    f"blocking_gap={gap or '<unrecorded>'}; "
                    f"cost so far: requests={cost.get('requests', 0)} "
                    f"tokens={cost.get('tokens', 0)} "
                    f"tool_calls={cost.get('tool_calls', 0)} "
                    f"recoveries={cost.get('recoveries', 0)}"
                ),
            }
        if state == "cost_exhausted":
            satisfied = list(cost.get("satisfied_items") or [])
            return {
                **common,
                "budget": limits.total_operations,
                "stop_code": "cost_budget_exhausted",
                "stop_reason": (
                    "autonomous recovery cost budget exhausted WITH progress for "
                    f"lineage {root} (budget={limits.total_operations}); this is NOT a no-progress "
                    "dead loop — established evidence and the checkpoint are kept: "
                    f"satisfied={satisfied}; "
                    f"requests={cost.get('requests', 0)} tokens={cost.get('tokens', 0)} "
                    f"tool_calls={cost.get('tool_calls', 0)} "
                    f"recoveries={cost.get('recoveries', 0)}"
                ),
            }
    from AstralNexus.core.recovery_control import recovery_totals
    totals = recovery_totals(ledger)
    exceeded = (
        "recoveries" if totals["recoveries"] >= limits.total_operations else
        "tokens" if limits.tokens and totals["tokens"] >= limits.tokens else
        "requests" if limits.requests and totals["requests"] >= limits.requests else ""
    )
    if exceeded:
        return {
            "scope": "run",
            "lineage_root": "",
            "state": "cost_exhausted",
            "budget": {"recoveries": limits.total_operations, "tokens": limits.tokens, "requests": limits.requests}[exceeded],
            "stop_code": "cost_budget_exhausted",
            "stop_reason": "run-wide cost budget exhausted: " + exceeded,
            "budget_dimension": exceeded,
            "cost": totals,
        }
    return None


@synchronized_state
def _record_lineage_event_cost(
    self: Orchestrator, node_id: str, event_type: str, payload: Dict[str, Any]
) -> None:
    """把 LLM 请求数 / Token / 工具调用数按**谱系根**记账。

    观测面而非闸门：任何失败都不改变运行行为（停机判据读的是账本，
    坏账不能把一次运行拦下来）。
    """
    from AstralNexus.core.recovery_control import event_cost
    cost = event_cost(event_type, payload)
    root = self._lineage_root_for(node_id)
    stream_usage = getattr(self, "_lineage_stream_usage", {})
    stream_key = (root, str(payload.get("request_id") or node_id))
    if event_type == "llm.stream.started":
        stream_usage[stream_key] = 0
    elif event_type == "llm.usage":
        observed = cost["tokens"]
        cost["tokens"] = max(0, observed - int(stream_usage.get(stream_key, 0)))
        stream_usage[stream_key] = max(observed, int(stream_usage.get(stream_key, 0)))
    if event_type in {"llm.stream.completed", "llm.stream.failed"}:
        stream_usage.pop(stream_key, None)
    while len(stream_usage) > 256:
        stream_usage.pop(next(iter(stream_usage)))
    self._lineage_stream_usage = stream_usage
    if not any(cost.values()):
        return
    try:
        self.runtime_state_manager.record_lineage_cost(
            root, persist=False, **cost,
        )
    except Exception:
        # 记账失败不得改变运行行为。
        pass


def _find_downstream_nodes(self: Orchestrator, node_id: str) -> List[str]:
    """Find all nodes that depend on the given node.

    Traverses task_graph edges to find nodes where this node is a dependency.

    Args:
        node_id: The source node ID

    Returns:
        List of downstream node IDs that depend on this node
    """
    if not self.task_graph:
        return []

    downstream = []
    for edge in self.task_graph.edges:
        # Find edges where node_id is the source (dependency)
        if edge.source == node_id:
            downstream.append(edge.target)

    return downstream


def _supersede_stale_recovery_children(self: Orchestrator, parent_node_id: str) -> list[str]:
    """Close exception-recovery nodes made obsolete by parent success.

    Failure recovery is asynchronous, so a recovery node can remain
    admitted while the original node succeeds on a later retry.  Leaving
    that node runnable creates a false second execution and can reopen a
    resolved failure.  Only exception-triggered dynamic nodes are
    superseded; verification repairs and ordinary follow-up work remain
    governed by their own contracts.
    """
    superseded: list[str] = []
    for candidate in self.task_graph.nodes.values():
        metadata = candidate.metadata or {}
        if (
            metadata.get("type") != "dynamic"
            or metadata.get("trigger") != "exception_fault"
            or metadata.get("parent_node_id") != parent_node_id
            or candidate.status not in {
                "proposed", "admitted", "waiting_dependencies", "ready",
                "repair_pending", "blocked",
            }
        ):
            continue
        try:
            self.task_graph.update_node_status(candidate.node_id, "superseded")
        except ValueError:
            continue
        metadata["superseded_reason"] = "parent_completed_before_recovery_execution"
        superseded.append(candidate.node_id)
        self.trace_recorder.record("recovery.stale_child_superseded", {
            "parent_node_id": parent_node_id,
            "node_id": candidate.node_id,
            "reason": metadata["superseded_reason"],
        })
    return superseded


@recovery_operation
def _replan_stalled_graph(
    self: Orchestrator,
    *,
    goal: str,
    blocked_nodes: List[str],
    reason: str,
) -> Dict[str, Any]:
    """Recover a stalled graph through the sole Governor mutation path."""
    llm_client = getattr(self.governor, "llm_client", None)
    if (
        not getattr(self.governor, "llm_matching_enabled", False)
        or llm_client is None
    ):
        fingerprint = json.dumps(
            {
                "reason": reason,
                "blocked_nodes": sorted(blocked_nodes),
                "mode": "no_llm",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        repeats = self.runtime_state_manager.record_recovery(
            fingerprint, changed=False
        )
        return {
            "changed": False,
            "repeat_count": repeats,
            "reason": "real_llm_governor_unavailable",
        }

    failed_dependencies = sorted(
        {
            dependency_id
            for node_id in blocked_nodes
            if (blocked_node := self.task_graph.get_node(node_id)) is not None
            for dependency_id in blocked_node.dependencies
            if (
                (dependency := self.task_graph.get_node(dependency_id)) is not None
                and (
                    dependency.status
                    in {"failed", "blocked", "repair_pending", "cancelled"}
                    or dependency.metadata.get(
                        "_terminal_failure"
                    )
                )
            )
        }
    )
    # Terminal-dependency dossier: status, terminal reason, the failed
    # worker's own root-cause proposals, and what its lineage already
    # tried (Fix B history).  Without this the planner re-proposed the
    # same exhausted workarounds or stalled (r11 N-006 family).
    dependency_dossier: List[Dict[str, Any]] = []
    for dependency_id in failed_dependencies:
        dependency = self.task_graph.get_node(dependency_id)
        if dependency is None:
            continue
        entry: Dict[str, Any] = {
            "node_id": dependency_id,
            "status": dependency.status,
            "terminal_reason": str(
                dependency.metadata.get("_terminal_reason") or ""
            )[:300],
            "retry_count": int(getattr(dependency, "retry_count", 0) or 0),
        }
        packet = dependency.metadata.get("evidence_packet") or {}
        proposals = [
            {
                "goal": str(
                    action.get("goal") or action.get("target") or ""
                )[:200],
                "reason": str(action.get("reason") or "")[:200],
            }
            for action in (packet.get("next_actions") or [])[:4]
            if isinstance(action, dict)
        ]
        if proposals:
            entry["worker_root_cause_proposals"] = proposals
        lineage_root_id = self._lineage_root_node_id(dependency)
        lineage_root = self.task_graph.get_node(lineage_root_id)
        history = (
            lineage_root.metadata.get("_lineage_recovery_history")
            if lineage_root is not None
            else None
        )
        if isinstance(history, list) and history:
            entry["lineage_root"] = lineage_root_id
            entry["already_tried"] = [
                {
                    "attempt_by": str(item.get("node_id")),
                    "added": [str(x) for x in (item.get("added") or [])],
                }
                for item in history[-6:]
                if isinstance(item, dict)
            ]
        # Round 8: the verifier's blocking gaps are the CURRENT truth
        # about what the lineage still lacks; stale ancestral root-cause
        # proposals alone misled three identical stalled replans (live
        # r11 run-blocked).
        verification_view = self._latest_verification_view(dependency)
        if verification_view is not None:
            entry["latest_verification"] = {
                "verdict": str(getattr(verification_view, "verdict", "") or ""),
                "overall_score": getattr(
                    verification_view, "overall_score", None
                ),
                "failed_dimensions": [
                    {
                        "name": str(
                            dimension.get("name")
                            if isinstance(dimension, dict)
                            else getattr(dimension, "name", "")
                        ),
                        "details": self._clip_head_tail(
                            dimension.get("details")
                            if isinstance(dimension, dict)
                            else getattr(dimension, "details", ""),
                            600, 250,
                        ),
                    }
                    for dimension in (
                        getattr(verification_view, "dimensions", []) or []
                    )
                    if (
                        not dimension.get("passed", False)
                        if isinstance(dimension, dict)
                        else not getattr(dimension, "passed", True)
                    )
                ],
            }
        dependency_dossier.append(entry)
    recovery_goal = (
        f"{goal}\n\n任务图无可执行 Ready 节点，但任务尚未完成。"
        f"\n阻塞原因：{reason}"
        f"\n阻塞节点：{json.dumps(blocked_nodes, ensure_ascii=False)}"
        f"\n失败或失效依赖：{json.dumps(failed_dependencies, ensure_ascii=False)}"
        "\n请生成最小增量 TaskGraphPatch 恢复进展。优先复用已有产物与事实；"
        "可以替换失效 Worker、补充能力、增加恢复节点、更新依赖或取消被替代节点。"
        "不得原样重试同一失败策略，不得删除用户约束，不得把未完成任务标记为完成。"
        "若新增节点接管已有失败或 repair_pending 节点，必须填写 recovery_of=原节点ID，"
        "并沿用或明确迁移其 input_artifacts、output_artifacts、owned_paths 和 handoff_contract；"
        "没有 recovery_of 时不得重复占用已有节点的产物路径。"
    )
    if dependency_dossier:
        recovery_goal += (
            "\n终态失败依赖档案（状态/终态原因/失败 Worker 的根因提案/谱系已试方案）："
            + json.dumps(dependency_dossier, ensure_ascii=False)
            + "\n处置规则：终态失败标记与谱系历史不得清除、回滚或伪装成功；"
            "接替生产节点必须 recovery_of=终态节点ID 并接管其产物路径，"
            "然后用 update_nodes 把受阻消费者的 dependencies 改接到接替节点，"
            "使报告聚合与交付门禁重新可达。"
            "若档案中的根因提案指向上游已完成节点拥有的文件，必须先派生持有这些文件的"
            "上游修复节点（已完成所有者不活跃，新节点可合法持有其路径），并让接替节点依赖该修复节点；"
            "不得重复谱系 already_tried 中失败的同一方案。"
        )
    previous_error = self.runtime_state_manager.state.recovery_error
    if previous_error:
        recovery_goal += (
            "\n上次恢复 Patch 被拒绝，以下是校验错误（诊断数据）：\n"
            + json.dumps(previous_error, ensure_ascii=False)
            + "\n请根据该错误修正增量 Patch，不得重复提交被拒绝的方案。"
        )
    # Round 8 — concrete lineage-cap exemption recipe.  The cap's
    # teaching text tells the planner to "route the declared root cause
    # to an upstream repair node owning the offending files", but live
    # r11 proved the planner cannot guess WHICH declarations make a
    # patch exempt: three consecutive proposals described init_db.py
    # repairs in goal text while declaring no owned_paths, so routing
    # enforcement saw nothing, the cap rejected again, and the run
    # blocked on identical outcomes.  Name the files, their inactive
    # owners and the required declaration explicitly.
    exemption_recipe: List[Dict[str, Any]] = []
    stale_goal_warnings: List[str] = []
    for dependency_id in failed_dependencies:
        dependency = self.task_graph.get_node(dependency_id)
        if dependency is None or not dependency.metadata.get("_terminal_failure"):
            continue
        verification_view = self._latest_verification_view(dependency)
        # include_self_owned (round 8): the live terminal node usually
        # owns the offending files itself; the recipe must still name
        # them so the planner can declare the ownership that exempts a
        # takeover patch from the depth cap.
        resolved = self._resolve_declared_root_causes(
            dependency,
            verification_view,
            self._worker_report_for_node(dependency),
            include_self_owned=True,
        )
        if resolved:
            exemption_recipe.append({
                "terminal_node": dependency_id,
                "root_cause_files": {
                    token: {"inactive_owner": owner, "paths": paths}
                    for token, (owner, paths) in sorted(resolved.items())
                },
            })
        # Round 8 fix 5: resume #13's planner aimed all three proposals
        # at a generations-old goal tail ("embed INSERT OR REPLACE into
        # init_db.py") that the verifier had already confirmed COMPLETE,
        # while the real blocking gaps (no live /health receipt, no RBAC
        # runtime evidence) rode ignored in the dossier.  Correct the
        # stale-goal dominance explicitly.
        if verification_view is not None:
            gap_texts: List[str] = []
            for dimension in (
                getattr(verification_view, "dimensions", []) or []
            ):
                if isinstance(dimension, dict):
                    if dimension.get("passed", False):
                        continue
                    name = str(dimension.get("name") or "")
                    gap_details = str(dimension.get("details") or "")
                else:
                    if getattr(dimension, "passed", True):
                        continue
                    name = str(getattr(dimension, "name", "") or "")
                    gap_details = str(getattr(dimension, "details", "") or "")
                gap_texts.append(f"{name}: {gap_details[:300]}")
            if gap_texts:
                stale_goal_warnings.append(
                    f"- {dependency_id}（verdict="
                    f"{getattr(verification_view, 'verdict', '')}）："
                    + "；".join(gap_texts)
                )
    if exemption_recipe:
        recovery_goal += (
            "\n谱系 cap 豁免配方（必须遵守，否则补丁会被再次拦截）："
            + json.dumps(exemption_recipe, ensure_ascii=False)
            + "\n上述 root_cause_files 的所有者均为非活跃节点"
            "（completed/superseded/failed），新修复节点可以合法持有这些路径。"
            "新增修复节点必须在 owned_paths（或 expected_outputs）中"
            "逐字声明这些文件的完整相对路径——仅在 goal 文本中描述修复"
            "不构成声明，会再次被谱系 cap 拒绝；并把受阻消费者的 "
            "dependencies 改接到该修复节点。修复节点的 goal 必须针对"
            "档案中 latest_verification.failed_dimensions 列出的实测"
            "证据缺口，不得重复 already_tried 中已失败的方案。"
        )
    if stale_goal_warnings:
        recovery_goal += (
            "\n重要纠偏：下列终态依赖的 goal 文本形成于数代之前，其中"
            "描述的修复目标可能早已完成并被验证器确认（例如数据库嵌入"
            "逻辑）。当前唯一真实缺口以各节点 latest_verification 的"
            "失败维度为准：\n"
            + "\n".join(stale_goal_warnings)
            + "\n新增修复/验证节点的 goal 必须直指上述实测证据缺口"
            "（启动服务、捕获真实 HTTP 回执、运行时权限拒绝证据等），"
            "不得重新实现已完成的修复。"
        )
    before = self.runtime_state_manager.fingerprint(
        [
            [
                (node.node_id, node.status, tuple(node.dependencies))
                for node in self.task_graph.nodes.values()
            ],
            [
                (edge.source, edge.target, edge.edge_type)
                for edge in self.task_graph.edges
            ],
        ]
    )
    from AstralNexus.core.governor import TaskGraphPatch

    # Fix 23b: typed empty default so the except-path fingerprint
    # sanitizer can read proposed IDs even when planning itself raised.
    patch = TaskGraphPatch()
    try:
        graph_summary, capability_catalog, resource_catalog = (
            self._build_governor_planning_context()
        )
        patch = self.governor.plan_task_graph_patch(
            recovery_goal,
            graph_summary=graph_summary,
            capability_catalog=capability_catalog,
            resource_catalog=resource_catalog,
            cancel_check=self._cancel_requested,
        )
        patch = self._normalize_recovery_ownership_transfer(patch)
        self._strip_unregistered_artifact_refs(patch)
        self._normalize_recovery_targets(patch)
        self._ensure_artifact_producer_deps(patch)
        # The stalled-graph path shares the root-cause routing and
        # lineage-dedup guards with verification recovery, keyed on the
        # primary terminal-failed dependency.  A rejected patch flows
        # into runtime_state.recovery_error and is fed back verbatim to
        # the next planner invocation (self-heal loop).
        primary = None
        for dependency_id in failed_dependencies:
            candidate = self.task_graph.get_node(dependency_id)
            if candidate is not None and candidate.metadata.get("_terminal_failure"):
                primary = candidate
                break
        if primary is None and failed_dependencies:
            primary = self.task_graph.get_node(failed_dependencies[0])
        stalled_fingerprint = ""
        if primary is not None:
            primary_report = self._worker_report_for_node(primary)
            # Round 8: route on the verifier's actual evidence truth.
            # The stalled path used to pass None here, so root-cause
            # resolution saw only the (often absent or stale) worker
            # report while the verifier's blocking-gap details — the
            # current truth about what is missing — never influenced
            # routing or the lineage fingerprint.
            primary_verification = self._latest_verification_view(primary)
            stalled_root_causes = self._resolve_declared_root_causes(
                primary, primary_verification, primary_report
            )
            # doc 18 修复E：与验证恢复路径同一规范化（停滞路径的
            # primary 是 failed 依赖，可安全作为重定向目标）。
            self._normalize_root_cause_takeover(
                primary, patch, stalled_root_causes
            )
            addresses_upstream = self._enforce_root_cause_routing(
                primary, patch, stalled_root_causes
            )
            # Round 8: self-owned offender files count for the depth-cap
            # exemption (ownership-declaring takeover = genuine change of
            # approach) even though they never count as UPSTREAM routing.
            declares_offender = self._patch_covers_root_causes(
                patch,
                self._resolve_declared_root_causes(
                    primary, primary_verification, primary_report,
                    include_self_owned=True,
                ),
            )
            stalled_fingerprint = self._lineage_recovery_fingerprint(
                primary, patch, primary_verification, primary_report,
                stalled_root_causes,
            )
            self._check_lineage_recovery_repeat(
                primary,
                stalled_fingerprint,
                addresses_upstream=addresses_upstream,
                declares_offender_ownership=declares_offender,
                derives_lineage=self._patch_derives_from_lineage(primary, patch),
            )
        intent_node = next(
            (
                node for node in self.task_graph.nodes.values()
                if node.role == "user_intent"
            ),
            None,
        )
        applied = self.governor.apply_task_graph_patch(
            patch,
            parent_task_id=(intent_node.node_id if intent_node else None),
        )
        if primary is not None and stalled_fingerprint:
            self._record_lineage_recovery_attempt(
                primary, patch, stalled_fingerprint, applied
            )
        # doc 18 修复C：替换提交内原子改写下游依赖死边；改写发生在
        # 指纹计算之前，结构变更如实计入 changed 判定。
        self._rewire_superseded_dependents()
        after = self.runtime_state_manager.fingerprint(
            [
                [
                    (node.node_id, node.status, tuple(node.dependencies))
                    for node in self.task_graph.nodes.values()
                ],
                [
                    (edge.source, edge.target, edge.edge_type)
                    for edge in self.task_graph.edges
                ],
            ]
        )
        changed = before != after
        recovery_fingerprint = json.dumps(
            {
                "reason": reason,
                "blocked_nodes": sorted(blocked_nodes),
                "failed_dependencies": failed_dependencies,
                "reason_codes": sorted(patch.reason_codes),
                "changed": changed,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        repeats = self.runtime_state_manager.record_recovery(
            recovery_fingerprint, changed=changed
        )
        # doc 18 / live b31 (F12): a replan that changed the graph, is not a
        # repeat of the same premise, and left an executable ready frontier
        # behind is structural progress.  It still enters the monotone
        # ledger, but it must not stand the run down by itself.
        replan_productive = (
            changed
            and repeats == 0
            and bool(self._executable_ready_frontier())
        )
        # doc 18 churn 预算：停滞重规划波次（无论是否产生变化）计入
        # run 级单调预算——重规划重新 rooting 谱系不能清零全局计数。
        self._consume_recovery_churn(
            "stalled_replan_changed" if changed else "stalled_replan_unchanged",
            structural_changed=replan_productive,
        )
        result = {
            "changed": changed,
            "repeat_count": repeats,
            "reason": "governor_incremental_replan",
            "patch": applied,
            "failed_dependencies": failed_dependencies,
        }
        self.trace_recorder.record(
            "orchestrator.autonomous_recovery.decided", result
        )
        if changed:
            self._persist_graph_checkpoint(stage="autonomous_replan")
        return result
    except InterruptionRequested:
        return {
            "changed": False,
            "interrupted": True,
            "reason": "cooperative_interrupt_during_replan",
        }
    except Exception as exc:
        # Fix 23b (live r12: 23 planning rounds without progress): the
        # same-outcome breaker never tripped because the planner rotated
        # the cosmetic proposed node IDs (A-001-R1/R2/R3/...), and the
        # rejection text embeds those IDs — every retry looked like a
        # DIFFERENT outcome. Fingerprints must be invariant to proposed
        # IDs: strip them from the error text before hashing while the
        # raw error is still surfaced verbatim for planner feedback.
        fingerprint_error = str(exc)
        try:
            proposed_ids = [
                str(item.get("node_id") or item.get("task_id") or "").strip()
                for item in (getattr(patch, "add_nodes", None) or [])
                if isinstance(item, dict)
            ]
        except Exception:  # pragma: no cover - defensive only
            proposed_ids = []
        for proposed_id in sorted(set(proposed_ids), key=len, reverse=True):
            if proposed_id:
                fingerprint_error = fingerprint_error.replace(
                    proposed_id, "<proposed-node>"
                )
        recovery_fingerprint = json.dumps(
            {
                "reason": reason,
                "blocked_nodes": sorted(blocked_nodes),
                "error_type": type(exc).__name__,
                "error": fingerprint_error,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        repeats = self.runtime_state_manager.record_recovery(
            recovery_fingerprint, changed=False, error=str(exc)
        )
        self._consume_recovery_churn("stalled_replan_failed")
        result = {
            "changed": False,
            "repeat_count": repeats,
            "reason": "governor_replan_failed",
            "error": str(exc),
        }
        self.trace_recorder.record(
            "orchestrator.autonomous_recovery.failed", result
        )
        return result


def _check_for_blocked_tasks(self: Orchestrator) -> List[str]:
    """Check if there are blocked tasks that cannot proceed.

    A task is blocked when:
    1. Its status is explicitly "blocked" (state machine), OR
    2. It is admitted/waiting and at least one dependency is in a
       terminal-failure state (capability_gap_exhausted, recovery_exhausted).
       These nodes can never become ready — the entire dependency chain is dead.
    3. Dependencies are satisfied but an accepted input artifact is missing.

    Returns:
        List of blocked node IDs, or empty list if none
    """
    blocked = []
    for node_id, node in self.task_graph.nodes.items():
        if node.status == "blocked":
            # Fix 24 (live r12): blocked is recoverable, not terminal.
            # A node blocked on a permanently dead dependency (marker
            # _blocked_by_terminal_dep) is re-evaluated every wave: once
            # a takeover supersedes the dead dependency and the consumer
            # rewiring lands, all of the node's dependencies are alive
            # or done again — revive it to waiting_dependencies so the
            # normal readiness promotion re-dispatches it instead of
            # forcing a redundant recovery generation on an already
            # repaired blocker (r12: I-001 blocked on failed A-001,
            # rewired to takeover A-001-R4).
            revived = False
            if node.metadata.get("_blocked_by_terminal_dep"):
                revived = True
                for dep_id in node.dependencies:
                    dep = self.task_graph.get_node(dep_id)
                    if dep is None or dep.status not in {
                        "completed", "admitted", "waiting_dependencies",
                        "ready", "running", "result_submitted",
                        "verifying", "repair_pending",
                    }:
                        revived = False
                        break
                if revived:
                    try:
                        self.task_graph.update_node_status(
                            node_id, "waiting_dependencies"
                        )
                    except ValueError as transition_error:
                        revived = False
                        self.trace_recorder.record(
                            "orchestrator.node_unblock_failed",
                            {"node_id": node_id, "error": str(transition_error)},
                        )
                if revived:
                    node.metadata.pop("_blocked_by_terminal_dep", None)
                    self.trace_recorder.record(
                        "orchestrator.node_unblocked_by_dependency_recovery",
                        {"node_id": node_id, "dependencies": list(node.dependencies)},
                    )
            if not revived:
                blocked.append(node_id)
        # Detect admitted/waiting nodes permanently blocked by terminal deps
        elif node.status in ("waiting_dependencies", "admitted"):
            blockers = self.task_graph.readiness_blockers(node)
            if (
                not blockers["unsatisfied_dependencies"]
                and blockers["missing_input_artifacts"]
            ):
                self.trace_recorder.record(
                    "orchestrator.node_blocked_by_artifact",
                    {"node_id": node_id, **blockers},
                )
                blocked.append(node_id)
                continue
            for dep_id in node.dependencies:
                dep = self.task_graph.get_node(dep_id)
                # Fix 20 (live r11 resume #31 blocked 20:24): fix 19
                # normalized the report-regeneration takeover into
                # recovery_of=G-001-GENERATE-TEST-REPORT, but the patch
                # validator requires a recovery target to be failed/
                # repair_pending/blocked — and the zombie was still
                # ``admitted`` because its dead deps (superseded with no
                # living successor, cancelled) fell outside the old
                # terminal-failure-only blocking rule.  A node whose
                # dependency can never be satisfied again must persist as
                # blocked so recovery patches may legally target it
                # (takeover or rewire); left in admitted it invalidates
                # every proposal and deadlocks the run (r11 N-006 rule).
                if dep is None:
                    dead_reason = "missing_dependency"
                elif dep.metadata.get("_terminal_failure"):
                    dead_reason = str(
                        dep.metadata.get("_terminal_reason", "unknown")
                    )
                elif self._dependency_permanently_dead(dep):
                    dead_reason = f"dependency_{dep.status}_never_satisfiable"
                else:
                    continue
                # Fix 18 (live r11 resume #29 blocked terminal):
                # a recovery/takeover node declares its terminal-
                # failed predecessor via ``recovery_of`` and often
                # keeps it as a dependency.  That predecessor's
                # terminal failure is the recovery node's REASON TO
                # EXIST, not an impossible dependency — blocking the
                # recovery on it stillbirths the only legitimate
                # repair route and deadlocks the run (live case:
                # G-001-REPAIR-ROLLBACK-PROOF-JSON blocked by the
                # very REPAIR-V4 node it recovers, run went blocked
                # with a healthy dispatchable repair in the graph).
                recovery_target = str(
                    node.metadata.get("recovery_of")
                    or node.metadata.get("supersedes_node_id")
                    or ""
                ).strip()
                if recovery_target == dep_id:
                    continue
                node.metadata["_blocked_by_terminal_dep"] = dep_id
                # Persist the blocked lifecycle status, not only the
                # metadata marker: Governor recovery patches may only
                # target failed/repair_pending/blocked nodes, so a
                # permanently blocked node left in "admitted" makes
                # every recovery proposal invalid and deadlocks the
                # run (r11 N-006).
                try:
                    self.task_graph.update_node_status(node_id, "blocked")
                except ValueError as transition_error:
                    self.trace_recorder.record("orchestrator.node_block_persist_failed", {
                        "node_id": node_id,
                        "error": str(transition_error),
                    })
                self.trace_recorder.record("orchestrator.node_blocked_by_terminal", {
                    "node_id": node_id,
                    "blocked_by": dep_id,
                    "terminal_reason": dead_reason,
                })
                blocked.append(node_id)
                break
    return blocked


def _call_with_circuit_wait(
    self: Orchestrator, call: Callable[[], Any], *, description: str, max_waits: int = 2,
) -> Any:
    """doc 18 修复A：恢复关键 LLM 调用的熔断感知有界等待。

    attempt3 两次孤儿失败的共同入口：worker 被网络风暴击杀 → 节点
    failed → 同秒触发的恢复规划调用撞上同一风暴打开的提供方熔断 →
    LLMClientError 立即冒泡 → host 停机，恢复半途而废。熔断冷却仅
    15-30s，恢复却一次都不等。有界等待短冷却（每次解析 retry_after，
    仅 ≤60s 才等；最多 max_waits 次），穷尽后原样冒泡——blocked+
    host_exception 语义不变，但孤儿不再于源头生成。
    """
    from AstralNexus.runtime.llm_client import LLMClientError

    waits = 0
    while True:
        try:
            return call()
        except LLMClientError as exc:
            message = str(exc)
            if waits >= max_waits or "circuit open" not in message:
                raise
            match = re.search(r"retry in (\d+(?:\.\d+)?)s", message)
            wait_seconds = float(match.group(1)) if match else 0.0
            if wait_seconds <= 0.0 or wait_seconds > 60.0:
                raise
            waits += 1
            self.trace_recorder.record("orchestrator.circuit_wait", {
                "description": description,
                "wait": waits,
                "max_waits": max_waits,
                "wait_seconds": round(wait_seconds + 2.0, 2),
            })
            time.sleep(wait_seconds + 2.0)


def _recover_orphaned_failures(self: Orchestrator) -> bool:
    """Re-drive governed recovery for failed nodes no wave will revisit.

    doc 18 attempt3 修复：worker 失败发生在宿主异常拆机瞬间时，恢复
    通道被打断，失败节点残留在图里。它非终态（可修）、无在世修复
    后继，依赖它的节点永不就绪，而重规划提案又以它为非法目标——
    唯一合法出路就是把它重新送入与新失败完全相同的受治理恢复通道
    （同一验证器、同一谱系规则、同一 churn 预算），不另设机制。
    """
    recovered = False
    for node in list(self.task_graph.nodes.values()):
        if node.status != "failed":
            continue
        metadata = node.metadata or {}
        if metadata.get("_terminal_failure") or metadata.get(
            "_capability_gap_exhausted"
        ) or metadata.get("_recovery_patch_rejected"):
            continue  # 终态/恢复已穷尽：不重复驱动
        has_living_successor = any(
            other.node_id != node.node_id
            and other.status not in ("failed", "cancelled", "superseded")
            and str(
                (other.metadata or {}).get("recovery_of")
                or (other.metadata or {}).get("supersedes_node_id")
                or ""
            ) == node.node_id
            for other in self.task_graph.nodes.values()
        )
        if has_living_successor:
            continue
        self.trace_recorder.record("orchestrator.orphaned_failure.recovery", {
            "node_id": node.node_id,
        })
        try:
            if self._attempt_recovery(node.node_id):
                recovered = True
        except Exception as exc:
            # 宿主级 LLM/提供方异常（如熔断打开）照常向上冒泡，走
            # blocked+host_exception 可恢复语义；节点级恢复失败留在
            # 本层记录，继续驱动其余孤儿失败。
            from AstralNexus.runtime.llm_client import LLMClientError

            if isinstance(exc, LLMClientError):
                raise
            self.trace_recorder.record(
                "orchestrator.orphaned_failure.recovery_failed",
                {"node_id": node.node_id, "error": str(exc)[:500]},
            )
    return recovered


def _execute_governed_fault(
    self: Orchestrator,
    fault: Fault,
    impact: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Route one diagnosed fault through Governor and RecoveryExecutor."""
    impact = impact or self.impact_analyzer.analyze_impact(fault)
    try:
        # doc 18 修复A：熔断短冷却有界等待——恢复规划不再因同一场
        # 网络风暴立即放弃（attempt3 两次孤儿的生成入口）。
        plan = self._call_with_circuit_wait(
            lambda: self.governor.plan_fault_recovery(
                fault={
                    "fault_id": fault.fault_id,
                    "fault_type": fault.fault_type,
                    "target": fault.target,
                    "payload": _json_safe(fault.payload),
                    "status": fault.status,
                    "timestamp": fault.timestamp,
                },
                impact=_json_safe(impact),
            ),
            description="plan_fault_recovery",
        )
    except (OSError, LLMClientError) as exc:
        # Fix AC (doc 18, 2026-09-15 r13b): the ValueError handler below
        # states the intent — "do not crash the whole run" — but it only
        # caught malformed JSON.  A transport failure while ASKING Governor
        # to plan escaped it and took the orchestrator down with it: live
        # generations 8 and 9 both ended as host_exception after four
        # consecutive "SSL: UNEXPECTED_EOF_WHILE_READING" on the recovery
        # planning call, losing a healthy run (8 nodes completed) to a
        # transient provider drop.  OSError covers URLError, ssl.SSLError,
        # socket.timeout and ConnectionError; the non-streaming chat()
        # path wraps them into LLMClientError ("Network error from LLM
        # API after ..."), so both classes are needed here.
        #
        # This path deliberately does NOT consume recovery churn: the churn
        # budget bounds STRUCTURAL recovery/replan loops, and an unreachable
        # planner is infrastructure, not a loop.  Charging it there is what
        # exhausted the budget earlier in this same run.
        self.trace_recorder.record(
            "governor.fault_recovery.planner_unavailable",
            {
                "fault_id": fault.fault_id,
                "fault_type": fault.fault_type,
                "error": str(exc)[:500],
                "error_type": type(exc).__name__,
                "action": "defer_recovery_to_next_observation",
                "churn_consumed": False,
            },
        )
        return {
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "impact": impact,
            "plan": {"strategy": "planner_unavailable", "actions": []},
            "execution": {"failure_count": 1, "failures": [str(exc)]},
            "success": False,
        }
    except ValueError as exc:
        # Invalid LLM recovery JSON is a recoverable control-plane fault.
        # Do not silently execute a guessed action and do not crash the
        # whole run; the autonomous cycle can record the failure and ask
        # Governor to replan on the next observation.
        self._consume_recovery_churn("governed_fault_invalid_proposal")
        self.trace_recorder.record(
            "governor.fault_recovery.invalid_proposal",
            {
                "fault_id": fault.fault_id,
                "fault_type": fault.fault_type,
                "error": str(exc)[:500],
                "action": "repair_pending_and_replan",
            },
        )
        return {
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "impact": impact,
            "plan": {"strategy": "invalid_proposal", "actions": []},
            "execution": {"failure_count": 1, "failures": [str(exc)]},
            "success": False,
        }
    if (
        not isinstance(plan, dict)
        or not str(plan.get("strategy") or "").strip()
        or not isinstance(plan.get("actions"), list)
        or not plan["actions"]
    ):
        raise ValueError(
            "Governor returned an invalid fault-recovery contract; "
            "strategy and at least one validated action are required"
        )
    execution = self.fault_recovery_engine.execute_governed_recovery(
        fault,
        plan["actions"],
    )
    # doc 18 修复C：受治理恢复若产生替换，下游依赖死边同场改写。
    self._rewire_superseded_dependents()
    # doc 18：受治理故障恢复执行也是结构性恢复事件——计 run 级预算
    self._consume_recovery_churn(
        "governed_fault_recovery_applied"
        if execution["failure_count"] == 0
        else "governed_fault_recovery_failed"
    )
    result = {
        "fault_id": fault.fault_id,
        "fault_type": fault.fault_type,
        "impact": impact,
        "plan": plan,
        "execution": execution,
        "success": execution["failure_count"] == 0,
    }
    self.trace_recorder.record(
        "orchestrator.governed_recovery.completed",
        {
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "strategy": plan["strategy"],
            "success_count": execution["success_count"],
            "failure_count": execution["failure_count"],
        },
    )
    return result


def _worker_report_for_node(self: Orchestrator, node) -> Dict[str, Any]:
    """Project the worker's own report out of persisted node metadata.

    Carries the summary, the structured ``next_actions`` (root-cause
    escalations) and the last runtime failure, so every recovery planner
    — verification recovery and stalled-graph replan alike — routes on
    the worker's findings instead of guessing.

    Cross-generation recovery state (user directive 2026-09-11): when
    this node's own packet carries no structured proposals, the nearest
    ancestor's original failure receipts are inherited, and the lineage
    root's already-tried recovery history rides along so the planner
    sees what previous generations attempted.
    """
    evidence_packet = node.metadata.get("evidence_packet") or {}
    report: Dict[str, Any] = {
        "summary": str(evidence_packet.get("summary") or "")[:800],
        "execution_outcome": str(evidence_packet.get("execution_outcome") or ""),
        "next_actions": list(evidence_packet.get("next_actions") or [])[:8],
    }
    verification_receipt = node.metadata.get("_verification_receipt") or {}
    if verification_receipt.get("observations"):
        report["verification_observations"] = verification_receipt["observations"]
    last_failure = node.metadata.get("last_failure")
    if isinstance(last_failure, dict):
        from AstralNexus.runtime.recovery_handoff import runtime_failure_is_historical
        failure_key = ("historical_execution_failure" if runtime_failure_is_historical(node.metadata)
                       else "last_failure")
        report[failure_key] = {
            "message": str(last_failure.get("message") or "")[:500],
            "error_type": str(last_failure.get("error_type") or ""),
        }
    # Merge-inherit across the WHOLE ancestor chain (live r11 V5 round:
    # nearest-ancestor-wins masked the deep init_db.py receipt behind
    # V5's own shallow sqlite3 complaint — the workaround vocabulary of
    # one generation must not hide the business root cause declared by
    # another).  Own actions keep priority; ancestor actions are appended
    # deduplicated by normalized goal/target text.
    inherited = self._inherit_lineage_next_actions(node)
    if inherited:
        existing_keys = {
            re.sub(r"\s+", " ", str(action.get("goal") or action.get("target") or "").strip().lower())[:120]
            for action in report["next_actions"]
            if isinstance(action, dict)
        }
        sources: list[str] = []
        for action, source_id in inherited:
            key = re.sub(r"\s+", " ", str(action.get("goal") or action.get("target") or "").strip().lower())[:120]
            if not key or key in existing_keys:
                continue
            existing_keys.add(key)
            report["next_actions"].append(action)
            sources.append(source_id)
        report["next_actions"] = report["next_actions"][:8]
        if sources:
            report["next_actions_inherited_from"] = ",".join(
                dict.fromkeys(sources)
            )[:160]
    root, history = self._lineage_history(node)
    if history:
        report["lineage_root"] = root.node_id
        report["lineage_already_tried"] = [
            {
                "attempt_by": str(entry.get("node_id") or ""),
                "added": [str(item) for item in (entry.get("added") or [])],
            }
            for entry in history[-6:]
        ]
    return report


def _inherit_lineage_next_actions(
    self: Orchestrator, node
) -> list[tuple[dict, str]] | None:
    """Recover the lineage's original failure receipts across generations.

    Generations that ran before the envelope-extraction fix persisted
    empty ``next_actions`` even though their workers had escalated the
    true root cause inside the raw response content.  Walk the WHOLE
    recovery_of / supersedes chain (≤16 hops, cycle-safe) and collect
    every ancestor's structured proposals — from its evidence packet
    first, else by re-parsing its execution outcome content with the
    fixed extractor — as ``(action, source_node_id)`` pairs, nearest
    ancestor first.  Recovery then routes on the ORIGINAL root-cause
    declarations instead of the workaround contract's own vocabulary.
    """
    collected: list[tuple[dict, str]] = []
    seen = {node.node_id}
    current = node
    hops = 0
    reparsed_bytes = 0
    started = time.monotonic()
    for _ in range(16):
        metadata = getattr(current, "metadata", None) or {}
        parent_id = str(
            metadata.get("recovery_of")
            or metadata.get("supersedes_node_id")
            or ""
        ).strip()
        if not parent_id or parent_id in seen:
            break
        parent = self.task_graph.get_node(parent_id)
        if parent is None:
            break
        seen.add(parent_id)
        packet = parent.metadata.get("evidence_packet") or {}
        actions = [
            dict(action)
            for action in (packet.get("next_actions") or [])
            if isinstance(action, dict)
            and (action.get("goal") or action.get("target"))
        ]
        if not actions:
            execution_id = str(parent.metadata.get("runtime_execution_id") or "")
            if execution_id:
                cache = getattr(self, "_lineage_envelope_cache", None)
                if cache is None:
                    cache = {}
                    self._lineage_envelope_cache = cache
                if execution_id in cache:
                    actions = [dict(a) for a in cache[execution_id]]
                else:
                    actions = []
                    try:
                        from AstralNexus.runtime.execution_store import ExecutionStore

                        execution = ExecutionStore(self.run_dir).get(execution_id) or {}
                        outcome = execution.get("outcome") or {}
                        content = str(outcome.get("content") or "")
                        reparsed_bytes += len(content)
                        # Envelopes are the TAIL of the worker narrative;
                        # cap the regex scan window.  Live r11 resume #9:
                        # unbounded re-parse of multi-MB transcripts burned
                        # 15+ silent CPU minutes between iterations while
                        # every stalled round re-paid the same cost.
                        window = content[-262144:] if len(content) > 262144 else content
                        envelope = _extract_response_envelope(window)
                        if isinstance(envelope, dict):
                            actions = [
                                dict(action)
                                for action in (envelope.get("next_actions") or [])
                                if isinstance(action, dict)
                                and (action.get("goal") or action.get("target"))
                            ]
                    except Exception:
                        actions = []
                    cache[execution_id] = [dict(a) for a in actions]
                    while len(cache) > 128:
                        cache.pop(next(iter(cache)))
        collected.extend((action, parent_id) for action in actions[:8])
        hops += 1
        if len(collected) >= 16:
            break
        current = parent
    if reparsed_bytes:
        self.trace_recorder.record(
            "governor.lineage_inheritance.reparsed",
            {
                "node_id": node.node_id,
                "hops": hops,
                "reparsed_bytes": reparsed_bytes,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )
    return collected or None


def _strip_unregistered_artifact_refs(self: Orchestrator, patch) -> None:
    """Drop hallucinated logical artifact references from added nodes.

    Live r11 (17:14-17:16 UTC): the recovery planner referenced an
    unregistered ``artifact:sqlite3_exe_v3_45_2_win32_x86`` in three
    consecutive patches; every rejection repeated the identical error
    and the failed node went terminal without the planner ever dropping
    the reference.  An unknown input-artifact reference carries no
    contract value — strip it (audited by trace) instead of failing the
    whole patch.  Registered artifacts and output_artifacts stay strict.
    """
    known = set((getattr(self.task_graph, "artifacts", None) or {}).keys())
    dropped: list[str] = []
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        refs = raw.get("input_artifacts")
        if not isinstance(refs, list):
            continue
        kept = []
        for ref in refs:
            text = str(ref or "").strip()
            # Governor validation rejects ANY unregistered input-artifact
            # reference (prefixed or bare), so both shapes are stripped.
            if text and text not in known:
                dropped.append(f"{raw.get('node_id') or '?'}:{text}")
                continue
            kept.append(ref)
        raw["input_artifacts"] = kept
    if dropped:
        self.trace_recorder.record(
            "governor.patch.unregistered_artifact_refs_stripped",
            {"dropped": dropped[:16]},
        )


def _normalize_recovery_targets(self: Orchestrator, patch) -> None:
    """Strip ``recovery_of`` references to nodes that cannot be recovered.

    Live r11 (resume #7): the stalled planner twice attached
    ``recovery_of`` to COMPLETED/SUPERSEDED nodes (N-002-R5, N-004-R4),
    and the governor invariant rejected the whole patch each time —
    killing otherwise compliant upstream-repair proposals.  Claiming an
    inactive owner's FILES needs no ``recovery_of`` (inactive owners do
    not conflict); the invalid reference is dropped with an audit trail
    instead of failing the patch.
    """
    dropped: list[str] = []
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        target_id = str(raw.get("recovery_of") or "").strip()
        if not target_id:
            continue
        target = self.task_graph.get_node(target_id)
        if target is not None and target.status in {
            "completed", "superseded", "cancelled",
        }:
            dropped.append(
                f"{raw.get('node_id') or '?'}->{target_id}({target.status})"
            )
            raw.pop("recovery_of", None)
    if dropped:
        self.trace_recorder.record(
            "governor.patch.invalid_recovery_targets_stripped",
            {"dropped": dropped[:16]},
        )


def _ensure_artifact_producer_deps(self: Orchestrator, patch) -> None:
    """Guarantee the artifact-producer dependency invariant on the patch.

    Governor validation rejects a patch when an active consumer's final
    dependencies omit the producer of one of its input artifacts.  Live
    r11 (resume #7): the stalled planner rewired blocked consumers onto
    the new repair node and dropped the completed producer, killing an
    otherwise compliant patch.  Re-adding the producer is deterministic
    and semantically free (a dependency on a completed producer is
    already satisfied), so normalize instead of rejecting.
    """
    producers: dict[str, str] = {}
    artifacts = getattr(self.task_graph, "artifacts", None) or {}
    for artifact_id, artifact in artifacts.items():
        producer = str(getattr(artifact, "node_id", "") or "")
        if producer:
            producers[str(artifact_id)] = producer
    # Mirror the governor's producer resolution: node-declared
    # output_artifacts also establish production (live r11 resume #8:
    # 'artifact:requirements_md' was registry-listed without a node_id
    # while failed N-007-R1 declared it, so the exact-registry lookup
    # missed and validation rejected the patch for a missing producer
    # dependency).  Precedence MIRRORS the governor's own resolution
    # (register registry first, then let node declarations override):
    # live r11 resume #9 terminalized N-002-R8 because this map kept
    # registry owner N-002-R5 for 'artifact:backend_component' while
    # governor validation saw the patch-declared producer
    # N-002-R8-RBAC-VERIFY — the restored dependency satisfied neither.
    node_ids = set()
    for other in (getattr(self.task_graph, "nodes", None) or {}).values():
        other_id = str(getattr(other, "node_id", "") or "")
        if not other_id:
            continue
        node_ids.add(other_id)
        for output_id in getattr(other, "output_artifacts", None) or []:
            key = str(output_id or "").strip()
            if key:
                producers[key] = other_id
    add_ids = {
        str(raw.get("node_id") or raw.get("task_id") or "")
        for raw in (getattr(patch, "add_nodes", []) or [])
        if isinstance(raw, dict)
    }
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        declared_id = str(raw.get("node_id") or raw.get("task_id") or "")
        for output_id in raw.get("output_artifacts") or []:
            key = str(output_id or "").strip()
            if key and declared_id:
                producers[key] = declared_id
    restored: list[str] = []

    def _fix(node_id: str, raw: dict, inputs: Any, deps: list[str]) -> bool:
        changed = False
        for ref in inputs or []:
            producer = producers.get(str(ref))
            if not producer or producer == node_id or producer in deps:
                continue
            if producer not in node_ids and producer not in add_ids:
                continue
            # Never auto-attach a dependency on a terminally failed
            # producer: that would silently create a dead dependency.
            # Validation rejection educates the planner to drop the
            # reference instead (live r11: N-007-R1 requirements_md).
            producer_node = self.task_graph.get_node(producer)
            if producer_node is not None and (
                producer_node.status == "failed"
                or producer_node.metadata.get("_terminal_failure")
            ):
                continue
            deps.append(producer)
            restored.append(f"{node_id}+={producer}")
            changed = True
        return changed

    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id") or raw.get("task_id") or "")
        deps = [str(item) for item in (raw.get("dependencies") or [])]
        if _fix(node_id, raw, raw.get("input_artifacts"), deps):
            raw["dependencies"] = deps
    for raw in list(getattr(patch, "update_nodes", []) or []):
        if not isinstance(raw, dict) or "dependencies" not in raw:
            continue
        node_id = str(raw.get("node_id") or raw.get("task_id") or "")
        node = self.task_graph.get_node(node_id)
        if node is None:
            continue
        inputs = raw.get("input_artifacts")
        if inputs is None:
            inputs = list(getattr(node, "input_artifacts", []) or [])
        deps = [str(item) for item in (raw.get("dependencies") or [])]
        if _fix(node_id, raw, inputs, deps):
            raw["dependencies"] = deps
    if restored:
        self.trace_recorder.record(
            "governor.patch.artifact_producer_deps_restored",
            {"restored": restored[:24]},
        )


def _clip_head_tail(text: Any, total: int, tail: int) -> str:
    """Clip long verifier details keeping head AND tail (round 8).

    The live r11 semantic details ran ~1500 chars with the decisive
    "Blocking gaps:" section at the END; a plain [:800] truncation cut
    exactly the current truth the stalled planner needs.
    """
    text = str(text or "")
    if len(text) <= total:
        return text
    head = text[: max(0, total - tail - 1)]
    return f"{head}…{text[-tail:]}"


def _resolve_declared_root_causes(
    self: Orchestrator, node, verification_result, worker_report,
    include_self_owned: bool = False,
):
    """Resolve declared root-cause files to their inactive owners.

    Returns ``{token: (owner_node_id, [resolved relative paths])}`` for
    every path-like token that a strong declaration (worker
    ``next_actions``/summary) or a root-cause-worded verifier detail
    mentions AND that is owned by another COMPLETED/SUPERSEDED node.
    Empty when nothing is declared, or when every declared file belongs
    to the failed node itself or to an active owner (which may still
    self-recover through its own lineage).

    ``include_self_owned=True`` (round 8) keeps tokens covered by the
    failed node's OWN contract in the result.  Routing enforcement must
    keep excluding them (a self-owned file is never an UPSTREAM defect —
    live r11 resume #8 lesson), but the lineage depth-cap exemption must
    not: when the terminal node owned the offending files itself, an
    ownership-declaring takeover is the only remaining legal shape, and
    without this variant the cap's recommended escape route is
    unreachable (live r11 run-blocked: six generations, every root-cause
    file self-owned, every stalled patch rejected).

    Shared by the routing enforcement and the lineage-repeat fingerprint
    so both reason about exactly the same root-cause picture.
    """
    report = worker_report or {}
    strong_texts: list[str] = []
    for action in report.get("next_actions") or []:
        if isinstance(action, dict):
            strong_texts.append(str(action.get("goal") or ""))
            strong_texts.append(str(action.get("reason") or ""))
    strong_texts.append(str(report.get("summary") or ""))
    weak_texts: list[str] = []
    for dimension in getattr(verification_result, "dimensions", []) or []:
        if isinstance(dimension, dict):
            weak_texts.append(str(dimension.get("details") or ""))
        else:
            weak_texts.append(str(getattr(dimension, "details", "") or ""))

    root_cause_words = (
        "root cause", "due to", "caused by", "hardcoded", "hard-coded",
        "skip", "missing", "absent", "violat", "not found", "omitted",
        "excluded", "instead of", "根因", "缺陷", "跳过", "缺失", "故意",
        # Round 8 fix 5 (resume #13 blocked in 90 seconds): the live
        # verifier receipt described its blocking gaps as "was not
        # executed / no evidence / Blocking gaps" — none of the original
        # words matched, the weak text was dropped, and the whole
        # recipe/exemption chain starved to {}.  These phrases occur in
        # gap narratives, never in praise.
        "not executed", "no evidence", "blocking gap", "unverified",
        "no receipt", "未执行", "无证据", "证据缺口", "未被验证",
    )

    token_sources: list[str] = [t for t in strong_texts if t.strip()]
    for text in weak_texts:
        # A verdict mixes satisfied checks with blocking failures. Once
        # it has an explicit gap section, only that section declares
        # defects; otherwise a successful health receipt mentioned above
        # an unrelated failure becomes a mandatory upstream repair.
        sections = re.split(r"blocking gaps\s*:", text, maxsplit=1, flags=re.IGNORECASE)
        if len(sections) == 2:
            text = sections[1]
        lowered = text.lower()
        if text.strip() and any(word in lowered for word in root_cause_words):
            token_sources.append(text)
    blob = "\n".join(token_sources)
    if not blob.strip():
        return {}

    # b33 S2 (D4, safe half): the suffix lexer is the single module-level table
    # declared above — it used to be written twice (here as a tuple and again
    # inside the regex alternation).  The regex is derived from it in the same
    # order, so the extraction is unchanged; the redundant re-check that every
    # token ends with a known suffix is gone because the alternation is the
    # last element of the pattern.  The ``deliverables|src|data|run`` prefix
    # regex and the evidence-artifact heuristic below are NOT this table and
    # are deliberately left untouched (S4 owns the tokenizer unification).
    suffix_alternation = "|".join(
        suffix.lstrip(".") for suffix in ROOT_CAUSE_TEXT_SUFFIXES
    )
    tokens: set[str] = set()
    for match in re.finditer(
        r"[A-Za-z0-9_./\\-]*[A-Za-z0-9_-]\." + "(?:" + suffix_alternation + ")",
        blob,
    ):
        tokens.add(match.group(0).replace("\\", "/").lstrip("./").strip("/"))
    for match in re.finditer(r"(?:deliverables|src|data|run)/[A-Za-z0-9_./-]+/", blob):
        tokens.add(match.group(0).replace("\\", "/").strip("/").rstrip("/"))
    # Fix 17 (live r11 resume #27 blocked twice within four minutes and
    # never dispatched a worker): the miner declared ``health_resp.json``
    # — a /health response RECEIPT owned by completed N-002-R5 — an
    # upstream root cause.  Routing enforcement then deterministically
    # rejected every recovery patch that did not add a node claiming that
    # receipt ("recovery patch ignores the declared upstream root cause"),
    # the identical rejection repeated three times, and the run blocked on
    # "same autonomous recovery outcome repeated three times".  Receipts
    # and run-evidence files are regenerated evidence OUTPUTS, not defect
    # files; they must never drive mandatory-ownership enforcement or the
    # lineage fingerprint.  Planner-driven recovery may still target them
    # freely — this filter only narrows the declared-root-cause picture.
    def _is_evidence_artifact(token: str) -> bool:
        lowered_token = token.lower()
        if "deliverables/runs/" in lowered_token or lowered_token.startswith("runs/"):
            return True
        if ".astralnexus_process_logs" in lowered_token:
            return True
        base = lowered_token.rsplit("/", 1)[-1]
        return (
            base.endswith("_resp.json")
            or base.endswith("_response.json")
            or base.endswith("_evidence.json")
            or "receipt" in base
        )

    tokens = {token for token in tokens if not _is_evidence_artifact(token)}
    if not tokens:
        return {}

    def _norm(value: Any) -> str:
        return identity_key(value)

    def _owner_for(path: str) -> str:
        """Inactive owner whose contract covers ``path`` ("" when none).

        COMPLETED owners take precedence over superseded ones: a file
        committed by a completed node keeps that node as its authoritative
        owner even when superseded lineage ancestors also held the path
        (live r11: init_db.py resolved to superseded N-002 instead of
        completed N-002-R5, which disarmed the class-aware enforcement).
        """
        for wanted_status in ("completed", "superseded"):
            for other in self.task_graph.nodes.values():
                if other.node_id == node.node_id:
                    continue
                if other.status != wanted_status:
                    continue
                for owned in other.owned_paths:
                    owned_norm = _norm(owned)
                    if not owned_norm:
                        continue
                    if paths_overlap(path, owned_norm):
                        return other.node_id
        return ""

    # Resolve tokens to repo-relative paths.  Bare file names (no slash)
    # are looked up in the run's deliverables tree; ambiguity across
    # different owners disables enforcement for that token.  Coverage is
    # compared on the RESOLVED paths, so a bare "init_db.py" declaration
    # still matches a patch owning "deliverables/app/backend/init_db.py".
    resolved: dict[str, tuple[str, list[str]]] = {}
    own_paths = [
        _norm(owned)
        for owned in (getattr(node, "owned_paths", None) or [])
        if _norm(owned)
    ]

    def _self_owned(path: str) -> bool:
        """True when the FAILED node's own contract covers ``path``.

        A file owned by the failed node itself is never an UPSTREAM root
        cause: takeover/rework through its own lineage legally re-owns
        and repairs it.  Live r11 (resume #8): N-002-R7 owned the whole
        backend tree and its verified init_db.py repair passed all five
        acceptance criteria; verifier PRAISE mentions of those files were
        misread as upstream declarations, so every (correct) 
        verification-node patch was rejected and the node went terminal
        on three strikes while the repair sat real on disk.
        """
        for owned in own_paths:
            if paths_overlap(path, owned):
                return True
        return False

    # Fix S1 (b32_path_identity): a bare file name is relocated inside the
    # DECLARED path set only — the union of every node's ``owned_paths``.
    # The old code searched the run's deliverables tree for a same-named
    # file (``deliverables_root.rglob``), which is the forbidden "looks
    # close enough" fallback: any stray copy of the name could stand in for
    # the declared file, so the declared-root-cause picture depended on what
    # happened to exist on disk.  A unique suffix match inside the declared
    # set is the only relocation; an ambiguous name resolves to nothing and
    # simply disables enforcement for that token, exactly as it did before.
    declared_paths: list[str] = []
    for other in self.task_graph.nodes.values():
        for owned in getattr(other, "owned_paths", None) or []:
            key = identity_key(owned)
            if key and key not in declared_paths:
                declared_paths.append(key)
    for token in sorted(tokens):
        candidates: list[str] = []
        # The ONE identity plane decides this (Fix S1 / b32_path_identity): a
        # unique match inside the declared set relocates the token, an
        # ambiguous or absent one stays unresolved, and the filesystem is
        # never consulted.
        resolved_path = ""
        try:
            bound = workspace_identity("", token, declared=declared_paths)
            if bound.status != "unbound":
                resolved_path = bound.rel
            elif "/" in token:
                # An explicit path nobody declared still resolves to itself.
                resolved_path = token
            else:
                # A bare name: the owning node may declare the TREE holding
                # it.  Unique tree only — the shared plane decides.
                resolved_path = declared_tree_member(token, declared_paths)
        except PathIdentityError:
            resolved_path = ""
        if resolved_path:
            candidates.append(resolved_path)
        if (
            not include_self_owned
            and candidates
            and any(_self_owned(c) for c in candidates)
        ):
            # Self-owned files route through the node's own rework /
            # takeover lineage, not through an upstream repair demand.
            continue
        owners = {_owner_for(c) for c in candidates}
        owners.discard("")
        if len(owners) == 1:
            resolved[token] = (next(iter(owners)), candidates)
    return resolved


def _patch_covers_root_causes(self: Orchestrator, patch, resolved) -> bool:
    """True when any added node declares ownership of a resolved file.

    Round 8: shared by routing enforcement (``addressed``) and the
    lineage depth-cap offender-ownership exemption so both read the
    patch's ownership declarations exactly the same way — goal text
    alone never counts (live r11: three proposals described init_db.py
    repairs while declaring no owned_paths and were cap-rejected).
    """
    if not resolved:
        return False

    def _norm(value: Any) -> str:
        return identity_key(value)

    patch_paths: set[str] = set()
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        for key in ("owned_paths", "expected_outputs"):
            for value in raw.get(key) or []:
                text = str(value).strip()
                if text and not text.startswith("artifact:"):
                    patch_paths.add(_norm(text))

    # Fix S1: ``_covers`` was a byte-for-byte copy of
    # ``core.governor.contracts_overlap``; both are now the shared
    # ``paths_overlap`` identity comparison.
    return any(
        paths_overlap(candidate, owned)
        for _owner, candidates in resolved.values()
        for candidate in candidates
        for owned in patch_paths
    )


def _convert_redundant_to_dependent(self: Orchestrator, patch, reason: str) -> bool:
    """doc 18 修复F③：健康活跃所有者冗余冲突的最后一搏重塑。

    attempt6 live：N-001/N-001-R2 两代各烧 3 单位终判——提案反复
    重新声明健康 ready/verifying 节点（N-002-R*）的合同文件或要求
    接管它们。健康节点不可接管（Round 12 原则：拒绝词是老师），
    但判决词自己给出合法形状："复用而非新增：重挂依赖" / "只读
    需求声明 expected_inputs"。第三次提案确定性重塑：
    (a) 所有权冲突词 → 剥离与健康所有者重叠的声明转为
        expected_inputs 只读引用 + 对该所有者挂依赖；
    (b) "recovery target must be failed..." 且目标健康 → 摘除
        recovery_of（含 metadata 镜像），同样转引用挂依赖。
    剥离后合同为空（无 owned_paths 且无 output_artifacts）则放弃
    重塑保持拒绝语义（governor 要求实现节点声明合同）。返回是否重塑。
    """
    reason_text = str(reason)
    owner_id = ""
    shape = ""
    match = re.search(
        r"overlapping output ownership: (\S+) conflicts with (\S+?):",
        reason_text,
    )
    if match:
        new_id, owner_id = match.group(1), match.group(2)
        shape = "ownership_conflict"
    else:
        match = re.search(
            r"recovery target must be failed, repair_pending, or blocked: "
            r"(\S+) is (\w+)",
            reason_text,
        )
        if not match:
            return False
        owner_id = match.group(1)
        new_id = ""
        shape = "invalid_recovery_target"
    owner = self.task_graph.get_node(owner_id)
    if owner is None or owner.status in (
        "failed", "repair_pending", "blocked",          # 修复F②管辖
        "completed", "superseded", "cancelled",          # stripping/修复E管辖
    ):
        return False

    def _norm(value: Any) -> str:
        return identity_key(value)

    def _bare(value: Any) -> str:
        text = str(value).strip()
        return text[len("artifact:"):] if text.startswith("artifact:") else text

    owner_paths = {_norm(item) for item in owner.owned_paths if str(item).strip()}
    owner_artifacts = {_bare(item) for item in owner.output_artifacts if str(item).strip()}
    reshaped = False
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        if shape == "ownership_conflict":
            if str(raw.get("node_id") or "") != new_id:
                continue
        meta_value = raw.get("metadata")
        raw_meta: Dict[str, Any] = meta_value if isinstance(meta_value, dict) else {}
        declares_owner = (
            str(raw.get("recovery_of") or "") == owner_id
            or str(raw_meta.get("recovery_of") or "") == owner_id
        )
        if shape == "invalid_recovery_target" and not declares_owner:
            continue
        if shape == "invalid_recovery_target":
            raw.pop("recovery_of", None)
            if isinstance(raw.get("metadata"), dict):
                raw["metadata"].pop("recovery_of", None)
        kept_paths: List[Any] = []
        moved_paths: List[Any] = []
        for item in list(raw.get("owned_paths") or []):
            (moved_paths if _norm(item) in owner_paths else kept_paths).append(item)
        kept_artifacts: List[Any] = []
        moved_artifacts: List[Any] = []
        for item in list(raw.get("output_artifacts") or []):
            (moved_artifacts if _bare(item) in owner_artifacts else kept_artifacts).append(item)
        if shape == "invalid_recovery_target" and not moved_paths and not moved_artifacts:
            # (b) 形状无重叠声明：摘除非法 recovery_of + 挂依赖即可
            moved_paths = list(owner_paths)
            kept_paths = list(raw.get("owned_paths") or [])
        if not moved_paths and not moved_artifacts and shape == "ownership_conflict":
            continue
        if not kept_paths and not kept_artifacts:
            continue  # 合同全冲突——重塑产生空合同节点，保持拒绝语义
        raw["owned_paths"] = kept_paths
        raw["output_artifacts"] = kept_artifacts
        # 只读引用通道分两类：artifact 声明走 input_artifacts（合同
        # 字段，用所有者的注册形状保证 producer 解析通过——governor
        # 修正文本自认："改为 input_artifacts 并在 dependencies 加入
        # 生产者"）；文件路径走 metadata.expected_inputs（信息性引用，
        # 持久化但不进 input_artifacts 解析验证——未知文件路径会触发
        # input_contract_errors 拒绝）。
        if moved_paths:
            meta_target = raw.get("metadata")
            if not isinstance(meta_target, dict):
                meta_target = {}
                raw["metadata"] = meta_target
            meta_inputs = [str(item) for item in (meta_target.get("expected_inputs") or [])]
            for item in moved_paths:
                text = str(item)
                if text not in meta_inputs:
                    meta_inputs.append(text)
            meta_target["expected_inputs"] = meta_inputs
        if moved_artifacts:
            inputs = [str(item) for item in (raw.get("input_artifacts") or [])]
            for item in moved_artifacts:
                bare = _bare(item)
                owner_form = next(
                    (
                        str(artifact).strip()
                        for artifact in owner.output_artifacts
                        if _bare(artifact) == bare
                    ),
                    "",
                )
                text = owner_form or str(item)
                if text not in inputs:
                    inputs.append(text)
            raw["input_artifacts"] = inputs
        deps = [str(item) for item in (raw.get("dependencies") or [])]
        if owner_id not in deps:
            deps.append(owner_id)
        raw["dependencies"] = deps
        self.trace_recorder.record(
            "governor.verification_recovery.redundant_conflict_reshaped",
            {
                "replacement_node_id": str(raw.get("node_id") or ""),
                "healthy_owner_id": owner_id,
                "shape": shape,
                "moved_to_expected_inputs": [str(i) for i in [*moved_paths, *moved_artifacts]],
            },
        )
        reshaped = True
    return reshaped


def _convert_conflict_to_takeover(self: Orchestrator, patch, reason: str) -> bool:
    """doc 18 修复F②：所有权冲突提案的最后一搏接管转换。

    第三次提案因与 ACTIVE 节点所有权冲突被拒时，按判决词自己的
    指示（"在新增节点上显式设置 recovery_of=该活跃节点ID 完成
    接管"）确定性改写。冲突节点必须是 governor recovery_of 合法
    目标集（failed/repair_pending/blocked）；COMPLETED 冲突归
    修复E/stripping 管，不在本机制内。返回是否发生转换。
    """
    match = re.search(
        r"overlapping output ownership: (\S+) conflicts with (\S+?):",
        str(reason),
    )
    if not match:
        return False
    new_id, existing_id = match.group(1), match.group(2)
    existing = self.task_graph.get_node(existing_id)
    if existing is None or existing.status not in {
        "failed", "repair_pending", "blocked",
    }:
        return False
    converted = False
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("node_id") or "") != new_id:
            continue
        previous = str(raw.get("recovery_of") or "")
        if previous == existing_id:
            # 已是接管形状仍冲突——非本机制管辖，保持拒绝语义
            return False
        raw["recovery_of"] = existing_id
        raw.setdefault("metadata", {})["recovery_of"] = existing_id
        raw.setdefault("metadata", {})["last_resort_takeover_of"] = existing_id
        if previous:
            raw["metadata"]["previous_recovery_of"] = previous
        converted = True
    return converted


def _grant_completed_root_cause_paths(self: Orchestrator, patch, resolved) -> List[str]:
    """把 COMPLETED 所有且提案未覆盖的根因路径授予首个新增节点。

    非活跃所有者不构成所有权冲突（governor active_owner 检查与
    Round 13 takeover-grant 测试均已实证）。返回授予的路径列表。

    b33 case1 fix10 第 2 步：授予同时**记账**——已完成作者保留 owned_paths
    （历史归属），当前写权限记成一条确定性转移（from 作者 → to 接管者），
    每个路径至多一个当前持有者且记名幂等。
    """
    def _norm(value: Any) -> str:
        return identity_key(value)

    add_nodes = [
        raw for raw in list(getattr(patch, "add_nodes", []) or [])
        if isinstance(raw, dict)
    ]
    if not add_nodes or not resolved:
        return []
    completed_paths: Dict[str, str] = {}   # 规范化路径 -> 完成的作者节点
    for token, (owner, candidates) in sorted(resolved.items()):
        owner_node = self.task_graph.get_node(owner)
        if owner_node is None or owner_node.status != "completed":
            continue
        for candidate in candidates:
            text = _norm(candidate)
            if text and text not in completed_paths:
                completed_paths[text] = str(owner)
    if not completed_paths:
        return []
    first = add_nodes[0]
    existing = {_norm(p) for p in (first.get("owned_paths") or [])}
    missing = [p for p in completed_paths if p not in existing]
    if missing:
        first["owned_paths"] = list(first.get("owned_paths") or []) + missing
    claimant = str(first.get("node_id") or "")
    # A proposal is not a permission grant. Metadata is committed (or
    # rolled back) together with the graph, dependencies and owned paths.
    if claimant and missing:
        from AstralNexus.core.write_authority import TRANSFER_KEY
        metadata = dict(first.get("metadata") or {})
        transfers = list(metadata.get(TRANSFER_KEY) or [])
        for path in missing:
            entry = {"path": path, "from_node": completed_paths[path], "to_node": claimant}
            if entry not in transfers:
                transfers.append(entry)
        metadata[TRANSFER_KEY] = transfers
        first["metadata"] = metadata
    return missing


def _normalize_root_cause_takeover(self: Orchestrator, node, patch, resolved) -> Dict[str, Any]:
    """doc 18 修复E：接管意图明确但形状非法的提案自动规范化。

    attempt3/4 实证的矛盾指令对：路由验证器要求新节点接管 COMPLETED
    所有的根因文件（其判决词自认 "a new repair node may legally
    claim"），governor 验证器却禁止 recovery_of 指向 completed 节点
    ——LLM 提案反复落入两种非法形状之一，三连拒烧尽提案机会 → 终判
    （attempt4 一轮烧 4 单位预算）。

    边界（保住 Round 8 语义）：只规范化**接管意图明确**的提案——
    add_node 的 recovery_of 指向非活跃节点（completed/superseded/
    cancelled）。此时：1) 重定向 recovery_of 到当前被恢复节点（须自身
    处于 failed/repair_pending/blocked，否则整体不动）；2) 把未覆盖的
    COMPLETED 所有根因路径授予该接管节点的 owned_paths（非活跃所有者
    不构成所有权冲突——governor active_owner 检查与 Round 13
    takeover-grant 测试均已实证）。纯 workaround（无 recovery_of、
    不声明根因路径）不规范化——拒绝反馈循环是设计行为（r11 六代
    workaround 教训），必须保留。幂等；真空提案不动。
    """
    granted: List[str] = []
    retargeted_from = ""
    add_nodes = [
        raw for raw in list(getattr(patch, "add_nodes", []) or [])
        if isinstance(raw, dict)
    ]
    if not add_nodes:
        return {"granted_paths": granted, "retargeted_from": retargeted_from}

    def _norm(value: Any) -> str:
        return identity_key(value)

    # 1) 接管意图 = recovery_of 指向非活跃节点；重定向到被恢复节点。
    # doc 18 修复E2（attempt6 live）：非活跃严格限定 completed/
    # superseded/cancelled——健康活跃目标（verifying/ready/running 等）
    # 的重定向会制造跨谱系接管畸形（live：N-001-R2 恢复提案声明
    # recovery_of=verifying 的 N-002-R2 被改写成接管被恢复节点自身，
    # 三案全数死于与健康所有者的冲突）。健康活跃目标的冗余冲突留给
    # 拒绝反馈与修复F③处理。
    takeover_node: Optional[Dict[str, Any]] = None
    if node.status in ("failed", "repair_pending", "blocked"):
        for raw in add_nodes:
            recovery_of = str(raw.get("recovery_of") or "")
            if not recovery_of or recovery_of == node.node_id:
                continue
            owner = self.task_graph.get_node(recovery_of)
            if owner is not None and owner.status in (
                "completed", "superseded", "cancelled",
            ):
                raw["recovery_of"] = node.node_id
                retargeted_from = recovery_of
                if takeover_node is None:
                    takeover_node = raw
    if takeover_node is None:
        return {"granted_paths": granted, "retargeted_from": retargeted_from}

    # 2) 授予 COMPLETED 所有的未覆盖根因路径（仅接管意图提案）
    granted = self._grant_completed_root_cause_paths(patch, resolved)

    self.trace_recorder.record(
        "governor.verification_recovery.root_cause_takeover_normalized",
        {
            "node_id": node.node_id,
            "granted_paths": granted,
            "retargeted_from": retargeted_from,
        },
    )
    return {"granted_paths": granted, "retargeted_from": retargeted_from}


def _enforce_root_cause_routing(
    self: Orchestrator, node, patch, resolved, *, last_resort: bool = False,
) -> bool:
    """Reject recovery patches that ignore a declared upstream root cause.

    Live audit (r11 test track): six generations of test-side workaround
    nodes were derived while every worker report — and the verifier's own
    details — declared the root cause inside ``init_db.py`` / ``main.py``,
    files owned by a COMPLETED node.  Completed owners are inactive, so a
    new repair node may legally claim those paths; the replanner simply
    never routed there because nothing forced it.

    Class-aware coverage (live r11 V7 round): tokens owned by COMPLETED
    nodes are the committed-delivery defects; a patch must address at
    least one of them when any are declared.  Covering only superseded
    lineage-ancestor paths (the workaround contract's own vocabulary)
    does NOT count — that ANY-semantics hole let generation #11 slip
    through with init_db.py resolved but untouched.

    doc 18 修复E（last_resort）：前两次拒绝保留 Round 8 反馈学习语义；
    第三次（终判前最后一案）改为确定性补救——把 COMPLETED 所有的根因
    路径授予新增节点后放行（判决词自认的合法接管形状）。attempt3/4
    实证 LLM 三连拒烧尽提案机会 → 终判 → 孤儿级联（attempt4 该接缝
    烧掉约 13/21 单位预算）；补救把 3拒+终判 变成 2拒+1救。

    Returns True when the patch covers a COMPLETED-owned root cause —
    the shape the lineage depth cap explicitly recommends and therefore
    must not block.  Raising ``ValueError`` feeds the existing
    rejection-feedback loop, so the next planner attempt receives the
    routing instruction verbatim.
    """
    if not resolved:
        return False

    completed_owned = {
        token: (owner, candidates)
        for token, (owner, candidates) in resolved.items()
        if (owner_node := self.task_graph.get_node(owner)) is not None
        and owner_node.status == "completed"
    }
    scope = completed_owned or resolved
    addressed = self._patch_covers_root_causes(patch, scope)
    if not addressed and last_resort and completed_owned:
        granted = self._grant_completed_root_cause_paths(patch, scope)
        if granted:
            addressed = self._patch_covers_root_causes(patch, scope)
            self.trace_recorder.record(
                "governor.verification_recovery.root_cause_last_resort_grant",
                {
                    "node_id": node.node_id,
                    "granted_paths": granted,
                    "now_addresses_root_cause": bool(addressed),
                },
            )
    self.trace_recorder.record(
        "governor.verification_recovery.root_cause_routing",
        {
            "node_id": node.node_id,
            "root_cause_files": {
                token: owner
                for token, (owner, _paths) in sorted(resolved.items())
            },
            "completed_owned_files": sorted(completed_owned),
            "patch_addresses_root_cause": bool(addressed),
        },
    )
    if addressed:
        return bool(completed_owned)
    summary = "; ".join(
        f"{token} owned by {owner}"
        for token, (owner, _paths) in sorted(scope.items())[:3]
    )
    raise ValueError(
        "recovery patch ignores the declared upstream root cause: "
        f"{summary}. Those owners are inactive (completed/superseded), so a new "
        "repair node may legally claim the root-cause paths. recovery_of names the failed "
        "consumer, not its completed author; file permission is a separate committed grant. Add a node owning "
        "those files — with the failed node or its successor depending on it — "
        "instead of requiring the failed node to work around the upstream "
        "defect inside its own contract."
        + (
            " Covering only lineage-ancestor paths is not enough: at least "
            "one added node must own the COMPLETED-owned root-cause files."
            if completed_owned
            else ""
        )
    )


def _lineage_root_node_id(self: Orchestrator, node) -> str:
    """Walk the ``recovery_of`` chain back to the original task node.

    The lineage root is the anchor for cross-generation recovery state:
    superseding a node mints a new ID but must NOT reset the attempt
    history (doc 14 §12:50, user directive step 3).  Superseded roots
    stay in the graph, so their metadata is durable storage.
    """
    root_id = str(node.node_id)
    seen = {root_id}
    current = node
    for _ in range(32):
        metadata = getattr(current, "metadata", None) or {}
        parent_id = str(
            metadata.get("recovery_of")
            or metadata.get("supersedes_node_id")
            or ""
        ).strip()
        if not parent_id or parent_id in seen:
            break
        parent = self.task_graph.get_node(parent_id)
        if parent is None:
            break
        seen.add(parent_id)
        root_id = parent_id
        current = parent
    return root_id


def _lineage_root_of_id(self: Orchestrator, node_id: str) -> str:
    """Lineage-root normalization for IDs referenced inside a patch."""
    node = self.task_graph.get_node(str(node_id))
    if node is None:
        return str(node_id)
    return self._lineage_root_node_id(node)


def _lineage_recovery_fingerprint(
    self: Orchestrator, node, patch, verification_result, worker_report, resolved
) -> str:
    """Stable identity for "same root cause + same file state + same plan".

    Components are deliberately generation-independent:
    * root-cause key — resolved inactive-owner files when declared, else
      the normalized verifier findings;
    * file-state key — content digest (size-capped sha256) of every
      resolved root-cause file, so a changed upstream file legitimately
      unlocks a retry of the same plan;
    * approach key — the canonical patch shape (goal/role/owned paths and
      lineage-normalized dependencies for adds; lineage-normalized target
      + status for updates; cancels), EXCLUDING generation node IDs so
      minting ``-R5``/``-R6``/``-V2`` suffixes cannot launder a repeat.
    """
    def _normalized_goal(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())[:120]

    verdict_value = str(getattr(verification_result, "verdict", "") or "")
    try:
        score_bucket: Any = round(
            float(getattr(verification_result, "overall_score", 0.0) or 0.0), 1
        )
    except (TypeError, ValueError):
        score_bucket = None
    if resolved:
        causes: Any = {
            token: [owner, sorted(paths)]
            for token, (owner, paths) in sorted(resolved.items())
        }
    else:
        # Volatile verifier DETAILS text must not anchor identity: r11
        # recorded three workaround generations whose rephrased details
        # produced three different fallback keys and evaded the repeat
        # block.  Dimension names + contract goal + verdict + score
        # bucket are generation-stable for the same unresolved failure.
        names: list[str] = []
        for dimension in getattr(verification_result, "dimensions", []) or []:
            if isinstance(dimension, dict):
                names.append(str(dimension.get("name") or ""))
            else:
                names.append(str(getattr(dimension, "name", "") or ""))
        causes = sorted(name for name in names if name) or [
            verdict_value or "unknown"
        ]
    root_cause_key = json.dumps(
        {
            "contract_goal": _normalized_goal(getattr(node, "goal", "")),
            "verdict": verdict_value,
            "score_bucket": score_bucket,
            "causes": causes,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    run_root = Path(str(getattr(self, "run_dir", "") or "."))
    file_state: list[str] = []
    seen_paths: set[str] = set()
    for _token, (_owner, paths) in sorted(resolved.items()):
        for rel in paths:
            if rel in seen_paths:
                continue
            seen_paths.add(rel)
            target = run_root / rel
            try:
                if target.is_file():
                    data = target.read_bytes()[: 2 * 1024 * 1024]
                    digest = hashlib.sha256(data).hexdigest()[:16]
                    file_state.append(f"{rel}:{len(data)}:{digest}")
                else:
                    file_state.append(f"{rel}:absent")
            except OSError:
                file_state.append(f"{rel}:unreadable")

    approach: list[str] = []
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        approach.append(json.dumps([
            "add",
            _normalized_goal(raw.get("goal")),
            str(raw.get("role") or ""),
            sorted(
                str(item).replace("\\", "/").strip().rstrip("/")
                for item in (raw.get("owned_paths") or [])
            ),
            sorted(
                self._lineage_root_of_id(str(item))
                for item in (raw.get("dependencies") or [])
            ),
        ], ensure_ascii=False, sort_keys=True))
    for raw in list(getattr(patch, "update_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        approach.append(json.dumps([
            "upd",
            self._lineage_root_of_id(
                str(raw.get("node_id") or raw.get("task_id") or "")
            ),
            str(raw.get("status") or ""),
        ], ensure_ascii=False, sort_keys=True))
    for raw in sorted(str(item) for item in (getattr(patch, "cancel_nodes", []) or [])):
        approach.append(json.dumps(["cancel", self._lineage_root_of_id(raw)]))

    payload = json.dumps(
        {
            "root_cause": root_cause_key,
            "file_state": sorted(file_state),
            "approach": sorted(approach),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _lineage_history(self: Orchestrator, node) -> tuple[Any, list[dict]]:
    """Return (storage node, history list) for the node's lineage root."""
    root_id = self._lineage_root_node_id(node)
    root = self.task_graph.get_node(root_id) or node
    history = root.metadata.get("_lineage_recovery_history")
    if not isinstance(history, list):
        history = []
    return root, [entry for entry in history if isinstance(entry, dict)]


def _patch_derives_from_lineage(self: Orchestrator, node, patch) -> bool:
    """True when the patch re-derives work inside the node's lineage.

    Round 10 (live r11 resume #15, blocked in 80 seconds): the depth cap
    exists to stop WITHIN-LINEAGE re-derivation ("do not re-derive
    within this lineage"), but it was applied to EVERY stalled patch
    keyed on a capped primary — including independent additions that
    derive nothing: no recovery_of/supersedes into the lineage, no
    ownership of the lineage node's contract files, only a new
    verification node capturing the missing receipts plus a rewire of
    the blocked consumers.  That shape is precisely the alternative the
    cap's own rejection text recommends ("escalate with genuinely new
    evidence"), yet the cap rejected it three times and blocked the run
    while the planner's reasoning was already correct ("instead of
    repeating prior recovery attempts on backend implementation").
    Non-deriving patches skip the DEPTH cap; the identical-approach
    fingerprint check still guards against clone spam, and the
    run-level no-progress guard still bounds futile cycles.
    """
    root, history = self._lineage_history(node)
    lineage_ids = {node.node_id, root.node_id}
    for entry in history:
        entry_id = str(entry.get("node_id") or "")
        if entry_id:
            lineage_ids.add(entry_id)
        for added in entry.get("added") or []:
            if str(added):
                lineage_ids.add(str(added))

    def _norm(value: Any) -> str:
        return identity_key(value)

    own_paths = [
        _norm(owned)
        for owned in (getattr(node, "owned_paths", None) or [])
        if _norm(owned)
    ]

    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        for key in ("recovery_of", "supersedes_node_id"):
            target = str(raw.get(key) or "").strip()
            if target and target in lineage_ids:
                return True
        for key in ("owned_paths", "expected_outputs"):
            for value in raw.get(key) or []:
                text = _norm(value)
                if text and any(paths_overlap(text, owned) for owned in own_paths):
                    # Claiming the lineage node's own contract files is a
                    # takeover of its work — a derivation by definition
                    # (the offender-ownership exemption handles it).
                    return True
    for raw in list(getattr(patch, "update_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("node_id") or raw.get("task_id") or "").strip()
        if (
            target in lineage_ids
            and str(raw.get("status") or "").strip()
        ):
            # Re-arming a lineage node's status is a derivation attempt.
            return True
    return False


def _check_lineage_recovery_repeat(
    self: Orchestrator, node, fingerprint: str, *, addresses_upstream: bool = False,
    declares_offender_ownership: bool = False,
    derives_lineage: bool = True,
) -> None:
    """Block re-derivation of an already-tried identical recovery plan.

    The per-node attempt counters reset whenever a generation supersedes
    its predecessor, which let the r11 test track re-run the same
    workaround seven times under fresh IDs.  The lineage history lives on
    the root node, so a new ID cannot clear it.

    ``addresses_upstream`` carries the enforcement verdict "this patch
    covers a COMPLETED-owned root-cause file": the depth cap must not
    block the very upstream-repair route its own rejection message
    recommends (live r11: three stalled rounds were cap-blocked into a
    terminal state because every patch keyed on the capped lineage).

    ``declares_offender_ownership`` (round 8) is the self-owned variant
    of the same escape: when the terminal node owned the offending files
    itself, no token can ever resolve as UPSTREAM, so without this flag
    the cap deadlocks the lineage permanently (live r11 run-blocked:
    six generations, backend tree self-owned, every stalled patch
    rejected regardless of shape).  An ownership-declaring takeover with
    a NOVEL fingerprint is a genuine change of approach; the identical-
    approach check below still blocks clone shapes at any depth.

    ``derives_lineage`` (round 10) scopes the depth cap to patches that
    actually re-derive WITHIN the lineage (recovery_of/supersedes into
    it, ownership of its contract files, or status re-arms of its
    nodes).  An independent addition — e.g. a verification node
    capturing the missing receipts on disjoint output paths plus a
    rewire of the blocked consumers — derives nothing and must not be
    capped (live r11 resume #15: three such proposals, each rejected by
    the cap, blocked the run in 80 seconds while the planner's gap
    targeting was already correct).
    """
    root, history = self._lineage_history(node)
    # Round 13 (live r11 resume #20, blocked 21:48): two CORRECT hybrid
    # repair nodes were worker-match-blocked (NO_CAPABILITY_CANDIDATE)
    # BEFORE any execution, yet their fingerprint entered the lineage
    # history and this guard then rejected every later proposal of the
    # same — never actually tried — approach three times into a run
    # block.  An approach that never executed was never tried: filter
    # provably match-blocked attempts out of BOTH the depth cap and the
    # identical-approach check.
    history = [
        entry for entry in history
        if self._lineage_attempt_ever_dispatched(entry)
    ]
    if (
        len(history) >= 6
        and derives_lineage
        and not addresses_upstream
        and not declares_offender_ownership
    ):
        self.trace_recorder.record(
            "governor.verification_recovery.lineage_depth_blocked",
            {
                "node_id": node.node_id,
                "lineage_root": root.node_id,
                "attempt_count": len(history),
            },
        )
        raise ValueError(
            f"需要改变方案 (change of approach required): lineage {root.node_id} "
            f"has derived {len(history)} recovery generations without "
            "resolution (cap 6). Do not re-derive within this lineage: "
            "route the declared root cause to an upstream repair node "
            "owning the offending files, or escalate with genuinely new "
            "evidence. A patch that adds a node owning the COMPLETED-"
            "owned root-cause files is exempt from this cap, and so is a "
            "takeover node that explicitly declares (in owned_paths) the "
            "offending files named by the latest failure/verification "
            "receipts — goal text alone is not a declaration. The "
            "identical-approach check still applies at any depth."
        )
    prior = [
        entry for entry in history
        if entry.get("fingerprint") == fingerprint
    ]
    if not prior:
        return
    self.trace_recorder.record(
        "governor.verification_recovery.lineage_repeat_blocked",
        {
            "node_id": node.node_id,
            "lineage_root": root.node_id,
            "fingerprint": fingerprint,
            "repeat_count": len(prior) + 1,
            "previous_nodes": [str(entry.get("node_id")) for entry in prior],
        },
    )
    raise ValueError(
        f"需要改变方案 (change of approach required): lineage {root.node_id} already "
        "tried this exact approach for the same root cause with unchanged file "
        f"state — previously by {prior[-1].get('node_id')} (fingerprint "
        f"{fingerprint}, {len(prior)} prior attempt(s)). A new node ID does not "
        "reset lineage history. Propose a genuinely different plan: an upstream "
        "repair node owning the root-cause files, a different fix strategy, or "
        "escalation carrying new evidence."
    )


def _lineage_attempt_ever_dispatched(self: Orchestrator, entry) -> bool:
    """False only when every node of the attempt is provably match-blocked.

    ``last_failure.stage == "worker_profile_matching"`` is stamped only
    by the two worker-match-blocked branches — such a node never
    received an executable profile, never ran, never touched its
    declared files.  Live r11 resume #20: two correct hybrid repair
    nodes match-blocked (NO_CAPABILITY_CANDIDATE), their fingerprint
    entered the lineage history, and the repeat guard then rejected
    every later proposal of the same — never actually tried — approach
    three times into a run block.  Merely not-yet-dispatched nodes
    (ready/admitted, no match-block marker) still count as attempted:
    the approach was committed to the graph.  Missing nodes and
    unexpected metadata shapes are conservative (True) so the filter
    can never weaken the guard.
    """
    ids = [str(entry.get("node_id") or "")]
    ids += [str(item) for item in (entry.get("added") or [])]
    saw_match_blocked = False
    for node_id in ids:
        if not node_id:
            continue
        existing = self.task_graph.get_node(node_id)
        if existing is None:
            return True
        last_failure = (existing.metadata or {}).get("last_failure")
        if (
            not isinstance(last_failure, dict)
            or str(last_failure.get("stage") or "")
            != "worker_profile_matching"
        ):
            return True
        saw_match_blocked = True
    return not saw_match_blocked


def _record_lineage_recovery_attempt(self: Orchestrator, node, patch, fingerprint: str, applied) -> None:
    """Persist a successfully applied recovery attempt into the lineage."""
    root, history = self._lineage_history(node)
    history.append({
        "node_id": str(node.node_id),
        "fingerprint": fingerprint,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "added": [str(item) for item in ((applied or {}).get("added") or [])],
        "updates": len(list(getattr(patch, "update_nodes", []) or [])),
    })
    root.metadata["_lineage_recovery_history"] = history[-32:]


def _convert_completed_reopen_to_repair_node(self: Orchestrator, patch, source_node):
    """Rewrite illegal completed-node reopens as legal repair derivations.

    Verification recovery for a rejected deliverable naturally wants the
    COMPLETED producer to redo its work.  ``completed`` is immutable by
    design (no completed->ready transition), and ``recovery_of`` requires
    a failed-family target, so a reopen patch can never validate: r11
    N-008 exhausted three such attempts and went terminal, blocking the
    run.  The legal shape — already used by governed verification repair
    — is a NEW ``<producer>-R<k>`` node that inherits the producer's
    responsibility (owned_paths/output_artifacts), acceptance criteria,
    and role; depends on the completed producer; and re-arms the
    verification source on success via ``parent_node_id``.  Completed
    nodes are excluded from the Governor's active-ownership conflict
    check, so the takeover needs no ``recovery_of`` waiver.
    """
    reopen_statuses = {
        "ready", "repair_pending", "running", "result_submitted", "verifying",
    }
    updates = list(getattr(patch, "update_nodes", []) or [])
    kept_updates: list = []
    repair_specs: list = []
    converted = False
    for item in updates:
        if not isinstance(item, dict):
            kept_updates.append(item)
            continue
        target_id = str(item.get("node_id") or item.get("task_id") or "").strip()
        target = self.task_graph.get_node(target_id) if target_id else None
        if (
            target is None
            or target.status != "completed"
            or str(item.get("status") or "").strip() not in reopen_statuses
        ):
            kept_updates.append(item)
            continue
        suffix = 1
        while self.task_graph.get_node(f"{target_id}-R{suffix}") is not None:
            suffix += 1
        repair_id = f"{target_id}-R{suffix}"
        details = str(
            item.get("details") or item.get("reason") or item.get("goal") or ""
        ).strip()
        target_metadata = dict(target.metadata or {})
        goal = (
            f"修复 {target_id} 已验收交付物中的缺陷（原目标：{target.goal}）。"
            + (f"验证发现：{details[:500]}。" if details else "")
            + f"由 {source_node.node_id} 的验证恢复派生；完成标准与原节点一致。"
        )
        repair_node = {
            "node_id": repair_id,
            "goal": goal,
            "role": target.role,
            "task_type": target_metadata.get("task_type", "implementation"),
            "required_capabilities": list(
                target_metadata.get("required_capabilities", []) or []
            ),
            "dependencies": [target_id],
            "acceptance_criteria": list(
                getattr(target, "acceptance_criteria", None)
                or target_metadata.get("acceptance_criteria", [])
                or []
            ),
            "owned_paths": list(target.owned_paths),
            "output_artifacts": list(target.output_artifacts),
            "metadata": {
                "_is_repair_node": True,
                "_repair_target": target_id,
                "repairs_completed_node": target_id,
                "parent_node_id": source_node.node_id,
                "converted_from_invalid_reopen": {
                    "proposed_status": str(item.get("status") or ""),
                    "source_node": source_node.node_id,
                    "details": details[:500],
                },
            },
        }
        add_nodes = list(getattr(patch, "add_nodes", []) or [])
        add_nodes.append(repair_node)
        patch.add_nodes = add_nodes
        repair_specs.append({
            "repair_id": repair_id,
            "target_id": target_id,
            "owned_paths": set(repair_node["owned_paths"]),
            "output_artifacts": set(repair_node["output_artifacts"]),
        })
        converted = True
        self.trace_recorder.record(
            "governor.verification_recovery.reopen_converted", {
                "node_id": source_node.node_id,
                "completed_producer": target_id,
                "repair_node_id": repair_id,
                "reason": (
                    "completed status is immutable; derived a legal repair "
                    "node instead of reopening the producer"
                ),
            },
        )
    if converted:
        patch.update_nodes = kept_updates
        self._normalize_reverifier_ownership(patch, source_node, repair_specs)
    return patch


def _normalize_reverifier_ownership(self: Orchestrator, patch, source_node, repair_specs):
    """Deconflict converted repair nodes vs the same-patch re-verifier.

    Recovery planners habitually add a ``<source>-R<k>`` re-verifier and
    hand it the faulty producer's files/artifacts as well; the Governor's
    leaf-level ownership check then rejects the whole patch (r11 09:12
    live: converted N-007-R1 and planner-added N-008-R1 both claimed
    docs/database.md and artifact:database_md; three such rejections
    terminalized N-008 a second time).  Responsibility must sit on
    exactly one node each: the converted repair node keeps the producer's
    paths/artifacts, while the re-verifier is re-anchored onto the
    verification evidence owned by the source node — legal because the
    Governor exempts a recovery node's own ``recovery_of`` target from
    the active-owner conflict check — and made to depend on the repair
    node so re-verification reads repaired output, never stale docs.
    """
    if not repair_specs:
        return patch
    repair_ids = [str(spec["repair_id"]) for spec in repair_specs]
    repair_paths: set = set()
    repair_artifacts: set = set()
    for spec in repair_specs:
        repair_paths |= set(spec.get("owned_paths") or set())
        repair_artifacts |= set(spec.get("output_artifacts") or set())
    prefix = f"{source_node.node_id}-R"
    for entry in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("node_id") or entry.get("task_id") or "").strip()
        if not entry_id.startswith(prefix) or entry_id in repair_ids:
            continue
        dependencies = [str(d) for d in (entry.get("dependencies") or [])]
        owned = [str(p) for p in (entry.get("owned_paths") or [])]
        artifacts = [str(a) for a in (entry.get("output_artifacts") or [])]
        overlap_paths = [p for p in owned if p in repair_paths]
        overlap_artifacts = [a for a in artifacts if a in repair_artifacts]
        metadata = dict(entry.get("metadata") or {})
        changed = False
        # r11 10:03 live edge (attempt 1): the planner attached
        # ``recovery_of=N-007`` — the completed faulty producer — to its
        # ``<source>-R<k>`` re-verifier.  The naming normalization skips
        # nodes that already carry a recovery target, and the Governor's
        # ownership conflict check fires before its target-status gate,
        # so the re-verifier was rejected against the still-active source
        # (repair_pending) whose evidence paths it must inherit.  The
        # ``<source>-R<k>`` naming convention is decisive: retarget onto
        # the source node, which is always failed/repair_pending/blocked
        # by the time verification recovery runs — legal under both the
        # Governor's recovery_of exemption and its target-status rule.
        declared_recovery = str(
            entry.get("recovery_of")
            or entry.get("supersedes")
            or metadata.get("recovery_of")
            or ""
        ).strip()
        if declared_recovery != source_node.node_id:
            entry["recovery_of"] = source_node.node_id
            metadata["recovery_of"] = source_node.node_id
            metadata["auto_recovery_of"] = True
            if declared_recovery:
                metadata["recovery_of_retargeted_from"] = declared_recovery
            changed = True
            self.trace_recorder.record(
                "governor.verification_recovery.reverifier_recovery_of_retargeted", {
                    "node_id": source_node.node_id,
                    "reverification_node_id": entry_id,
                    "retargeted_from": declared_recovery,
                },
            )
        if overlap_paths or overlap_artifacts:
            kept_paths = [p for p in owned if p not in repair_paths]
            kept_artifacts = [a for a in artifacts if a not in repair_artifacts]
            stripped = set(overlap_paths)
            for holder in (entry, metadata):
                declared = holder.get("expected_outputs")
                if isinstance(declared, list):
                    holder["expected_outputs"] = [
                        x for x in declared if str(x) not in stripped
                    ]
            if not kept_paths and not kept_artifacts:
                kept_paths = [str(p) for p in (source_node.owned_paths or [])]
                kept_artifacts = [
                    str(a) for a in (source_node.output_artifacts or [])
                ]
                source_expected = [
                    str(x)
                    for x in ((source_node.metadata or {}).get("expected_outputs") or [])
                ]
                if source_expected:
                    metadata["expected_outputs"] = source_expected
                    if "expected_outputs" in entry:
                        entry["expected_outputs"] = source_expected
            entry["owned_paths"] = kept_paths
            entry["output_artifacts"] = kept_artifacts
            metadata["ownership_normalized_from_repair_conflict"] = {
                "stripped_paths": overlap_paths,
                "stripped_artifacts": overlap_artifacts,
                "repair_nodes": repair_ids,
            }
            # r11 10:06 live edge: the planner births its re-verifier with
            # a producer goal ("Regenerate database documentation...") but
            # deconfliction moved producer responsibility to the repair
            # node; the worker then hit the ownership guard on its first
            # write and burned the attempt in a tool loop.  Align the
            # contract: keep the planner's goal text, append the governance
            # scope so the worker knows what it may and must produce.
            goal_text = str(entry.get("goal") or "").strip()
            marker = "[governance-normalized]"
            if goal_text and marker not in goal_text:
                moved = ", ".join(sorted(set(overlap_paths) | set(overlap_artifacts)))
                entry["goal"] = (
                    goal_text
                    + f" {marker} 生产者责任（{moved}）已移交修复节点 "
                    + f"{', '.join(repair_ids)}；本节点仅在其完成后执行重验证："
                    "对照修复后的交付物产出验证证据（限本节点 owned_paths），"
                    "不得修改交付物本身。"
                )
                metadata["responsibility_scope"] = "reverification_only"
            changed = True
        missing_deps = [rid for rid in repair_ids if rid not in dependencies]
        if missing_deps:
            entry["dependencies"] = dependencies + missing_deps
            # Planners may birth the re-verifier as "ready"; get_ready_nodes
            # dispatches purely by status, so a ready-born add would bypass
            # the dependency gate and run BEFORE its repair dependency
            # (r11 10:06:56 live: N-008-R1 executed while N-007-R1 was
            # still admitted).  Route it through admission instead, where
            # promote_waiting_nodes enforces readiness_blockers.
            if str(entry.get("status") or "").strip() == "ready":
                entry["status"] = "admitted"
                metadata["ready_downgraded_for_repair_dependency"] = True
            changed = True
        if changed:
            entry["metadata"] = metadata
            self.trace_recorder.record(
                "governor.verification_recovery.reverifier_ownership_normalized", {
                    "node_id": source_node.node_id,
                    "reverification_node_id": entry_id,
                    "repair_nodes": repair_ids,
                    "stripped_paths": overlap_paths,
                    "stripped_artifacts": overlap_artifacts,
                    "dependencies_added": missing_deps,
                },
            )
    return patch


def _dependency_permanently_dead(self: Orchestrator, dep) -> bool:
    """Fix 19: a dependency that can never reach a satisfying state again.

    Cancelled nodes are never revived; terminal-failed nodes keep their
    markers forever; a superseded node is only "satisfied" while a living
    (or completed) successor carries its lineage forward.
    """
    if dep.status == "cancelled":
        return True
    if dep.status == "failed" and (dep.metadata or {}).get("_terminal_failure"):
        return True
    if dep.status == "superseded":
        # doc 18 修复B：后继链传递遍历。单跳检查在深谱系下误判
        # （attempt3 实证：R-002-R→RECOVER→SSLFIX，直接后继 RECOVER
        # 自身 superseded 即判旧祖先永久死亡，尽管链深处 SSLFIX 在世
        # → 下游 R-003-R 永久 blocked → 重规划无效提案空转）。
        return not self._lineage_has_living_successor(dep.node_id)
    return False


def _lineage_has_living_successor(
    self: Orchestrator, node_id: str, *, _seen: Optional[set] = None,
) -> bool:
    """沿 recovery_of/supersedes_node_id 链传递查找在世后继（防环）。"""
    seen = set() if _seen is None else _seen
    if node_id in seen:
        return False
    seen.add(node_id)
    for other in self.task_graph.nodes.values():
        if other.node_id == node_id:
            continue
        other_meta = other.metadata or {}
        successor_of = str(
            other_meta.get("recovery_of")
            or other_meta.get("supersedes_node_id")
            or ""
        )
        if successor_of != node_id:
            continue
        if other.status not in ("failed", "cancelled", "superseded"):
            return True  # 在世后继
        if other.status == "superseded" and self._lineage_has_living_successor(
            other.node_id, _seen=seen,
        ):
            return True  # 链深处的在世后继
    return False


def _stop_node_services(self: Orchestrator, node_id: str) -> None:
    """doc 18 修复G：停止节点会话启动的后台服务（清理失败不致命，
    只留痕——服务清理是卫生边界，不得反过来杀死主执行链）。"""
    try:
        from AstralNexus.tools.builtin_tools import stop_services_for_node

        stopped = stop_services_for_node(node_id, self.run_dir)
        if stopped:
            self.trace_recorder.record("orchestrator.node_services.stopped", {
                "node_id": node_id,
                "stopped": stopped,
            })
    except Exception as exc:  # pragma: no cover - 卫生边界不致命
        self.trace_recorder.record("orchestrator.node_services.stop_failed", {
            "node_id": node_id,
            "error": str(exc)[:200],
        })


def _rewire_superseded_dependents(self: Orchestrator) -> int:
    """doc 18 修复C：supersede 提交内原子改写下游 depends_on 死边。

    attempt3 实证：替换补丁只加新边不删旧边——R-002-R 被 supersede 后
    R-002-R→R-003-R 死边与 `_blocked_by_terminal_dep=R-002-R` 标记原样
    留存，下游仍指旧生产者，靠"扫描复活"补偿且在多 hop 谱系下失效。
    原生产者→当前生产者→下游依赖满足的链必须在替换提交内统一维护：
    对每条 superseded 谱系链求在世末端，把在世下游节点的 depends_on
    死边改写至该末端（依赖列表同步替换并去重）。evidence_for/
    communicates_to 不动（历史证据归属不可变）。blocked 标记留给既有
    复活检查（死边移除后复活分支自然通过）。幂等：无死边零变更。
    返回改写的下游节点数。
    """
    from .lineage_rewriter import LineageRewriter
    return LineageRewriter(self.task_graph, commit=self.governor.apply_task_graph_patch,
                          record=self.trace_recorder.record).apply()


def _is_takeover_eligible_conflict_owner(self: Orchestrator, node) -> bool:
    """Fix 19 (live r11 resume #30 blocked 20:11): blocked/repair_pending
    owners are takeover-eligible (round 12).  ZOMBIE owners — admitted or
    waiting nodes with a permanently dead dependency (missing, cancelled,
    terminal-failed, or superseded with no living successor) — can never
    become ready, yet they keep holding output ownership from limbo and
    every autonomous recovery proposal touching that output dies on the
    overlapping-ownership validator until the breaker blocks the run
    (live case: G-001-GENERATE-TEST-REPORT admitted with its boundary-test
    deps superseded+cancelled and no successor; three READY-WAVE-5
    proposals owning test_report.json rejected identically).  A zombie is
    therefore a legitimate takeover target; healthy admitted nodes are
    NOT — for them the rejection text remains the teacher.
    """
    if node.status in ("blocked", "repair_pending"):
        return True
    if node.status not in ("admitted", "waiting_dependencies"):
        return False
    for dep_id in node.dependencies:
        dep = self.task_graph.get_node(dep_id)
        if dep is None or self._dependency_permanently_dead(dep):
            return True
    return False


def _is_gated_ready_conflict_owner(self: Orchestrator, node) -> bool:
    """Fix P5#16 (live r17-resume blocked 14:48:58): a ``ready`` node
    that cannot be dispatched because a direct dependency is stuck in
    ``repair_pending``/``blocked`` holds its ownership indefinitely —
    and when the recovery of that very stuck lineage needs the file,
    every takeover proposal died: this stripper removed the root-cause
    claim (foreign to an ACTIVE owner) and root_cause_routing then
    rejected the patch for not addressing the root cause.  Circular
    wait, no legal planner shape: 12 churn events, breaker at
    repeat_count=3 (live: R-CONTRACT-RT-REPAIR ready-gated by
    repair_pending R-UPSTREAM-CONTRACT-REPAIR owns interface.md, the
    declared root cause; six differently-shaped proposals all rejected).

    A gated-ready owner is not a healthy concurrent writer.  Keeping
    the claim on the takeover is safe because the transfer pass below
    strips the duplicated paths from the gated owner through explicit
    ``ownership_transfer_to`` updates, which the governor pre-merges
    into the validation state before add-side ownership checks
    (doc 18 fix H).  The gated node keeps its remaining contract and
    the consumer-rewire pass rebinds it onto the replacement — no role
    is destroyed, the circular wait is cut.  Narrow on purpose: only
    ``ready`` status with a directly stuck dependency qualifies;
    running/leased and healthily-gated owners keep full protection.
    """
    if node.status != "ready":
        return False
    for dep_id in node.dependencies:
        dep = self.task_graph.get_node(dep_id)
        if dep is not None and dep.status in {"repair_pending", "blocked"}:
            return True
    return False


def _strip_foreign_takeover_claims(self: Orchestrator, raw: Dict[str, Any], target_id: str) -> None:
    """Fix 25c: a takeover may claim its target's contract plus
    conflict-free new paths — never another ACTIVE node's files.

    Live r12 resume-2 blocked at iter 36: the planner learned to declare
    recovery_of=A-001 (taught by the rejection feedback) but kept
    claiming deliverables/docs/architecture.md, a file of the healthy
    docs-bundle node D-001. The ownership validator exempts only the
    recovery TARGET from conflicts, so the identical rejection repeated
    to the breaker. Stripping is deterministic and lossless: paths the
    target owns stay (they are the inheritable contract, and active
    duplicate owners lose them through the ownership-transfer pass),
    while foreign claims fall away — the apply-side inheritance walk
    unions the target's real contract back onto the takeover.
    """
    proposal_id = str(raw.get("node_id") or raw.get("task_id") or "").strip()

    def _norm(value: Any) -> str:
        return identity_key(value)

    target = self.task_graph.get_node(target_id)
    target_paths: list[str] = []
    if target is not None:
        target_paths = [
            _norm(item) for item in target.owned_paths if str(item).strip()
        ]

    def _in_target_contract(norm: str) -> bool:
        return any(
            norm == owned
            or norm.startswith(f"{owned.rstrip('/')}/")
            or owned.startswith(f"{norm.rstrip('/')}/")
            for owned in target_paths
        )

    gated_kept: list[str] = []

    def _conflicts_with_active_owner(path: Any) -> bool:
        norm = _norm(path)
        if not norm or _in_target_contract(norm):
            return False
        for existing in self.task_graph.nodes.values():
            if existing.node_id in {target_id, proposal_id}:
                continue
            if existing.status in {"completed", "failed", "cancelled", "superseded"}:
                continue
            matched = False
            for owned in existing.owned_paths:
                owned_norm = _norm(owned)
                if not owned_norm:
                    continue
                if (
                    owned_norm == norm
                    or norm.startswith(f"{owned_norm.rstrip('/')}/")
                    or owned_norm.startswith(f"{norm.rstrip('/')}/")
                ):
                    matched = True
                    break
            if not matched:
                continue
            if self._is_gated_ready_conflict_owner(existing):
                # Fix P5#16: keep the claim — the transfer pass strips the
                # duplicated paths from the gated owner via explicit
                # ownership_transfer_to updates (governor pre-merges them
                # before add validation).  Stripping here instead would
                # leave root_cause_routing with no legal patch shape.
                gated_kept.append(f"{existing.node_id}:{norm}")
                continue
            return True
        return False

    stripped: list[str] = []
    for key in ("owned_paths", "expected_outputs"):
        items = raw.get(key)
        if isinstance(items, list):
            kept: list[Any] = []
            for item in items:
                if _conflicts_with_active_owner(item):
                    stripped.append(str(item))
                else:
                    kept.append(item)
            raw[key] = kept
    raw_metadata = raw.get("metadata")
    if isinstance(raw_metadata, dict) and isinstance(
        raw_metadata.get("expected_outputs"), list
    ):
        kept_meta: list[Any] = []
        for item in raw_metadata["expected_outputs"]:
            if _conflicts_with_active_owner(item):
                stripped.append(str(item))
            else:
                kept_meta.append(item)
        raw_metadata["expected_outputs"] = kept_meta
    if stripped:
        self.trace_recorder.record(
            "governor.patch.foreign_takeover_claims_stripped",
            {
                "node_id": proposal_id,
                "recovery_of": target_id,
                "stripped": stripped,
            },
        )
    if gated_kept:
        # Fix P5#16 observability: a root-cause claim was kept against a
        # gated-ready owner; the transfer pass below moves the ownership.
        self.trace_recorder.record(
            "governor.patch.gated_ready_claim_kept",
            {
                "node_id": proposal_id,
                "recovery_of": target_id,
                "kept": list(dict.fromkeys(gated_kept)),
            },
        )


def _normalize_recovery_ownership_transfer(self: Orchestrator, patch):
    """Transfer exact files/artifacts from an active owner to a recovery node.

    A recovery proposal may correctly take over a failed node while an
    unstarted sibling still carries a stale copy of the same contract.
    Keep the mutation explicit in ``update_nodes`` so the Governor can
    validate the complete graph atomically; never silently drop an owner.
    """
    # Round 12 (live r11 resume #18, blocked 19:21): three stalled rounds
    # each proposed a new security node owning exactly the BLOCKED
    # security node's security_v6/*.json outputs with no recovery_of —
    # rejected three times by the ownership-conflict validation whose own
    # error text teaches the takeover shape ("或在新增节点上显式设置
    # recovery_of=该活跃节点ID 完成接管"), then the run blocked on "same
    # recovery outcome repeated".  The verification path already
    # normalizes this unambiguous takeover (normalized_takeover); do the
    # same here for blocked/repair_pending conflicts so the legal shape
    # is reachable without the provider re-reading rejection text.
    # Ambiguous cases (multiple conflicting owners) stay rejected — the
    # validation message remains the teacher there.
    # Fix 19: takeover eligibility now also covers ZOMBIE admitted/
    # waiting owners (permanently dead dependencies) via
    # _is_takeover_eligible_conflict_owner — see its docstring for the
    # live r11 resume #30 deadlock.

    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("recovery_of") or raw.get("supersedes") or "").strip():
            continue
        proposed_paths = [
            identity_key(item)
            for item in (raw.get("owned_paths") or [])
            if str(item).strip()
        ]
        if not proposed_paths:
            proposed_paths = [
                identity_key(item).strip("/")
                for item in (
                    raw.get("expected_outputs")
                    or (raw.get("metadata") or {}).get("expected_outputs")
                    or []
                )
                if str(item).strip()
                and not str(item).strip().startswith("artifact:")
                and Path(str(item)).suffix
            ]
        proposed_artifacts = {
            str(item).strip()
            for item in (
                raw.get("output_artifacts")
                or (raw.get("metadata") or {}).get("output_artifacts")
                or []
            )
            if str(item).strip()
        }
        if not proposed_paths and not proposed_artifacts:
            continue
        conflicts: list[str] = []
        for existing in self.task_graph.nodes.values():
            if not self._is_takeover_eligible_conflict_owner(existing):
                continue
            existing_paths = [
                identity_key(item) for item in existing.owned_paths
                if str(item).strip()
            ]
            existing_artifacts = {
                str(item).strip() for item in existing.output_artifacts
                if str(item).strip()
            }
            path_hit = any(
                paths_overlap(new, old)
                for new in proposed_paths
                for old in existing_paths
            )
            if path_hit or (proposed_artifacts & existing_artifacts):
                conflicts.append(existing.node_id)
        if len(conflicts) == 1:
            target_id = conflicts[0]
            raw["recovery_of"] = target_id
            raw.setdefault("metadata", {})["recovery_of"] = target_id
            raw.setdefault("metadata", {})["auto_recovery_of"] = True
            self.trace_recorder.record(
                "governor.stalled_recovery.normalized_blocked_takeover",
                {
                    "replacement_node_id": str(
                        raw.get("node_id") or raw.get("task_id") or ""
                    ),
                    "blocked_node_id": target_id,
                    "reason": "unique ownership conflict with blocked/repair_pending node",
                },
            )
        # Fix 23a (live r12 initial-graph double ownership, 23 planning
        # rounds without progress): A-001 failed owning the exact file
        # deliverables/docs/architecture.md while admitted D-001 carried
        # the SAME exact file (seed double ownership). Repair proposals
        # A-001-R1/R2/R3 without recovery_of were rejected forever on the
        # D-001 conflict although the teaching text names the takeover
        # shape. When a proposal re-declares the output contract of
        # exactly ONE failed node, that node is the takeover target even
        # without an explicit recovery_of; the ownership-transfer pass
        # below then strips the duplicated paths from active duplicate
        # owners (D-001) so validation can pass deterministically.
        if str(raw.get("recovery_of") or raw.get("supersedes") or "").strip():
            continue
        proposed_path_set = {identity_key(item) for item in proposed_paths}
        failed_owners: list[str] = []
        for existing in self.task_graph.nodes.values():
            if existing.status != "failed":
                continue
            failed_owner_paths = {
                identity_key(item) for item in existing.owned_paths
                if str(item).strip()
            }
            existing_artifacts = {
                str(item).strip() for item in existing.output_artifacts
                if str(item).strip()
            }
            if (proposed_path_set and proposed_path_set & failed_owner_paths) or (
                proposed_artifacts and proposed_artifacts & existing_artifacts
            ):
                failed_owners.append(existing.node_id)
        if len(failed_owners) == 1:
            target_id = failed_owners[0]
            raw["recovery_of"] = target_id
            raw.setdefault("metadata", {})["recovery_of"] = target_id
            raw.setdefault("metadata", {})["auto_recovery_of"] = True
            self.trace_recorder.record(
                "governor.stalled_recovery.normalized_failed_owner_takeover",
                {
                    "replacement_node_id": str(
                        raw.get("node_id") or raw.get("task_id") or ""
                    ),
                    "failed_node_id": target_id,
                    "reason": (
                        "proposal re-declares the unique failed node's "
                        "output contract without recovery_of"
                    ),
                },
            )
        elif not failed_owners:
            # Fix 25b (live r12 blocked iter 33): the verifier rejected
            # architect A-001 (owns architecture_draft.md) partly for
            # "missing architecture.md" — a file owned by the healthy
            # docs-bundle node D-001. The repair proposals answered that
            # mis-scoped gap by claiming architecture.md, conflicting
            # with D-001 forever (A-001-R1/R2/R3, breaker at 3). A
            # repair must stay inside the failed node's contract: when
            # the proposal ID follows the planner's lineage naming
            # (A-001-R3 for failed A-001), normalize recovery_of to the
            # longest matching failed lineage member and strip claims
            # that conflict with active owners — the apply-side
            # inheritance walk then unions the failed node's real
            # contract (paths, artifacts, delivery ids) back onto the
            # takeover.
            proposal_id = str(raw.get("node_id") or raw.get("task_id") or "").strip()
            prefix_owners = [
                existing.node_id
                for existing in self.task_graph.nodes.values()
                if existing.status == "failed"
                and proposal_id
                and proposal_id != existing.node_id
                and proposal_id.startswith(existing.node_id + "-")
            ]
            if prefix_owners:
                target_id = max(prefix_owners, key=len)
                raw["recovery_of"] = target_id
                raw.setdefault("metadata", {})["recovery_of"] = target_id
                raw.setdefault("metadata", {})["auto_recovery_of"] = True
                self.trace_recorder.record(
                    "governor.stalled_recovery.normalized_lineage_prefix_takeover",
                    {
                        "replacement_node_id": proposal_id,
                        "failed_node_id": target_id,
                        "reason": (
                            "repair proposal claimed active-owner paths outside "
                            "the failed lineage contract; reshaped into takeover"
                        ),
                    },
                )
    # Fix 27 (live r12 iter 46-60 blocked): the planner aimed takeovers
    # at the lineage ROOT (recovery_of=A-001) while the living carrier
    # of the contract was the latest lineage member (A-001-R2,
    # repair_pending). The ownership validator exempts only the declared
    # recovery target, so every proposal conflicted with the current
    # carrier and the rejection rotated to the breaker. Retarget a
    # declared takeover to the LATEST LIVING lineage successor (walk
    # recovery_of/supersedes chains; unambiguous single successors only;
    # extinct lineages keep the declared target). Must run BEFORE the
    # fix-25c foreign-claim strip so the retargeted target's full
    # contract counts as inheritable.
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        declared_target = str(
            raw.get("recovery_of") or raw.get("supersedes") or ""
        ).strip()
        if not declared_target:
            continue
        proposal_id = str(raw.get("node_id") or raw.get("task_id") or "").strip()
        head = declared_target
        latest_living = ""
        seen_heads: set[str] = set()
        while head and head not in seen_heads:
            seen_heads.add(head)
            successors = [
                node for node in self.task_graph.nodes.values()
                if node.node_id != proposal_id
                and str(
                    node.metadata.get("recovery_of")
                    or node.metadata.get("supersedes_node_id")
                    or ""
                ).strip() == head
            ]
            if len(successors) != 1:
                break
            successor = successors[0]
            from AstralNexus.core.lifecycle_policy import RECOVERY_TARGET_STATUSES
            if successor.status in RECOVERY_TARGET_STATUSES:
                latest_living = successor.node_id
            head = successor.node_id
        if latest_living and latest_living != declared_target:
            raw["recovery_of"] = latest_living
            raw_metadata = raw.setdefault("metadata", {})
            if isinstance(raw_metadata, dict):
                raw_metadata["recovery_of"] = latest_living
                raw_metadata["retargeted_from"] = declared_target
            self.trace_recorder.record(
                "governor.stalled_recovery.takeover_retargeted_to_lineage_tip",
                {
                    "replacement_node_id": proposal_id,
                    "declared_target": declared_target,
                    "retargeted_to": latest_living,
                    "reason": (
                        "declared recovery target is not the latest living "
                        "carrier of the lineage contract"
                    ),
                },
            )
    # Fix 25c (live r12 resume-2 blocked iter 36): the planner declared
    # recovery_of=A-001 as taught, but kept claiming architecture.md — a
    # file of the healthy docs-bundle node D-001. The validator exempts
    # only the recovery TARGET from ownership conflicts, so foreign
    # active-owner claims must be stripped from EVERY takeover proposal,
    # declared or normalized, before the transfer pass and validation
    # run. Target-contract paths are kept (active duplicate owners lose
    # them through the transfer pass below).
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        takeover_target = str(
            raw.get("recovery_of") or raw.get("supersedes") or ""
        ).strip()
        if takeover_target:
            self._strip_foreign_takeover_claims(raw, takeover_target)
    updates = list(getattr(patch, "update_nodes", []) or [])
    updated_ids = {str(item.get("node_id") or item.get("task_id") or "") for item in updates if isinstance(item, dict)}
    for raw in list(getattr(patch, "add_nodes", []) or []):
        if not isinstance(raw, dict):
            continue
        recovery_of = str(raw.get("recovery_of") or raw.get("supersedes") or "").strip()
        if not recovery_of:
            continue
        replacement_id = str(raw.get("node_id") or raw.get("task_id") or "").strip()
        # Fix 18: a replacement must not depend on the very node it
        # recovers — that dependency can never be satisfied (the
        # predecessor is failed/terminal), and keeping it stillbirths
        # the takeover through the terminal-dep blocking rule.
        own_dependencies = raw.get("dependencies")
        if isinstance(own_dependencies, list) and recovery_of in own_dependencies:
            raw["dependencies"] = [
                item for item in own_dependencies if item != recovery_of
            ]
            self.trace_recorder.record(
                "governor.patch.recovery_self_dependency_pruned",
                {
                    "node_id": replacement_id,
                    "pruned_dependency": recovery_of,
                },
            )
        if replacement_id:
            for consumer in self.task_graph.nodes.values():
                if consumer.node_id in {recovery_of, replacement_id} or recovery_of not in consumer.dependencies:
                    continue
                update = next((item for item in updates if str(item.get("node_id") or item.get("task_id")) == consumer.node_id), None)
                if update is None:
                    update = {"node_id": consumer.node_id}
                    updates.append(update)
                    updated_ids.add(consumer.node_id)
                update["dependencies"] = list(dict.fromkeys(
                    replacement_id if dependency == recovery_of else dependency
                    for dependency in update.get("dependencies", consumer.dependencies)
                ))
        new_paths = {str(item).replace("\\", "/").rstrip("/") for item in raw.get("owned_paths", []) or []}
        if not new_paths:
            new_paths = {
                str(item).replace("\\", "/").strip("/")
                for item in (
                    raw.get("expected_outputs", [])
                    or (raw.get("metadata") or {}).get("expected_outputs", [])
                )
                if str(item).strip()
                and not str(item).strip().startswith("artifact:")
                and Path(str(item)).suffix
            }
        new_artifacts = {str(item).strip() for item in raw.get("output_artifacts", []) or []}
        if not new_artifacts:
            new_artifacts = {
                str(item).strip()
                for item in (raw.get("metadata") or {}).get("output_artifacts", []) or []
                if str(item).strip()
            }
        if not new_paths and not new_artifacts:
            continue
        for existing in self.task_graph.nodes.values():
            if existing.node_id in {recovery_of, str(raw.get("node_id") or "")}:
                continue
            if existing.status in {"completed", "failed", "cancelled", "superseded"}:
                continue
            # Fix 23a: a prior consumer-rewire update for the SAME active
            # node must not shield its duplicated ownership from the
            # strip — merge into it instead of skipping, or the validator
            # still sees the double claim and rejects the takeover
            # forever (live r12: D-001 was both a consumer of failed
            # A-001 and a duplicate owner of architecture.md).
            prior = next(
                (update for update in updates
                 if update.get("node_id") == existing.node_id),
                None,
            )
            base_paths = prior.get("owned_paths", existing.owned_paths) if prior else existing.owned_paths
            base_artifacts = prior.get("output_artifacts", existing.output_artifacts) if prior else existing.output_artifacts
            kept_paths = [path for path in base_paths
                          if str(path).replace("\\", "/").rstrip("/") not in new_paths]
            kept_artifacts = [artifact for artifact in base_artifacts
                              if str(artifact).strip() not in new_artifacts]
            if len(kept_paths) == len(base_paths) and len(kept_artifacts) == len(base_artifacts):
                # Fix P5#16b (live resume#2 attempt 2, 15:34:43): the
                # planner's own update_nodes entry may already strip the
                # duplicated paths from this active owner (the rejection
                # text teaches update_nodes usage).  base_paths is then
                # the prior's kept list, so nothing is left to strip —
                # but without the ownership_transfer_to marker the
                # governor pre-merge (doc 18 fix H) ignores the update
                # and add validation still sees the double claim,
                # rejecting an otherwise legal takeover.  Mark it.
                if prior is not None:
                    real_paths = {
                        str(path).replace("\\", "/").rstrip("/")
                        for path in existing.owned_paths
                        if str(path).strip()
                    }
                    real_artifacts = {
                        str(artifact).strip()
                        for artifact in existing.output_artifacts
                        if str(artifact).strip()
                    }
                    if (real_paths & new_paths) or (real_artifacts & new_artifacts):
                        prior.setdefault("metadata", {})["ownership_transfer_to"] = str(raw.get("node_id") or "")
                        updated_ids.add(existing.node_id)
                continue
            if prior is not None:
                prior["owned_paths"] = kept_paths
                prior["output_artifacts"] = kept_artifacts
                prior.setdefault("metadata", {})["ownership_transfer_to"] = str(raw.get("node_id") or "")
                updated_ids.add(existing.node_id)
                continue
            if existing.node_id not in updated_ids:
                updates.append({
                    "node_id": existing.node_id,
                    "owned_paths": kept_paths,
                    "output_artifacts": kept_artifacts,
                    "metadata": {"ownership_transfer_to": str(raw.get("node_id") or "")},
                })
                updated_ids.add(existing.node_id)
    patch.update_nodes = updates
    return patch


def _downstream_consumers(self: Orchestrator, node_id: str) -> List[str]:
    """Direct consumers of ``node_id`` — the nodes a replan has to unblock."""
    return [
        candidate.node_id
        for candidate in self.task_graph.nodes.values()
        if candidate.node_id != node_id
        and node_id in (candidate.dependencies or [])
    ]


def _escalate_invalid_recovery_proposal(self: Orchestrator, node, failed_node_id: str) -> bool:
    """Bounded immediate replan after a malformed recovery proposal (F3).

    Live b31 (2026-10-05): N-202 failed at 22:02:08, the governor's recovery
    proposal was malformed (``governor.fault_recovery.invalid_proposal``),
    and the run then idled until 22:16:58 — 33 iterations and ~15 minutes of
    blocked consumers — before the stalled-replan path fired on its own.
    Escalating once per node turns that dead window into a governed replan;
    the one-shot marker keeps the escalation from becoming its own loop.
    """
    if node.metadata.get("_invalid_recovery_proposal_replanned"):
        return False
    node.metadata["_invalid_recovery_proposal_replanned"] = True
    # _replan_stalled_graph derives the failure dossier from the *failed
    # dependencies of blocked_nodes*; handing it the failed node itself would
    # describe that node's own (usually completed) producers instead.
    blocked_nodes = self._downstream_consumers(failed_node_id) or [failed_node_id]
    goal = (
        getattr(getattr(self.governor, "goal_spec", None), "task_goal", "")
        or str(getattr(node, "goal", "") or "")
    )
    try:
        result = self._replan_stalled_graph(
            goal=goal,
            blocked_nodes=blocked_nodes,
            reason="invalid_recovery_proposal",
        )
    except Exception as exc:  # a failed replan must not crash the recovery path
        self.trace_recorder.record(
            "orchestrator.invalid_recovery_proposal.replan_failed",
            {"node_id": failed_node_id, "error": str(exc)},
        )
        return False
    self.trace_recorder.record(
        "orchestrator.invalid_recovery_proposal.replanned",
        {
            "node_id": failed_node_id,
            "blocked_nodes": blocked_nodes,
            "changed": bool(result.get("changed")),
            "repeat_count": int(result.get("repeat_count", 0) or 0),
            "reason": str(result.get("reason") or ""),
        },
    )
    return bool(result.get("changed"))


@recovery_operation
def _attempt_recovery(self: Orchestrator, failed_node_id: str) -> bool:
    """Attempt to recover from a failed task using FaultRecoveryEngine.

    Unified fault recovery entry point (天枢方案书 §5.6):
    1. Detect fault using FaultDetector
    2. Delegate to FaultRecoveryEngine for complete recovery pipeline

    FaultRecoveryEngine encapsulates impact analysis, recovery planning,
    and action execution. This method only handles orchestration-level
    concerns like terminal failure checks and retry counting.

    Args:
        failed_node_id: The ID of the failed node

    Returns:
        True if recovery was successful, False otherwise
    """
    node = self.task_graph.get_node(failed_node_id)
    if not node:
        return False

    # Pre-flight: block recovery for terminal-failure nodes
    if node.metadata.get("_terminal_failure") or node.metadata.get("_capability_gap_exhausted"):
        self.trace_recorder.record("recovery.blocked_terminal", {
            "node_id": failed_node_id,
            "reason": node.metadata.get("_terminal_reason", "terminal_failure"),
        })
        return False

    # ``retry_count`` is strategy-local.  Formal runs do not impose a
    # retry/recovery round cap; the Governor must change the worker,
    # tools, context, or graph before another attempt.  Keep cumulative
    # diagnostics for audit, but never convert repetition into a silent
    # terminal failure.
    failure = node.metadata.get("last_failure", {}) or {}
    fingerprint = "|".join(
        str(value or "").strip().lower()
        for value in (
            failure.get("error_type"),
            node.metadata.get("error_type"),
            failure.get("execution_path"),
        )
        if str(value or "").strip()
    ) or "execution_failure"
    fingerprints = dict(node.metadata.get("recovery_failure_fingerprints", {}) or {})
    cumulative_attempts = int(fingerprints.get(fingerprint, 0)) + 1
    fingerprints[fingerprint] = cumulative_attempts
    node.metadata["recovery_failure_fingerprints"] = fingerprints
    node.metadata["recovery_attempts_total"] = int(
        node.metadata.get("recovery_attempts_total", 0)
    ) + 1
    if cumulative_attempts > 1:
        self.trace_recorder.record("recovery.repeat_observed", {
            "node_id": failed_node_id,
            "fingerprint": fingerprint,
            "repeat_count": cumulative_attempts,
            "recovery_attempts_total": node.metadata["recovery_attempts_total"],
            "action": "governor_replan_required",
        })

    recovery_attempt_payload = {
        "node_id": failed_node_id,
        "role": node.role,
        "retry_count": node.retry_count,
        "failure_fingerprint": fingerprint,
        "repeat_count": cumulative_attempts,
        "recovery_attempts_total": node.metadata["recovery_attempts_total"],
    }
    self.trace_recorder.record("recovery.attempted", recovery_attempt_payload)

    # Detect fault (first step of recovery pipeline)
    fault = self.fault_detector.detect_node_failure(
        failed_node_id,
        f"Node {failed_node_id} execution failed after {node.retry_count} attempts"
    )
    # Recovery must see the actual failure, not just the scheduler's
    # generic boolean result. In particular, repeated capability gaps
    # cannot be repaired by blindly repeating the same reassignment.
    fault.payload.update({
        "last_failure": _json_safe(failure),
        "worker_selection": _json_safe(node.metadata.get("governor_worker_selection", {})),
        "approved_worker_type": node.metadata.get("approved_worker_type"),
        "failure_fingerprint": fingerprint,
        "repeat_count": cumulative_attempts,
        "reassignment_count": node.metadata.get("reassignment_count", 0),
    })

    governed = self._execute_governed_fault(fault)
    recovery_success = bool(governed["success"])
    if (
        not recovery_success
        and str(((governed.get("plan") or {}).get("strategy")) or "")
        == "invalid_proposal"
    ):
        # F3 (live b31): the malformed proposal already consumed churn and was
        # reported; without this escalation the run fell back to the main loop
        # and idled until the stalled-replan path noticed, minutes later.
        self._escalate_invalid_recovery_proposal(node, failed_node_id)

    if recovery_success:
        self.trace_recorder.record("recovery.completed", {
            "node_id": failed_node_id,
            "fault_id": fault.fault_id,
            "retry_count": node.retry_count,
        })
    else:
        self.trace_recorder.record("recovery.failed", {
            "node_id": failed_node_id,
            "fault_id": fault.fault_id,
            "retry_count": node.retry_count,
        })

    return recovery_success


