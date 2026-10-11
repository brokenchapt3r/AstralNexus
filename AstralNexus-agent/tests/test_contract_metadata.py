from copy import deepcopy
from dataclasses import asdict
import pytest
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.graph_transaction import TaskGraphTransaction
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore


def test_legacy_import_and_both_update_faces_have_one_value():
    node = TaskNode('N', 'task', 'coder', metadata={'owned_paths': ['old.py']})
    assert node.owned_paths == ['old.py']
    node.owned_paths = []
    assert node.metadata['owned_paths'] == []
    node.metadata.update({'owned_paths': ['new.py']})
    assert node.owned_paths == ['new.py']
    node.metadata['owned_paths'].append('second.py')
    assert node.owned_paths == ['new.py', 'second.py']
    assert asdict(node)['metadata']['owned_paths'] == node.owned_paths


def test_copy_is_detached_and_transaction_rollback_keeps_binding():
    graph = TaskGraph()
    node = graph.add_node(TaskNode('N', 'task', 'coder', owned_paths=['before.py']))
    copied = deepcopy(node.metadata)
    copied['owned_paths'].append('foreign.py')
    assert node.owned_paths == ['before.py']
    with pytest.raises(ValueError):
        with TaskGraphTransaction(graph):
            node.metadata['owned_paths'] = ['changed.py']
            raise ValueError('rollback')
    restored = graph.get_node('N')
    restored.owned_paths = ['after.py']
    assert restored.metadata['owned_paths'] == ['after.py']


def test_checkpoint_does_not_resurrect_cleared_ownership(tmp_path):
    graph = TaskGraph()
    node = graph.add_node(TaskNode('N', 'task', 'coder', metadata={'owned_paths': ['old.py']}))
    node.owned_paths = []
    store = SQLiteTaskGraphStore(tmp_path / 'graph.sqlite')
    store.create_run('run', 'task', [])
    assert store.save_task_graph('run', graph)
    restored = store.load_task_graph('run').get_node('N')
    assert restored.owned_paths == [] and restored.metadata['owned_paths'] == []


def test_union_assignment_updates_owner_but_merge_is_detached():
    node = TaskNode('N', 'task', 'coder', owned_paths=['before.py'])
    node.metadata |= {'owned_paths': ['after.py']}
    assert node.owned_paths == ['after.py']
    merged = node.metadata | {'owned_paths': ['detached.py']}
    assert type(merged) is dict
    assert node.owned_paths == ['after.py']


def test_explicit_empty_fields_and_replaced_metadata_never_resurrect_old_contract():
    node = TaskNode('N', 'task', 'coder', owned_paths=[], input_artifacts=[], output_artifacts=[], handoff_contract={},
                    metadata={'owned_paths': ['old.py'], 'input_artifacts': ['old-in'], 'output_artifacts': ['old-out'], 'handoff_contract': {'old': True}})
    assert node.owned_paths == [] and node.input_artifacts == [] and node.output_artifacts == [] and node.handoff_contract == {}
    node.metadata = {'owned_paths': ['resurrect.py']}
    assert node.owned_paths == [] and node.metadata['owned_paths'] == []
    legacy = TaskNode('legacy', 'task', 'coder', metadata={'owned_paths': ['legacy.py']})
    assert legacy.owned_paths == ['legacy.py']
    legacy.owned_paths = []
    legacy.metadata = {'owned_paths': ['resurrect.py']}
    assert legacy.owned_paths == []


def test_new_columns_import_legacy_once_but_existing_empty_is_canonical(tmp_path):
    import json
    import sqlite3
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE nodes(node_id TEXT PRIMARY KEY,run_id TEXT,goal TEXT,role TEXT,status TEXT,
                    dependencies TEXT,constraints TEXT,inputs TEXT,outputs TEXT,risk_level TEXT,sensitivity TEXT,
                    resource_tier TEXT,retry_count INTEGER,acceptance_criteria TEXT,created_at TEXT,updated_at TEXT,metadata TEXT)""")
        db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('N','run','task','coder','admitted','[]','[]','[]','[]','low','public','local',0,'[]','','',json.dumps({'owned_paths':['legacy.py']})))
    store = SQLiteTaskGraphStore(path)
    assert store.get_node('N', run_id='run').owned_paths == ['legacy.py']
    with sqlite3.connect(path) as db:
        db.execute("UPDATE nodes SET owned_paths='[]' WHERE run_id='run' AND node_id='N'")
    store = SQLiteTaskGraphStore(path)
    assert store.get_node('N', run_id='run').owned_paths == []


def test_replaced_metadata_reference_is_a_detached_snapshot():
    node = TaskNode('N', 'task', 'coder', owned_paths=['active.py'])
    old = node.metadata
    node.metadata = {'label': 'new'}
    old['owned_paths'].append('detached.py')
    old['input_artifacts'] = ['detached-input']
    assert node.owned_paths == ['active.py'] and node.input_artifacts == []
    assert node.metadata['owned_paths'] == ['active.py']
    current = node.metadata
    node.metadata |= {'owned_paths': ['latest.py']}
    assert node.metadata is current and node.owned_paths == ['latest.py']


def test_invalid_metadata_replacement_keeps_existing_binding():
    node = TaskNode('N', 'task', 'coder', owned_paths=['before.py'])
    previous = node.metadata
    with pytest.raises(ValueError):
        node.metadata = [('invalid',)]
    assert node.metadata is previous
    node.metadata['owned_paths'] = ['after.py']
    assert node.owned_paths == ['after.py']


def test_expected_outputs_and_routing_contract_are_projections(tmp_path):
    node = TaskNode('N', 'task', 'coder', metadata={'expected_outputs': ['report.json'],
                    'task_contract': {'expected_outputs': ['wrong.json'], 'role_hint': 'tester'}})
    assert node.expected_outputs == ['report.json']
    assert node.metadata['task_contract']['expected_outputs'] is node.expected_outputs
    node.expected_outputs = []
    assert node.metadata['expected_outputs'] == [] and node.metadata['task_contract']['expected_outputs'] == []
    node.metadata['expected_outputs'] = ['new.json']
    assert node.metadata['task_contract']['expected_outputs'] == ['new.json']
    node.metadata['task_contract'] = {'expected_outputs': ['wrong-again.json'], 'role_hint': 'analyst'}
    assert node.expected_outputs == ['new.json'] and node.metadata['task_contract']['expected_outputs'] == ['new.json']
    assert node.metadata['task_contract']['role_hint'] == 'analyst'
    graph = TaskGraph()
    graph.add_node(node)
    store = SQLiteTaskGraphStore(tmp_path / 'graph.sqlite')
    store.create_run('run', 'task', [])
    store.save_task_graph('run', graph)
    restored = store.load_task_graph('run').get_node('N')
    assert restored.expected_outputs == ['new.json']
    assert restored.metadata['task_contract']['expected_outputs'] == ['new.json']
