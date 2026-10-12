from AstralNexus.core.state_machine import TaskStatus
from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.message_protocol import StructuredMessage


def test_interrupted_worker_remains_ready_and_checkpointed(tmp_path, monkeypatch):
    from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
    from AstralNexus.agents.coder import Coder
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="interrupted-worker") as orchestrator:
        node = TaskNode(
            node_id="coder-1", role="coder", goal="implement a Python function",
            status="ready", metadata={"required_capabilities": ["code_generation"]},
        )
        orchestrator.task_graph.add_node(node)
        agent = Coder()
        monkeypatch.setattr(agent, "execute", lambda context: AgentResponse(
            outcome=ExecutionOutcome.INTERRUPTED, content="user interrupted",
        ))
        monkeypatch.setattr(orchestrator, "_instantiate_governed_worker", lambda *args: agent)
        assert orchestrator.execute_task(node.node_id) is None
        assert node.status == "ready"
        assert orchestrator.graph_store.load_task_graph("interrupted-worker").get_node(node.node_id).status == "ready"


def test_checkpoint_round_trip_rearms_running_node(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_iterations=1, run_id="resume-test")
    orchestrator.task_graph.add_node({
        "node_id": "T-001",
        "goal": "resume me",
        "role": "coder",
        "status": "running",
        "dependencies": [],
        "constraints": ["preserve API"],
        "metadata": {"kind": "test"},
    })
    orchestrator.iteration_count = 4
    orchestrator._save_checkpoint()

    restored = Orchestrator(tmp_path, max_iterations=1, run_id="resume-test")
    assert restored.restore_checkpoint() is True
    node = restored.task_graph.get_node("T-001")
    assert node is not None
    assert node.status == "ready"
    assert node.metadata["kind"] == "test"
    assert restored.iteration_count == 4


def test_failed_worker_checkpoint_does_not_resume_as_running(tmp_path, monkeypatch):
    from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
    from AstralNexus.agents.coder import Coder
    from AstralNexus.core.task_graph import TaskNode

    with Orchestrator(tmp_path, run_id="failed-worker") as orchestrator:
        node = TaskNode(node_id="coder-1", role="coder", goal="implement a Python function",
                        status="ready", metadata={"required_capabilities": ["code_generation"]})
        orchestrator.task_graph.add_node(node)
        agent = Coder()
        monkeypatch.setattr(agent, "execute", lambda context: AgentResponse(
            outcome=ExecutionOutcome.FAILED, content="startup import failed",
        ))
        monkeypatch.setattr(orchestrator, "_instantiate_governed_worker", lambda *args: agent)
        assert orchestrator.execute_task(node.node_id) is False
        saved = orchestrator.graph_store.load_task_graph("failed-worker").get_node(node.node_id)
        assert saved.status == "failed"
        assert "startup import failed" in saved.metadata["last_failure"]["message"]
    with Orchestrator(tmp_path, run_id="failed-worker") as restored:
        assert restored.restore_checkpoint()
        assert restored.task_graph.get_node("coder-1").status == "failed"


def test_resume_reconciles_failure_committed_before_graph_checkpoint(tmp_path):
    from AstralNexus.runtime.execution_store import ExecutionStore
    with Orchestrator(tmp_path, run_id="crash-after-failure") as orchestrator:
        execution = ExecutionStore(tmp_path).begin("tester-1", {})
        orchestrator.task_graph.add_node({
            "node_id": "tester-1", "role": "tester", "goal": "verify startup",
            "status": "running", "metadata": {"runtime_execution_id": execution},
        })
        orchestrator._save_checkpoint()
        ExecutionStore(tmp_path).finish(execution, "failed", {"error": "startup import failed"})
    with Orchestrator(tmp_path, run_id="crash-after-failure") as restored:
        assert restored.restore_checkpoint()
        node = restored.task_graph.get_node("tester-1")
        assert node.status == "failed"
        assert node.metadata["last_failure"]["message"] == "startup import failed"


def test_checkpoint_serializes_structured_messages_in_metadata(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_iterations=1, run_id="message-test")
    message = StructuredMessage(
        node_id="T-001",
        role="researcher",
        status=TaskStatus.COMPLETED,
        summary="evidence collected",
        claims=[{"text": "claim", "confidence": 0.9}],
    )
    orchestrator.task_graph.add_node({
        "node_id": "T-001",
        "goal": "persist structured state",
        "role": "researcher",
        "status": "ready",
        "dependencies": [],
        "metadata": {"last_message": message},
    })

    orchestrator._save_checkpoint()
    persisted_graph = orchestrator.graph_store.load_task_graph("message-test")
    assert persisted_graph is not None
    persisted_node = persisted_graph.get_node("T-001")
    assert persisted_node is not None
    persisted = persisted_node.metadata["last_message"]
    assert persisted["node_id"] == "T-001"
    assert persisted["claims"][0]["confidence"] == 0.9


def test_checkpoint_does_not_duplicate_graph_into_runtime_state(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_iterations=1, run_id="ssot-test")
    orchestrator.task_graph.add_node({
        "node_id": "T-001",
        "goal": "single source",
        "role": "researcher",
        "status": "ready",
    })
    orchestrator._save_checkpoint()

    state = __import__("json").loads(
        (tmp_path / "runtime_state.json").read_text(encoding="utf-8")
    )
    assert "task_graph" not in state
    assert (tmp_path / "task_graph.sqlite").exists()
    assert not (tmp_path / "checkpoint.json").exists()


def test_resume_verifies_submitted_results_without_reexecuting_workers(tmp_path, monkeypatch):
    import pytest
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.core.verifier import VerificationVerdict, VerificationResult
    for status in ['result_submitted', 'verifying']:
        run_dir = tmp_path / status
        with Orchestrator(run_dir, run_id=status, max_iterations=1) as original:
            original.task_graph.add_node(TaskNode(node_id='producer', goal='deliver result',
                                                   role='coder', status=status))
            original._save_checkpoint()
        with Orchestrator(run_dir, run_id=status, max_iterations=1) as restored:
            assert restored.restore_checkpoint()
            checked = []
            def verify(node_id):
                checked.append(node_id)
                return VerificationResult(node_id=node_id, verdict=VerificationVerdict.APPROVED,
                                          dimensions=[], overall_score=1.0)
            monkeypatch.setattr(restored.verifier, 'verify_node', verify)
            monkeypatch.setattr(restored, '_execute_wave', lambda *a: pytest.fail('worker rerun'))
            monkeypatch.setattr(restored, '_replan_stalled_graph', lambda **kw: pytest.fail('false stall'))
            restored.run()
            assert checked == ['producer']
            assert restored.task_graph.get_node('producer').status == 'completed'


def test_checkpoint_preserves_governance_contract_and_goal(tmp_path):
    from AstralNexus.core.governor import GoalSpec
    with Orchestrator(tmp_path, run_id='governance') as original:
        original.governor.goal_spec = GoalSpec(goal_id='G1', task_goal='original requirements',
                                              normalized_goal='normalized', hard_constraints=['local only'])
        original.governor.delivery_contract = {'required_outputs': [{'output_id': 'O-01'}],
                                                 'execution_evidence_policy': 'runtime_required'}
        original.governor.goal_version = 3
        original._save_checkpoint()
    with Orchestrator(tmp_path, run_id='governance') as restored:
        assert restored.restore_checkpoint()
        assert restored.governor.goal_spec.task_goal == 'original requirements'
        assert restored.governor.goal_spec.hard_constraints == ['local only']
        assert restored.governor.delivery_contract['execution_evidence_policy'] == 'runtime_required'
        assert restored.governor.delivery_contract['required_outputs'] == [{'output_id': 'O-01', 'required': True}]
        assert restored.governor.delivery_contract['contract_schema_version'] == 2
        assert restored.governor.delivery_contract['summary_required'] is False
        assert restored.governor.goal_version == 3


def test_legacy_checkpoint_cannot_run_original_and_repair_writers(tmp_path):
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path, run_id='legacy-takeover') as original:
        original.task_graph.add_node(TaskNode(node_id='old', goal='backend', role='coder', status='ready',
                                              owned_paths=['app/backend/']))
        original.task_graph.add_node(TaskNode(node_id='repair', goal='repair backend', role='coder', status='ready',
                                              owned_paths=['app/backend/'], metadata={'recovery_of': 'old'}))
        original.task_graph.add_node(TaskNode(node_id='test', goal='verify', role='tester', status='admitted',
                                              dependencies=['old']))
        original._save_checkpoint()
    with Orchestrator(tmp_path, run_id='legacy-takeover') as restored:
        assert restored.restore_checkpoint()
        assert restored.task_graph.get_node('old').status == 'superseded'
        assert restored.task_graph.get_node('test').dependencies == ['repair']
        assert [node.node_id for node in restored.task_graph.get_ready_nodes()] == ['repair']


def test_restore_rearms_old_ownership_rejection_once_and_keeps_diagnostic(tmp_path):
    from AstralNexus.core.task_graph import TaskNode
    reason = 'invalid governed verification recovery patch: graph patch update has overlapping output ownership: old conflicts with repair'
    with Orchestrator(tmp_path, run_id='ownership-migration') as original:
        original.runtime_state_manager.state.checkpoint_schema_version = 0
        original.task_graph.add_node(TaskNode(node_id='frontend', goal='frontend', role='coder', status='failed',
            metadata={'_terminal_failure': True, '_terminal_reason': reason, '_recovery_patch_rejected': True}))
        original._save_checkpoint()
    with Orchestrator(tmp_path, run_id='ownership-migration') as restored:
        assert restored.restore_checkpoint()
        node = restored.task_graph.get_node('frontend')
        assert node.status == 'ready'
        assert node.metadata['_terminal_reason'] == reason
        assert not node.metadata['_terminal_failure']
        restored.task_graph.update_node_status('frontend', 'running')
        restored.task_graph.update_node_status('frontend', 'failed')
        node.metadata['_recovery_patch_rejected'] = True
        node.metadata['_terminal_failure'] = True
        restored._save_checkpoint()
    with Orchestrator(tmp_path, run_id='ownership-migration') as again:
        assert again.restore_checkpoint()
        assert again.task_graph.get_node('frontend').status == 'failed'


def test_resume_restores_retired_ancestor_component_ownership(tmp_path):
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path, run_id="ownership-chain") as original:
        original.task_graph.add_node(TaskNode(node_id="old", role="coder", goal="component", status="superseded",
            owned_paths=["deliverables/backend/"], metadata={"expected_outputs": ["deliverables/backend/init.py"]}))
        original.task_graph.add_node(TaskNode(node_id="repair", role="coder", goal="repair", status="ready",
            owned_paths=["deliverables/backend/main.py"], metadata={"recovery_of": "old"}))
        original._save_checkpoint()
    with Orchestrator(tmp_path, run_id="ownership-chain") as resumed:
        assert resumed.restore_checkpoint()
        node = resumed.task_graph.get_node("repair")
        assert "deliverables/backend/" in node.owned_paths
        assert "deliverables/backend/init.py" in node.metadata["expected_outputs"]
        assert resumed.task_graph.get_node("old").status == "superseded"
