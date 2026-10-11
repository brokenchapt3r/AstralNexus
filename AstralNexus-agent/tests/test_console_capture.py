import io
import json
import threading
import sys
from AstralNexus.runtime.console_capture import capture_console
from AstralNexus.tui_gateway import entry


def test_parallel_capture_cannot_swallow_protocol_or_peer_output(monkeypatch):
    transport = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', transport)
    ready = threading.Barrier(3)
    release = threading.Event()
    captures = [io.StringIO(), io.StringIO()]
    def command(index):
        with capture_console(captures[index], io.StringIO()):
            print('command-' + str(index))
            ready.wait(timeout=3)
            assert release.wait(timeout=3)
            entry._result(index, {'ok': True})
    threads = [threading.Thread(target=command, args=(index,)) for index in range(2)]
    for thread in threads: thread.start()
    ready.wait(timeout=3)
    print('outside-console')
    entry._result('main', {'ok': True})
    release.set()
    for thread in threads: thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert [capture.getvalue() for capture in captures] == ['command-0\n', 'command-1\n']
    assert sys.stdout is transport
    lines = transport.getvalue().splitlines()
    assert lines[0] == 'outside-console'
    assert {json.loads(line)['id'] for line in lines[1:]} == {'main', 0, 1}


def test_nested_capture_and_exception_restore_stream(monkeypatch):
    transport = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', transport)
    outer, inner = io.StringIO(), io.StringIO()
    with capture_console(outer, io.StringIO()):
        print('outer')
        try:
            with capture_console(inner, io.StringIO()):
                print('inner')
                raise ValueError('failed command')
        except ValueError:
            print('after')
    assert outer.getvalue() == 'outer\nafter\n'
    assert inner.getvalue() == 'inner\n'
    assert sys.stdout is transport
