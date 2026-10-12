import pytest

from pathlib import Path

from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import (
    Artifact,
    ArtifactAcceptanceStatus,
    TaskGraph,
    TaskNode,
)
from AstralNexus.core.verifier import VerificationVerdict
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
from AstralNexus.tools.executor import ToolExecutor


def _governor(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="root",
        goal="deliver a working application",
        role="planner",
        status="running",
    ))
    return graph, GlobalGovernor(graph, TraceRecorder(tmp_path))


def test_patch_preserves_node_dependencies_without_edge_records(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id="consumer", goal="consume root", role="coder", dependencies=["root"]))
    governor.apply_task_graph_patch(TaskGraphPatch())
    assert graph.get_node("consumer").dependencies == ["root"]


def test_patch_rejects_field_name_wrapper_instead_of_silently_dropping_update(tmp_path):
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="direct fields"):
        governor.apply_task_graph_patch(TaskGraphPatch(update_nodes=[{
            "node_id": "root", "field_name": "dependencies", "new_value": [],
        }]))
    assert graph.get_node("root").status == "running"


def test_recovery_cannot_leave_consumer_bound_to_old_artifact_owner(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id="producer", goal="produce contract", role="coder", status="blocked", output_artifacts=["artifact:api"]))
    graph.add_node(TaskNode(node_id="consumer", goal="consume API", role="coder", dependencies=["producer"], input_artifacts=["artifact:api"]))
    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "replacement", "goal": "repair API", "recovery_of": "producer",
        "output_artifacts": ["artifact:api"],
    }])
    with pytest.raises(ValueError, match="not a declared dependency"):
        governor.apply_task_graph_patch(patch)
    assert graph.get_node("replacement") is None
    assert graph.get_node("consumer").dependencies == ["producer"]


def test_takeover_inherits_lineage_delivery_ids(tmp_path):
    """Fix 22 (live r11 acceptance-reference audit): the security-track
    rebuild reworded goals toward evidence normalization and successors
    declared only what the reworded text restated — O-02.C04 fell off the
    lineage and final acceptance became permanently unevaluable because no
    living node carried the criterion. A takeover must inherit the UNION of
    its lineage's delivery ids: the planner may add commitments, never
    silently subtract them."""
    graph, governor = _governor(tmp_path)
    failed = TaskNode(
        node_id="sec-v3", goal="run the security track", role="tester",
        status="failed", owned_paths=["deliverables/runs/security/"],
    )
    failed.metadata["delivery_output_ids"] = ["O-02"]
    failed.metadata["delivery_criterion_ids"] = ["O-02.C03", "O-02.C04"]
    graph.add_node(failed)

    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "sec-v4",
        "goal": "normalize security receipts and rerun the track",
        "role": "tester",
        "recovery_of": "sec-v3",
        "owned_paths": ["deliverables/runs/security/"],
        "delivery_criterion_ids": ["O-02.C05"],
    }]))

    node = graph.get_node("sec-v4")
    assert node is not None
    assert node.metadata["delivery_output_ids"] == ["O-02"]
    assert node.metadata["delivery_criterion_ids"] == [
        "O-02.C05", "O-02.C03", "O-02.C04",
    ]


def test_criterion_binding_outside_contract_is_stripped_when_carrier_exists(tmp_path):
    """Fix 26 (live r12 A-001 lineage immortality): O-03.C02 demands
    "File deliverables/docs/architecture.md exists" — a file owned by the
    docs-bundle node D-001 — yet the architect lineage carried the binding
    (propagated by fix 22 through every takeover). Workers cannot write
    outside owned_paths, so each generation was verified against an
    impossible demand and the lineage looped forever. An unsatisfiable
    binding must be stripped at admission when another node carries the same
    criterion; a sole carrier's binding must survive (orphan protection);
    content-only criteria stay regardless."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "required_outputs": [{
            "output_id": "O-03",
            "required": False,
            "acceptance_criteria": [
                "状态迁移表与权限矩阵必须完整准确",
                "File deliverables/docs/architecture.md exists and is UTF-8 Markdown",
                "File deliverables/docs/solo.md exists",
            ],
        }],
    }
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "D-DOC",
        "goal": "assemble the required documents",
        "role": "coder",
        "task_type": "implementation",
        "owned_paths": ["deliverables/docs/architecture.md"],
        "delivery_output_ids": ["O-03"],
        "delivery_criterion_ids": ["O-03.C01", "O-03.C02"],
    }]))
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "A-DOC",
        "goal": "output the structured design contract draft",
        "role": "architect",
        "task_type": "implementation",
        "owned_paths": ["deliverables/docs/architecture_draft.md"],
        "delivery_output_ids": ["O-03"],
        "delivery_criterion_ids": ["O-03.C01", "O-03.C02", "O-03.C03"],
    }]))

    node = graph.get_node("A-DOC")
    assert node is not None
    # C02 demands D-DOC's file and D-DOC carries it -> stripped here;
    # C01 is content-only -> kept; C03 has no other carrier -> kept.
    assert node.metadata["delivery_criterion_ids"] == ["O-03.C01", "O-03.C03"]
    # the carrier's own binding is untouched
    assert graph.get_node("D-DOC").metadata["delivery_criterion_ids"] == [
        "O-03.C01", "O-03.C02",
    ]
    events = [
        event["event_type"] for event in governor.trace_recorder.events
    ]
    assert "governor.patch.criterion_binding_stripped" in events


def test_later_patch_cannot_bind_invented_criterion_ids(tmp_path):
    """Doc 17 第二批②（恢复目标不凭空增加格式要求）: the first-patch
    unknown-ID gate does not guard later patches; an invented criterion ID
    would become immortal through lineage inheritance (fix 22), so EVERY
    patch is gated against the contract's criterion universe."""
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "required_outputs": [{
            "output_id": "O-03",
            "required": True,
            "acceptance_criteria": [
                "content complete",
                "File deliverables/docs/architecture.md exists",
            ],
        }],
    }
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "D-DOC", "goal": "assemble docs", "role": "coder",
        "task_type": "implementation",
        "owned_paths": ["deliverables/docs/architecture.md"],
        "delivery_output_ids": ["O-03"],
        "delivery_criterion_ids": ["O-03.C01", "O-03.C02"],
    }]))
    # a later patch binding an invented criterion id is rejected
    with pytest.raises(ValueError, match="unknown to the contract"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "F-DOC", "goal": "docs in a stricter format",
            "role": "coder", "task_type": "implementation",
            "owned_paths": ["deliverables/docs/format.md"],
            "delivery_output_ids": ["O-03"],
            "delivery_criterion_ids": ["O-03.C01", "O-03.C07"],
        }]))
    assert graph.get_node("F-DOC") is None
    # control: only known criterion ids passes the same patch shape
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "F-DOC", "goal": "docs in a stricter format",
        "role": "coder", "task_type": "implementation",
        "owned_paths": ["deliverables/docs/format.md"],
        "delivery_output_ids": ["O-03"],
        "delivery_criterion_ids": ["O-03.C01"],
    }]))
    assert graph.get_node("F-DOC") is not None
    assert graph.get_node("F-DOC").metadata["delivery_criterion_ids"] == ["O-03.C01"]


def test_explicit_delivery_paths_are_not_rewritten():
    from AstralNexus.core.output_paths import normalize_worker_output_path
    assert normalize_worker_output_path("deliverables/result.json") == "deliverables/result.json"
    assert normalize_worker_output_path("deliverables/check.md") == "deliverables/check.md"


def test_patch_persists_dependency_and_worker_handoff_contract(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "architect",
        "goal": "define the application contract",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/spec/"],
        "output_artifacts": ["architecture_spec.md", "api_contract.yaml"],
        "handoff_contract": {"schema": "api-v1", "required_fields": ["routes"]},
    }]))
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "coder",
        "goal": "implement the application from the approved contract",
        "dependencies": ["architect"],
        "input_artifacts": ["api_contract.yaml"],
        "owned_paths": ["deliverables/app/"],
        "output_artifacts": ["source_tree"],
        "handoff_contract": {"schema": "api-v1"},
    }]))

    coder = graph.get_node("coder")
    assert coder.dependencies == ["architect"]
    assert coder.input_artifacts == ["api_contract.yaml"]
    assert coder.owned_paths == ["deliverables/app/"]
    assert coder.handoff_contract["schema"] == "api-v1"
    assert any(
        edge.source == "architect" and edge.target == "coder"
        and edge.edge_type == "communicates_to"
        for edge in graph.edges
    )


def test_patch_resolves_unique_staged_artifact_alias(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_artifact(Artifact(
        artifact_id="artifact:source_record_md",
        node_id="root",
        artifact_type="md",
        path="inputs/data/maintenance/SOURCE_RECORD.md",
        description="fixture provenance",
    ))

    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "consumer",
        "goal": "consume the staged source record",
        "dependencies": ["root"],
        "input_artifacts": ["artifact:SOURCE_RECORD.md"],
    }]))

    assert graph.get_node("consumer").input_artifacts == ["artifact:source_record_md"]


def test_artifact_alias_uses_verified_handoff_and_collapses_duplicate_path(tmp_path):
    graph, governor = _governor(tmp_path)
    for key, path, status in [
        ("old", "app/main.py", "rejected"),
        ("current-abs", str(tmp_path / "app/main.py"), "accepted"),
        ("current-rel", "app/main.py", "accepted"),
    ]:
        graph.add_artifact(Artifact(key, "root", "file", path, "source", acceptance_status=status,
                                    metadata={"task_version": 1, "retry_count": 0, "verified_at": "same-verification"}))
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "consumer", "goal": "test backend", "dependencies": ["root"],
        "input_artifacts": ["app/main.py"],
    }]))
    assert graph.get_node("consumer").input_artifacts == ["current-abs"]


def test_distinct_verified_paths_do_not_collide_via_basename(tmp_path):
    # P4 Round-1 inversion: the alias key used to fold every path to its
    # basename, so first/main.py and second/main.py "collided" on main.py and
    # a consumer naming only the basename got an ambiguity error.  Path-
    # anchored keys mean distinct directories are distinct artifacts: the
    # bare basename matches neither registration, so the honest outcome is
    # an unknown-input rejection with no producer in the patch projection.
    graph, governor = _governor(tmp_path)
    for key in ["first", "second"]:
        graph.add_artifact(Artifact(key, "root", "file", f"{key}/main.py", "source",
                                    acceptance_status="accepted", metadata={"verified_at": "same"}))
    with pytest.raises(ValueError, match=r"unknown input artifact: main\.py \(consumer\) \[no producer in patch projection\]"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "consumer", "goal": "test backend", "dependencies": ["root"],
            "input_artifacts": ["main.py"],
        }]))


def test_same_path_competing_versions_remain_ambiguous(tmp_path):
    # P4 Round-1 companion: genuine ambiguity is when two registrations of
    # the SAME path-anchored key carry competing versions — different
    # task_version values are different contract commitments and must not
    # be merged, with the competing candidates spelled out in the error.
    graph, governor = _governor(tmp_path)
    for key, version in [("v1", 1), ("v2", 2)]:
        graph.add_artifact(Artifact(key, "root", "file", "app/main.py", "source",
                                    acceptance_status="accepted",
                                    metadata={"task_version": version, "retry_count": 0,
                                              "verified_at": "same-verification"}))
    with pytest.raises(ValueError, match=r"ambiguous.*competing versions.*task_version"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "consumer", "goal": "test backend", "dependencies": ["root"],
            "input_artifacts": ["app/main.py"],
        }]))


def test_directory_input_expands_only_accepted_artifacts(tmp_path):
    graph, governor = _governor(tmp_path)
    for key, path, status in [
        ("main", "deliverables/app/backend/main.py", "accepted"),
        ("schema", str(tmp_path / "deliverables/app/backend/schema.sql"), "accepted"),
        ("old", "deliverables/app/backend/main.py", "rejected"),
        ("other", "deliverables/app/backend-other/main.py", "accepted"),
    ]:
        graph.add_artifact(Artifact(key, "root", "file", path, "source", acceptance_status=status))
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "consumer", "goal": "test backend", "dependencies": ["root"],
        "input_artifacts": ["app/backend/"],
    }]))
    assert graph.get_node("consumer").input_artifacts == ["main", "schema"]


def test_unknown_directory_input_is_not_fabricated(tmp_path):
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="no accepted artifacts"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "consumer", "goal": "test backend", "dependencies": ["root"],
            "input_artifacts": ["missing/"],
        }]))
    assert graph.get_node("consumer") is None


def test_patch_rejects_unknown_input_artifact_atomically(tmp_path):
    graph, governor = _governor(tmp_path)

    with pytest.raises(ValueError, match="unknown input artifact"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "consumer",
            "goal": "consume evidence that no producer declared",
            "dependencies": ["root"],
            "input_artifacts": ["artifact:missing_evidence"],
        }]))

    assert graph.get_node("consumer") is None


def test_initial_patch_must_cover_every_required_delivery_output(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "required_outputs": [
            {"output_id": "O-01", "required": True},
            {"output_id": "O-02", "required": True},
            {"output_id": "O-03", "required": True},
        ]
    }

    with pytest.raises(ValueError, match="does not cover required delivery outputs"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "partial",
            "goal": "only build the database",
            "dependencies": ["root"],
            "delivery_output_ids": ["O-01"],
        }]), parent_task_id="root")

    assert graph.get_node("partial") is None


def test_initial_patch_accepts_distributed_delivery_output_coverage(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "required_outputs": [
            {"output_id": "O-01", "required": True},
            {"output_id": "O-02", "required": True},
            {"output_id": "O-03", "required": True},
        ]
    }

    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[
        {
            "node_id": "implementation",
            "goal": "build the application",
            "dependencies": ["root"],
            "delivery_output_ids": ["O-01"],
        },
        {
            "node_id": "verification",
            "goal": "run acceptance checks",
            "dependencies": ["implementation"],
            "delivery_output_ids": ["O-02"],
        },
        {
            "node_id": "documentation",
            "goal": "write delivery documentation",
            "dependencies": ["implementation"],
            "delivery_output_ids": ["O-03"],
        },
    ]), parent_task_id="root")

    assert result["added"] == ["implementation", "verification", "documentation"]


def test_delivery_coverage_gate_does_not_block_control_plane_bootstrap(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "required_outputs": [{"output_id": "O-01", "required": True}]
    }

    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "planning-root",
        "goal": "plan the admitted user goal",
        "role": "planner",
        "dependencies": ["root"],
        "metadata": {"plane": "control", "type": "planning_root"},
    }]), parent_task_id="root")

    assert result["added"] == ["planning-root"]


def test_initial_patch_cannot_hide_missing_acceptance_criterion(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {
        "required_outputs": [{
            "output_id": "O-01",
            "required": True,
            "acceptance_criteria": ["backend works", "frontend loads"],
            "acceptance_criterion_ids": ["O-01.C01", "O-01.C02"],
        }]
    }

    with pytest.raises(ValueError, match="does not cover enough required delivery criteria"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "backend-only",
            "goal": "implement the backend only",
            "dependencies": ["root"],
            "delivery_output_ids": ["O-01"],
            "delivery_criterion_ids": ["O-01.C01"],
        }]), parent_task_id="root")

    assert graph.get_node("backend-only") is None


def test_initial_patch_can_defer_criteria_only_with_explicit_relaxed_policy(tmp_path):
    graph, governor = _governor(tmp_path)
    from AstralNexus.core.governor_policy import GovernorPolicy
    governor.policy = GovernorPolicy(
        initial_contract_min_output_criterion_coverage=0.60,
        initial_contract_min_total_criterion_coverage=0.75,
    )
    governor.delivery_contract = {
        "required_outputs": [
            {
                "output_id": "O-01",
                "required": True,
                "acceptance_criteria": ["a", "b", "c", "d", "e"],
                "acceptance_criterion_ids": [
                    "O-01.C01", "O-01.C02", "O-01.C03", "O-01.C04", "O-01.C05",
                ],
            },
            {
                "output_id": "O-02",
                "required": True,
                "acceptance_criteria": ["v", "w", "x", "y", "z"],
                "acceptance_criterion_ids": [
                    "O-02.C01", "O-02.C02", "O-02.C03", "O-02.C04", "O-02.C05",
                ],
            },
        ]
    }
    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[
        {
            "node_id": "app",
            "goal": "build the core application",
            "dependencies": ["root"],
            "delivery_output_ids": ["O-01"],
            "delivery_criterion_ids": ["O-01.C01", "O-01.C02", "O-01.C03"],
        },
        {
            "node_id": "evidence",
            "goal": "collect runtime evidence",
            "dependencies": ["app"],
            "delivery_output_ids": ["O-02"],
            "delivery_criterion_ids": [
                "O-02.C01", "O-02.C02", "O-02.C03", "O-02.C04", "O-02.C05",
            ],
        },
    ]), parent_task_id="root")

    assert result["added"] == ["app", "evidence"]
    assert graph.get_node("app").metadata["deferred_delivery_criterion_ids"] == [
        "O-01.C04", "O-01.C05",
    ]


def test_rejected_patch_repair_delta_preserves_existing_candidate_nodes(tmp_path):
    _graph, governor = _governor(tmp_path)
    rejected = TaskGraphPatch(
        add_nodes=[
            {"node_id": "backend", "goal": "build backend"},
            {"node_id": "frontend", "goal": "build frontend"},
        ],
        reason_codes=["GOAL_DECOMPOSITION"],
    )
    repair = TaskGraphPatch(
        add_nodes=[
            {"node_id": "docs", "goal": "write documentation"},
        ],
        reason_codes=["VERIFICATION_GAP"],
    )

    merged = governor.merge_rejected_task_graph_patch(rejected, repair)

    assert [item["node_id"] for item in merged.add_nodes] == [
        "backend", "frontend", "docs",
    ]
    assert "REJECTED_PATCH_REPAIRED" in merged.reason_codes


def test_rejected_patch_repair_replaces_same_node_and_can_cancel_candidate(tmp_path):
    _graph, governor = _governor(tmp_path)
    rejected = TaskGraphPatch(add_nodes=[
        {"node_id": "contract", "goal": "design API", "owned_paths": ["app/"]},
        {"node_id": "backend", "goal": "build API", "owned_paths": ["app/"]},
    ])
    repair = TaskGraphPatch(
        add_nodes=[{
            "node_id": "contract",
            "goal": "design API",
            "owned_paths": ["docs/contracts/"],
        }],
        cancel_nodes=["backend"],
    )

    merged = governor.merge_rejected_task_graph_patch(rejected, repair)

    assert merged.add_nodes == [{
        "node_id": "contract",
        "goal": "design API",
        "owned_paths": ["docs/contracts/"],
    }]
    assert merged.cancel_nodes == []


def test_rejected_patch_full_candidate_drops_omitted_stale_node(tmp_path):
    _graph, governor = _governor(tmp_path)
    rejected = TaskGraphPatch(add_nodes=[
        {"node_id": "backend", "goal": "build backend"},
        {"node_id": "frontend", "goal": "build frontend"},
        {"node_id": "docs-old", "goal": "duplicate docs"},
    ])
    repair = TaskGraphPatch(candidate_mode="replace", add_nodes=[
        {"node_id": "backend", "goal": "build backend"},
        {"node_id": "frontend", "goal": "build frontend"},
        {"node_id": "docs", "goal": "write docs once"},
    ])

    merged = governor.merge_rejected_task_graph_patch(rejected, repair)

    assert [item["node_id"] for item in merged.add_nodes] == [
        "backend", "frontend", "docs",
    ]


def test_evidence_worker_aggregate_parent_is_namespaced(tmp_path):
    graph, governor = _governor(tmp_path)
    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[
        {
            "node_id": "e2e",
            "goal": "run end-to-end tests",
            "task_type": "testing",
            "dependencies": ["root"],
            "owned_paths": ["deliverables/runs/e2e/"],
        },
        {
            "node_id": "runtime-evidence",
            "goal": "record service runtime evidence",
            "task_type": "tool",
            "dependencies": ["root"],
            "owned_paths": ["deliverables/runs/"],
        },
    ]), parent_task_id="root")

    assert result["added"] == ["e2e", "runtime-evidence"]
    runtime = graph.get_node("runtime-evidence")
    assert runtime.owned_paths == ["deliverables/runs/runtime-evidence/"]
    assert runtime.metadata["ownership_normalizations"][0]["reason"] == (
        "evidence_worker_namespace"
    )


def test_document_only_node_is_not_treated_as_source_implementation(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "architecture-doc",
        "goal": "write the architecture document",
        "task_type": "implementation",
        "required_capabilities": ["system_architecture", "documentation"],
        "dependencies": ["root"],
        "owned_paths": ["deliverables/docs/"],
        "output_artifacts": ["artifact:architecture_document_md"],
    }]), parent_task_id="root")

    node = graph.get_node("architecture-doc")
    assert node.metadata["declared_task_type"] == "implementation"
    assert node.metadata["task_type"] == "documentation"
    assert node.owned_paths == ["deliverables/docs/architecture-doc/"]


def test_patch_rejects_two_active_workers_owning_same_tree(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "backend",
        "goal": "implement the backend service",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/app/backend/"],
    }]))

    with pytest.raises(ValueError, match="overlapping output ownership"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "backend-duplicate",
            "goal": "implement another backend service",
            "dependencies": ["root"],
            "owned_paths": ["deliverables/app/backend/"],
        }]))
    assert graph.get_node("backend-duplicate") is None


def test_artifact_id_is_not_compared_as_child_of_owned_directory(tmp_path):
    graph, governor = _governor(tmp_path)
    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "e2e",
        "goal": "execute the end-to-end test",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/runs/e2e/"],
        "output_artifacts": ["e2e_test_report.json"],
    }, {
        "node_id": "security",
        "goal": "execute security checks",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/runs/security/"],
        "output_artifacts": ["security_test_log.txt"],
    }]))
    assert result["added"] == ["e2e", "security"]


def test_recovery_worker_may_take_over_failed_owner(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "backend",
        "goal": "implement the backend service",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/app/backend/"],
    }]))
    graph.update_node_status("backend", "ready")
    graph.update_node_status("backend", "running")
    graph.update_node_status("backend", "failed")

    result = governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "backend-recovery",
        "goal": "repair the backend service after execution failure",
        "dependencies": ["root"],
        "owned_paths": ["deliverables/app/backend/"],
        "recovery_of": "backend",
    }]))
    recovery = graph.get_node("backend-recovery")
    assert recovery.metadata["supersedes_node_id"] == "backend"
    assert "backend-recovery" in result["added"]
    assert any(
        edge.source == "backend" and edge.target == "backend-recovery"
        and edge.edge_type == "supersedes"
        for edge in graph.edges
    )


def test_consumer_waits_for_declared_artifact(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "producer",
        "goal": "produce the API contract",
        "dependencies": ["root"],
        "output_artifacts": ["api_contract.yaml"],
    }, {
        "node_id": "consumer",
        "goal": "implement from the API contract",
        "dependencies": ["producer"],
        "input_artifacts": ["api_contract.yaml"],
    }]))
    graph.update_node_status("producer", "ready")
    graph.update_node_status("producer", "running")
    graph.update_node_status("producer", "result_submitted")
    graph.update_node_status("producer", "verifying")
    graph.update_node_status("producer", "completed")
    assert graph.promote_waiting_nodes() == []
    graph.add_artifact(Artifact(
        artifact_id="api_contract.yaml",
        node_id="producer",
        artifact_type="file",
        path="deliverables/spec/api_contract.yaml",
        description="approved API contract",
        acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
    ))
    assert graph.promote_waiting_nodes() == ["consumer"]


def test_verifier_binds_physical_files_to_logical_output_contract(tmp_path):
    graph = TaskGraph()
    producer = graph.add_node(TaskNode(
        node_id="producer",
        goal="produce the database initialization bundle",
        role="coder",
        status="verifying",
        output_artifacts=["artifact:db_init_sql"],
    ))
    graph.add_node(TaskNode(
        node_id="consumer",
        goal="implement the backend from the database contract",
        role="coder",
        status="admitted",
        dependencies=["producer"],
        input_artifacts=["artifact:db_init_sql"],
    ))
    physical_id = "A-producer-001"
    graph.add_artifact(Artifact(
        artifact_id=physical_id,
        node_id="producer",
        artifact_type="file",
        path="deliverables/app/db/init.sql",
        description="physical SQL file",
    ))
    producer.metadata["_pending_artifact_ids"] = [physical_id]

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    orchestrator.run_dir = tmp_path
    # See _finalize_orchestrator: the evidence-identity binding needs the run's
    # state manager (fix10 §7).
    orchestrator.runtime_state_manager = _state_manager(tmp_path, "finalize-binding")
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    orchestrator.memory_federation = type(
        "MemoryStub", (), {"add_fact": lambda self, fact: None}
    )()
    orchestrator.governor = GlobalGovernor(graph, orchestrator.trace_recorder)

    orchestrator._finalize_node_evidence(
        producer,
        approved=True,
        verification_score=1.0,
        verdict=VerificationVerdict.APPROVED,
    )
    graph.update_node_status("producer", "completed")

    logical = graph.artifacts["artifact:db_init_sql"]
    assert logical.node_id == "producer"
    assert logical.acceptance_status == ArtifactAcceptanceStatus.ACCEPTED
    assert logical.metadata["physical_artifact_ids"] == [physical_id]
    assert graph.promote_waiting_nodes() == ["consumer"]


def _state_manager(run_dir, run_id):
    """b33 case1 fix10 §7: the run-level manager ``_finalize_node_evidence`` needs."""
    from AstralNexus.core.runtime_state import RuntimeStateManager

    return RuntimeStateManager(run_dir, run_id)


def _finalize_orchestrator(tmp_path, graph):
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    orchestrator.run_dir = tmp_path
    # b33 case1 fix10 §7: _finalize_node_evidence now binds each verdict to an
    # evidence identity and credits progress through the run-level manager, so
    # the stub needs the same manager the real constructor builds.
    orchestrator.runtime_state_manager = _state_manager(tmp_path, "finalize-stub")
    orchestrator.trace_recorder = TraceRecorder(tmp_path)
    orchestrator.memory_federation = type(
        "MemoryStub", (), {"add_fact": lambda self, fact: None}
    )()
    orchestrator.governor = GlobalGovernor(graph, orchestrator.trace_recorder)
    return orchestrator


def test_contract_bundle_owns_its_own_file_not_the_first_accepted_one(tmp_path):
    """Live b33case1 regression: three logical outputs claimed ``run1.log``.

    The whole-node contract bundle was stamped with ``physical_paths[0]``, so
    ``artifact:deliverables_verify_case_py`` and
    ``artifact:runs_verify_logs_run2`` both stored ``run1.log`` -- a path that
    belonged to a different logical output.  That lie then travelled into the
    alias resolution in ``Governor.apply_task_graph_patch`` (rejecting a legal
    patch), into the Verifier's evidence set, and into ``output_roots``.
    """
    declared = [
        "deliverables/verify_case.py",
        "deliverables/runs/verify_logs/run1.log",
        "deliverables/runs/verify_logs/run2.log",
    ]
    for rel in declared:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("evidence\n", encoding="utf-8")

    graph = TaskGraph()
    node = graph.add_node(TaskNode(
        node_id="N-103-verify",
        goal="verify the delivered application against the contract",
        role="coder",
        status="verifying",
        output_artifacts=[
            "artifact:deliverables_verify_case_py",
            "artifact:runs_verify_logs_run1",
            "artifact:runs_verify_logs_run2",
        ],
    ))
    node.metadata["expected_outputs"] = list(declared)
    node.metadata["task_version"] = 1

    # The Worker reported the files in a different order than declared, and
    # each one twice (absolute from the framework, relative from the Worker).
    reported = [
        "deliverables/runs/verify_logs/run1.log",
        "deliverables/runs/verify_logs/run2.log",
        "deliverables/verify_case.py",
    ]
    pending: list[str] = []
    for rel in reported:
        for spelling in (str((tmp_path / rel).resolve()), rel):
            artifact_id = f"A-N-103-verify-{len(pending) + 1:03d}"
            graph.add_artifact(Artifact(
                artifact_id=artifact_id,
                node_id="N-103-verify",
                artifact_type="file",
                path=spelling,
                description="physical file reported by the Worker",
                acceptance_status=ArtifactAcceptanceStatus.PENDING.value,
                metadata={"task_version": 1, "retry_count": node.retry_count},
            ))
            pending.append(artifact_id)
    node.metadata["_pending_artifact_ids"] = pending

    _finalize_orchestrator(tmp_path, graph)._finalize_node_evidence(
        node,
        approved=True,
        verification_score=1.0,
        verdict=VerificationVerdict.APPROVED,
    )

    def tail(artifact):
        return Path(str(artifact.path).replace("\\", "/")).as_posix()

    bundles = {
        artifact_id: graph.artifacts[artifact_id]
        for artifact_id in node.output_artifacts
    }
    assert tail(bundles["artifact:deliverables_verify_case_py"]).endswith(
        "deliverables/verify_case.py"
    )
    assert tail(bundles["artifact:runs_verify_logs_run1"]).endswith(
        "deliverables/runs/verify_logs/run1.log"
    )
    assert tail(bundles["artifact:runs_verify_logs_run2"]).endswith(
        "deliverables/runs/verify_logs/run2.log"
    )
    assert len({tail(bundle) for bundle in bundles.values()}) == 3

    # One verification event is one identity: every artifact finalized in this
    # call carries the same stamp, so the Governor can recognise the absolute,
    # the relative and the bundle registrations as one reference.
    physical_stamps = {
        graph.artifacts[artifact_id].metadata["verified_at"] for artifact_id in pending
    }
    assert len(physical_stamps) == 1
    expected_stamp = next(iter(physical_stamps))
    for bundle in bundles.values():
        assert bundle.metadata["path_binding"] == "declared_output_index"
        assert bundle.metadata["retry_count"] == node.retry_count
        assert bundle.metadata["verified_at"] == expected_stamp


def test_contract_bundle_without_a_binding_claims_no_other_artifact_path(tmp_path):
    """No honest binding => no path claim.  Never a borrowed sibling path."""
    graph = TaskGraph()
    node = graph.add_node(TaskNode(
        node_id="N-200-multi",
        goal="produce two independent reports",
        role="coder",
        status="verifying",
        output_artifacts=["artifact:report_a", "artifact:report_b"],
    ))
    node.metadata["task_version"] = 1
    pending = []
    for index, rel in enumerate(("deliverables/a.md", "deliverables/b.md"), start=1):
        artifact_id = f"A-N-200-multi-{index:03d}"
        graph.add_artifact(Artifact(
            artifact_id=artifact_id,
            node_id="N-200-multi",
            artifact_type="file",
            path=rel,
            description="physical file reported by the Worker",
            acceptance_status=ArtifactAcceptanceStatus.PENDING.value,
            metadata={"task_version": 1, "retry_count": node.retry_count},
        ))
        pending.append(artifact_id)
    node.metadata["_pending_artifact_ids"] = pending

    _finalize_orchestrator(tmp_path, graph)._finalize_node_evidence(
        node,
        approved=True,
        verification_score=1.0,
        verdict=VerificationVerdict.APPROVED,
    )

    for artifact_id in node.output_artifacts:
        bundle = graph.artifacts[artifact_id]
        assert bundle.path == ""
        assert bundle.metadata["path_binding"] == "unbound"
        assert bundle.metadata["physical_paths"] == [
            graph.artifacts[pending[0]].path,
            graph.artifacts[pending[1]].path,
        ]


def test_tool_executor_enforces_worker_owned_write_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_TOOL_APPROVAL", "true")
    executor = ToolExecutor(
        workspace_root=tmp_path,
        owned_paths=["deliverables/app/backend/"],
    )
    denied = executor.execute_tool(
        "file_write",
        {"path": "deliverables/app/frontend/index.html", "content": "bad"},
    )
    allowed = executor.execute_tool(
        "file_write",
        {"path": "deliverables/app/backend/app.py", "content": "ok"},
    )
    assert denied["failure_class"] == "owned_path_violation"
    assert allowed["status"] == "success"


def test_process_tools_share_workspace_path_base_and_reject_explicit_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_TOOL_APPROVAL", "true")
    owned = tmp_path / "deliverables" / "app" / "backend"
    owned.mkdir(parents=True)
    executor = ToolExecutor(
        workspace_root=tmp_path,
        owned_paths=["deliverables/app/backend/"],
    )

    allowed = executor.execute_tool(
        "python_exec", {"code": "from pathlib import Path; print(Path.cwd())"}
    )
    denied = executor.execute_tool(
        "python_exec",
        {"code": "from pathlib import Path; Path('../frontend/index.html').write_text('bad')"},
    )

    assert allowed["status"] == "success"
    assert allowed["stdout"].strip() == str(tmp_path)
    explicit = executor.execute_tool('python_exec', {
        'cwd': 'deliverables/app/backend', 'code': 'from pathlib import Path; print(Path.cwd())',
    })
    assert explicit['stdout'].strip() == str(owned)
    assert denied["failure_class"] == "owned_path_violation"
    assert not (tmp_path / "deliverables" / "app" / "frontend" / "index.html").exists()


def test_consumer_rejects_same_named_artifact_from_unrelated_branch(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "producer",
        "goal": "produce the approved API contract",
        "dependencies": ["root"],
        "output_artifacts": ["api_contract.yaml"],
    }, {
        "node_id": "unrelated",
        "goal": "produce an unrelated draft",
        "dependencies": ["root"],
        "output_artifacts": ["draft.yaml"],
    }, {
        "node_id": "consumer",
        "goal": "implement from the approved API contract",
        "dependencies": ["producer"],
        "input_artifacts": ["api_contract.yaml"],
    }]))
    for node_id in ("producer", "unrelated"):
        graph.update_node_status(node_id, "ready")
        graph.update_node_status(node_id, "running")
        graph.update_node_status(node_id, "result_submitted")
        graph.update_node_status(node_id, "verifying")
        graph.update_node_status(node_id, "completed")
    graph.add_artifact(Artifact(
        artifact_id="api_contract.yaml",
        node_id="unrelated",
        artifact_type="file",
        path="deliverables/other/api_contract.yaml",
        description="unrelated artifact with a colliding name",
    ))

    assert graph.promote_waiting_nodes() == []


def test_worker_contract_survives_sqlite_checkpoint_resume(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="coder",
        goal="implement the backend",
        role="coder",
        status="admitted",
        dependencies=["architect"],
        owned_paths=["deliverables/app/backend/"],
        input_artifacts=["api_contract.yaml"],
        output_artifacts=["backend_source_tree"],
        handoff_contract={"schema": "api-v1", "required_fields": ["routes"]},
    ))
    store = SQLiteTaskGraphStore(tmp_path / "runtime.db")
    store.save_task_graph("contract-run", graph)

    restored = store.load_task_graph("contract-run").get_node("coder")
    assert restored.owned_paths == ["deliverables/app/backend/"]
    assert restored.input_artifacts == ["api_contract.yaml"]
    assert restored.output_artifacts == ["backend_source_tree"]
    assert restored.handoff_contract == {
        "schema": "api-v1", "required_fields": ["routes"]
    }


def test_artifact_lifecycle_survives_sqlite_checkpoint_resume(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(
        node_id="researcher", goal="draft evidence", role="researcher"
    ))
    graph.add_artifact(Artifact(
        artifact_id="draft",
        node_id="researcher",
        artifact_type="file",
        path="scratch/draft.md",
        description="temporary working draft",
        lifecycle_class="scaffold",
    ))
    store = SQLiteTaskGraphStore(tmp_path / "runtime.db")
    store.save_task_graph("artifact-run", graph)

    restored = store.load_task_graph("artifact-run").artifacts["draft"]
    assert restored.lifecycle_class == "scaffold"


def test_handoff_accepts_dependency_declared_in_add_edges(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(
        add_nodes=[
            {"node_id": "producer", "goal": "produce data", "output_artifacts": ["artifact:data"]},
            {"node_id": "consumer", "goal": "verify data", "input_artifacts": ["artifact:data"]},
        ],
        add_edges=[{"source": "producer", "target": "consumer", "edge_type": "depends_on"}],
    ))
    assert "producer" in graph.get_node("consumer").dependencies


def test_process_scope_handles_file_ownership_and_new_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_TOOL_APPROVAL", "true")
    for owned in ("deliverables/result.json", "deliverables/runs/check/"):
        executor = ToolExecutor(workspace_root=tmp_path, owned_paths=[owned])
        result = executor.execute_tool("python_exec", {"code": "print(1 + 2 + 3)"})
        assert result["status"] == "success", result
        assert result["stdout"].strip() == "6"
    assert not (tmp_path / "deliverables").exists()
    assert not (tmp_path / "deliverables/result.json").is_dir()


def test_patch_rejects_unwritable_expected_output_before_worker_execution(tmp_path):
    graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="declared output is outside owned_paths"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "synthesis", "goal": "package verified outputs",
            "owned_paths": ["deliverables/result.json"],
            "expected_outputs": ["deliverables/manifest.json"],
        }]))
    assert graph.get_node("synthesis") is None


@pytest.mark.parametrize("task_type", ["testing", "verification", "validation", "tool", "evidence", "documentation", "reporting", "analysis", "implementation"])
def test_evidence_declared_files_keep_contract_paths(tmp_path, task_type):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "report", "goal": "write report", "task_type": task_type,
        "owned_paths": ["deliverables/docs/"],
        "expected_outputs": ["deliverables/docs/architecture.md"],
    }]))
    node = graph.get_node("report")
    assert node.owned_paths == ["deliverables/docs/architecture.md"]
    assert node.metadata["expected_outputs"] == node.owned_paths


@pytest.mark.parametrize("duplicate", [False, True])
def test_shared_directory_uses_disjoint_declared_file_ownership(tmp_path, duplicate):
    graph, governor = _governor(tmp_path)
    nodes = [{
        "node_id": name, "goal": f"write evidence for {name}", "task_type": "documentation",
        "owned_paths": ["deliverables/docs/"],
        "metadata": {"expected_outputs": [f"deliverables/docs/{'a' if duplicate else name}.md"]},
    } for name in ("a", "b")]
    patch = TaskGraphPatch(add_nodes=nodes)
    if duplicate:
        with pytest.raises(ValueError, match="overlapping output ownership"):
            governor.apply_task_graph_patch(patch)
        assert graph.get_node("a") is None
    else:
        governor.apply_task_graph_patch(patch)
        assert graph.get_node("a").owned_paths == ["deliverables/docs/a.md"]
        assert graph.get_node("b").owned_paths == ["deliverables/docs/b.md"]


def test_implementation_directory_is_not_shrunk_to_minimum_outputs(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        'node_id': 'backend', 'goal': 'Implement runnable API', 'task_type': 'implementation',
        'owned_paths': ['deliverables/app/backend/'],
        'expected_outputs': ['deliverables/app/backend/schema.sql', 'deliverables/app/backend/init_db.py'],
    }]))
    assert graph.get_node('backend').owned_paths == ['deliverables/app/backend/']
    with pytest.raises(ValueError, match='overlapping output ownership'):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            'node_id': 'other', 'goal': 'Implement another module', 'task_type': 'implementation',
            'owned_paths': ['deliverables/app/backend/server.py'],
            'expected_outputs': ['deliverables/app/backend/server.py'],
        }]))


def test_majority_repair_preserves_omitted_delivery_coverage(tmp_path):
    _graph, governor = _governor(tmp_path)
    rejected = TaskGraphPatch(add_nodes=[
        {"node_id": name, "goal": name, "delivery_output_ids": [output]}
        for name, output in [("backend", "O-01"), ("frontend", "O-02"), ("docs", "O-03")]
    ])
    merged = governor.merge_rejected_task_graph_patch(
        rejected, TaskGraphPatch(add_nodes=rejected.add_nodes[:2]),
    )
    assert merged.add_nodes == rejected.add_nodes


def test_known_optional_delivery_coverage_is_allowed(tmp_path):
    graph, governor = _governor(tmp_path)
    governor.delivery_contract = {"required_outputs": [
        {"output_id": "O-01", "required": True},
        {"output_id": "O-02", "required": False, "acceptance_criteria": ["optional report"],
         "acceptance_criterion_ids": ["O-02.C01"]},
    ]}
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "worker", "goal": "deliver all outputs", "dependencies": ["root"],
        "delivery_output_ids": ["O-01", "O-02"], "delivery_criterion_ids": ["O-02.C01"],
    }]), parent_task_id="root")
    assert graph.get_node("worker") is not None


@pytest.mark.parametrize("owned,expected", [
    ("deliverables/analysis/report.md", "deliverables/analysis/report.md"),
    ("specs/report.md", "deliverables/app/specs/report.md"),
    ("deliverables/analysis/", "deliverables/analysis/report.md"),
])
def test_output_basename_resolves_unique_owned_file(tmp_path, owned, expected):
    graph, governor = _governor(tmp_path)
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "report", "goal": "write report", "owned_paths": [owned],
        "expected_outputs": ["report.md"],
    }]))
    assert graph.get_node("report").owned_paths == [expected]
    assert graph.get_node("report").metadata["expected_outputs"] == [expected]


def test_ambiguous_output_basename_is_rejected(tmp_path):
    _graph, governor = _governor(tmp_path)
    with pytest.raises(ValueError, match="outside owned_paths"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "report", "goal": "write report",
            "owned_paths": ["deliverables/a/report.md", "deliverables/b/report.md"],
            "expected_outputs": ["report.md"],
        }]))


def test_repair_can_withdraw_stale_parallelism_claim(tmp_path):
    _graph, governor = _governor(tmp_path)
    rejected = TaskGraphPatch(add_nodes=[{"node_id": "worker", "goal": "build"}],
                              reason_codes=["GOAL_DECOMPOSITION", "PARALLELIZABLE"])
    merged = governor.merge_rejected_task_graph_patch(
        rejected, TaskGraphPatch(reason_codes=["GOAL_DECOMPOSITION", "CRITICAL_PATH"]),
    )
    assert "PARALLELIZABLE" not in merged.reason_codes
    assert merged.add_nodes == rejected.add_nodes


@pytest.mark.parametrize("error,expected", [
    ("graph patch claims PARALLELIZABLE but creates one oversized initial bottleneck", "首波并行门禁"),
    ("graph patch does not cover enough required delivery criteria", "交付覆盖门禁"),
    ("graph patch has overlapping output ownership", "优先以 candidate_mode=merge"),
    ("overlapping output ownership: ['artifact_id:artifact:db_sqlite <> artifact:db_sqlite']", "单改 owned_paths 无法修复"),
])
def test_replan_feedback_matches_actual_validation_errors(tmp_path, monkeypatch, error, expected):
    _graph, governor = _governor(tmp_path)
    captured = []

    def plan(goal, **kwargs):
        captured.append(goal)
        return TaskGraphPatch()

    monkeypatch.setattr(governor, "plan_task_graph_patch", plan)
    governor.replan_rejected_task_graph_patch(
        "build", rejection_reason=error, graph_summary="", capability_catalog="",
        resource_catalog="", rejected_patch=TaskGraphPatch(),
    )
    assert expected in captured[0]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_requirement_change_repairs_atomically_and_rolls_back_version(tmp_path, monkeypatch, repair_succeeds):
    with Orchestrator(run_dir=tmp_path) as orchestrator:
        orchestrator.initialize_task_graph("build application")
        governor = orchestrator.governor
        governor.llm_matching_enabled = True
        bad = TaskGraphPatch(add_edges=[{"from": "U-001", "to": "P-001"}])
        monkeypatch.setattr(governor, "plan_requirement_change", lambda *a, **kw: {
            "patch": bad, "affected_node_ids": [], "new_constraints": ["keep audit evidence"],
        })
        repair = TaskGraphPatch(candidate_mode="replace") if repair_succeeds else bad
        monkeypatch.setattr(governor, "replan_rejected_task_graph_patch", lambda *a, **kw: repair)
        before = governor.goal_version
        if repair_succeeds:
            orchestrator.handle_requirement_change("retain evidence")
            assert governor.goal_version == before + 1
            assert any(e["event_type"] == "requirement_change.applied" for e in orchestrator.trace_recorder.events)
        else:
            with pytest.raises(ValueError, match="unknown edge endpoint"):
                orchestrator.handle_requirement_change("retain evidence")
            assert governor.goal_version == before
            assert not any(e["event_type"] == "requirement_change.applied" for e in orchestrator.trace_recorder.events)


def test_fullstack_database_contract_has_one_producer_and_explicit_consumer(tmp_path):
    graph, governor = _governor(tmp_path)
    seed = {"node_id": "seed", "goal": "initialize the fixture database",
            "owned_paths": ["deliverables/app/data/"], "output_artifacts": ["artifact:db_sqlite"]}
    backend = {"node_id": "backend", "goal": "serve maintenance APIs",
               "owned_paths": ["deliverables/app/backend/"], "output_artifacts": ["artifact:db_sqlite"]}
    rejected = TaskGraphPatch(add_nodes=[seed, backend])
    with pytest.raises(ValueError, match="artifact_id:artifact:db_sqlite"):
        governor.apply_task_graph_patch(rejected)
    assert graph.get_node("seed") is None
    repair = TaskGraphPatch(add_nodes=[{
        **backend, "output_artifacts": ["artifact:backend_source"],
        "input_artifacts": ["artifact:db_sqlite"], "dependencies": ["seed"],
    }])
    governor.apply_task_graph_patch(governor.merge_rejected_task_graph_patch(rejected, repair))
    assert graph.get_node("seed").output_artifacts == ["artifact:db_sqlite"]
    assert graph.get_node("backend").input_artifacts == ["artifact:db_sqlite"]
    assert graph.get_node("backend").dependencies == ["seed"]


def test_repair_focus_uses_current_failed_node_not_stale_reasoning(tmp_path, monkeypatch):
    _graph, governor = _governor(tmp_path)
    captured = []

    def plan(goal, **kwargs):
        captured.append(goal)
        return TaskGraphPatch()

    monkeypatch.setattr(governor, "plan_task_graph_patch", plan)
    governor.replan_rejected_task_graph_patch(
        "build fullstack", "declared output is outside owned_paths: I-002: deliverables/app/index.html",
        graph_summary="", capability_catalog="", resource_catalog="",
        rejected_patch=TaskGraphPatch(add_nodes=[
            {"node_id": "I-001", "goal": "backend"},
            {"node_id": "I-002", "goal": "frontend"},
        ], reasoning="Previously fixed I-001 startup log ownership"),
    )
    focus = captured[0].split("当前唯一阻塞诊断", 1)[1]
    assert "I-002" in focus
    assert "I-001" not in focus


def test_candidate_field_updates_repair_ownership_before_admission(tmp_path):
    graph, governor = _governor(tmp_path)
    rejected = TaskGraphPatch(add_nodes=[
        {"node_id": "schema", "goal": "define the database schema", "owned_paths": ["app/backend/schema.sql"],
         "expected_outputs": ["app/backend/schema.sql"]},
        {"node_id": "backend", "goal": "implement REST endpoints", "owned_paths": ["app/backend/"],
         "expected_outputs": ["app/backend/main.py", "app/backend/routes/"],
         "metadata": {"delivery_output_ids": ["O-01"]}},
    ])
    with pytest.raises(ValueError, match="overlapping output ownership"):
        governor.apply_task_graph_patch(rejected)
    repair = TaskGraphPatch(update_nodes=[{
        "node_id": "backend", "owned_paths": ["app/backend/main.py", "app/backend/routes/"],
        "metadata": {"repair_note": "exact source ownership"},
    }])
    merged = governor.merge_rejected_task_graph_patch(rejected, repair)
    assert merged.update_nodes == []
    assert merged.add_nodes[1]["metadata"]["delivery_output_ids"] == ["O-01"]
    governor.apply_task_graph_patch(merged)
    assert graph.get_node("backend").owned_paths == [
        "deliverables/app/backend/main.py", "deliverables/app/backend/routes/",
    ]


def test_candidate_repair_keeps_updates_for_committed_nodes(tmp_path):
    _graph, governor = _governor(tmp_path)
    merged = governor.merge_rejected_task_graph_patch(
        TaskGraphPatch(add_nodes=[{"node_id": "new", "goal": "write"}]),
        TaskGraphPatch(update_nodes=[{"node_id": "root", "goal": "updated"}]),
    )
    assert merged.update_nodes == [{"node_id": "root", "goal": "updated"}]


def test_app_prefix_normalization_is_idempotent():
    from AstralNexus.core.output_paths import normalize_worker_output_path
    expected = "deliverables/app/backend/main.py"
    assert normalize_worker_output_path("app/backend/main.py") == expected
    assert normalize_worker_output_path(expected) == expected


def test_verification_recovery_receives_file_and_handoff_contracts(tmp_path):
    import json
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id="seed", goal="initialize data", role="coder", status="repair_pending",
                            owned_paths=["deliverables/app/backend/seed.sql"],
                            output_artifacts=["artifact:seed"],
                            metadata={"delivery_output_ids": ["O-01"]}))
    captured = []

    class Client:
        def chat(self, messages):
            captured.extend(messages)
            return json.dumps({"update_nodes": [{"node_id": "seed", "status": "ready"}]})

    governor.enable_llm_worker_matching(Client())
    patch = governor.plan_verification_recovery(node_id="seed", verdict="replan_required",
                                               overall_score=0.8, dimensions=[])
    context = json.loads(captured[1]["content"])
    assert context["failed_node"]["owned_paths"] == ["deliverables/app/backend/seed.sql"]
    assert context["failed_node"]["output_artifacts"] == ["artifact:seed"]
    assert "recovery_of=failed_node.node_id" in captured[0]["content"]
    governor.apply_task_graph_patch(patch)
    assert graph.get_node("seed").status == "ready"


def test_verification_takeover_accepts_exact_failed_node_ownership(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id="failed", goal="seed", role="coder", status="repair_pending",
                            owned_paths=["deliverables/app/backend/seed.sql"],
                            output_artifacts=["artifact:seed"]))
    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "failed-repair", "goal": "retry seed",
        "owned_paths": ["deliverables/app/backend/seed.sql"],
        "output_artifacts": ["artifact:seed"], "recovery_of": "failed",
    }])
    result = governor.apply_task_graph_patch(patch)
    assert result["added"] == ["failed-repair"]
    assert graph.get_node("failed-repair").metadata["supersedes_node_id"] == "failed"


def test_recovery_ownership_transfer_removes_stale_sibling_contract(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id="failed", goal="seed", role="coder", status="failed",
                            owned_paths=["deliverables/app/backend/seed.py"],
                            output_artifacts=["artifact:seed"]))
    graph.add_node(TaskNode(node_id="backend", goal="serve API", role="coder", status="admitted",
                            owned_paths=["deliverables/app/backend/seed.py", "deliverables/app/backend/main.py"],
                            output_artifacts=["artifact:seed", "artifact:backend"]))
    patch = TaskGraphPatch(add_nodes=[{
        "node_id": "seed-repair", "goal": "retry seed", "recovery_of": "failed",
        "owned_paths": ["deliverables/app/backend/seed.py"],
        "output_artifacts": ["artifact:seed"],
    }])
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    normalized = orchestrator._normalize_recovery_ownership_transfer(patch)
    transfer = next(item for item in normalized.update_nodes if item["node_id"] == "backend")
    assert transfer["owned_paths"] == ["deliverables/app/backend/main.py"]
    assert transfer["output_artifacts"] == ["artifact:backend"]


def test_initial_patch_rejects_missing_criterion_even_at_ninety_percent(tmp_path):
    graph, governor = _governor(tmp_path)
    ids = [f'O-01.C{i:02}' for i in range(1, 11)]
    governor.delivery_contract = {'required_outputs': [{
        'output_id': 'O-01', 'required': True,
        'acceptance_criteria': [f'condition {i}' for i in range(10)],
        'acceptance_criterion_ids': ids,
    }]}
    with pytest.raises(ValueError, match='O-01.C10'):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            'node_id': 'partial', 'goal': 'implement nine required conditions',
            'dependencies': ['root'], 'delivery_output_ids': ['O-01'],
            'delivery_criterion_ids': ids[:-1],
        }]), parent_task_id='root')
    assert graph.get_node('partial') is None


def test_recovery_takeover_supersedes_original_even_if_model_requeues_it(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id='failed', goal='implement', role='coder', status='repair_pending',
                            owned_paths=['deliverables/backend/']))
    patch = TaskGraphPatch(add_nodes=[{
        'node_id': 'repair', 'goal': 'repair implementation', 'recovery_of': 'failed',
        'owned_paths': ['deliverables/backend/'],
    }], update_nodes=[{'node_id': 'failed', 'status': 'ready'}])
    governor.apply_task_graph_patch(patch)
    assert graph.get_node('failed').status == 'superseded'
    assert 'failed' not in [node.node_id for node in graph.get_ready_nodes()]


def test_recovery_transfer_rebinds_waiting_consumers(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id='failed', goal='implement', role='coder', status='repair_pending',
                            owned_paths=['deliverables/backend/'], output_artifacts=['api']))
    graph.add_node(TaskNode(node_id='test', goal='test', role='tester', status='admitted',
                            dependencies=['failed'], input_artifacts=['api']))
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.task_graph = graph
    patch = orchestrator._normalize_recovery_ownership_transfer(TaskGraphPatch(add_nodes=[{
        'node_id': 'repair', 'goal': 'repair', 'recovery_of': 'failed',
        'owned_paths': ['deliverables/backend/'], 'output_artifacts': ['api'],
    }]))
    governor.apply_task_graph_patch(patch)
    assert graph.get_node('test').dependencies == ['repair']
    assert graph.get_node('failed').status == 'superseded'
    graph.promote_waiting_nodes()
    assert graph.get_node('test').status != 'ready'


def test_takeover_may_preserve_retired_owners_historical_paths(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id='old', goal='frontend', role='coder', status='repair_pending',
                            owned_paths=['deliverables/frontend/']))
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        'node_id': 'replacement', 'goal': 'repair frontend', 'recovery_of': 'old',
        'owned_paths': ['deliverables/frontend/'],
    }], update_nodes=[{
        'node_id': 'old', 'status': 'repair_pending', 'owned_paths': ['deliverables/frontend/'],
    }]))
    assert graph.get_node('old').status == 'superseded'
    assert graph.get_node('replacement') is not None


def test_whole_producer_takeover_preserves_component_responsibility(tmp_path):
    graph, governor = _governor(tmp_path)
    graph.add_node(TaskNode(node_id="backend", goal="working backend", role="coder", status="failed",
                            owned_paths=["deliverables/app/backend/"], output_artifacts=["artifact:backend"],
                            metadata={"expected_outputs": ["deliverables/app/backend/init_db.py"]}))
    governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
        "node_id": "repair", "goal": "fix main", "recovery_of": "backend",
        "owned_paths": ["deliverables/app/backend/main.py"],
        "expected_outputs": ["deliverables/app/backend/main.py"],
    }]))
    replacement = graph.get_node("repair")
    assert "deliverables/app/backend/" in replacement.owned_paths
    assert "artifact:backend" in replacement.output_artifacts
    assert "deliverables/app/backend/init_db.py" in replacement.metadata["expected_outputs"]
    assert graph.get_node("backend").status == "superseded"
