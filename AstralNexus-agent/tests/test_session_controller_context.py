import threading
from contextvars import ContextVar
from AstralNexus.cli.session_controller import _InteractiveRunController


def test_background_run_captures_launch_callback_and_context(tmp_path):
    controller = _InteractiveRunController(tmp_path)
    release = threading.Event()
    calls, other = [], []
    context: ContextVar[str] = ContextVar('proof', default='outside')
    controller.set_event_callback(lambda event: calls.append(event))
    def target(args):
        assert release.wait(3)
        controller._emit('proof', context.get())
    token = context.set('captured')
    try:
        assert controller._launch(target, None, event_type='started', text='start', thread_name='test')
    finally:
        context.reset(token)
    controller.set_event_callback(lambda event: other.append(event))
    release.set()
    assert controller.thread is not None
    controller.thread.join(3)
    assert not controller.thread.is_alive()
    assert any(event['event_type'] == 'proof' and event['text'] == 'captured' for event in calls)
    assert not other
