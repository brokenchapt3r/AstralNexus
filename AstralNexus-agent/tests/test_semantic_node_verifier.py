import json
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.trace import TraceRecorder


def test_semantic_verifier_receives_accepted_upstream_comparison_files(tmp_path):
    from AstralNexus.runtime.execution_store import ExecutionStore
    (tmp_path / "spec.json").write_text('{"denied":403}', encoding="utf-8")
    (tmp_path / "old.md").write_text('superseded assumptions', encoding="utf-8")
    (tmp_path / "tests.py").write_text('assert status == 403', encoding="utf-8")
    store = ExecutionStore(tmp_path)
    execution = store.begin("tests", {"owned_paths": ["tests.py"], "upstream_artifacts": [
        {"path": "spec.json", "acceptance_status": "accepted"},
        {"path": "old.md", "acceptance_status": "rejected"},
    ]})
    class Client:
        def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            files = {item["path"]: item for item in payload["actual_files"]}
            assert files["spec.json"]["content"] == '{"denied":403}'
            assert "tests.py" in files and "old.md" not in files
            assert payload["owned_paths"] == ["tests.py"]
            assert payload["upstream_reference_paths"] == ["spec.json"]
            return '{"score":1,"blocking_gaps":[]}'
    node = TaskNode(node_id="tests", role="coder", goal="match spec", owned_paths=["tests.py"],
                    metadata={"runtime_execution_id": execution})
    result = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(),
                      workspace_root=tmp_path)._verify_goal_coverage(node)
    assert result.passed, result.details


def test_semantic_verifier_checks_actual_file_content(tmp_path):
    (tmp_path / "result.json").write_text('{"total":99}', encoding="utf-8")
    class Client:
        def chat(self, messages, **kwargs):
            evidence = json.loads(messages[-1]["content"])
            assert evidence["actual_files"][0]["content"] == '{"total":99}'
            assert kwargs["response_format"] == "json"
            return '{"score":0.1,"details":"actual total is wrong"}'
    node = TaskNode(node_id="test", goal="total must be 6", role="coder", metadata={
        "expected_artifact_paths": ["result.json"],
        "evidence_packet": {"summary": "total must be 6; all passed"},
    })
    verifier = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(), workspace_root=tmp_path)
    result = verifier._verify_goal_coverage(node)
    assert not result.passed and result.score == 0.1


def test_reference_tree_cannot_starve_current_report(tmp_path):
    from AstralNexus.runtime.execution_store import ExecutionStore
    inputs = tmp_path / "upstream"
    inputs.mkdir()
    for index in range(100):
        (inputs / f"log{index}.txt").write_text('reference ' * 1000, encoding='utf-8')
    report = 'REPORT_START\n' + 'real evidence\n' * 1200 + 'REPORT_END'
    (tmp_path / 'report.md').write_text(report, encoding='utf-8')
    execution = ExecutionStore(tmp_path).begin('review', {
        'owned_paths': ['report.md'], 'upstream_artifacts': [
            {'path': 'upstream', 'acceptance_status': 'accepted'}]})
    class Client:
        def chat(self, messages, **kwargs):
            files = json.loads(messages[-1]['content'])['actual_files']
            output = next(f for f in files if f['path'] == 'report.md')
            assert output['content'] == report and not output['truncated']
            assert len(files) == 101
            assert all(f['content'] for f in files)
            return '{"score":1,"blocking_gaps":[]}'
    node = TaskNode(node_id='review', role='verifier', goal='read report',
                    owned_paths=['report.md'], metadata={'runtime_execution_id': execution})
    result = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(),
                      workspace_root=tmp_path)._verify_goal_coverage(node)
    assert result.passed, result.details


def test_semantic_verifier_failure_never_falls_back_to_keyword_pass(tmp_path):
    class Client:
        def chat(self, *args, **kwargs):
            return '{"score":"NaN"}'
    node = TaskNode(node_id="test", goal="exact repeated goal", role="coder", metadata={
        "evidence_packet": {"summary": "exact repeated goal"},
    })
    verifier = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(), workspace_root=tmp_path)
    result = verifier._verify_goal_coverage(node)
    assert result.score == 0 and not result.passed
    assert "Semantic verification failed" in result.details


def test_required_gap_overrides_high_model_score(tmp_path):
    class Client:
        def chat(self, *args, **kwargs):
            return json.dumps({"score": 0.95, "details": "files exist",
                               "blocking_gaps": ["authenticated request returns HTTP 500"]})
    node = TaskNode(node_id="backend", goal="working authenticated API", role="coder")
    verifier = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(), workspace_root=tmp_path)
    result = verifier._verify_goal_coverage(node)
    assert result.score == 0 and not result.passed
    assert "HTTP 500" in result.details


def test_verifier_preserves_original_and_source_requirements(tmp_path):
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id='intent', role='user_intent', goal='only forward state transitions'))
    class Client:
        def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            assert payload['source_requirements'] == ['closed is terminal']
            assert payload['original_user_requirements'] == ['only forward state transitions']
            return '{"score":0,"blocking_gaps":["contract allows backward transition"]}'
    node = TaskNode(node_id='design', role='architect', goal='define API',
                    metadata={'source_requirements': ['closed is terminal']})
    verifier = Verifier(graph, TraceRecorder(tmp_path), llm_client=Client(), workspace_root=tmp_path)
    result = verifier._verify_goal_coverage(node)
    assert not result.passed
    assert 'backward transition' in result.details


def test_verifier_reads_complete_component_and_deduplicates_paths(tmp_path):
    from AstralNexus.core.task_graph import Artifact
    graph = TaskGraph()
    source = tmp_path / 'main.py'
    source.write_text('# padding\n' * 1800 + 'ENDPOINT_IMPLEMENTED = True\n', encoding='utf-8')
    (tmp_path / 'schema.sql').write_text('CREATE TABLE assets(id INTEGER);', encoding='utf-8')
    graph.add_artifact(Artifact(artifact_id='source', node_id='backend', artifact_type='file',
                                path=str(source), description='source'))
    class Client:
        def chat(self, messages, **kwargs):
            files = json.loads(messages[-1]['content'])['actual_files']
            assert [f['path'] for f in files] == ['main.py', 'schema.sql']
            assert files[0]['content'].endswith('ENDPOINT_IMPLEMENTED = True\n')
            assert not files[0]['truncated']
            assert 'CREATE TABLE' in files[1]['content']
            return '{"score":1,"blocking_gaps":[]}'
    node = TaskNode(node_id='backend', goal='implementation', role='coder', metadata={
        'expected_artifact_paths': ['main.py', 'schema.sql']})
    result = Verifier(graph, TraceRecorder(tmp_path), llm_client=Client(),
                      workspace_root=tmp_path)._verify_goal_coverage(node)
    assert result.passed


def test_recovery_verifier_reads_inherited_directory_without_new_artifacts(tmp_path):
    directory = tmp_path / "backend"
    directory.mkdir()
    (directory / "main.py").write_text("BROKEN = True", encoding="utf-8")
    (directory / "schema.sql").write_text("CREATE TABLE x(id INTEGER);", encoding="utf-8")
    class Client:
        def chat(self, messages, **kwargs):
            files = json.loads(messages[-1]["content"])["actual_files"]
            assert {item["path"] for item in files} == {"backend/main.py", "backend/schema.sql"}
            return '{"score":0,"blocking_gaps":["BROKEN implementation"]}'
    node = TaskNode(node_id="replacement", role="coder", goal="repair component", owned_paths=["backend/"])
    result = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(), workspace_root=tmp_path)._verify_goal_coverage(node)
    assert not result.passed
    assert "BROKEN" in result.details


def test_semantic_verifier_prompt_distinguishes_missing_from_null_and_status_aliases(tmp_path):
    """Fix 15a: live run derived endless REPAIR-UNAUTHORIZED-ASSIGN-EVIDENCE
    generations because the verifier described a receipt using {"code": 403}
    as "status_code: null" (field name misread + missing≠null conflation),
    and the recovery planner kept re-issuing the disproved literal premise."""
    (tmp_path / "receipt.json").write_text('{"code": 403}', encoding="utf-8")
    captured: dict = {}

    class Client:
        def chat(self, messages, **kwargs):
            captured["system"] = messages[0]["content"]
            evidence = json.loads(messages[-1]["content"])
            assert evidence["actual_files"][0]["content"] == '{"code": 403}'
            return '{"score":1.0,"details":"code=403 correlated with request log","blocking_gaps":[]}'

    node = TaskNode(node_id="sec", goal="unauthorized assign must be rejected", role="tester", metadata={
        "expected_artifact_paths": ["receipt.json"],
        "evidence_packet": {"summary": "403 recorded"},
    })
    verifier = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(), workspace_root=tmp_path)
    result = verifier._verify_goal_coverage(node)
    assert result.passed
    system = captured["system"]
    # missing vs null distinction
    assert "field_missing" in system
    assert "不得描述为null" in system
    # HTTP status field aliases + real-request correlation
    assert "status_code、code或status" in system
    assert "evidence_uncorrelated" in system
    # never demand proof of a premise contradicted by file facts
    assert "反证" in system


def test_semantic_verifier_payload_scopes_criteria_to_owned_paths(tmp_path):
    """Fix 25a (live r12 iter 33 blocked): architect A-001 owned
    architecture_draft.md while docs-bundle node D-001 owned the final
    architecture.md. The judge zeroed goal_coverage because the
    "architecture.md required by the bound criteria" did not exist — an
    out-of-contract file — and the repair planner then fought D-001 forever.
    The payload must carry the node's owned_paths and an acceptance-scope
    rule: criteria demanding files outside this node's contract are other
    carriers' responsibility and must never become this node's blocking
    gaps; content-level requirements still apply inside the contract."""
    captured: dict = {}

    class Client:
        def chat(self, messages, **kwargs):
            captured["payload"] = json.loads(messages[-1]["content"])
            return '{"score":1.0,"details":"draft complete","blocking_gaps":[]}'

    node = TaskNode(
        node_id="A-001", goal="输出结构化设计契约", role="architect",
        owned_paths=["deliverables/docs/architecture_draft.md"],
        acceptance_criteria=["architecture.md 必须定义状态迁移与权限矩阵"],
    )
    verifier = Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=Client(),
                        workspace_root=tmp_path)
    result = verifier._verify_goal_coverage(node)
    assert result.passed
    payload = captured["payload"]
    assert payload["owned_paths"] == ["deliverables/docs/architecture_draft.md"]
    rule = payload["acceptance_scope_rule"]
    assert "owned_paths" in rule
    assert "blocking_gap" in rule
    assert "其他节点职责" in rule
    # Fix 26b: the goal text itself may demand an out-of-contract file (the
    # live A-001-R1 goal said "Produce final architecture.md"); the judge
    # must scope goals the same way as criteria.
    assert "goal 文本" in rule
