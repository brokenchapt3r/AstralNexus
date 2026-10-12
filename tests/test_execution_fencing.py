from datetime import datetime, timedelta, timezone
import asyncio
import pytest
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.execution_fence import ExecutionOwnershipLost, execution_fence_scope
from AstralNexus.runtime.run_coordinator import RunCoordinator
from AstralNexus.tools.executor import ToolExecutor


def owner(tmp_path):
    now = [datetime(2026, 10, 9, tzinfo=timezone.utc)]
    coordinator = RunCoordinator(tmp_path, run_id='run', now_fn=lambda: now[0])
    lease = coordinator.acquire('A')
    return now, coordinator, lease


def test_takeover_rejects_stale_receipt_stage_finish_and_accept(tmp_path):
    now, coordinator, lease = owner(tmp_path)
    old = ExecutionStore(tmp_path, fence_token_provider=lambda: coordinator.fence_token(lease))
    execution = old.begin('node', {'owned_paths': []})
    first = old.append(execution, {'status': 'success', 'tool': 'probe'})
    assert first['ownership']['generation'] == 1
    now[0] += timedelta(seconds=1000)
    newer = coordinator.acquire('B')
    assert newer is not None and newer.generation == 2
    for invoke in [lambda: old.append(execution, {'status': 'success'}),
        lambda: old.stage_response(execution, {'content': 'late'}),
        lambda: old.finish(execution, 'succeeded', {}), lambda: old.accept(execution, {'approved': False})]:
        with pytest.raises(ExecutionOwnershipLost): invoke()
    state = old.get(execution)
    assert state['status'] == 'running' and state['outcome'] is None and state['acceptance'] is None
    assert state['receipts'] == [first]
    fresh = ExecutionStore(tmp_path, fence_token_provider=lambda: coordinator.fence_token(newer))
    with pytest.raises(ExecutionOwnershipLost, match='different owner'):
        fresh.append(execution, {'status': 'success'})
    fresh.interrupt_open(execution)
    assert fresh.get(execution)['status'] == 'interrupted'
    assert fresh.get(execution)['receipts'] == [first]


def test_missing_or_faulty_provider_never_becomes_unfenced_write(tmp_path):
    owner(tmp_path)
    with pytest.raises(ExecutionOwnershipLost, match='no fence provider'):
        ExecutionStore(tmp_path).begin('node', {})
    def fail(): raise OSError('ownership store unavailable')
    with pytest.raises(ExecutionOwnershipLost, match='unavailable'):
        ExecutionStore(tmp_path, fence_token_provider=fail).begin('node', {})


def test_current_owner_can_reverify_finished_evidence_without_rewriting_its_origin(tmp_path):
    now, coordinator, lease = owner(tmp_path)
    first = ExecutionStore(tmp_path, fence_token_provider=lambda: coordinator.fence_token(lease))
    execution = first.begin('node', {'owned_paths': []})
    receipt = first.append(execution, {'status': 'success', 'tool': 'probe'})
    first.finish(execution, 'succeeded', {})
    now[0] += timedelta(seconds=1000)
    newer = coordinator.acquire('B')
    second = ExecutionStore(tmp_path, fence_token_provider=lambda: coordinator.fence_token(newer))
    second.accept(execution, {'approved': True})
    result = second.get(execution)
    assert result['ownership']['generation'] == 1
    assert result['acceptance']['verification_owner']['generation'] == 2
    assert result['receipts'] == [receipt]


def test_tool_refuses_cancelled_execution_even_with_live_run_lease(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_ALLOW_FILE_WRITE', 'true')
    _, coordinator, lease = owner(tmp_path)
    provider = lambda: coordinator.fence_token(lease)
    store = ExecutionStore(tmp_path, fence_token_provider=provider)
    execution = store.begin('node', {'owned_paths': ['app.py']})
    with execution_fence_scope(tmp_path, provider, execution):
        executor = ToolExecutor(workspace_root=tmp_path)
    store.finish(execution, 'interrupted', {'error': 'wave cancelled'})
    with pytest.raises(ExecutionOwnershipLost, match='no longer active'):
        executor.execute_tool('file_write', {'path': 'app.py', 'content': 'late'})
    assert not (tmp_path / 'app.py').exists()


def test_async_worker_inherits_fence_and_model_context(tmp_path):
    from AstralNexus.core.orchestration.worker_dispatcher import WorkerDispatcher
    from AstralNexus.runtime.run_context import RunContext, RunSettings, get_env
    from AstralNexus.runtime.execution_fence import current_execution_identity
    _, coordinator, lease = owner(tmp_path)
    context = RunContext(tmp_path, RunSettings.capture(environment={'ASTRALNEXUS_MODEL': 'frozen-model'}))
    with context.scope(), execution_fence_scope(tmp_path, lambda: coordinator.fence_token(lease), 'execution'):
        result = asyncio.run(WorkerDispatcher().call(lambda: (get_env('ASTRALNEXUS_MODEL'), current_execution_identity(tmp_path))))
    assert result == ('frozen-model', 'execution')


def test_cancelled_real_worker_cannot_dispatch_late_file_write(tmp_path, monkeypatch):
    import threading
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.agents.base import AgentResponse, ExecutionOutcome
    monkeypatch.setenv('ASTRALNEXUS_ALLOW_FILE_WRITE', 'true')
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    failures = []
    class Agent:
        role = 'researcher'
        def execute(self, context):
            executor = ToolExecutor(workspace_root=tmp_path)
            entered.set()
            try:
                assert release.wait(5)
                executor.execute_tool('file_write', {'path': 'late.txt', 'content': 'late'})
            except ExecutionOwnershipLost as error:
                failures.append(str(error))
            finally:
                exited.set()
            return AgentResponse(outcome=ExecutionOutcome.FAILED, content='cancelled')
    with Orchestrator(tmp_path, run_id='cancelled') as runtime:
        node = runtime.task_graph.add_node({'node_id': 'N', 'goal': 'test', 'role': 'researcher', 'status': 'running'})
        async def scenario():
            task = asyncio.create_task(runtime._run_agent_task_async(Agent(), node))
            for _ in range(300):
                if entered.is_set(): break
                await asyncio.sleep(.01)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        try:
            asyncio.run(scenario())
            execution = node.metadata['runtime_execution_id']
            assert ExecutionStore(tmp_path).get(execution)['status'] == 'interrupted'
        finally:
            release.set()
            assert exited.wait(3)
        assert failures and not (tmp_path / 'late.txt').exists()


def test_frozen_receipt_sets_reject_invented_ids_and_rewrites(tmp_path):
    store = ExecutionStore(tmp_path)
    execution = store.begin('node', {})
    first = store.append(execution, {'status': 'success'})
    second = store.append(execution, {'status': 'success'})
    with pytest.raises(ValueError, match='unknown receipt'):
        store.freeze_request_receipts('invented', execution, ['fake'])
    store.freeze_request_receipts('request', execution, [first['receipt_id']])
    store.freeze_request_receipts('request', execution, [first['receipt_id']])
    with pytest.raises(ValueError, match='cannot be rewritten'):
        store.freeze_request_receipts('request', execution, [second['receipt_id']])
    assert store.request_receipts('request') == [first['receipt_id']]
