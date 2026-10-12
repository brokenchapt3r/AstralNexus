"""Zero-API historical replay plus an explicitly synthetic local repair continuation."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder
from AstralNexus.core.orchestrator import Orchestrator
from AstralNexus.core.task_graph import TaskNode
from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
from AstralNexus.runtime.recovery_handoff import build_recovery_handoff
from AstralNexus.runtime.execution_store import ExecutionStore

FIXTURE = Path(__file__).parent / 'fixtures/r17_offline_recovery.json'
UI = 'RF-UI-PAGE-EVIDENCE-ONESHOT'


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Offline replay attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    # Fail even before an HTTP transport can retry or hide a socket exception.
    from AstralNexus.runtime.llm_client import LLMClient
    monkeypatch.setattr(LLMClient, 'chat', denied)


@pytest.fixture
def history():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@pytest.mark.parametrize('index,holds,reason', [
    (0, False, 'TEST_EXECUTION_FAILED'),
    (1, None, 'TEST_SUMMARY_NOT_RECOGNIZED'),
    (2, True, ''),
])
def test_recorded_execution_replays_without_rewriting_evidence(history, index, holds, reason):
    entry = history['executions'][index]
    original = deepcopy(entry)
    receipts = [row['receipt'] for row in entry['receipts']]
    command = json.loads(receipts[-1]['arguments'])['command']
    observation = run_acceptance_checks(['tests_passed:' + ' '.join(command)], receipts=receipts)[0]
    assert observation['holds'] is holds
    assert observation['reason'] == reason
    assert observation['receipt_ids'] == [receipts[-1]['receipt_id']]
    assert entry == original
    assert entry['worker_content']  # Stored worker text is a claim, not a verdict.


def test_historical_rejection_to_governed_repair_and_local_recheck(history, tmp_path):
    """Real historical handoff, real Governor admission; scripted future decision.

    The tiny local script below is a synthetic assertion fixture, NOT a repaired
    copy of the sample application and NOT evidence of live UI acceptance.
    """
    metadata = deepcopy(history['nodes'][UI]['metadata'])
    original = deepcopy(metadata)
    criterion = 'tests_passed:python probe.py'
    planned = {'update_nodes': [{'node_id': UI, 'status': 'ready',
        'goal': 'Repair the missing assertion summary in probe.py and re-execute the assertions.'}],
        'reason_codes': ['OFFLINE_SYNTHETIC_CONTINUATION']}

    class ScriptedPlanner:
        def __init__(self):
            self.calls = []

        def chat(self, messages):
            assert not self.calls, 'Unexpected extra model turn'
            self.calls.append(deepcopy(messages))
            return json.dumps(planned)

    with Orchestrator(tmp_path, run_id='offline-r17') as orch:
        node = TaskNode(node_id=UI, goal='verify local assertions', role='coder',
                        status='repair_pending', acceptance_criteria=[criterion],
                        owned_paths=['probe.py'], metadata=metadata)
        orch.task_graph.add_node(node)
        handoff = build_recovery_handoff(orch.task_graph, node)
        assert handoff['failure'] == ''
        assert 'MissingDeclaredArtifacts' in handoff['historical_execution_failure'] or 'materialize' in handoff['historical_execution_failure']
        assert 'TEST_SUMMARY_NOT_RECOGNIZED' in json.dumps(handoff['failed_dimensions'])
        assert handoff['runtime_accounting']['tool_calls'] == 25
        report = orch._worker_report_for_node(node)
        assert 'last_failure' not in report
        client = ScriptedPlanner()
        orch.governor.llm_client = client
        orch.governor.llm_matching_enabled = True
        patch = orch.governor.plan_verification_recovery(node_id=UI, verdict='inconclusive',
            overall_score=0, dimensions=handoff['failed_dimensions'], worker_report=report)
        payload = json.loads(client.calls[0][-1]['content'])
        assert 'TEST_SUMMARY_NOT_RECOGNIZED' in json.dumps(payload)
        assert 'historical_execution_failure' in json.dumps(payload)
        orch.governor.apply_task_graph_patch(patch)
        repaired = orch.task_graph.get_node(UI)
        assert repaired.status == 'ready'
        assert repaired.acceptance_criteria == [criterion]
        prompt = Coder()._build_runtime_prompt(AgentContext(node_id=UI, goal=repaired.goal,
            metadata={'run_dir': str(tmp_path), 'acceptance_criteria': [criterion],
                      'recovery_handoff': handoff}), 'implementation')
        assert '测试执行证据契约（执行前）' in prompt
        assert 'TEST_SUMMARY_NOT_RECOGNIZED' in prompt

        # Execute only this hardcoded local microfixture, never historical commands.
        script = tmp_path / 'probe.py'
        script.write_text('import json\nassert 2 + 2 == 4\nprint(json.dumps({"passed": 1, "failed": 0}))\n', encoding='utf-8')
        # R16 sandbox-compatible capture: anonymous pipes are refused here
        # (CreatePipe WinError 5, P0-4 boundary layer 1), so the synthetic
        # recheck collects output through file redirection. The receipt
        # semantics and every assertion below are unchanged.
        stdout_path = tmp_path / 'probe_stdout.txt'
        stderr_path = tmp_path / 'probe_stderr.txt'
        with open(stdout_path, 'wb') as out_f, open(stderr_path, 'wb') as err_f:
            completed = subprocess.run([sys.executable, '-I', str(script)],
                                       stdin=subprocess.DEVNULL, stdout=out_f,
                                       stderr=err_f, timeout=10)
        stdout_text = stdout_path.read_text(encoding='utf-8', errors='replace')
        stderr_text = stderr_path.read_text(encoding='utf-8', errors='replace')
        receipt = {'tool': 'terminal_exec', 'arguments': {'command': ['python', 'probe.py']},
                   'status': 'success' if completed.returncode == 0 else 'failed',
                   'returncode': completed.returncode, 'stdout_tail': stdout_text,
                   'stderr_tail': stderr_text, 'receipt_id': 'synthetic-local-recheck', 'files': {}}
        assert completed.returncode == 0, stderr_text
        assert run_acceptance_checks([criterion], receipts=[receipt])[0]['holds'] is True
        assert run_acceptance_checks([criterion], receipts=[])[0]['holds'] is not True
        store = ExecutionStore(tmp_path / 'synthetic-ledger')
        execution = store.begin('synthetic-local-probe', {'owned_paths': [], 'acceptance_criteria': [criterion]})
        store.append(execution, receipt)
        store.finish(execution, 'succeeded', {'owned_files': {}})
        store.accept(execution, {'approved': True})
        assert store.get(execution)['acceptance']['approved'] is True
        assert repaired.status != 'completed'  # No semantic/final delivery approval simulated.
    assert history['nodes'][UI]['metadata'] == original


def test_recorded_worker_success_claim_cannot_bypass_execution_store(history, tmp_path):
    entry = history['executions'][1]
    receipts = [r['receipt'] for r in entry['receipts']]
    criterion = 'tests_passed:' + ' '.join(json.loads(receipts[-1]['arguments'])['command'])
    store = ExecutionStore(tmp_path)
    execution = store.begin(UI, {'owned_paths': [], 'acceptance_criteria': [criterion]})
    for receipt in receipts:
        store.append(execution, deepcopy(receipt))
    store.finish(execution, 'succeeded', {'owned_files': {}, 'content': entry['worker_content']})
    with pytest.raises(ValueError, match='TEST_SUMMARY_NOT_RECOGNIZED'):
        store.accept(execution, {'approved': True})
    assert not store.get(execution).get('acceptance')
