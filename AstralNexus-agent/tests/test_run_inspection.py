from types import SimpleNamespace
from AstralNexus.tui_gateway import inspection


def test_dependency_chain_reports_missing_endpoints_and_cycles():
    nodes = [{'node_id': key} for key in ['A', 'B', 'C']]
    assert inspection.dependency_chain(nodes, {'A': [], 'B': ['A'], 'C': ['B']}) == 'A → B → C'
    assert '环' in inspection.dependency_chain(nodes, {'A': ['C'], 'B': ['A'], 'C': ['B']})
    assert '端点缺失' in inspection.dependency_chain(nodes, {'A': ['missing'], 'B': [], 'C': []})


def test_evidence_view_keeps_acceptance_and_failure_distinct(monkeypatch):
    projection = {'verified': True, 'generation': 2, 'commit_seq': 4, 'cursor': {}, 'run_status': 'running',
                  'generation_alignment': 'pending_generation',
                  'graph': {'nodes': [{'node_id': 'N', 'status': 'repair_pending', 'metadata': {
                      '_verification_receipt': {'verdict': 'repair_required', 'observations': ['HTTP test failed'],
                                               'failed_dimensions': [{'name': 'API', 'details': 'expected 200 got 500'}]}}}],
                            'edges': [], 'artifacts': [{'path': 'rejected.json', 'acceptance_status': 'rejected'},
                                                      {'path': 'accepted.json', 'acceptance_status': 'accepted'}]}}
    monkeypatch.setattr(inspection, 'read_current_projection', lambda *args: projection)
    session = SimpleNamespace(title='run', workspace='workspace', run_dir='run', store=None)
    text = inspection.inspect_session(session, 'evidence')['text']
    assert '上一代' in text and '已拒绝' in text and '已采用' in text
    assert 'HTTP test failed' in text and 'expected 200 got 500' in text
    assert 'repair_required' in text


def test_delivery_view_uses_verified_gate_and_never_labels_partial_as_complete(tmp_path, monkeypatch):
    import hashlib
    import json
    root = tmp_path / 'run'
    version = root / 'deliverables' / 'v1'
    version.mkdir(parents=True)
    gate = {'overall_status': 'fail', 'delivery_authorized': False, 'failed_tests': ['AC-1'], 'unknown_tests': [],
            'criterion_results': [{'criterion_id': 'AC-1', 'status': 'fail', 'explanation': 'API failed'}]}
    summary = version / 'delivery_summary.json'
    summary.write_text(json.dumps({'delivery_gate': gate}), encoding='utf-8')
    (version / 'delivery_verification.json').write_text(json.dumps(gate), encoding='utf-8')
    (version / 'delivery_manifest.json').write_text(json.dumps({'status': 'published', 'version': 1,
        'artifacts': [{'path': summary.name, 'sha256': hashlib.sha256(summary.read_bytes()).hexdigest()}]}), encoding='utf-8')
    monkeypatch.setattr(inspection, 'read_current_projection', lambda *args: {'verified': True, 'generation': 1,
        'commit_seq': 1, 'cursor': {}, 'graph': {'nodes': [], 'edges': []}})
    session = SimpleNamespace(title='delivery', workspace=str(root), run_dir=str(root), review=SimpleNamespace(changes=lambda: []))
    text = inspection.inspect_session(session, 'delivery')['text']
    assert '未闭合' in text and 'API failed' in text and 'AC-1' in text
    assert '完整验收并发布' not in text
    summary.write_text('tampered', encoding='utf-8')
    text = inspection.inspect_session(session, 'delivery')['text']
    assert '不可确认' in text and '完整验收并发布' not in text
