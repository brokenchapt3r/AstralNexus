"""Inversion tests for the real Governor mutation and evidence boundaries."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import hashlib
import threading
import time

import pytest

from AstralNexus.core.governor import GlobalGovernor, GlobalBudget, TaskGraphPatch, TaskProposal, WorkerMatcherProfileError
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder


def governor(tmp_path, **kwargs):
    return GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path), **kwargs)


def node(ident, **extra):
    return {'node_id': ident, 'goal': 'implement ' + ident, 'role': 'coder', 'required_capabilities': ['code_generation'], **extra}


@pytest.mark.parametrize('limit', ['active', 'total', 'tokens'])
def test_actual_patch_cannot_bypass_admission_limits(tmp_path, limit):
    gov = governor(tmp_path, max_active_tasks=1 if limit == 'active' else 20,
                   max_total_tasks=1 if limit == 'total' else 20)
    if limit == 'tokens':
        gov.budget.max_tokens = 10
    patch = TaskGraphPatch(add_nodes=[node('A', metadata={'estimated_cost': 6}), node('B', metadata={'estimated_cost': 6})])
    before = deepcopy(patch)
    with pytest.raises(ValueError, match='admission'):
        gov.apply_task_graph_patch(patch)
    assert not gov.task_graph.nodes
    assert patch == before
    assert gov.get_admission_stats()['rejected'] == 2


@pytest.mark.parametrize('bad', [None, {'node_id': 'BAD'}, {'node_id': 'BAD', 'goal': ''}])
def test_malformed_candidate_rejects_entire_patch(tmp_path, bad):
    gov = governor(tmp_path)
    with pytest.raises(ValueError, match='add_nodes'):
        gov.apply_task_graph_patch(TaskGraphPatch(add_nodes=[node('GOOD'), bad]))
    assert not gov.task_graph.nodes


@pytest.mark.parametrize('field', ['add_edges', 'remove_edges', 'update_nodes'])
def test_malformed_operation_cannot_be_silently_skipped(tmp_path, field):
    gov = governor(tmp_path)
    patch = TaskGraphPatch(add_nodes=[node('GOOD')], **{field: [None]})
    with pytest.raises(ValueError, match=field):
        gov.apply_task_graph_patch(patch)
    assert not gov.task_graph.nodes


def test_rejected_candidate_does_not_publish_gaps_or_mutate_input(tmp_path, monkeypatch):
    gov = governor(tmp_path)
    gov.contract_clause_gaps = [{'path': 'old.txt'}]
    monkeypatch.setattr(gov, '_unanchored_contract_clauses', lambda *a: [{'path': 'candidate.txt'}])
    patch = TaskGraphPatch(add_nodes=[node('A')], add_edges=[{'source': 'A', 'target': 'missing'}])
    before = deepcopy(patch)
    with pytest.raises(ValueError):
        gov.apply_task_graph_patch(patch)
    assert gov.contract_clause_gaps == [{'path': 'old.txt'}]
    assert patch == before
    assert not gov.task_graph.nodes


def test_concurrent_validation_and_commit_share_the_same_graph_lock(tmp_path, monkeypatch):
    gov = governor(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    real = gov._unanchored_contract_clauses
    def inspect_candidate(*args):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return real(*args)
    monkeypatch.setattr(gov, '_unanchored_contract_clauses', inspect_candidate)
    def patch(letter):
        return TaskGraphPatch(add_nodes=[{'goal': 'build ' + letter, 'role': 'coder', 'owned_paths': ['deliverables/' + letter + '.py'], 'expected_outputs': ['deliverables/' + letter + '.py'], 'required_capabilities': ['code_generation']}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gov.apply_task_graph_patch, patch('alpha'))
        assert entered.wait(5)
        second = pool.submit(gov.apply_task_graph_patch, patch('beta'))
        time.sleep(.1)
        assert calls == [1]
        release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert set(a['added']).isdisjoint(b['added'])
    assert len(gov.task_graph.nodes) == 2
    assert gov.total_tasks_admitted == 2


def summary():
    return {'group_id': 'G', 'source_nodes': ['N'], 'verified_claims': [{'claim': 'evidence', 'evidence_refs': ['deliverables/evidence.txt']}],
            'source_artifacts': ['deliverables/evidence.txt'], 'conflicts': [], 'coverage_gaps': [], 'constraints': [], 'recommended_followups': []}


def accepted_evidence(tmp_path):
    gov = governor(tmp_path)
    gov.task_graph.add_node({'node_id': 'N', 'goal': 'verified evidence', 'role': 'tester', 'status': 'completed', 'metadata': {'task_version': 1}})
    path = tmp_path / 'deliverables/evidence.txt'
    path.parent.mkdir(exist_ok=True)
    path.write_text('verified evidence', encoding='utf-8')
    gov.task_graph.add_artifact({'artifact_id': 'A', 'node_id': 'N', 'artifact_type': 'file', 'description': 'verified fixture', 'path': 'deliverables/evidence.txt',
                                 'acceptance_status': 'accepted', 'metadata': {'task_version': 1, 'content_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}})
    return gov, path


@pytest.mark.parametrize('invalid', ['empty', 'missing_node', 'missing_file', 'pending', 'stale_artifact', 'stale_producer', 'changed_bytes', 'wrong_producer', 'unregistered'])
def test_global_verifier_rejects_unbound_evidence(tmp_path, invalid):
    gov, path = accepted_evidence(tmp_path)
    value = summary()
    if invalid == 'empty':
        value['source_nodes'] = []; value['verified_claims'] = []; value['source_artifacts'] = []
    elif invalid == 'missing_node':
        value['source_nodes'] = ['MISSING']
    elif invalid == 'missing_file':
        path.unlink()
    elif invalid == 'pending':
        gov.task_graph.artifacts['A'].acceptance_status = 'pending'
    elif invalid == 'stale_artifact':
        gov.task_graph.artifacts['A'].metadata['task_version'] = 0
    elif invalid == 'stale_producer':
        gov.task_graph.get_node('N').metadata['task_version'] = 0
    elif invalid == 'changed_bytes':
        path.write_text('later overwrite', encoding='utf-8')
    elif invalid == 'wrong_producer':
        gov.task_graph.artifacts['A'].node_id = 'OTHER'
    elif invalid == 'unregistered':
        gov.task_graph.artifacts.clear()
    report = gov.verify_evidence_summaries({}, {'G': value}, run_dir=tmp_path)
    assert report['overall_status'] == 'fail'
    assert report['criterion_results'][0]['checks']['claim_evidence_binding'] is False


def test_accepted_current_bound_evidence_passes(tmp_path):
    gov, _ = accepted_evidence(tmp_path)
    assert gov.verify_evidence_summaries({}, {'G': summary()}, run_dir=tmp_path)['overall_status'] == 'pass'




def test_token_reservations_prevent_parallel_over_admission_and_settle_once():
    budget = GlobalBudget(max_tokens=100, max_parallel_slots=4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        def reserve(ident):
            try:
                budget.reserve(ident, 60)
                return True
            except ValueError:
                return False
        assert sorted(pool.map(reserve, ['A', 'B'])) == [False, True]
    assert budget.reserved_tokens == 60 and budget.remaining_tokens() == 40
    ident = next(iter(budget._starts))
    budget.settle(ident, 50, 1)
    assert budget.reserved_tokens == 0 and budget.used_tokens == 50
    with pytest.raises(ValueError):
        budget.settle(ident, 50, 1)
    assert budget.used_tokens == 50


def test_parallel_worker_seconds_are_not_wall_clock_and_full_slots_do_not_terminate(tmp_path):
    clock = [0.0]
    gov = governor(tmp_path)
    gov.budget = GlobalBudget(max_time_seconds=70, max_parallel_slots=8, _clock=lambda: clock[0])
    for index in range(8):
        gov.record_task_start(str(index))
    assert gov.get_termination_reason()['reason_code'] == 'NO_TERMINATION'
    clock[0] = 60
    for index in range(8):
        gov.record_task_end(str(index), actual_time_seconds=60)
    assert gov.budget.used_time_seconds == 60
    assert gov.budget.worker_time_seconds == 480
    assert gov.budget.remaining_time() == 10


def test_replacement_patch_drops_retired_background_candidates(tmp_path):
    gov = governor(tmp_path)
    old = TaskGraphPatch(background_candidates=[{'goal': 'obsolete'}])
    repair = TaskGraphPatch(candidate_mode='replace', background_candidates=[{'goal': 'current'}])
    merged = gov.merge_rejected_task_graph_patch(old, repair)
    assert merged.background_candidates == repair.background_candidates
    assert merged.candidate_mode == 'replace'


def test_programming_error_in_matcher_is_not_retried(tmp_path):
    from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
    calls = []
    def broken(*a):
        calls.append(1)
        raise KeyError('bug')
    gov = governor(tmp_path, llm_client=SimpleNamespace(chat=broken))
    with pytest.raises(KeyError):
        gov.select_worker_profile({'goal': 'write code', 'required_capabilities': ['code_generation']}, HETEROGENEOUS_WORKERS)
    assert len(calls) == 1


def test_early_proposal_rejection_updates_admission_statistics(tmp_path):
    gov = governor(tmp_path, max_total_tasks=1)
    gov.task_graph.add_node({'node_id': 'existing', 'goal': 'existing work', 'role': 'coder'})
    proposal = TaskProposal(goal='new work', required_capabilities=['code_generation'], expected_information_gain=1.0, risk_reduction=1.0, parallel_benefit=1.0, estimated_cost=1, dependencies=[])
    decision = gov.evaluate_task_proposal(proposal)
    assert not decision.admitted
    assert gov.total_tasks_created == 1 and gov.total_tasks_rejected == 1


def test_required_and_optional_string_flags_are_parsed_as_values(tmp_path):
    import json
    gov = governor(tmp_path)
    response = {'required_outputs': [{'name': 'optional file', 'required': 'false', 'artifact_path': 'deliverables/result.md'}],
                'summary_required': 'false', 'needs_clarification': 'false', 'needs_citations': 'false', 'needs_artifact_refs': 'false'}
    contract = gov.derive_delivery_contract('analyze data', SimpleNamespace(chat=lambda *a, **kw: json.dumps(response)))
    for key in ['summary_required', 'needs_clarification', 'needs_citations', 'needs_artifact_refs']:
        assert contract[key] is False
    assert contract['required_outputs'][0]['required'] is False
    assert contract['required_outputs'][0]['path'] == 'deliverables/result.md'


def test_specific_matcher_profile_error_is_preserved(tmp_path):
    from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
    gov = governor(tmp_path, llm_client=SimpleNamespace(chat=lambda *a: '{"worker_type":"INVENTED"}'))
    with pytest.raises(WorkerMatcherProfileError):
        gov.select_worker_profile({'goal': 'write code', 'required_capabilities': ['code_generation']}, HETEROGENEOUS_WORKERS)


def test_unknown_usage_retains_reservation_instead_of_becoming_zero():
    budget = GlobalBudget(max_tokens=100)
    budget.reserve('A', 60)
    budget.settle('A', None, 1)
    assert budget.active_parallel_tasks == 0 and budget.used_tokens == 0
    assert budget.reserved_tokens == 60 and budget.unsettled_tokens == 60
    assert budget.remaining_tokens() == 40


def test_root_estimate_cannot_disappear_during_patch_normalization(tmp_path):
    gov = governor(tmp_path)
    gov.budget.max_tokens = 10
    with pytest.raises(ValueError, match='budget_exhausted'):
        gov.apply_task_graph_patch(TaskGraphPatch(add_nodes=[node('A', estimated_cost=11)]))
    assert not gov.task_graph.nodes


def test_matcher_programming_error_escapes_the_runtime_without_recovery(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    with Orchestrator(tmp_path, run_id='matcher-error') as runtime:
        runtime.task_graph.add_node({'node_id': 'N', 'goal': 'write code', 'role': 'coder', 'status': 'ready', 'metadata': {'required_capabilities': ['code_generation']}})
        def broken(*a, **kw):
            raise KeyError('framework bug')
        runtime.governor.enable_llm_worker_matching(SimpleNamespace(chat=broken))
        with pytest.raises(KeyError):
            runtime._execute_wave(['N'])
        assert runtime.runtime_state_manager.state.recovery_churn_total == 0
        assert runtime.task_graph.get_node('N').status == 'running'


def test_artifact_id_references_use_the_same_catalog_for_validation_and_reading(tmp_path):
    from AstralNexus.runtime.evidence_catalog import EvidenceCatalog
    gov, _ = accepted_evidence(tmp_path)
    value = summary()
    value['verified_claims'][0]['evidence_refs'] = ['A']
    value['source_artifacts'] = ['A']
    assert gov.verify_evidence_summaries({}, {'G': value}, run_dir=tmp_path)['overall_status'] == 'pass'
    assert EvidenceCatalog(tmp_path, graph=gov.task_graph, task_version=1).read_text('A') == 'verified evidence'


def test_preview_is_not_committed_admission_and_legacy_commit_counts_once(tmp_path):
    gov = governor(tmp_path)
    proposal = TaskProposal(goal='implement legacy module', required_capabilities=['code_generation'],
                            expected_information_gain=1.0, risk_reduction=1.0, parallel_benefit=1.0,
                            estimated_cost=1, dependencies=[])
    decision = gov.evaluate_task_proposal(proposal)
    assert decision.admitted
    assert gov.total_proposals_evaluated == 1
    assert gov.total_tasks_admitted == 0
    ident = gov.admit_task(proposal, decision)
    assert gov.task_graph.get_node(ident) is not None
    assert gov.total_tasks_created == 1 and gov.total_tasks_admitted == 1
    assert gov.get_admission_stats()['graph_total_tasks'] == 1


def test_reserved_capacity_does_not_terminate_running_tasks(tmp_path):
    gov = governor(tmp_path, max_total_tasks=1)
    gov.task_graph.add_node({'node_id': 'N', 'goal': 'run', 'role': 'coder', 'status': 'running'})
    gov.budget.max_tokens = 100
    gov.record_task_start('N', 100)
    assert gov.budget.remaining_tokens() == 0
    assert gov.get_termination_reason()['reason_code'] == 'NO_TERMINATION'


def test_actual_worker_journal_to_real_global_verifier_needs_no_invented_filename(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.layered_synthesis import _load_packets, _normalize_reducer
    from AstralNexus.agents.base import AgentResponse
    with Orchestrator(tmp_path, run_id='journal-chain') as runtime:
        runtime.verifier.workspace_root = tmp_path
        task = runtime.task_graph.add_node({'node_id': 'N', 'goal': 'create a result document', 'role': 'researcher', 'status': 'running',
            'acceptance_criteria': ['file:deliverables/result.txt exists'], 'owned_paths': ['deliverables/result.txt'],
            'outputs': ['deliverables/result.txt'], 'metadata': {'expected_outputs': ['deliverables/result.txt'], 'task_version': 1}})
        class Agent:
            role = 'researcher'
            def execute(self, context):
                target = tmp_path / 'deliverables/result.txt'
                target.parent.mkdir(exist_ok=True)
                target.write_text('The requested result exists.', encoding='utf-8')
                return AgentResponse(content='create a result document: completed, deliverables/result.txt exists.', artifacts=['deliverables/result.txt'])
        assert runtime._run_agent_task(Agent(), task) is True
        runtime.task_graph.update_node_status('N', 'result_submitted')
        results = {'completed_tasks': 0, 'failed_tasks': 0, 'recovery_attempts': 0}
        runtime._process_worker_results({'N': True}, results)
        assert task.status == 'completed', task.metadata
        packets = _load_packets(tmp_path, task_version=1, graph=runtime.task_graph)
        assert len(packets) == 1
        value = _normalize_reducer({'group_id': 'G', 'node_ids': ['N']}, {'verified_claims': [{'claim': 'Created the requested result document.'}]}, packets, task_version=1)
        refs = value['verified_claims'][0]['evidence_refs']
        assert refs == packets[0]['evidence_refs']
        assert refs != ['node_io/N_output.json']
        report = runtime.governor.verify_evidence_summaries({}, {'G': value}, run_dir=tmp_path)
        assert report['overall_status'] == 'pass', report
        from AstralNexus.core.layered_synthesis import build_layered_context
        import json
        class ReducerClient:
            def chat(self, messages, **kwargs):
                system = messages[0]["content"]
                if "Local Reducer" in system:
                    return json.dumps({"verified_claims": [{"claim": "Created the requested result document."}]})
                if "全局上下文治理器" in system:
                    return json.dumps({"groups": [{"group_id": "G", "title": "result", "node_ids": ["N"]}], "actions": [{"node_id": "N", "action": "retain"}]})
                if "交付契约管理器" in messages[-1]["content"]:
                    return json.dumps({"required_outputs": [], "summary_required": False})
                return "# Result outline"
        layered = build_layered_context(task_content="create a result document", run_dir=tmp_path,
                                        client=ReducerClient(), governor=runtime.governor)
        assert layered['global_verifier']['overall_status'] == 'pass'
        assert layered['outline']
        subject = tmp_path / 'deliverables/result.txt'
        original = subject.read_bytes()
        subject.write_text('later changed result', encoding='utf-8')
        assert _load_packets(tmp_path, task_version=1, graph=runtime.task_graph) == []
        subject.write_bytes(original)
        journal = tmp_path / refs[0]
        journal.write_text('late overwrite', encoding='utf-8')
        assert _load_packets(tmp_path, task_version=1, graph=runtime.task_graph) == []


def test_reducer_never_synthesizes_a_node_io_filename_from_a_node_id():
    from AstralNexus.core.layered_synthesis import _normalize_reducer
    packet = {'node_id': 'N', 'conclusion': 'known source', 'evidence_refs': ['node_io/exec-000007__node-N__v001__a001__output.json']}
    value = _normalize_reducer({'group_id': 'G', 'node_ids': ['N']}, {'verified_claims': [{'claim': 'known source'}]}, [packet], task_version=1)
    assert value['source_artifacts'] == packet['evidence_refs']
    assert value['verified_claims'][0]['evidence_refs'] == packet['evidence_refs']



def test_structured_future_artifact_refs_keep_identity_and_real_dependency(tmp_path):
    gov = governor(tmp_path)
    producer = node("P", owned_paths=["deliverables/main.py"], output_artifacts=[{"artifact_id": "artifact:P-main", "path": "deliverables/main.py"}])
    consumer = node("V", task_type="verification", owned_paths=[], input_artifacts=[{"artifact_id": "artifact:P-main", "path": "deliverables/main.py"}], dependencies=["P"])
    result = gov.apply_task_graph_patch(TaskGraphPatch(add_nodes=[producer, consumer]))
    assert set(result["added"]) == {"P", "V"}
    assert gov.task_graph.get_node("P").output_artifacts == ["artifact:P-main"]
    assert gov.task_graph.get_node("V").input_artifacts == ["artifact:P-main"]
    assert gov.task_graph.get_node("V").dependencies == ["P"]
