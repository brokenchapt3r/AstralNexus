"""Execution paths share enabled tools without bypassing explicit caller scope."""
import json
from AstralNexus.tools.assembly import assemble_tools, capability_guidance
from AstralNexus.tools.registry import registry

def names(tools):
    return {t["function"]["name"] for t in tools}

def test_lead_exposes_every_registered_enabled_tool(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_DISABLED_TOOLSETS", "[]")
    tools=assemble_tools()
    assert names(tools)=={n for n in registry.get_all_tool_names() if registry.is_tool_available(n)}
    assert {"runtime_dependencies","http_request","skills_list","skill_view","mcp_servers","lsp_query","workflow_submit"} <= names(tools)

def test_disabled_toolsets_stay_disabled_in_schema_and_guidance(monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_DISABLED_TOOLSETS", '["skills","mcp"]')
    tools=assemble_tools()
    assert not {"skills_list","skill_view","mcp_call"} & names(tools)
    assert "skills_list" not in capability_guidance(tools)
    assert "mcp_call" not in capability_guidance(tools)

def test_worker_scope_is_explicit_and_cannot_present():
    assert names(assemble_tools(candidates=["file_read","present_files","present","delegate_task","unknown"],lead=False))=={"file_read"}

def test_managed_run_host_tools_bind_original_session_workspace(tmp_path,monkeypatch):
    from AstralNexus.runtime.presentation import Presentation,presentation_scope
    from AstralNexus.tools.workbench_tools import _session
    workspace=tmp_path/"workspace";workspace.mkdir()
    run=tmp_path/"run";run.mkdir();outputs=run/"outputs";outputs.mkdir()
    monkeypatch.delenv("ASTRALNEXUS_CHAT_SESSION",raising=False)
    with presentation_scope(Presentation(run,outputs,workspace=workspace)):
        assert _session({"sessions":[{"id":"right","workspace":str(workspace)}],"selected":"wrong"})=="right"

def test_worker_does_not_hide_authorized_tools_by_goal_keywords(tmp_path,monkeypatch):
    from AstralNexus.agents.base import AgentContext
    from AstralNexus.agents.resource_broker import ResourceBroker
    from AstralNexus.runtime.llm_client import LLMClient
    monkeypatch.setenv("ASTRALNEXUS_API_KEY","test-key")
    captured=[]
    def chat(client,messages,**kwargs):
        captured.append(names(kwargs["tools"]))
        return "Read-only task complete."
    monkeypatch.setattr(LLMClient,"stream_chat",chat)
    ctx=AgentContext(node_id="assembly",goal="Inspect dependencies",tools=["runtime_dependencies","skills_list","skill_view"],metadata={"run_dir":str(tmp_path),"worker_profile":{"tools":["runtime_dependencies","skills_list","skill_view"]}})
    result=ResourceBroker().try_execute_with_runtime(ctx,task_kind="test")
    assert result.outcome.value=="succeeded"
    assert captured[0]=={"runtime_dependencies","skills_list","skill_view"}


def test_read_tools_agree_on_session_input_directories(tmp_path):
    from AstralNexus.runtime.presentation import Presentation,presentation_scope
    from AstralNexus.tools.executor import ToolExecutor
    workspace=tmp_path/"workspace";app=workspace/"previous_draft/app";app.mkdir(parents=True)
    (app/"app.py").write_text("def example(): pass")
    run=tmp_path/"run";run.mkdir();outputs=run/"outputs";outputs.mkdir()
    with presentation_scope(Presentation(run,outputs,workspace=workspace)):
        executor=ToolExecutor(workspace_root=run,owned_paths=["outputs"])
        assert executor.execute_tool("file_read",{"path":str(app/"app.py")})["status"]=="success"
        for path in (str(app), "previous_draft/app"):
            found=executor.execute_tool("glob_files",{"path":path,"pattern":"*.py"})
            assert found["status"]=="success" and found["files"]==["app.py"],found
            searched=executor.execute_tool("search_files",{"path":path,"query":"example"})
            assert searched["status"]=="success" and searched.get("matches"),searched
        searched=executor.execute_tool("search_files",{"path":str(app/"app.py"),"query":"example"})
        assert searched["status"]=="success" and searched["scanned_files"]==1
        assert len(searched["matches"])==1
        missing=executor.execute_tool("search_files",{"path":str(app/"missing.py"),"query":"example"})
        assert missing["status"]=="error"

def test_error_boolean_keeps_message_and_original_flag(tmp_path):
    from AstralNexus.tools.executor import ToolExecutor
    error=ToolExecutor(workspace_root=tmp_path)._annotate_failure("glob_files",{"status":"error","error":True,"message":"path is outside the workspace"})
    assert error["error"]=="path is outside the workspace"
    assert error["error_flag"] is True
