from pathlib import Path
import pytest
from AstralNexus.runtime.workbench import WorkbenchStore
from AstralNexus.runtime.workspace_review import WorkspaceReview
from AstralNexus.runtime.user_checkpoint import UserCheckpoints


def write(review, root, name, content):
    path = root / name
    def invoke():
        path.write_text(content)
        return {'status': 'success'}
    return review.execute('file_write', {'path': name}, root, invoke)


def test_checkpoint_restores_captured_versions_and_keeps_original_records(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'a.py').write_text('before')
    checkpoints = UserCheckpoints(review)
    checkpoint = checkpoints.create([{'role': 'user', 'content': 'before conversation'}], 'before')
    write(review, root, 'a.py', 'first')
    write(review, root, 'a.py', 'second')
    write(review, root, 'new.py', 'new')
    (root / 'unobserved.txt').write_text('external')
    restored = UserCheckpoints(review).restore(checkpoint['id'])
    assert (root / 'a.py').read_text() == 'before' and not (root / 'new.py').exists()
    assert (root / 'unobserved.txt').read_text() == 'external'
    assert restored['history'][0]['content'] == 'before conversation'
    assert len(review.changes()) == 3 and all(row['restored'] for row in review.changes())
    assert checkpoints.restore(checkpoint['id'])['already_restored']


def test_one_foreign_edit_refuses_entire_checkpoint_before_mutating_files(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    checkpoint = UserCheckpoints(review).create([])
    write(review, tmp_path, 'a.py', 'agent-a')
    write(review, tmp_path, 'b.py', 'agent-b')
    (tmp_path / 'a.py').write_text('user-a')
    with pytest.raises(ValueError, match='whole checkpoint'):
        UserCheckpoints(review).restore(checkpoint['id'])
    assert (tmp_path / 'a.py').read_text() == 'user-a'
    assert (tmp_path / 'b.py').read_text() == 'agent-b'
    assert all(not row['restored'] for row in review.changes())


def test_unobserved_intermediate_change_breaks_chain(tmp_path):
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    checkpoints = UserCheckpoints(review)
    checkpoint = checkpoints.create([])
    write(review, tmp_path, 'a.py', 'first')
    (tmp_path / 'a.py').write_text('user')
    write(review, tmp_path, 'a.py', 'second')
    with pytest.raises(ValueError, match='version chain'):
        checkpoints.restore(checkpoint['id'])
    assert (tmp_path / 'a.py').read_text() == 'second'


def test_failed_second_write_compensates_first_and_preserves_failure(tmp_path, monkeypatch):
    from AstralNexus.runtime import user_checkpoint
    review = WorkspaceReview(WorkbenchStore(tmp_path / 'home'), 'session')
    checkpoints = UserCheckpoints(review)
    checkpoint = checkpoints.create([])
    write(review, tmp_path, 'a.py', 'a')
    write(review, tmp_path, 'b.py', 'b')
    original = user_checkpoint.write_version
    def fail(path, content):
        if path.name == 'a.py': raise OSError('injected restore write failure')
        original(path, content)
    monkeypatch.setattr(user_checkpoint, 'write_version', fail)
    with pytest.raises(OSError, match='injected'):
        checkpoints.restore(checkpoint['id'])
    assert (tmp_path / 'a.py').read_text() == 'a' and (tmp_path / 'b.py').read_text() == 'b'
    assert not checkpoints.list()[0]['restored']
    assert all(not row['restored'] for row in review.changes())


def test_gateway_checkpoint_forks_conversation_and_does_not_rewrite_run(tmp_path, monkeypatch):
    from AstralNexus.tui_gateway import entry
    monkeypatch.setattr(entry, '_event', lambda *args, **kwargs: None)
    gateway = entry.Gateway(home=tmp_path / 'home')
    session = gateway.create_session()
    session.history = [{'role': 'user', 'content': 'before'}]
    gateway.dispatch('models.set', {'session_id': session.session_id, 'main_model': 'parent-main', 'sub_model': 'parent-sub'})
    checkpoint = gateway.dispatch('checkpoints.create', {'session_id': session.session_id})
    session.history.append({'role': 'assistant', 'content': 'later'})
    write(session.review, tmp_path, 'new.py', 'new')
    session.run_dir = 'original-run'
    restored = gateway.dispatch('checkpoints.restore', {'session_id': session.session_id, 'checkpoint_id': checkpoint['id']})
    fork = gateway.sessions[restored['session_id']]
    assert fork.history == [{'role': 'user', 'content': 'before'}] and fork.run_dir == ''
    assert gateway.dispatch('models.get', {'session_id': fork.session_id})['effective_sub'] == 'parent-sub'
    assert session.history[-1]['content'] == 'later' and session.run_dir == 'original-run'
    assert not (tmp_path / 'new.py').exists() and len(session.review.changes()) == 1
    again = gateway.dispatch('checkpoints.restore', {'session_id': session.session_id, 'checkpoint_id': checkpoint['id']})
    assert again['session_id'] == fork.session_id
    assert gateway.dispatch('session.resume', {'session_id': fork.session_id})['messages'][0]['text'] == 'before'


def test_cross_session_and_live_run_cannot_restore(tmp_path, monkeypatch):
    from AstralNexus.tui_gateway import entry
    monkeypatch.setattr(entry, '_event', lambda *args, **kwargs: None)
    gateway = entry.Gateway(home=tmp_path / 'home')
    owner = gateway.create_session()
    checkpoint = gateway.dispatch('checkpoints.create', {'session_id': owner.session_id})
    peer = gateway.create_session()
    with pytest.raises(ValueError, match='unknown checkpoint'):
        gateway.dispatch('checkpoints.restore', {'session_id': peer.session_id, 'checkpoint_id': checkpoint['id']})
    owner.running = True
    with pytest.raises(ValueError, match='stop the active run'):
        gateway.dispatch('checkpoints.restore', {'session_id': owner.session_id, 'checkpoint_id': checkpoint['id']})
    assert not UserCheckpoints(owner.review).list()[0]['restored']
