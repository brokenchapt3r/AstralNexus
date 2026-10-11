from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder
import pytest
from AstralNexus.core.orchestrator import Orchestrator


def test_scheduler_forwards_recovery_choice_and_failure_diagnostics(tmp_path, monkeypatch):
    with Orchestrator(tmp_path, run_id='recovery-wiring') as orchestrator:
        orchestrator.task_graph.add_node({
            'node_id': 'check', 'goal': 'Execute checks', 'role': 'tester', 'status': 'ready',
            'metadata': {'approved_worker_type': 'Testing'},
        })
        matched_tasks = []
        def match(task, *args, **kwargs):
            matched_tasks.append(task)
            return {'selected': None, 'reason_codes': ['TEST_CAPABILITY_GAP']}
        monkeypatch.setattr(orchestrator.governor, 'select_worker_profile', match)
        assert orchestrator.execute_task('check') is False
        assert matched_tasks[0]['approved_worker_type'] == 'Testing'
        diagnosed = []
        def recover(fault):
            diagnosed.append(fault.payload)
            return {'success': False}
        monkeypatch.setattr(orchestrator, '_execute_governed_fault', recover)
        assert orchestrator._attempt_recovery('check') is False
        assert diagnosed[0]['last_failure']['error_type'] == 'CapabilityGap'
        assert diagnosed[0]['worker_selection']['reason_codes'] == ['TEST_CAPABILITY_GAP']
        assert diagnosed[0]['approved_worker_type'] == 'Testing'
        assert diagnosed[0]['repeat_count'] == 1


def test_recovery_assignment_reused_without_second_matcher_call(tmp_path, monkeypatch):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    def unexpected(*args):
        raise AssertionError('recovery decision must not be overwritten by matcher')
    monkeypatch.setattr(governor, '_llm_worker_match', unexpected)
    result = governor.select_worker_profile({
        'required_capabilities': ['shell_commands'], 'approved_worker_type': 'Tool',
    }, HETEROGENEOUS_WORKERS)
    assert result['selected']['worker_type'] == 'Tool'
    assert 'GOVERNOR_RECOVERY_ASSIGNMENT' in result['reason_codes']


def test_recovery_assignment_cannot_bypass_implementation_boundary(tmp_path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    result = governor.select_worker_profile({
        'task_type': 'implementation', 'required_capabilities': ['code_generation'],
        'approved_worker_type': 'Tool',
    }, HETEROGENEOUS_WORKERS)
    assert result['selected']['worker_type'] == 'Coding'


def test_unknown_recovery_worker_is_explicit_failure(tmp_path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    with pytest.raises(ValueError, match='not executable'):
        governor.select_worker_profile({'approved_worker_type': 'unknown'}, HETEROGENEOUS_WORKERS)


def test_database_query_does_not_turn_acceptance_into_implementation(tmp_path, monkeypatch):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    monkeypatch.setattr(governor, '_llm_worker_match', lambda *args: {
        'worker_type': 'Testing', 'confidence': .95, 'reason_codes': ['TEST_EXECUTION'],
    })
    result = governor.select_worker_profile({
        'task_type': 'verification',
        'goal': 'Run an API flow and query database state before and after requests',
        'required_capabilities': ['end_to_end_testing', 'api_testing', 'database_query', 'test_execution'],
        'expected_outputs': ['reports/runtime.json'],
    }, HETEROGENEOUS_WORKERS)
    assert result['selected']['worker_type'] == 'Testing'


def test_delivery_workers_are_real_heterogeneous_profiles():
    architect = HETEROGENEOUS_WORKERS["Architect"]
    coding = HETEROGENEOUS_WORKERS["Coding"]
    testing = HETEROGENEOUS_WORKERS["Testing"]
    verifier = HETEROGENEOUS_WORKERS["Verifier"]
    synthesizer = HETEROGENEOUS_WORKERS["Synthesizer"]

    assert "system_architecture" in architect.capabilities
    assert "database_modeling" in architect.capabilities
    assert coding.model_preference == "coding_specialized"
    assert testing.model_preference == "testing_balanced"
    assert verifier.model_preference == "verifier_reasoning"
    assert "delivery_packaging" in synthesizer.capabilities


def test_reasoning_attributes_are_not_matchable_capabilities():
    forbidden = {"logical_reasoning", "analysis", "reasoning"}
    for profile in HETEROGENEOUS_WORKERS.values():
        assert not (forbidden & set(profile.capabilities))
        assert profile.reasoning_level in {"low", "medium", "high"}


def test_file_contract_excludes_workers_without_materialization_tools(tmp_path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    profiles = [
        {"worker_type": "Narrator", "capabilities": ["data_generation"], "tools": []},
        {"worker_type": "FileWorker", "capabilities": ["data_generation"], "tools": ["file_write"]},
    ]
    result = governor.select_worker_profile({
        "node_id": "write-json", "required_capabilities": ["data_generation"],
        "expected_outputs": ["deliverables/result.json"],
    }, profiles)
    assert result["selected"]["worker_type"] == "FileWorker"


def test_capability_policy_routes_distinct_delivery_workers(tmp_path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    cases = [
        (["system_architecture", "api_design"], "Architect", "qwen3.7-max"),
        (["code_generation", "backend_development"], "Coding", "qwen3-coder-plus"),
        (["test_execution", "integration_testing"], "Testing", "qwen-plus"),
        (["evidence_check", "acceptance_test"], "Verifier", "qwen3.7-max"),
        (["artifact_integration", "delivery_packaging"], "Synthesizer", "qwen3.7-max"),
    ]
    for capabilities, worker_type, model in cases:
        decision = governor.select_worker_profile(
            {"task_id": worker_type, "required_capabilities": capabilities},
            HETEROGENEOUS_WORKERS,
        )
        assert decision["selected"]["worker_type"] == worker_type
        assert decision["selected"]["selected_model"] == model
