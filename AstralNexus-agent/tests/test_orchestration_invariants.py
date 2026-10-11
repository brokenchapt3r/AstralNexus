"""Behavioral regressions for checkpoint authority and verified run accounting."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import Fact
from AstralNexus.core.verifier import VerificationVerdict
from AstralNexus.runtime.run_coordinator import RunCoordinator


@pytest.mark.parametrize('failure', ['exception', 'none', 'incomplete', 'invalid_generation'])
def test_configured_fence_failure_cannot_write_graph_or_cache(tmp_path, failure):
    with Orchestrator(tmp_path, run_id='fenced') as runtime:
        runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'coder'})
        def provider():
            if failure == 'exception':
                raise RuntimeError('token service unavailable')
            if failure == 'invalid_generation':
                return {'runs_db_path': 'unused', 'run_id': 'invalid_generation', 'owner_id': 'A',
                        'generation': 0, 'now_iso': 'now'}
            return None if failure == 'none' else {'generation': 1}
        runtime.fence_token_provider = provider
        before = runtime.runtime_state_manager.path.read_bytes() if runtime.runtime_state_manager.path.exists() else None
        assert runtime._persist_graph_checkpoint(stage='test') is False
        assert not runtime.graph_store.load_task_graph('fenced').nodes
        assert runtime.graph_store.latest_commit_fence('fenced') is None
        after = runtime.runtime_state_manager.path.read_bytes() if runtime.runtime_state_manager.path.exists() else None
        assert after == before
        assert runtime._commit_seq == 0
        assert runtime._ownership_lost
        runtime.fence_token_provider = None
        assert runtime._persist_graph_checkpoint(stage='retry') is False


def _fenced_runtime(tmp_path):
    coordinator = RunCoordinator(tmp_path, run_id='joint', lease_seconds=120)
    lease = coordinator.acquire('writer')
    assert lease is not None
    runtime = Orchestrator(tmp_path, run_id='joint',
                           fence_token_provider=lambda: coordinator.fence_token(lease))
    runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'coder', 'status': 'completed'})
    return runtime, coordinator, lease


def test_concurrent_checkpoint_serializes_preparation_commit_and_confirmation(tmp_path):
    runtime, coordinator, lease = _fenced_runtime(tmp_path)
    with runtime:
        entered = threading.Event()
        release = threading.Event()
        calls = []
        confirmations = []
        def provider():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return coordinator.fence_token(lease)
        def confirm(payload):
            fence = runtime.graph_store.latest_commit_fence('joint')
            assert fence['commit_seq'] == payload['commit_seq']
            assert fence['cursor']['last_commit_seq'] == payload['commit_seq']
            cache = json.loads(runtime.runtime_state_manager.path.read_text(encoding='utf-8'))
            assert cache['last_commit_seq'] == payload['commit_seq']
            confirmations.append(payload['commit_seq'])
            return True
        runtime.fence_token_provider = provider
        runtime.checkpoint_sink = confirm
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runtime._persist_graph_checkpoint, stage='A')
            assert entered.wait(5)
            second = pool.submit(runtime._persist_graph_checkpoint, stage='B')
            time.sleep(.1)
            assert calls == [1], 'second checkpoint prepared while first still held the commit boundary'
            release.set()
            assert first.result(timeout=5) and second.result(timeout=5)
        assert confirmations == [1, 2]
        assert runtime.graph_store.latest_commit_fence('joint')['commit_seq'] == 2


def test_missing_json_restores_db_cursor_and_default_run_identity(tmp_path):
    runtime, _, _ = _fenced_runtime(tmp_path)
    with runtime:
        runtime.runtime_state_manager.state.iteration = 12
        assert runtime._persist_graph_checkpoint(stage='before-crash')
        runtime.runtime_state_manager.path.unlink()
    with Orchestrator(tmp_path) as restored:
        assert restored.run_id == 'joint'
        assert restored.restore_checkpoint()
        assert restored.runtime_state_manager.state.iteration == 12
        assert restored.runtime_state_manager.state.last_commit_seq == 1
        assert restored._commit_seq == 1
        assert restored.task_graph.get_node('N').status == 'completed'


def test_default_memory_store_persists_facts_across_restart(tmp_path):
    with Orchestrator(tmp_path, run_id='memory') as runtime:
        assert runtime.memory_federation.store is runtime.graph_store
        runtime.memory_federation.add_fact(Fact(fact_id='durable', text='keep me', source='test', confidence=1.0, recorded_at='now'))
    with Orchestrator(tmp_path, run_id='memory') as restored:
        assert restored.memory_federation.medium_term_memory['durable'].text == 'keep me'


@pytest.mark.parametrize('dimension', ['recoveries', 'tokens', 'requests'])
def test_multiple_lineages_cross_run_budget_returns_blocked_not_keyerror(tmp_path, monkeypatch, dimension):
    monkeypatch.setenv('ASTRALNEXUS_TOTAL_RECOVERY_OPERATIONS', '10')
    monkeypatch.setenv('ASTRALNEXUS_RUN_TOKEN_BUDGET', '10')
    monkeypatch.setenv('ASTRALNEXUS_RUN_REQUEST_BUDGET', '10')
    with Orchestrator(tmp_path, run_id='budget', max_iterations=1) as runtime:
        runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'coder', 'status': 'ready'})
        for root in ['A', 'B']:
            runtime.runtime_state_manager.record_lineage_cost(root, **{dimension: 6})
        result = runtime.run(goal='test')
        assert result['status'] == 'blocked'
        assert result['stop_code'] == 'cost_budget_exhausted'
        assert result['completed_tasks'] == 0
        event = next(e for e in runtime.trace_recorder.events if e['event_type'] == 'orchestrator.blocked')
        assert event['payload']['state'] == 'cost_exhausted'


@pytest.mark.parametrize('verdict', [VerificationVerdict.REPAIR_REQUIRED, VerificationVerdict.REPLAN_REQUIRED, VerificationVerdict.APPROVED])
def test_worker_success_is_not_completion_until_approved(tmp_path, monkeypatch, verdict):
    with Orchestrator(tmp_path, run_id='counts') as runtime:
        runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'coder', 'status': 'result_submitted'})
        vr = SimpleNamespace(verdict=verdict, overall_score=1.0, dimensions=[])
        monkeypatch.setattr(runtime.verifier, 'verify_node', lambda _: vr)
        monkeypatch.setattr(runtime, '_finalize_node_evidence', lambda *a, **kw: None)
        monkeypatch.setattr(runtime, '_record_worker_verdict', lambda *a, **kw: None)
        monkeypatch.setattr(runtime, '_apply_governed_verification_recovery', lambda *a, **kw: {})
        result = {'completed_tasks': 99, 'failed_tasks': 99, 'recovery_attempts': 0}
        runtime._process_worker_results({'N': True}, result)
        assert result['completed_tasks'] == int(verdict == VerificationVerdict.APPROVED)
        assert result['failed_tasks'] == 0
        runtime._refresh_result_counts(result)
        assert result['completed_tasks'] == int(verdict == VerificationVerdict.APPROVED)


def test_sink_rejection_reports_failure_and_blocks_followup_writes(tmp_path):
    with Orchestrator(tmp_path, run_id='sink', checkpoint_sink=lambda _: False) as runtime:
        runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'coder'})
        assert runtime._persist_graph_checkpoint(stage='committed-but-lost') is False
        assert runtime.graph_store.load_task_graph('sink').get_node('N') is not None
        assert runtime._ownership_lost
        runtime.task_graph.add_node({'node_id': 'late', 'goal': 'test', 'role': 'coder'})
        assert runtime._persist_graph_checkpoint(stage='late') is False
        assert runtime.graph_store.load_task_graph('sink').get_node('late') is None


@pytest.mark.parametrize('kind', ['mixed', 'event_only', 'ownership_lost', 'change_only'])
def test_stop_and_requirement_changes_do_not_overwrite_each_other(tmp_path, monkeypatch, kind):
    from queue import Queue
    with Orchestrator(tmp_path, run_id='interrupt') as runtime:
        event = threading.Event()
        queue = Queue()
        runtime.attach_interrupt_control(event, queue)
        monkeypatch.setattr(runtime, 'handle_requirement_change', lambda *a, **kw: None)
        if kind in {'mixed', 'ownership_lost', 'change_only'}:
            queue.put({'type': 'requirement_change', 'description': 'new requirement'})
        if kind == 'mixed':
            queue.put({'type': 'interrupt', 'message': 'stop'})
        if kind == 'ownership_lost':
            runtime._ownership_lost = True
        event.set()
        control = runtime._process_pending_interrupts()
        assert control['changed'] == (kind == 'change_only')
        if kind in {'mixed', 'ownership_lost'}:
            assert control['deferred_changes'] == 1
            assert queue.qsize() == 1
        assert control['stop'] == (kind != 'change_only')


def test_run_stops_when_cancel_and_requirement_change_coexist(tmp_path, monkeypatch):
    from queue import Queue
    with Orchestrator(tmp_path, run_id='mixed-control', max_iterations=1) as runtime:
        runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'coder', 'status': 'ready'})
        event = threading.Event()
        queue = Queue()
        queue.put({'type': 'requirement_change', 'description': 'change'})
        queue.put({'type': 'interrupt', 'message': 'explicit stop'})
        event.set()
        runtime.attach_interrupt_control(event, queue)
        monkeypatch.setattr(runtime, 'handle_requirement_change', lambda *a, **kw: None)
        monkeypatch.setattr(runtime, '_execute_wave', lambda *a, **kw: pytest.fail('cancel was swallowed'))
        assert runtime.run(goal='test')['status'] == 'interrupted'
        assert event.is_set()


def test_acceptance_criteria_supply_missing_receipt_premises(tmp_path):
    with Orchestrator(tmp_path, run_id='premise') as runtime:
        node = runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'tester',
                                           'acceptance_criteria': ['file_exists:deliverables/check.txt']})
        paths, _ = runtime._verification_premise_baseline(node)
        assert any(path.endswith('check.txt') for path in paths)


def _planner(tmp_path, monkeypatch):
    from AstralNexus.core.governor import TaskGraphPatch
    runtime = Orchestrator(tmp_path, run_id='planning')
    root = runtime.initialize_task_graph('plan', [])
    runtime.governor.llm_matching_enabled = True
    runtime.governor.llm_client = object()
    patch = TaskGraphPatch(add_nodes=[{'node_id': 'new', 'goal': 'test'}])
    monkeypatch.setattr(runtime.governor, 'plan_task_graph_patch', lambda *a, **kw: patch)
    monkeypatch.setattr(runtime.governor, 'replan_rejected_task_graph_patch', lambda *a, **kw: patch)
    return runtime, root


def test_distinct_planning_errors_have_absolute_and_persistent_run_limits(tmp_path, monkeypatch):
    from AstralNexus.core.exceptions import RealLLMRequiredError
    monkeypatch.setenv('ASTRALNEXUS_PLAN_MAX_ATTEMPTS', '4')
    monkeypatch.setenv('ASTRALNEXUS_RUN_PLAN_ATTEMPTS', '6')
    runtime, root = _planner(tmp_path, monkeypatch)
    with runtime:
        calls = []
        def invalid(*a, **kw):
            calls.append(1)
            raise ValueError(f'unknown dependency: different-{len(calls)}')
        monkeypatch.setattr(runtime.governor, 'apply_task_graph_patch', invalid)
        with pytest.raises(RealLLMRequiredError):
            runtime.plan_task_decomposition(root)
        assert len(calls) == 4
        assert runtime.runtime_state_manager.state.planning_attempts_total == 4
        runtime.task_graph.get_node(root).status = 'ready'
        with pytest.raises(RealLLMRequiredError):
            runtime.plan_task_decomposition(root)
        assert len(calls) == 6
        assert runtime.runtime_state_manager.state.recovery_churn_total == 6
    from AstralNexus.core.runtime_state import RuntimeStateManager
    assert RuntimeStateManager(tmp_path, 'planning').state.planning_attempts_total == 6


def test_planning_transport_failure_does_not_relabel_stale_patch(tmp_path, monkeypatch):
    from AstralNexus.core.exceptions import RealLLMRequiredError
    monkeypatch.setenv('ASTRALNEXUS_PLAN_MAX_ATTEMPTS', '3')
    runtime, root = _planner(tmp_path, monkeypatch)
    with runtime:
        inputs = []
        monkeypatch.setattr(runtime.governor, 'apply_task_graph_patch', lambda *a, **kw: (_ for _ in ()).throw(ValueError('invalid first patch')))
        def unavailable(*a, **kw):
            inputs.append(kw['rejected_patch'])
            raise TimeoutError('new proposal was never produced')
        monkeypatch.setattr(runtime.governor, 'replan_rejected_task_graph_patch', unavailable)
        with pytest.raises(RealLLMRequiredError):
            runtime.plan_task_decomposition(root)
        assert inputs[0] is not None
        assert inputs[1] is None


def test_cost_events_accumulate_without_rewriting_cache_until_checkpoint(tmp_path, monkeypatch):
    with Orchestrator(tmp_path, run_id='cost') as runtime:
        calls = []
        monkeypatch.setattr(runtime.runtime_state_manager, 'persist', lambda: calls.append(1))
        for i in range(10):
            runtime._record_lineage_event_cost('N', 'llm.request', {'request_id': str(i)})
        assert calls == []
        assert runtime.runtime_state_manager.lineage_entry('N')['requests'] == 10
        assert runtime._persist_graph_checkpoint(stage='flush')
        assert calls == [1]


def test_actor_wave_keeps_graph_commits_on_owner_and_after_peer_reads(tmp_path, monkeypatch):
    """Exercise the real response staging path with deterministic offline agents."""
    from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
    from AstralNexus.core.orchestration.wave_executor import WaveExecutor
    owner = threading.get_ident()
    barrier = threading.Barrier(2)
    finished = set()
    model_threads = set()
    commit_threads = []
    with Orchestrator(tmp_path, run_id='actor', max_parallel=2) as runtime:
        for ident in ['A', 'B']:
            runtime.task_graph.add_node({'node_id': ident, 'goal': 'write evidence', 'role': 'researcher', 'status': 'running'})
        class Agent:
            role = "researcher"
            def execute(self, context):
                model_threads.add(threading.get_ident())
                barrier.wait(timeout=5)
                # Peer evidence is not committed while either worker consumes inputs.
                assert not runtime.task_graph.artifacts
                finished.add(context.node_id)
                return AgentResponse(outcome=ExecutionOutcome.SUCCEEDED, content='Evidence ready', artifacts=['deliverables/' + context.node_id + '.txt'])
        add_artifact = runtime.task_graph.add_artifact
        def record_commit(value):
            assert finished == {'A', 'B'}
            commit_threads.append(threading.get_ident())
            return add_artifact(value)
        monkeypatch.setattr(runtime.task_graph, 'add_artifact', record_commit)
        async def execute(node_id):
            return await runtime._run_agent_task_async(Agent(), runtime.task_graph.get_node(node_id))
        results = []
        WaveExecutor().run(['A', 'B'], parallel=2, execute=execute,
                           set_barrier=lambda value: setattr(runtime, '_wave_commit_barrier', value),
                           on_result=results.append, cancelled=lambda: False)
        assert all(result.error is None for result in results), [repr(result.error) for result in results]
        assert model_threads and owner not in model_threads
        assert commit_threads == [owner, owner]
        assert len(results) == 2 and all(result.error is None for result in results)
        assert len(runtime.task_graph.artifacts) == 2


def test_missing_fence_token_prevents_worker_model_dispatch(tmp_path):
    with Orchestrator(tmp_path, run_id='no-dispatch', fence_token_provider=lambda: None) as runtime:
        node = runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'researcher', 'status': 'running'})
        class Agent:
            role = 'researcher'
            def execute(self, context):
                pytest.fail('model dispatched after the configured fence failed')
        assert runtime._run_agent_task(Agent(), node) is None
        assert runtime._ownership_lost


def test_worker_inputs_cannot_mutate_the_graph_contract(tmp_path):
    from AstralNexus.agents.base import AgentResponse
    with Orchestrator(tmp_path, run_id='detached-input') as runtime:
        node = runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'researcher', 'status': 'running', 'constraints': ['frozen']})
        class Agent:
            role = 'researcher'
            def execute(self, context):
                context.constraints.append('mutated by worker')
                context.metadata['owned_paths'].append('foreign.txt')
                return AgentResponse(content='Done')
        runtime._run_agent_task(Agent(), node)
        assert node.constraints == ['frozen']
        assert 'foreign.txt' not in node.owned_paths


def test_planning_attempt_reservation_is_in_the_authoritative_db(tmp_path, monkeypatch):
    from AstralNexus.core.exceptions import RealLLMRequiredError
    from AstralNexus.core.governor import TaskGraphPatch
    monkeypatch.setenv('ASTRALNEXUS_PLAN_MAX_ATTEMPTS', '2')
    runtime, _, _ = _fenced_runtime(tmp_path)
    with runtime:
        root = runtime.initialize_task_graph('plan', [])
        runtime.governor.llm_matching_enabled = True
        runtime.governor.llm_client = object()
        proposal = TaskGraphPatch(add_nodes=[{'node_id': 'new', 'goal': 'test'}])
        monkeypatch.setattr(runtime.governor, 'plan_task_graph_patch', lambda *a, **kw: proposal)
        monkeypatch.setattr(runtime.governor, 'replan_rejected_task_graph_patch', lambda *a, **kw: proposal)
        monkeypatch.setattr(runtime.governor, 'apply_task_graph_patch', lambda *a, **kw: (_ for _ in ()).throw(ValueError('invalid proposal')))
        with pytest.raises(RealLLMRequiredError):
            runtime.plan_task_decomposition(root)
        runtime.runtime_state_manager.path.unlink()
        fence = runtime.graph_store.latest_commit_fence('joint')
        assert fence['cursor']['planning_attempts_total'] == 2
        assert fence['cursor']['recovery_churn_total'] == 2
