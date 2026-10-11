import threading

from AstralNexus.core.orchestrator import Orchestrator


def test_finished_worker_processed_before_slow_peer_completes(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    with Orchestrator(tmp_path, run_id="independent-results", max_parallel=2) as orchestrator:
        monkeypatch.setattr(orchestrator.adaptive_concurrency, "choose", lambda *a, **kw: 2)
        def execute(node_id):
            if node_id == "slow":
                started.set()
                assert release.wait(3), "fast result was held behind the worker wave"
                return True
            assert started.wait(3)
            return False
        monkeypatch.setattr(orchestrator, "execute_task", execute)
        handled = []
        def process(node_id, success):
            handled.append((node_id, success))
            if node_id == "fast":
                release.set()
        results = orchestrator._execute_wave(["slow", "fast"], on_result=process)
        assert handled == [("fast", False), ("slow", True)]
        assert results == {"fast": False, "slow": True}
