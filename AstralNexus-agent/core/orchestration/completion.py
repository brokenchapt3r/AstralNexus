"""Completion responsibilities on the shared orchestration context."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .lineage_index import LineageIndex
from .support import (
    Any,
    Dict,
    List,
    Path,
    TaskStatus,
    _delivery_evidence_visible,
    json,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from AstralNexus.core.governor import TaskGraphPatch


def is_execution_complete(self: Orchestrator) -> bool:
    """Check whether the currently admitted subgraph is terminal.

    Returns:
        True when all current nodes have converged successfully or were
        explicitly superseded/cancelled.  This does not authorize final
        delivery; Global Verifier and Synthesizer own that later gate.
    """
    lineage = LineageIndex.build(self.task_graph)
    for node in self.task_graph.nodes.values():
        if node.status in ("completed", "superseded", "cancelled"):
            continue
        # Fix AU（doc 18, r13b, 2026-09-17）：被 completed 后继接管的
        # failed 节点是"死记录"，不得永久挡住收敛闸门。
        #
        # 实测：r13b 跑到第 28 代仍未返回 completed，根因就是这里——图里
        # 永久留着三个 failed 节点（PATCH-R002-E2E / PATCH-R003-RUNTIME /
        # PATCH-R005-RUNTIME），而它们的交付契约早已被 completed 后继整体
        # 接管（R002-E2E → … → PATCH-R006-E2E completed 0.995；
        # R003/R005-RUNTIME → … → PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE
        # completed 0.983）。Governor 自己在 09-16 03:25 的 patch 里写明
        # "属被取代的死记录而非可重试工作单元，故按取消被替代节点处理"，
        # 但该 patch 的 cancelled 为空、意图未落地，于是本函数恒为 False，
        # _assess_delivery_completion 一次都没有被调用过（trace 内
        # governor.execution_completion.assessed 事件数为 0），交付闸门
        # 永远走不到，运行只能在重规划环里烧预算。
        #
        # 口径从严：只豁免 status=="failed" 且其谱系**确有 completed
        # 后继**者。真正无后继的失败、以及后继尚未完成的失败（admitted/
        # running/ready/…）仍然阻塞收敛，不把失败粉饰为完成；最终是否交付
        # 仍由 Global Verifier + Synthesizer 闸门决定。
        if node.status == "failed" and lineage.has_completed_carrier(self.task_graph, node.node_id):
            if node.node_id not in self._absorbed_dead_records:
                self._absorbed_dead_records.add(node.node_id)
                self.trace_recorder.record(
                    "orchestrator.lineage.absorbed_failure",
                    {
                        "node_id": node.node_id,
                        "reason": "failed_node_contract_carried_by_completed_successor",
                        "reason_codes": ["LINEAGE_TAKEOVER_COMPLETED"],
                    },
                )
            continue
        return False  # Not all nodes are in terminal state

    return True


def _lineage_has_completed_carrier(self: Orchestrator, node_id: str) -> bool:
    """Fix AU：该节点的交付契约是否已被某个 completed 后继接管。

    沿 ``recovery_of`` / ``supersedes_node_id`` 的后继闭包行走（防环），
    与 ``_rewire_superseded_dependents.living_tail`` 的单路径行走不同：
    r13b 实测谱系会分叉（PATCH-R005-RUNTIME 同时派生 PATCH-R006-RUNTIME
    与 PATCH-R010-SERVE-ENTRY，只有后者那一支走到 completed 的
    PATCH-R012-SERVE-ENTRY-LEDGER-FREEZE），单路径会在死支上返回 None，
    从而误判"无 completed 后继"。这里取闭包，任一分支到达 completed 即真。

    中间代状态（superseded / failed / cancelled）不中断行走；只有真正
    走到 completed 才返回 True，链尾停在非 completed 者一律为 False。
    """
    return LineageIndex.build(self.task_graph).has_completed_carrier(self.task_graph, node_id)


def _delivery_progress_snapshot(self: Orchestrator) -> tuple[str, List[str]]:
    """Build a bounded, artifact-backed view for Global Verifier."""
    refs: List[str] = []
    run_root = self.run_dir.resolve()

    def canonical_ref(raw: Any) -> str:
        path = Path(str(raw))
        candidate = path if path.is_absolute() else self.run_dir / path
        try:
            return candidate.resolve().relative_to(run_root).as_posix()
        except (OSError, ValueError):
            return str(candidate.resolve())

    for artifact in self.task_graph.artifacts.values():
        if artifact.path:
            refs.append(canonical_ref(artifact.path))
    # Explicit instance injection wins; the one documented legacy replacement
    # hook is read dynamically so facade patching cannot silently be ignored.
    from AstralNexus.core import orchestrator as compatibility
    visible = getattr(self, "delivery_evidence_visible", None) or compatibility._delivery_evidence_visible
    deliverables = self.run_dir / "deliverables"
    if deliverables.is_dir():
        refs.extend(
            canonical_ref(path)
            for path in deliverables.rglob("*")
            if path.is_file() and visible(path)
        )
    refs = list(dict.fromkeys(refs))
    lines = ["# Runtime delivery progress", "", "## Task nodes"]
    for node in self.task_graph.nodes.values():
        lines.append(
            f"- {node.node_id} [{node.status}] "
            f"worker={(node.metadata.get('worker_profile') or {}).get('worker_type', node.role)} "
            f"goal={node.goal}"
        )
    lines.extend(["", "## Materialized artifacts"])
    for ref in refs:
        lines.append(f"- {ref}")

    # Put compact verification evidence before large source documents.  The
    # delivery verifier intentionally consumes a bounded prefix, so a
    # filesystem traversal order must never hide the newest test report or
    # evidence manifest behind a large generated API document.
    evidence_markers = (
        "evidence", "report", "result", "test", "health", "server",
        "manifest", "tree", "验收", "测试", "证据", "日志",
        # Fix AV: sealed final-round vocabulary.  A sealed acceptance
        # re-run ("acceptance-rerun.json") carries no marker from the
        # original list, so it sorted into the unprioritised tail and its
        # content never reached the verifier.
        "acceptance", "rerun", "freeze", "convergence", "verification",
        "audit", "自检", "冻结", "交付",
    )

    def _freshness(ref: str) -> float:
        """mtime of the referenced file, or 0.0 when it cannot be stat'ed.

        Fix AV: inside a priority tier the newest evidence is the most
        authoritative — a re-run supersedes every earlier run — so plain
        alphabetical order must not decide what the bounded budget shows.
        A vanished path sorts last instead of raising.
        """
        candidate = Path(ref)
        path = candidate if candidate.is_absolute() else self.run_dir / candidate
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    ordered_refs = sorted(
        refs,
        key=lambda ref: (
            0 if any(marker in ref.casefold() for marker in evidence_markers) else 1,
            -_freshness(ref),
            ref.casefold(),
        ),
    )
    lines.extend(["", "## Prioritized artifact evidence"])
    remaining = 220_000
    for ref in ordered_refs:
        raw_path = Path(ref)
        path = raw_path if raw_path.is_absolute() else self.run_dir / raw_path
        if (
            remaining > 0
            and path.is_file()
            and path.suffix.lower() in {
                ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml",
                ".html", ".log", ".csv",
            }
        ):
            try:
                sample = path.read_text(encoding="utf-8", errors="ignore")[
                    : min(16_000, remaining)
                ]
            except OSError:
                continue
            lines.append(f"\n### Artifact: {ref}")
            lines.append(sample)
            remaining -= len(sample)
    return "\n".join(lines), refs


def _completed_synthesizer_nodes(self: Orchestrator) -> List[str]:
    return [
        node.node_id
        for node in self.task_graph.nodes.values()
        if node.status == TaskStatus.COMPLETED.value
        and (
            str((node.metadata.get("worker_profile") or {}).get("worker_type", "")).lower()
            == "synthesizer"
            or str(node.role).lower() == "synthesizer"
        )
    ]


def _assess_delivery_completion(self: Orchestrator, goal: str) -> Dict[str, Any]:
    """Execution frontier is complete; presentation belongs to application service."""
    return {"authorized": True, "reason": "execution_complete_delivery_pending", "completion_scope": "execution"}


def _converge_runtime_state_after_terminal(self: Orchestrator, node_id: str, reason: str) -> None:
    """Synchronously converge run state after a node terminalizes.

    Live r11 (resume #10, both terminalizations): ``_terminal_failure``
    was written into node metadata mid-wave, but the runtime_state
    snapshot kept reporting the pre-terminal truth for minutes (monitor
    and resume both observed stale ``ready:1 / failed:7``), dependents
    were not re-evaluated until a later wave scan, and the stalled
    replan only fired after long unrelated silent preambles.  A
    terminalization is a graph-truth change: dependent blocking, the
    observable snapshot, and the durable checkpoint must converge AT
    THAT POINT instead of at the next iteration boundary.  Recovery and
    replan decisions still belong to the Governor — this method changes
    no node status beyond persisting the terminal-dep blocking that
    ``_check_for_blocked_tasks`` already proves, and every step is
    isolated so convergence friction can never mask the terminal state.
    """
    try:
        blocked = self._check_for_blocked_tasks()
    except Exception as exc:  # pragma: no cover - defensive isolation
        blocked = []
        self.trace_recorder.record(
            "orchestrator.terminal_convergence.blocked_scan_failed",
            {"node_id": node_id, "error": str(exc)[:300]},
        )
    state = self.runtime_state_manager.state
    try:
        self.runtime_state_manager.observe(
            task_graph=self.task_graph,
            global_goal=(
                self.governor.goal_spec.task_goal
                if self.governor.goal_spec else state.global_goal
            ),
            task_version=self.governor.goal_version,
            iteration=self.iteration_count,
            phase=state.phase or "executing",
            run_status=state.run_status or "running",
            lifecycle_manager=self.lifecycle_manager,
            memory_federation=self.memory_federation,
            resource_scheduler=self.resource_scheduler,
            decision={
                "action": "terminal_state_converged",
                "node_id": node_id,
                "reason": reason,
                "blocked_dependents": list(blocked),
            },
        )
    except Exception as exc:  # pragma: no cover - defensive isolation
        self.trace_recorder.record(
            "orchestrator.terminal_convergence.snapshot_failed",
            {"node_id": node_id, "error": str(exc)[:300]},
        )
    try:
        self._persist_graph_checkpoint(stage="node_terminal", node_id=node_id)
    except Exception as exc:  # pragma: no cover - defensive isolation
        self.trace_recorder.record(
            "orchestrator.terminal_convergence.checkpoint_failed",
            {"node_id": node_id, "error": str(exc)[:300]},
        )
    self.trace_recorder.record(
        "orchestrator.terminal_convergence.converged",
        {
            "node_id": node_id,
            "reason": reason,
            "blocked_dependents": list(blocked),
        },
    )


