import pytest

from AstralNexus.core.task_graph import Artifact, ArtifactAcceptanceStatus, TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.trace import TraceRecorder


def graph_with_replacement(status):
    graph = TaskGraph()
    graph.add_node(TaskNode('old', 'produce', 'coder', status='superseded'))
    graph.add_node(TaskNode('new', 'produce', 'coder', status=status,
                            metadata={'recovery_of': 'old'}))
    consumer = TaskNode('consumer', 'consume', 'tester', dependencies=['old'])
    graph.add_node(consumer)
    return graph, consumer


@pytest.mark.parametrize('status,available', [('completed', True), ('ready', False),
                                            ('running', False), ('failed', False)])
def test_scheduler_and_verifier_resolve_same_producer(tmp_path, status, available):
    graph, consumer = graph_with_replacement(status)
    verifier = Verifier(graph, TraceRecorder(tmp_path))
    assert (not graph.readiness_blockers(consumer)['unsatisfied_dependencies']) is available
    assert verifier._verify_cross_agent_consistency(consumer).score == float(available)


@pytest.mark.parametrize('shape', ['missing', 'cycle', 'ambiguous'])
def test_invalid_lineage_cannot_satisfy_dependency(shape):
    graph, consumer = graph_with_replacement('completed')
    if shape == 'missing':
        del graph.nodes['new']
    elif shape == 'cycle':
        graph.nodes['new'].status = 'superseded'
        graph.nodes['old'].metadata['recovery_of'] = 'new'
    else:
        graph.add_node(TaskNode('other', 'produce', 'coder', status='completed',
                                metadata={'recovery_of': 'old'}))
    assert graph.resolve_dependency('old') is None
    assert graph.readiness_blockers(consumer)['unsatisfied_dependencies'] == ['old']
    assert not graph.dependency_available(consumer, 'old', for_verification=True)


def test_multigeneration_handoff_requires_successors_accepted_artifact():
    graph, consumer = graph_with_replacement('superseded')
    graph.add_node(TaskNode('latest', 'produce', 'coder', status='completed',
                            metadata={'supersedes_node_id': 'new'}))
    consumer.input_artifacts = ['output.json']
    graph.add_artifact(Artifact(artifact_id='old-file', node_id='old', artifact_type='file',
                                path='output.json', description='old result',
                                acceptance_status=ArtifactAcceptanceStatus.ACCEPTED))
    assert graph.readiness_blockers(consumer)['missing_input_artifacts'] == ['output.json']
    artifact = Artifact(artifact_id='new-file', node_id='latest', artifact_type='file',
                        path='output.json', description='current result')
    graph.add_artifact(artifact)
    assert graph.readiness_blockers(consumer)['missing_input_artifacts'] == ['output.json']
    artifact.acceptance_status = ArtifactAcceptanceStatus.ACCEPTED
    assert not any(graph.readiness_blockers(consumer).values())


def test_repair_may_consume_failed_parent_without_claiming_product_acceptance():
    graph, consumer = graph_with_replacement('failed')
    consumer.metadata['trigger'] = 'fault_recovery'
    assert graph.dependency_available(consumer, 'old')
    assert graph.dependency_available(consumer, 'old', for_verification=True)
    assert graph.nodes['new'].status == 'failed'


def test_replacement_cannot_depend_on_itself_through_ancestor():
    graph, _ = graph_with_replacement('completed')
    node = graph.nodes['new']
    node.dependencies = ['old']
    assert not graph.dependency_available(node, 'old', for_verification=True)
