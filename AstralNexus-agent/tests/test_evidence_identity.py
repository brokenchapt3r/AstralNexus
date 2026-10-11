"""证据身份（b33 case1 fix10 §7）：版本识别 / 执行追溯 / 原始验收项，三者分开。

本文件锁住本批第 1 步要求证明的两件事：

* **P-111 转绿可计功** —— 接管节点带来**新内容版本**时，进展要计一次；
* **同一证据跨节点传递只计一次** —— 内容相同（经多个接管节点转手）只算一份。

以及评审给的两条约束：

* **哈希不等于证据身份** —— 同内容来自不同执行时，版本 key 相同而
  ``source_execution`` 不同，两者不得混为一谈；
* **验收项身份必须跨接管稳定** —— 用契约逻辑 id（``artifact:…``）锚定，
  而不是随节点更换的 ``criterion_id``；
* **缺失即不可判定** —— 缺哈希/缺执行/缺验收项一律 ``decidable=False``，
  且不得用现有内容倒推历史版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _Node:
    node_id: str
    metadata: dict = field(default_factory=dict)


@dataclass
class _Artifact:
    path: str


def _write(tmp_path: Path, name: str, text: str) -> str:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return str(target)


_SATISFIED = ({"criterion": "tests_passed:python verify_case.py", "kind": "tests_passed",
               "checked": True, "holds": True, "receipt_ids": ["r0001"]},)


def _seal(tmp_path, **overrides):
    from AstralNexus.runtime import evidence_identity as ei

    kwargs = {
        "node_id": "P-111",
        "nodes": {},
        "accepted_artifacts": [_Artifact(_write(tmp_path, "dual_run_evidence.json", "v1"))],
        "contract_ids": ["artifact:dual-run-evidence"],
        "execution_id": "exec-000010",
        "receipt_ids": ["r0001"],
        "run_dir": tmp_path,
        "acceptance_observations": _SATISFIED,
    }
    kwargs.update(overrides)
    return ei.build_evidence_seal(**kwargs)


# ---------------------------------------------------------------- (a) 计功


def test_evidence_backed_acceptance_item_earns_one_credit(tmp_path):
    """P-111 转绿：接管节点让原验收项**获得证据支撑** ⇒ 计一次进展。"""
    from AstralNexus.runtime import evidence_identity as ei

    seal = _seal(tmp_path)
    assert seal["decidable"] is True
    assert seal["credit_eligible"] is True
    assert seal["satisfied_acceptance_items"] == ["tests_passed:python verify_case.py"]
    assert seal["requirements"] == ["artifact:dual-run-evidence"]

    decision = ei.credit_decision(seal, already_credited=[])

    assert decision["credited"] is True
    assert decision["reason"] == "acceptance_item_now_evidence_backed"
    assert decision["satisfied_acceptance_items"] == [
        "tests_passed:python verify_case.py"
    ]
    assert decision["evidence_versions"][0]["content_sha256"]


def test_counterexample_identical_bytes_but_only_the_second_run_is_evidence_backed(
    tmp_path,
):
    """业主反例：**文件字节不变**，第一次没有有效复验，第二次真跑两遍把缺口关掉。

    此时必须认可一次进展；旧判据（逻辑 id + 文件哈希）无法区分它与纯转手。
    新判据看**验收项的状态变化**：第一次无成立项 ⇒ 不给 key；第二次成立 ⇒ 计一次。
    """
    from AstralNexus.runtime import evidence_identity as ei

    same = _write(tmp_path, "deliverables/verify_case.py", "identical bytes")
    not_yet = ei.build_evidence_seal(
        node_id="P-104", nodes={}, accepted_artifacts=[_Artifact(same)],
        contract_ids=["artifact:verify-case-script"], execution_id="exec-1",
        run_dir=tmp_path,
        acceptance_observations=[{
            "criterion": "tests_passed:python deliverables/verify_case.py",
            "kind": "tests_passed", "checked": True, "holds": False,
            "reason": "no anchored execution receipt",
        }],
    )
    closed = ei.build_evidence_seal(
        node_id="P-111", nodes={"P-111": _Node("P-111", {"recovery_of": "P-104"}),
                                "P-104": _Node("P-104")},
        accepted_artifacts=[_Artifact(same)],
        contract_ids=["artifact:verify-case-script"], execution_id="exec-2",
        run_dir=tmp_path,
        acceptance_observations=[{
            "criterion": "tests_passed:python deliverables/verify_case.py",
            "kind": "tests_passed", "checked": True, "holds": True,
            "receipt_ids": ["r-run1", "r-run2"],
        }],
    )

    # 第一次：字节与第二次完全一致，但没有成立项 ⇒ 没有进展身份。
    assert not_yet["evidence_versions"] == closed["evidence_versions"]  # 同一份内容
    assert not_yet["credit_eligible"] is False
    assert not_yet["credit_key"] is None
    assert ei.credit_decision(not_yet, []) == {
        "credited": False,
        "reason": "no_acceptance_item_satisfied",
        "undecidable_reasons": [],
        "credit_key": "",
        "requirements": ["artifact:verify-case-script"],
        "satisfied_acceptance_items": [],
        "node_scoped_criterion_ids": [],
        "evidence_versions": not_yet["evidence_versions"],
        "source_execution": {"execution_id": "exec-1", "receipt_ids": []},
        "lineage_root": "P-104",
    }

    # 第二次：真跑两遍 ⇒ 原验收项获得证据支撑 ⇒ 计一次。
    decision = ei.credit_decision(closed, [])
    assert decision["credited"] is True
    assert decision["lineage_root"] == "P-104"        # 映射回原始任务
    assert decision["source_execution"]["receipt_ids"] == ["r-run1", "r-run2"]


def test_same_evidence_relayed_through_a_takeover_counts_once(tmp_path):
    """同一份证据跨节点传递：验收项早已成立 ⇒ key 相同 ⇒ 第二次不计功。

    fix10 现场形状：P-104 → P-109 → P-110 → P-112 → P-115 → P-116 → P-117
    依次改写同一个 ``deliverables/verify_case.py``。
    """
    from AstralNexus.runtime import evidence_identity as ei

    same = _write(tmp_path, "verify_case.py", "print('identical')")
    first = ei.build_evidence_seal(
        node_id="P-110", nodes={}, accepted_artifacts=[_Artifact(same)],
        contract_ids=["artifact:verify-case-script"], execution_id="exec-000008",
        run_dir=tmp_path, acceptance_observations=_SATISFIED,
    )
    relayed = ei.build_evidence_seal(
        node_id="P-116", nodes={}, accepted_artifacts=[_Artifact(same)],
        contract_ids=["artifact:verify-case-script"], execution_id="exec-000018",
        run_dir=tmp_path, acceptance_observations=_SATISFIED,
    )

    assert first["credit_key"] == relayed["credit_key"]
    assert ei.credit_decision(first, [])["credited"] is True
    second = ei.credit_decision(relayed, [first["credit_key"]])
    assert second["credited"] is False
    assert second["reason"] == "same_acceptance_item_already_credited"
    # 追溯信息仍在：同一份证据来自两次不同的真实执行。
    assert second["source_execution"]["execution_id"] == "exec-000018"


def test_changed_content_without_a_changed_acceptance_item_earns_nothing(tmp_path):
    """**内容变化本身不自动计功**：验收项没变 ⇒ key 不变 ⇒ 不再计一次。"""
    from AstralNexus.runtime import evidence_identity as ei

    v1 = ei.build_evidence_seal(
        node_id="P-112", nodes={},
        accepted_artifacts=[_Artifact(_write(tmp_path, "verify_case.py", "v1"))],
        contract_ids=["artifact:verify-case-script"], execution_id="e1",
        run_dir=tmp_path, acceptance_observations=_SATISFIED,
    )
    v2 = ei.build_evidence_seal(
        node_id="P-117", nodes={},
        accepted_artifacts=[_Artifact(_write(tmp_path, "verify_case.py", "v2-fixed"))],
        contract_ids=["artifact:verify-case-script"], execution_id="e2",
        run_dir=tmp_path, acceptance_observations=_SATISFIED,
    )

    assert v1["evidence_versions"] != v2["evidence_versions"]   # 内容确实变了
    assert v1["credit_key"] == v2["credit_key"]                 # 验收项没变
    assert ei.credit_decision(v2, [v1["credit_key"]])["credited"] is False


def test_a_new_acceptance_item_later_earns_its_own_credit(tmp_path):
    """真正的新验收项（例如 自测报告.md 那条）应当各自计一次。"""
    from AstralNexus.runtime import evidence_identity as ei

    first = _seal(tmp_path)
    later = _seal(
        tmp_path,
        acceptance_observations=list(_SATISFIED) + [{
            "criterion": "file_written:自测报告.md", "kind": "file_written",
            "checked": True, "holds": True,
        }],
    )

    assert first["credit_key"] != later["credit_key"]
    assert ei.credit_decision(later, [first["credit_key"]])["credited"] is True


def test_node_scoped_criterion_id_cannot_mint_progress(tmp_path):
    """接管节点只有节点作用域的 criterion_id ⇒ 不得据此制造进展。"""
    from AstralNexus.runtime import evidence_identity as ei

    seal = _seal(
        tmp_path,
        acceptance_observations=[{
            "criterion_id": "P-111.EO01", "kind": "file_written",
            "checked": True, "holds": True,
        }],
    )

    assert seal["credit_eligible"] is False
    assert seal["node_scoped_criterion_ids"] == ["P-111.EO01"]
    decision = ei.credit_decision(seal, [])
    assert decision["credited"] is False
    assert decision["reason"] == "node_scoped_item_is_not_a_requirement"
    assert ei.UNDECIDABLE_NODE_SCOPED_ITEM in seal["undecidable_reasons"]


# ------------------------------------------------ 哈希 != 证据身份（执行追溯）


def test_hash_and_execution_are_recorded_separately(tmp_path):
    """同内容、不同执行：版本 key 相同，但 source_execution 必须各自保留。"""
    from AstralNexus.runtime import evidence_identity as ei

    path = _write(tmp_path, "verify_case.py", "stable content")
    one = ei.build_evidence_seal(
        node_id="P-110", nodes={}, accepted_artifacts=[_Artifact(path)],
        contract_ids=["artifact:verify-case-script"], execution_id="exec-A",
        receipt_ids=["r1", "r2"], run_dir=tmp_path,
        acceptance_observations=_SATISFIED,
    )
    two = ei.build_evidence_seal(
        node_id="P-110", nodes={}, accepted_artifacts=[_Artifact(path)],
        contract_ids=["artifact:verify-case-script"], execution_id="exec-B",
        receipt_ids=["r9"], run_dir=tmp_path,
        acceptance_observations=_SATISFIED,
    )

    assert one["credit_key"] is not None
    assert one["credit_key"] == two["credit_key"]          # 版本识别：同一份证据
    assert one["source_execution"] != two["source_execution"]  # 执行追溯：两次真实执行
    assert two["source_execution"]["execution_id"] == "exec-B"
    # 显式收据 + 支撑成立项的观测自带收据，两者都记（后者才是验收项的执行追溯）。
    assert two["source_execution"]["receipt_ids"] == ["r9", "r0001"]


# --------------------------------------------- 验收项身份跨接管稳定（谱系锚）


def test_requirement_identity_survives_takeover_through_lineage(tmp_path):
    """接管节点映射回原始任务：谱系根跨换代稳定。"""
    from AstralNexus.runtime import evidence_identity as ei

    nodes = {
        "P-107": _Node("P-107"),
        "P-105": _Node("P-105"),
        "P-111": _Node("P-111", {"recovery_of": "P-105"}),
        "P-123": _Node("P-123", {"recovery_of": "P-122"}),
        "P-122": _Node("P-122", {"recovery_of": "P-107"}),
    }

    assert ei.lineage_root("P-111", nodes) == "P-105"
    assert ei.lineage_root("P-123", nodes) == "P-107"
    assert ei.lineage_root("P-105", nodes) == "P-105"

    seal = _seal(tmp_path, node_id="P-111", nodes=nodes)
    assert seal["lineage_root"] == "P-105"


def test_lineage_root_survives_a_cycle_without_hanging(tmp_path):
    from AstralNexus.runtime import evidence_identity as ei

    nodes = {
        "A": _Node("A", {"recovery_of": "B"}),
        "B": _Node("B", {"recovery_of": "A"}),
    }
    assert ei.lineage_root("A", nodes) in {"A", "B"}


# ------------------------------------------------------------- 不可判定


def test_missing_hash_is_undecidable_and_earns_nothing(tmp_path):
    """文件读不到 ⇒ 不可判定，不得当成"空版本"发放进展。"""
    from AstralNexus.runtime import evidence_identity as ei

    seal = _seal(tmp_path, accepted_artifacts=[_Artifact(str(tmp_path / "gone.bin"))])

    assert seal["decidable"] is False
    assert ei.UNDECIDABLE_NO_HASH in seal["undecidable_reasons"]
    assert seal["credit_key"] is None
    decision = ei.credit_decision(seal, [])
    assert decision["credited"] is False
    assert decision["reason"] == "undecidable"


def test_missing_execution_is_undecidable(tmp_path):
    """没有来源执行 ⇒ 无法追溯实际执行 ⇒ 不可判定（哈希不能替代执行身份）。"""
    from AstralNexus.runtime import evidence_identity as ei

    seal = _seal(tmp_path, execution_id="")

    assert seal["decidable"] is False
    assert ei.UNDECIDABLE_NO_EXECUTION in seal["undecidable_reasons"]


def test_missing_contract_id_is_undecidable(tmp_path):
    """没有契约逻辑 id ⇒ 无法绑定原始验收项 ⇒ 不可判定。"""
    from AstralNexus.runtime import evidence_identity as ei

    seal = _seal(tmp_path, contract_ids=[])

    assert seal["decidable"] is False
    assert ei.UNDECIDABLE_NO_REQUIREMENT in seal["undecidable_reasons"]


def test_rejected_verdict_never_earns_credit(tmp_path):
    from AstralNexus.runtime import evidence_identity as ei

    seal = _seal(tmp_path, accepted=False)

    assert seal["decidable"] is False
    assert ei.UNDECIDABLE_NO_ARTIFACT in seal["undecidable_reasons"]


def test_relative_paths_resolve_against_the_run_dir(tmp_path):
    """运行目录相对路径（node_io 记的是这种）必须能解析到真实文件。"""
    from AstralNexus.runtime import evidence_identity as ei

    _write(tmp_path, "deliverables/verify_case.py", "x")
    seal = _seal(
        tmp_path,
        accepted_artifacts=[_Artifact("deliverables/verify_case.py")],
    )

    assert seal["evidence_versions"][0]["content_sha256"]
    assert seal["decidable"] is True


# ------------------------------------------------ 账本接线（Verifier 边界）


class _VerifierStub:
    """供 ``_finalize_node_evidence`` 读取验收项观测（与 deterministic_acceptance 同源）。"""

    def __init__(self, observations):
        self._observations = list(observations)

    def deterministic_observations(self, node):
        return list(self._observations)


def _finalize(tmp_path, *, node_id, contract_id, artifact_id, rel_path, price=None):
    """Drive the real Verifier boundary: ``Orchestrator._finalize_node_evidence``."""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.runtime_state import RuntimeStateManager
    from AstralNexus.core.task_graph import Artifact, TaskGraph, TaskNode
    from AstralNexus.core.verifier import VerificationVerdict
    from AstralNexus.runtime.execution_store import ExecutionStore
    from AstralNexus.runtime.trace import TraceRecorder

    graph = TaskGraph()
    node = graph.add_node(TaskNode(
        node_id=node_id, goal="close the repeatability gap", role="tester",
        status="verifying", output_artifacts=[contract_id],
    ))
    graph.add_artifact(Artifact(
        artifact_id=artifact_id, node_id=node_id, artifact_type="file",
        path=rel_path, description="evidence",
    ))
    node.metadata["_pending_artifact_ids"] = [artifact_id]
    store = ExecutionStore(tmp_path)
    execution_id = store.begin(node_id, {"owned_paths": ["deliverables/"]})
    receipt = store.append(execution_id, {"kind": "file_written", "path": rel_path})
    store.finish(execution_id, "succeeded", {"status": "success"})
    node.metadata["runtime_execution_id"] = execution_id
    node.metadata["_verification_receipt"] = {
        "observations": [{"receipt_ids": [receipt["receipt_id"]]}],
    }
    # 支撑该验收项的观测：它携带的收据正是该验收项的执行追溯。
    verifier_observations = [{
        "criterion": "tests_passed:python verify_case.py", "kind": "tests_passed",
        "checked": True, "holds": True,
        "receipt_ids": [receipt["receipt_id"]],
    }]

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.fence_token_provider = None  # This fixture is an explicitly unmanaged legacy run.
    orchestrator.task_graph = graph
    orchestrator.run_dir = tmp_path
    orchestrator.runtime_state_manager = RuntimeStateManager(tmp_path, "evidence-ledger")
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    orchestrator.memory_federation = type(
        "MemoryStub", (), {"add_fact": lambda self, fact: None}
    )()
    orchestrator.verifier = _VerifierStub(verifier_observations)
    orchestrator.governor = None

    orchestrator._finalize_node_evidence(
        node, approved=True, verification_score=1.0,
        verdict=VerificationVerdict.APPROVED,
    )
    events = [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "task.evidence.finalized"
    ]
    return orchestrator, events[-1], receipt["receipt_id"]


def test_finalize_binds_the_verdict_to_an_evidence_identity(tmp_path):
    """判定事件必须带上 证据版本 / 来源执行 / 原始验收项，并据此发放一次信用。"""
    _write(tmp_path, "deliverables/dual_run_evidence.json", "run1+run2 receipts")

    orchestrator, payload, receipt_id = _finalize(
        tmp_path, node_id="P-111", contract_id="artifact:dual-run-evidence",
        artifact_id="A-P-111-038", rel_path="deliverables/dual_run_evidence.json",
    )

    seal = payload["evidence_seal"]
    assert seal["schema"] == "astralnexus.evidence_seal.v1"
    assert seal["requirements"] == ["artifact:dual-run-evidence"]
    assert seal["evidence_versions"][0]["content_sha256"]
    assert seal["source_execution"]["execution_id"]
    # 执行追溯：execution_id + receipt_id 都要落到判定里（哈希不能替代它）。
    assert seal["source_execution"]["receipt_ids"] == [receipt_id]
    assert seal["decidable"] is True

    credit = payload["progress_credit"]
    assert credit["credited"] is True
    assert credit["reason"] == "acceptance_item_now_evidence_backed"
    assert orchestrator.runtime_state_manager.state.credited_evidence_keys == [
        credit["credit_key"]
    ]


def test_relayed_evidence_through_a_second_node_credits_once(tmp_path):
    """现场形状：P-105 的证据被 P-111 接管转手 ⇒ 只计一次进展。"""
    _write(tmp_path, "deliverables/dual_run_evidence.json", "identical bytes")

    _, first, _ = _finalize(
        tmp_path, node_id="P-105", contract_id="artifact:dual-run-evidence",
        artifact_id="A-P-105-013", rel_path="deliverables/dual_run_evidence.json",
    )
    _, relayed, _ = _finalize(
        tmp_path, node_id="P-111", contract_id="artifact:dual-run-evidence",
        artifact_id="A-P-111-038", rel_path="deliverables/dual_run_evidence.json",
    )

    assert first["progress_credit"]["credited"] is True
    assert relayed["progress_credit"]["credited"] is False
    assert relayed["progress_credit"]["reason"] == "same_acceptance_item_already_credited"
    # 追溯信息仍逐次保留：两次真实执行各自记录。
    assert relayed["evidence_seal"]["source_execution"]["execution_id"]
    assert (
        relayed["evidence_seal"]["source_execution"]["execution_id"]
        != first["evidence_seal"]["source_execution"]["execution_id"]
    )


def test_verdict_without_an_execution_is_recorded_undecidable(tmp_path):
    """缺来源执行 ⇒ 不可判定，且不发放信用（哈希不能替代执行身份）。"""
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.runtime_state import RuntimeStateManager
    from AstralNexus.core.task_graph import Artifact, TaskGraph, TaskNode
    from AstralNexus.core.verifier import VerificationVerdict
    from AstralNexus.runtime.trace import TraceRecorder

    _write(tmp_path, "deliverables/dual_run_evidence.json", "bytes")
    graph = TaskGraph()
    node = graph.add_node(TaskNode(
        node_id="P-111", goal="g", role="tester", status="verifying",
        output_artifacts=["artifact:dual-run-evidence"],
    ))
    graph.add_artifact(Artifact(
        artifact_id="A-P-111-038", node_id="P-111", artifact_type="file",
        path="deliverables/dual_run_evidence.json", description="e",
    ))
    node.metadata["_pending_artifact_ids"] = ["A-P-111-038"]   # no execution id

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    orchestrator.run_dir = tmp_path
    orchestrator.runtime_state_manager = RuntimeStateManager(tmp_path, "undecidable")
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    orchestrator.memory_federation = type(
        "MemoryStub", (), {"add_fact": lambda self, fact: None}
    )()
    orchestrator.governor = None

    orchestrator._finalize_node_evidence(
        node, approved=True, verification_score=1.0,
        verdict=VerificationVerdict.APPROVED,
    )

    payload = [
        event["payload"] for event in orchestrator.trace_recorder.events
        if event["event_type"] == "task.evidence.finalized"
    ][-1]
    assert payload["evidence_seal"]["decidable"] is False
    assert payload["progress_credit"]["credited"] is False
    assert payload["progress_credit"]["reason"] == "undecidable"
    assert orchestrator.runtime_state_manager.state.credited_evidence_keys == []


def test_legacy_state_without_the_credit_ledger_still_loads(tmp_path):
    """pre-fix11 的 runtime_state.json 没有新键：照常加载，账本从空开始。"""
    import json

    from AstralNexus.core.runtime_state import RuntimeState, RuntimeStateManager

    run_dir = tmp_path / "legacy"
    run_dir.mkdir()
    seed = RuntimeState(run_id="legacy-evidence")
    legacy = {
        key: value for key, value in vars(seed).items()
        if key != "credited_evidence_keys"
    }
    assert "credited_evidence_keys" not in legacy
    (run_dir / "runtime_state.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8",
    )

    manager = RuntimeStateManager(run_dir, "legacy-evidence")

    assert manager.state.credited_evidence_keys == []
    assert manager.credit_evidence_key("k1") is True
    assert manager.credit_evidence_key("k1") is False   # 幂等
    assert manager.state.credited_evidence_keys == ["k1"]
