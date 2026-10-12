"""Contract-clause anchoring (F11/F14) and the b33 S4 contraction.

Live b31 evidence (doc 51 §29): the frozen delivery contract named the entry
file the whole delivery is supposed to start from (``deliverables/docs/
contract.md`` restated it as the unique start command) while no node owned that
file and no acceptance criterion anchored it.  The graph then satisfied the
clause by spending an entire repair round on an empty shim (``task.
workspace_delta`` 02:07:20Z, ``after_bytes`` 3128).  Minutes later the final
replan asserted in its reasoning that the entry file "does not exist on disk"
although that same file had been on disk and already verified since 02:09:19Z;
that patch consumed the last recovery-budget event and the turn ended blocked
with zero delivery.

b33 S4 (CONTRACTION_PLAN §8vigesies, user 2026-10-06) removes the second and
accidental face of that fact: a document this run wrote no longer promotes a
file it names into a hard acceptance obligation.  An ordinary document is a
HINT.  Two routes into ``required`` survive — the frozen contract (goal_spec /
delivery_contract prose, which is what ``_unanchored_contract_clauses`` still
reads) and an admitted node's ``owned_paths`` coverage.  A cited path is still
re-read from disk first, then from recent workspace/verifier evidence — a plan
may only claim absence from a current disk read, never from stale prose.
"""

import json

import pytest

from AstralNexus.core.governor import GlobalGovernor, GoalSpec, TaskGraphPatch
from AstralNexus.core.output_paths import (
    declared_output_claims,
    declared_output_path_field,
)
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder


def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="running",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


class _RecordingPlanner:
    """Minimal real-client stand-in that keeps the planning prompt."""

    def __init__(self, payload=None):
        self.messages = []
        self.payload = payload or {
            "candidate_mode": "merge",
            "add_nodes": [],
            "reason_codes": ["GOAL_DECOMPOSITION"],
            "reasoning": "no change",
            "planning_status": "OK",
        }

    def chat(self, messages, **_kwargs):
        self.messages = list(messages)
        return json.dumps(self.payload, ensure_ascii=False)


def _clause_gap_events(governor):
    return [
        event
        for event in governor.trace_recorder.events
        if event["event_type"] == "governor.contract.clause_unanchored"
    ]


def _service_patch(**overrides):
    node = {
        "node_id": "N-SVC",
        "goal": "implement the service entry point",
        "role": "coder",
        "task_type": "implementation",
        "owned_paths": ["deliverables/app/server.py"],
        "acceptance_criteria": ["file:deliverables/app/server.py non-empty"],
    }
    node.update(overrides)
    return TaskGraphPatch(add_nodes=[node])


def test_unanchored_contract_clause_is_recorded_as_gap(tmp_path):
    """(a) frozen prose that names a concrete file nobody owns becomes a gap."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": [
            "唯一启动命令：python entrypoint.py",
            "服务监听 127.0.0.1:8123，端点 /api/tickets 返回 status 枚举",
        ],
        "required_outputs": [{
            "output_id": "O-01",
            "required": False,
            "acceptance_criteria": [
                "环境探针原始输出写入 deliverables/runs/env_probe.json",
            ],
        }],
    }

    result = governor.apply_task_graph_patch(_service_patch())

    gaps = result["contract_clause_gaps"]
    assert [gap["path"] for gap in gaps] == [
        "entrypoint.py",
        "deliverables/runs/env_probe.json",
    ]
    entry = gaps[0]
    assert entry["clause"] == "唯一启动命令：python entrypoint.py"
    assert entry["source"] == "contract.global_acceptance_criteria"
    assert entry["suggested_owner"] == "N-SVC"
    assert "interface constraint" in entry["reason"]
    probe = gaps[1]
    assert probe["source"] == "required_outputs[O-01].acceptance_criteria"
    # advisory only: no shared path prefix, so the same-suffix owner is hinted
    assert probe["suggested_owner"] == "N-SVC"
    # b33 S4 并②: one scan records ONE event carrying every gap and its count.
    # Before this landing each gap emitted its own event, so the same scan of
    # 14 paths multiplied the reported transitions (b31 frozen trace: 63 events
    # = 14 paths × 12 scans).  One scan = one transition; the per-gap detail
    # stays in the payload, so nothing became unobservable.
    events = _clause_gap_events(governor)
    assert len(events) == 1
    assert events[0]["payload"]["count"] == 2
    assert [gap["path"] for gap in events[0]["payload"]["paths"]] == [
        "entrypoint.py",
        "deliverables/runs/env_probe.json",
    ]
    assert events[0]["payload"]["paths"][0]["clause"] == "唯一启动命令：python entrypoint.py"


def test_clause_naming_an_owned_file_is_not_reported(tmp_path):
    """(b) a clause whose file is inside some node's owned_paths stays quiet."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": [
            "唯一启动命令：python deliverables/app/entrypoint.py",
            "入口脚本文件：entrypoint.py",
        ],
    }

    result = governor.apply_task_graph_patch(_service_patch(
        owned_paths=["deliverables/app/entrypoint.py"],
    ))

    assert result["contract_clause_gaps"] == []
    assert _clause_gap_events(governor) == []


def test_interface_clause_is_never_reported(tmp_path):
    """(c) port / endpoint / field / enum clauses name no file at all."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": [
            "服务监听 127.0.0.1:8123，端点 /api/tickets 支持 status 过滤",
            "status 只接受 待处理/处理中/已完成 三个枚举值",
            "响应体必须包含 id、title、status 三个字段",
        ],
    }

    result = governor.apply_task_graph_patch(_service_patch())

    assert result["contract_clause_gaps"] == []
    assert _clause_gap_events(governor) == []


def test_bare_file_name_is_reported_even_without_path_wording(tmp_path):
    """The prohibition is on silence: a bare name is still a named obligation."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": ["前置调研记录 research.md 由分析节点整理"],
    }

    result = governor.apply_task_graph_patch(_service_patch())

    assert [gap["path"] for gap in result["contract_clause_gaps"]] == ["research.md"]


def test_clause_file_that_already_exists_on_disk_is_anchored(tmp_path):
    """Reality anchors a clause: an existing deliverable is not an obligation."""
    graph, governor = _governor(tmp_path)
    delivered = tmp_path / "deliverables" / "app"
    delivered.mkdir(parents=True)
    (delivered / "entrypoint.py").write_text("print('ready')\n", encoding="utf-8")
    governor.delivery_contract = {
        "global_acceptance_criteria": ["唯一启动命令：python deliverables/app/entrypoint.py"],
    }

    result = governor.apply_task_graph_patch(_service_patch())

    assert result["contract_clause_gaps"] == []


def test_next_planning_round_receives_the_open_gap(tmp_path):
    """The gap reaches the planner prompt instead of a later repair round."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": ["唯一启动命令：python entrypoint.py"],
    }
    governor.apply_task_graph_patch(_service_patch())

    client = _RecordingPlanner()
    governor.plan_task_graph_patch("补齐交付图", client)
    prompt = client.messages[-1]["content"]

    assert "entrypoint.py" in prompt
    assert "governor.contract.clause_unanchored" in prompt
    assert "owned_paths" in prompt


def test_path_premise_is_refreshed_from_disk_instead_of_asserted_absent(tmp_path):
    """(d) the b31 contradiction: the file existed while the plan said it did not."""
    graph, governor = _governor(tmp_path)
    delivered = tmp_path / "deliverables" / "app"
    delivered.mkdir(parents=True)
    (delivered / "entrypoint.py").write_text("print('ready')\n", encoding="utf-8")

    premises = governor._refresh_path_premises(["deliverables/app/entrypoint.py"])
    assert premises == [{
        "path": "deliverables/app/entrypoint.py",
        "exists": True,
        "source": "disk",
    }]

    client = _RecordingPlanner()
    governor.plan_task_graph_patch(
        "确认 deliverables/app/entrypoint.py 是否已经是启动入口",
        client,
    )
    prompt = client.messages[-1]["content"]
    assert '"path": "deliverables/app/entrypoint.py"' in prompt
    assert '"exists": true' in prompt
    assert '"source": "disk"' in prompt
    # the planner is told to trust this read, not a historical narrative
    assert "不得断言" in prompt


def test_premise_falls_back_to_recent_workspace_evidence(tmp_path):
    """Without a readable run directory, recorded evidence still refreshes it."""
    graph, governor = _governor(tmp_path)
    governor.trace_recorder.record("task.workspace_delta", {
        "node_id": "R-701",
        "entries": [{
            "path": "deliverables/app/entrypoint.py",
            "kind": "added",
            "after_bytes": 3128,
        }],
    })
    governor.trace_recorder.run_dir = None

    premises = governor._refresh_path_premises(["deliverables/app/entrypoint.py"])

    assert [item["exists"] for item in premises] == [True]
    assert premises[0]["source"] == "task.workspace_delta"
    assert premises[0]["last_evidence"]["delta_kind"] == "added"


def test_unrefreshable_path_is_unverified_not_absent(tmp_path):
    """A path that cannot be refreshed is never reported as missing."""
    graph, governor = _governor(tmp_path)
    governor.trace_recorder.run_dir = None

    premises = governor._refresh_path_premises(["deliverables/app/ghost.py"])

    assert premises == [{
        "path": "deliverables/app/ghost.py",
        "exists": None,
        "source": "unverified",
    }]

    client = _RecordingPlanner()
    governor.plan_task_graph_patch("确认 deliverables/app/ghost.py 的去向", client)
    prompt = client.messages[-1]["content"]
    assert '"exists": null' in prompt
    assert 'source=unverified 的路径不得断言“不存在”' in prompt


def test_proposed_patch_event_carries_the_premises_it_relied_on(tmp_path):
    """F14: the emitted patch event is auditable against the disk state."""
    graph, governor = _governor(tmp_path)
    delivered = tmp_path / "deliverables" / "app"
    delivered.mkdir(parents=True)
    (delivered / "entrypoint.py").write_text("print('ready')\n", encoding="utf-8")
    client = _RecordingPlanner()

    governor.plan_task_graph_patch(
        "确认 deliverables/app/entrypoint.py 是否已经是启动入口",
        client,
    )

    proposed = [
        event for event in governor.trace_recorder.events
        if "graph_patch.proposed" in str(event["event_type"])
    ]
    assert proposed, "planning must record a proposed patch"
    assert {
        "path": "deliverables/app/entrypoint.py",
        "exists": True,
        "source": "disk",
    } in proposed[-1]["payload"]["premises"]


def test_goal_spec_prose_is_anchored_too(tmp_path):
    """The task goal itself is frozen prose and is checked the same way."""
    graph, governor = _governor(tmp_path)
    governor.goal_spec = GoalSpec(
        goal_id="G-01",
        task_goal="交付可运行服务，唯一启动命令 python entrypoint.py",
        normalized_goal="交付可运行服务",
    )

    result = governor.apply_task_graph_patch(_service_patch())

    assert [gap["path"] for gap in result["contract_clause_gaps"]] == ["entrypoint.py"]
    assert result["contract_clause_gaps"][0]["source"] == "goal_spec.task_goal"


def _write_doc(tmp_path, relative, text):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record_delta(governor, entries, node_id="N-101"):
    governor.trace_recorder.record("task.workspace_delta", {
        "node_id": node_id,
        "entries": list(entries),
    })


def test_document_prose_that_names_a_file_is_only_a_hint(tmp_path):
    """b33 S4 ①: an ordinary document no longer creates a hard obligation.

    原来钉住什么（F11b）：本次运行写出的 md/rst/txt 里出现的具体文件名会被冻结成
    ``artifact:<路径>`` 条款——文档里写一句"唯一启动命令：python app.py"就自动产生
    一条未归属条款和一条缺口事件（b31 现场正是这句话烧掉一个修复轮）。
    现在钉住什么：这份文档只是**提示**。同一份字节不再产生 required 义务，也不再
    上报缺口；而同一句话写在冻结契约里（global_acceptance_criteria）仍然是缺口——
    用户要求与已批准声明仍是硬要求，Worker 不得用文档自述豁免。
    """
    graph, governor = _governor(tmp_path)
    _write_doc(
        tmp_path,
        "deliverables/docs/contract.md",
        "## 3. 唯一启动命令\n\n唯一启动命令：python app.py\n",
    )
    _record_delta(governor, [{
        "path": "deliverables/docs/contract.md",
        "kind": "added",
        "after_bytes": 48,
    }])

    hinted = governor.apply_task_graph_patch(_service_patch())
    assert hinted["contract_clause_gaps"] == []
    assert hinted["contract_clause_gap_metrics"]["emitted"] == 0
    assert _clause_gap_events(governor) == []

    # The same sentence is a hard requirement only where the contract states it.
    governor.delivery_contract = {
        "global_acceptance_criteria": ["唯一启动命令：python app.py"],
    }
    frozen = governor.apply_task_graph_patch(_service_patch(
        node_id="N-SVC-2",
        owned_paths=["deliverables/other/helper.py"],
        acceptance_criteria=["file:deliverables/other/helper.py non-empty"],
    ))
    assert [gap["path"] for gap in frozen["contract_clause_gaps"]] == ["app.py"]
    assert frozen["contract_clause_gaps"][0]["source"] == (
        "contract.global_acceptance_criteria"
    )
    assert "the frozen contract" in frozen["contract_clause_gaps"][0]["reason"]


def test_structured_declaration_of_an_unowned_entry_file_demands_an_owner(tmp_path):
    """b33 S4 ②: the surviving route into required is ownership, not prose.

    原来钉住什么：F11b 靠"运行中写出的文档点名了入口文件"产生义务，而框架里
    ``required_fields``/``start_command`` 并没有生产读取点（零载体，见报告）。
    现在钉住什么：同一条事实由**既有准入层**（``owned_paths`` 覆盖检查）承担——
    结构化声明点名的入口文件若无人拥有，准入阶段就要求 owner 并点名该文件；
    被覆盖时同一份声明照常通过。没有新增字段，也没有新增 required_fields 读点。
    """
    declared = [{
        "artifact_id": "entry",
        "path": "deliverables/app/entrypoint.py",
        "kind": "entry",
        "required": True,
        "stage": "post_execution",
    }]

    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="declared output is outside owned_paths"):
        governor.apply_task_graph_patch(_service_patch(expected_outputs=declared))
    assert graph.get_node("N-SVC") is None

    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(_service_patch(
        owned_paths=["deliverables/app/"],
        expected_outputs=declared,
    ))
    assert graph.get_node("N-SVC") is not None


def test_chinese_named_deliverable_stays_a_hint(tmp_path):
    r"""b33 S4 ③: a Chinese-named document never becomes an obligation as prose.

    现场样本是夹具 ``case_small/task_book_case1.md`` 的 ``验收清单.md``：词法提取器
    的 token 形状是 ``[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,10}``，中文名不匹配，
    所以它在散文里既不进 required 也不上报缺口（保持不可见，而不是被误当成义务）。
    """
    assert GlobalGovernor._concrete_file_tokens("验收清单 写入 验收清单.md，逐条对照") == []

    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": ["验收清单 写入 验收清单.md，逐条对照"],
    }

    result = governor.apply_task_graph_patch(_service_patch())

    assert result["contract_clause_gaps"] == []
    assert _clause_gap_events(governor) == []


def test_structured_readme_name_is_visible_while_prose_stays_a_hint(tmp_path):
    """b33 S4 ④ (F16 / 5bis): a declared extensionless name is not silenced.

    原来钉住什么：无后缀的声明名（README）被 ``not Path(spelling).suffix`` 静默跳过，
    即使它写在结构化路径字段里；于是"声明了却没人拥有"在准入阶段看不见。
    现在钉住什么：结构化路径字段里的 README 可见、可归一化，并进入既有两条 required
    路由（无人拥有 ⇒ 拒绝；有人拥有 ⇒ 通过）；同一个名字只出现在散文里时仍是 hint
    （声明读取器对它返回空），绝不反向。
    """
    assert declared_output_path_field("README") == ["README"]
    assert declared_output_claims("README") == []
    assert declared_output_claims({"path": "README"}) == ["README"]
    readme = [{"artifact_id": "readme", "path": "README", "required": True}]

    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="declared output is outside owned_paths"):
        governor.apply_task_graph_patch(_service_patch(
            owned_paths=["deliverables/app/server.py"],
            expected_outputs=readme,
        ))
    assert graph.get_node("N-SVC") is None

    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(_service_patch(
        owned_paths=["README"],
        expected_outputs=readme,
    ))
    assert graph.get_node("N-SVC") is not None

    # The same name as prose is not a claim at all, so the gate never runs.
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(_service_patch(expected_outputs=["README"]))
    assert graph.get_node("N-SVC") is not None


def test_one_scan_records_one_clause_gap_event_with_its_sources(tmp_path):
    """b33 S4 ⑤ (并②): one scan = one event, and the counts ride the same carrier.

    原来钉住什么：每条缺口各发一条 ``governor.contract.clause_unanchored`` 事件
    （b31 冻结轨迹 63 条 = 14 条路径 × 12 次扫描），上报量按条放大。
    现在钉住什么：一次扫描一条事件，载荷带 ``count`` 与全部 ``paths``；计数走既有
    ``contract_clause_gaps`` 载体上的 ``contract_clause_gap_metrics``，不新增事件类型。
    """
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": ["唯一启动命令：python entrypoint.py"],
        "required_outputs": [{
            "output_id": "O-01",
            "required": False,
            "acceptance_criteria": [
                "环境探针原始输出写入 deliverables/runs/env_probe.json",
            ],
        }],
    }

    result = governor.apply_task_graph_patch(_service_patch())

    events = _clause_gap_events(governor)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["count"] == 2
    assert [gap["path"] for gap in payload["paths"]] == [
        "entrypoint.py",
        "deliverables/runs/env_probe.json",
    ]
    assert payload["paths"][0]["clause"] == "唯一启动命令：python entrypoint.py"
    metrics = result["contract_clause_gap_metrics"]
    assert metrics["emitted"] == len(result["contract_clause_gaps"]) == 2
    assert metrics["events_recorded"] == 1
    assert metrics["sources"] == [
        "contract.global_acceptance_criteria",
        "required_outputs[O-01].acceptance_criteria",
    ]
    assert metrics["capped"] is False

    # A scan that finds nothing records nothing: the event count follows scans,
    # not paths, and an empty scan adds zero transitions.
    governor.delivery_contract = {}
    clean = governor.apply_task_graph_patch(_service_patch(
        node_id="N-SVC-2",
        owned_paths=["deliverables/other/helper.py"],
        acceptance_criteria=["file:deliverables/other/helper.py non-empty"],
    ))
    assert clean["contract_clause_gaps"] == []
    assert clean["contract_clause_gap_metrics"]["events_recorded"] == 0
    assert len(_clause_gap_events(governor)) == 1


def test_written_document_carrier_is_gone_and_the_clause_cap_is_unchanged(tmp_path):
    """b33 S4 ⑥: the deletion is visible in bytes, and the cap is untouched.

    原来钉住什么：``WRITTEN_DOC_MAX_PATHS`` 是"本次运行写出的文档路径"注册表的上限，
    ``_written_doc_paths()`` 用它计数，旧用例 (h) 钉住该上限与它的截断行为。
    现在钉住什么：整套载体（4 个常量 + 4 个读取器 + 1 个实例字段）都不存在了——删掉的
    东西能在属性与字节上看得见；同时 ``CLAUSE_GAP_LIMIT`` 仍是 24，缺口上限没有放宽，
    也没有换一个更大的上限。提示读取器（``_concrete_file_tokens``）保留：它正是"散文
    仍受后缀约束、但不再变成义务"的那一侧。
    """
    for name in (
        "WRITTEN_DOC_SUFFIXES",
        "WRITTEN_DOC_MAX_PATHS",
        "WRITTEN_DOC_MAX_BYTES",
        "WRITTEN_DOC_SCAN_EVENTS",
        "_read_written_doc",
        "_written_doc_path",
        "_written_doc_paths",
        "_written_doc_clauses",
    ):
        assert not hasattr(GlobalGovernor, name), name
    assert GlobalGovernor.CLAUSE_GAP_LIMIT == 24
    graph, governor = _governor(tmp_path)
    assert not hasattr(governor, "_written_doc_path_registry")
    # the hint reader survives: prose stays suffix-bound, it just stops obliging
    assert GlobalGovernor._concrete_file_tokens("python app.py") == ["app.py"]


def test_clause_gap_list_is_capped(tmp_path):
    """(d) document noise cannot grow the gap list without bound."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": [
            f"交付物 {index}：artifact-{index}.json" for index in range(40)
        ],
    }

    gaps = governor.apply_task_graph_patch(_service_patch())["contract_clause_gaps"]

    assert len(gaps) == GlobalGovernor.CLAUSE_GAP_LIMIT
    assert len({gap["path"] for gap in gaps}) == GlobalGovernor.CLAUSE_GAP_LIMIT


def test_machine_absolute_paths_are_not_gaps(tmp_path):
    """A file-server path inside an artifact is not a run-relative obligation."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "global_acceptance_criteria": [
            "运行日志：已写入 //fileserver/share/deliverables/app/app.py",
            "唯一启动命令：python app.py",
        ],
    }

    gaps = governor.apply_task_graph_patch(_service_patch())["contract_clause_gaps"]

    assert [gap["path"] for gap in gaps] == ["app.py"]