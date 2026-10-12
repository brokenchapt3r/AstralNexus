"""F-A inversion tests (P4 Round-1 fix): graph patch input resolution v2.

Round-1 crash chain background: the verifier-gap replan patch (N-201
e2e producer + N-202 report consumer) crashed ``apply_task_graph_patch``
with an uncaught ``ValueError`` because (1) the artifact alias key folded
every path to its basename, so one file registered three ways (absolute,
relative, contract-bundle id) became "ambiguous", and sibling-directory
files sharing a basename collided; (2) the identity dedupe demanded
non-null ``verified_at``; (3) inputs produced by a NEW node in the same
patch were treated as unknown artifacts even though the patch itself
declared their producer.

Fixtures are trimmed from the real round-1 replan patch
(P4_ROUND1_REPORT.md / p4_round1_snapshot) with no runtime dependency on
``02_运行产物``.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.task_graph import Artifact, TaskGraph, TaskNode
from AstralNexus.runtime.trace import TraceRecorder


def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="running",
    ))
    recorder = TraceRecorder(tmp_path)
    return graph, GlobalGovernor(graph, recorder), recorder


# --- round-1 triple registration: one static page, three spellings ----------

ROUND1_TRIPLE_ROWS = [
    # (artifact_id, artifact_type, path, retry_count, verified_at)
    ("A-N-102-ui-impl-005", "file", "{abs}/deliverables/app/static/index.html", 0,
     "2026-10-06T12:05:20.285092+00:00"),
    ("A-N-102-ui-impl-006", "file", "deliverables/app/static/index.html", 0,
     "2026-10-06T12:05:20.285092+00:00"),
    ("artifact:deliverables_app_static_index_html", "contract_bundle",
     "{abs}/deliverables/app/static/index.html", None, None),
]


def _register_round1_triple(graph, tmp_path):
    abs_root = str(tmp_path).replace("\\", "/")
    for artifact_id, artifact_type, path, retry_count, verified_at in ROUND1_TRIPLE_ROWS:
        graph.add_artifact(Artifact(
            artifact_id=artifact_id,
            node_id="N-102-ui-impl",
            artifact_type=artifact_type,
            path=path.format(abs=abs_root),
            description="static index page",
            acceptance_status="accepted",
            metadata={
                "task_version": 1,
                "retry_count": retry_count,
                "verified_at": verified_at,
                "verification_verdict": "approved",
            },
        ))


# --- round-1 prior artifacts sharing basenames with the replan inputs -------

ROUND1_PRIOR_ROWS = [
    ("A-N-103-verify-run1", "deliverables/runs/verify_logs/run1.log"),
    ("A-N-103-baseline-summary", "deliverables/app/baseline/summary.json"),
    ("A-N-103-verify-acceptance", "deliverables/runs/verify_logs/acceptance_report.before.md"),
]


def _register_round1_prior(graph):
    for artifact_id, path in ROUND1_PRIOR_ROWS:
        graph.add_artifact(Artifact(
            artifact_id=artifact_id,
            node_id="N-103-verify",
            artifact_type="file",
            path=path,
            description="prior evidence",
            acceptance_status="accepted",
            metadata={"task_version": 1, "retry_count": 0, "verified_at": "same-verification"},
        ))
    graph.add_node(TaskNode(
        node_id="N-103-verify", goal="静态核对", role="tester", status="completed",
    ))


# --- snapshots for atomicity proof ------------------------------------------

def _node_snapshot(node):
    return dataclasses.asdict(node)


def _graph_snapshot(graph):
    return (
        {node_id: _node_snapshot(node) for node_id, node in graph.nodes.items()},
        {artifact_id: dataclasses.asdict(artifact) for artifact_id, artifact in graph.artifacts.items()},
        [dataclasses.asdict(edge) for edge in graph.edges],
        {fact_id: dataclasses.asdict(fact) for fact_id, fact in graph.facts.items()},
        {metric_id: dataclasses.asdict(metric) for metric_id, metric in graph.metrics.items()},
    )


def test_round1_triple_registration_resolves_to_single_input(tmp_path):
    """Taskbook test 3: the round-1 ① inversion.

    One physical file (deliverables/app/static/index.html) was registered
    three ways — absolute path, relative path, and the contract-bundle
    artifact id.  The basename alias key plus the verified_at-requiring
    identity dedupe turned that ONE file into an "ambiguous" rejection
    (round-1 ValueError ①).  Path-anchored keys unify the spellings; the
    merge check then collapses them to one representative input because
    they agree on producer node, task_version and retry_count.
    """
    graph, governor, recorder = _governor(tmp_path)
    _register_round1_triple(graph, tmp_path)
    graph.add_node(TaskNode(
        node_id="N-102-ui-impl", goal="实现工单页面", role="coder", status="completed",
    ))

    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "N-201-e2e-real-server",
        "goal": "在真实服务上复跑 e2e 断言",
        "role": "tester",
        "dependencies": ["N-102-ui-impl"],
        "input_artifacts": ["deliverables/app/static/index.html"],
    }]))

    node = graph.get_node("N-201-e2e-real-server")
    assert node.input_artifacts == ["A-N-102-ui-impl-005"]


def test_round1_replan_patch_replays_without_crash(tmp_path):
    """Taskbook test 4: the full round-1 replan patch now applies cleanly.

    This is the exact crash-chain replay: N-202 consumes
    runs/e2e_realserver/{run1.log, summary.json,
    acceptance_report.before.md} — files that do not exist yet because
    sibling N-201 (added by the SAME patch) will produce them, while
    prior run artifacts share those basenames in different directories.
    Old behavior: basename collision -> "ambiguous" (②), and the
    not-yet-produced inputs -> "unknown input artifact" (③④), crashing
    the turn.  New behavior: the three inputs are admitted as planned
    dependencies on the patch's own producer, with trace evidence.
    """
    graph, governor, recorder = _governor(tmp_path)
    _register_round1_triple(graph, tmp_path)
    _register_round1_prior(graph)
    graph.add_node(TaskNode(
        node_id="N-102-ui-impl", goal="实现工单页面", role="coder", status="completed",
    ))

    governor.apply_task_graph_patch(TaskGraphPatch(
        add_nodes=[
            {
                "node_id": "N-201-e2e-real-server",
                "goal": "在真实 server.py 上复跑 Playwright 页面断言并留存 run1.log",
                "role": "tester",
                "dependencies": ["N-102-ui-impl"],
                "input_artifacts": ["deliverables/app/static/index.html"],
                "owned_paths": ["runs/e2e_realserver/"],
            },
            {
                "node_id": "N-202-report-real-e2e",
                "goal": "汇总真实 e2e 复跑证据为验收报告",
                "role": "analyst",
                "dependencies": ["N-201-e2e-real-server"],
                "input_artifacts": [
                    "runs/e2e_realserver/run1.log",
                    "runs/e2e_realserver/summary.json",
                    "runs/e2e_realserver/acceptance_report.before.md",
                ],
            },
        ],
        reason_codes=["EVIDENCE_COVERAGE_GAP"],
    ))

    producer = graph.get_node("N-201-e2e-real-server")
    consumer = graph.get_node("N-202-report-real-e2e")
    # The existing index.html resolves through the (collapsed) alias channel.
    assert producer.input_artifacts == ["A-N-102-ui-impl-005"]
    # The not-yet-produced inputs stay on the raw declared plane.
    assert consumer.input_artifacts == [
        "runs/e2e_realserver/run1.log",
        "runs/e2e_realserver/summary.json",
        "runs/e2e_realserver/acceptance_report.before.md",
    ]
    planned = consumer.metadata.get("planned_dependencies", [])
    assert {item["path"] for item in planned} == {
        "runs/e2e_realserver/run1.log",
        "runs/e2e_realserver/summary.json",
        "runs/e2e_realserver/acceptance_report.before.md",
    }
    assert {item["producer"] for item in planned} == {"N-201-e2e-real-server"}
    admitted = [
        event["payload"]
        for event in recorder.events
        if event.get("event_type") == "governor.patch.planned_input_admitted"
    ]
    assert len(admitted) == 3
    assert {item["producer"] for item in admitted} == {"N-201-e2e-real-server"}
    assert all(
        item["reason_code"] == "PLANNED_DEPENDENCY_IN_PATCH_PROJECTION"
        for item in admitted
    )


def test_unknown_input_without_patch_producer_rejects_whole_patch_atomically(tmp_path):
    """Taskbook test 5 (governor layer): no-producer input rejects, graph intact.

    A future artifact is a LEGAL input only when the patch projection
    itself declares the producer.  With no producer anywhere, the input
    is unknown (③④ kept, now with the explicit no-producer reason), and
    the whole patch must be rejected before any mutation reaches the
    graph — field-by-field equality on nodes, artifacts, edges, facts
    and metrics.
    """
    graph, governor, recorder = _governor(tmp_path)
    graph.add_node(TaskNode(
        node_id="N-100-done", goal="completed baseline work", role="coder",
        status="completed", owned_paths=["deliverables/app/baseline/"],
    ))
    graph.add_artifact(Artifact(
        artifact_id="A-N-100-baseline", node_id="N-100-done", artifact_type="file",
        path="deliverables/app/baseline/summary.json", description="baseline",
        acceptance_status="accepted", metadata={"task_version": 1, "retry_count": 0},
    ))
    before = _graph_snapshot(graph)

    with pytest.raises(ValueError, match=(
        r"unknown input artifact: runs/nowhere/orphan\.log \(N-202-bad\) "
        r"\[no producer in patch projection\]"
    )):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[
            {
                "node_id": "N-201-ok",
                "goal": "producer of the fresh evidence",
                "role": "tester",
                "dependencies": ["root"],
                "owned_paths": ["runs/e2e_realserver/"],
            },
            {
                "node_id": "N-202-bad",
                "goal": "consumer of an orphan input",
                "role": "analyst",
                "dependencies": ["N-201-ok"],
                "input_artifacts": ["runs/nowhere/orphan.log"],
            },
        ]))

    assert _graph_snapshot(graph) == before
    assert graph.get_node("N-202-bad") is None
    assert graph.get_node("N-201-ok") is None




def test_same_path_competing_retries_remain_ambiguous(tmp_path):
    """Taskbook test 3 (companion): competing retries do not merge.

    Missing metadata is undeclared (never a conflict), but two DECLARED
    retry_count values on the same path-anchored key are competing
    contract commitments: the error keeps the verbatim ambiguous prefix
    and spells out each candidate's node/task_version/retry_count.
    """
    graph, governor, recorder = _governor(tmp_path)
    for artifact_id, retry_count in [("A-r0", 0), ("A-r1", 1)]:
        graph.add_artifact(Artifact(
            artifact_id=artifact_id, node_id="root", artifact_type="file",
            path="runs/e2e_realserver/run1.log", description="retry variant",
            acceptance_status="accepted",
            metadata={"task_version": 1, "retry_count": retry_count,
                      "verified_at": "same-verification"},
        ))

    with pytest.raises(ValueError, match=(
        r"ambiguous.*competing versions.*retry_count"
    )) as excinfo:
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "consumer",
            "goal": "consume the e2e log",
            "role": "analyst",
            "dependencies": ["root"],
            "input_artifacts": ["runs/e2e_realserver/run1.log"],
        }]))

    message = str(excinfo.value)
    assert "retry_count=0" in message
    assert "retry_count=1" in message


def test_same_path_competing_producers_remain_ambiguous(tmp_path):
    """Taskbook test 3 (companion): different producers do not merge.

    Two accepted registrations of the same path from DIFFERENT producer
    nodes are a genuine handoff ambiguity (never "same node+same path
    family" alone): the error names both producers.
    """
    graph, governor, recorder = _governor(tmp_path)
    for producer_id in ["N-A-verify", "N-B-verify"]:
        graph.add_node(TaskNode(
            node_id=producer_id, goal="verify e2e", role="tester", status="completed",
        ))
        graph.add_artifact(Artifact(
            artifact_id=f"A-{producer_id}", node_id=producer_id, artifact_type="file",
            path="runs/e2e_realserver/run1.log", description="producer variant",
            acceptance_status="accepted",
            metadata={"task_version": 1, "retry_count": 0,
                      "verified_at": "same-verification"},
        ))

    with pytest.raises(ValueError, match=(
        r"ambiguous.*competing versions.*N-A-verify.*N-B-verify"
    )):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "consumer",
            "goal": "consume the e2e log",
            "role": "analyst",
            "dependencies": ["root"],
            "input_artifacts": ["runs/e2e_realserver/run1.log"],
        }]))
