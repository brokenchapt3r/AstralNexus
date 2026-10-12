import pytest

from AstralNexus.core.governor import GlobalGovernor, TaskGraphPatch
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.runtime_state import RuntimeStateManager
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.write_authority import committed_transfers
from AstralNexus.runtime.trace import TraceRecorder


def test_evidence_catalog_rejects_wrong_versions_and_changed_bytes(tmp_path):
    import hashlib
    from AstralNexus.core.task_graph import Artifact, ArtifactAcceptanceStatus
    from AstralNexus.runtime.evidence_catalog import EvidenceCatalog
    path = tmp_path / "proof.txt"
    path.write_text("passed", encoding="utf-8")
    graph = TaskGraph()
    graph.add_artifact(Artifact(artifact_id="proof", node_id="", artifact_type="file", description="proof",
        path="proof.txt", acceptance_status=ArtifactAcceptanceStatus.ACCEPTED,
        metadata={"task_version": 1, "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    assert EvidenceCatalog(tmp_path, graph=graph, task_version=1).read_text("proof.txt") == "passed"
    assert EvidenceCatalog(tmp_path, graph=graph, task_version=2).read_text("proof.txt") is None
    path.write_text("different", encoding="utf-8")
    assert EvidenceCatalog(tmp_path, graph=graph, task_version=1).read_text("proof.txt") is None
    outside = tmp_path.parent / "outside-proof.txt"
    outside.write_text("outside", encoding="utf-8")
    assert EvidenceCatalog(tmp_path).read_text(outside) is None


def test_published_binding_declares_the_source_and_destination_bases(tmp_path):
    from AstralNexus.runtime.workspace_publish import publish_deliverables, read_binding
    from tests.publish_support import make_run, make_workspace
    run, source = make_run(tmp_path, {"README.md": b"project"})
    workspace = make_workspace(tmp_path)
    result = publish_deliverables(run_dir=run, workspace_root=workspace, delivery_id="mapping")
    bases = read_binding(workspace, "mapping")["path_bases"]
    assert bases["source"] == str(source.resolve())
    assert bases["published"] == result.published_root
    assert bases["file_keys_relative_to"] == "source_and_published"


def test_foreground_timeout_reaps_a_real_descendant(tmp_path):
    import os
    import sys
    import psutil  # type: ignore[import-untyped]
    from AstralNexus.tools.builtin_tools import _run_foreground_command
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"open({str(child_pid_file)!r},'w').write(str(p.pid)); time.sleep(30)"
    )
    result = _run_foreground_command([sys.executable, "-c", code], cwd=tmp_path,
                                     env=os.environ.copy(), timeout=2)
    assert result["timed_out"] is True
    assert child_pid_file.is_file()
    child_pid = int(child_pid_file.read_text())
    try:
        assert psutil.Process(child_pid).status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.NoSuchProcess:
        pass


def test_rejected_repair_proposal_never_grants_write_authority(tmp_path):
    with Orchestrator(tmp_path, run_id="atomic-grant") as runtime:
        runtime.task_graph.add_node(TaskNode(node_id="author", goal="author", role="coder",
                                             status="completed", owned_paths=["deliverables/app.py"]))
        patch = TaskGraphPatch(add_nodes=[{"node_id": "repair", "goal": "repair", "role": "coder",
                                          "owned_paths": [], "dependencies": ["missing"]}])
        runtime._grant_completed_root_cause_paths(patch, {"app.py": ("author", ["deliverables/app.py"])})
        assert runtime.runtime_state_manager.state.ownership_transfers == []
        with pytest.raises(ValueError):
            runtime.governor.apply_task_graph_patch(patch)
        assert committed_transfers(runtime.task_graph.nodes) == []
        assert set(runtime.task_graph.nodes.keys()) == {"author"}


def test_committed_holder_is_checked_even_without_original_owned_paths(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="holder", goal="holder", role="coder", status="ready"))
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    governor.ownership_transfers_provider = lambda: [{"path": "deliverables/app.py", "to_node": "holder"}]
    with pytest.raises(ValueError, match="current write authority conflict.*other.*holder"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "other", "goal": "repair", "role": "coder", "owned_paths": ["deliverables/app.py"],
        }]))
    assert set(graph.nodes.keys()) == {"holder"}


def test_transfer_history_allows_returning_to_a_previous_holder(tmp_path):
    manager = RuntimeStateManager(tmp_path, "grant-history")
    assert manager.record_ownership_transfer(path="a.py", from_node="A", to_node="B")
    assert not manager.record_ownership_transfer(path="a.py", from_node="A", to_node="B")
    assert manager.record_ownership_transfer(path="a.py", from_node="B", to_node="A")
    assert manager.record_ownership_transfer(path="a.py", from_node="A", to_node="B")
    assert manager.current_write_holder("a.py") == "B"




def test_cost_budget_cannot_be_bypassed_by_rotating_lineages(tmp_path):
    with Orchestrator(tmp_path, run_id="rotating") as runtime:
        runtime.recovery_churn_budget = 3
        for root in ("A", "B", "C"):
            runtime.runtime_state_manager.record_lineage_cost(root, recoveries=1)
        verdict = runtime._lineage_stand_down_verdict()
        assert verdict is not None and verdict["stop_code"] == "cost_budget_exhausted"
        assert verdict["cost"]["recoveries"] == 3


def test_actual_streaming_and_response_events_charge_the_delivery_ledger(tmp_path):
    with Orchestrator(tmp_path, run_id="actual-usage") as runtime:
        runtime.task_graph.add_node(TaskNode(node_id="active", goal="active", role="coder", status="running"))
        runtime._record_lineage_event_cost("__delivery__", "llm.request", {})
        runtime._record_lineage_event_cost("__delivery__", "llm.response", {"prompt_tokens": 100, "completion_tokens": 20})
        runtime._record_lineage_event_cost("__delivery__", "llm.stream.started", {})
        for count in (50, 50, 70):
            runtime._record_lineage_event_cost("__delivery__", "llm.usage", {"prompt_tokens": count, "completion_tokens": 10})
        entry = runtime.runtime_state_manager.lineage_entry("__delivery__")
        assert entry["requests"] == 2 and entry["tokens"] == 200
        assert runtime.runtime_state_manager.lineage_entry("active")["tokens"] == 0


def test_staged_grant_cannot_claim_another_authors_file(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="author", goal="author", role="coder", status="completed", owned_paths=["deliverables/a.py"]))
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    with pytest.raises(ValueError, match="invalid staged write transfer"):
        governor.apply_task_graph_patch(TaskGraphPatch(add_nodes=[{
            "node_id": "repair", "goal": "repair", "role": "coder", "owned_paths": ["deliverables/b.py"],
            "metadata": {"write_permission_transfers": [{"path": "deliverables/b.py", "from_node": "author", "to_node": "repair"}]},
        }]))
    assert set(graph.nodes.keys()) == {"author"}
