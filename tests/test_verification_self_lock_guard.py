from types import SimpleNamespace

from AstralNexus.core.governor import TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator


def _detector():
    # Only the detector is exercised here: constructing a full Orchestrator
    # requires a task graph, recorder and provider wiring.
    return Orchestrator.__new__(Orchestrator)


def test_self_lock_reason_codes_are_detected():
    """Fix P (doc 18 attempt8 layer-2 audit): the verification recovery planner
    can declare that its own evidence is stale and unfreshable by this premise.
    Those codes must be detected so the run stops instead of growing RN-301..
    RN-903 while nothing changes."""
    patch = TaskGraphPatch(
        reason_codes=[
            "VERIFICATION_GAP",
            "STALE_EXECUTION_EVIDENCE",
            "PREMISE_REFUTED_READ_ONLY_PASS_CANNOT_REFRESH_EVIDENCE",
            "SCHEME_CHANGE_FROZEN_BYTE_IDENTITY",
            "UNRELATED_CODE",
        ],
    )
    codes = _detector()._verification_self_lock_codes(patch)
    assert "STALE_EXECUTION_EVIDENCE" in codes
    assert any("CANNOT_REFRESH_EVIDENCE" in code for code in codes)
    assert any("FROZEN_BYTE_IDENTITY" in code for code in codes)
    assert "UNRELATED_CODE" not in codes


def test_ordinary_recovery_patch_reports_no_self_lock():
    patch = TaskGraphPatch(reason_codes=["VERIFICATION_GAP", "ROOT_CAUSE_ROUTED"])
    assert _detector()._verification_self_lock_codes(patch) == []


# --- DeerFlow 2.0 对齐：按**证据**而不是按"有没有加节点"判进展 --------------------
# 线上实证（r13b）：验证恢复补丁每次都在加节点，于是旧判据 ``not added_nodes``
# 从不成立，no-progress 制动从未触发，长出了 R003..R009 七代运行时自证与
# R016..R021 六代终验。DeerFlow 的分层里验收是就地判定、不可判定即 UNVERIFIED、
# 绝不为"再验一次"扩建工作图——因此只有声明了图上还没有的交付物/证据才算推进。


def _orchestrator_with_existing(paths=(), artifacts=()):
    orch = Orchestrator.__new__(Orchestrator)
    nodes = {}
    for index, path in enumerate(paths):
        nodes[f"N-{index:03d}"] = SimpleNamespace(
            owned_paths=[path], output_artifacts=list(artifacts) if index == 0 else []
        )
    if not paths:
        nodes["N-000"] = SimpleNamespace(owned_paths=[], output_artifacts=list(artifacts))
    orch.task_graph = SimpleNamespace(nodes=nodes)
    return orch


def test_added_node_declaring_a_new_deliverable_is_progress():
    """新交付物 = 真进展，不能被误判成自锁。"""
    orch = _orchestrator_with_existing(["deliverables/app/main.py"])
    assert (
        orch._added_nodes_claim_new_evidence(
            [{"node_id": "R-100", "owned_paths": ["deliverables/app/serve.py"]}]
        )
        is True
    )


def test_added_node_reclaiming_existing_ownership_is_not_progress():
    """只重新验证同一批既有归属 = 重跑不可判定前提 = 自锁（即使新增了节点）。"""
    orch = _orchestrator_with_existing(["deliverables/app/main.py"])
    assert (
        orch._added_nodes_claim_new_evidence(
            [{"node_id": "R-101", "owned_paths": ["deliverables/app/main.py"]}]
        )
        is False
    )


def test_added_node_declaring_nothing_is_not_progress():
    orch = _orchestrator_with_existing(["deliverables/app/main.py"])
    assert orch._added_nodes_claim_new_evidence([{"node_id": "R-102"}]) is False


def test_added_node_declaring_a_fresh_evidence_artifact_is_progress():
    orch = _orchestrator_with_existing(
        ["deliverables/app/main.py"], ["artifact:existing"]
    )
    assert (
        orch._added_nodes_claim_new_evidence(
            [{"node_id": "R-103", "output_artifacts": ["artifact:fresh-proof"]}]
        )
        is True
    )