import json
import sqlite3
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from AstralNexus.dashboard.workbench_server import WorkbenchHost
from AstralNexus.tests.publish_support import temporary_host
from AstralNexus.runtime.workbench import WorkbenchStore, WorkflowEngine, validate_workflow
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.run_coordinator import RunCoordinator


def pipeline(code='print("actual command")'):
    return {'name': 'test', 'steps': [{'id': 'first', 'command': [sys.executable, '-c', code], 'timeout': 10}]}


def job(store, ident):
    with store.db() as db:
        row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone())
    row['result'] = json.loads(row['result']) if row['result'] else None
    return row


def test_host_reconnect_preserves_sessions_and_embeds_existing_monitor(tmp_path, temporary_host):
    home = tmp_path / 'host'
    with temporary_host(home) as host:
        client = host.app.test_client()
        response = client.post('/api/host/sessions', json={'title': 'persist', 'workspace': str(tmp_path)})
        assert response.status_code == 201
        ident = response.json['id']
        assert b'/monitor' in client.get('/').data
        assert client.get('/monitor').status_code == 200
        assert client.get('/api/snapshot').status_code == 200
        assert client.get('/api/host').json['selected'] == ident
        with pytest.raises(RuntimeError, match='already owns'):
            WorkbenchHost(home)
    with temporary_host(home) as reopened:
        assert reopened.app.test_client().get('/api/host').json['selected'] == ident
        assert reopened.store.session(ident)['title'] == 'persist'


def test_workflow_failure_has_real_receipt_and_stops_downstream(tmp_path):
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('test', str(tmp_path))
    spec = pipeline('raise SystemExit(7)')
    spec['steps'].append({'id': 'must-not-run', 'depends_on': ['first'],
                          'command': [sys.executable, '-c', "open('wrong.txt','w').write('bad')"]})
    ident = store.submit(session, spec)
    assert WorkflowEngine(store).run_once()
    result = job(store, ident)
    assert result['status'] == 'failed'
    assert not (tmp_path / 'wrong.txt').exists()
    assert result['result']['steps'][0]['returncode'] == 7
    ledger = ExecutionStore(store.home / 'jobs' / ident).get(result['result']['execution_id'])
    assert ledger['status'] == 'failed' and ledger['acceptance'] is None
    assert ledger['receipts'][0]['returncode'] == 7


def test_invalid_graph_never_admitted_and_spawn_failure_not_running_forever(tmp_path):
    with pytest.raises(ValueError, match='acyclic'):
        validate_workflow({'steps': [{'id': 'a', 'command': ['x'], 'depends_on': ['a']}]})
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('test', str(tmp_path))
    ident = store.submit(session, {'steps': [{'id': 'bad', 'command': ['astralnexus-no-such-program-xyz']}]})
    WorkflowEngine(store).run_once()
    result = job(store, ident)
    assert result['status'] == 'failed'
    assert ExecutionStore(store.home / 'jobs' / ident).get(result['result']['execution_id'])['status'] == 'failed'


def test_schedule_transaction_coalesces_missed_ticks_and_survives_restart(tmp_path):
    home = tmp_path / 'host'
    store = WorkbenchStore(home)
    session = store.register_session('test', str(tmp_path))
    ident = store.schedule(session, pipeline(), due=100, interval=60)
    with ThreadPoolExecutor(2) as pool:
        counts = list(pool.map(lambda _: WorkbenchStore(home).tick(1000), range(2)))
    assert sum(counts) == 1
    fresh = WorkbenchStore(home)
    assert len(fresh.snapshot()['jobs']) == 1
    assert fresh.snapshot()['schedules'][0]['due'] == 1060
    fresh.enable_schedule(ident, False)
    assert fresh.tick(2000) == 0


def test_cancel_running_process_and_unknown_outcome_never_auto_replays(tmp_path):
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('test', str(tmp_path))
    ident = store.submit(session, pipeline('import time;time.sleep(30)'))
    engine = WorkflowEngine(store)
    thread = threading.Thread(target=engine.run_once)
    thread.start()
    deadline = time.monotonic() + 5
    while job(store, ident)['status'] != 'running' and time.monotonic() < deadline:
        time.sleep(.01)
    store.cancel(ident)
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert job(store, ident)['status'] == 'cancelled'
    # Simulate host loss after dequeue without inventing a successful receipt.
    second = store.submit(session, pipeline())
    with store.db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (second,))
    restarted = WorkflowEngine(WorkbenchStore(store.home))
    restarted.recover_interrupted()
    assert job(store, second)['status'] == 'interrupted'
    assert not restarted.run_once()


def test_serve_survives_a_transient_lock_during_startup_recovery(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('test', str(tmp_path))
    ident = store.submit(session, pipeline())
    with store.db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (ident,))
    engine = WorkflowEngine(store)
    real_db, attempts = store.db, []

    def flaky_db():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError('database is locked')
        return real_db()

    def first_pass_then_stop():
        engine.stop.set()
        return False

    monkeypatch.setattr(store, 'db', flaky_db)
    monkeypatch.setattr(engine, 'run_once', first_pass_then_stop)
    engine.serve()
    # The host must still be alive after the transient lock: recovery ran, not the whole engine.
    assert len(attempts) >= 2
    assert job(store, ident)['status'] == 'interrupted'


def test_pause_uses_coordinator_and_workflows_do_not_resume_agent(tmp_path, temporary_host):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'trace.jsonl').touch()
    coordinator = RunCoordinator(run, run_id='real')
    lease = coordinator.acquire('owner')
    assert lease
    with temporary_host(tmp_path / 'host') as host:
        ident = host.store.register_session('test', str(tmp_path), str(run))
        host.store.select(ident)
        response = host.app.test_client().post('/api/host/pause/' + ident)
        assert response.status_code == 200
        assert coordinator.should_stop(lease)
        scheduled = host.store.schedule(ident, pipeline(), time.time() - 1)
        assert scheduled and host.store.tick() == 1
        host.engine.run_once()
        assert coordinator.current()['paused_by_user']

def test_registration_publishes_pointer_under_concurrency_and_a_live_reader(tmp_path, temporary_host):
    """R27: /api/host/sessions used to race on one shared pointer temp file.

    Two registrations shared '<home>/selected_run.tmp', so the published content could
    tear and os.replace failed with WinError 5 while the target was open; the
    PermissionError handler then answered a misleading 503 "unavailable".  Publishing
    must retry, stay torn-free and leave no temp file behind.
    """
    home = tmp_path / 'host'
    with temporary_host(home) as host:
        first = host.app.test_client().post(
            '/api/host/sessions', json={'title': 'first', 'workspace': str(tmp_path)})
        assert first.status_code == 201
        target = home / 'selected_run.json'
        assert target.exists()

        def reader_holds_target():
            with open(target, 'rb') as handle:
                time.sleep(0.5)

        def register(index):
            return host.app.test_client().post(
                '/api/host/sessions', json={'title': 'c%d' % index, 'workspace': str(tmp_path)})

        holder = threading.Thread(target=reader_holds_target)
        holder.start()
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(register, range(8)))
        finally:
            holder.join()
        assert [r.status_code for r in responses] == [201] * 8
        assert json.loads(target.read_text(encoding='utf-8')) == {'run_dir': str(host.home / 'waiting')}
        assert not list(home.glob('*.tmp'))


def test_workflow_receipt_has_owner_and_live_recovery_is_refused(tmp_path):
    from AstralNexus.runtime.job_ownership import JobOwnership
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('test', str(tmp_path))
    ident = store.submit(session, pipeline())
    assert WorkflowEngine(store).run_once()
    result = job(store, ident)
    execution = ExecutionStore(store.home / 'jobs' / ident).get(result['result']['execution_id'])
    assert execution['receipts'][0]['ownership']['generation'] == 1
    assert execution['receipts'][0]['ownership']['owner_id'].startswith('workflow-')
    live = store.submit(session, pipeline())
    with store.db() as db: db.execute("UPDATE jobs SET status='running' WHERE id=?", (live,))
    with JobOwnership(store.home / 'jobs' / live):
        WorkflowEngine(store).recover_interrupted()
        assert job(store, live)['status'] == 'running'


def test_lost_workflow_owner_cannot_update_job_state(tmp_path):
    from AstralNexus.runtime.job_ownership import JobOwnership
    from AstralNexus.runtime.execution_fence import ExecutionOwnershipLost
    store = WorkbenchStore(tmp_path / 'host')
    session = store.register_session('test', str(tmp_path))
    ident = store.submit(session, pipeline())
    with JobOwnership(store.home / 'jobs' / ident) as owner:
        owner.finish('completed', 'workflow_succeeded')
        with pytest.raises(ExecutionOwnershipLost):
            with owner.job_db(store) as db:
                db.execute("UPDATE jobs SET status='failed' WHERE id=?", (ident,))
    assert job(store, ident)['status'] == 'queued'
