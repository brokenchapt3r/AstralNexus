"""Branching lineage and explicit async/deadline embedding regressions."""
import asyncio
import json
import threading
import time
import warnings

import pytest
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.orchestration.wave_executor import WaveExecutor, WaveTimeoutError
from AstralNexus.core.orchestration.worker_dispatcher import WorkerDispatcher


def add(runtime, ident, status, *, parent='', deps=(), outputs=(), inputs=()):
    return runtime.task_graph.add_node({'node_id': ident, 'goal': ident, 'role': 'coder', 'status': status,
        'dependencies': list(deps), 'output_artifacts': list(outputs), 'input_artifacts': list(inputs),
        'metadata': {'recovery_of': parent} if parent else {}})


@pytest.mark.parametrize('dead_first', [True, False])
def test_unique_living_fork_is_selected_independent_of_insertion_order(tmp_path, dead_first):
    with Orchestrator(tmp_path) as runtime:
        add(runtime, 'ROOT', 'failed')
        branches = [('DEAD', 'cancelled'), ('LIVE', 'ready')]
        for ident, status in branches if dead_first else list(reversed(branches)):
            add(runtime, ident, status, parent='ROOT')
        consumer = add(runtime, 'C', 'blocked', deps=['ROOT'])
        assert runtime._rewire_superseded_dependents() == 1
        assert consumer.dependencies == ['LIVE']
        assert runtime._rewire_superseded_dependents() == 0


def test_multiple_living_forks_are_not_arbitrarily_selected(tmp_path):
    with Orchestrator(tmp_path) as runtime:
        add(runtime, 'ROOT', 'failed')
        add(runtime, 'A', 'ready', parent='ROOT')
        add(runtime, 'B', 'ready', parent='ROOT')
        consumer = add(runtime, 'C', 'blocked', deps=['ROOT'])
        assert runtime._rewire_superseded_dependents() == 0
        assert consumer.dependencies == ['ROOT']
        event = next(e for e in runtime.trace_recorder.events if e['payload'].get('reason') == 'ambiguous_living_successors')
        assert event['payload']['candidates'] == ['A', 'B']


def test_branch_contract_can_uniquely_select_the_required_producer(tmp_path):
    with Orchestrator(tmp_path) as runtime:
        add(runtime, 'ROOT', 'failed', outputs=['artifact:code'])
        add(runtime, 'A', 'ready', parent='ROOT', outputs=['artifact:code'])
        add(runtime, 'B', 'ready', parent='ROOT', outputs=['artifact:report'])
        consumer = add(runtime, 'C', 'blocked', deps=['ROOT'], inputs=['artifact:code'])
        assert runtime._rewire_superseded_dependents() == 1
        assert consumer.dependencies == ['A']


def test_dead_cycle_does_not_hide_a_living_fork(tmp_path):
    with Orchestrator(tmp_path) as runtime:
        add(runtime, 'ROOT', 'failed', parent='LOOP')
        add(runtime, 'LOOP', 'cancelled', parent='ROOT')
        add(runtime, 'LIVE', 'ready', parent='ROOT')
        consumer = add(runtime, 'C', 'blocked', deps=['ROOT'])
        assert runtime._rewire_superseded_dependents() == 1
        assert consumer.dependencies == ['LIVE']


def test_native_async_entry_works_inside_an_existing_event_loop(tmp_path, monkeypatch):
    with Orchestrator(tmp_path) as runtime:
        async def fake(node_id):
            return node_id == 'N'
        monkeypatch.setattr(runtime, '_execute_task_async', fake)
        # Public API is the actual coroutine method, independently test the
        # internal async worker API and public whole-run embedding.
        monkeypatch.setattr(runtime, 'execute_task_async', fake)
        monkeypatch.setattr(runtime, 'run', lambda **kw: {'status': 'completed'})
        async def check():
            assert await runtime.execute_task_async('N')
            assert (await runtime.run_async(goal='test'))['status'] == 'completed'
            results = []
            await WaveExecutor().run_async(['N'], parallel=1, execute=fake,
                set_barrier=lambda _: None, on_result=results.append, cancelled=lambda: False)
            assert results[0].succeeded
        asyncio.run(check())


def test_sync_entry_in_async_host_fails_before_constructing_a_coroutine(tmp_path):
    with Orchestrator(tmp_path) as runtime:
        async def check():
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter('always')
                with pytest.raises(RuntimeError, match='execute_task_async'):
                    runtime.execute_task('N')
                assert not seen
        asyncio.run(check())


def test_legacy_visibility_hook_and_instance_injection_both_work(tmp_path, monkeypatch):
    from AstralNexus.core import orchestrator as facade
    with Orchestrator(tmp_path) as runtime:
        path = tmp_path / 'deliverables/a.txt'; path.parent.mkdir(exist_ok=True); path.write_text('a')
        monkeypatch.setattr(facade, '_delivery_evidence_visible', lambda _: False)
        assert runtime._delivery_progress_snapshot()[1] == []
        runtime.delivery_evidence_visible = lambda _: True
        assert 'deliverables/a.txt' in runtime._delivery_progress_snapshot()[1]
        assert facade.__all__ == ['Orchestrator', 'ExecutionRunResult']


def test_wave_deadline_cancels_actor_commits_without_joining_hung_thread(monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_WAVE_TIMEOUT', '.05')
    release = threading.Event()
    barriers = []
    results = []
    returned = []
    async def execute(ident):
        if ident == 'slow':
            await WorkerDispatcher().call(lambda: release.wait(5))
        else:
            returned.append(ident)
        await barriers[-1].wait(ident)
        results.append('unsafe commit')
        return True
    start = time.monotonic()
    try:
        with pytest.raises(WaveTimeoutError) as captured:
            WaveExecutor().run(['fast', 'slow'], parallel=2, execute=execute,
                set_barrier=lambda value: barriers.append(value) if value is not None else None,
                on_result=results.append, cancelled=lambda: False)
        assert time.monotonic() - start < 1
        assert returned == ['fast'] and results == []
        assert captured.value.pending == ['slow'] and captured.value.waiting == ['fast']
    finally:
        release.set()


def test_model_return_staging_is_durable_but_not_acceptance(tmp_path):
    from AstralNexus.runtime.execution_store import ExecutionStore
    store = ExecutionStore(tmp_path)
    ident = store.begin('N', {'owned_paths': []})
    store.stage_response(ident, {'outcome': 'succeeded', 'content': 'raw result'})
    record = store.get(ident)
    assert record['status'] == 'running'
    assert record['outcome']['staged_response']['content'] == 'raw result'
    assert not record['acceptance']


def test_planning_attempt_audit_equals_actual_model_calls(tmp_path, monkeypatch):
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.core.exceptions import RealLLMRequiredError
    monkeypatch.setenv('ASTRALNEXUS_PLAN_MAX_ATTEMPTS', '5')
    with Orchestrator(tmp_path) as runtime:
        root = runtime.initialize_task_graph('plan', [])
        runtime.governor.llm_matching_enabled = True
        runtime.governor.llm_client = object()
        calls = []
        def proposal(*a, **kw):
            calls.append(1)
            return TaskGraphPatch(add_nodes=[{'node_id': 'N', 'goal': 'write code', 'required_capabilities': ['code_generation']}])
        monkeypatch.setattr(runtime.governor, 'plan_task_graph_patch', proposal)
        monkeypatch.setattr(runtime.governor, 'replan_rejected_task_graph_patch', proposal)
        def reject(*a, **kw):
            raise ValueError('unknown dependency: changed-' + str(len(calls)))
        monkeypatch.setattr(runtime.governor, 'apply_task_graph_patch', reject)
        with pytest.raises(RealLLMRequiredError):
            runtime.plan_task_decomposition(root)
        event = next(e for e in runtime.trace_recorder.events if e['event_type'] == 'orchestrator.planning_failed')
        assert len(calls) == event['payload']['attempts'] == 5


def test_public_async_worker_uses_native_coroutine_and_propagates_cancellation(tmp_path, monkeypatch):
    with Orchestrator(tmp_path) as runtime:
        async def worker(ident):
            await asyncio.Event().wait()
        monkeypatch.setattr(runtime, '_execute_task_async', worker)
        async def check():
            task = asyncio.create_task(runtime.execute_task_async('N'))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime._cancel_requested()
        asyncio.run(check())
