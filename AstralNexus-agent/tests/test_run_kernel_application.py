from __future__ import annotations
from AstralNexus.tests.publication_support import authorize_files
import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.run_kernel import RunKernel
from AstralNexus.core.run_application import RunApplicationService
from AstralNexus.core.delivery import DeliveryManager
from AstralNexus.core.delivery_ports import adapt_delivery_runtime


class Engine:
    def __init__(self, calls, entered=None, release=None):
        calls.append(threading.get_ident())
        self.calls = calls
        self.entered, self.release = entered, release
        self.task_graph = TaskGraph()
        self.checkpoint_sink = None
        self.governor = SimpleNamespace(apply_task_graph_patch=self.patch)
    def attach_interrupt_control(self, event, queue): self.interrupt = event
    def run(self, **kwargs):
        self.calls.append(threading.get_ident())
        if self.entered is not None:
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(3)
            else:
                assert self.interrupt.wait(3)
        return {"status": "interrupted" if self.interrupt.is_set() else "completed"}
    def patch(self, patch):
        self.calls.append(threading.get_ident())
        for item in patch["add_nodes"]:
            self.task_graph.add_node(item)
        return {"added": [item["node_id"] for item in patch["add_nodes"]]}
    def _persist_graph_checkpoint(self, **kwargs):
        if self.checkpoint_sink: self.checkpoint_sink({})
        return True
    def shutdown(self): self.calls.append(threading.get_ident())


def test_commands_and_initialization_share_one_owner_and_snapshot_is_readonly(tmp_path):
    calls = []
    with RunKernel(tmp_path, engine_factory=lambda: Engine(calls)) as kernel:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = list(pool.map(lambda index: kernel.apply_patch({"add_nodes": [{
                "node_id": str(index), "goal": "task", "role": "coder", "metadata": {"nested": [index]},
            }]}), range(12)))
        for future in futures: future.result(timeout=3)
        snapshot = kernel.snapshot()
        assert len(snapshot["nodes"]) == 12
        with pytest.raises(TypeError): snapshot["nodes"] = ()
        with pytest.raises(TypeError): snapshot["nodes"][0]["metadata"]["nested"] = ()
        with pytest.raises(TypeError): futures[0].result()["added"] = ()
    assert len(set(calls)) == 1 and calls[0] != threading.get_ident()


def test_admitted_patch_cannot_be_mutated_by_caller_before_owner_executes(tmp_path):
    entered, release = threading.Event(), threading.Event()
    with RunKernel(tmp_path, engine_factory=lambda: Engine([], entered, release)) as kernel:
        execution = kernel.execute(goal="wait")
        assert entered.wait(2)
        assert not kernel.snapshot()["nodes"]  # Nonblocking committed projection.
        patch = {"add_nodes": [{"node_id": "submitted", "goal": "task", "role": "coder"}]}
        applied = kernel.apply_patch(patch)
        patch["add_nodes"][0]["node_id"] = "tampered"
        release.set()
        execution.result(timeout=3)
        assert applied.result(timeout=3)["added"] == ("submitted",)


def test_cancel_does_not_wait_behind_long_execution(tmp_path):
    entered = threading.Event()
    with RunKernel(tmp_path, engine_factory=lambda: Engine([], entered)) as kernel:
        future = kernel.execute(goal="wait")
        assert entered.wait(2)
        kernel.cancel()
        assert future.result(timeout=3)["status"] == "interrupted"
    with pytest.raises(RuntimeError, match="closed"): kernel.checkpoint()


def test_failed_factory_does_not_return_half_initialized_kernel(tmp_path):
    def fail(): raise OSError("storage unavailable")
    with pytest.raises(OSError, match="storage unavailable"):
        RunKernel(tmp_path, engine_factory=fail)


def test_delivery_projection_is_detached_from_engine_nodes():
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="node", goal="task", role="coder", metadata={"items": [1]}))
    projection = adapt_delivery_runtime(SimpleNamespace(task_graph=graph)).task_graph
    graph.nodes["node"].metadata["items"].append(2)
    assert projection.nodes["node"].metadata["items"] == (1,)
    with pytest.raises(TypeError): projection.nodes["other"] = projection.nodes["node"]


class Publication:
    def __init__(self, root, gate=None):
        self.root = root
        self.gate = gate or {"delivery_authorized": True, "overall_status": "pass", "failed_tests": [], "unknown_tests": []}
        self.calls = 0
    def execute(self, **kwargs):
        self.calls += 1
        manager = DeliveryManager(self.root)
        draft = manager.begin()
        for name, content in [("delivery_summary.json", {"delivery_gate": self.gate}), ("delivery_verification.json", self.gate)]:
            (draft / name).write_text(json.dumps(content), encoding="utf-8")
        (draft / "delivery_response.md").write_text("result", encoding="utf-8")
        manifest = manager.publish(authorized=authorize_files(manager))
        version = self.root / "deliverables" / f"v{manifest['version']}"
        return {"summary_json": str(version / "delivery_summary.json"), "main_markdown": str(version / "delivery_response.md")}


@pytest.mark.parametrize("status", ["failed", "blocked", "interrupted"])
def test_application_never_synthesizes_incomplete_execution(tmp_path, status):
    pipeline = Publication(tmp_path)
    app = RunApplicationService(SimpleNamespace(run=lambda **kwargs: {"status": status}), pipeline=pipeline)
    outcome = app.execute(task_content="task", run_dir=tmp_path)
    assert outcome["status"] == status and not outcome["delivery_authorized"]
    assert pipeline.calls == 0


def test_library_entry_requires_real_gate_and_publication(tmp_path):
    app = RunApplicationService(SimpleNamespace(run=lambda **kwargs: {"status": "completed"}), pipeline=Publication(tmp_path))
    outcome = app.execute(task_content="task", run_dir=tmp_path)
    assert outcome["status"] == "completed" and outcome["delivery_status"] == "passed"
    assert outcome["publication"]["published"]


@pytest.mark.parametrize("kind", ["limited", "tampered", "other_run", "failed_report"])
def test_node_completion_cannot_override_delivery_failures(tmp_path, kind):
    root = tmp_path / "run"
    gate = {"delivery_authorized": True, "overall_status": "pass", "unknown_tests": ["pending"]} if kind == "limited" else None
    publication = Publication(tmp_path / "other" if kind == "other_run" else root, gate)
    def execute(**kwargs):
        artifacts = publication.execute(**kwargs)
        if kind == "tampered": Path(artifacts["main_markdown"]).write_text("changed", encoding="utf-8")
        if kind == "failed_report":
            failure = root / "failure.json"
            failure.write_text('{"status":"failed"}', encoding="utf-8")
            return {"summary_json": str(failure)}
        return artifacts
    app = RunApplicationService(SimpleNamespace(run=lambda **kwargs: {"status": "completed"}), pipeline=SimpleNamespace(execute=execute))
    outcome = app.execute(task_content="task", run_dir=root)
    assert outcome["status"] == "delivery_failed"
    assert outcome["delivery_status"] == "failed"  # Current publisher never promotes an unknown acceptance verdict.


def test_application_delivery_has_no_cli_dependency():
    root = Path(__file__).resolve().parents[1] / "core"
    for name in ("delivery_pipeline.py",  "delivery_ports.py", "run_application.py", "run_kernel.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and ".cli" in (node.module or "")]


def test_managed_kernel_excludes_second_owner_and_commits_with_real_fence(tmp_path):
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    run = tmp_path / "managed"
    with RunKernel(run) as kernel:
        with pytest.raises(RuntimeError, match="ownership rejected"):
            RunKernel(run)
        applied = kernel.apply_patch({"add_nodes": [{
            "node_id": "node", "goal": "write result", "role": "coder",
            "owned_paths": ["deliverables/result.txt"], "outputs": ["deliverables/result.txt"],
            "acceptance_criteria": ["result exists"],
        }]}).result(timeout=5)
        assert applied["added"] == ("node",)
        fence = SQLiteTaskGraphStore(run / "task_graph.sqlite").latest_commit_fence()
        assert fence is not None and fence["generation"] == 1 and fence["commit_seq"] >= 1
    assert RunCoordinator(run, run_id=run.name).current()["status"] == "paused"
    with pytest.raises(RuntimeError, match="ownership rejected"):
        RunKernel(run)
    with RunKernel(run, user_resume=True) as resumed:
        assert resumed.snapshot()["nodes"][0]["node_id"] == "node"


def test_managed_startup_failure_records_actual_error(tmp_path, monkeypatch):
    import time
    from AstralNexus.core import orchestrator
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    def fail(*args, **kwargs):
        raise OSError("storage unavailable")
    monkeypatch.setattr(orchestrator, "Orchestrator", fail)
    with pytest.raises(OSError, match="storage unavailable"):
        RunKernel(tmp_path)
    coordinator = RunCoordinator(tmp_path, run_id=tmp_path.name)
    deadline = time.monotonic() + 3
    while coordinator.current()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    row = coordinator.current()
    assert row["status"] == "blocked"
    assert row["stop_code"] == "host_exception"
    assert row["stop_reason"] == "OSError: storage unavailable"


@pytest.mark.parametrize("operation", ["execute", "patch"])
def test_command_failure_cannot_reuse_previous_delivery_pass(tmp_path, monkeypatch, operation):
    from AstralNexus.core import orchestrator
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    class ManagedEngine(Engine):
        def __init__(self, *args, **kwargs):
            super().__init__([])
            self.checkpoint_sink = kwargs["checkpoint_sink"]
        def run(self, **kwargs):
            raise ValueError("new command failed")
        def patch(self, patch):
            raise ValueError("new command failed")
    monkeypatch.setattr(orchestrator, "Orchestrator", ManagedEngine)
    monkeypatch.setattr(RunApplicationService, "execute", lambda *args, **kwargs:
        {"status": "completed", "delivery_status": "passed"})
    with RunKernel(tmp_path) as kernel:
        assert kernel.run_complete(task_content="first").result(timeout=3)["delivery_status"] == "passed"
        future = kernel.execute(goal="second") if operation == "execute" else kernel.apply_patch({})
        with pytest.raises(ValueError, match="new command failed"):
            future.result(timeout=3)
    row = RunCoordinator(tmp_path, run_id=tmp_path.name).current()
    assert row["status"] == "blocked" and row["stop_code"] == "host_exception"


def test_repeated_close_preserves_cleanup_failure(tmp_path):
    class BrokenCleanup(Engine):
        def shutdown(self): raise OSError("cleanup failed")
    kernel = RunKernel(tmp_path, engine_factory=lambda: BrokenCleanup([]))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="resource cleanup failed"):
            kernel.close()


def test_application_execution_receives_complete_task_not_only_url_first_line(tmp_path):
    content = 'https://example.invalid/task\nImplement backend and frontend.\nRun the declared API and browser checks.'
    received = []
    def run(**kwargs):
        received.append(kwargs['goal'])
        return {'status': 'blocked'}
    pipeline = Publication(tmp_path)
    outcome = RunApplicationService(SimpleNamespace(run=run), pipeline=pipeline).execute(task_content=content, run_dir=tmp_path)
    assert received == [content] and outcome['status'] == 'blocked'
    assert pipeline.calls == 0




def test_entry_bridge_preserves_context_and_executes_on_single_owner(tmp_path):
    from contextvars import ContextVar
    marker = ContextVar('entry-context', default='missing')
    marker.set('captured')
    calls = []
    caller = threading.get_ident()
    with RunKernel(tmp_path, engine_factory=lambda: Engine(calls)) as kernel:
        result = kernel._run_entry(lambda engine: {'thread': threading.get_ident(), 'context': marker.get()}).result(timeout=5)
    assert result['context'] == 'captured' and result['thread'] != caller
    assert len(set(calls + [result['thread']])) == 1



def test_application_classifies_planning_rejection_without_delivery(tmp_path):
    from AstralNexus.core.exceptions import PlanningRejectedError
    def reject(**kwargs):
        raise PlanningRejectedError("invalid artifact reference")
    pipeline = Publication(tmp_path)
    outcome = RunApplicationService(SimpleNamespace(run=reject), pipeline=pipeline).execute(task_content="task", run_dir=tmp_path)
    assert outcome["status"] == "failed"
    assert outcome["stop_code"] == "planning_failed"
    assert pipeline.calls == 0
