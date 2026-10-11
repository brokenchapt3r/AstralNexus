import json

from AstralNexus.runtime.json_evidence import observe_json


def test_missing_is_not_null_and_body_is_not_transport_proof():
    observation = observe_json('receipt.json', '{"code":403,"status":"failed"}')
    fields = observation['status_fields']
    assert fields['status_code'] == {'presence': 'missing'}
    assert fields['code']['value'] == 403
    assert fields['code']['http_status_candidate'] is True
    assert fields['status']['http_status_candidate'] is False
    assert observation['http_transport_verified'] is False
    explicit_null = observe_json('receipt.json', '{"status_code":null}')
    assert explicit_null['status_fields']['status_code']['presence'] == 'present'
    assert explicit_null['status_fields']['status_code']['is_null'] is True
    assert observation['content_sha256'] != explicit_null['content_sha256']


def test_ambiguous_or_incomplete_json_cannot_supply_field_facts():
    for content, truncated, expected in [
        ('{"code":200,"code":403}', False, 'ambiguous_duplicate_keys'),
        ('{"code":403}', True, 'not_parsed_truncated'),
        ('{"code":', False, 'invalid_json'),
    ]:
        observed = observe_json('r.json', content, truncated=truncated)
        assert observed['parse_status'] == expected
        assert 'status_fields' not in observed


def test_verifier_observations_survive_checkpoint_and_reach_recovery(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    from AstralNexus.core.verifier import VerificationResult, VerificationVerdict

    (tmp_path / 'receipt.json').write_text('{"code":403}', encoding='utf-8')
    captured = {}

    class Client:
        def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if 'json_observations' in payload:
                captured['verification'] = payload['json_observations']
                return json.dumps({'score': 0, 'blocking_gaps': ['HTTP transport not correlated']})
            captured['recovery'] = payload['failed_node_report']
            return '{"add_nodes":[],"update_nodes":[]}'

    with Orchestrator(tmp_path, run_id='json-observations') as orchestrator:
        node = TaskNode(node_id='verify', role='tester', goal='verify HTTP rejection',
                        status='repair_pending', owned_paths=['receipt.json'])
        orchestrator.task_graph.add_node(node)
        orchestrator.verifier.llm_client = Client()
        orchestrator.verifier.workspace_root = tmp_path
        dimension = orchestrator.verifier._verify_semantic_goal_coverage(node)
        assert not dimension.passed
        verdict = VerificationResult(node_id=node.node_id, verdict=VerificationVerdict.REPLAN_REQUIRED,
                                     dimensions=[dimension], overall_score=0)
        orchestrator._persist_verification_receipt(node, verdict)
        orchestrator._persist_graph_checkpoint(stage='test')
        restored = orchestrator.graph_store.load_task_graph(orchestrator.run_id)
        restored_node = restored.get_node('verify')
        report = orchestrator._worker_report_for_node(restored_node)
        orchestrator.governor.llm_client = Client()
        orchestrator.governor.llm_matching_enabled = True
        orchestrator.governor.plan_verification_recovery(
            node_id=node.node_id, verdict='replan_required', overall_score=0,
            dimensions=[], worker_report=report)
        assert captured['recovery']['verification_observations'] == captured['verification']
        assert captured['verification'][0]['status_fields']['status_code'] == {'presence': 'missing'}
