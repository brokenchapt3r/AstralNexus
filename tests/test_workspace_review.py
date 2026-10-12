from pathlib import Path
import pytest
from AstralNexus.runtime.workspace_review import WorkspaceReview, review_scope
from AstralNexus.runtime.workbench import WorkbenchStore
from AstralNexus.tools.executor import ToolExecutor


def test_real_file_tool_has_reviewable_before_after_and_undo(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRALNEXUS_ALLOW_FILE_WRITE', 'true')
    store = WorkbenchStore(tmp_path / 'home')
    root = tmp_path / 'workspace'
    root.mkdir()
    path = root / 'app.py'
    path.write_text('before\n', encoding='utf-8')
    review = WorkspaceReview(store, 'session')
    with review_scope(review): executor = ToolExecutor(workspace_root=root)
    result = executor.execute_tool('file_write', {'path': 'app.py', 'content': 'after\n'})
    assert result['status'] == 'success'
    changes = WorkspaceReview(store, 'session').changes()
    assert changes[0]['id'] == result['change_id'] and changes[0]['changed']
    assert '-before' in changes[0]['diff'] and '+after' in changes[0]['diff']
    assert review.restore(result['change_id'])['ok']
    assert path.read_text() == 'before\n'
    assert review.changes()[0]['restored'] == 1


def test_new_file_undo_and_foreign_session_rejection(tmp_path):
    store = WorkbenchStore(tmp_path / 'home')
    review = WorkspaceReview(store, 'owner')
    path = tmp_path / 'new.txt'
    def write():
        path.write_text('new')
        return {'status': 'success'}
    result = review.execute('file_write', {'path': str(path)}, tmp_path, write)
    with pytest.raises(ValueError, match='unknown change'):
        WorkspaceReview(store, 'another').restore(result['change_id'])
    review.restore(result['change_id'])
    assert not path.exists()


def test_restore_refuses_user_edit_and_retains_both_evidence_versions(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    path = tmp_path / 'app.py'
    path.write_text('before')
    def write():
        path.write_text('agent')
        return {'status': 'success'}
    result = review.execute('file_write', {'path': str(path)}, tmp_path, write)
    path.write_text('user')
    with pytest.raises(ValueError, match='file changed'):
        review.restore(result['change_id'])
    assert path.read_text() == 'user'
    assert review.changes()[0]['restored'] == 0


def test_failed_write_is_not_reported_as_success_even_if_file_changed(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    path = tmp_path / 'app.py'
    def fail():
        path.write_text('partial')
        raise OSError('write interrupted')
    with pytest.raises(OSError):
        review.execute('file_write', {'path': str(path)}, tmp_path, fail)
    change = review.changes()[0]
    assert change['changed'] and change['tool_status'] == 'exception'
    review.restore(change['id'])
    assert not path.exists()


def test_live_session_cannot_restore(tmp_path):
    from AstralNexus.tui_gateway.entry import Gateway
    gateway = Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    session.running = True
    with pytest.raises(ValueError, match='stop the active run'):
        gateway.dispatch('changes.restore', {'change_id': 'any'})


def test_synchronous_command_changes_can_be_reviewed_and_restored(tmp_path):
    store = WorkbenchStore(tmp_path / 'home')
    review = WorkspaceReview(store, 'session')
    root = tmp_path / 'workspace'
    root.mkdir()
    path = root / 'app.py'
    path.write_text('before')
    (root / 'trace.jsonl').write_text('audit before')
    def command():
        path.write_text('after')
        (root / 'trace.jsonl').write_text('audit after')
        return {'status': 'success'}
    result = review.execute('python_exec', {}, root, command)
    assert result['review_coverage'] == 'workspace' and len(result['change_ids']) == 1
    review.restore(result['change_ids'][0])
    assert path.read_text() == 'before'
    assert (root / 'trace.jsonl').read_text() == 'audit after'


def test_owned_command_snapshot_does_not_attribute_peer_node_changes(tmp_path):
    store = WorkbenchStore(tmp_path / 'home')
    review = WorkspaceReview(store, 'session')
    root = tmp_path / 'workspace'
    root.mkdir()
    def command():
        (root / 'owned.txt').write_text('owned')
        (root / 'peer.txt').write_text('peer')
        return {'status': 'success'}
    result = review.execute('terminal_exec', {}, root, command, ('owned.txt',))
    assert result['review_coverage'] == 'owned_paths'
    assert [Path(change['path']).name for change in review.changes()] == ['owned.txt']


def test_background_command_cannot_claim_complete_review_coverage(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    root = tmp_path / 'workspace'
    root.mkdir()
    result = review.execute('terminal_exec', {}, root, lambda: {'status': 'success', 'background': True})
    assert result['status'] == 'success'
    assert result['review_coverage'] == 'partial_background'
    assert result['review_warning'] and result['change_ids'] == []


def test_unavailable_snapshot_keeps_tool_result_and_warns(tmp_path, monkeypatch):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    def unavailable(*args):
        raise OSError('snapshot unavailable')
    monkeypatch.setattr(review, '_inventory', unavailable)
    result = review.execute('python_exec', {}, tmp_path, lambda: {'status': 'failed', 'returncode': 3})
    assert result['status'] == 'failed' and result['returncode'] == 3
    assert result['review_coverage'] == 'unavailable'
    assert result['review_warning'] == 'snapshot unavailable'


def test_accept_is_version_bound_and_does_not_rewrite_tool_failure(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    path = tmp_path / 'app.py'
    def write():
        path.write_text('new')
        return {'status': 'failed'}
    result = review.execute('file_write', {'path': str(path)}, tmp_path, write)
    accepted = review.accept(result['change_id'])
    assert accepted['acceptance_scope'] == 'user_file_review_only'
    change = review.changes()[0]
    assert change['review_decision'] == 'accepted' and change['tool_status'] == 'failed'
    path.write_text('user')
    with pytest.raises(ValueError, match='file changed'):
        review.accept(result['change_id'])


def test_reject_keeps_history_and_cannot_be_accepted_as_current(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    path = tmp_path / 'new.py'
    def write():
        path.write_text('new')
        return {'status': 'success'}
    result = review.execute('file_write', {'path': str(path)}, tmp_path, write)
    review.restore(result['change_id'])
    assert review.changes()[0]['review_decision'] == 'rejected'
    with pytest.raises(ValueError, match='restored edit'):
        review.accept(result['change_id'])
