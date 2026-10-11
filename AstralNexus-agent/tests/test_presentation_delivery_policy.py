import json
from pathlib import Path
import pytest
from AstralNexus.runtime.presentation import Presentation, delivery_receipt, output_inventory, presentation_scope
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.goal_evaluator import GoalState


@pytest.mark.parametrize("before,after,presented,expected", [
    ({},{},[],True),
    ({},{"outputs/a": ("new",1)},[],False),
    ({"outputs/old": ("old",1)}, {"outputs/old": ("old",1),"outputs/new": ("new",1)}, ["outputs/old"],False),
    ({},{"outputs/a": ("new",1),"outputs/b": ("other",1)},["outputs/a"],True),
    ({},{"outputs/app/a": ("new",1)},["outputs/app"],True),
])
def test_default_deerflow_delivery_shapes(before,after,presented,expected):
    assert delivery_receipt(before,after,presented)["satisfied"] is expected


def test_lead_tool_records_real_paths_and_workers_cannot_present(tmp_path):
    from AstralNexus.tools.executor import ToolExecutor
    from AstralNexus.tools.registry import discover_builtin_tools
    discover_builtin_tools()
    outputs=tmp_path/"outputs";outputs.mkdir();(outputs/"a.txt").write_text("new")
    assert ToolExecutor(workspace_root=tmp_path).execute_tool("present_files",{"paths":["outputs/a.txt"]})["status"]=="error"
    context=Presentation(tmp_path,outputs)
    with presentation_scope(context):
        result=ToolExecutor(workspace_root=tmp_path).execute_tool("present_files",{"paths":["/mnt/user-data/outputs/a.txt"]})
    assert result["status"]=="success"
    assert context.paths==["outputs/a.txt"]
    with pytest.raises(ValueError): context.present(["../secret"])


def test_receipt_has_one_idempotent_terminal_fact(tmp_path):
    store=ExecutionStore(tmp_path);receipt=delivery_receipt({}, {"outputs/a":("new",1)},["outputs/a"])
    assert store.record_delivery("R",receipt)==store.record_delivery("R",receipt)
    with pytest.raises(ValueError): store.record_delivery("R",{**receipt,"satisfied":False})


def test_goal_only_continues_recoverable_unmet_condition_and_has_no_progress_cap():
    state=GoalState("finish")
    evidence={"outputs":{},"tools":[]}
    verdict={"satisfied":False,"blocker":"goal_not_met_yet","relied_on_assumption":False}
    assert state.decide(verdict,evidence)=="continue"
    assert state.decide(verdict,{**evidence,"assistant":"different wording"})=="continue"
    assert state.decide(verdict,evidence)=="goal_no_progress"
    for blocker in ("needs_user_input","run_failed","external_wait","missing_evidence"):
        assert GoalState("finish").decide({**verdict,"blocker":blocker},evidence)==blocker
    assert GoalState("finish",continuations=8).decide(verdict,evidence)=="goal_continuation_limit"
    satisfied=GoalState("finish")
    assert satisfied.decide({"satisfied":True,"relied_on_assumption":False},evidence)=="satisfied"
    assert satisfied.active  # Delivery/terminal persistence must finish first.


class _Runtime:
    run_id = "R"
    def __init__(self): self.events=[]
    @property
    def trace_recorder(self): return self
    def record(self,name,payload): self.events.append((name,payload))
    def record_cost(self,*args): pass
    def owns_runtime(self): return True
    def publication_fence(self): return None
    def cancel_requested(self): return False


class _PresentClient:
    def __init__(self, present=True): self.present=present
    def stream_chat(self, messages, **kwargs):
        from AstralNexus.tools.executor import ToolExecutor
        assert {schema["function"]["name"] for schema in kwargs["tools"]} == {"present_files"}
        if self.present:
            outcome=ToolExecutor(workspace_root=kwargs["workspace_root"],allowed_tools={"present_files"}).execute_tool("present_files",{"paths":["outputs/app/main.py"]})
            assert outcome["status"]=="success",outcome
        return "One file was presented."
    def chat(self,*args,**kwargs):
        raise AssertionError("Default delivery must not call a semantic judge")


def test_direct_goal_guard_handoff_is_blocked_not_host_exception(tmp_path):
    from types import SimpleNamespace
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.run_application import RunApplicationService
    from AstralNexus.runtime.llm_client import RuntimeNoProgressError
    states = []
    calls = []
    class StalledClient:
        def stream_chat(self, messages, **kwargs):
            calls.append(messages)
            (tmp_path / "outputs" / "partial.txt").write_text("preserved evidence")
            raise RuntimeNoProgressError({"action": "handoff", "cycle_id": "same-cycle"})
    pipeline = FinalDeliveryPipeline(_Runtime())
    pipeline.client = StalledClient()
    engine = SimpleNamespace(record_application_state=lambda state: states.append(state) or True)
    result = RunApplicationService(engine, pipeline=pipeline).execute(
        task_content="/goal repair project", run_dir=tmp_path)
    assert result["status"] == "blocked"
    assert result["execution_status"] == "blocked"
    assert result["stop_code"] == "no_progress_breaker"
    assert result["delivery_verdict"]["guard"]["cycle_id"] == "same-cycle"
    assert states[-1]["run_status"] == "blocked"
    assert states[-1]["goal_state"]["active"] is True
    assert len(calls) == 1
    assert (tmp_path / "outputs" / "partial.txt").read_text() == "preserved evidence"
    assert not (tmp_path / "deliverables" / "current.json").exists()
    assert ExecutionStore(tmp_path).read_delivery("R")["satisfied"] is False


def test_real_pipeline_presents_one_of_multiple_outputs_and_publishes_exact_bytes(tmp_path):
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.delivery_ports import adapt_delivery_runtime
    from AstralNexus.core.publication_validation import inspect_publication
    runtime=_Runtime();pipeline=FinalDeliveryPipeline(runtime);pipeline.client=_PresentClient()
    pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True)
    (app/"main.py").write_text("print(5)")
    (app/"README.md").write_text("Readme")
    artifacts=pipeline.execute(task_content="deliver files",run_dir=tmp_path)
    assert pipeline.receipt["matched_paths"]==["outputs/app/main.py"]
    assert len(pipeline.receipt["produced_paths"])==2
    assert inspect_publication(tmp_path,artifacts)["complete"]
    assert len([event for event in runtime.events if event[0]=="run.delivery"])==1
    assert ExecutionStore(tmp_path).read_delivery("R")["satisfied"]


def test_missing_presentation_is_failed_with_durable_receipt_and_no_publication(tmp_path):
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.delivery_snapshot import DeliveryRejected
    pipeline=FinalDeliveryPipeline(_Runtime());pipeline.client=_PresentClient(present=False);pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True);(app/"main.py").write_text("pass")
    with pytest.raises(DeliveryRejected,match="artifact_delivery_incomplete"):
        pipeline.execute(task_content="deliver",run_dir=tmp_path)
    assert ExecutionStore(tmp_path).read_delivery("R")["satisfied"] is False
    assert not (tmp_path/"deliverables/current.json").exists()


def test_receipt_persistence_failure_prevents_publication(tmp_path,monkeypatch):
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.delivery_snapshot import DeliveryRejected
    pipeline=FinalDeliveryPipeline(_Runtime());pipeline.client=_PresentClient();pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True);(app/"main.py").write_text("pass")
    def fail(*args,**kwargs): raise OSError("journal unavailable")
    monkeypatch.setattr(ExecutionStore,"record_delivery",fail)
    with pytest.raises(DeliveryRejected,match="delivery_receipt_persistence_failed"):
        pipeline.execute(task_content="deliver",run_dir=tmp_path)
    assert not (tmp_path/"deliverables/current.json").exists()


def test_ordinary_no_output_turn_succeeds_without_model_or_present_tool(tmp_path):
    from types import SimpleNamespace
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.run_application import RunApplicationService
    pipeline=FinalDeliveryPipeline(_Runtime())
    outcome=RunApplicationService(SimpleNamespace(run=lambda **kwargs:{"status":"completed"}),pipeline=pipeline).execute(task_content="hello",run_dir=tmp_path)
    assert outcome["status"]=="completed" and outcome["publication_status"]=="not_published"
    assert pipeline.client is None
    assert ExecutionStore(tmp_path).read_delivery("R")["stage"]=="not_required"


def test_delivery_lead_cannot_execute_unadvertised_product_write(tmp_path):
    from AstralNexus.tools.executor import ToolExecutor
    executor=ToolExecutor(workspace_root=tmp_path,allowed_tools={"present_files"})
    result=executor.execute_tool("file_write",{"path":"main.py","content":"changed"})
    assert result["executed"] is False and not (tmp_path/"main.py").exists()


def test_authoritative_product_status_waits_for_receipt(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.runtime_state import read_authoritative_cursor
    from AstralNexus.core.run_application import RunApplicationService
    with Orchestrator(tmp_path,run_id="R") as engine:
        pipeline=FinalDeliveryPipelineForTest(_Runtime())
        outcome=RunApplicationService(engine,pipeline=pipeline).complete(execution={"status":"completed"},task_content="hello",run_dir=tmp_path)
        state=read_authoritative_cursor(tmp_path,run_id="R")["cursor"]
        assert state["execution_status"]=="completed"
        assert state["delivery_status"]=="passed"
        assert state["publication_status"]=="not_published"
        assert state["run_status"]=="completed"
        assert state["delivery_receipt"]["satisfied"]


from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline as FinalDeliveryPipelineForTest


def test_explicit_goal_command_creates_goal_without_model_route_or_implicit_activation(tmp_path,monkeypatch):
    from AstralNexus.cli.session_controller import _InteractiveRunController
    from AstralNexus.cli.commands import resolve_command
    controller=_InteractiveRunController(tmp_path);requests=[]
    monkeypatch.setattr(controller,"_launch",lambda target,args,**kwargs: requests.append(args) or True)
    assert resolve_command("/goal")=="goal"
    assert controller.dispatch_command("/goal make the result visible")["ok"]
    assert requests[0].goal=="make the result visible"
    controller.start_text("ordinary task")
    assert requests[1].goal is None


def test_output_named_report_is_a_real_output_not_framework_metadata(tmp_path):
    root=tmp_path/"outputs";root.mkdir();(root/"acceptance_report.md").write_text("user artifact")
    assert "outputs/acceptance_report.md" in output_inventory(root)


def test_default_node_criteria_are_advisory_not_another_business_gate(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path,run_id="R") as engine:
        node=TaskNode("N","ordinary task","coder",acceptance_criteria=["file:missing.py exists"],
                      metadata={"evidence_packet":{"execution_outcome":"succeeded","summary":"claims success"}})
        engine.task_graph.add_node(node)
        assert engine.verifier.business_acceptance_checks is False
        verdict=engine.verifier.verify_node("N")
        assert verdict.verdict.value == "approved"
        assert any(item["reason"]=="FILE_NOT_FOUND" for dim in verdict.dimensions for item in dim.observations)


def test_conversation_file_output_uses_scoped_outputs_and_requires_present(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from AstralNexus.runtime.workbench_chat_worker import execute_turn,ChatOutput
    from AstralNexus.tools.executor import ToolExecutor
    from AstralNexus.tools.registry import discover_builtin_tools
    import threading
    discover_builtin_tools()
    monkeypatch.setenv("ASTRALNEXUS_ALLOW_FILE_WRITE", "true")
    workspace=tmp_path/"workspace";workspace.mkdir()
    monkeypatch.setenv("ASTRALNEXUS_WORKSPACE",str(workspace))
    events=[];output=ChatOutput(lambda kind,payload:events.append((kind,payload)),threading.Event())
    controller=SimpleNamespace(home=tmp_path,status=lambda:{"running":False,"run_dir":None,"last_result":None})
    def chat(message,history,**kwargs):
        executor=ToolExecutor(workspace_root=workspace)
        assert executor.execute_tool("file_write",{"path":"outputs/result.txt","content":"new"})["status"]=="success"
        assert executor.execute_tool("present_files",{"paths":["outputs/result.txt"]})["status"]=="success"
        history.append({"role":"assistant","content":"done"})
    status=execute_turn({"home":str(tmp_path),"message":"write","history":[]},controller,output,lambda *args,**kwargs:False,chat)
    assert status=="completed"
    assert (tmp_path/"outputs/result.txt").read_text()=="new"
    assert any(kind=="run.delivery" and payload["satisfied"] for kind,payload in events)


@pytest.mark.parametrize("blocker",["needs_user_input","external_wait","missing_evidence"])
def test_explicit_goal_stops_without_clearing_goal_but_keeps_delivery_fact(tmp_path,blocker):
    from types import SimpleNamespace
    from AstralNexus.core.run_application import RunApplicationService
    class Client(_PresentClient):
        def chat(self,*args,**kwargs):
            return json.dumps({"satisfied":False,"blocker":blocker,"reason":"waiting","evidence_summary":"partial","relied_on_assumption":False})
    pipeline=FinalDeliveryPipelineForTest(_Runtime());pipeline.client=Client();pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True);(app/"main.py").write_text("pass")
    outcome=RunApplicationService(SimpleNamespace(),pipeline=pipeline).complete(execution={"status":"completed"},task_content="task",run_dir=tmp_path,goal="must finish")
    assert outcome["status"]=="blocked" and pipeline.goal_state.active
    assert outcome["delivery_status"]=="passed"
    assert ExecutionStore(tmp_path).read_delivery("R")["satisfied"]
    assert not (tmp_path/"deliverables/current.json").exists()


def test_goal_clears_only_after_delivery_publication_and_terminal_commit(tmp_path):
    from types import SimpleNamespace
    from AstralNexus.core.run_application import RunApplicationService
    class Client(_PresentClient):
        def chat(self,*args,**kwargs):
            return json.dumps({"satisfied":True,"blocker":"","reason":"observed","evidence_summary":"presented","relied_on_assumption":False})
    pipeline=FinalDeliveryPipelineForTest(_Runtime());pipeline.client=Client();pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True);(app/"main.py").write_text("pass")
    stages=[]
    def persist(payload):
        if payload.get("run_status")=="completed":
            assert ExecutionStore(tmp_path).read_delivery("R")["satisfied"]
            assert (tmp_path/"deliverables/current.json").exists()
        stages.append(payload)
        return True
    outcome=RunApplicationService(SimpleNamespace(record_application_state=persist),pipeline=pipeline).complete(
        execution={"status":"completed"},task_content="task",run_dir=tmp_path,goal="finish")
    assert outcome["status"]=="completed" and not pipeline.goal_state.active
    assert stages[-1]["goal_state"]["active"] is False


def test_cancellation_never_clears_goal_or_publishes(tmp_path):
    from AstralNexus.core.exceptions import InterruptionRequested
    runtime=_Runtime();cancel=[False];runtime.cancel_requested=lambda:cancel[0]
    class Client(_PresentClient):
        def chat(self,*args,**kwargs):
            cancel[0]=True
            return json.dumps({"satisfied":True,"relied_on_assumption":False})
    pipeline=FinalDeliveryPipelineForTest(runtime);pipeline.client=Client();pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True);(app/"main.py").write_text("pass")
    with pytest.raises(InterruptionRequested): pipeline.execute(task_content="task",run_dir=tmp_path,goal="finish")
    assert pipeline.goal_state.active
    assert not (tmp_path/"deliverables/current.json").exists()


@pytest.mark.parametrize("lease_state", ["current", "expired", "missing"])
def test_delivery_keeps_managed_execution_fence_after_worker_wave(tmp_path, lease_state):
    from datetime import datetime, timedelta, timezone
    from AstralNexus.runtime.run_coordinator import RunCoordinator
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    from AstralNexus.core.delivery_snapshot import DeliveryRejected
    from AstralNexus.core.delivery_ports import adapt_delivery_runtime
    from types import SimpleNamespace
    now = [datetime.now(timezone.utc)]
    coordinator = RunCoordinator(tmp_path, run_id="managed", now_fn=lambda: now[0])
    lease = coordinator.acquire("lead")
    assert lease is not None
    provider = lambda: coordinator.fence_token(lease)
    source = SimpleNamespace(run_id="managed", fence_token_provider=provider if lease_state != "missing" else None,
                             trace_recorder=_Runtime(), _ownership_ok=lambda: True)
    runtime = adapt_delivery_runtime(source)
    pipeline = FinalDeliveryPipeline(runtime)
    pipeline.client = _PresentClient()
    pipeline.begin(tmp_path)
    app = tmp_path / "deliverables/app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("print(5)")
    if lease_state == "expired":
        now[0] += timedelta(seconds=1000)
        assert coordinator.acquire("new-lead") is not None
    if lease_state == "current":
        pipeline.execute(task_content="present", run_dir=tmp_path)
        assert ExecutionStore(tmp_path).read_delivery("managed")["satisfied"]
        assert (tmp_path / "deliverables/current.json").exists()
    else:
        with pytest.raises(DeliveryRejected, match="delivery"):
            pipeline.execute(task_content="present", run_dir=tmp_path)
        assert ExecutionStore(tmp_path).read_delivery("managed") is None
        assert not (tmp_path / "deliverables/current.json").exists()


def test_default_execution_failure_still_blocks_completion(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.task_graph import TaskNode
    with Orchestrator(tmp_path,run_id="R") as engine:
        node=TaskNode("N","task","coder",metadata={"evidence_packet":{"execution_outcome":"failed","summary":"failed execution"}})
        engine.task_graph.add_node(node)
        verdict=engine.verifier.verify_node("N")
        assert verdict.verdict.value != "approved"
        assert next(d for d in verdict.dimensions if d.name=="execution_success").score == 0


def test_execution_acceptance_does_not_reintroduce_business_gate(tmp_path):
    store=ExecutionStore(tmp_path)
    execution=store.begin("node", {"owned_paths":[],"acceptance_criteria":["tests_passed:python never_executed.py"]})
    store.finish(execution,"succeeded",{"content":"file work completed"})
    store.accept(execution,{"approved":True,"acceptance_scope":"execution"})
    decision=store.get(execution)["acceptance"]
    assert decision["approved"] and decision["business_accepted"] is False
    assert decision["criterion_diagnostics"][0]["reason"]=="TEST_EXECUTION_NOT_FOUND"
    with pytest.raises(ValueError,match="not anchored"):
        store.accept(execution,{"approved":True})
    with pytest.raises(ValueError,match="unknown.*scope"):
        store.accept(execution,{"approved":True,"acceptance_scope":"typo"})


def test_execution_scope_reaches_worker_prompt(tmp_path):
    from AstralNexus.agents.coder import Coder
    from AstralNexus.agents.base import AgentContext
    context=AgentContext(node_id="N",goal="write",metadata={"run_dir":str(tmp_path),"acceptance_scope":"execution"})
    prompt=Coder()._build_runtime_prompt(context,"code_or_script_execution")
    assert "模型生成的节点业务条款用于诊断" in prompt
    assert "失败的实际验证必须修复或如实上报" in prompt


def test_explicit_goal_bootstraps_one_lead_without_planner(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_TOOL_APPROVAL", "true")
    from types import SimpleNamespace
    from AstralNexus.core.run_application import RunApplicationService
    from AstralNexus.tools.executor import ToolExecutor
    class Client:
        def stream_chat(self,messages,**kwargs):
            assert kwargs["max_tool_rounds"] is None
            assert {"runtime_dependencies", "skills_list", "http_request", "lsp_query", "mcp_servers"} <= {t["function"]["name"] for t in kwargs["tools"]}
            executor=ToolExecutor(workspace_root=kwargs["workspace_root"],owned_paths=kwargs["owned_paths"],allowed_tools={s["function"]["name"] for s in kwargs["tools"]})
            assert executor.execute_tool("file_write",{"path":"outputs/hello.txt","content":"hello"})["status"]=="success"
            assert executor.execute_tool("present_files",{"paths":["outputs/hello.txt"]})["status"]=="success"
            return "Created and presented hello."
        def chat(self,*args,**kwargs):
            return json.dumps({"satisfied":True,"relied_on_assumption":False,"blocker":""})
    def forbidden(**kwargs):
        raise AssertionError("Goal must not be forced through a planner graph")
    states=[]
    engine=SimpleNamespace(run=forbidden,record_application_state=lambda p:states.append(p) or True)
    pipeline=FinalDeliveryPipelineForTest(_Runtime());pipeline.client=Client()
    result=RunApplicationService(engine,pipeline=pipeline).execute(task_content="/goal create hello",run_dir=tmp_path)
    assert result["status"]=="completed"
    assert result["execution"]["status"]=="not_run" and result["execution"]["mode"]=="direct_goal"
    assert states[0]["execution_status"]=="running" and states[-1]["execution_status"]=="completed"
    assert states[-1]["phase"]=="completed" and not pipeline.goal_state.active
    assert (tmp_path/"outputs/hello.txt").read_text()=="hello"


def test_application_cancel_records_terminal_state_and_keeps_goal(tmp_path):
    from types import SimpleNamespace
    from AstralNexus.core.run_application import RunApplicationService
    runtime=_Runtime();runtime.cancel_requested=lambda:True
    pipeline=FinalDeliveryPipelineForTest(runtime)
    states=[]
    engine=SimpleNamespace(record_application_state=lambda p:states.append(p) or True)
    result=RunApplicationService(engine,pipeline=pipeline).complete(execution={"status":"not_run","mode":"direct_goal"},task_content="hello",run_dir=tmp_path,goal="hello")
    assert result["status"]=="interrupted" and result["stop_code"]=="user_interrupt"
    assert states[-1]["run_status"]=="interrupted" and states[-1]["goal_state"]["active"]
    assert ExecutionStore(tmp_path).read_delivery("R")["satisfied"]
    assert not (tmp_path/"deliverables/current.json").exists()


def test_cancel_after_native_publish_preserves_fact_without_clearing_goal(tmp_path):
    from types import SimpleNamespace
    from AstralNexus.core.run_application import RunApplicationService
    from AstralNexus.core.exceptions import InterruptionRequested
    runtime=_Runtime();cancel=[False];runtime.cancel_requested=lambda:cancel[0]
    class Pipeline(FinalDeliveryPipelineForTest):
        def _execute(self,**kwargs):
            super()._execute(**kwargs)
            cancel[0]=True
            raise InterruptionRequested("cancel after publication")
    class Client(_PresentClient):
        def chat(self,*args,**kwargs):
            return json.dumps({"satisfied":True,"relied_on_assumption":False,"blocker":""})
    pipeline=Pipeline(runtime);pipeline.client=Client();pipeline.begin(tmp_path)
    app=tmp_path/"deliverables/app";app.mkdir(parents=True);(app/"main.py").write_text("pass")
    states=[]
    result=RunApplicationService(SimpleNamespace(record_application_state=lambda p:states.append(p) or True),pipeline=pipeline).complete(
        execution={"status":"completed"},task_content="task",run_dir=tmp_path,goal="finish")
    assert result["status"]=="interrupted" and result["publication_status"]=="published"
    assert result["delivery_status"]=="passed" and pipeline.goal_state.active
    assert states[-1]["goal_state"]["active"]


def test_cli_renders_completed_application_without_retired_report_fields(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from AstralNexus.cli.main import _run_task
    from AstralNexus.core.run_application import RunApplicationService
    task=tmp_path/"goal.md";task.write_text("create hello",encoding="utf-8")
    calls=[]
    def completed(self,**kwargs):
        calls.append(kwargs)
        return {"status":"completed","execution":{"status":"not_run","mode":"direct_goal"},
                "execution_status":"completed","delivery_status":"passed",
                "artifacts":{"summary_json":str(tmp_path/"summary.json"),"acceptance_report":str(tmp_path/"present.md")}}
    monkeypatch.setattr(RunApplicationService,"execute",completed)
    args=SimpleNamespace(task=str(task),output=str(tmp_path/"run"),model="qwen-plus",provider="dashscope",max_parallel=1,config=None,goal="create hello")
    assert _run_task(args,tmp_path/"home")==0
    assert calls[0]["task_content"]=="/goal create hello"


def test_session_input_read_is_mapped_without_granting_write(tmp_path):
    from AstralNexus.tools.executor import ToolExecutor
    run=tmp_path/"run"; run.mkdir()
    outputs=run/"outputs"; outputs.mkdir()
    workspace=tmp_path/"workspace"; (workspace/"data").mkdir(parents=True)
    source=workspace/"data"/"fixture.csv"; source.write_text("original")
    with presentation_scope(Presentation(run,outputs,workspace=workspace)):
        executor=ToolExecutor(workspace_root=run,owned_paths=["outputs"],allowed_tools={"file_read","file_write"})
        assert executor.execute_tool("file_read",{"path":"data/fixture.csv"})["status"]=="success"
        assert executor.execute_tool("file_read",{"path":str(source)})["status"]=="success"
        assert executor.execute_tool("file_write",{"path":str(source),"content":"changed"})["status"]=="error"
        from AstralNexus.runtime.presentation import mapped_input_path
        with pytest.raises(ValueError): mapped_input_path("../workspace/data/fixture.csv")
        (run/"data").mkdir(); (run/"data/fixture.csv").write_text("local")
        assert mapped_input_path("data/fixture.csv")==run/"data/fixture.csv"
    assert source.read_text()=="original"


def test_goal_evidence_keeps_execution_before_later_report_writes(tmp_path):
    from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
    store=ExecutionStore(tmp_path)
    execution=store.begin("unit-test", {"owned_paths": [], "acceptance_criteria": []})
    with store._connect() as db:
        for index in range(31):
            payload={"tool":"terminal_exec" if index==0 else "file_write", "status":"success", "returncode":0, "stdout_tail":"Ran 5 tests\nOK" if index==0 else ""}
            db.execute("INSERT INTO receipts(id, execution_id, payload) VALUES(?,?,?)",(str(index),execution,json.dumps(payload)))
    visible=FinalDeliveryPipeline(_Runtime())._visible_evidence(tmp_path,"a report claims 100 tests")
    assert visible["execution_receipt_counts"]=={"terminal_exec":1}
    assert visible["tools"][0]["tool"]=="terminal_exec"
    assert "Ran 5 tests" in visible["tools"][0]["stdout"]
