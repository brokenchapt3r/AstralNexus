from copy import deepcopy
import json
import pytest

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder
from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
from AstralNexus.runtime.development_playbook import fullstack_playbook, recovery_procedures
from AstralNexus.runtime.recovery_handoff import build_recovery_handoff
from AstralNexus.runtime.trace import TraceRecorder


def observation(reason='TEST_SUMMARY_NOT_RECOGNIZED'):
    return dict(criterion_id='AC-01', criterion='tests_passed:python probe.py',
                execution_id='exec-original', checked=False, holds=None, reason=reason)


def test_fullstack_route_only_for_explicit_intent():
    assert fullstack_playbook('开发全栈工单平台')
    assert fullstack_playbook('Develop FULL-STACK application')
    assert not fullstack_playbook('Summarize quarterly report')
    assert '最小纵向功能' in fullstack_playbook('全栈')


def test_procedures_keep_identity_and_do_not_turn_unknown_into_repair():
    leaf = observation()
    before = deepcopy(leaf)
    results = recovery_procedures([leaf, leaf, observation('UNDECIDABLE'),
        {**leaf, 'checked': True, 'holds': True}, None])
    assert len(results) == 1
    assert results[0]['criterion'] == leaf['criterion']
    assert results[0]['source_execution_id'] == 'exec-original'
    assert results[0]['recheck_command'] == 'python probe.py'
    assert results[0]['route'] == 'repair_test_adapter'
    assert leaf == before


def test_business_failure_and_staleness_have_different_routes():
    assert recovery_procedures([observation('TESTS_REPORTED_FAILURES')])[0]['route'] == 'repair_implementation'
    assert recovery_procedures([observation('TEST_EXECUTION_STALE')])[0]['route'] == 'rerun_after_change'
    assert recovery_procedures(None) == []


def test_recorded_leaf_survives_handoff_into_actual_worker_prompt():
    graph = TaskGraph()
    old = TaskNode('old', 'verify', 'tester', metadata={
        '_verification_receipt': {'verdict': 'inconclusive', 'observations': [observation()]}})
    repair = TaskNode('repair', 'fix adapter', 'coder', metadata={'recovery_of': 'old'})
    graph.add_node(old)
    graph.add_node(repair)
    original = deepcopy(old.metadata)
    handoff = build_recovery_handoff(graph, repair)
    assert handoff['repair_procedures'][0]['criterion_id'] == 'AC-01'
    prompt = Coder()._build_runtime_prompt(AgentContext(node_id='repair', goal='fix adapter',
        metadata={'recovery_handoff': handoff}), 'implementation')
    assert 'repair_test_adapter' in prompt and 'python probe.py' in prompt
    assert old.metadata == original


def test_actual_planner_gets_route_and_exact_recheck(tmp_path):
    class Client:
        messages = None
        def chat(self, messages):
            self.messages = messages
            return json.dumps({'update_nodes': [{'node_id': 'old', 'status': 'ready'}]})
    graph = TaskGraph()
    graph.add_node(TaskNode('old', 'verify', 'tester', status='repair_pending', metadata={
        '_verification_receipt': {'verdict': 'inconclusive', 'observations': [observation()]}}))
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    client = Client()
    governor.llm_client = client
    governor.llm_matching_enabled = True
    governor.plan_verification_recovery(node_id='old', verdict='inconclusive', overall_score=0, dimensions=[])
    payload = json.loads(client.messages[-1]['content'])
    assert payload['failed_node_report']['repair_procedures'][0]['recheck_command'] == 'python probe.py'


def test_recipe_or_report_alone_never_satisfies_original_check():
    leaf = observation()
    recipe = recovery_procedures([leaf])[0]
    assert run_acceptance_checks([recipe['criterion']], receipts=[])[0]['holds'] is not True
    # An unrelated successful report command cannot close the probe obligation.
    receipt = dict(tool='terminal_exec', command='python report.py', status='success',
                   returncode=0, stdout_tail='1 passed', files={})
    assert run_acceptance_checks([recipe['criterion']], receipts=[receipt])[0]['holds'] is not True


@pytest.mark.parametrize('unrelated', [False, True])
def test_actual_admission_rejects_lost_recheck_atomically(tmp_path, unrelated):
    graph = TaskGraph()
    graph.add_node(TaskNode('old', 'verify', 'tester', status='repair_pending',
        acceptance_criteria=[observation()['criterion']], metadata={
        '_verification_receipt': {'verdict': 'inconclusive', 'observations': [observation()]}}))
    if unrelated:
        graph.add_node(TaskNode('unrelated', 'other tests', 'tester',
            acceptance_criteria=[observation()['criterion']]))
    before = deepcopy(graph.get_node('old').metadata)
    governor = GlobalGovernor(graph, TraceRecorder(tmp_path))
    with pytest.raises(ValueError, match='RECOVERY_RECHECK_DROPPED'):
        governor.apply_task_graph_patch({
            'update_nodes': [{'node_id': 'old', 'status': 'superseded'}],
            'add_nodes': [{'node_id': 'repair', 'goal': 'investigate and write report',
                'role': 'coder', 'metadata': {'recovery_of': 'old'}}]})
    assert graph.get_node('old').status == 'repair_pending'
    assert graph.get_node('old').metadata == before
    assert graph.get_node('repair') is None


def test_recheck_may_live_on_dependent_tester():
    from AstralNexus.core.governor import TaskGraphPatch
    from AstralNexus.runtime.development_playbook import validate_replacement_rechecks
    graph = TaskGraph()
    graph.add_node(TaskNode('old', 'verify', 'tester', status='repair_pending',
        acceptance_criteria=[observation()['criterion']], metadata={
        '_verification_receipt': {'observations': [observation()]}}))
    patch = TaskGraphPatch(update_nodes=[{'node_id': 'old', 'status': 'superseded'}], add_nodes=[
        {'node_id': 'repair', 'metadata': {'recovery_of': 'old'}},
        {'node_id': 'recheck', 'dependencies': ['repair'], 'acceptance_criteria': [observation()['criterion']]},
    ])
    validate_replacement_rechecks(graph, patch)
    patch.add_nodes[1]['acceptance_criteria'] = ['tests_passed:python report.py']
    with pytest.raises(ValueError, match='RECOVERY_RECHECK_DROPPED'):
        validate_replacement_rechecks(graph, patch)
